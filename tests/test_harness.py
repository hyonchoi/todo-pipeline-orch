"""Unit tests for harness.py — fixture factory, preflight, convergence, monitor."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import subprocess
import tomllib
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_pipeline.config import Config
from hermes_pipeline.contract import ContractSchemaError
from hermes_pipeline.github_issues import GitHubIssuesError
from hermes_pipeline.harness import (
    ConvergenceDetector,
    ConvergenceHaltError,
    GitHubPreflight,
    HarnessMonitor,
    HarnessPreflightError,
    HarnessProfileError,
    HarnessResult,
    RunBaseline,
    SandboxRepo,
    _build_harness_profile_data,
    _classify_error_class,
    _ConvergenceMonitor,
    _offline_terminal_phase_key,
    _prune_retained_state,
    _validate_profile_prerequisites,
    _with_offline_terminal_workflow,
    clone_sandbox,
    create_mock_project,
    filter_phases,
    github_preflight,
    isolate_config,
    other_ready_issues,
    preflight_check,
    resolve_sandbox_repo,
    run_harness,
    sandbox_seed_check,
    take_baseline,
    validate_live_profile,
    write_project_contract,
)
from hermes_pipeline.phases import Phase, load_phases, resolve_profile_phases_path
from tests.gh_fakes import API_ARGV, seed_project_issues, todo_payload


class TestCreateMockProject:
    """Test fixture factory creates valid mock projects."""

    def test_create_mock_project_happy_path(self, tmp_path: Path):
        result = create_mock_project(tmp_path, "happy-path")
        assert (tmp_path / ".git").exists()
        assert not (tmp_path / "TODOS.md").exists()
        assert "project_slug" in result
        assert "todo_id" in result
        assert "branch" in result
        assert result["repo"] == "tpo-harness/mock-project"

    def test_create_mock_project_seeds_one_eligible_issue_via_fake_gh(self, tmp_path: Path):
        from hermes_pipeline.github_issues import parse_issue_body

        result = create_mock_project(tmp_path, "happy-path")

        gh = tmp_path / "bin" / "gh"
        assert gh.is_file() and os.access(gh, os.X_OK)
        assert result["gh_bin"] == str(gh)
        state = json.loads(Path(result["gh_state"]).read_text())
        assert state["repo"] == "tpo-harness/mock-project"
        assert list(state["issues"]) == ["1"]
        issue = state["issues"]["1"]
        assert issue["title"] == "Implement mock name normalization"
        assert {label["name"] for label in issue["labels"]} == {"tpo:todo", "ready-for-agent"}
        assert issue["issue_dependencies_summary"]["blocked_by"] == 0
        assert issue["html_url"] == "https://github.com/tpo-harness/mock-project/issues/1"
        sections = parse_issue_body(issue["body"])
        assert sections["Plan"] == ("docs/harness/TODO-1-plan.md",)
        assert sections["Branch"] == ("feat/mock-happy-path",)
        required_contract = (
            "mock_transform.py",
            "normalize_names(names: list[str]) -> list[str]",
            "strip surrounding whitespace",
            "discard empty strings",
            "preserve input order",
            "Return an empty list",
            "standard library only",
            "uv run pytest",
        )
        for requirement in required_contract:
            assert requirement in issue["body"]

    def test_create_mock_project_sets_placeholder_origin(self, tmp_path: Path):
        from hermes_pipeline.github_issues import repository_identity

        create_mock_project(tmp_path, "happy-path")

        assert repository_identity(tmp_path) == "tpo-harness/mock-project"
        push_url = subprocess.run(
            ["git", "remote", "get-url", "--push", "origin"],
            cwd=tmp_path, check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert push_url == "no-push://tpo-harness/mock-project.git"
        push = subprocess.run(
            ["git", "push", "origin", "HEAD"],
            cwd=tmp_path, capture_output=True, text=True, timeout=30,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        assert push.returncode != 0
        helper = subprocess.run(
            ["git", "config", "--local", "credential.helper"],
            cwd=tmp_path, capture_output=True, text=True,
        )
        assert helper.stdout.strip() == ""
        assert helper.returncode == 0

    def test_fake_gh_lists_seeded_issue_through_client(self, tmp_path: Path, monkeypatch):
        from hermes_pipeline.github_issues import list_todo_issues

        result = create_mock_project(tmp_path, "happy-path")
        monkeypatch.setenv("TPO_GH_BIN", result["gh_bin"])
        monkeypatch.setenv("TPO_FAKE_GH_STATE", result["gh_state"])

        issues = list_todo_issues(tmp_path)

        assert [issue.number for issue in issues] == [1]
        assert issues[0].plan_values == ("docs/harness/TODO-1-plan.md",)
        assert issues[0].branch_values == ("feat/mock-happy-path",)
        assert issues[0].blocked_by_open == 0

    def test_create_mock_project_unknown_fixture_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="Unknown fixture"):
            create_mock_project(tmp_path, "nonexistent-fixture")

    def test_create_mock_project_returns_metadata(self, tmp_path: Path):
        result = create_mock_project(tmp_path, "happy-path")
        assert result["project_slug"] == "mock-project"
        assert result["todo_id"] == 1
        assert result["fixture_name"] == "happy-path"

    def test_native_sdd_mock_project_compiles_the_manifest_fan_out(self, tmp_path: Path):
        """The fixture Plan carries a tpo-plan manifest, so native-sdd gets plan:/validate: cards."""
        from hermes_pipeline.harness import _HARNESS_PLAN_PATH
        from hermes_pipeline.kanban_tasks import prepare_todo_phases
        from hermes_pipeline.phases import resolve_profile_phases_path

        create_mock_project(tmp_path, "happy-path", "native-sdd")

        prepared = prepare_todo_phases(
            todo_id="TODO-1", tick_id="01HARNESS", board_slug="mock-project",
            phases_path=resolve_profile_phases_path("native-sdd"), project_dir=tmp_path,
            plan_path=_HARNESS_PLAN_PATH,
        )
        keys = [task.phase_key for task in prepared]
        assert keys == ["plan:task-1", "validate:task-1"]
        assert "phase_4_development" not in keys
        assert "mock_transform.py" in prepared[0].body and "uv run pytest" in prepared[0].body

        gstack = prepare_todo_phases(
            todo_id="TODO-1", tick_id="01HARNESS", board_slug="mock-project",
            phases_path=resolve_profile_phases_path("gstack"), project_dir=tmp_path,
            plan_path=_HARNESS_PLAN_PATH,
        )
        assert [task.phase_key for task in gstack][0] == "phase_2_autoplan"

    def test_poll_converges_on_manifest_cards_not_profile_phases(self, tmp_path: Path, mocker, monkeypatch):
        """Expected/terminal/gate logic must follow the registered plan:/validate: cards."""
        import threading

        import yaml

        from hermes_pipeline.harness import (
            _build_harness_profile_data,
            _offline_terminal_phase_key,
            _poll_kanban_phases,
            _with_offline_terminal_workflow,
        )
        from hermes_pipeline.kanban_tasks import KanbanTaskInfo
        from hermes_pipeline.phases import load_phases, resolve_profile_phases_path

        create_mock_project(tmp_path, "happy-path", "native-sdd")
        profile_path = resolve_profile_phases_path("native-sdd")
        phases = load_phases(profile_path)
        phases = _with_offline_terminal_workflow(phases, _offline_terminal_phase_key(phases))
        harness_yaml = tmp_path / "harness-phases.yaml"
        harness_yaml.write_text(yaml.safe_dump(_build_harness_profile_data(
            yaml.safe_load(profile_path.read_text()), phases,
        )))
        monkeypatch.setattr("hermes_pipeline.harness.time.sleep", lambda *_a, **_k: None)
        mocker.patch("hermes_pipeline.kanban_tasks.create_prepared_todo_phases", return_value=["t1", "t2"])
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        board = {"plan:task-1": "ready", "validate:task-1": "blocked"}
        cancel = threading.Event()
        snapshots = iter([
            dict(board),
            {"plan:task-1": "running", "validate:task-1": "blocked"},
            {"plan:task-1": "done", "validate:task-1": "blocked"},
        ])

        def status(*_a, **_k):
            try:
                snap = next(snapshots)
            except StopIteration:
                snap = dict(board)
                cancel.set()  # under a regression the loop would spin forever
            board.update(snap)
            return dict(board)

        completed = []

        def complete(_tenant, task_id):
            completed.append(task_id)
            board["validate:task-1"] = "done"
            return True

        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=status)
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
            side_effect=lambda *_a: {
                key: KanbanTaskInfo(task_id=f"t_{key}", phase_key=key, status=value, todo_id="TODO-1")
                for key, value in board.items()
            },
        )
        mocker.patch("hermes_pipeline.kanban_tasks.complete_todo_kanban_task", side_effect=complete)
        log_path = tmp_path / "events.jsonl"
        detector = ConvergenceDetector(threshold=99)
        monitor = _ConvergenceMonitor(HarnessMonitor(log_path), detector, {})

        assert _poll_kanban_phases(
            project_slug="mock-project", tick_id="01HARNESS", state_dir=tmp_path / ".hermes",
            todo_id="TODO-1", project_dir=tmp_path, phases_path=harness_yaml,
            monitor=monitor, detector=detector, phases=phases,
            offline_terminal_phase_key="phase_8_finish_branch", cancel_event=cancel,
        ) is True
        assert completed == ["t_validate:task-1"]

    @pytest.mark.parametrize("profile_name", ["gstack", "agent-skills", "native-sdd"])
    def test_create_mock_project_writes_selected_profile_and_plan(
        self, tmp_path: Path, profile_name: str
    ):
        result = create_mock_project(tmp_path, "happy-path", profile_name)

        contract = tomllib.loads((tmp_path / ".hermes" / "pipeline.toml").read_text())
        body = json.loads(Path(result["gh_state"]).read_text())["issues"]["1"]["body"]
        plan_path = tmp_path / "docs" / "harness" / "TODO-1-plan.md"
        assert result["profile"] == profile_name
        assert contract["profile"] == profile_name
        assert set(contract["capabilities"]) == {"Read", "Write", "Edit", "Bash"}
        assert "### Plan\n\ndocs/harness/TODO-1-plan.md\n" in body
        assert plan_path.is_file()
        assert "confirm they fail" in plan_path.read_text().lower()
        assert subprocess.run(
            ["git", "status", "--short"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout == ""

    def test_create_mock_project_omits_harness_owned_and_legacy_state(
        self, tmp_path: Path
    ):
        create_mock_project(tmp_path, "happy-path")

        assert (tmp_path / ".hermes" / "pipeline.toml").exists()
        assert not (tmp_path / ".hermes" / "todo_id_counter").exists()
        assert not (tmp_path / "events.jsonl").exists()
        assert not (tmp_path / "reports").exists()

        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert status == ""

    def test_create_mock_project_ignores_runtime_and_scratch_artifacts(
        self, tmp_path: Path
    ):
        create_mock_project(tmp_path, "happy-path")

        (tmp_path / "events.jsonl").write_text("{}\n")
        (tmp_path / ".hermes" / "tpo-config.yaml").write_text("state_dir: .hermes\n")
        (tmp_path / ".hermes" / "fake-gh-state.json.tmp").write_text("{}\n")
        (tmp_path / ".hermes" / "outcomes").mkdir()
        (tmp_path / ".hermes" / "outcomes" / "expected-phases.json").write_text("{}\n")
        (tmp_path / ".superpowers").mkdir()
        (tmp_path / ".superpowers" / "scratch.md").write_text("scratch\n")
        (tmp_path / ".code-review-graph").mkdir()
        (tmp_path / ".code-review-graph" / "cache.json").write_text("{}\n")
        (tmp_path / "src" / "__pycache__").mkdir(parents=True)
        (tmp_path / "src" / "__pycache__" / "cache.py").write_text("cache = True\n")
        (tmp_path / "compiled.pyc").write_bytes(b"cache")
        (tmp_path / "optimized.pyo").write_bytes(b"cache")
        (tmp_path / "extension.pyd").write_bytes(b"cache")

        status = subprocess.run(
            ["git", "status", "--short", "--ignored"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

        assert "?? events.jsonl" in status
        assert "!! .hermes/outcomes/" in status
        assert "!! .hermes/tpo-config.yaml" in status
        assert "!! .hermes/fake-gh-state.json" in status
        assert "!! .hermes/fake-gh-state.json.tmp" in status
        assert "!! .superpowers/" in status
        assert "!! .code-review-graph/" in status
        assert "!! compiled.pyc" in status
        assert "!! optimized.pyo" in status
        assert "!! extension.pyd" in status
        assert "!! src/" in status


class TestFakeGhStub:
    """Behavioural contract of the bundled offline ``gh`` stand-in."""

    @pytest.fixture
    def gh(self, tmp_path: Path):
        from hermes_pipeline.harness import (
            _fake_gh_state_for_fixture,
            fake_gh_script_path,
        )

        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(_fake_gh_state_for_fixture("happy-path")))

        def run(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [str(fake_gh_script_path()), *args],
                capture_output=True,
                text=True,
                env={**os.environ, "TPO_FAKE_GH_STATE": str(state_path)},
            )

        run.state = lambda: json.loads(state_path.read_text())  # type: ignore[attr-defined]
        return run

    _API = ("api", "-H", "Accept: application/vnd.github+json")
    _REPO = "tpo-harness/mock-project"

    def test_auth_status_succeeds(self, gh):
        assert gh("auth", "status", "--hostname", "github.com").returncode == 0

    def test_list_returns_slurped_pages_and_filters(self, gh):
        listed = gh(*self._API, "--paginate", "--slurp",
                    f"repos/{self._REPO}/issues?state=open&labels=tpo%3Atodo&per_page=100")
        assert listed.returncode == 0
        pages = json.loads(listed.stdout)
        assert [issue["number"] for page in pages for issue in page] == [1]

        closed = gh(*self._API, "--paginate", "--slurp",
                    f"repos/{self._REPO}/issues?state=closed&labels=tpo%3Atodo&per_page=100")
        assert json.loads(closed.stdout) == [[]]
        other_label = gh(*self._API, "--paginate", "--slurp",
                         f"repos/{self._REPO}/issues?state=all&labels=tpo%3Aon-hold&per_page=100")
        assert json.loads(other_label.stdout) == [[]]

    def test_single_issue_and_comments(self, gh):
        single = gh(*self._API, f"repos/{self._REPO}/issues/1")
        assert json.loads(single.stdout)["number"] == 1
        assert gh(*self._API, f"repos/{self._REPO}/issues/9").returncode == 1

        comments = gh(*self._API, "--paginate", "--slurp", f"repos/{self._REPO}/issues/1/comments")
        assert json.loads(comments.stdout) == [[]]

    def test_label_edit_mutates_state(self, gh):
        assert gh("issue", "edit", "1", "--repo", self._REPO, "--add-label", "tpo:in-progress").returncode == 0
        assert "tpo:in-progress" in {label["name"] for label in gh.state()["issues"]["1"]["labels"]}
        assert gh("issue", "edit", "1", "--repo", self._REPO, "--remove-label", "ready-for-agent").returncode == 0
        assert "ready-for-agent" not in {label["name"] for label in gh.state()["issues"]["1"]["labels"]}

    def test_label_edit_splits_comma_separated_labels(self, gh):
        assert gh("issue", "edit", "1", "--repo", self._REPO, "--add-label", "a,b").returncode == 0
        assert {"a", "b"} <= {label["name"] for label in gh.state()["issues"]["1"]["labels"]}

    def test_concurrent_mutations_are_serialized(self, gh):
        import threading

        results = {}

        def create(name: str) -> None:
            results[name] = gh("label", "create", "--repo", self._REPO, "--color", "ededed",
                               "--description", "d", "--", name)

        threads = [threading.Thread(target=create, args=(f"label-{i}",)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert all(result.returncode == 0 for result in results.values())
        assert {f"label-{i}" for i in range(6)} <= set(gh.state()["labels"])

    def test_comment_appends_from_body_file(self, gh, tmp_path: Path):
        body = tmp_path / "body.md"
        body.write_text("first\n")
        assert gh("issue", "comment", "1", "--repo", self._REPO, "--body-file", str(body)).returncode == 0
        body.write_text("second\n")
        assert gh("issue", "comment", "1", "--repo", self._REPO, "--body-file", str(body)).returncode == 0
        assert [item["body"] for item in gh.state()["comments"]["1"]] == ["first\n", "second\n"]

    def test_close_is_idempotent(self, gh):
        first = gh("issue", "close", "1", "--repo", self._REPO, "--reason", "completed")
        assert first.returncode == 0
        assert gh.state()["issues"]["1"]["state"] == "closed"
        again = gh("issue", "close", "1", "--repo", self._REPO, "--reason", "completed")
        assert again.returncode == 0
        assert "already closed" in again.stderr

    def test_label_list_and_create(self, gh):
        listed = gh("label", "list", "--repo", self._REPO, "--json", "name", "--limit", "1000")
        assert {item["name"] for item in json.loads(listed.stdout)} == {"tpo:todo", "ready-for-agent"}
        created = gh("label", "create", "--repo", self._REPO, "--color", "ededed",
                     "--description", "d", "--force", "--", "--repo")
        assert created.returncode == 0
        assert "--repo" in gh.state()["labels"]

    def test_create_assigns_next_number_and_prints_url(self, gh, tmp_path: Path):
        body = tmp_path / "body.md"
        body.write_text("### What\n\nNew\n")
        created = gh("issue", "create", "--repo", self._REPO, "--title", "Second",
                     "--body-file", str(body), "--label", "tpo:todo", "--label", "needs-triage")
        assert created.returncode == 0
        assert created.stdout.strip() == f"https://github.com/{self._REPO}/issues/2"
        issue = gh.state()["issues"]["2"]
        assert issue["title"] == "Second"
        assert [label["name"] for label in issue["labels"]] == ["tpo:todo", "needs-triage"]

    def test_dependency_post_rejects_duplicate_with_422(self, gh, tmp_path: Path):
        body = tmp_path / "body.md"
        body.write_text("x")
        gh("issue", "create", "--repo", self._REPO, "--title", "Blocker", "--body-file", str(body))
        blocker_id = json.loads(gh(*self._API, f"repos/{self._REPO}/issues/2").stdout)["id"]
        post = (*self._API, "--method", "POST", f"repos/{self._REPO}/issues/1/dependencies/blocked_by",
                "-F", f"issue_id={blocker_id}")
        assert gh(*post).returncode == 0
        assert json.loads(gh(*self._API, f"repos/{self._REPO}/issues/1").stdout)["issue_dependencies_summary"]["blocked_by"] == 1
        duplicate = gh(*post)
        assert duplicate.returncode == 1
        assert "Validation Failed (HTTP 422)" in duplicate.stderr

    def test_unsupported_argv_fails(self, gh):
        result = gh("pr", "list")
        assert result.returncode == 1
        assert "fake gh: unsupported" in result.stderr


class TestPreflightCheck:
    def test_preflight_check_gh_not_found_unless_overridden(self, monkeypatch):
        available = {"git", "hermes", "claude"}
        monkeypatch.setattr(
            "hermes_pipeline.harness.shutil.which",
            lambda executable: f"/bin/{executable}" if executable in available else None,
        )
        monkeypatch.delenv("TPO_GH_BIN", raising=False)
        with pytest.raises(RuntimeError, match="gh"):
            preflight_check()

        monkeypatch.setenv("TPO_GH_BIN", "/fixture/bin/gh")
        preflight_check()

    def test_preflight_check_git_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("PATH", "")
        with pytest.raises(RuntimeError, match="[Gg]it"):
            preflight_check()

    def test_preflight_check_hermes_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import shutil

        from hermes_pipeline.hermes_adapter import HermesDependencyError

        git_dir = Path(shutil.which("git")).parent
        monkeypatch.setenv("PATH", str(git_dir))
        monkeypatch.setenv("TPO_GH_BIN", "/fixture/bin/gh")
        with pytest.raises(HermesDependencyError, match="[Hh]ermes"):
            preflight_check()

    @pytest.mark.parametrize(
        ("prompt_client", "selected_executable", "unselected_executable"),
        (("claude", "claude", "codex"), ("codex", "codex", "claude")),
    )
    def test_preflight_requires_only_the_selected_client(
        self,
        monkeypatch,
        prompt_client,
        selected_executable,
        unselected_executable,
    ):
        available = {"git", "gh", "hermes", selected_executable}
        monkeypatch.setattr(
            "hermes_pipeline.harness.shutil.which",
            lambda executable: f"/bin/{executable}" if executable in available else None,
        )

        preflight_check(prompt_client=prompt_client)

        available.remove(selected_executable)
        available.add(unselected_executable)
        from hermes_pipeline.hermes_adapter import AgentClientDependencyError

        with pytest.raises(
            AgentClientDependencyError,
            match=rf"{selected_executable}.*selected prompt client",
        ):
            preflight_check(prompt_client=prompt_client)


class TestConvergenceDetector:
    def test_halts_after_threshold(self):
        d = ConvergenceDetector(threshold=3)
        d.record("p1", "hermes_error")
        d.record("p2", "hermes_error")
        assert d.should_halt() is False
        d.record("p3", "hermes_error")
        assert d.should_halt() is True

    def test_resets_on_success(self):
        d = ConvergenceDetector(threshold=3)
        d.record("p1", "hermes_error")
        d.record("p2", "hermes_error")
        d.record("p3", None)
        assert d.should_halt() is False
        d.record("p4", "hermes_error")
        assert d.should_halt() is False

    def test_different_error_class(self):
        d = ConvergenceDetector(threshold=3)
        d.record("p1", "hermes_error")
        d.record("p2", "hermes_error")
        d.record("p3", "timeout")
        assert d.should_halt() is False

    def test_custom_threshold(self):
        d = ConvergenceDetector(threshold=2)
        d.record("p1", "hermes_error")
        assert d.should_halt() is False
        d.record("p2", "hermes_error")
        assert d.should_halt() is True


class TestHarnessMonitor:
    def test_writes_jsonl_events(self, tmp_path: Path):
        log_path = tmp_path / "events.jsonl"
        monitor = HarnessMonitor(log_path)

        monitor("phase_started", {"phase_key": "phase_2_autoplan", "todo_id": "TODO-1"})
        monitor("phase_completed", {"phase_key": "phase_2_autoplan", "duration_ms": 5000})

        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 2

        event1 = json.loads(lines[0])
        assert event1["event_type"] == "phase_started"
        assert event1["phase_key"] == "phase_2_autoplan"
        assert "timestamp" in event1

        event2 = json.loads(lines[1])
        assert event2["event_type"] == "phase_completed"
        assert event2["duration_ms"] == 5000


class TestFilterPhases:
    def test_filters_to_single_phase(self):
        phases = [
            Phase(phase_key="phase_2", name="Phase 2", prompt="p2", tools="", turns=0),
            Phase(phase_key="phase_3", name="Phase 3", prompt="p3", tools="", turns=0),
            Phase(phase_key="phase_4", name="Phase 4", prompt="p4", tools="", turns=0),
        ]
        filtered = filter_phases(phases, "phase_3")
        assert len(filtered) == 1
        assert filtered[0].phase_key == "phase_3"

    def test_unknown_phase_raises(self):
        phases = [
            Phase(phase_key="phase_2", name="Phase 2", prompt="p2", tools="", turns=0),
        ]
        with pytest.raises(ValueError, match="phase_99"):
            filter_phases(phases, "phase_99")

    def test_with_gate_phase_list(self):
        all_phases = [
            Phase(phase_key="phase_8_finish_branch", name="Phase 8", prompt="p8", tools="", turns=0),
            Phase(phase_key="phase_9_ship", name="Phase 9", prompt="", tools="", turns=0, gate=True),
        ]
        filtered = filter_phases(all_phases, "phase_9_ship")
        assert len(filtered) == 1
        assert filtered[0].gate is True


class TestHarnessProfileTopology:
    def test_offline_terminal_uses_executable_terminal(self):
        phases = [
            Phase(phase_key="develop", name="Develop"),
            Phase(phase_key="finish", name="Finish", terminal=True),
        ]

        target = _offline_terminal_phase_key(phases, "direct-terminal")
        rewritten = _with_offline_terminal_workflow(phases, target)

        assert target == "finish"
        assert "local terminal workflow" in rewritten[-1].prompt.lower()

    def test_offline_terminal_uses_predecessor_of_terminal_gate(self):
        phases = [
            Phase(phase_key="develop", name="Develop"),
            Phase(phase_key="ship", name="Ship"),
            Phase(phase_key="review", name="Review", gate=True, terminal=True),
        ]

        assert _offline_terminal_phase_key(phases, "gate-terminal") == "ship"

    @pytest.mark.parametrize(
        "phases",
        (
            [Phase(phase_key="develop", name="Develop")],
            [
                Phase(phase_key="one", name="One", terminal=True),
                Phase(phase_key="two", name="Two", terminal=True),
            ],
            [Phase(phase_key="gate", name="Gate", gate=True, terminal=True)],
        ),
    )
    def test_invalid_terminal_topology_fails_closed(self, phases):
        with pytest.raises(HarnessProfileError, match="invalid_terminal_topology"):
            _offline_terminal_phase_key(phases, "broken")

    def test_harness_profile_data_preserves_top_level_metadata(self):
        source = {"requires_plan": True, "custom": {"owner": "test"}, "phases": []}

        result = _build_harness_profile_data(
            source,
            [Phase(phase_key="develop", name="Develop", terminal=True)],
        )

        assert result["requires_plan"] is True
        assert result["custom"] == {"owner": "test"}
        assert result["phases"][0]["phase_key"] == "develop"
        assert source["phases"] == []

    def test_unverified_profile_fails_before_preflight_or_workspace(self, mocker):
        preflight = mocker.patch("hermes_pipeline.harness.preflight_check")
        mkdtemp = mocker.patch("hermes_pipeline.harness.tempfile.mkdtemp")

        with pytest.raises(HarnessProfileError, match="unverified_prerequisites"):
            run_harness(
                fixture_name="happy-path",
                loop=False,
                phase_only=None,
                keep_dir=False,
                timeout=60,
                convergence_threshold=3,
                config=None,
                profile_name="agent-skills",
            )

        preflight.assert_not_called()
        mkdtemp.assert_not_called()

    def test_gate_only_phase_fails_before_preflight_or_workspace(self, mocker):
        from hermes_pipeline.phases import load_profile_prerequisites

        mocker.patch(
            "hermes_pipeline.phases.load_profile_prerequisites",
            return_value=load_profile_prerequisites("gstack"),
        )
        preflight = mocker.patch("hermes_pipeline.harness.preflight_check")
        mkdtemp = mocker.patch("hermes_pipeline.harness.tempfile.mkdtemp")

        with pytest.raises(HarnessProfileError, match="gate_phase_not_executable"):
            run_harness(
                fixture_name="happy-path",
                loop=False,
                phase_only="phase_8_ship",
                keep_dir=False,
                timeout=60,
                convergence_threshold=3,
                config=None,
                profile_name="agent-skills",
            )

        preflight.assert_not_called()
        mkdtemp.assert_not_called()

    def test_missing_conditional_skill_fails_profile_preflight(self, mocker):
        from hermes_pipeline.phases import load_profile_prerequisites

        mocker.patch(
            "hermes_pipeline.harness.verify_hermes_skill_registry_prerequisite",
            return_value=(False, "missing"),
        )

        with pytest.raises(
            HarnessProfileError, match="missing_conditional_prerequisite"
        ):
            _validate_profile_prerequisites(
                profile_name="gstack",
                prompt_client="claude",
                prerequisites=load_profile_prerequisites("gstack"),
            )


class TestIsolateConfig:
    def test_sets_env_vars(self, tmp_path: Path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        with isolate_config(state_dir=state_dir):
            assert os.environ.get("TPO_CONFIG_FILE") == str(state_dir / "tpo-config.yaml")

        assert "TPO_CONFIG_FILE" not in os.environ

    def test_saves_and_restores(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("TPO_CONFIG_FILE", "/original/config.yaml")

        with isolate_config(state_dir=Path("/tmp")):
            assert os.environ["TPO_CONFIG_FILE"] == "/tmp/tpo-config.yaml"

        assert os.environ["TPO_CONFIG_FILE"] == "/original/config.yaml"


class TestFakeGhEnv:
    def test_sets_and_restores_env(self, tmp_path: Path, monkeypatch):
        import shutil

        from hermes_pipeline.harness import fake_gh_env

        monkeypatch.setenv("TPO_GH_BIN", "/real/gh")
        monkeypatch.delenv("TPO_FAKE_GH_STATE", raising=False)
        original_path = os.environ["PATH"]
        stub = tmp_path / "bin" / "gh"
        stub.parent.mkdir()
        stub.write_text("#!/bin/sh\n")
        stub.chmod(0o755)
        with fake_gh_env(tmp_path):
            assert os.environ["TPO_GH_BIN"] == str(stub)
            assert os.environ["TPO_FAKE_GH_STATE"] == str(tmp_path / ".hermes" / "fake-gh-state.json")
            assert os.environ["PATH"].split(os.pathsep)[0] == str(stub.parent)
            assert shutil.which("gh") == str(stub)
        assert os.environ["TPO_GH_BIN"] == "/real/gh"
        assert "TPO_FAKE_GH_STATE" not in os.environ
        assert os.environ["PATH"] == original_path


def test_prune_retained_state_removes_only_safe_terminal_state(tmp_path):
    state_dir = tmp_path / ".hermes"
    state_dir.mkdir()
    (state_dir / "pipeline.toml").write_text("schema_version = 2\n")
    (state_dir / "pipeline_branch.txt").write_text("feat/mock\n")
    (state_dir / "tpo-config.yaml").write_text("state_dir: .hermes\n")
    (state_dir / "unknown.json").write_text("{}\n")

    empty_outcomes = state_dir / "outcomes"
    empty_outcomes.mkdir()
    empty_checkpoints = state_dir / "pipeline_checkpoints"
    empty_checkpoints.mkdir()
    evidence_dir = state_dir / "ready_for_review"
    evidence_dir.mkdir()
    (evidence_dir / "failure.json").write_text("{}\n")

    _prune_retained_state(state_dir)

    assert (state_dir / "pipeline.toml").exists()
    assert (state_dir / "unknown.json").exists()
    assert not (state_dir / "pipeline_branch.txt").exists()
    assert not (state_dir / "tpo-config.yaml").exists()
    assert not empty_outcomes.exists()
    assert not empty_checkpoints.exists()
    assert (evidence_dir / "failure.json").exists()


class TestHarnessResult:
    def test_dataclass_fields(self):
        result = HarnessResult(exit_code=0, report_path=Path("/tmp/report.json"), temp_dir=None, summary="1/1 passed")
        assert result.exit_code == 0
        assert str(result.report_path) == "/tmp/report.json"
        assert result.temp_dir is None
        assert "passed" in result.summary


class TestClassifyErrorClass:
    def test_dependency_errors(self):
        from hermes_pipeline.hermes_adapter import (
            AgentClientDependencyError,
            ClaudeDependencyError,
            HermesDependencyError,
        )

        assert _classify_error_class(HermesDependencyError("x")) == "dependency_error"
        assert _classify_error_class(ClaudeDependencyError("x")) == "dependency_error"
        assert _classify_error_class(AgentClientDependencyError("x")) == "dependency_error"

    def test_call_errors(self):
        from hermes_pipeline.hermes_adapter import ClaudeCallError, HermesCallError

        assert _classify_error_class(HermesCallError(1)) == "hermes_error"
        assert _classify_error_class(ClaudeCallError(1)) == "claude_error"

    def test_timeout(self):
        assert _classify_error_class(TimeoutError("x")) == "timeout"

    def test_unknown_falls_back_to_phase_failure(self):
        assert _classify_error_class(RuntimeError("x")) == "phase_failure"


class TestConvergenceMonitor:
    """The convergence detector must actually halt a harness run, not just track state."""

    def test_halts_after_threshold_consecutive_failures(self, tmp_path: Path):
        events = []
        inner = lambda et, data=None: events.append((et, data))
        detector = ConvergenceDetector(threshold=2)
        error_holder = {}
        monitor = _ConvergenceMonitor(inner, detector, error_holder)

        error_holder["error_class"] = "hermes_error"
        monitor("phase_started", {"phase_key": "p1"})
        monitor("phase_failed", {"phase_key": "p1"})

        error_holder["error_class"] = "hermes_error"
        monitor("phase_started", {"phase_key": "p2"})
        with pytest.raises(ConvergenceHaltError, match="hermes_error"):
            monitor("phase_failed", {"phase_key": "p2"})

        assert len(events) == 4

    def test_success_resets_the_detector(self):
        inner = lambda et, data=None: None
        detector = ConvergenceDetector(threshold=2)
        monitor = _ConvergenceMonitor(inner, detector, {})

        monitor("phase_completed", {"phase_key": "p1"})
        assert detector.should_halt() is False

    def test_tracks_current_phase_for_timeout_reporting(self):
        inner = lambda et, data=None: None
        detector = ConvergenceDetector(threshold=3)
        monitor = _ConvergenceMonitor(inner, detector, {})

        monitor("phase_started", {"phase_key": "phase_2_autoplan"})
        assert monitor.current_phase_key == "phase_2_autoplan"


class TestPollKanbanPhasesConsoleOutput:
    """Each phase transition must be logged to the console (via log.info), not just
    written to events.jsonl, so `tpo test` is no longer silent mid-run."""

    def _run_poll(self, monkeypatch, mocker, status_sequence, tmp_path):
        from hermes_pipeline.harness import (
            ConvergenceDetector,
            HarnessMonitor,
            _ConvergenceMonitor,
            _poll_kanban_phases,
        )

        monkeypatch.setattr("hermes_pipeline.harness.time.sleep", lambda *_a, **_kw: None)
        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            side_effect=status_sequence,
        )

        log_path = tmp_path / "events.jsonl"
        base_monitor = HarnessMonitor(log_path)
        detector = ConvergenceDetector(threshold=99)
        monitor = _ConvergenceMonitor(base_monitor, detector, {})

        return _poll_kanban_phases(
            project_slug="proj",
            tick_id="tick-1",
            state_dir=tmp_path,
            todo_id="TODO-30",
            project_dir=tmp_path,
            phases_path=None,
            monitor=monitor,
            detector=detector,
            poll_interval=0.0,
            max_poll_interval=0.0,
        )

    def test_none_to_running_logs_phase_start(self, monkeypatch, mocker, tmp_path, caplog):
        caplog.set_level("INFO", logger="hermes_pipeline.harness")
        self._run_poll(
            monkeypatch, mocker, tmp_path=tmp_path,
            status_sequence=[
                {"p1": "running"},
                {"p1": "done"},
                {"p1": "done"},
            ],
        )
        assert any("p1" in r.message and "running" in r.message for r in caplog.records)

    def test_running_to_done_logs_completion(self, monkeypatch, mocker, tmp_path, caplog):
        caplog.set_level("INFO", logger="hermes_pipeline.harness")
        self._run_poll(
            monkeypatch, mocker, tmp_path=tmp_path,
            status_sequence=[
                {"p1": "running"},
                {"p1": "done"},
                {"p1": "done"},
            ],
        )
        assert any("p1" in r.message and "done" in r.message for r in caplog.records)

    def test_running_to_failed_logs_failure(self, monkeypatch, mocker, tmp_path, caplog):
        caplog.set_level("INFO", logger="hermes_pipeline.harness")
        self._run_poll(
            monkeypatch, mocker, tmp_path=tmp_path,
            status_sequence=[
                {"p1": "running"},
                {"p1": "failed"},
                {"p1": "failed"},
            ],
        )
        assert any("p1" in r.message and "failed" in r.message for r in caplog.records)

    def test_fast_phase_none_to_done_still_logs(self, monkeypatch, mocker, tmp_path, caplog):
        """Phase finishes between polls without ever being observed as 'running'."""
        caplog.set_level("INFO", logger="hermes_pipeline.harness")
        self._run_poll(
            monkeypatch, mocker, tmp_path=tmp_path,
            status_sequence=[
                {"p1": "done"},
                {"p1": "done"},
            ],
        )
        assert any("p1" in r.message and "done" in r.message for r in caplog.records)

    def test_fast_phase_none_to_failed_still_logs(self, monkeypatch, mocker, tmp_path, caplog):
        """Phase fails between polls without ever being observed as 'running'."""
        caplog.set_level("INFO", logger="hermes_pipeline.harness")
        self._run_poll(
            monkeypatch, mocker, tmp_path=tmp_path,
            status_sequence=[
                {"p1": "failed"},
                {"p1": "failed"},
            ],
        )
        assert any("p1" in r.message and "failed" in r.message for r in caplog.records)

    def test_todo_to_blocked_still_logs_blocked_phase(
        self, monkeypatch, mocker, tmp_path, caplog
    ):
        """A phase can move from todo directly to blocked between polls."""
        caplog.set_level("INFO", logger="hermes_pipeline.harness")
        self._run_poll(
            monkeypatch,
            mocker,
            tmp_path=tmp_path,
            status_sequence=[
                {"p0": "running", "p1": "todo"},
                {"p0": "done", "p1": "todo"},
                {"p0": "done", "p1": "blocked"},
                {"p0": "done", "p1": "blocked"},
            ],
        )
        event_lines = (tmp_path / "events.jsonl").read_text().splitlines()
        assert any("p1" in r.message and "blocked" in r.message for r in caplog.records)
        assert any('"event_type": "phase_blocked"' in line for line in event_lines)

    def test_initial_status_table_prints_after_registration(self, monkeypatch, mocker, tmp_path, caplog):
        caplog.set_level("INFO", logger="hermes_pipeline.harness")
        self._run_poll(
            monkeypatch, mocker, tmp_path=tmp_path,
            status_sequence=[
                {"p1": "ready"},
                {"p1": "done"},
                {"p1": "done"},
            ],
        )
        assert any("initial phase status" in r.message.lower() for r in caplog.records)

    def test_initial_status_table_prints_before_any_transition(self, monkeypatch, mocker, tmp_path, caplog):
        """Even if the first poll already shows a phase terminal, the initial table
        must have printed first."""
        caplog.set_level("INFO", logger="hermes_pipeline.harness")
        self._run_poll(
            monkeypatch, mocker, tmp_path=tmp_path,
            status_sequence=[
                {"p1": "done"},
                {"p1": "done"},
            ],
        )
        messages = [r.message.lower() for r in caplog.records]
        initial_idx = next(i for i, m in enumerate(messages) if "initial phase status" in m)
        transition_idx = next(i for i, m in enumerate(messages) if "p1" in m and "-> done" in m)
        assert initial_idx < transition_idx

    def test_partial_terminal_snapshot_waits_for_complete_registered_profile(
        self, monkeypatch, mocker, tmp_path
    ):
        """Terminal statuses cannot complete a poll until every phase is present."""
        from hermes_pipeline.harness import _poll_kanban_phases

        phases = [
            Phase(phase_key="p1", name="P1"),
            Phase(phase_key="p2", name="P2"),
        ]
        status = mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            side_effect=[
                {"p1": "done"},
                {"p1": "done"},
                {"p1": "done", "p2": "done"},
                {"p1": "done", "p2": "done"},
            ],
        )
        monkeypatch.setattr(
            "hermes_pipeline.harness.time.sleep", lambda *_a, **_kw: None
        )
        mocker.patch(
            "hermes_pipeline.kanban_tasks.register_todo_phases",
            return_value=["t1", "t2"],
        )
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")

        log_path = tmp_path / "events.jsonl"
        monitor = _ConvergenceMonitor(
            HarnessMonitor(log_path),
            ConvergenceDetector(threshold=99),
            {},
        )

        assert _poll_kanban_phases(
            project_slug="proj",
            tick_id="tick-1",
            state_dir=tmp_path,
            todo_id="TODO-30",
            project_dir=tmp_path,
            phases_path=None,
            monitor=monitor,
            detector=ConvergenceDetector(threshold=99),
            poll_interval=0.0,
            max_poll_interval=0.0,
            phases=phases,
        )
        assert status.call_count == 4
        events = [
            json.loads(line)
            for line in log_path.read_text().splitlines()
        ]
        assert any(
            event["event_type"] == "phase_completed"
            and event["phase_key"] == "p2"
            for event in events
        )

    def test_registration_failure_emits_no_transition_logs(self, monkeypatch, mocker, tmp_path, caplog):
        """If register_todo_phases() raises, the poll loop must never start —
        matches today's behavior, no new failure path."""
        from hermes_pipeline.harness import (
            ConvergenceDetector,
            HarnessMonitor,
            _ConvergenceMonitor,
            _poll_kanban_phases,
        )

        caplog.set_level("INFO", logger="hermes_pipeline.harness")
        monkeypatch.setattr("hermes_pipeline.harness.time.sleep", lambda *_a, **_kw: None)
        mocker.patch(
            "hermes_pipeline.kanban_tasks.register_todo_phases",
            side_effect=RuntimeError("boom"),
        )
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status")

        log_path = tmp_path / "events.jsonl"
        base_monitor = HarnessMonitor(log_path)
        detector = ConvergenceDetector(threshold=99)
        monitor = _ConvergenceMonitor(base_monitor, detector, {})

        with pytest.raises(RuntimeError, match="boom"):
            _poll_kanban_phases(
                project_slug="proj",
                tick_id="tick-1",
                state_dir=tmp_path,
                todo_id="TODO-30",
                project_dir=tmp_path,
                phases_path=None,
                monitor=monitor,
                detector=detector,
                poll_interval=0.0,
                max_poll_interval=0.0,
            )

        assert not any("initial phase status" in r.message.lower() for r in caplog.records)
        assert not any("->" in r.message for r in caplog.records)


class TestRunHarnessTimeout:
    """Overall --timeout must actually bound a hung phase, not just be accepted and ignored."""

    def test_timed_out_retained_run_preserves_live_state(
        self, tmp_path, monkeypatch, mocker
    ):
        workspace = tmp_path / "harness-run"
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda **_kwargs: None)
        monkeypatch.setattr(
            "hermes_pipeline.harness.tempfile.mkdtemp",
            lambda prefix=None, dir=None: str(workspace),
        )
        mocker.patch("hermes_pipeline.harness._kanban_preflight")
        mocker.patch(
            "hermes_pipeline.harness._run_with_timeout",
            return_value=(False, True, {}),
        )
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={"phase_2_autoplan": "running"},
        )
        mocker.patch(
            "hermes_pipeline.kanban_tasks.cancel_todo_kanban_tasks",
            return_value=True,
        )

        result = run_harness(
            fixture_name="happy-path",
            loop=False,
            phase_only=None,
            keep_dir=True,
            timeout=1,
            convergence_threshold=3,
            config=None,
        )

        state_dir = workspace / "project" / ".hermes"
        assert result.exit_code == 1
        assert result.temp_dir == workspace
        assert (state_dir / "tpo-config.yaml").exists()
        assert (state_dir / "pipeline_checkpoints").exists()
        assert (state_dir / "ready_for_review").exists()

    def test_timeout_stops_poll_and_remote_run_before_report(
        self, tmp_path, monkeypatch, mocker
    ):
        lifecycle = []
        monkeypatch.setattr(
            "hermes_pipeline.harness.preflight_check",
            lambda **_kwargs: None,
        )
        monkeypatch.setattr(
            "hermes_pipeline.harness.tempfile.mkdtemp",
            lambda prefix=None, dir=None: str(tmp_path / "harness-run"),
        )
        mocker.patch("hermes_pipeline.harness._kanban_preflight")

        def _cooperative_poll(**kwargs):
            lifecycle.append("poll_started")
            assert kwargs["cancel_event"].wait(2)
            lifecycle.append("poll_stopped")
            return False

        mocker.patch(
            "hermes_pipeline.harness._poll_kanban_phases",
            side_effect=_cooperative_poll,
        )
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={"phase_2_autoplan": "running"},
        )

        def _cancel_remote(*_args, **_kwargs):
            assert lifecycle[-1] == "poll_stopped"
            lifecycle.append("remote_terminated")
            return True

        mocker.patch(
            "hermes_pipeline.kanban_tasks.cancel_todo_kanban_tasks",
            side_effect=_cancel_remote,
        )

        from hermes_pipeline.test_report import generate_report as real_generate_report

        def _generate_report(*args, **kwargs):
            assert lifecycle[-1] == "remote_terminated"
            lifecycle.append("report_generated")
            return real_generate_report(*args, **kwargs)

        mocker.patch(
            "hermes_pipeline.test_report.generate_report",
            side_effect=_generate_report,
        )

        result = run_harness(
            fixture_name="happy-path",
            loop=False,
            phase_only="phase_2_autoplan",
            keep_dir=True,
            timeout=0.01,
            convergence_threshold=3,
            config=None,
        )

        assert result.exit_code == 1
        assert "timeout" in result.summary.lower()
        assert lifecycle == [
            "poll_started",
            "poll_stopped",
            "remote_terminated",
            "report_generated",
        ]
        report_data = json.loads(result.report_path.read_text())
        assert any(p["status"] == "timeout" for p in report_data["phases"])

    def test_timeout_retains_workspace_when_remote_termination_is_unconfirmed(
        self, tmp_path, monkeypatch, mocker
    ):
        from hermes_pipeline.harness import HarnessCleanupError

        workspace = tmp_path / "harness-run"
        monkeypatch.setattr(
            "hermes_pipeline.harness.preflight_check",
            lambda **_kwargs: None,
        )
        monkeypatch.setattr(
            "hermes_pipeline.harness.tempfile.mkdtemp",
            lambda prefix=None, dir=None: str(workspace),
        )
        mocker.patch("hermes_pipeline.harness._kanban_preflight")
        mocker.patch(
            "hermes_pipeline.harness._run_with_timeout",
            return_value=(False, True, {}),
        )
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={"phase_2_autoplan": "running"},
        )
        mocker.patch(
            "hermes_pipeline.kanban_tasks.cancel_todo_kanban_tasks",
            return_value=False,
        )
        generate = mocker.patch("hermes_pipeline.test_report.generate_report")

        with pytest.raises(HarnessCleanupError, match="workspace retained"):
            run_harness(
                fixture_name="happy-path",
                loop=False,
                phase_only=None,
                keep_dir=False,
                timeout=1,
                convergence_threshold=3,
                config=None,
            )

        assert workspace.exists()
        generate.assert_not_called()

    def test_poll_cancellation_failure_does_not_race_remote_cleanup(
        self, tmp_path, monkeypatch, mocker
    ):
        from hermes_pipeline.harness import HarnessCleanupError, PollCancellationError

        workspace = tmp_path / "harness-run"
        monkeypatch.setattr(
            "hermes_pipeline.harness.preflight_check",
            lambda **_kwargs: None,
        )
        monkeypatch.setattr(
            "hermes_pipeline.harness.tempfile.mkdtemp",
            lambda prefix=None, dir=None: str(workspace),
        )
        mocker.patch("hermes_pipeline.harness._kanban_preflight")
        mocker.patch(
            "hermes_pipeline.harness._run_with_timeout",
            side_effect=PollCancellationError("poll worker did not stop"),
        )
        cancel = mocker.patch(
            "hermes_pipeline.kanban_tasks.cancel_todo_kanban_tasks",
            return_value=True,
        )

        with pytest.raises(HarnessCleanupError, match="workspace retained"):
            run_harness(
                fixture_name="happy-path",
                loop=False,
                phase_only=None,
                keep_dir=False,
                timeout=1,
                convergence_threshold=3,
                config=None,
            )

        cancel.assert_not_called()
        assert workspace.exists()

    def test_poll_exception_after_registration_cleans_remote_or_retains_workspace(
        self, tmp_path, monkeypatch, mocker
    ):
        from hermes_pipeline.harness import HarnessCleanupError

        workspace = tmp_path / "harness-run"
        monkeypatch.setattr(
            "hermes_pipeline.harness.preflight_check",
            lambda **_kwargs: None,
        )
        monkeypatch.setattr(
            "hermes_pipeline.harness.tempfile.mkdtemp",
            lambda prefix=None, dir=None: str(workspace),
        )
        mocker.patch("hermes_pipeline.harness._kanban_preflight")
        register = mocker.patch(
            "hermes_pipeline.kanban_tasks.register_todo_phases",
            return_value=["t_00000001"],
        )
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            side_effect=RuntimeError("poll exploded"),
        )
        cancel = mocker.patch(
            "hermes_pipeline.kanban_tasks.cancel_todo_kanban_tasks",
            return_value=False,
        )
        generate = mocker.patch("hermes_pipeline.test_report.generate_report")

        with pytest.raises(HarnessCleanupError, match="workspace retained"):
            run_harness(
                fixture_name="happy-path",
                loop=False,
                phase_only=None,
                keep_dir=False,
                timeout=60,
                convergence_threshold=3,
                config=None,
            )

        register.assert_called_once()
        cancel.assert_called_once()
        assert workspace.exists()
        generate.assert_not_called()

    def test_partial_registration_failure_retains_workspace_when_cleanup_unconfirmed(
        self, tmp_path, monkeypatch, mocker
    ):
        from hermes_pipeline.harness import HarnessCleanupError

        workspace = tmp_path / "harness-run"
        monkeypatch.setattr(
            "hermes_pipeline.harness.preflight_check",
            lambda **_kwargs: None,
        )
        monkeypatch.setattr(
            "hermes_pipeline.harness.tempfile.mkdtemp",
            lambda prefix=None, dir=None: str(workspace),
        )
        mocker.patch("hermes_pipeline.harness._kanban_preflight")
        register = mocker.patch(
            "hermes_pipeline.kanban_tasks.register_todo_phases",
            side_effect=RuntimeError("recovery remains pending"),
        )
        cancel = mocker.patch(
            "hermes_pipeline.kanban_tasks.cancel_todo_kanban_tasks",
            return_value=False,
        )
        generate = mocker.patch("hermes_pipeline.test_report.generate_report")

        with pytest.raises(HarnessCleanupError, match="workspace retained"):
            run_harness(
                fixture_name="happy-path",
                loop=False,
                phase_only=None,
                keep_dir=False,
                timeout=60,
                convergence_threshold=3,
                config=None,
            )

        register.assert_called_once()
        cancel.assert_called_once()
        assert workspace.exists()
        generate.assert_not_called()

    def test_partial_registration_failure_retains_workspace_when_cleanup_raises(
        self, tmp_path, monkeypatch, mocker
    ):
        from hermes_pipeline.harness import HarnessCleanupError

        workspace = tmp_path / "harness-run"
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda **_kwargs: None)
        monkeypatch.setattr(
            "hermes_pipeline.harness.tempfile.mkdtemp",
            lambda prefix=None, dir=None: str(workspace),
        )
        mocker.patch("hermes_pipeline.harness._kanban_preflight")
        mocker.patch(
            "hermes_pipeline.kanban_tasks.register_todo_phases",
            side_effect=RuntimeError("registration failed"),
        )
        mocker.patch(
            "hermes_pipeline.kanban_tasks.cancel_todo_kanban_tasks",
            side_effect=RuntimeError("cleanup failed"),
        )
        generate = mocker.patch("hermes_pipeline.test_report.generate_report")

        with pytest.raises(HarnessCleanupError, match="workspace retained"):
            run_harness(
                fixture_name="happy-path",
                loop=False,
                phase_only=None,
                keep_dir=False,
                timeout=60,
                convergence_threshold=3,
                config=None,
            )

        assert workspace.exists()
        generate.assert_not_called()

    def test_partial_registration_failure_removes_workspace_after_confirmed_cleanup(
        self, tmp_path, monkeypatch, mocker
    ):
        workspace = tmp_path / "harness-run"
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda **_kwargs: None)
        monkeypatch.setattr(
            "hermes_pipeline.harness.tempfile.mkdtemp",
            lambda prefix=None, dir=None: str(workspace),
        )
        mocker.patch("hermes_pipeline.harness._kanban_preflight")
        mocker.patch(
            "hermes_pipeline.kanban_tasks.register_todo_phases",
            side_effect=RuntimeError("registration failed"),
        )
        mocker.patch(
            "hermes_pipeline.kanban_tasks.cancel_todo_kanban_tasks",
            return_value=True,
        )

        with pytest.raises(RuntimeError, match="registration failed"):
            run_harness(
                fixture_name="happy-path",
                loop=False,
                phase_only=None,
                keep_dir=False,
                timeout=60,
                convergence_threshold=3,
                config=None,
            )

        assert not workspace.exists()

    def test_timeout_cleanup_exception_retains_workspace(
        self, tmp_path, monkeypatch, mocker, caplog
    ):
        from hermes_pipeline.harness import HarnessCleanupError

        workspace = tmp_path / "harness-run"
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda **_kwargs: None)
        monkeypatch.setattr(
            "hermes_pipeline.harness.tempfile.mkdtemp",
            lambda prefix=None, dir=None: str(workspace),
        )
        mocker.patch("hermes_pipeline.harness._kanban_preflight")
        mocker.patch(
            "hermes_pipeline.harness._run_with_timeout",
            return_value=(False, True, {}),
        )
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={"phase_2_autoplan": "running"},
        )
        mocker.patch(
            "hermes_pipeline.kanban_tasks.cancel_todo_kanban_tasks",
            side_effect=RuntimeError("cleanup failed token=secret-value"),
        )
        caplog.set_level(logging.WARNING)

        with pytest.raises(HarnessCleanupError, match="workspace retained"):
            run_harness(
                fixture_name="happy-path",
                loop=False,
                phase_only=None,
                keep_dir=False,
                timeout=1,
                convergence_threshold=3,
                config=None,
            )

        assert workspace.exists()
        assert "secret-value" not in caplog.text
        assert "error_type=RuntimeError" in caplog.text


class TestKanbanModeHermes:
    """Tests for --kanban hermes wiring in run_harness() using kanban-as-scheduler."""

    @patch("hermes_pipeline.harness.subprocess.run")
    def test_kanban_hermes_registers_and_polls(self, mock_harness_sp, tmp_path, monkeypatch, mocker):
        """--kanban hermes uses register_todo_phases + polling, not PipelineRunner."""

        preflight_result = MagicMock(returncode=0, stdout="[]", stderr="")
        mock_harness_sp.return_value = preflight_result
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda **_kwargs: None)

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status",
                      return_value={"phase_2_autoplan": "done"})
        mocker.patch("time.sleep")
        mock_observe = mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")

        result = run_harness(
            fixture_name="happy-path", loop=False,
            phase_only="phase_2_autoplan", keep_dir=True,
            timeout=60, convergence_threshold=3,
 config=None,
        )

        assert result.exit_code == 0
        mock_observe.assert_called_once()

    @patch("hermes_pipeline.harness.subprocess.run")
    def test_kanban_hermes_preflight_failure_raises(self, mock_run, monkeypatch):
        from hermes_pipeline.harness import KanbanPreflightError

        preflight_fail = MagicMock(returncode=1, stdout="", stderr="not authenticated")
        mock_run.return_value = preflight_fail
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda **_kwargs: None)

        with pytest.raises(KanbanPreflightError, match="hermes login"):
            run_harness(
                fixture_name="happy-path", loop=False, phase_only=None,
                keep_dir=False, timeout=60, convergence_threshold=3,
 config=None,
            )

    @pytest.mark.skip(reason="phases.run deleted in Task 4; restored when Task 5 rewrites harness dispatch")
    @patch("hermes_pipeline.harness.subprocess.run")
    def test_kanban_null_explicit_produces_no_kanban_calls(self, mock_run, monkeypatch, tmp_path):
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda **_kwargs: None)
        with patch("hermes_pipeline.phases.run") as mock_phases_run:
            mock_phases_run.return_value = {"status": "success"}
            run_harness(
                fixture_name="happy-path", loop=False,
                phase_only="phase_2_autoplan", keep_dir=True,
                timeout=60, convergence_threshold=3,
 config=None,
            )
        kanban_calls = [c for c in mock_run.call_args_list
                        if c[0][0][:2] == ["hermes", "kanban"]]
        assert kanban_calls == []

    @patch("hermes_pipeline.harness.subprocess.run")
    def test_kanban_hermes_polling_emits_jsonl_events(self, mock_harness_sp, tmp_path, monkeypatch, mocker):
        preflight_result = MagicMock(returncode=0, stdout="[]", stderr="")
        mock_harness_sp.return_value = preflight_result
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda **_kwargs: None)

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=[
            {"phase_2_autoplan": "running"},       # poll loop iteration 1
            {"phase_2_autoplan": "done"},          # poll loop iteration 2 (terminal)
            {"phase_2_autoplan": "done"},          # observe_outcomes call inside _poll_kanban_phases
            {"phase_2_autoplan": "done"},          # final status check at end of run_harness
        ])

        result = run_harness(
            fixture_name="happy-path", loop=False,
            phase_only="phase_2_autoplan", keep_dir=True,
            timeout=60, convergence_threshold=3,
 config=None,
        )

        assert result.exit_code == 0
        report = json.loads(result.report_path.read_text())
        phases = report["phases"]
        assert len(phases) == 1
        assert phases[0]["phase_key"] == "phase_2_autoplan"
        assert phases[0]["status"] == "completed"

    @patch("hermes_pipeline.harness.subprocess.run")
    def test_kanban_preflight_timeout_raises_actionable_error(self, mock_run, monkeypatch):
        import subprocess

        from hermes_pipeline.harness import KanbanPreflightError

        def _run_side_effect(*args, **kwargs):
            cmd = args[0]
            if isinstance(cmd, (list, tuple)) and len(cmd) >= 3 and cmd[:3] == ["hermes", "kanban", "list"]:
                raise subprocess.TimeoutExpired(cmd, 15)
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = _run_side_effect
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda **_kwargs: None)

        with pytest.raises(KanbanPreflightError, match="timed out.*15s"):
            run_harness(
                fixture_name="happy-path", loop=False, phase_only=None,
                keep_dir=False, timeout=60, convergence_threshold=3,
 config=None,
            )

    def test_convergence_halt_stops_polling_hermes(self, monkeypatch, mocker):
        mock_sp = mocker.patch("hermes_pipeline.harness.subprocess.run")
        mock_sp.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda **_kwargs: None)

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1", "t2", "t3"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")

        call_count = [0]
        def fake_status(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"p1": "running", "p2": "ready", "p3": "ready"}
            elif call_count[0] == 2:
                return {"p1": "failed", "p2": "running", "p3": "ready"}
            elif call_count[0] == 3:
                return {"p1": "failed", "p2": "failed", "p3": "running"}
            return {"p1": "failed", "p2": "failed", "p3": "failed"}

        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=fake_status)

        result = run_harness(
            fixture_name="happy-path", loop=False, phase_only=None,
            keep_dir=True, timeout=60, convergence_threshold=3,
 config=None,
        )

        assert result.exit_code == 1

    @patch("hermes_pipeline.harness.subprocess.run")
    def test_kanban_hermes_single_phase_registers_filtered(self, mock_harness_sp, tmp_path, monkeypatch, mocker):
        """--phase retains the filtered path and selected prompt client."""

        preflight_result = MagicMock(returncode=0, stdout="[]", stderr="")
        mock_harness_sp.return_value = preflight_result
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda **_kwargs: None)

        mock_register = mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status",
                      return_value={"phase_2_autoplan": "done"})
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")

        run_harness(
            fixture_name="happy-path", loop=False,
            phase_only="phase_2_autoplan", keep_dir=True,
            timeout=60, convergence_threshold=3,
            config=Config(prompt_client="codex"),
        )

        call_kwargs = mock_register.call_args
        assert call_kwargs.kwargs.get("phases_path") is not None
        assert call_kwargs.kwargs["prompt_client"] == "codex"
        # The harness compiles without TODOS.md: the Plan is passed explicitly.
        assert call_kwargs.kwargs["plan_path"] == "docs/harness/TODO-1-plan.md"
        assert call_kwargs.kwargs["spec_path"] is None
        assert call_kwargs.kwargs["reference_paths"] == ()

    @pytest.mark.parametrize(
        ("config", "expected"),
        [(None, "claude"), (Config(prompt_client="codex"), "codex")],
    )
    def test_run_harness_resolves_prompt_client_once(
        self, config, expected, monkeypatch, mocker
    ):
        mock_sp = mocker.patch("hermes_pipeline.harness.subprocess.run")
        mock_sp.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
        preflight = mocker.patch("hermes_pipeline.harness.preflight_check")
        poll = mocker.patch(
            "hermes_pipeline.harness._poll_kanban_phases",
            return_value=True,
        )
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={"phase_2_autoplan": "done"},
        )

        result = run_harness(
            fixture_name="happy-path",
            loop=False,
            phase_only=None,
            keep_dir=False,
            timeout=60,
            convergence_threshold=3,
            config=config,
        )

        assert result.exit_code == 0
        preflight.assert_called_once()
        assert preflight.call_args.kwargs["prompt_client"] == expected
        assert preflight.call_args.kwargs["profile_name"] == "gstack"
        assert preflight.call_args.kwargs["prerequisites"].profile == "gstack"
        assert poll.call_args.kwargs["prompt_client"] == expected

    def test_run_harness_separates_project_from_artifacts(
        self, tmp_path, monkeypatch, mocker
    ):
        """The retained workspace keeps the Git fixture separate from run output."""
        workspace = tmp_path / "harness-run"
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda **_kwargs: None)
        monkeypatch.setattr(
            "hermes_pipeline.harness.tempfile.mkdtemp",
            lambda prefix=None, dir=None: str(workspace),
        )
        mocker.patch("hermes_pipeline.harness._kanban_preflight")
        poll = mocker.patch(
            "hermes_pipeline.harness._poll_kanban_phases",
            return_value=True,
        )
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={"phase_2_autoplan": "done"},
        )

        result = run_harness(
            fixture_name="happy-path",
            loop=False,
            phase_only=None,
            keep_dir=True,
            timeout=60,
            convergence_threshold=3,
            config=None,
        )

        project_dir = workspace / "project"
        artifacts_dir = workspace / "artifacts"
        assert result.temp_dir == workspace
        assert result.report_path == artifacts_dir / "reports" / "report.json"
        assert (project_dir / ".git").exists()
        assert (artifacts_dir / "events.jsonl").exists()
        assert not (project_dir / "events.jsonl").exists()
        assert not (project_dir / "reports").exists()
        assert (project_dir / ".hermes" / "pipeline.toml").exists()
        assert not (project_dir / ".hermes" / "tpo-config.yaml").exists()
        assert not (project_dir / ".hermes" / "pipeline_checkpoints").exists()
        assert not (project_dir / ".hermes" / "ready_for_review").exists()
        assert poll.call_args.kwargs["project_dir"] == project_dir
        assert poll.call_args.kwargs["state_dir"] == project_dir / ".hermes"

    def test_run_harness_keep_dir_false_removes_workspace(
        self, tmp_path, monkeypatch, mocker
    ):
        workspace = tmp_path / "harness-run"
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda **_kwargs: None)
        monkeypatch.setattr(
            "hermes_pipeline.harness.tempfile.mkdtemp",
            lambda prefix=None, dir=None: str(workspace),
        )
        mocker.patch("hermes_pipeline.harness._kanban_preflight")
        mocker.patch(
            "hermes_pipeline.harness._poll_kanban_phases",
            return_value=True,
        )
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={"phase_2_autoplan": "done"},
        )

        result = run_harness(
            fixture_name="happy-path",
            loop=False,
            phase_only=None,
            keep_dir=False,
            timeout=60,
            convergence_threshold=3,
            config=None,
        )

        assert result.temp_dir is None
        assert not workspace.exists()

    def test_run_harness_stores_loop_reports_in_artifacts(
        self, tmp_path, monkeypatch, mocker
    ):
        """Loop snapshots are retained beside artifacts, outside the Git fixture."""
        workspace = tmp_path / "harness-run"
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda **_kwargs: None)
        monkeypatch.setattr(
            "hermes_pipeline.harness.tempfile.mkdtemp",
            lambda prefix=None, dir=None: str(workspace),
        )
        mocker.patch("hermes_pipeline.harness._kanban_preflight")
        mocker.patch(
            "hermes_pipeline.harness._poll_kanban_phases",
            return_value=True,
        )
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={"phase_2_autoplan": "done"},
        )

        run_harness(
            fixture_name="happy-path",
            loop=True,
            phase_only=None,
            keep_dir=True,
            timeout=60,
            convergence_threshold=3,
            config=None,
        )

        assert (workspace / "artifacts" / "happy-path-report.1.json").exists()
        assert not (workspace / "project" / "happy-path-report.1.json").exists()


class TestAutoCompleteGateTasks:
    """Tests for _auto_complete_gate_tasks()."""

    def test_completes_blocked_gate_tasks(self, mocker):
        import json as _json

        from hermes_pipeline.harness import _auto_complete_gate_tasks

        header_gate = _json.dumps(
            {"tick_id": "01TICK", "phase_key": "phase_9_ship",
             "todo_id": "TODO-1", "project_slug": "demo"},
            sort_keys=True,
        )
        header_dev = _json.dumps(
            {"tick_id": "01TICK", "phase_key": "phase_4_development",
             "todo_id": "TODO-1", "project_slug": "demo"},
            sort_keys=True,
        )

        mock_data = [
            {"id": "t_gate", "status": "blocked", "body": header_gate + "\ngate"},
            {"id": "t_dev", "status": "ready", "body": header_dev + "\nphase"},
        ]

        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = mocker.Mock(returncode=0, stdout=_json.dumps(mock_data), stderr="")
        mock_complete = mocker.patch("hermes_pipeline.kanban_tasks.complete_todo_kanban_task")

        phases = [
            Phase(phase_key="phase_8_finish_branch", name="Phase 8", prompt="p8", tools="", turns=0),
            Phase(phase_key="phase_9_ship", name="Phase 9", prompt="", tools="", turns=0, gate=True),
        ]

        _auto_complete_gate_tasks(
            "demo", "01TICK", completed_phase_key="phase_8_finish_branch", phases=phases
        )

        mock_complete.assert_called_once_with("demo", "t_gate")

    def test_does_not_log_success_when_completion_fails(self, mocker, caplog):
        import json as _json

        from hermes_pipeline.harness import _auto_complete_gate_tasks

        header_gate = _json.dumps(
            {"tick_id": "01TICK", "phase_key": "phase_9_ship",
             "todo_id": "TODO-1", "project_slug": "demo"},
            sort_keys=True,
        )
        mock_data = [
            {"id": "t_gate", "status": "blocked", "body": header_gate + "\ngate"},
        ]

        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = mocker.Mock(returncode=0, stdout=_json.dumps(mock_data), stderr="")
        mocker.patch("hermes_pipeline.kanban_tasks.complete_todo_kanban_task", return_value=False)

        with caplog.at_level("INFO"):
            phases = [
                Phase(phase_key="phase_8_finish_branch", name="Phase 8", prompt="p8", tools="", turns=0),
                Phase(phase_key="phase_9_ship", name="Phase 9", prompt="", tools="", turns=0, gate=True),
            ]
            _auto_complete_gate_tasks(
                "demo", "01TICK", completed_phase_key="phase_8_finish_branch", phases=phases
            )

        assert "auto-completed gate task" not in caplog.text

    def test_warns_when_completion_fails(self, mocker, caplog):
        import json as _json

        from hermes_pipeline.harness import _auto_complete_gate_tasks

        header_gate = _json.dumps(
            {"tick_id": "01TICK", "phase_key": "phase_9_ship",
             "todo_id": "TODO-1", "project_slug": "demo"},
            sort_keys=True,
        )
        mock_data = [
            {"id": "t_gate", "status": "blocked", "body": header_gate + "\ngate"},
        ]

        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = mocker.Mock(returncode=0, stdout=_json.dumps(mock_data), stderr="")
        mocker.patch("hermes_pipeline.kanban_tasks.complete_todo_kanban_task", return_value=False)

        with caplog.at_level("WARNING"):
            phases = [
                Phase(phase_key="phase_8_finish_branch", name="Phase 8", prompt="p8", tools="", turns=0),
                Phase(phase_key="phase_9_ship", name="Phase 9", prompt="", tools="", turns=0, gate=True),
            ]
            _auto_complete_gate_tasks(
                "demo", "01TICK", completed_phase_key="phase_8_finish_branch", phases=phases
            )

        assert "t_gate" in caplog.text
        assert "phase_9_ship" in caplog.text
        assert "remains blocked" in caplog.text

    def test_skips_non_blocked_tasks(self, mocker):
        import json as _json

        from hermes_pipeline.harness import _auto_complete_gate_tasks

        header = _json.dumps(
            {"tick_id": "01TICK", "phase_key": "phase_2_autoplan",
             "todo_id": "TODO-1", "project_slug": "demo"},
            sort_keys=True,
        )

        mock_data = [
            {"id": "t1", "status": "running", "body": header},
            {"id": "t2", "status": "done", "body": header.replace("phase_2_autoplan", "phase_3")},
        ]

        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = mocker.Mock(returncode=0, stdout=_json.dumps(mock_data), stderr="")
        mock_complete = mocker.patch("hermes_pipeline.kanban_tasks.complete_todo_kanban_task")

        _auto_complete_gate_tasks("demo", "01TICK", completed_phase_key="phase_2_autoplan")

        mock_complete.assert_not_called()

    def test_is_best_effort_on_query_failure(self, mocker):
        """If get_todo_kanban_tasks raises, the function returns without error."""
        from hermes_pipeline.harness import _auto_complete_gate_tasks

        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
            side_effect=RuntimeError("query failed"),
        )

        _auto_complete_gate_tasks("demo", "01TICK", completed_phase_key="phase_2_autoplan")  # Should not raise


class TestPollKanbanPhases:
    """Tests for _poll_kanban_phases()."""

    def test_registers_phases_and_polls_to_completion(self, tmp_path, mocker):
        import json as _json

        from hermes_pipeline.harness import (
            ConvergenceDetector,
            HarnessMonitor,
            _ConvergenceMonitor,
            _poll_kanban_phases,
        )

        events_log = tmp_path / "events.jsonl"
        base_monitor = HarnessMonitor(events_log)
        detector = ConvergenceDetector(threshold=3)
        monitor = _ConvergenceMonitor(base_monitor, detector, {})

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1", "t2"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")

        call_count = [0]
        def fake_status(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 2:
                return {"phase_2_autoplan": "running", "phase_4_development": "ready"}
            return {"phase_2_autoplan": "done", "phase_4_development": "done"}

        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=fake_status)

        result = _poll_kanban_phases(
            project_slug="demo", tick_id="01TICK",
            state_dir=tmp_path / ".hermes", todo_id="TODO-1",
            project_dir=tmp_path, phases_path=None,
            monitor=monitor, detector=detector, poll_interval=0.1,
        )

        assert result is True
        assert call_count[0] >= 2

        lines = events_log.read_text().strip().splitlines()
        events = [_json.loads(l) for l in lines if l.strip()]
        event_types = [e["event_type"] for e in events]
        assert "phase_started" in event_types
        assert "phase_completed" in event_types

    def test_passes_prompt_client_to_registration(self, tmp_path, mocker):
        from hermes_pipeline.harness import (
            ConvergenceDetector,
            HarnessMonitor,
            _ConvergenceMonitor,
            _poll_kanban_phases,
        )

        register = mocker.patch(
            "hermes_pipeline.kanban_tasks.register_todo_phases",
            return_value=["task-1"],
        )
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={"phase_2_autoplan": "done"},
        )
        detector = ConvergenceDetector(threshold=3)
        monitor = _ConvergenceMonitor(
            HarnessMonitor(tmp_path / "events.jsonl"),
            detector,
            {},
        )

        _poll_kanban_phases(
            project_slug="demo",
            tick_id="01CLIENT",
            state_dir=tmp_path / ".hermes",
            todo_id="TODO-41",
            project_dir=tmp_path,
            phases_path=None,
            monitor=monitor,
            detector=detector,
            prompt_client="codex",
            poll_interval=0,
        )

        assert register.call_args.kwargs["prompt_client"] == "codex"

    def test_cancellation_interrupts_poll_wait_without_writing_outcomes(
        self, tmp_path, mocker
    ):
        import threading

        from hermes_pipeline.harness import (
            ConvergenceDetector,
            HarnessMonitor,
            _ConvergenceMonitor,
            _poll_kanban_phases,
        )

        cancel_event = threading.Event()
        mocker.patch(
            "hermes_pipeline.kanban_tasks.register_todo_phases",
            return_value=["t_00000001"],
        )

        def _initial_status(*_args, **_kwargs):
            cancel_event.set()
            return {"phase_2_autoplan": "running"}

        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            side_effect=_initial_status,
        )
        sleep = mocker.patch("hermes_pipeline.harness.time.sleep")
        observe = mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        detector = ConvergenceDetector(threshold=3)
        monitor = _ConvergenceMonitor(
            HarnessMonitor(tmp_path / "events.jsonl"),
            detector,
            {},
        )

        result = _poll_kanban_phases(
            project_slug="demo",
            tick_id="01CANCEL",
            state_dir=tmp_path / ".hermes",
            todo_id="TODO-1",
            project_dir=tmp_path,
            phases_path=None,
            monitor=monitor,
            detector=detector,
            cancel_event=cancel_event,
        )

        assert result is False
        sleep.assert_not_called()
        observe.assert_not_called()

    def test_emits_phase_failed_event_on_kanban_failure(self, tmp_path, mocker):
        import json as _json

        from hermes_pipeline.harness import (
            ConvergenceDetector,
            HarnessMonitor,
            _ConvergenceMonitor,
            _poll_kanban_phases,
        )

        events_log = tmp_path / "events.jsonl"
        base_monitor = HarnessMonitor(events_log)
        detector = ConvergenceDetector(threshold=3)
        monitor = _ConvergenceMonitor(base_monitor, detector, {})

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=[
            {"phase_2_autoplan": "running"},
            {"phase_2_autoplan": "failed"},
        ])

        result = _poll_kanban_phases(
            project_slug="demo", tick_id="01TICK",
            state_dir=tmp_path / ".hermes", todo_id="TODO-1",
            project_dir=tmp_path, phases_path=None,
            monitor=monitor, detector=detector, poll_interval=0.1,
        )

        assert result is False
        lines = events_log.read_text().strip().splitlines()
        events = [_json.loads(l) for l in lines if l.strip()]
        failed = [e for e in events if e["event_type"] == "phase_failed"]
        assert len(failed) == 1
        assert failed[0]["phase_key"] == "phase_2_autoplan"

    def test_emits_phase_blocked_event_on_kanban_block(self, tmp_path, mocker):
        import json as _json

        from hermes_pipeline.harness import (
            ConvergenceDetector,
            HarnessMonitor,
            _ConvergenceMonitor,
            _poll_kanban_phases,
        )

        events_log = tmp_path / "events.jsonl"
        base_monitor = HarnessMonitor(events_log)
        detector = ConvergenceDetector(threshold=3)
        monitor = _ConvergenceMonitor(base_monitor, detector, {})

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=[
            {"phase_4_development": "running"},
            {"phase_4_development": "blocked"},
            {"phase_4_development": "blocked"},
        ])

        result = _poll_kanban_phases(
            project_slug="demo", tick_id="01TICK",
            state_dir=tmp_path / ".hermes", todo_id="TODO-1",
            project_dir=tmp_path, phases_path=None,
            monitor=monitor, detector=detector, poll_interval=0.1,
            phases=[
                Phase(
                    phase_key="phase_4_development",
                    name="Phase 4: Development",
                    prompt="",
                    tools="",
                    turns=0,
                )
            ],
        )

        assert result is False
        lines = events_log.read_text().strip().splitlines()
        events = [_json.loads(l) for l in lines if l.strip()]
        blocked = [e for e in events if e["event_type"] == "phase_blocked"]
        assert len(blocked) == 1
        assert blocked[0]["phase_key"] == "phase_4_development"

    def test_convergence_halt_stops_polling(self, tmp_path, mocker):

        from hermes_pipeline.harness import (
            ConvergenceDetector,
            HarnessMonitor,
            _ConvergenceMonitor,
            _poll_kanban_phases,
        )

        events_log = tmp_path / "events.jsonl"
        base_monitor = HarnessMonitor(events_log)
        detector = ConvergenceDetector(threshold=3)
        monitor = _ConvergenceMonitor(base_monitor, detector, {})

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1", "t2", "t3"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("time.sleep")
        mock_observe = mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")

        call_count = [0]
        def fake_status(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"p1": "running", "p2": "ready", "p3": "blocked"}
            elif call_count[0] == 2:
                return {"p1": "failed", "p2": "running", "p3": "blocked"}
            elif call_count[0] == 3:
                return {"p1": "failed", "p2": "failed", "p3": "blocked"}
            return {"p1": "failed", "p2": "failed", "p3": "failed"}

        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=fake_status)

        result = _poll_kanban_phases(
            project_slug="demo", tick_id="01TICK",
            state_dir=tmp_path / ".hermes", todo_id="TODO-1",
            project_dir=tmp_path, phases_path=None,
            monitor=monitor, detector=detector, poll_interval=0.1,
        )

        assert result is False
        mock_observe.assert_called_once()

    def test_auto_completes_blocked_gates(self, tmp_path, mocker):
        from hermes_pipeline.harness import (
            ConvergenceDetector,
            HarnessMonitor,
            _ConvergenceMonitor,
            _poll_kanban_phases,
        )

        events_log = tmp_path / "events.jsonl"
        base_monitor = HarnessMonitor(events_log)
        detector = ConvergenceDetector(threshold=3)
        monitor = _ConvergenceMonitor(base_monitor, detector, {})

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1", "t2"])
        mock_auto = mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        call_count = [0]
        def fake_status(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"phase_2_autoplan": "running", "phase_2b_plan_gate": "blocked"}
            return {"phase_2_autoplan": "done", "phase_2b_plan_gate": "done"}

        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=fake_status)
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")

        _poll_kanban_phases(
            project_slug="demo", tick_id="01TICK",
            state_dir=tmp_path / ".hermes", todo_id="TODO-1",
            project_dir=tmp_path, phases_path=None,
            monitor=monitor, detector=detector, poll_interval=0.1,
        )

        mock_auto.assert_any_call("demo", "01TICK", completed_phase_key="phase_2_autoplan", phases=None)
        mock_auto.assert_any_call("demo", "01TICK", completed_phase_key="phase_2b_plan_gate", phases=None)
        assert mock_auto.call_count == 2

    def test_emits_phase_failed_when_ready_transitions_directly_to_failed(self, tmp_path, mocker):
        """Regression: a phase can jump straight from ready/blocked to failed
        without ever passing through running. Prior to this fix, such a
        transition was silently absorbed by the terminal-status check without
        emitting phase_failed or being seen by the convergence detector."""
        import json as _json

        from hermes_pipeline.harness import (
            ConvergenceDetector,
            HarnessMonitor,
            _ConvergenceMonitor,
            _poll_kanban_phases,
        )

        events_log = tmp_path / "events.jsonl"
        base_monitor = HarnessMonitor(events_log)
        detector = ConvergenceDetector(threshold=3)
        monitor = _ConvergenceMonitor(base_monitor, detector, {})

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status",
                      return_value={"phase_2_autoplan": "failed"})

        result = _poll_kanban_phases(
            project_slug="demo", tick_id="01TICK",
            state_dir=tmp_path / ".hermes", todo_id="TODO-1",
            project_dir=tmp_path, phases_path=None,
            monitor=monitor, detector=detector, poll_interval=0.1,
        )

        assert result is False
        lines = events_log.read_text().strip().splitlines()
        events = [_json.loads(l) for l in lines if l.strip()]
        failed = [e for e in events if e["event_type"] == "phase_failed"]
        assert len(failed) == 1
        assert failed[0]["phase_key"] == "phase_2_autoplan"

    def test_poll_interval_backs_off_and_resets_on_change(self, tmp_path, mocker):
        """Regression: fixed 5s poll interval added constant load for
        long-running phases. Interval should grow while status is unchanged
        and reset when a transition occurs."""
        from hermes_pipeline.harness import (
            ConvergenceDetector,
            HarnessMonitor,
            _ConvergenceMonitor,
            _poll_kanban_phases,
        )

        events_log = tmp_path / "events.jsonl"
        base_monitor = HarnessMonitor(events_log)
        detector = ConvergenceDetector(threshold=3)
        monitor = _ConvergenceMonitor(base_monitor, detector, {})

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")

        sleep_calls = []
        mocker.patch("time.sleep", side_effect=lambda s: sleep_calls.append(s))

        call_count = [0]
        def fake_status(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 4:
                return {"phase_2_autoplan": "running"}
            return {"phase_2_autoplan": "done"}

        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=fake_status)

        _poll_kanban_phases(
            project_slug="demo", tick_id="01TICK",
            state_dir=tmp_path / ".hermes", todo_id="TODO-1",
            project_dir=tmp_path, phases_path=None,
            monitor=monitor, detector=detector, poll_interval=1.0, max_poll_interval=10.0,
        )

        # Unchanged status ("running" repeated) should back off between polls 2-3.
        assert sleep_calls[2] > sleep_calls[1]
        # Transition to "done" grows the interval for that poll (backoff is only
        # reset for the *next* sleep, after the transition is observed).
        assert sleep_calls[3] > sleep_calls[2]
        # First two sleeps stay at the base interval: poll 1 sees the initial
        # empty->running "change" and resets before poll 2 fires.
        assert sleep_calls[0] == sleep_calls[1] == 1.0

    def test_assignee_resolved_from_contract(self, tmp_path, mocker):
        """register_todo_phases' assignee comes from contract.load_contract."""
        from hermes_pipeline.harness import (
            ConvergenceDetector,
            HarnessMonitor,
            _ConvergenceMonitor,
            _poll_kanban_phases,
        )

        mock_register = mocker.patch(
            "hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1"]
        )
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={"phase_2_autoplan": "done"},
        )
        mock_contract = mocker.Mock(assignee="alice")
        mocker.patch("hermes_pipeline.contract.load_contract", return_value=mock_contract)

        events_log = tmp_path / "events.jsonl"
        base_monitor = HarnessMonitor(events_log)
        detector = ConvergenceDetector(threshold=3)
        monitor = _ConvergenceMonitor(base_monitor, detector, {})

        _poll_kanban_phases(
            project_slug="demo", tick_id="01TICK",
            state_dir=tmp_path / ".hermes", todo_id="TODO-1",
            project_dir=tmp_path, phases_path=None,
            monitor=monitor, detector=detector, poll_interval=0.1,
        )

        assert mock_register.call_args.kwargs["assignee"] == "alice"

    def test_assignee_defaults_when_contract_load_fails(self, tmp_path, mocker, caplog):
        """If load_contract raises, assignee falls back to 'default' and warns."""
        from hermes_pipeline.harness import (
            ConvergenceDetector,
            HarnessMonitor,
            _ConvergenceMonitor,
            _poll_kanban_phases,
        )

        mock_register = mocker.patch(
            "hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1"]
        )
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={"phase_2_autoplan": "done"},
        )
        mocker.patch(
            "hermes_pipeline.contract.load_contract", side_effect=Exception("no contract")
        )

        events_log = tmp_path / "events.jsonl"
        base_monitor = HarnessMonitor(events_log)
        detector = ConvergenceDetector(threshold=3)
        monitor = _ConvergenceMonitor(base_monitor, detector, {})

        with caplog.at_level("WARNING"):
            _poll_kanban_phases(
                project_slug="demo", tick_id="01TICK",
                state_dir=tmp_path / ".hermes", todo_id="TODO-1",
                project_dir=tmp_path, phases_path=None,
                monitor=monitor, detector=detector, poll_interval=0.1,
            )

        assert mock_register.call_args.kwargs["assignee"] == "default"
        assert "failed to load pipeline contract" in caplog.text


class TestResolveSandboxRepo:
    """resolve_sandbox_repo: CLI beats env, strict owner/name shape, slug validity."""

    def test_cli_value_wins_over_env(self):
        repo = resolve_sandbox_repo("cli-owner/cli-repo", {"TPO_HARNESS_REPO": "env-owner/env-repo"})
        assert repo.repo == "cli-owner/cli-repo"

    def test_env_fallback(self):
        repo = resolve_sandbox_repo(None, {"TPO_HARNESS_REPO": "env-owner/env-repo"})
        assert repo.repo == "env-owner/env-repo"

    def test_env_none_reads_os_environ(self):
        with patch.dict(os.environ, {"TPO_HARNESS_REPO": "os-owner/os-repo"}):
            assert resolve_sandbox_repo(None).repo == "os-owner/os-repo"

    @pytest.mark.parametrize("cli_value", [None, "", "   "])
    def test_missing_raises_preflight_error(self, cli_value):
        with pytest.raises(HarnessPreflightError) as exc_info:
            resolve_sandbox_repo(cli_value, {"TPO_HARNESS_REPO": "  "})
        assert isinstance(exc_info.value, RuntimeError)
        assert exc_info.value.code == "repo_missing"
        assert "--repo" in exc_info.value.detail
        assert "TPO_HARNESS_REPO" in exc_info.value.detail
        assert HarnessPreflightError("x").detail == ""

    @pytest.mark.parametrize(
        "bad",
        [
            "nope",
            "a/b/c",
            "owner/name.git",
            "owner/ na me",
            "owner/dot.",
            "-owner/name",
            "acme-/repo",
            "ac--me/repo",
        ],
    )
    def test_rejects_invalid_repo(self, bad):
        with pytest.raises(HarnessPreflightError) as exc_info:
            resolve_sandbox_repo(bad, {})
        assert exc_info.value.code == "invalid_repo"
        assert exc_info.value.detail == bad

    def test_sandbox_repo_rejects_unsafe_repo_on_construction(self):
        with pytest.raises(HarnessPreflightError) as exc_info:
            SandboxRepo("-x/y", "y", "https://github.com/-x/y.git")
        assert exc_info.value.code == "invalid_repo"
        assert exc_info.value.detail == "-x/y"

    def test_slug_is_repo_name(self):
        repo = resolve_sandbox_repo("acme/tpo-sandbox", {})
        assert repo.slug == "tpo-sandbox"
        assert repo.url == "https://github.com/acme/tpo-sandbox.git"

    @pytest.mark.parametrize(
        ("value", "name"),
        [("acme/x", "x"), ("acme/-x", "-x"), ("owner/.dot", ".dot"), ("owner/a..b", "a..b")],
    )
    def test_rejects_invalid_slug(self, value, name):
        with pytest.raises(HarnessPreflightError) as exc_info:
            resolve_sandbox_repo(value, {})
        assert exc_info.value.code == "invalid_slug"
        assert exc_info.value.detail == name


class TestValidateLiveProfile:
    """validate_live_profile: one terminal phase and an allow-listed profile."""

    @pytest.mark.parametrize("name", ["gstack", "agent-skills"])
    def test_gstack_and_agent_skills_ok(self, name):
        phases = load_phases(resolve_profile_phases_path(name))
        validate_live_profile(phases, name)

    def test_native_sdd_rejected_unsafe_terminal(self):
        phases = load_phases(resolve_profile_phases_path("native-sdd"))
        with pytest.raises(HarnessProfileError) as exc_info:
            validate_live_profile(phases, "native-sdd")
        assert exc_info.value.code == "unsafe_terminal"
        assert exc_info.value.profile_name == "native-sdd"

    def test_unknown_profile_rejected(self):
        phases = [Phase(phase_key="p1", name="P1", terminal=True)]
        with pytest.raises(HarnessProfileError) as exc_info:
            validate_live_profile(phases, "custom-profile")
        assert exc_info.value.code == "unsafe_terminal"

    @pytest.mark.parametrize(
        "phases",
        [
            [Phase(phase_key="p1", name="P1")],
            [Phase(phase_key="p1", name="P1", terminal=True), Phase(phase_key="p2", name="P2", terminal=True)],
        ],
        ids=["zero_terminals", "two_terminals"],
    )
    def test_wrong_terminal_count_rejected(self, phases):
        with pytest.raises(HarnessProfileError) as exc_info:
            validate_live_profile(phases, "gstack")
        assert exc_info.value.code == "invalid_terminal_topology"
        assert exc_info.value.detail == "expected exactly one terminal phase"


SANDBOX = SandboxRepo(repo="acme/repo", slug="repo", url="https://github.com/acme/repo.git")
_REPO_VIEW_JQ = '{permission: .viewerPermission, default_branch: (.defaultBranchRef.name // "")}'
_REPO_VIEW_ARGV = (
    "gh", "repo", "view", "acme/repo",
    "--json", "viewerPermission,defaultBranchRef",
    "--jq", _REPO_VIEW_JQ,
)
_USER_ARGV = (*API_ARGV, "user", "--jq", ".login")


def _seed_github(fake, issues=(), *, permission="WRITE", default_branch="main", viewer="octo"):
    """Serve a healthy sandbox: auth ok, viewer login, repo view, and ``issues`` as tpo:todo.

    The repo-view and user rules are registered on the FULL argv so a silent change
    to the production call (fields or jq) fails matching. The fake returns the
    post-jq shape; the ``// ""`` fallback for ``defaultBranchRef: null`` itself is
    verified by the live gate, not here.
    """
    fake.on("gh", "auth", "status")
    fake.on(*_USER_ARGV, stdout=f"{viewer}\n")
    fake.on(
        *_REPO_VIEW_ARGV,
        stdout=json.dumps({"permission": permission, "default_branch": default_branch}) + "\n",
    )
    seed_project_issues(fake, list(issues))
    return fake


class TestGithubPreflight:
    """github_preflight: auth, viewer, push permission, and sandbox quiescence via the gh seam."""

    def test_requires_gh_auth(self, fake_gh, tmp_path):
        fake_gh.on("gh", "auth", "status", rc=1, stderr="You are not logged into any GitHub hosts. To log in, run: gh auth login\n")
        with pytest.raises(GitHubIssuesError) as exc_info:
            github_preflight(tmp_path, SANDBOX)
        assert exc_info.value.code == "gh_auth"
        assert fake_gh.gh_calls()[0][:2] == ["auth", "status"]

    def test_rejects_read_permission(self, fake_gh, tmp_path):
        _seed_github(fake_gh, permission="READ")
        with pytest.raises(HarnessPreflightError) as exc_info:
            github_preflight(tmp_path, SANDBOX)
        assert exc_info.value.code == "gh_permission"
        assert exc_info.value.detail == "READ"

    def test_null_permission_rejected_as_gh_permission(self, fake_gh, tmp_path):
        _seed_github(fake_gh)
        fake_gh.on(*_REPO_VIEW_ARGV, stdout='{"permission": null, "default_branch": "main"}\n')
        with pytest.raises(HarnessPreflightError) as exc_info:
            github_preflight(tmp_path, SANDBOX)
        assert exc_info.value.code == "gh_permission"
        assert exc_info.value.detail == "unknown"

    @pytest.mark.parametrize("permission", ["WRITE", "MAINTAIN", "ADMIN"])
    def test_accepts_write_maintain_admin(self, fake_gh, tmp_path, permission):
        _seed_github(fake_gh, permission=permission, default_branch="trunk", viewer="octo")
        result = github_preflight(tmp_path, SANDBOX)
        assert result == GitHubPreflight(viewer="octo", default_branch="trunk", permission=permission)
        assert list(_REPO_VIEW_ARGV[1:]) in fake_gh.gh_calls()
        assert list(_USER_ARGV[1:]) in fake_gh.gh_calls()
        assert all(kw.get("cwd") == tmp_path for kw in fake_gh.kwargs)

    def test_empty_repo_yields_blank_default_branch(self, fake_gh, tmp_path):
        _seed_github(fake_gh, default_branch="")
        assert github_preflight(tmp_path, SANDBOX).default_branch == ""

    @pytest.mark.parametrize("stdout", ["not json\n", "[]\n", '{"permission": "ADMIN", "default_branch": null}\n'])
    def test_malformed_repo_view_rejected(self, fake_gh, tmp_path, stdout):
        _seed_github(fake_gh)
        fake_gh.on(*_REPO_VIEW_ARGV, stdout=stdout)
        with pytest.raises(HarnessPreflightError) as exc_info:
            github_preflight(tmp_path, SANDBOX)
        assert exc_info.value.code == "gh_invalid"

    def test_repo_view_error_propagates(self, fake_gh, tmp_path):
        _seed_github(fake_gh)
        fake_gh.on(*_REPO_VIEW_ARGV, rc=1, stderr="GraphQL: Could not resolve to a Repository with the name 'acme/repo'.\n")
        with pytest.raises(GitHubIssuesError) as exc_info:
            github_preflight(tmp_path, SANDBOX)
        assert exc_info.value.code == "gh_not_found"

    def test_fails_when_another_ready_todo_is_open(self, fake_gh, tmp_path):
        _seed_github(fake_gh, [todo_payload(15), todo_payload(12)])
        with pytest.raises(HarnessPreflightError) as exc_info:
            github_preflight(tmp_path, SANDBOX)
        assert exc_info.value.code == "sandbox_not_quiescent"
        assert exc_info.value.detail == "#12, #15"
        list_call = next(call for call in fake_gh.gh_calls() if "--paginate" in call)
        assert list_call[-1].startswith("repos/acme/repo/issues?state=open&labels=tpo%3Atodo")

    def test_excludes_own_issue(self, fake_gh, tmp_path):
        _seed_github(fake_gh, [todo_payload(12)])
        assert other_ready_issues(tmp_path, SANDBOX, exclude_issue=12) == ()
        assert github_preflight(tmp_path, SANDBOX, exclude_issue=12).viewer == "octo"

    def test_ignores_open_todo_without_ready_label(self, fake_gh, tmp_path):
        _seed_github(fake_gh, [todo_payload(12, labels=("tpo:todo",)), todo_payload(15)])
        assert other_ready_issues(tmp_path, SANDBOX) == (15,)
        with pytest.raises(HarnessPreflightError) as exc_info:
            github_preflight(tmp_path, SANDBOX)
        assert exc_info.value.detail == "#15"

    @pytest.mark.parametrize("viewer", ["  ", "a\nb"])
    def test_blank_viewer_rejected(self, fake_gh, tmp_path, viewer):
        _seed_github(fake_gh, viewer=viewer)
        with pytest.raises(HarnessPreflightError) as exc_info:
            github_preflight(tmp_path, SANDBOX)
        assert exc_info.value.code == "gh_viewer_unknown"

    def test_viewer_lookup_other_errors_propagate(self, fake_gh, tmp_path):
        _seed_github(fake_gh)
        fake_gh.on(*_USER_ARGV, rc=1, stderr="HTTP 403: API rate limit exceeded\n")
        with pytest.raises(GitHubIssuesError) as exc_info:
            github_preflight(tmp_path, SANDBOX)
        assert exc_info.value.code != "gh_invalid"


def _seed_bare_remote(tmp_path: Path, *, seed_paths: tuple[str, ...]) -> tuple[Path, str]:
    """Create a bare ``main`` remote seeded with *seed_paths*; return (bare, head sha)."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": "/dev/null"}

    def git(*args: str, cwd: Path) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
        ).stdout.strip()

    bare = tmp_path / "remote.git"
    bare.mkdir()
    git("init", "--bare", "-b", "main", cwd=bare)
    work = tmp_path / "seed"
    work.mkdir()
    git("init", "-b", "main", cwd=work)
    git("config", "user.email", "seed@localhost", cwd=work)
    git("config", "user.name", "Seed", cwd=work)
    for rel in seed_paths:
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {rel}\n")
    git("add", ".", cwd=work)
    git("commit", "-m", "seed sandbox", cwd=work)
    git("remote", "add", "origin", f"file://{bare}", cwd=work)
    git("push", "origin", "main", cwd=work)
    return bare, git("rev-parse", "HEAD", cwd=work)


_ALL_SEED_PATHS = ("pyproject.toml", "tests/__init__.py", "docs/harness/SANDBOX.md")


class TestCloneSandbox:
    sandbox = SandboxRepo(repo="acme/sandbox", slug="sandbox", url="https://github.com/acme/sandbox.git")

    def test_clone_uses_seam_argv(self, tmp_path, monkeypatch):
        calls: list[dict] = []

        def fake_git(argv, **kwargs):
            calls.append({"argv": argv, **kwargs})
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr("hermes_pipeline.harness._git", fake_git)
        project_dir = tmp_path / "sandbox"

        clone_sandbox(self.sandbox, project_dir)

        assert calls[0]["argv"] == ["git", "clone", "--", self.sandbox.url, str(project_dir)]
        assert calls[0]["cwd"] == tmp_path
        assert calls[0]["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert [c["argv"] for c in calls[1:]] == [
            ["git", "config", "user.email", "test@localhost"],
            ["git", "config", "user.name", "TPO Harness"],
        ]
        assert all(c["cwd"] == project_dir for c in calls[1:])

    def test_clone_refuses_existing_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "hermes_pipeline.harness._git",
            lambda argv, **kw: pytest.fail("git must not run"),
        )
        project_dir = tmp_path / "sandbox"
        project_dir.mkdir()

        with pytest.raises(HarnessPreflightError) as exc_info:
            clone_sandbox(self.sandbox, project_dir)

        assert exc_info.value.code == "workspace_exists"
        assert exc_info.value.detail == str(project_dir)

    def test_git_failure_raises_git_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "hermes_pipeline.harness._git",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 128, "", "fatal: repository not found\n"),
        )

        with pytest.raises(HarnessPreflightError) as exc_info:
            clone_sandbox(self.sandbox, tmp_path / "sandbox")

        assert exc_info.value.code == "git_error"
        assert "git clone failed" in exc_info.value.detail
        assert "repository not found" in exc_info.value.detail

    def test_clone_creates_missing_parent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "hermes_pipeline.harness._git",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""),
        )
        project_dir = tmp_path / "deep" / "nested" / "sandbox"

        clone_sandbox(self.sandbox, project_dir)

        assert project_dir.parent.is_dir()

    def test_seam_oserror_raises_git_error(self, tmp_path, monkeypatch):
        def broken(argv, **kw):
            raise FileNotFoundError(2, "No such file or directory", "git")

        monkeypatch.setattr("hermes_pipeline.harness._git", broken)

        with pytest.raises(HarnessPreflightError) as exc_info:
            clone_sandbox(self.sandbox, tmp_path / "sandbox")

        assert exc_info.value.code == "git_error"
        assert exc_info.value.detail.startswith("git clone failed: ")
        assert "No such file or directory" in exc_info.value.detail

    def test_git_error_redacts_userinfo_and_disables_prompts(self, tmp_path, monkeypatch):
        seen_env: list[dict] = []

        def fake_git(argv, **kw):
            seen_env.append(kw["env"])
            return subprocess.CompletedProcess(
                argv, 128, "", "fatal: Authentication failed for 'https://user:tok@github.com/x'\n"
            )

        monkeypatch.setattr("hermes_pipeline.harness._git", fake_git)

        with pytest.raises(HarnessPreflightError) as exc_info:
            clone_sandbox(self.sandbox, tmp_path / "sandbox")

        assert "://***@github.com/x" in exc_info.value.detail
        assert "tok" not in exc_info.value.detail
        assert seen_env[0]["GIT_ASKPASS"] == ""
        assert seen_env[0]["GCM_INTERACTIVE"] == "never"

    def test_seed_check_treats_tree_as_missing(self, tmp_path, monkeypatch):
        def fake_git(argv, **kw):
            assert argv[:3] == ["git", "cat-file", "-t"]
            kind = "tree" if argv[3] == "HEAD:tests/__init__.py" else "blob"
            return subprocess.CompletedProcess(argv, 0, f"{kind}\n", "")

        monkeypatch.setattr("hermes_pipeline.harness._git", fake_git)

        with pytest.raises(HarnessPreflightError) as exc_info:
            sandbox_seed_check(tmp_path, self.sandbox)

        assert exc_info.value.code == "sandbox_not_seeded"
        assert "missing: tests/__init__.py;" in exc_info.value.detail

    @pytest.mark.real_git
    def test_clone_from_local_bare_remote_and_seed_check(self, tmp_path):
        bare, sha = _seed_bare_remote(tmp_path, seed_paths=_ALL_SEED_PATHS)
        sandbox = dataclasses.replace(self.sandbox, url=f"file://{bare}")
        project_dir = tmp_path / "workspace" / "sandbox"
        project_dir.parent.mkdir()

        clone_sandbox(sandbox, project_dir)

        assert (project_dir / ".git").is_dir()
        assert (project_dir / "docs/harness/SANDBOX.md").is_file()
        name = subprocess.run(
            ["git", "config", "user.name"], cwd=project_dir, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert name == "TPO Harness"

        sandbox_seed_check(project_dir, sandbox)

        baseline = take_baseline(project_dir, viewer="octocat", default_branch="main")
        assert isinstance(baseline, RunBaseline)
        assert dict(baseline.heads) == {"main": sha}
        assert baseline.viewer == "octocat"
        assert baseline.default_branch == "main"

    @pytest.mark.real_git
    def test_seed_check_reports_missing_paths(self, tmp_path):
        bare, _sha = _seed_bare_remote(tmp_path, seed_paths=("pyproject.toml", "tests/__init__.py"))
        sandbox = dataclasses.replace(self.sandbox, url=f"file://{bare}")
        project_dir = tmp_path / "sandbox"
        clone_sandbox(sandbox, project_dir)

        with pytest.raises(HarnessPreflightError) as exc_info:
            sandbox_seed_check(project_dir, sandbox)

        assert exc_info.value.code == "sandbox_not_seeded"
        assert "docs/harness/SANDBOX.md" in exc_info.value.detail
        assert "pyproject.toml" not in exc_info.value.detail
        assert "tpo test --repo acme/sandbox --init-sandbox" in exc_info.value.detail

        # Present on disk but not tracked at HEAD must still count as missing.
        on_disk = project_dir / "docs" / "harness" / "SANDBOX.md"
        on_disk.parent.mkdir(parents=True)
        on_disk.write_text("# uncommitted\n")
        with pytest.raises(HarnessPreflightError) as exc_info:
            sandbox_seed_check(project_dir, sandbox)
        assert exc_info.value.code == "sandbox_not_seeded"
        assert "docs/harness/SANDBOX.md" in exc_info.value.detail

    def test_take_baseline_floors_started_at(self, tmp_path, monkeypatch):
        sha = "a" * 40
        sha2 = "b" * 40
        seen: list[list[str]] = []

        stdout = (
            f"ref: refs/heads/main\tHEAD\n"
            f"{sha}\tHEAD\n"
            f"{sha}\trefs/heads/main\n"
            f"{sha2}\trefs/heads/feat/x\n"
            f"{sha2}\trefs/tags/v1\n"
            f"{sha}\trefs/tags/v1^{{}}\n"
            f"{'c' * 40} refs/heads/space-separated\n"
        )

        def fake_git(argv, **kwargs):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout, "")

        monkeypatch.setattr("hermes_pipeline.harness._git", fake_git)
        now = datetime(2026, 9, 1, 12, 0, 0, 750000, tzinfo=UTC)

        baseline = take_baseline(tmp_path, viewer="octocat", default_branch="main", now=now)

        assert seen == [["git", "ls-remote", "--heads", "origin"]]
        assert baseline.started_at == datetime(2026, 9, 1, 11, 59, 59, tzinfo=UTC)
        assert dict(baseline.heads) == {"main": sha, "feat/x": sha2}
        with pytest.raises(TypeError):
            baseline.heads["main"] = sha2  # type: ignore[index]
        assert dataclasses.asdict(baseline)["head_pairs"] == (("main", sha), ("feat/x", sha2))

    def test_take_baseline_normalizes_aware_now_to_utc(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "hermes_pipeline.harness._git",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""),
        )
        seoul = timezone(timedelta(hours=9))
        now = datetime(2026, 9, 1, 21, 0, 0, 250000, tzinfo=seoul)

        baseline = take_baseline(tmp_path, viewer="octocat", default_branch="main", now=now)

        assert baseline.started_at == datetime(2026, 9, 1, 11, 59, 59, tzinfo=UTC)
        assert baseline.started_at.tzinfo is UTC
        assert dict(baseline.heads) == {}

    def test_take_baseline_rejects_naive_now(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "hermes_pipeline.harness._git",
            lambda argv, **kw: pytest.fail("git must not run"),
        )

        with pytest.raises(ValueError, match="timezone-aware"):
            take_baseline(tmp_path, viewer="octocat", default_branch="main", now=datetime(2026, 9, 1))


class TestWriteProjectContract:
    def test_write_project_contract_writes_expected_toml(self, tmp_path):
        from hermes_pipeline.contract import required_capabilities

        write_project_contract(tmp_path, "gstack")

        text = (tmp_path / ".hermes" / "pipeline.toml").read_text()
        assert text.splitlines()[:2] == [
            "# Pipeline execution contract — read at tick start.",
            "# See docs/tutorial-getting-started.md and `tpo doctor --help`.",
        ]
        expected = sorted(required_capabilities(load_phases(resolve_profile_phases_path("gstack"))))
        assert tomllib.loads(text) == {
            "schema_version": 2,
            "assignee": "pipeline",
            "capabilities": expected,
            "profile": "gstack",
        }

    def test_write_project_contract_reuses_existing_hermes_dir(self, tmp_path):
        (tmp_path / ".hermes").mkdir()
        (tmp_path / ".hermes" / "keep").write_text("x")

        write_project_contract(tmp_path, "gstack")

        assert (tmp_path / ".hermes" / "keep").read_text() == "x"
        assert (tmp_path / ".hermes" / "pipeline.toml").is_file()

    def test_create_mock_project_unknown_profile_fails_before_touching_fs(self, tmp_path):
        target = tmp_path / "mock"

        with pytest.raises(ContractSchemaError):
            create_mock_project(target, "simple", profile_name="no-such-profile")

        assert not target.exists()
