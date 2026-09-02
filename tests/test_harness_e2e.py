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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from hermes_pipeline import harness as harness_mod
from hermes_pipeline.github_issues import LABEL_VOCABULARY
from hermes_pipeline.harness import HarnessCleanupError, SandboxRepo, run_harness
from hermes_pipeline.phases import load_phases, resolve_profile_phases_path
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


def _serve_github(fake_gh, *, title: str) -> None:
    """Serve a healthy sandbox: auth, viewer, repo view, labels, issue create/view, PR listing/view, closes."""
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
            numbers = [{"number": _PR}] if f"head=acme:{pr_head}&" in path else []
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
        _serve_github(fake_gh, title=harness_mod._issue_title(_RUN_TOKEN))

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
