"""Integration tests — the live flow driven end-to-end through run_harness.

Exercises the live orchestration (GitHub preflight, sandbox clone and seed check,
baseline, issue + plan commit, production tick, card polling, PR verification,
fail-closed shutdown, report) against a local bare Git remote standing in for the
sandbox. ``gh`` is served by the ``fake_gh`` recorder, the production tick is
replaced by a runner that persists the tick state and pushes the agent's branch,
and the kanban is scripted: phase statuses advance one step per poll and the
archived-inclusive snapshot ``shutdown_run`` verifies against is a fixed list, so
board quiescence is asserted by construction, not observed. No Hermes, Claude, or
network calls happen.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from hermes_pipeline import harness as harness_mod
from hermes_pipeline.github_issues import (
    LABEL_VOCABULARY,
    issue_from_api,
    render_issue_body,
)
from hermes_pipeline.harness import HarnessCleanupError, SandboxRepo, run_harness
from hermes_pipeline.phases import load_phases, resolve_profile_phases_path
from hermes_pipeline.run_registration import register_pinned_run
from tests.gh_fakes import API_ARGV, issue_payload

_RUN_TOKEN = "e2e00000"
_ISSUE = 42
_PR = 11
_TICK_ID = "01E2ETICK"
_REPO = "acme/sandbox"
_BRANCH = f"feat/harness-{_RUN_TOKEN}"
_PR_VIEW_FIELDS = "number,state,mergedAt,headRefName,baseRefName,author,createdAt,isCrossRepository,title,body"
_KEYS = [phase.phase_key for phase in load_phases(resolve_profile_phases_path("gstack"))]


def _git(*args: str, cwd: Path) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": "/dev/null"}
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _make_bare_remote(tmp_path: Path, files: dict[str, str]) -> Path:
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git("init", "--bare", "-b", "main", cwd=bare)
    work = tmp_path / "seed"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    _git("config", "user.email", "seed@localhost", cwd=work)
    _git("config", "user.name", "Seed", cwd=work)
    for rel, content in files.items():
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    _git("add", ".", cwd=work)
    _git("commit", "-m", "chore(harness): seed sandbox", cwd=work)
    _git("remote", "add", "origin", f"file://{bare}", cwd=work)
    _git("push", "origin", "main", cwd=work)
    return bare


def _remote_branches(bare: Path) -> list[str]:
    return _git("for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=bare).splitlines()


def _kanban_task(tick_id: str, phase_key: str, status: str) -> dict[str, object]:
    header = {"tick_id": tick_id, "phase_key": phase_key, "todo_id": f"TODO-{_ISSUE}"}
    return {"id": f"task-{phase_key}", "status": status, "body": json.dumps(header) + "\nbody"}


def _serve_github(fake_gh, *, title: str, pr_exists: Callable[[], bool] = lambda: True) -> None:
    """Serve a healthy sandbox: auth, viewer, repo view, labels, issue create/view, PR listing/view, closes.

    The run's PR is listed for its head branch only while ``pr_exists()`` holds, so a
    fixture can tie the PR's existence to the branch actually reaching the remote.
    """
    fake_gh.on("gh", "auth", "status")
    fake_gh.on(*API_ARGV, "user", "--jq", ".login", stdout="octo\n")
    fake_gh.on(
        "gh", "repo", "view", _REPO, "--json", "viewerPermission,defaultBranchRef",
        "--jq", harness_mod._REPO_VIEW_JQ,
        stdout=json.dumps({"permission": "WRITE", "default_branch": "main"}) + "\n",
    )
    fake_gh.on(
        "gh", "label", "list", "--repo", _REPO,
        stdout=json.dumps([{"name": name} for name, _, _ in LABEL_VOCABULARY]),
    )
    fake_gh.on("gh", "label", "create", "--repo", _REPO)
    listing_state = {"created": False, "lag": 1}

    def create_issue(argv):
        listing_state["created"] = True
        return 0, f"https://github.com/{_REPO}/issues/{_ISSUE}\n", ""

    fake_gh.on("gh", "issue", "create", handler=create_issue)
    fake_gh.on(
        *API_ARGV, f"repos/{_REPO}/issues/{_ISSUE}",
        stdout=json.dumps(issue_payload(_ISSUE, title=title)),
    )
    pr_head = quote(_BRANCH, safe="")

    def paginated(argv):
        path = argv[-1]
        if path.startswith(f"repos/{_REPO}/issues?"):
            # GitHub's label-filtered listing lags a fresh create: empty before the
            # create, empty once more right after it, then the issue appears as ready.
            if not listing_state["created"] or listing_state["lag"] > 0:
                if listing_state["created"]:
                    listing_state["lag"] -= 1
                return 0, json.dumps([[]]), ""
            ready = issue_payload(_ISSUE, title=title, labels=["tpo:todo", "ready-for-agent"])
            return 0, json.dumps([[ready]]), ""
        if path.startswith(f"repos/{_REPO}/pulls?"):
            numbers = [{"number": _PR}] if f"head=acme:{pr_head}&" in path and pr_exists() else []
            return 0, json.dumps([numbers]), ""
        if path.startswith("search/issues?"):
            return 0, json.dumps([{"total_count": 0, "incomplete_results": False, "items": []}]), ""
        return 1, "", f"fake gh: unexpected api path {path}"

    fake_gh.on(*API_ARGV, "--paginate", "--slurp", handler=paginated)
    created_at = (datetime.now(UTC) + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake_gh.on(
        "gh", "pr", "view", str(_PR), "--repo", _REPO, "--json", _PR_VIEW_FIELDS,
        stdout=json.dumps({
            "number": _PR, "state": "OPEN", "mergedAt": None, "headRefName": _BRANCH, "baseRefName": "main",
            "author": {"login": "octo"}, "createdAt": created_at, "isCrossRepository": False,
            "title": f"[harness {_RUN_TOKEN}] normalize names", "body": "",
        }),
    )
    fake_gh.on("gh", "issue", "close")
    fake_gh.on("gh", "pr", "close")


@pytest.fixture
def scripted_kanban(mocker):
    """Cards advance ready -> running -> done one step per status poll.

    ``snapshot`` is the archived-inclusive list ``shutdown_run`` verifies quiescence
    against; tests mutate it before running to script a non-quiescent board.
    """
    board = dict.fromkeys(_KEYS, "ready")
    polls = {"n": 0}

    def status(*_args, **_kwargs):
        polls["n"] += 1
        if polls["n"] == 2:
            board.update(dict.fromkeys(_KEYS, "running"))
        elif polls["n"] >= 3:
            board.update(dict.fromkeys(_KEYS, "done"))
        return dict(board)

    snapshot = [_kanban_task(_TICK_ID, key, "archived") for key in _KEYS]
    mocker.patch("hermes_pipeline.harness._kanban_preflight")
    mocker.patch("hermes_pipeline.harness.time.sleep")
    mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
    mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
    mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=status)
    cancel = mocker.patch("hermes_pipeline.harness._cancel_registered_tasks", return_value=True)
    mocker.patch("hermes_pipeline.kanban_tasks._list_task_snapshot", side_effect=lambda _tenant: list(snapshot))
    return SimpleNamespace(board=board, cancel=cancel, snapshot=snapshot)


class _LiveSandbox:
    """A seeded bare remote, a fake ``gh``, a fake tick runner, and a hermetic workspace."""

    def __init__(self, tmp_path: Path, monkeypatch, fake_gh) -> None:
        self.bare = _make_bare_remote(tmp_path, harness_mod._SANDBOX_SEED_FILES)
        self.sandbox = SandboxRepo(repo=_REPO, slug="sandbox", url=f"file://{self.bare}")
        self.tmp_root = tmp_path / "hermes-tmp"
        self.workspace = self.tmp_root / "harness-run"
        self.project_dir = self.workspace / "projects" / "sandbox"
        self.removed: list[Path] = []
        self.tick_calls: list[dict] = []
        self.fake_gh = fake_gh

        monkeypatch.setattr(harness_mod, "preflight_check", lambda **_kwargs: None)
        monkeypatch.setattr(harness_mod, "resolve_sandbox_repo", lambda *_a, **_k: self.sandbox)
        monkeypatch.setattr(harness_mod, "_run_token", lambda: _RUN_TOKEN)
        monkeypatch.setattr(harness_mod, "_harness_tmp_root", lambda: self.tmp_root)
        real_mkdtemp = harness_mod.tempfile.mkdtemp

        def workspace_mkdtemp(prefix=None, dir=None, **kwargs):
            # Only the workspace allocation is redirected; provenance/staging dirs stay real.
            if dir is not None and Path(dir) == self.tmp_root:
                return str(self.workspace)
            return real_mkdtemp(prefix=prefix, dir=dir, **kwargs)

        monkeypatch.setattr(harness_mod.tempfile, "mkdtemp", workspace_mkdtemp)
        real_rmtree = harness_mod.shutil.rmtree

        def recording_rmtree(path, **kwargs):
            self.removed.append(Path(path))
            if Path(path) != self.workspace:  # keep the workspace inspectable
                real_rmtree(path, **kwargs)

        monkeypatch.setattr(harness_mod.shutil, "rmtree", recording_rmtree)
        monkeypatch.setattr(harness_mod, "_tick_runner", self._fake_tick)
        _serve_github(
            fake_gh,
            title=harness_mod._issue_title(_RUN_TOKEN),
            pr_exists=lambda: _BRANCH in _remote_branches(self.bare),
        )

    def _fake_tick(self, argv, *, cwd, env, **kwargs):
        """Stand in for ``tpo tick``: persist the registration, then act as the agent."""
        self.tick_calls.append({"argv": argv, "cwd": Path(cwd), "config": env.get("TPO_CONFIG_FILE")})
        state = self.project_dir / ".hermes"
        outcomes = state / "outcomes"
        outcomes.mkdir(parents=True, exist_ok=True)
        (state / "current_tick_id.txt").write_text(_TICK_ID + "\n")
        (outcomes / f"{_TICK_ID}-phases.json").write_text(json.dumps({"outcome": "tick_started"}) + "\n")
        (outcomes / "expected-phases.json").write_text(json.dumps(_KEYS))
        (state / "pipeline_branch.txt").write_text(_BRANCH + "\n")
        _git("checkout", "-b", _BRANCH, cwd=self.project_dir)
        (self.project_dir / "mock_transform.py").write_text(
            "def normalize_names(names):\n    return [n.strip().lower() for n in names if n.strip()]\n"
        )
        _git("add", "mock_transform.py", cwd=self.project_dir)
        _git("commit", "-m", "feat: add normalize_names mock transform", cwd=self.project_dir)
        _git("push", "origin", _BRANCH, cwd=self.project_dir)
        return SimpleNamespace(returncode=0, stdout="tick ok\n", stderr="")

    def run(self, **overrides):
        kwargs = dict(
            fixture_name="happy-path", repo=_REPO, loop=False, keep_dir=False,
            timeout=60, convergence_threshold=3, config=None, profile_name="gstack",
        )
        kwargs.update(overrides)
        return run_harness(**kwargs)

    def gh_verbs(self) -> list[tuple[str, str]]:
        return [tuple(call[:2]) for call in self.fake_gh.gh_calls()]


@pytest.fixture
def live(tmp_path, monkeypatch, fake_gh):
    return _LiveSandbox(tmp_path, monkeypatch, fake_gh)


def test_happy_path_live_flow_with_local_bare_remote(live, capsys, scripted_kanban):
    result = live.run()

    assert result.exit_code == 0, result.summary
    assert result.issue_number == _ISSUE
    assert result.pr_numbers == (_PR,)
    assert result.cleanup_leftovers == ()
    assert result.temp_dir is None
    assert live.removed[-1] == live.workspace  # earlier rmtree calls are cleanup_remote's staging/provenance dirs
    assert live.removed.count(live.workspace) == 1
    assert all(path.is_relative_to(live.workspace / "artifacts") for path in live.removed[:-1])
    assert live.workspace.parent == live.tmp_root

    # The production tick ran inside the isolated config against the cloned sandbox.
    (tick,) = live.tick_calls
    assert tick["argv"][-2:] == ["tick", "sandbox"]
    assert tick["cwd"] == live.workspace
    assert tick["config"] == str(live.workspace / "state" / "tpo-config.yaml")
    assert "TPO_CONFIG_FILE" not in os.environ
    assert (live.project_dir / ".hermes" / "pipeline.toml").is_file()
    assert (live.project_dir / f"docs/harness/{_RUN_TOKEN}-plan.md").is_file()

    # Report and events carry the live identity.
    assert result.report_path is not None and result.report_path.is_file()
    report = json.loads(result.report_path.read_text())
    assert report["profile"] == "gstack"
    assert report["fixture_name"] == "happy-path"
    assert {phase["phase_key"] for phase in report["phases"]} == set(_KEYS)
    assert all(phase["status"] == "completed" for phase in report["phases"])
    events = [json.loads(line) for line in (live.workspace / "artifacts" / "events.jsonl").read_text().splitlines()]
    (started,) = [event for event in events if event["event_type"] == "run_started"]
    assert started["repo"] == _REPO
    assert started["issue_number"] == _ISSUE
    assert started["run_token"] == _RUN_TOKEN
    (finished,) = [event for event in events if event["event_type"] == "run_finished"]
    assert finished["pr_numbers"] == [_PR]
    assert finished["remote_all_ok"] is True
    assert f"repo={_REPO} issue=#{_ISSUE} pr=#{_PR}" in capsys.readouterr().out
    assert f"{len(_KEYS)}/{len(_KEYS)} phases passed" in result.summary

    # Shutdown cancelled the tick, then cleaned the remote: branch gone, issue and PR closed.
    scripted_kanban.cancel.assert_called_once()
    assert _remote_branches(live.bare) == ["main"]
    gh_calls = live.fake_gh.gh_calls()
    assert ["issue", "close", str(_ISSUE), "--repo", _REPO, "--reason", "completed"] in gh_calls
    assert [call[:5] for call in gh_calls if call[:2] == ["pr", "close"]] == [
        ["pr", "close", str(_PR), "--repo", _REPO]
    ]
    verbs = live.gh_verbs()
    assert verbs.index(("issue", "create")) < verbs.index(("pr", "view")) < verbs.index(("issue", "close"))


def test_non_quiescent_board_closes_issue_but_deletes_nothing(live, monkeypatch, scripted_kanban):
    """One card still live after cancel: fail closed — issue closed, branch and PR left, workspace kept."""
    scripted_kanban.snapshot[0] = _kanban_task(_TICK_ID, _KEYS[0], "running")
    real_shutdown = harness_mod.shutdown_run
    monkeypatch.setattr(
        harness_mod, "shutdown_run",
        lambda *a, **k: real_shutdown(*a, quiescence_timeout=0.0, poll_interval=0.01, **k),
    )

    with pytest.raises(HarnessCleanupError) as exc_info:
        live.run()

    message = str(exc_info.value)
    assert "could not be confirmed for tick 01E2ETICK" in message
    assert str(live.workspace) in message
    assert "branch/PR cleanup skipped" in message
    scripted_kanban.cancel.assert_called_once()
    assert live.workspace not in live.removed
    assert live.workspace.is_dir()
    assert _remote_branches(live.bare) == [_BRANCH, "main"]
    gh_calls = live.fake_gh.gh_calls()
    assert ["issue", "close", str(_ISSUE), "--repo", _REPO, "--reason", "completed"] in gh_calls
    assert not any(call[:2] == ["pr", "close"] for call in gh_calls)
    # Pre-shutdown PR verification did run; only the destructive cleanup was skipped.
    assert ("pr", "view") in live.gh_verbs()


def test_keep_dir_touches_nothing_remote_and_prunes_only_the_config(live, scripted_kanban):
    result = live.run(keep_dir=True)

    assert result.exit_code == 0, result.summary
    assert result.temp_dir == live.workspace
    assert result.pr_numbers == (_PR,)
    assert result.cleanup_leftovers == (
        f"kept remote artifacts for issue #{_ISSUE} in {_REPO} (run {_RUN_TOKEN})",
    )
    assert live.workspace not in live.removed
    scripted_kanban.cancel.assert_called_once()  # workers are still stopped
    assert _remote_branches(live.bare) == [_BRANCH, "main"]
    verbs = live.gh_verbs()
    assert ("issue", "close") not in verbs
    assert ("pr", "close") not in verbs
    assert not (live.workspace / "state" / "tpo-config.yaml").exists()
    assert (live.project_dir / ".hermes" / "pipeline_branch.txt").read_text() == _BRANCH + "\n"
    assert (live.project_dir / ".hermes" / "pipeline.toml").is_file()


# -- native-sdd: one registration, several ticks ------------------------------------
#
# The compiled-plan profile is not driven to completion by a single ``tpo tick``:
# the first tick registers the run and its ``plan:`` worker cards, and each
# later tick reconciles finished cards into the next stage (review, finish,
# human-gate). The harness therefore has to keep ticking the same run until the
# board is quiescent and the human gate stands, and to fail closed when a tick
# changes nothing.

_NATIVE_SDD = "native-sdd"
_PLAN_PATH = f"docs/harness/{_RUN_TOKEN}-plan.md"
_STEP_KEYS = ("plan:task-1",)
_REGISTRATION_BARRIER = "__registration_barrier__"


@pytest.fixture
def native_sdd_kanban(mocker, monkeypatch):
    """A board the fake ticks populate; every poll advances a card exactly one step.

    ``todo -> ready -> running -> done``: a freshly registered card therefore takes
    several polls to settle, so a poller that stopped at the first non-empty board
    would report one that is not settled. ``blocked`` cards only move when a tick
    moves them (that is the reconciler's job), so a tick that changes nothing leaves
    the board stalled. ``snapshot()`` is the list ``shutdown_run`` verifies quiescence
    against: archived for every board key unless a test puts a live status in
    ``snapshot_overrides``.
    """
    board: dict[str, str] = {}
    snapshot_overrides: dict[str, str] = {}

    def status(*_args, **_kwargs):
        # Most-advanced first, so one call moves a card one step and no card skips a
        # status: done <- running <- ready <- todo.
        for key, value in list(board.items()):
            if value == "running":
                board[key] = "done"
        for key, value in list(board.items()):
            if value == "ready":
                board[key] = "running"
        for key, value in list(board.items()):
            if value == "todo":
                board[key] = "ready"
        return dict(board)

    def snapshot(_tenant):
        return [
            _kanban_task(_TICK_ID, key, snapshot_overrides.get(key, "archived")) for key in board
        ]

    mocker.patch("hermes_pipeline.harness._kanban_preflight")
    mocker.patch("hermes_pipeline.harness.time.sleep")
    mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
    mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
    mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=status)
    cancel = mocker.patch("hermes_pipeline.harness._cancel_registered_tasks", return_value=True)
    mocker.patch("hermes_pipeline.kanban_tasks._list_task_snapshot", side_effect=snapshot)
    # Polls block on a real Event.wait (harness.time.sleep is patched, but the pinned
    # poller waits on the cancel event); keep the multi-tick run fast.
    fast = {"poll_interval": 0.01, "max_poll_interval": 0.05}
    for name in ("poll_pinned_run",):
        real_poll = getattr(harness_mod, name)
        monkeypatch.setattr(
            harness_mod, name,
            lambda *a, _real=real_poll, **k: _real(*a, **{**fast, **k}),
        )
    return SimpleNamespace(board=board, cancel=cancel, snapshot_overrides=snapshot_overrides)


class _NativeSddSandbox(_LiveSandbox):
    """``_LiveSandbox`` whose fake tick plays the native-sdd tick sequence on ``board``.

    Call 1 runs the REAL ``register_pinned_run`` against the clone (the harness has
    already committed the Plan at HEAD) and acts as the plan worker inside the run
    worktree. Later calls act as the reconciler: they only move cards. ``script``
    maps the call number to that call's effect; unscripted calls change nothing.
    """

    def __init__(self, tmp_path, monkeypatch, fake_gh, board: dict[str, str], *, script) -> None:
        super().__init__(tmp_path, monkeypatch, fake_gh)
        self.board = board
        self.script = script
        self.worktree: Path | None = None

    def _fake_tick(self, argv, *, cwd, env, **kwargs):
        self.tick_calls.append({"argv": argv, "cwd": Path(cwd), "config": env.get("TPO_CONFIG_FILE")})
        call = len(self.tick_calls)
        if call == 1:
            self._register()
        else:
            self.script.get(call, lambda _sandbox: None)(self)
        return SimpleNamespace(returncode=0, stdout=f"tick {call} ok\n", stderr="")

    def _register(self) -> None:
        state = self.project_dir / ".hermes"
        title = harness_mod._issue_title(_RUN_TOKEN)
        body = render_issue_body(
            harness_mod._issue_fields(branch=_BRANCH, plan_path=_PLAN_PATH), include_empty=False
        )
        payload = issue_payload(
            _ISSUE, title=title, body=body, html_url=f"https://github.com/{_REPO}/issues/{_ISSUE}"
        )
        registration = register_pinned_run(
            project_dir=self.project_dir,
            state_dir=state,
            tick_id=_TICK_ID,
            selected_issue=issue_from_api(payload, repo=_REPO),
            plan_path=_PLAN_PATH,
            profile=_NATIVE_SDD,
            prompt_client="claude",
            assignee="pipeline",
            review_assignee=None,
            step_keys=_STEP_KEYS,
            repo=_REPO,
        )
        self.worktree = registration.worktree
        assert registration.branch == _BRANCH
        outcomes = state / "outcomes"
        outcomes.mkdir(parents=True, exist_ok=True)
        (state / "current_tick_id.txt").write_text(_TICK_ID + "\n")
        (outcomes / f"{_TICK_ID}-phases.json").write_text(json.dumps({"outcome": "tick_started"}) + "\n")
        run_outcomes = registration.worktree / ".hermes" / "outcomes"
        run_outcomes.mkdir(parents=True, exist_ok=True)
        (run_outcomes / "expected-phases.json").write_text(json.dumps(list(_STEP_KEYS)))
        # A freshly registered worker card starts in todo; no gate stands behind it.
        self.board.update({_REGISTRATION_BARRIER: "done", "plan:task-1": "todo"})
        # The plan worker: one atomic task commit on the run branch, not pushed yet.
        (registration.worktree / "mock_transform.py").write_text(
            "def normalize_names(names):\n    return [n.strip().lower() for n in names if n.strip()]\n"
        )
        (registration.worktree / "tests" / "test_mock_transform.py").write_text(
            "from mock_transform import normalize_names\n\n\n"
            "def test_normalize_names():\n"
            "    assert normalize_names([' Alice ', '', 'BOB']) == ['alice', 'bob']\n"
            "    assert normalize_names([]) == []\n"
        )
        _git("add", "mock_transform.py", "tests/test_mock_transform.py", cwd=registration.worktree)
        _git("commit", "-m", "feat: add normalize_names mock transform", cwd=registration.worktree)

    def push_branch(self) -> None:
        assert self.worktree is not None
        _git("push", "origin", _BRANCH, cwd=self.worktree)

    def run(self, **overrides):
        overrides.setdefault("profile_name", _NATIVE_SDD)
        return super().run(**overrides)


def _happy_native_sdd_script(*, verified: bool = True) -> dict[int, Callable[[_NativeSddSandbox], None]]:
    """The reconciler hops of a delivered run: review, finish, then the human gate.

    With ``verified=False`` tick 4 raises the gate exactly as the happy path does but
    ``todos_completion`` never writes its ``finish-verified`` marker, which is the
    proof of delivery ``classify_pinned_run`` requires.
    """

    def review(sandbox):
        sandbox.board.update({"review:0": "ready", "review-acceptance": "blocked"})

    def finish(sandbox):
        sandbox.board.update({"review-acceptance": "done", "finish": "ready"})

    def human_gate(sandbox):
        sandbox.push_branch()
        sandbox.board["human-gate"] = "blocked"
        if not verified:
            return
        marker = sandbox.project_dir / ".hermes" / "runs" / _TICK_ID / "finish-verified"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(_git("rev-parse", "HEAD", cwd=sandbox.worktree) + "\n")

    return {2: review, 3: finish, 4: human_gate}


@pytest.mark.real_git
def test_native_sdd_multi_tick_live_flow(tmp_path, monkeypatch, fake_gh, native_sdd_kanban):
    live = _NativeSddSandbox(
        tmp_path, monkeypatch, fake_gh, native_sdd_kanban.board, script=_happy_native_sdd_script()
    )

    result = live.run()

    assert result.exit_code == 0, result.summary
    assert result.issue_number == _ISSUE
    assert result.pr_numbers == (_PR,)
    assert result.cleanup_leftovers == ()

    # The same isolated config drove one registration tick and three reconciling ticks.
    assert len(live.tick_calls) == 4
    assert {tick["config"] for tick in live.tick_calls} == {str(live.workspace / "state" / "tpo-config.yaml")}
    assert all(tick["argv"][-2:] == ["tick", "sandbox"] for tick in live.tick_calls)
    assert (live.project_dir / _PLAN_PATH).is_file()

    # The PR is listed for ``head=acme:<branch>`` only once that branch reaches the
    # remote, so a verified PR number means the run's own branch was pushed, and
    # ``verify_pull_request`` accepted it only because it is open and unmerged.
    assert ("pr", "view") in live.gh_verbs()

    # The board the run ends on -- the human gate standing BLOCKED behind a done
    # finish -- is pinned by the per-tick snapshots, and the delivery verdict was
    # taken from the ``finish-verified`` marker written into the run dir.
    events = [
        json.loads(line)
        for line in (live.workspace / "artifacts" / "events.jsonl").read_text().splitlines()
    ]
    boards = [event["status_map"] for event in events if event["event_type"] == "tick_completed"]
    assert len(boards) == 4
    # Every reported board really is settled: complete in the step keys and holding
    # no card still on its way anywhere.
    assert all(
        set(_STEP_KEYS) <= board.keys() and set(board.values()) <= {"done", "blocked", "failed", "archived"}
        for board in boards
    )
    assert boards[-1] == {
        _REGISTRATION_BARRIER: "done",  # fixture-only stand-in for the run's own card
        "plan:task-1": "done",
        "review:0": "done",
        "review-acceptance": "done",
        "finish": "done",
        "human-gate": "blocked",
    }

    # The report's phases are the transitions the poller *observed*. The emitter does
    # handle a card first seen as blocked (``_UNSTARTED_STATUSES`` holds ``None``), so
    # the sole reason ``human-gate`` is absent is the baseline: poll_pinned_run seeds
    # ``previous_status`` from the fetch that opens each tick's poll, and ticks never
    # run during a poll -- so a card a tick created already ``blocked`` (human-gate)
    # or flipped straight to ``done`` (the gates) is in that state before the first
    # comparison and never counts as a change. Emitting for the standing gate would
    # also be wrong here: generate_report scores ``blocked`` as a failed phase, which
    # would report a delivered run as having failed one.
    report = json.loads(result.report_path.read_text())
    assert report["profile"] == _NATIVE_SDD
    assert {phase["phase_key"] for phase in report["phases"]} == {"plan:task-1", "review:0", "finish"}
    assert all(phase["status"] == "completed" for phase in report["phases"])
    assert report["failed_phases"] == 0

    # Shutdown verified the archived-inclusive snapshot and left nothing behind.
    (finished,) = [event for event in events if event["event_type"] == "run_finished"]
    assert finished["kanban_quiescent"] is True
    assert finished["remote_all_ok"] is True
    assert finished["leftovers"] == []

    # Shutdown cancelled the run once, then cleaned the remote: branch gone, issue and PR closed.
    native_sdd_kanban.cancel.assert_called_once()
    assert _remote_branches(live.bare) == ["main"]
    gh_calls = live.fake_gh.gh_calls()
    assert ["issue", "close", str(_ISSUE), "--repo", _REPO, "--reason", "completed"] in gh_calls
    assert [call[:5] for call in gh_calls if call[:2] == ["pr", "close"]] == [
        ["pr", "close", str(_PR), "--repo", _REPO]
    ]
    assert live.removed[-1] == live.workspace
    assert live.removed.count(live.workspace) == 1


@pytest.mark.real_git
def test_native_sdd_stall_fails_closed(tmp_path, monkeypatch, fake_gh, native_sdd_kanban):
    """Ticks after the registration change nothing: the run is stalled, not complete."""
    live = _NativeSddSandbox(tmp_path, monkeypatch, fake_gh, native_sdd_kanban.board, script={})

    result = live.run()

    assert result.exit_code == 1, result.summary
    assert "tick_stalled" in result.summary
    assert result.pr_numbers == ()
    assert result.cleanup_leftovers == ()
    # The registration tick, then exactly one tick that changed nothing: the driver
    # stops on the repeat instead of ticking out its budget.
    assert len(live.tick_calls) == 2

    native_sdd_kanban.cancel.assert_called_once()
    gh_calls = live.fake_gh.gh_calls()
    assert ["issue", "close", str(_ISSUE), "--repo", _REPO, "--reason", "completed"] in gh_calls
    assert not any(call[:2] == ["pr", "close"] for call in gh_calls)
    assert _remote_branches(live.bare) == ["main"]  # nothing was pushed, so nothing to delete
    assert live.removed[-1] == live.workspace  # the board is quiescent, so the workspace goes


@pytest.mark.real_git
def test_native_sdd_gate_without_finish_verified_never_delivers(
    tmp_path, monkeypatch, fake_gh, native_sdd_kanban
):
    """A standing human gate is not delivery: without the marker the run fails closed.

    Tick 4 raises the gate exactly as the delivered run does -- ``finish`` done,
    ``human-gate`` blocked, branch pushed -- but writes no ``finish-verified``, so
    ``classify_pinned_run`` must keep the verdict at ``in_progress`` and the driver
    must end on a fail-closed code rather than reporting a delivered run.
    """
    live = _NativeSddSandbox(
        tmp_path, monkeypatch, fake_gh, native_sdd_kanban.board,
        script=_happy_native_sdd_script(verified=False),
    )

    result = live.run()

    assert result.exit_code == 1, result.summary
    assert "tick_stalled" in result.summary or "tick_budget_exhausted" in result.summary
    # The run never succeeded, so its pushed branch is never promoted to a verified PR.
    assert result.pr_numbers == ()
    assert not (live.project_dir / ".hermes" / "runs" / _TICK_ID / "finish-verified").exists()
    events = [
        json.loads(line)
        for line in (live.workspace / "artifacts" / "events.jsonl").read_text().splitlines()
    ]
    boards = [event["status_map"] for event in events if event["event_type"] == "tick_completed"]
    assert boards[-1]["finish"] == "done"
    assert boards[-1]["human-gate"] == "blocked"

    # Cleanup still ran: the run cancelled once, the issue closed, the pushed branch swept.
    native_sdd_kanban.cancel.assert_called_once()
    gh_calls = live.fake_gh.gh_calls()
    assert ["issue", "close", str(_ISSUE), "--repo", _REPO, "--reason", "completed"] in gh_calls
    assert _remote_branches(live.bare) == ["main"]
    # Shutdown rediscovers the pushed branch's PR and closes it, even though the
    # run never verified one of its own.
    assert [call[:5] for call in gh_calls if call[:2] == ["pr", "close"]] == [
        ["pr", "close", str(_PR), "--repo", _REPO]
    ]
    assert live.removed[-1] == live.workspace


@pytest.mark.real_git
def test_non_quiescent_pinned_board_leaves_branch_and_pr(
    tmp_path, monkeypatch, fake_gh, native_sdd_kanban
):
    """A pinned run whose cards outlive cancel: issue closed, branch and PR left alone."""
    live = _NativeSddSandbox(
        tmp_path, monkeypatch, fake_gh, native_sdd_kanban.board, script=_happy_native_sdd_script()
    )
    # The gate and its finish card are still live when shutdown re-reads the board.
    native_sdd_kanban.snapshot_overrides.update({"finish": "running", "human-gate": "running"})
    real_shutdown = harness_mod.shutdown_run
    monkeypatch.setattr(
        harness_mod, "shutdown_run",
        lambda *a, **k: real_shutdown(*a, quiescence_timeout=0.0, poll_interval=0.01, **k),
    )

    with pytest.raises(HarnessCleanupError) as exc_info:
        live.run()

    message = str(exc_info.value)
    assert f"could not be confirmed for tick {_TICK_ID}" in message
    assert str(live.workspace) in message
    assert "branch/PR cleanup skipped" in message
    native_sdd_kanban.cancel.assert_called_once()
    assert live.workspace not in live.removed
    assert live.workspace.is_dir()
    # Nothing destructive: the delivery branch and its PR survive for a human.
    assert _remote_branches(live.bare) == [_BRANCH, "main"]
    gh_calls = live.fake_gh.gh_calls()
    assert ["issue", "close", str(_ISSUE), "--repo", _REPO, "--reason", "completed"] in gh_calls
    assert not any(call[:2] == ["pr", "close"] for call in gh_calls)
    # Pre-shutdown PR verification did run; only the destructive cleanup was skipped.
    assert ("pr", "view") in live.gh_verbs()
