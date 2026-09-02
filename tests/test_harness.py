"""Unit tests for harness.py — fixture factory, preflight, convergence, monitor."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import subprocess
import sys
import tomllib
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from hermes_pipeline import harness as harness_mod
from hermes_pipeline.config import Config
from hermes_pipeline.contract import ContractSchemaError
from hermes_pipeline.github_issues import (
    LABEL_VOCABULARY,
    GitHubIssuesError,
    compile_eligible_issues,
    issue_from_api,
    parse_issue_body,
    render_issue_body,
)
from hermes_pipeline.harness import (
    ConvergenceDetector,
    ConvergenceHaltError,
    GitHubPreflight,
    HarnessIssue,
    HarnessMonitor,
    HarnessPreflightError,
    HarnessProfileError,
    HarnessRemoteCleanupError,
    HarnessResult,
    HarnessTickError,
    PullRequest,
    PullRequestInvariantError,
    RemoteArtifacts,
    RunBaseline,
    SandboxRepo,
    TickRegistration,
    _build_harness_profile_data,
    _classify_error_class,
    _ConvergenceMonitor,
    _offline_terminal_phase_key,
    _prune_retained_state,
    _validate_profile_prerequisites,
    _with_offline_terminal_workflow,
    branch_has_run_provenance,
    cards_for_registered_keys,
    clone_sandbox,
    commit_plan,
    create_harness_issue,
    create_mock_project,
    discover_candidate_prs,
    discover_remote_artifacts,
    fetch_pull_request,
    filter_phases,
    github_preflight,
    init_sandbox,
    is_attributable_pr,
    is_deletable_branch,
    isolate_config,
    other_ready_issues,
    pr_invariant_event,
    preflight_check,
    read_current_tick_id,
    read_recorded_branch,
    reconcile_created_issue,
    recover_tick_registration,
    resolve_sandbox_repo,
    run_harness,
    run_tick,
    sandbox_seed_check,
    take_baseline,
    validate_live_profile,
    verify_pull_request,
    write_project_contract,
)
from hermes_pipeline.phases import Phase, load_phases, resolve_profile_phases_path
from hermes_pipeline.plan_manifest import validate_plan_candidate
from tests.gh_fakes import API_ARGV, issue_payload, seed_project_issues, todo_payload


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
    def test_sets_env_vars(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("TPO_CONFIG_FILE", raising=False)
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        with isolate_config(state_dir=state_dir, projects_dir=tmp_path / "projects"):
            assert os.environ.get("TPO_CONFIG_FILE") == str(state_dir / "tpo-config.yaml")

        assert "TPO_CONFIG_FILE" not in os.environ

    def test_saves_and_restores(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("TPO_CONFIG_FILE", "/original/config.yaml")
        state_dir = tmp_path / "state"

        with isolate_config(state_dir=state_dir, projects_dir=tmp_path / "projects"):
            assert os.environ["TPO_CONFIG_FILE"] == str(state_dir / "tpo-config.yaml")

        assert os.environ["TPO_CONFIG_FILE"] == "/original/config.yaml"

    def test_writes_full_config_and_creates_dirs(self, tmp_path: Path):
        import yaml

        state_dir = tmp_path / "state"
        projects_dir = tmp_path / "nested" / "projects"

        with isolate_config(state_dir=state_dir, projects_dir=projects_dir, prompt_client="hermes"):
            data = yaml.safe_load(Path(os.environ["TPO_CONFIG_FILE"]).read_text())
            assert data == {
                "state_dir": str(state_dir),
                "projects_dir": str(projects_dir),
                "prompt_client": "hermes",
            }
            assert state_dir.is_dir()
            assert projects_dir.is_dir()

    def test_prompt_client_defaults_to_claude(self, tmp_path: Path):
        import yaml

        with isolate_config(state_dir=tmp_path / "s", projects_dir=tmp_path / "p"):
            data = yaml.safe_load(Path(os.environ["TPO_CONFIG_FILE"]).read_text())
        assert data["prompt_client"] == "claude"


class TestRunTick:
    def test_invokes_cli_main_via_sys_executable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("TPO_CONFIG_FILE", str(tmp_path / "tpo-config.yaml"))
        calls: list[dict] = []

        def recorder(argv, **kwargs):
            calls.append({"argv": argv, **kwargs})
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(harness_mod, "_tick_runner", recorder)
        cwd = tmp_path / "projects"
        cwd.mkdir()

        rc = run_tick("sandbox", cwd=cwd, log_path=tmp_path / "logs" / "tick.log", timeout=42.0)

        assert rc == 0
        assert len(calls) == 1
        call = calls[0]
        argv = call["argv"]
        assert argv[0] == sys.executable
        assert argv[1] == "-c"
        assert argv[2] == harness_mod._TICK_ENTRYPOINT
        assert argv[3:] == ["tick", "sandbox"]
        assert call["cwd"] == cwd
        assert call["timeout"] == 42.0
        assert call["capture_output"] is True
        assert call["text"] is True
        assert call["encoding"] == "utf-8"
        assert call["errors"] == "replace"
        assert call["env"]["TPO_CONFIG_FILE"] == str(tmp_path / "tpo-config.yaml")

    def test_entrypoint_smoke_runs_cli_help(self, tmp_path: Path):
        result = subprocess.run(
            [sys.executable, "-c", harness_mod._TICK_ENTRYPOINT, "--help"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert result.returncode == 0, result.stderr
        assert "tick" in result.stdout

    def test_env_overrides_merge_over_os_environ(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("TPO_CONFIG_FILE", "/iso/config.yaml")
        seen: dict = {}

        def recorder(argv, **kwargs):
            seen.update(kwargs["env"])
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(harness_mod, "_tick_runner", recorder)

        run_tick(
            "sandbox", cwd=tmp_path, log_path=tmp_path / "tick.log", timeout=1.0, env={"EXTRA": "1"}
        )

        assert seen["TPO_CONFIG_FILE"] == "/iso/config.yaml"
        assert seen["EXTRA"] == "1"

    def test_appends_output_and_header_to_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        def fake(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 3, stdout="out line\n", stderr="err line\n")

        monkeypatch.setattr(harness_mod, "_tick_runner", fake)
        log_path = tmp_path / "logs" / "tick.log"
        log_path.parent.mkdir()
        log_path.write_text("previous\n")

        rc = run_tick("sandbox", cwd=tmp_path, log_path=log_path, timeout=5.0)

        assert rc == 3
        text = log_path.read_text()
        assert text.startswith("previous\n")
        header = text.splitlines()[1]
        assert header.startswith("# tpo tick sandbox rc=3 ")
        assert header.endswith("+00:00")
        assert "out line\n" in text
        assert "err line\n" in text

    @pytest.mark.parametrize(
        ("output", "stderr"),
        [
            ("partial", None),
            (b"partial", None),
            ("partial", "err-partial"),
            (b"partial", b"err-partial"),
        ],
        ids=["str-out", "bytes-out", "str-both", "bytes-both"],
    )
    def test_timeout_writes_partial_and_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output, stderr
    ):
        def fake(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 5, output=output, stderr=stderr)

        monkeypatch.setattr(harness_mod, "_tick_runner", fake)
        log_path = tmp_path / "deep" / "logs" / "tick.log"

        with pytest.raises(HarnessTickError) as excinfo:
            run_tick("sandbox", cwd=tmp_path, log_path=log_path, timeout=5.0)

        assert excinfo.value.code == "tick_timeout"
        assert excinfo.value.detail == "5.0s"
        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert lines[0].startswith("# tpo tick sandbox rc=timeout ")
        assert "partial" in lines
        if stderr is not None:
            assert "err-partial" in lines
        assert lines[-1] == "# timeout after 5.0s"


def _write_tick_state(
    project_state: Path,
    *,
    tick_id: str = "01TICK",
    phases_outcome: str | None = "tick_started",
    expected_phases: object = ("plan", "implement"),
    spawn_outcome: dict | None = None,
) -> None:
    outcomes = project_state / "outcomes"
    outcomes.mkdir(parents=True, exist_ok=True)
    (project_state / "current_tick_id.txt").write_text(tick_id + "\n")
    if phases_outcome is not None:
        (outcomes / f"{tick_id}-phases.json").write_text(
            json.dumps({"outcome": phases_outcome}) + "\n"
        )
    if expected_phases is not None:
        (outcomes / "expected-phases.json").write_text(json.dumps(expected_phases))
    if spawn_outcome is not None:
        (outcomes / f"{tick_id}.json").write_text(json.dumps(spawn_outcome) + "\n")


_SPAWN_FAILURE_DETAIL = {"todo_id": "TODO-1", "reason": "phase_spawn", "error_type": "OSError"}


class TestReadCurrentTickId:
    def test_missing_returns_none(self, tmp_path: Path):
        assert read_current_tick_id(tmp_path) is None

    def test_blank_returns_none(self, tmp_path: Path):
        (tmp_path / "current_tick_id.txt").write_text(" \n")
        assert read_current_tick_id(tmp_path) is None

    def test_returns_stripped_id(self, tmp_path: Path):
        (tmp_path / "current_tick_id.txt").write_text("01TICK\n")
        assert read_current_tick_id(tmp_path) == "01TICK"


class TestRecoverTickRegistration:
    def test_happy_path(self, tmp_path: Path):
        state = tmp_path / ".hermes"
        _write_tick_state(state, expected_phases=["plan", "implement", "review"])

        reg = recover_tick_registration(state, expected_issue=17)

        assert reg == TickRegistration(
            tick_id="01TICK", todo_id="TODO-17", phase_keys=("plan", "implement", "review")
        )

    def test_changed_tick_id_is_recovered(self, tmp_path: Path):
        state = tmp_path / ".hermes"
        _write_tick_state(state, tick_id="02TICK")

        reg = recover_tick_registration(state, expected_issue=1, previous_tick_id="01TICK")

        assert reg.tick_id == "02TICK"

    def test_unchanged_tick_id_is_stale(self, tmp_path: Path):
        state = tmp_path / ".hermes"
        _write_tick_state(state, tick_id="01TICK")
        log = tmp_path / "tick.log"
        log.write_text("boom\n")

        with pytest.raises(HarnessTickError) as excinfo:
            recover_tick_registration(
                state, expected_issue=1, previous_tick_id="01TICK", tick_log=log
            )

        assert excinfo.value.code == "tick_not_persisted"
        assert "boom" in excinfo.value.detail

    def test_phases_file_may_have_appended_entries(self, tmp_path: Path):
        state = tmp_path / ".hermes"
        _write_tick_state(state)
        with (state / "outcomes" / "01TICK-phases.json").open("a") as fh:
            fh.write(json.dumps({"phase_key": "plan", "outcome": "done"}) + "\n")

        reg = recover_tick_registration(state, expected_issue=1)

        assert reg.tick_id == "01TICK"

    def test_missing_tick_id_includes_log_tail(self, tmp_path: Path):
        state = tmp_path / ".hermes"
        state.mkdir()
        log = tmp_path / "tick.log"
        log.write_text("\n".join(f"line {i}" for i in range(30)) + "\n")

        with pytest.raises(HarnessTickError) as excinfo:
            recover_tick_registration(state, expected_issue=1, tick_log=log)

        assert excinfo.value.code == "tick_not_persisted"
        assert "line 29" in excinfo.value.detail
        assert "line 10" in excinfo.value.detail
        assert "line 9" not in excinfo.value.detail

    def test_blank_tick_id(self, tmp_path: Path):
        state = tmp_path / ".hermes"
        state.mkdir()
        (state / "current_tick_id.txt").write_text("  \n")

        with pytest.raises(HarnessTickError) as excinfo:
            recover_tick_registration(state, expected_issue=1)

        assert excinfo.value.code == "tick_not_persisted"
        assert excinfo.value.detail == ""

    def test_pre_persist_spawn_failure_is_reported(self, tmp_path: Path):
        # The tick id file still holds the previous run's id (or is absent) but a
        # fresh <tick>.json records failed_to_spawn -> surface that instead.
        state = tmp_path / ".hermes"
        _write_tick_state(state, tick_id="01TICK")
        (state / "outcomes" / "02TICK.json").write_text(
            json.dumps(
                {"tick_id": "02TICK", "outcome": "failed_to_spawn", "detail": _SPAWN_FAILURE_DETAIL}
            )
            + "\n"
        )

        with pytest.raises(HarnessTickError) as excinfo:
            recover_tick_registration(state, expected_issue=1, previous_tick_id="01TICK")

        assert excinfo.value.code == "failed_to_spawn"
        assert json.loads(excinfo.value.detail) == _SPAWN_FAILURE_DETAIL

    def test_pre_persist_spawn_failure_without_tick_id_file(self, tmp_path: Path):
        state = tmp_path / ".hermes"
        outcomes = state / "outcomes"
        outcomes.mkdir(parents=True)
        (outcomes / "02TICK.json").write_text(
            json.dumps(
                {"tick_id": "02TICK", "outcome": "failed_to_spawn", "detail": _SPAWN_FAILURE_DETAIL}
            )
        )

        with pytest.raises(HarnessTickError) as excinfo:
            recover_tick_registration(state, expected_issue=1)

        assert excinfo.value.code == "failed_to_spawn"

    def test_stale_spawn_failure_from_previous_tick_is_ignored(self, tmp_path: Path):
        state = tmp_path / ".hermes"
        _write_tick_state(
            state,
            tick_id="01TICK",
            spawn_outcome={
                "tick_id": "01TICK",
                "outcome": "failed_to_spawn",
                "detail": _SPAWN_FAILURE_DETAIL,
            },
        )

        with pytest.raises(HarnessTickError) as excinfo:
            recover_tick_registration(state, expected_issue=1, previous_tick_id="01TICK")

        assert excinfo.value.code == "tick_not_persisted"

    def test_picked_none(self, tmp_path: Path):
        state = tmp_path / ".hermes"
        _write_tick_state(state, phases_outcome="picked_none")

        with pytest.raises(HarnessTickError) as excinfo:
            recover_tick_registration(state, expected_issue=1)

        assert (excinfo.value.code, excinfo.value.detail) == ("picked_none", "01TICK")

    def test_tick_not_started_when_phases_file_missing(self, tmp_path: Path):
        state = tmp_path / ".hermes"
        _write_tick_state(state, phases_outcome=None)

        with pytest.raises(HarnessTickError) as excinfo:
            recover_tick_registration(state, expected_issue=1)

        assert (excinfo.value.code, excinfo.value.detail) == ("tick_not_started", "01TICK")

    def test_tick_not_started_when_outcome_unknown(self, tmp_path: Path):
        state = tmp_path / ".hermes"
        _write_tick_state(state, phases_outcome="something_else")

        with pytest.raises(HarnessTickError) as excinfo:
            recover_tick_registration(state, expected_issue=1)

        assert excinfo.value.code == "tick_not_started"

    def test_failed_to_spawn(self, tmp_path: Path):
        state = tmp_path / ".hermes"
        _write_tick_state(
            state,
            spawn_outcome={
                "tick_id": "01TICK",
                "outcome": "failed_to_spawn",
                "detail": _SPAWN_FAILURE_DETAIL,
            },
        )

        with pytest.raises(HarnessTickError) as excinfo:
            recover_tick_registration(state, expected_issue=1)

        assert excinfo.value.code == "failed_to_spawn"
        assert excinfo.value.detail == json.dumps(
            _SPAWN_FAILURE_DETAIL, separators=(",", ":"), sort_keys=True
        )

    def test_other_outcome_file_does_not_block(self, tmp_path: Path):
        state = tmp_path / ".hermes"
        _write_tick_state(
            state, spawn_outcome={"tick_id": "01TICK", "outcome": "spawned", "detail": {}}
        )

        reg = recover_tick_registration(state, expected_issue=1)

        assert reg.phase_keys == ("plan", "implement")

    @pytest.mark.parametrize(
        "expected",
        [None, [], {"a": 1}, ["plan", 3]],
        ids=["missing", "empty", "not-list", "non-str-item"],
    )
    def test_expected_phases_invalid(self, tmp_path: Path, expected):
        state = tmp_path / ".hermes"
        _write_tick_state(state, expected_phases=expected)

        with pytest.raises(HarnessTickError) as excinfo:
            recover_tick_registration(state, expected_issue=1)

        assert (excinfo.value.code, excinfo.value.detail) == (
            "expected_phases_missing",
            "01TICK",
        )


class TestCardsForRegisteredKeys:
    PHASES: ClassVar[list[Phase]] = [
        Phase(phase_key="plan", name="Plan"),
        Phase(phase_key="gate", name="Gate", gate=True),
        Phase(phase_key="implement", name="Implement"),
        Phase(phase_key="done", name="Done", terminal=True),
    ]

    def test_preserves_registration_order_with_flags(self):
        cards = cards_for_registered_keys(self.PHASES, ["implement", "gate", "done"])

        assert [c.phase_key for c in cards] == ["implement", "gate", "done"]
        assert [c.gate for c in cards] == [False, True, False]
        assert cards[2].terminal is True
        assert cards[1] is self.PHASES[1]

    def test_unknown_key(self):
        with pytest.raises(HarnessTickError) as excinfo:
            cards_for_registered_keys(self.PHASES, ["plan", "nope"])

        assert (excinfo.value.code, excinfo.value.detail) == ("unexpected_registration", "nope")

    def test_duplicate_key(self):
        with pytest.raises(HarnessTickError) as excinfo:
            cards_for_registered_keys(self.PHASES, ["plan", "gate", "plan"])

        assert (excinfo.value.code, excinfo.value.detail) == ("unexpected_registration", "plan")

    def test_empty_keys(self):
        assert cards_for_registered_keys(self.PHASES, []) == []


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

    def test_clone_branch_keyword_adds_branch_flag(self, tmp_path, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(
            "hermes_pipeline.harness._git",
            lambda argv, **kw: (calls.append(argv), subprocess.CompletedProcess(argv, 0, "", ""))[1],
        )
        project_dir = tmp_path / "sandbox"

        clone_sandbox(self.sandbox, project_dir, branch="develop")

        assert calls[0] == ["git", "clone", "--branch", "develop", "--", self.sandbox.url, str(project_dir)]

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

        baseline = take_baseline(project_dir, sandbox, viewer="octocat", default_branch="main")
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
        )

        def fake_git(argv, **kwargs):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout, "")

        monkeypatch.setattr("hermes_pipeline.harness._git", fake_git)
        now = datetime(2026, 9, 1, 12, 0, 0, 750000, tzinfo=UTC)

        baseline = take_baseline(tmp_path, self.sandbox, viewer="octocat", default_branch="main", now=now)

        # Enumeration targets the sandbox URL, never the clone's agent-mutable ``origin``.
        assert seen == [["git", "ls-remote", "--heads", "--", "https://github.com/acme/sandbox.git"]]
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

        baseline = take_baseline(tmp_path, self.sandbox, viewer="octocat", default_branch="main", now=now)

        assert baseline.started_at == datetime(2026, 9, 1, 11, 59, 59, tzinfo=UTC)
        assert baseline.started_at.tzinfo is UTC
        assert dict(baseline.heads) == {}

    def test_take_baseline_rejects_unparseable_ls_remote_line(self, tmp_path, monkeypatch):
        stdout = f"{'a' * 40}\trefs/heads/main\n{'c' * 40} refs/heads/space-separated\n"
        monkeypatch.setattr(
            "hermes_pipeline.harness._git",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout, ""),
        )

        with pytest.raises(HarnessPreflightError) as exc_info:
            take_baseline(tmp_path, self.sandbox, viewer="octocat", default_branch="main")

        assert exc_info.value.code == "git_error"
        assert "space-separated" in exc_info.value.detail

    def test_take_baseline_rejects_non_hex_sha(self, tmp_path, monkeypatch):
        stdout = f"{'a' * 40}\trefs/heads/main\n{'z' * 40}\trefs/heads/feat/x\n"
        monkeypatch.setattr(
            "hermes_pipeline.harness._git",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout, ""),
        )

        with pytest.raises(HarnessPreflightError) as exc_info:
            take_baseline(tmp_path, self.sandbox, viewer="octocat", default_branch="main")

        assert exc_info.value.code == "git_error"

    def test_take_baseline_rejects_naive_now(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "hermes_pipeline.harness._git",
            lambda argv, **kw: pytest.fail("git must not run"),
        )

        with pytest.raises(ValueError, match="timezone-aware"):
            take_baseline(tmp_path, self.sandbox, viewer="octocat", default_branch="main", now=datetime(2026, 9, 1))


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


_SANDBOX_VIEW_ARGV = (
    "gh", "repo", "view", "acme/sandbox",
    "--json", "defaultBranchRef",
    "--jq", '.defaultBranchRef.name // ""',
)
_SANDBOX_PATCH_ARGV = ["api", "-X", "PATCH", "repos/acme/sandbox", "-f", "default_branch=main"]
_SEED_SUBJECT = "chore(harness): seed sandbox"


def _real_git(*args: str, cwd: Path) -> str:
    # Env is read at call time so monkeypatched variables reach the helper; the
    # global config is nulled so operator settings never leak into fixtures.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": "/dev/null"}
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _push_files(bare: Path, work: Path, files: dict[str, str], *, branch: str, subject: str = "seed") -> str:
    """Init *work*, commit *files* on *branch*, push to *bare*; return the commit sha."""
    work.mkdir()
    _real_git("init", "-b", branch, cwd=work)
    _real_git("config", "user.email", "seed@localhost", cwd=work)
    _real_git("config", "user.name", "Seed", cwd=work)
    for rel, content in files.items():
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    _real_git("add", ".", cwd=work)
    _real_git("commit", "-m", subject, cwd=work)
    _real_git("remote", "add", "origin", f"file://{bare}", cwd=work)
    _real_git("push", "origin", branch, cwd=work)
    return _real_git("rev-parse", "HEAD", cwd=work)


def _make_bare_remote(tmp_path: Path, files: dict[str, str], *, branch: str = "main") -> Path:
    """Bare remote on *branch* tracking *files*; no commits at all when *files* is empty."""
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _real_git("init", "--bare", "-b", branch, cwd=bare)
    if files:
        _push_files(bare, tmp_path / "seed", files, branch=branch)
    return bare


def _advance_remote(bare: Path, tmp_path: Path, branch: str) -> str:
    """Add one commit on *branch* of *bare* from a fresh clone; return the new tip."""
    work = tmp_path / "advance"
    _real_git("clone", "-b", branch, f"file://{bare}", str(work), cwd=tmp_path)
    _real_git("config", "user.email", "other@localhost", cwd=work)
    _real_git("config", "user.name", "Other", cwd=work)
    (work / "RACE.txt").write_text("racing commit\n")
    _real_git("add", "RACE.txt", cwd=work)
    _real_git("commit", "-m", "race", cwd=work)
    _real_git("push", "origin", branch, cwd=work)
    return _real_git("rev-parse", "HEAD", cwd=work)


def _remote_branches(bare: Path) -> list[str]:
    return _real_git("for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=bare).splitlines()


def _remote_tree(bare: Path, branch: str) -> dict[str, str]:
    names = _real_git("ls-tree", "-r", "--name-only", branch, cwd=bare).splitlines()
    return {n: _real_git("show", f"{branch}:{n}", cwd=bare) for n in names}


def _assert_porcelain_clean_with_runtime_junk(clone: Path) -> None:
    for rel in (
        ".hermes/pipeline.toml",
        ".hermes/outcomes/x.json",
        "__pycache__/m.pyc",
        ".venv/bin/python",
        ".superpowers/scratch.md",
        ".code-review-graph/graph.json",
    ):
        target = clone / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
    assert _real_git("status", "--porcelain", cwd=clone) == ""


class TestInitSandbox:
    # Offline url: a forgotten ``dataclasses.replace(url=...)`` fails fast instead of
    # reaching GitHub.
    sandbox = SandboxRepo(repo="acme/sandbox", slug="sandbox", url="file:///nonexistent-sandbox")

    def test_seed_paths_are_subset_of_seed_files(self):
        assert set(harness_mod._SANDBOX_SEED_PATHS) <= set(harness_mod._SANDBOX_SEED_FILES)
        assert set(harness_mod._SANDBOX_SEED_FILES) == {
            "README.md", ".gitignore", "pyproject.toml", "tests/__init__.py", "docs/harness/SANDBOX.md"
        }
        assert ".hermes/\n" in harness_mod._SANDBOX_GITIGNORE

    def _serve_gh(self, fake_gh, bare: Path | None, *, default_branch: str | None = None):
        """Serve gh against the bare remote's real state.

        With *default_branch* given, ``gh repo view`` always reports it. Otherwise it
        reports ``main`` once ``refs/heads/main`` exists in *bare* and ``""`` before,
        and PATCH fails (as GitHub does) while the repository has no branch at all.
        """

        def branches() -> list[str]:
            return _remote_branches(bare) if bare is not None else []

        def view(argv):
            if default_branch is not None:
                return 0, f"{default_branch}\n", ""
            return 0, ("main\n" if "main" in branches() else "\n"), ""

        def patch(argv):
            if not branches():
                return 1, "", "HTTP 422: Cannot update default branch for an empty repository\n"
            return 0, "{}\n", ""

        fake_gh.on("gh", "auth", "status")
        fake_gh.on(*_SANDBOX_VIEW_ARGV, handler=view)
        fake_gh.on("gh", *_SANDBOX_PATCH_ARGV, handler=patch)

    # --- empty repository path -------------------------------------------------

    @pytest.mark.real_git
    def test_empty_remote_creates_main_and_seeds(self, fake_gh, tmp_path):
        bare = _make_bare_remote(tmp_path, {})
        sandbox = dataclasses.replace(self.sandbox, url=f"file://{bare}")
        self._serve_gh(fake_gh, bare)
        workspace = tmp_path / "workspace"

        assert init_sandbox(sandbox, workspace) == "seeded"

        assert _remote_branches(bare) == ["main"]
        tree = _remote_tree(bare, "refs/heads/main")
        assert set(tree) == set(harness_mod._SANDBOX_SEED_FILES)
        assert _real_git("log", "-1", "--format=%s", "main", cwd=bare) == _SEED_SUBJECT
        assert "seed_version: 1" in tree["docs/harness/SANDBOX.md"]
        assert 'testpaths = ["tests"]' in tree["pyproject.toml"]
        assert "pytest>=8" in tree["pyproject.toml"]
        assert "TPO harness sandbox" in tree["README.md"]
        assert ".hermes/" in tree[".gitignore"].splitlines()
        assert _SANDBOX_PATCH_ARGV in fake_gh.gh_calls()
        _assert_porcelain_clean_with_runtime_junk(workspace / "sandbox")

    @pytest.mark.real_git
    def test_empty_path_commits_even_when_operator_config_requires_signing(
        self, fake_gh, tmp_path, monkeypatch
    ):
        bare = _make_bare_remote(tmp_path, {})
        sandbox = dataclasses.replace(self.sandbox, url=f"file://{bare}")
        self._serve_gh(fake_gh, bare)
        gitconfig = tmp_path / "operator.gitconfig"
        gitconfig.write_text("[commit]\n\tgpgsign = true\n[user]\n\tsigningkey = 0000DEAD\n")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))

        assert init_sandbox(sandbox, tmp_path / "workspace") == "seeded"

        assert _remote_branches(bare) == ["main"]

    def test_default_branch_not_set_after_patch_raises(self, fake_gh, tmp_path, monkeypatch):
        # Defensive: the view handler ignores remote state and keeps answering "" even
        # after a successful PATCH, which a real GitHub never does.
        monkeypatch.setattr(
            "hermes_pipeline.harness._git", self._seam([], ls_remote="", head="main", tracked="")
        )
        fake_gh.on("gh", "auth", "status")
        fake_gh.on(*_SANDBOX_VIEW_ARGV, stdout="\n")
        fake_gh.on("gh", *_SANDBOX_PATCH_ARGV, stdout="{}\n")

        with pytest.raises(HarnessPreflightError) as exc_info:
            init_sandbox(self.sandbox, tmp_path / "workspace")

        assert exc_info.value.code == "default_branch_unset"
        assert _SANDBOX_PATCH_ARGV in fake_gh.gh_calls()

    @pytest.mark.real_git
    def test_refs_present_but_gh_reports_no_default_branch_is_refused(self, fake_gh, tmp_path):
        bare = _make_bare_remote(tmp_path, {"README.md": "# x\n"}, branch="develop")
        before = _real_git("rev-parse", "develop", cwd=bare)
        sandbox = dataclasses.replace(self.sandbox, url=f"file://{bare}")
        self._serve_gh(fake_gh, bare, default_branch="")

        with pytest.raises(HarnessPreflightError) as exc_info:
            init_sandbox(sandbox, tmp_path / "workspace")

        assert exc_info.value.code == "default_branch_unknown"
        assert "refs/heads/develop" in exc_info.value.detail
        assert _remote_branches(bare) == ["develop"]
        assert _real_git("rev-parse", "develop", cwd=bare) == before
        assert _SANDBOX_PATCH_ARGV not in fake_gh.gh_calls()
        assert not (tmp_path / "workspace" / "sandbox").exists()

    @pytest.mark.real_git
    def test_empty_path_refuses_existing_workspace_dir(self, fake_gh, tmp_path):
        bare = _make_bare_remote(tmp_path, {})
        sandbox = dataclasses.replace(self.sandbox, url=f"file://{bare}")
        self._serve_gh(fake_gh, bare)
        project_dir = tmp_path / "workspace" / "sandbox"
        project_dir.mkdir(parents=True)
        (project_dir / "keep").write_text("x")

        with pytest.raises(HarnessPreflightError) as exc_info:
            init_sandbox(sandbox, tmp_path / "workspace")

        assert exc_info.value.code == "workspace_exists"
        assert (project_dir / "keep").is_file()
        assert _remote_branches(bare) == []

    @pytest.mark.real_git
    def test_failed_patch_propagates_and_removes_project_dir(self, fake_gh, tmp_path):
        bare = _make_bare_remote(tmp_path, {})
        sandbox = dataclasses.replace(self.sandbox, url=f"file://{bare}")
        self._serve_gh(fake_gh, bare)
        fake_gh.on("gh", *_SANDBOX_PATCH_ARGV, rc=1, stderr="HTTP 500: boom\n")
        workspace = tmp_path / "workspace"

        with pytest.raises(GitHubIssuesError):
            init_sandbox(sandbox, workspace)

        assert not (workspace / "sandbox").exists()

    def test_creates_workspace_before_running_gh(self, fake_gh, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "hermes_pipeline.harness._git",
            self._seam([], ls_remote="abc\trefs/heads/main\n", head="main", tracked=""),
        )
        workspace = tmp_path / "deep" / "workspace"

        def view(argv):
            assert Path(fake_gh.kwargs[-1]["cwd"]).is_dir(), "gh ran before workspace existed"
            return 0, "main\n", ""

        fake_gh.on("gh", "auth", "status")
        fake_gh.on(*_SANDBOX_VIEW_ARGV, handler=view)

        init_sandbox(self.sandbox, workspace)

        assert workspace.is_dir()
        assert all(Path(kw["cwd"]).is_dir() for kw in fake_gh.kwargs)

    # --- push argv pinning (seam) ----------------------------------------------

    @staticmethod
    def _seam(calls: list[list[str]], *, ls_remote: str, head: str, tracked: str):
        def fake_git(argv, **kw):
            calls.append(argv)
            verb = argv[1]
            out = ""
            if verb == "ls-remote":
                out = ls_remote
            elif verb == "symbolic-ref":
                out = f"{head}\n"
            elif verb == "ls-tree":
                out = tracked
            elif verb == "cat-file":
                out = "blob\n"
            return subprocess.CompletedProcess(argv, 0, out, "")

        return fake_git

    def test_empty_path_push_argv_is_plain_fast_forward(self, fake_gh, tmp_path, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(
            "hermes_pipeline.harness._git", self._seam(calls, ls_remote="", head="main", tracked="")
        )
        fake_gh.on("gh", "auth", "status")
        fake_gh.on("gh", *_SANDBOX_PATCH_ARGV, stdout="{}\n")
        # First view must answer "" for the empty path: register a sequenced handler.
        answers = iter(["\n", "main\n"])
        fake_gh.on(*_SANDBOX_VIEW_ARGV, handler=lambda argv: (0, next(answers), ""))

        assert init_sandbox(self.sandbox, tmp_path / "workspace") == "seeded"

        pushes = [c for c in calls if c[1] == "push"]
        assert pushes == [["git", "push", "origin", "HEAD:refs/heads/main"]]
        assert calls[0][:4] == ["git", "ls-remote", "--heads", "--tags"]
        assert ["git", "add", "-A"] not in calls

    def test_non_empty_path_push_argv_targets_default_branch(self, fake_gh, tmp_path, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(
            "hermes_pipeline.harness._git",
            self._seam(calls, ls_remote="abc\trefs/heads/develop\n", head="develop", tracked=".github/x\n"),
        )
        self._serve_gh(fake_gh, None, default_branch="develop")

        assert init_sandbox(self.sandbox, tmp_path / "workspace") == "seeded"

        pushes = [c for c in calls if c[1] == "push"]
        assert pushes == [["git", "push", "origin", "HEAD:refs/heads/develop"]]
        clone = next(c for c in calls if c[1] == "clone")
        assert clone[:4] == ["git", "clone", "--branch", "develop"]
        adds = [c for c in calls if c[1] == "add"]
        assert adds and all(c[2:4] == ["-f", "--"] for c in adds)
        assert ["git", "add", "-A"] not in calls

    # --- non-empty repository path -----------------------------------------------

    @pytest.mark.real_git
    def test_already_seeded_is_noop(self, fake_gh, tmp_path):
        bare = _make_bare_remote(tmp_path, dict(harness_mod._SANDBOX_SEED_FILES), branch="trunk")
        before = _real_git("rev-parse", "trunk", cwd=bare)
        sandbox = dataclasses.replace(self.sandbox, url=f"file://{bare}")
        self._serve_gh(fake_gh, bare, default_branch="trunk")

        assert init_sandbox(sandbox, tmp_path / "workspace") == "already_seeded"

        assert _real_git("rev-parse", "trunk", cwd=bare) == before
        assert _SANDBOX_PATCH_ARGV not in fake_gh.gh_calls()

    @pytest.mark.real_git
    def test_refuses_repo_with_foreign_tracked_files(self, fake_gh, tmp_path):
        bare = _make_bare_remote(tmp_path, {"src/app.py": "print(1)\n", "README.md": "# app\n"})
        before = _real_git("rev-parse", "main", cwd=bare)
        sandbox = dataclasses.replace(self.sandbox, url=f"file://{bare}")
        self._serve_gh(fake_gh, bare, default_branch="main")

        with pytest.raises(HarnessPreflightError) as exc_info:
            init_sandbox(sandbox, tmp_path / "workspace")

        assert exc_info.value.code == "sandbox_not_empty"
        assert "src/app.py" in exc_info.value.detail
        assert "README.md" not in exc_info.value.detail
        assert _real_git("rev-parse", "main", cwd=bare) == before
        assert _SANDBOX_PATCH_ARGV not in fake_gh.gh_calls()
        assert not (tmp_path / "workspace" / "sandbox").exists()

    @pytest.mark.real_git
    def test_fully_seeded_repo_with_foreign_files_is_still_refused(self, fake_gh, tmp_path):
        files = {**harness_mod._SANDBOX_SEED_FILES, "src/app.py": "print(1)\n"}
        bare = _make_bare_remote(tmp_path, files)
        sandbox = dataclasses.replace(self.sandbox, url=f"file://{bare}")
        self._serve_gh(fake_gh, bare, default_branch="main")

        with pytest.raises(HarnessPreflightError) as exc_info:
            init_sandbox(sandbox, tmp_path / "workspace")

        assert exc_info.value.code == "sandbox_not_empty"

    @pytest.mark.real_git
    def test_foreign_detail_truncates_with_count(self, fake_gh, tmp_path):
        files = {f"src/m{i}.py": "x\n" for i in range(8)}
        bare = _make_bare_remote(tmp_path, files)
        sandbox = dataclasses.replace(self.sandbox, url=f"file://{bare}")
        self._serve_gh(fake_gh, bare, default_branch="main")

        with pytest.raises(HarnessPreflightError) as exc_info:
            init_sandbox(sandbox, tmp_path / "workspace")

        assert exc_info.value.detail.endswith(", +3 more")
        assert exc_info.value.detail.count("src/") == 5

    @pytest.mark.real_git
    def test_allows_github_dir_and_seeds_missing_files(self, fake_gh, tmp_path):
        bare = _make_bare_remote(
            tmp_path, {".github/workflows/ci.yml": "on: push\n", "README.md": "# custom readme\n"}
        )
        sandbox = dataclasses.replace(self.sandbox, url=f"file://{bare}")
        self._serve_gh(fake_gh, bare, default_branch="main")

        assert init_sandbox(sandbox, tmp_path / "workspace") == "seeded"

        tree = _remote_tree(bare, "main")
        assert set(tree) == set(harness_mod._SANDBOX_SEED_FILES) | {".github/workflows/ci.yml"}
        assert tree["README.md"] == "# custom readme"
        assert tree[".gitignore"] == harness_mod._SANDBOX_GITIGNORE.rstrip("\n")
        assert _real_git("log", "-1", "--format=%s", "main", cwd=bare) == _SEED_SUBJECT
        assert _SANDBOX_PATCH_ARGV not in fake_gh.gh_calls()

    @pytest.mark.real_git
    def test_partial_seed_pushes_to_non_main_default_branch(self, fake_gh, tmp_path):
        bare = _make_bare_remote(tmp_path, {"pyproject.toml": "[project]\n"}, branch="develop")
        sandbox = dataclasses.replace(self.sandbox, url=f"file://{bare}")
        self._serve_gh(fake_gh, bare, default_branch="develop")

        assert init_sandbox(sandbox, tmp_path / "workspace") == "seeded"

        tree = _remote_tree(bare, "develop")
        assert set(tree) == set(harness_mod._SANDBOX_SEED_FILES)
        assert tree["pyproject.toml"] == "[project]"
        assert _remote_branches(bare) == ["develop"]

    @pytest.mark.real_git
    def test_clones_gh_default_branch_not_remote_head(self, fake_gh, tmp_path):
        bare = _make_bare_remote(tmp_path, {"NOTES.md": "dev\n"}, branch="develop")
        _push_files(bare, tmp_path / "main-seed", {"README.md": "# main\n"}, branch="main")
        develop_before = _real_git("rev-parse", "develop", cwd=bare)
        assert _real_git("symbolic-ref", "HEAD", cwd=bare) == "refs/heads/develop"
        sandbox = dataclasses.replace(self.sandbox, url=f"file://{bare}")
        self._serve_gh(fake_gh, bare, default_branch="main")
        workspace = tmp_path / "workspace"

        assert init_sandbox(sandbox, workspace) == "seeded"

        assert _real_git("symbolic-ref", "--short", "HEAD", cwd=workspace / "sandbox") == "main"
        assert set(_remote_tree(bare, "main")) == set(harness_mod._SANDBOX_SEED_FILES)
        assert _real_git("rev-parse", "develop", cwd=bare) == develop_before

    @pytest.mark.real_git
    def test_tracked_gitignore_without_hermes_rule_is_replaced(self, fake_gh, tmp_path):
        bare = _make_bare_remote(tmp_path, {".gitignore": "*.log\n"})
        sandbox = dataclasses.replace(self.sandbox, url=f"file://{bare}")
        self._serve_gh(fake_gh, bare, default_branch="main")
        workspace = tmp_path / "workspace"

        assert init_sandbox(sandbox, workspace) == "seeded"

        assert ".hermes/" in _remote_tree(bare, "main")[".gitignore"].splitlines()
        _assert_porcelain_clean_with_runtime_junk(workspace / "sandbox")

    @pytest.mark.real_git
    def test_tracked_gitignore_hiding_docs_does_not_block_seed(self, fake_gh, tmp_path):
        # ``.hermes/`` is present so the .gitignore is kept as-is and ``docs/`` stays
        # ignored: only ``add -f`` can land docs/harness/SANDBOX.md.
        bare = _make_bare_remote(tmp_path, {".gitignore": ".hermes/\ndocs/\n"})
        sandbox = dataclasses.replace(self.sandbox, url=f"file://{bare}")
        self._serve_gh(fake_gh, bare, default_branch="main")

        assert init_sandbox(sandbox, tmp_path / "workspace") == "seeded"

        tree = _remote_tree(bare, "main")
        assert "docs/harness/SANDBOX.md" in tree
        assert tree[".gitignore"] == ".hermes/\ndocs/"

    @pytest.mark.real_git
    def test_non_empty_path_never_removes_preexisting_project_dir(self, fake_gh, tmp_path):
        bare = _make_bare_remote(tmp_path, dict(harness_mod._SANDBOX_SEED_FILES))
        sandbox = dataclasses.replace(self.sandbox, url=f"file://{bare}")
        self._serve_gh(fake_gh, bare, default_branch="main")
        project_dir = tmp_path / "workspace" / "sandbox"
        project_dir.mkdir(parents=True)
        (project_dir / "keep.txt").write_text("x")

        with pytest.raises(HarnessPreflightError) as exc_info:
            init_sandbox(sandbox, tmp_path / "workspace")

        assert exc_info.value.code == "workspace_exists"
        assert (project_dir / "keep.txt").is_file()

    def test_run_git_error_falls_back_to_stdout_when_stderr_blank(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "hermes_pipeline.harness._git",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "nothing to commit, working tree clean\n", ""),
        )

        with pytest.raises(HarnessPreflightError) as exc_info:
            harness_mod._run_git(["-c", "commit.gpgsign=false", "commit", "-m", "x"], cwd=tmp_path)

        assert exc_info.value.code == "git_error"
        assert exc_info.value.detail.startswith("git commit failed: ")
        assert "nothing to commit, working tree clean" in exc_info.value.detail

    @pytest.mark.real_git
    def test_non_fast_forward_push_fails_and_leaves_remote_unchanged(self, fake_gh, tmp_path, monkeypatch):
        bare = _make_bare_remote(tmp_path, {"README.md": "# x\n"})
        sandbox = dataclasses.replace(self.sandbox, url=f"file://{bare}")
        self._serve_gh(fake_gh, bare, default_branch="main")
        real_clone = harness_mod.clone_sandbox
        raced: dict[str, str] = {}

        def racing_clone(*args, **kwargs):
            real_clone(*args, **kwargs)
            raced["tip"] = _advance_remote(bare, tmp_path, "main")

        monkeypatch.setattr(harness_mod, "clone_sandbox", racing_clone)

        with pytest.raises(HarnessPreflightError) as exc_info:
            init_sandbox(sandbox, tmp_path / "workspace")

        assert exc_info.value.code == "git_error"
        assert exc_info.value.detail.startswith("git push failed")
        assert _real_git("rev-parse", "main", cwd=bare) == raced["tip"]
        assert "RACE.txt" in _remote_tree(bare, "main")
        assert not (tmp_path / "workspace" / "sandbox").exists()


_RUN_TOKEN = "abcd1234"
_HARNESS_TITLE = f"[harness {_RUN_TOKEN}] Implement mock name normalization"
_LIST_QUERY = "repos/acme/sandbox/issues?state=all&creator=octocat&per_page=100"
_LIST_ARGV = (*API_ARGV, "--paginate", "--slurp", _LIST_QUERY)
_LABEL_LIST_ARGV = ("gh", "label", "list", "--repo", "acme/sandbox")
_LABEL_CREATE_ARGV = ("gh", "label", "create", "--repo", "acme/sandbox")
_ISSUE_URL = "https://github.com/acme/sandbox/issues/{}\n"


def _baseline() -> RunBaseline:
    return RunBaseline(
        head_pairs=(("main", "0" * 40),),
        started_at=datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC),
        viewer="octocat",
        default_branch="main",
    )


def _seeded_clone(tmp_path: Path) -> tuple[Path, SandboxRepo]:
    bare = _make_bare_remote(tmp_path, harness_mod._SANDBOX_SEED_FILES)
    sandbox = SandboxRepo(repo="acme/sandbox", slug="sandbox", url=f"file://{bare}")
    project_dir = tmp_path / "workspace" / "sandbox"
    clone_sandbox(sandbox, project_dir)
    return project_dir, sandbox


def _harness_issue(number: int = 42, token: str = "tok00000") -> HarnessIssue:
    return HarnessIssue(
        number=number,
        todo_id=f"TODO-{number}",
        branch=f"feat/harness-{token}",
        plan_path=f"docs/harness/{token}-plan.md",
        title=f"[harness {token}] Implement mock name normalization",
        run_token=token,
    )


class TestHappyPathFixture:
    def test_rendered_issue_parses_back_eligible(self, tmp_project):
        branch = f"feat/harness-{_RUN_TOKEN}"
        plan_path = f"docs/harness/{_RUN_TOKEN}-plan.md"
        body = render_issue_body(
            harness_mod._issue_fields(branch=branch, plan_path=plan_path), include_empty=False
        )
        issue = issue_from_api(
            issue_payload(7, title=_HARNESS_TITLE, body=body, labels=harness_mod._harness_labels()),
            repo="acme/sandbox",
        )

        result = compile_eligible_issues(tmp_project, [issue], in_flight=(), requires_plan=False)

        assert result.blocked_reasons == {}
        assert result.todo_ids == frozenset({"TODO-7"})
        (candidate,) = result.candidates
        assert candidate.entry.branch_values == (branch,)
        assert candidate.entry.plan_values == (plan_path,)

    @pytest.mark.parametrize("todo_id", ["TODO-x", "todo-1", "TODO-1\n", "", "TODO-", "TODO-\u0661"])  # U+0661: ARABIC-INDIC DIGIT ONE
    def test_plan_document_rejects_bad_todo_id(self, todo_id):
        with pytest.raises(ValueError):
            harness_mod._plan_document(todo_id)

    def test_run_token_shape(self):
        token = harness_mod._run_token()

        assert len(token) == 8
        assert token == token.lower()
        assert all(char in "0123456789abcdefghijklmnopqrstuvwxyz" for char in token)

    def test_seed_pyproject_collects_tests(self, tmp_path):
        clone = tmp_path / "sandbox"
        for rel, content in harness_mod._SANDBOX_SEED_FILES.items():
            target = clone / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        (clone / "mock_transform.py").write_text(
            "def normalize_names(names: list[str]) -> list[str]:\n"
            "    return [n.strip().lower() for n in names if n.strip()]\n"
        )
        (clone / "tests" / "test_mock_transform.py").write_text(
            "from mock_transform import normalize_names\n\n\n"
            "def test_acceptance():\n"
            '    assert normalize_names([" Alice ", "", "BOB"]) == ["alice", "bob"]\n'
            "    assert normalize_names([]) == []\n"
        )
        pyproject = tomllib.loads((clone / "pyproject.toml").read_text())
        assert any(dep.startswith("pytest") for dep in pyproject["dependency-groups"]["dev"])
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("PYTEST_ADDOPTS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTHONPATH")
        }
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        # No path argument: the seeded ``testpaths`` must drive collection.
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=clone,
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout

    def test_harness_labels_are_in_vocabulary(self):
        labels = harness_mod._harness_labels()

        names = {name for name, _, _ in LABEL_VOCABULARY}
        assert set(labels) <= names
        assert labels[:2] == ["tpo:todo", "ready-for-agent"]
        assert set(labels[2:]) == {
            "priority:P1",
            "effort:S",
            "phase:4-development",
            "test-coverage:required",
            "security-review:not-required",
            "ui-review:not-required",
        }
        assert len(labels) == len(set(labels))


class TestCreateHarnessIssue:
    sandbox = SandboxRepo(repo="acme/sandbox", slug="sandbox", url="file:///nonexistent-sandbox")

    @staticmethod
    def _listing(*pages: list[dict] | Exception):
        """Serve one response per call from *pages* (last one repeats); record the count.

        An ``Exception`` entry is served as ``rc=1`` with an HTTP 502 stderr.
        """
        state = {"calls": 0}

        def handler(argv):
            page = pages[min(state["calls"], len(pages) - 1)]
            state["calls"] += 1
            if isinstance(page, Exception):
                return 1, "", "HTTP 502: Bad Gateway\n"
            return 0, json.dumps([page]), ""

        return handler, state

    def _serve_labels(self, fake_gh, existing: list[str] | None = None):
        """``gh label list`` reports *existing* (default: the whole vocabulary); create succeeds."""
        names = existing if existing is not None else [name for name, _, _ in LABEL_VOCABULARY]
        fake_gh.on(*_LABEL_LIST_ARGV, stdout=json.dumps([{"name": name} for name in names]))
        fake_gh.on(*_LABEL_CREATE_ARGV, stdout="")

    def _serve_view(self, fake_gh, number: int, title: str = _HARNESS_TITLE, **extra):
        fake_gh.on(
            *API_ARGV,
            f"repos/acme/sandbox/issues/{number}",
            stdout=json.dumps(issue_payload(number, title=title, **extra)),
        )

    def _create(self, fake_gh, tmp_project, **kwargs):
        kwargs.setdefault("sleep", lambda _: None)
        return create_harness_issue(
            tmp_project, self.sandbox, run_token=_RUN_TOKEN, baseline=_baseline(), **kwargs
        )

    def test_creates_issue_with_renderer_labels_and_token_title(self, fake_gh, tmp_project):
        self._serve_labels(fake_gh)
        self._serve_view(fake_gh, 7)
        captured: dict[str, str] = {}

        def create(argv):
            captured["body"] = Path(argv[argv.index("--body-file") + 1]).read_text()
            return 0, _ISSUE_URL.format(7), ""

        fake_gh.on("gh", "issue", "create", handler=create)

        issue = self._create(fake_gh, tmp_project)

        assert issue == HarnessIssue(
            number=7,
            todo_id="TODO-7",
            branch=f"feat/harness-{_RUN_TOKEN}",
            plan_path=f"docs/harness/{_RUN_TOKEN}-plan.md",
            title=_HARNESS_TITLE,
            run_token=_RUN_TOKEN,
        )
        (argv,) = [call for call in fake_gh.gh_calls() if call[:2] == ["issue", "create"]]
        assert argv[argv.index("--repo") + 1] == "acme/sandbox"
        assert argv[argv.index("--title") + 1] == _HARNESS_TITLE
        given = [argv[i + 1] for i, item in enumerate(argv) if item == "--label"]
        assert given == harness_mod._harness_labels()
        sections = parse_issue_body(captured["body"])
        assert sections["Branch"] == (f"feat/harness-{_RUN_TOKEN}",)
        assert sections["Plan"] == (f"docs/harness/{_RUN_TOKEN}-plan.md",)
        assert "_No response_" not in captured["body"]
        api_calls = [call for call in fake_gh.gh_calls() if call[:1] == ["api"]]
        assert [call[-1] for call in api_calls] == ["repos/acme/sandbox/issues/7"]

    def test_ensures_labels_before_creating(self, fake_gh, tmp_project):
        self._serve_labels(fake_gh, existing=["tpo:todo"])
        self._serve_view(fake_gh, 7)
        fake_gh.on("gh", "issue", "create", stdout=_ISSUE_URL.format(7))

        self._create(fake_gh, tmp_project)

        verbs = [tuple(call[:2]) for call in fake_gh.gh_calls()]
        assert verbs.index(("label", "list")) < verbs.index(("issue", "create"))
        created = [call[-1] for call in fake_gh.gh_calls() if call[:2] == ["label", "create"]]
        assert "ready-for-agent" in created
        assert "phase:4-development" in created
        assert max(i for i, v in enumerate(verbs) if v == ("label", "create")) < verbs.index(("issue", "create"))

    def test_rejects_malformed_run_token(self, fake_gh, tmp_project):
        for token in ("ABCD1234", "abcd123", "abcd12345", "abcd/234", "abcd 234", "abcd-234", "abcd1234\n", ""):
            with pytest.raises(ValueError):
                create_harness_issue(tmp_project, self.sandbox, run_token=token, baseline=_baseline())
        assert fake_gh.gh_calls() == []

    def test_adopted_number_with_foreign_title_is_reconciled(self, fake_gh, tmp_project):
        self._serve_labels(fake_gh)
        fake_gh.on("gh", "issue", "create", stdout=_ISSUE_URL.format(12))
        self._serve_view(fake_gh, 12, title="Somebody else's issue")
        handler, state = self._listing([issue_payload(123, title=_HARNESS_TITLE)])
        fake_gh.on(*_LIST_ARGV, handler=handler)

        issue = self._create(fake_gh, tmp_project)

        assert issue.number == 123
        assert issue.todo_id == "TODO-123"
        assert state["calls"] == 1

    def test_adopted_number_that_is_a_pull_request_is_reconciled(self, fake_gh, tmp_project):
        self._serve_labels(fake_gh)
        fake_gh.on("gh", "issue", "create", stdout=_ISSUE_URL.format(12))
        self._serve_view(fake_gh, 12, pull_request=True)
        handler, _ = self._listing([issue_payload(123, title=_HARNESS_TITLE)])
        fake_gh.on(*_LIST_ARGV, handler=handler)

        assert self._create(fake_gh, tmp_project).number == 123

    def test_timeout_after_remote_success_is_reconciled(self, fake_gh, tmp_project, caplog):
        self._serve_labels(fake_gh)
        fake_gh.on(
            "gh", "issue", "create", raises=subprocess.TimeoutExpired(cmd="gh issue create", timeout=60)
        )
        handler, state = self._listing([], [], [issue_payload(7, title=_HARNESS_TITLE)])
        fake_gh.on(*_LIST_ARGV, handler=handler)
        sleeps: list[float] = []

        with caplog.at_level(logging.WARNING, logger="hermes_pipeline.harness"):
            issue = self._create(fake_gh, tmp_project, sleep=sleeps.append)

        assert issue.number == 7
        assert issue.todo_id == "TODO-7"
        assert state["calls"] == 3
        assert sleeps == [2.0, 2.0]
        assert "reconciled issue #7 after create failure" in caplog.text

    def test_malformed_create_stdout_reconciles(self, fake_gh, tmp_project):
        self._serve_labels(fake_gh)
        fake_gh.on("gh", "issue", "create", stdout="Creating issue... done\n")
        handler, state = self._listing([issue_payload(7, title=_HARNESS_TITLE)])
        fake_gh.on(*_LIST_ARGV, handler=handler)
        sleeps: list[float] = []

        issue = self._create(fake_gh, tmp_project, sleep=sleeps.append)

        assert issue.number == 7
        assert state["calls"] == 1
        assert sleeps == []

    def test_oserror_from_create_is_reconciled(self, fake_gh, tmp_project):
        self._serve_labels(fake_gh)
        fake_gh.on("gh", "issue", "create", raises=PermissionError("body file"))
        handler, state = self._listing([issue_payload(7, title=_HARNESS_TITLE)])
        fake_gh.on(*_LIST_ARGV, handler=handler)

        issue = self._create(fake_gh, tmp_project)

        assert issue.number == 7
        assert state["calls"] == 1

    @pytest.mark.parametrize(
        ("code", "failure"),
        [
            ("gh_auth", {"rc": 1, "stderr": "error: not logged in\n"}),
            ("gh_missing", {"raises": FileNotFoundError("gh")}),
            ("gh_version", {"rc": 1, "stderr": "unknown flag: --body-file\n"}),
            ("gh_not_found", {"rc": 1, "stderr": "HTTP 404: Not Found\n"}),
            ("gh_rejected", {"rc": 1, "stderr": "HTTP 422: Validation Failed\n"}),
        ],
    )
    def test_create_failure_without_side_effect_is_raised_without_listing(
        self, fake_gh, tmp_project, code, failure
    ):
        self._serve_labels(fake_gh)
        fake_gh.on("gh", "issue", "create", **failure)
        handler, state = self._listing([issue_payload(7, title=_HARNESS_TITLE)])
        fake_gh.on(*_LIST_ARGV, handler=handler)
        sleeps: list[float] = []

        with pytest.raises(GitHubIssuesError) as exc_info:
            self._create(fake_gh, tmp_project, sleep=sleeps.append)

        assert exc_info.value.code == code
        assert state["calls"] == 0
        assert sleeps == []

    @pytest.mark.parametrize(
        "view_failure",
        [
            {"rc": 1, "stderr": "HTTP 404: Not Found\n"},
            {"rc": 1, "stderr": "HTTP 403: Forbidden\n"},
            {"raises": subprocess.TimeoutExpired(cmd="gh api", timeout=60)},
        ],
    )
    def test_verification_failure_falls_through_to_reconciliation(self, fake_gh, tmp_project, view_failure):
        self._serve_labels(fake_gh)
        fake_gh.on("gh", "issue", "create", stdout=_ISSUE_URL.format(12))
        fake_gh.on(*API_ARGV, "repos/acme/sandbox/issues/12", **view_failure)
        handler, state = self._listing([issue_payload(12, title=_HARNESS_TITLE)])
        fake_gh.on(*_LIST_ARGV, handler=handler)

        issue = self._create(fake_gh, tmp_project)

        assert issue.number == 12
        assert state["calls"] == 1

    def test_zero_matches_after_retries_raises_issue_unverified(self, fake_gh, tmp_project):
        self._serve_labels(fake_gh)
        fake_gh.on("gh", "issue", "create", rc=1, stderr="HTTP 502\n")
        handler, state = self._listing([issue_payload(8, title="unrelated")])
        fake_gh.on(*_LIST_ARGV, handler=handler)
        sleeps: list[float] = []

        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            self._create(fake_gh, tmp_project, sleep=sleeps.append)

        assert exc_info.value.code == "issue_unverified"
        assert state["calls"] == 5
        assert sleeps == [2.0] * 4
        assert isinstance(exc_info.value.__cause__, GitHubIssuesError)
        assert (
            f"gh issue list --repo acme/sandbox --state all --search '[harness {_RUN_TOKEN}] in:title'"
            in exc_info.value.detail
        )
        assert str(exc_info.value).startswith("issue_unverified: ")

    def test_two_matches_raises_issue_ambiguous(self, fake_gh, tmp_project):
        self._serve_labels(fake_gh)
        fake_gh.on("gh", "issue", "create", rc=1, stderr="HTTP 502\n")
        handler, _ = self._listing(
            [issue_payload(10, title=_HARNESS_TITLE), issue_payload(11, title=_HARNESS_TITLE + " (again)")]
        )
        fake_gh.on(*_LIST_ARGV, handler=handler)

        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            self._create(fake_gh, tmp_project)

        assert exc_info.value.code == "issue_ambiguous"
        assert exc_info.value.detail == (
            f"#10, #11 in acme/sandbox for [harness {_RUN_TOKEN}]; "
            "close duplicates: gh issue close <n> --repo acme/sandbox"
        )

    def _reconcile(self, tmp_project, **kwargs):
        kwargs.setdefault("sleep", lambda _: None)
        return reconcile_created_issue(
            tmp_project,
            self.sandbox,
            run_token=_RUN_TOKEN,
            baseline=_baseline(),
            cause=RuntimeError("boom"),
            **kwargs,
        )

    def test_listing_skips_pull_requests(self, fake_gh, tmp_project):
        handler, _ = self._listing(
            [
                issue_payload(6, title=_HARNESS_TITLE, pull_request=True),
                issue_payload(7, title=_HARNESS_TITLE),
            ]
        )
        fake_gh.on(*_LIST_ARGV, handler=handler)

        assert self._reconcile(tmp_project) == 7

    def test_listing_requires_title_prefix(self, fake_gh, tmp_project):
        handler, _ = self._listing(
            [
                issue_payload(6, title=f"Re: {_HARNESS_TITLE}"),
                issue_payload(7, title=_HARNESS_TITLE),
            ]
        )
        fake_gh.on(*_LIST_ARGV, handler=handler)

        assert self._reconcile(tmp_project) == 7

    def test_listing_quotes_viewer_and_uses_list_timeout(self, fake_gh, tmp_project):
        baseline = dataclasses.replace(_baseline(), viewer="octo cat/x")
        fake_gh.on(*API_ARGV, "--paginate", "--slurp", stdout=json.dumps([[issue_payload(7, title=_HARNESS_TITLE)]]))

        number = reconcile_created_issue(
            tmp_project, self.sandbox, run_token=_RUN_TOKEN, baseline=baseline, cause=RuntimeError("boom")
        )

        assert number == 7
        (argv,) = fake_gh.gh_calls()
        assert argv[-1] == "repos/acme/sandbox/issues?state=all&creator=octo%20cat%2Fx&per_page=100"
        assert fake_gh.kwargs[0]["timeout"] == 180.0

    def test_listing_failures_are_retried_then_adopted(self, fake_gh, tmp_project, caplog):
        handler, state = self._listing(
            RuntimeError(), RuntimeError(), [issue_payload(7, title=_HARNESS_TITLE)]
        )
        fake_gh.on(*_LIST_ARGV, handler=handler)
        sleeps: list[float] = []

        with caplog.at_level(logging.WARNING, logger="hermes_pipeline.harness"):
            number = self._reconcile(tmp_project, sleep=sleeps.append)

        assert number == 7
        assert state["calls"] == 3
        assert sleeps == [2.0, 2.0]
        assert "reconcile listing attempt 1 failed" in caplog.text
        assert "reconcile listing attempt 2 failed" in caplog.text

    def test_listing_failing_every_attempt_raises_issue_unverified(self, fake_gh, tmp_project):
        handler, state = self._listing(RuntimeError())
        fake_gh.on(*_LIST_ARGV, handler=handler)
        sleeps: list[float] = []

        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            self._reconcile(tmp_project, sleep=sleeps.append)

        assert exc_info.value.code == "issue_unverified"
        assert state["calls"] == 5
        assert sleeps == [2.0] * 4


class TestHarnessIssue:
    @pytest.mark.parametrize(
        "plan_path",
        [
            "/docs/harness/tok00000-plan.md",
            "docs/harness/../tok00000-plan.md",
            "docs/harness/TOK00000-plan.md",
            "docs/harness/tok0000-plan.md",
            "docs/harness/tok00000-plan.md\n",
            "plans/tok00000-plan.md",
        ],
    )
    def test_rejects_bad_plan_path(self, plan_path):
        with pytest.raises(ValueError):
            dataclasses.replace(_harness_issue(), plan_path=plan_path)

    def test_accepts_canonical_plan_path(self):
        assert _harness_issue().plan_path == "docs/harness/tok00000-plan.md"


class TestCommitPlan:
    @pytest.mark.real_git
    def test_commit_plan_document_validates_for_issue_number(self, tmp_path):
        project_dir, _ = _seeded_clone(tmp_path)
        issue = _harness_issue(42)

        sha = commit_plan(project_dir, issue)

        assert sha == _real_git("rev-parse", "HEAD", cwd=project_dir)
        assert _real_git("status", "--porcelain", cwd=project_dir) == ""
        assert _real_git("log", "-1", "--format=%s", cwd=project_dir) == "docs(harness): plan for TODO-42"
        manifest = validate_plan_candidate(project_dir, issue.plan_path, expected_todo_id="TODO-42")
        assert manifest is not None
        assert manifest.todo_id == "TODO-42"
        document = (project_dir / issue.plan_path).read_text()
        assert document.startswith("# TODO-42 Mock Name Normalization Plan\n")
        assert '"todo_id": "TODO-42"' in document
        assert '"todo_id": "TODO-1"' not in document

    @pytest.mark.real_git
    def test_commit_plan_does_not_push_and_pins_no_verify(self, tmp_path, monkeypatch):
        project_dir, _ = _seeded_clone(tmp_path)
        recorded: list[list[str]] = []

        def recording_git(argv, **kwargs):
            recorded.append(list(argv))
            return subprocess.run(argv, **kwargs)

        monkeypatch.setattr(harness_mod, "_git", recording_git)

        commit_plan(project_dir, _harness_issue(3))

        verbs = [harness_mod._git_verb(argv[1:]) for argv in recorded]
        assert "commit" in verbs
        assert "push" not in verbs
        (commit_argv,) = [argv for argv in recorded if harness_mod._git_verb(argv[1:]) == "commit"]
        assert "--no-verify" in commit_argv
        assert commit_argv[1:3] == ["-c", "commit.gpgsign=false"]
        assert commit_argv[-2:] == ["--", "docs/harness/tok00000-plan.md"]
        assert _real_git("rev-parse", "origin/main", cwd=project_dir) != _real_git("rev-parse", "HEAD", cwd=project_dir)

    @pytest.mark.real_git
    def test_commit_plan_commits_only_the_plan(self, tmp_path):
        project_dir, _ = _seeded_clone(tmp_path)
        (project_dir / "SECRET.txt").write_text("hunter2\n")
        _real_git("add", "SECRET.txt", cwd=project_dir)

        commit_plan(project_dir, _harness_issue(3))

        committed = _real_git("show", "--name-only", "--format=", "HEAD", cwd=project_dir).splitlines()
        assert committed == ["docs/harness/tok00000-plan.md"]
        assert _real_git("status", "--porcelain", cwd=project_dir) == "A  SECRET.txt"

    @pytest.mark.real_git
    def test_commit_plan_is_idempotent(self, tmp_path):
        project_dir, _ = _seeded_clone(tmp_path)
        issue = _harness_issue(3)

        first = commit_plan(project_dir, issue)
        second = commit_plan(project_dir, issue)

        assert first == second == _real_git("rev-parse", "HEAD", cwd=project_dir)
        assert _real_git("rev-list", "--count", "HEAD", cwd=project_dir) == "2"

    @pytest.mark.real_git
    def test_commit_plan_recommits_when_tracked_plan_differs(self, tmp_path):
        project_dir, _ = _seeded_clone(tmp_path)
        issue = _harness_issue(3)
        first = commit_plan(project_dir, issue)
        (project_dir / issue.plan_path).write_text("# stale\n")
        _real_git("commit", "-am", "tamper", cwd=project_dir)

        second = commit_plan(project_dir, issue)

        assert second != first
        assert (project_dir / issue.plan_path).read_text() == harness_mod._plan_document("TODO-3")


class TestPollRegisteredPhases:
    """Tests for poll_registered_phases(): polling without registration."""

    @staticmethod
    def _monitor(tmp_path):
        from hermes_pipeline.harness import (
            ConvergenceDetector,
            HarnessMonitor,
            _ConvergenceMonitor,
        )

        detector = ConvergenceDetector(threshold=99)
        monitor = _ConvergenceMonitor(HarnessMonitor(tmp_path / "events.jsonl"), detector, {})
        return monitor, detector

    def test_poll_does_not_register(self, tmp_path, mocker):
        from hermes_pipeline.harness import poll_registered_phases

        mocker.patch(
            "hermes_pipeline.kanban_tasks.register_todo_phases",
            side_effect=lambda **_kw: pytest.fail("poll_registered_phases must not register"),
        )
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("time.sleep")
        observe = mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        status = mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            side_effect=[
                {"p1": "ready", "p2": "ready"},
                {"p1": "running", "p2": "ready"},
                {"p1": "done", "p2": "running"},
                {"p1": "done", "p2": "done"},
                {"p1": "done", "p2": "done"},
            ],
        )
        monitor, detector = self._monitor(tmp_path)
        cards = [Phase(phase_key="p1", name="P1"), Phase(phase_key="p2", name="P2")]

        result = poll_registered_phases(
            project_slug="demo", tick_id="01TICK", state_dir=tmp_path / ".hermes",
            todo_id="TODO-1", project_dir=tmp_path, cards=cards,
            monitor=monitor, detector=detector, poll_interval=0.0, max_poll_interval=0.0,
        )

        assert result is True
        assert status.call_count == 5
        observe.assert_called_once_with(
            state_dir=tmp_path / ".hermes", tick_id="01TICK",
            status_map={"p1": "done", "p2": "done"},
        )
        events = [json.loads(l)["event_type"] for l in (tmp_path / "events.jsonl").read_text().splitlines() if l.strip()]
        assert events.count("phase_started") == 2
        assert events.count("phase_completed") == 2

    def test_poll_uses_supplied_cards_for_gate_terminality(self, tmp_path, mocker):
        from hermes_pipeline.harness import poll_registered_phases
        from hermes_pipeline.kanban_tasks import KanbanTaskInfo

        mocker.patch(
            "hermes_pipeline.kanban_tasks.register_todo_phases",
            side_effect=lambda **_kw: pytest.fail("poll_registered_phases must not register"),
        )
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        board = {"a": "ready", "b": "blocked"}
        snapshots = iter([{"a": "running", "b": "blocked"}, {"a": "done", "b": "blocked"}])

        def status(*_a, **_k):
            try:
                board.update(next(snapshots))
            except StopIteration:
                pass
            return dict(board)

        class _TrippingCancel:
            """Cancels after a bounded number of poll waits so a regression that
            never auto-completes gate B fails (returns False) instead of hanging.
            A raising status stub would not do: the poll loop swallows it."""

            def __init__(self, budget):
                self.budget = budget

            def wait(self, _timeout):
                self.budget -= 1
                return self.budget < 0

        def complete(_tenant, task_id):
            board["b"] = "done"
            return True

        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=status)
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
            side_effect=lambda *_a: {
                key: KanbanTaskInfo(task_id=f"t_{key}", phase_key=key, status=value, todo_id="TODO-1")
                for key, value in board.items()
            },
        )
        completed = mocker.patch(
            "hermes_pipeline.kanban_tasks.complete_todo_kanban_task", side_effect=complete
        )
        monitor, detector = self._monitor(tmp_path)
        cards = [Phase(phase_key="a", name="A"), Phase(phase_key="b", name="B", gate=True)]

        result = poll_registered_phases(
            project_slug="demo", tick_id="01TICK", state_dir=tmp_path / ".hermes",
            todo_id="TODO-1", project_dir=tmp_path, cards=cards,
            monitor=monitor, detector=detector, poll_interval=0.0, max_poll_interval=0.0,
            cancel_event=_TrippingCancel(budget=5),
        )

        assert result is True
        completed.assert_called_once_with("demo", "t_b")

    def test_poll_rejects_empty_cards(self, tmp_path, mocker):
        from hermes_pipeline.harness import poll_registered_phases

        status = mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status")
        monitor, detector = self._monitor(tmp_path)

        with pytest.raises(ValueError, match="cards"):
            poll_registered_phases(
                project_slug="demo", tick_id="01TICK", state_dir=tmp_path / ".hermes",
                todo_id="TODO-1", project_dir=tmp_path, cards=[],
                monitor=monitor, detector=detector,
            )
        status.assert_not_called()

    def test_legacy_wrapper_delegates(self, tmp_path, mocker):
        import threading

        from hermes_pipeline.harness import _poll_kanban_phases
        from hermes_pipeline.kanban_tasks import PreparedPhaseTask

        prepared = [
            PreparedPhaseTask(phase_key="a", name="A", body="", turns=1, gate=False),
            PreparedPhaseTask(phase_key="b", name="B", body="", turns=0, gate=True),
        ]

        def register(**kwargs):
            kwargs["transform_prepared"](list(prepared))
            return ["t_a", "t_b"]

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", side_effect=register)
        poll = mocker.patch("hermes_pipeline.harness.poll_registered_phases", return_value=True)
        monitor, detector = self._monitor(tmp_path)
        registration_event = threading.Event()

        assert _poll_kanban_phases(
            project_slug="demo", tick_id="01TICK", state_dir=tmp_path / ".hermes",
            todo_id="TODO-1", project_dir=tmp_path, phases_path=None,
            monitor=monitor, detector=detector, poll_interval=0.25, max_poll_interval=7.0,
            registration_event=registration_event,
        ) is True

        assert registration_event.is_set()
        poll.assert_called_once()
        kwargs = poll.call_args.kwargs
        assert kwargs["cards"] == [
            Phase(phase_key="a", name="A", gate=False, kind="worker"),
            Phase(phase_key="b", name="B", gate=True, kind="controller_gate"),
        ]
        assert kwargs["project_slug"] == "demo"
        assert kwargs["tick_id"] == "01TICK"
        assert kwargs["state_dir"] == tmp_path / ".hermes"
        assert kwargs["todo_id"] == "TODO-1"
        assert kwargs["project_dir"] == tmp_path
        assert kwargs["monitor"] is monitor
        assert kwargs["detector"] is detector
        assert kwargs["poll_interval"] == 0.25
        assert kwargs["max_poll_interval"] == 7.0


# ---------------------------------------------------------------------------
# Remote artifact discovery (Task 8): which PRs/branches belong to a live run.
# ---------------------------------------------------------------------------

_PR_VIEW_FIELDS = "number,state,mergedAt,headRefName,baseRefName,author,createdAt,isCrossRepository,title,body"
_PULLS_ARGV = (
    *API_ARGV, "--paginate", "--slurp",
    "repos/acme/sandbox/pulls?state=all&head=acme:feat%2Fx&per_page=100",
)
_ISSUE_BRANCH_PULLS_ARGV = (
    *API_ARGV, "--paginate", "--slurp",
    "repos/acme/sandbox/pulls?state=all&head=acme:feat%2Fharness-abcd1234&per_page=100",
)
_RECORDED_PULLS_ARGV = (
    *API_ARGV, "--paginate", "--slurp",
    "repos/acme/sandbox/pulls?state=all&head=acme:recorded%2Fx&per_page=100",
)
_SEARCH_ARGV = (
    *API_ARGV, "--paginate", "--slurp",
    "search/issues?q=repo%3Aacme%2Fsandbox%20is%3Apr%20%22%5Bharness%20abcd1234%5D%22&per_page=100",
)
_SANDBOX = SandboxRepo(repo="acme/sandbox", slug="sandbox", url="https://github.com/acme/sandbox.git")
_T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def _pr_view_argv(number: int) -> tuple[str, ...]:
    return ("gh", "pr", "view", str(number), "--repo", "acme/sandbox", "--json", _PR_VIEW_FIELDS)


def _pr_view_payload(
    number: int = 5,
    *,
    head: str = "feat/x",
    author: str = "octocat",
    created_at: str = "2026-09-01T12:00:05Z",
    cross: bool = False,
    title: str = "Implement widget",
    body: str = "",
    merged_at: str | None = None,
    state: str = "OPEN",
) -> dict:
    return {
        "number": number,
        "state": state,
        "mergedAt": merged_at,
        "headRefName": head,
        "baseRefName": "main",
        "author": {"login": author},
        "createdAt": created_at,
        "isCrossRepository": cross,
        "title": title,
        "body": body,
    }


def _search_page(*numbers: int, incomplete: bool = False) -> str:
    page = {"total_count": len(numbers), "incomplete_results": incomplete, "items": [{"number": n} for n in numbers]}
    return json.dumps([page])


def _pr(
    number: int = 5,
    *,
    head_ref: str = "feat/x",
    author: str = "octocat",
    created_at: datetime = datetime(2026, 9, 1, 12, 0, 5, tzinfo=UTC),
    cross_repository: bool = False,
    title: str = "Implement widget",
    body: str = "",
) -> PullRequest:
    return PullRequest(
        number=number,
        state="OPEN",
        merged=False,
        head_ref=head_ref,
        base_ref="main",
        author=author,
        created_at=created_at,
        cross_repository=cross_repository,
        title=title,
        body=body,
    )


# Default branch deliberately absent from head_pairs so its clause is load-bearing.
_PREDICATE_BASELINE = RunBaseline(
    head_pairs=(("feat/old", "1" * 40),),
    started_at=_T0,
    viewer="octocat",
    default_branch="trunk",
)


class TestReadRecordedBranch:
    def _write(self, tmp_path: Path, text: str) -> Path:
        (tmp_path / ".hermes").mkdir(exist_ok=True)
        (tmp_path / ".hermes" / "pipeline_branch.txt").write_text(text)
        return tmp_path

    def test_valid_single_line_is_stripped(self, tmp_path: Path):
        assert read_recorded_branch(self._write(tmp_path, "feat/x\n")) == "feat/x"

    def test_missing_file_is_none(self, tmp_path: Path):
        assert read_recorded_branch(tmp_path) is None

    def test_blank_file_is_none(self, tmp_path: Path):
        assert read_recorded_branch(self._write(tmp_path, "  \n")) is None

    def test_multi_line_is_none(self, tmp_path: Path, caplog):
        with caplog.at_level(logging.WARNING, logger="hermes_pipeline.harness"):
            assert read_recorded_branch(self._write(tmp_path, "feat/x\nfeat/y\n")) is None
        assert "pipeline_branch.txt" in caplog.text

    def test_invalid_ref_is_rejected_via_check_ref_format(self, tmp_path: Path, caplog):
        with caplog.at_level(logging.WARNING, logger="hermes_pipeline.harness"):
            assert read_recorded_branch(self._write(tmp_path, "main..x\n")) is None
        assert "main..x" in caplog.text


class TestPrFromPayload:
    def test_maps_gh_pr_view_shape(self):
        pr = harness_mod._pr_from_payload(
            _pr_view_payload(5, merged_at="2026-09-01T13:00:00Z", state="MERGED", body="b")
        )
        assert pr == PullRequest(
            number=5, state="MERGED", merged=True, head_ref="feat/x", base_ref="main",
            author="octocat", created_at=datetime(2026, 9, 1, 12, 0, 5, tzinfo=UTC),
            cross_repository=False, title="Implement widget", body="b",
        )

    def test_null_merged_at_and_null_body(self):
        pr = harness_mod._pr_from_payload(_pr_view_payload(5) | {"body": None})
        assert pr.merged is False
        assert pr.body == ""

    @pytest.mark.parametrize(
        "override",
        [
            {"number": True},
            {"createdAt": "2026-09-01T12:00:05"},
            {"createdAt": "yesterday"},
            {"isCrossRepository": "false"},
            {"author": {"login": ""}},
            {"author": None},
            {"headRefName": ""},
            {"state": "open"},
            {"state": "DRAFT"},
        ],
    )
    def test_malformed_fields_raise(self, override: dict):
        with pytest.raises((ValueError, TypeError, KeyError)):
            harness_mod._pr_from_payload(_pr_view_payload(5) | override)


class TestAttributablePr:
    issue = HarnessIssue(
        number=7, todo_id="TODO-7", branch="feat/harness-abcd1234",
        plan_path="docs/harness/abcd1234-plan.md",
        title="[harness abcd1234] Implement mock name normalization", run_token="abcd1234",
    )

    def _ok(self, pr: PullRequest, provenance: bool = True) -> bool:
        return is_attributable_pr(
            pr, baseline=_PREDICATE_BASELINE, issue=self.issue, provenance_of_head=provenance
        )

    def test_happy_with_run_provenance(self):
        assert self._ok(_pr(head_ref="feat/x")) is True

    def test_without_provenance_rejected_even_with_token_and_todo_id(self):
        pr = _pr(title="TODO-7 [harness abcd1234]", body="[harness abcd1234] TODO-7")
        assert self._ok(pr, provenance=False) is False

    def test_fork_head_rejected(self):
        assert self._ok(_pr(cross_repository=True)) is False

    def test_other_author_rejected(self):
        assert self._ok(_pr(author="mallory")) is False

    def test_created_before_baseline_rejected(self):
        assert self._ok(_pr(created_at=_T0 - timedelta(seconds=1))) is False

    def test_created_same_second_as_baseline_accepted(self):
        assert self._ok(_pr(created_at=_T0)) is True

    def test_head_in_baseline_rejected(self):
        assert self._ok(_pr(head_ref="feat/old")) is False

    @pytest.mark.parametrize("head", ["trunk", "Trunk", "TRUNK"])
    def test_head_equal_default_branch_rejected_casefold(self, head: str):
        assert self._ok(_pr(head_ref=head)) is False


class TestDeletableBranch:
    def _ok(self, tmp_path: Path, name: str, *, provenance: bool = True) -> bool:
        return is_deletable_branch(
            name, baseline=_PREDICATE_BASELINE, project_dir=tmp_path, provenance=provenance
        )

    def test_deletable_with_run_provenance(self, tmp_path: Path):
        assert self._ok(tmp_path, "feat/x") is True

    def test_without_provenance_rejected(self, tmp_path: Path):
        assert self._ok(tmp_path, "feat/x", provenance=False) is False

    @pytest.mark.parametrize("name", ["main", "Main", "master", "MASTER", "trunk", "Trunk"])
    def test_protected_and_default_rejected_casefold(self, tmp_path: Path, name: str):
        assert self._ok(tmp_path, name) is False

    @pytest.mark.parametrize("name", ["refs/heads/main", "refs/x", "feat/refs/x", "x/refs"])
    def test_refs_namespace_rejected(self, tmp_path: Path, name: str):
        assert self._ok(tmp_path, name) is False

    def test_baseline_head_rejected(self, tmp_path: Path):
        assert self._ok(tmp_path, "feat/old") is False

    def test_bad_ref_rejected(self, tmp_path: Path):
        assert self._ok(tmp_path, "a..b") is False


def _issue7() -> HarnessIssue:
    return _harness_issue(7, "abcd1234")


def _push_new_branch(
    work: Path, name: str, *, base: str = "main", email: str = "other@localhost", subject: str = "work"
) -> str:
    """Commit one file on new branch *name* (from *base*) in *work* and push it; return the tip."""
    _real_git("checkout", "-q", "-b", name, base, cwd=work)
    (work / f"{name.replace('/', '_')}.txt").write_text(f"{subject}\n")
    _real_git("add", ".", cwd=work)
    _real_git(
        "-c", f"user.email={email}", "-c", "user.name=Someone", "-c", "commit.gpgsign=false",
        "commit", "-q", "-m", subject, cwd=work,
    )
    _real_git("push", "-q", "origin", name, cwd=work)
    return _real_git("rev-parse", "HEAD", cwd=work)


def _foreign_clone(tmp_path: Path, sandbox: SandboxRepo, name: str = "foreign") -> Path:
    work = tmp_path / name
    _real_git("clone", "-q", sandbox.url, str(work), cwd=tmp_path)
    _real_git("config", "user.email", "mallory@example.com", cwd=work)
    _real_git("config", "user.name", "Mallory", cwd=work)
    return work


def _run_branch(project_dir: Path, issue: HarnessIssue, name: str = "feat/x") -> tuple[str, str]:
    """The legitimate flow: plan commit on a new branch, agent commit on top, pushed. (plan_sha, tip)."""
    _real_git("checkout", "-q", "-b", name, "main", cwd=project_dir)
    plan_sha = commit_plan(project_dir, issue)
    (project_dir / "agent.txt").write_text("agent work\n")
    _real_git("add", ".", cwd=project_dir)
    _real_git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "agent work", cwd=project_dir)
    _real_git("push", "-q", "origin", name, cwd=project_dir)
    tip = _real_git("rev-parse", "HEAD", cwd=project_dir)
    _real_git("checkout", "-q", "main", cwd=project_dir)
    return plan_sha, tip


def _provenance_dir(project_dir: Path) -> Path:
    """Harness-owned location outside the clone's parent tree (workspace/artifacts/provenance)."""
    return project_dir.parent.parent / "artifacts" / "provenance"


@pytest.mark.real_git
class TestBranchProvenance:
    def _check(
        self, project_dir: Path, sandbox: SandboxRepo, name: str, tip: str, plan_sha: str, default: str = "main"
    ) -> bool:
        return branch_has_run_provenance(
            project_dir, sandbox, name=name, tip_sha=tip, plan_sha=plan_sha, default_branch=default,
            provenance_dir=_provenance_dir(project_dir),
        )

    def test_graft_in_agent_clone_does_not_forge_provenance(self, tmp_path: Path):
        project_dir, sandbox = _seeded_clone(tmp_path)
        plan_sha, _ = _run_branch(project_dir, _issue7())
        ops_tip = _push_new_branch(_foreign_clone(tmp_path, sandbox), "ops", email="mallory@example.com")
        _real_git("fetch", "-q", "origin", "ops", cwd=project_dir)
        _real_git("replace", "--graft", ops_tip, plan_sha, cwd=project_dir)
        # The forgery works inside the clone: ancestry there now claims ops descends from the plan.
        assert _real_git("merge-base", "--is-ancestor", plan_sha, ops_tip, cwd=project_dir) == ""

        assert self._check(project_dir, sandbox, "ops", ops_tip, plan_sha) is False

    def test_provenance_dir_is_recreated_fresh_on_every_check(self, tmp_path: Path, monkeypatch):
        project_dir, sandbox = _seeded_clone(tmp_path)
        plan_sha, tip = _run_branch(project_dir, _issue7())
        provenance_dir = _provenance_dir(project_dir)
        assert not provenance_dir.exists()
        seen: list[list[str]] = []
        seen_kw: list[dict] = []
        real = harness_mod._git

        def spy(argv, **kw):
            seen.append(argv)
            seen_kw.append(kw)
            return real(argv, **kw)

        monkeypatch.setattr("hermes_pipeline.harness._git", spy)

        assert self._check(project_dir, sandbox, "feat/x", tip, plan_sha) is True
        # The root is harness-owned and persists; each check ran in a fresh ``prov-*`` subdir
        # that was removed afterwards, and unrelated files in the root are left alone.
        assert provenance_dir.is_dir()
        assert not (provenance_dir / "HEAD").exists()
        keep = provenance_dir / "keep.txt"
        keep.write_text("operator file\n")
        assert self._check(project_dir, sandbox, "feat/x", tip, plan_sha) is True
        assert keep.read_text() == "operator file\n"
        assert list(provenance_dir.glob("prov-*")) == []
        assert sum(1 for argv in seen if "init" in argv) == 2
        init_dirs = {Path(kw["cwd"]) for argv, kw in zip(seen, seen_kw, strict=True) if "init" in argv}
        assert len(init_dirs) == 2
        assert all(d.parent == provenance_dir and d.name.startswith("prov-") for d in init_dirs)
        # Every ancestry query runs with replace refs disabled.
        ancestry = [argv for argv in seen if "merge-base" in argv]
        assert ancestry and all(argv[1:3] == ["-c", "core.useReplaceRefs=false"] for argv in ancestry)

    def test_preseeded_grafts_and_alternates_are_discarded(self, fake_gh, tmp_path: Path):
        # Plan commit exists only in the clone; a forger plants a bare repo in the provenance ROOT
        # pointing at the clone's objects (alternates) and grafting the operator tip onto the plan.
        # The check runs in a fresh ``prov-*`` subdir, so the planted files cannot shape ancestry.
        project_dir, sandbox = _seeded_clone(tmp_path)
        baseline = take_baseline(project_dir, sandbox, viewer="octocat", default_branch="main")
        _real_git("checkout", "-q", "-b", "plan-only", "main", cwd=project_dir)
        plan_sha = commit_plan(project_dir, _issue7())
        _real_git("checkout", "-q", "main", cwd=project_dir)
        ops_tip = _push_new_branch(_foreign_clone(tmp_path, sandbox), "ops", email="mallory@example.com")
        provenance_dir = _provenance_dir(project_dir)
        _real_git("init", "-q", "--bare", str(provenance_dir), cwd=tmp_path)
        (provenance_dir / "info").mkdir(exist_ok=True)
        (provenance_dir / "info" / "grafts").write_text(f"{ops_tip} {plan_sha}\n")
        (provenance_dir / "objects" / "info").mkdir(parents=True, exist_ok=True)
        (provenance_dir / "objects" / "info" / "alternates").write_text(f"{project_dir / '.git' / 'objects'}\n")
        sentinel = provenance_dir / "SENTINEL"
        sentinel.write_text("seeded\n")
        fake_gh.on(*_ISSUE_BRANCH_PULLS_ARGV, stdout=json.dumps([[]]))
        fake_gh.on(*_SEARCH_ARGV, stdout=_search_page())

        artifacts = discover_remote_artifacts(
            project_dir, sandbox, issue=_issue7(), baseline=baseline, plan_sha=plan_sha,
            provenance_dir=provenance_dir,
        )

        assert artifacts.deletable_branches == ()
        assert artifacts.leftovers == (f"branch ops ({ops_tip[:7]}): no run provenance",)
        # Root contents are the caller's: never deleted, and never consulted.
        assert sentinel.read_text() == "seeded\n"
        assert (provenance_dir / "info" / "grafts").is_file()
        assert list(provenance_dir.glob("prov-*")) == []

    def test_provenance_root_containing_clone_is_rejected_before_any_deletion(self, tmp_path: Path):
        project_dir, sandbox = _seeded_clone(tmp_path)
        plan_sha, tip = _run_branch(project_dir, _issue7())
        before = sorted(str(p.relative_to(project_dir)) for p in project_dir.rglob("*"))

        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            branch_has_run_provenance(
                project_dir, sandbox, name="feat/x", tip_sha=tip, plan_sha=plan_sha, default_branch="main",
                provenance_dir=project_dir.parent,
            )

        assert exc_info.value.code == "pr_discovery_incomplete"
        assert exc_info.value.detail == "provenance_dir must not contain the clone"
        assert (project_dir / ".git" / "HEAD").is_file()
        assert sorted(str(p.relative_to(project_dir)) for p in project_dir.rglob("*")) == before

    def test_inherited_git_dir_does_not_redirect_provenance(self, tmp_path: Path, monkeypatch):
        project_dir, sandbox = _seeded_clone(tmp_path)
        plan_sha, _ = _run_branch(project_dir, _issue7())
        ops_tip = _push_new_branch(_foreign_clone(tmp_path, sandbox), "ops", email="mallory@example.com")
        _real_git("fetch", "-q", "origin", "ops", cwd=project_dir)
        _real_git("replace", "--graft", ops_tip, plan_sha, cwd=project_dir)
        monkeypatch.setenv("GIT_DIR", str(project_dir / ".git"))
        monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(project_dir / ".git" / "objects"))
        envs: list[dict] = []
        real = harness_mod._git

        def spy(argv, **kw):
            envs.append(kw["env"])
            return real(argv, **kw)

        monkeypatch.setattr("hermes_pipeline.harness._git", spy)

        assert self._check(project_dir, sandbox, "ops", ops_tip, plan_sha) is False
        assert envs and all("GIT_DIR" not in env and "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in env for env in envs)
        assert _provenance_dir(project_dir).is_dir()
        assert list(_provenance_dir(project_dir).glob("prov-*")) == []

    @pytest.mark.parametrize("bad", ["abc", "g" * 40, "", "0" * 39, "HEAD"])
    def test_malformed_tip_sha_fails_closed(self, tmp_path: Path, bad: str):
        project_dir, sandbox = _seeded_clone(tmp_path)
        plan_sha, _ = _run_branch(project_dir, _issue7())

        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            self._check(project_dir, sandbox, "feat/x", bad, plan_sha)

        assert exc_info.value.code == "pr_discovery_incomplete"

    @pytest.mark.parametrize("bad", ["abc", "g" * 40, "", "refs/harness/default"])
    def test_malformed_plan_sha_fails_closed(self, tmp_path: Path, bad: str):
        project_dir, sandbox = _seeded_clone(tmp_path)
        _, tip = _run_branch(project_dir, _issue7())

        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            self._check(project_dir, sandbox, "feat/x", tip, bad)

        assert exc_info.value.code == "pr_discovery_incomplete"

    def test_plan_absent_from_remote_lacks_provenance(self, tmp_path: Path):
        # The plan commit exists only in the clone (never pushed): no remote branch can be the run's.
        project_dir, sandbox = _seeded_clone(tmp_path)
        _real_git("checkout", "-q", "-b", "plan-only", "main", cwd=project_dir)
        plan_sha = commit_plan(project_dir, _issue7())
        _real_git("checkout", "-q", "main", cwd=project_dir)
        tip = _push_new_branch(project_dir, "feat/x", email="test@localhost")

        assert self._check(project_dir, sandbox, "feat/x", tip, plan_sha) is False

    def test_invalid_default_branch_name_lacks_provenance(self, tmp_path: Path):
        project_dir, sandbox = _seeded_clone(tmp_path)
        plan_sha, tip = _run_branch(project_dir, _issue7())

        assert self._check(project_dir, sandbox, "feat/x", tip, plan_sha, default="a..b") is False

    def test_default_branch_missing_on_remote_fails_closed(self, tmp_path: Path):
        project_dir, sandbox = _seeded_clone(tmp_path)
        plan_sha, tip = _run_branch(project_dir, _issue7())

        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            self._check(project_dir, sandbox, "feat/x", tip, plan_sha, default="trunk")

        assert exc_info.value.code == "pr_discovery_incomplete"

    def test_is_ancestor_raises_on_unknown_object(self, tmp_path: Path):
        project_dir, _ = _seeded_clone(tmp_path)

        with pytest.raises(HarnessPreflightError) as exc_info:
            harness_mod._is_ancestor(project_dir, "0" * 40, "HEAD")

        assert exc_info.value.code == "git_error"

    def test_legit_run_branch_has_provenance(self, tmp_path: Path):
        project_dir, sandbox = _seeded_clone(tmp_path)
        plan_sha, tip = _run_branch(project_dir, _issue7())

        assert self._check(project_dir, sandbox, "feat/x", tip, plan_sha) is True

    def test_foreign_branch_after_baseline_lacks_provenance(self, tmp_path: Path):
        project_dir, sandbox = _seeded_clone(tmp_path)
        plan_sha, _ = _run_branch(project_dir, _issue7())
        tip = _push_new_branch(_foreign_clone(tmp_path, sandbox), "ops", email="mallory@example.com")

        assert self._check(project_dir, sandbox, "ops", tip, plan_sha) is False

    def test_foreign_branch_merging_plan_commit_lacks_provenance(self, tmp_path: Path):
        project_dir, sandbox = _seeded_clone(tmp_path)
        plan_sha, _ = _run_branch(project_dir, _issue7())
        foreign = _foreign_clone(tmp_path, sandbox)
        _push_new_branch(foreign, "ops", email="mallory@example.com")
        _real_git("fetch", "-q", "origin", "feat/x", cwd=foreign)
        _real_git("-c", "commit.gpgsign=false", "merge", "-q", "--no-ff", "-m", "absorb plan", "origin/feat/x", cwd=foreign)
        _real_git("push", "-q", "origin", "ops", cwd=foreign)
        tip = _real_git("rev-parse", "HEAD", cwd=foreign)
        assert _real_git("merge-base", "--is-ancestor", plan_sha, tip, cwd=foreign) == ""  # plan IS an ancestor

        assert self._check(project_dir, sandbox, "ops", tip, plan_sha) is False

    def test_fast_forwarded_foreign_branch_lacks_provenance(self, tmp_path: Path):
        # ops = foreign commit, then run branch rebased on top: plan is ancestor of tip, foreign commit is not below plan.
        project_dir, sandbox = _seeded_clone(tmp_path)
        foreign = _foreign_clone(tmp_path, sandbox)
        _push_new_branch(foreign, "ops", email="mallory@example.com")
        _real_git("fetch", "-q", "origin", cwd=project_dir)
        _real_git("checkout", "-q", "-b", "feat/x", "origin/ops", cwd=project_dir)
        plan_sha = commit_plan(project_dir, _issue7())
        _real_git("push", "-q", "origin", "feat/x:ops", cwd=project_dir)
        _real_git("checkout", "-q", "main", cwd=project_dir)

        assert self._check(project_dir, sandbox, "ops", plan_sha, plan_sha) is False

    def test_tip_moved_between_discovery_and_check(self, tmp_path: Path):
        project_dir, sandbox = _seeded_clone(tmp_path)
        plan_sha, stale_tip = _run_branch(project_dir, _issue7())
        foreign = _foreign_clone(tmp_path, sandbox)
        _real_git("checkout", "-q", "-b", "feat/x", "origin/feat/x", cwd=foreign)
        (foreign / "more.txt").write_text("more\n")
        _real_git("add", ".", cwd=foreign)
        _real_git("-c", "commit.gpgsign=false", "commit", "-q", "-m", "more", cwd=foreign)
        _real_git("push", "-q", "origin", "feat/x", cwd=foreign)

        assert self._check(project_dir, sandbox, "feat/x", stale_tip, plan_sha) is False

    def test_plan_reachable_from_default_is_vacuous(self, tmp_path: Path):
        project_dir, sandbox = _seeded_clone(tmp_path)
        plan_sha = commit_plan(project_dir, _issue7())
        _real_git("push", "-q", "origin", "main", cwd=project_dir)
        tip = _push_new_branch(project_dir, "feat/x", email="test@localhost")

        assert self._check(project_dir, sandbox, "feat/x", tip, plan_sha) is False

    def test_fetch_failure_fails_closed(self, tmp_path: Path):
        project_dir, sandbox = _seeded_clone(tmp_path)
        plan_sha, tip = _run_branch(project_dir, _issue7())
        broken = dataclasses.replace(sandbox, url=f"file://{tmp_path / 'missing.git'}")

        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            self._check(project_dir, broken, "feat/x", tip, plan_sha)

        assert exc_info.value.code == "pr_discovery_incomplete"
        # The git failure detail (not just the ``git_error`` code) reaches the operator.
        assert "git fetch failed" in exc_info.value.detail

    def test_provenance_dir_inside_clone_is_rejected(self, tmp_path: Path):
        project_dir, sandbox = _seeded_clone(tmp_path)
        plan_sha, tip = _run_branch(project_dir, _issue7())

        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            branch_has_run_provenance(
                project_dir, sandbox, name="feat/x", tip_sha=tip, plan_sha=plan_sha, default_branch="main",
                provenance_dir=project_dir / ".hermes" / "provenance",
            )

        assert exc_info.value.code == "pr_discovery_incomplete"
        assert exc_info.value.detail == "provenance_dir must not be inside the clone"

    def test_invalid_name_never_fetched(self, tmp_path: Path, monkeypatch):
        project_dir, sandbox = _seeded_clone(tmp_path)
        seen: list[list[str]] = []
        real = harness_mod._git

        def spy(argv, **kw):
            seen.append(argv)
            return real(argv, **kw)

        monkeypatch.setattr("hermes_pipeline.harness._git", spy)

        assert self._check(project_dir, sandbox, "a..b", "0" * 40, "0" * 40) is False
        assert not any("fetch" in argv for argv in seen)


class TestDiscoverCandidatePrs:
    def _discover(self, tmp_path: Path, head_refs=("feat/x",)) -> tuple[int, ...]:
        return discover_candidate_prs(tmp_path, _SANDBOX, head_refs=head_refs, run_token="abcd1234")

    def test_unions_paginated_pulls_with_search_results_sorted_unique(self, fake_gh, tmp_path: Path):
        # Sparse, descending numbers so ``sorted`` is load-bearing; the qualifying PR sits on page 3.
        pages = [
            [{"number": n} for n in range(900, 700, -2)],
            [{"number": n} for n in range(700, 500, -2)],
            [{"number": n} for n in range(500, 300, -2)] + [{"number": 7}],
        ]
        fake_gh.on(*_PULLS_ARGV, stdout=json.dumps(pages))
        fake_gh.on(*_SEARCH_ARGV, stdout=_search_page(7, 9, 900, 301))

        numbers = self._discover(tmp_path)

        assert numbers == (7, 9, 301, *range(302, 901, 2))
        assert len(numbers) == len(set(numbers))
        assert [argv[-1] for argv in fake_gh.gh_calls()] == [_PULLS_ARGV[-1], _SEARCH_ARGV[-1]]
        assert all(kw["timeout"] == 180.0 for kw in fake_gh.kwargs)

    def test_queries_each_head_ref_once(self, fake_gh, tmp_path: Path):
        fake_gh.on(*_PULLS_ARGV, stdout=json.dumps([[{"number": 5}]]))
        fake_gh.on(*_ISSUE_BRANCH_PULLS_ARGV, stdout=json.dumps([[{"number": 6}]]))
        fake_gh.on(*_SEARCH_ARGV, stdout=_search_page())

        numbers = self._discover(tmp_path, head_refs=("feat/x", "feat/harness-abcd1234", "feat/x"))

        assert numbers == (5, 6)
        assert [argv[-1] for argv in fake_gh.gh_calls()] == [
            _PULLS_ARGV[-1], _ISSUE_BRANCH_PULLS_ARGV[-1], _SEARCH_ARGV[-1],
        ]

    def test_no_head_refs_skips_pulls_query(self, fake_gh, tmp_path: Path):
        fake_gh.on(*_SEARCH_ARGV, stdout=_search_page(9))

        assert self._discover(tmp_path, head_refs=()) == (9,)
        assert [argv[-1] for argv in fake_gh.gh_calls()] == [_SEARCH_ARGV[-1]]

    def test_listing_failure_fails_closed(self, fake_gh, tmp_path: Path):
        fake_gh.on(*_PULLS_ARGV, rc=1, stderr="HTTP 502")
        fake_gh.on(*_SEARCH_ARGV, stdout=_search_page())

        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            self._discover(tmp_path)

        assert exc_info.value.code == "pr_discovery_incomplete"

    def test_search_failure_fails_closed(self, fake_gh, tmp_path: Path):
        fake_gh.on(*_PULLS_ARGV, stdout=json.dumps([[]]))
        fake_gh.on(*_SEARCH_ARGV, rc=1, stderr="HTTP 422")

        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            self._discover(tmp_path)

        assert exc_info.value.code == "pr_discovery_incomplete"

    def test_incomplete_search_results_fail_closed(self, fake_gh, tmp_path: Path):
        fake_gh.on(*_PULLS_ARGV, stdout=json.dumps([[]]))
        fake_gh.on(*_SEARCH_ARGV, stdout=_search_page(9, incomplete=True))

        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            self._discover(tmp_path)

        assert exc_info.value.code == "pr_discovery_incomplete"
        assert "incomplete" in exc_info.value.detail

    @pytest.mark.parametrize(
        ("pulls", "search"),
        [
            ('[[{"number": 1}], "oops"]', '[{"total_count": 0, "items": []}]'),
            ('[[{"number": "1"}]]', '[{"total_count": 0, "items": []}]'),
            ("[[]]", '[{"total_count": 1}]'),
            ("[[]]", '[{"items": "nope"}]'),
            ("[[]]", '[{"items": [{"number": null}]}]'),
            ("[[]]", "not json"),
        ],
    )
    def test_malformed_page_fails_closed(self, fake_gh, tmp_path: Path, pulls: str, search: str):
        fake_gh.on(*_PULLS_ARGV, stdout=pulls)
        fake_gh.on(*_SEARCH_ARGV, stdout=search)

        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            self._discover(tmp_path)

        assert exc_info.value.code == "pr_discovery_incomplete"


class TestFetchPullRequest:
    def test_views_pr_with_json_fields(self, fake_gh, tmp_path: Path):
        fake_gh.on(*_pr_view_argv(5), stdout=json.dumps(_pr_view_payload(5)))

        pr = fetch_pull_request(tmp_path, _SANDBOX, 5)

        assert pr.number == 5 and pr.head_ref == "feat/x" and pr.author == "octocat"
        assert fake_gh.gh_calls() == [list(_pr_view_argv(5))[1:]]

    def test_failure_fails_closed(self, fake_gh, tmp_path: Path):
        fake_gh.on(*_pr_view_argv(5), rc=1, stderr="not found")

        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            fetch_pull_request(tmp_path, _SANDBOX, 5)

        assert exc_info.value.code == "pr_discovery_incomplete"

    @pytest.mark.parametrize(
        "override",
        [
            {"number": True},
            {"createdAt": "2026-09-01T12:00:05"},
            {"isCrossRepository": "false"},
            {"author": {"login": ""}},
            {"headRefName": ""},
            {"state": "weird"},
        ],
    )
    def test_malformed_payload_fails_closed(self, fake_gh, tmp_path: Path, override: dict):
        fake_gh.on(*_pr_view_argv(5), stdout=json.dumps(_pr_view_payload(5) | override))

        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            fetch_pull_request(tmp_path, _SANDBOX, 5)

        assert exc_info.value.code == "pr_discovery_incomplete"


@pytest.mark.real_git
class TestDiscoverRemoteArtifacts:
    def _setup(self, tmp_path: Path) -> tuple[Path, SandboxRepo, RunBaseline]:
        project_dir, sandbox = _seeded_clone(tmp_path)
        baseline = take_baseline(
            project_dir, sandbox, viewer="octocat", default_branch="main",
            now=datetime(2026, 9, 1, 12, 0, 1, tzinfo=UTC),
        )
        return project_dir, sandbox, baseline

    def _discover(self, project_dir, sandbox, baseline, plan_sha) -> RemoteArtifacts:
        return discover_remote_artifacts(
            project_dir, sandbox, issue=_issue7(), baseline=baseline, plan_sha=plan_sha,
            provenance_dir=_provenance_dir(project_dir),
        )

    def test_classifies_branches_and_prs_by_run_provenance(self, fake_gh, tmp_path: Path):
        project_dir, sandbox, baseline = self._setup(tmp_path)
        plan_sha, feat_sha = _run_branch(project_dir, _issue7(), "feat/x")
        (project_dir / ".hermes").mkdir(exist_ok=True)
        (project_dir / ".hermes" / "pipeline_branch.txt").write_text("recorded/x\n")
        foreign = _foreign_clone(tmp_path, sandbox)
        ops_sha = _push_new_branch(foreign, "ops")
        stray_sha = _push_new_branch(foreign, "stray")

        fake_gh.on(*_RECORDED_PULLS_ARGV, stdout=json.dumps([[]]))
        fake_gh.on(*_PULLS_ARGV, stdout=json.dumps([[{"number": 5}]]))
        fake_gh.on(*_ISSUE_BRANCH_PULLS_ARGV, stdout=json.dumps([[]]))
        fake_gh.on(*_SEARCH_ARGV, stdout=_search_page(9, 11))
        fake_gh.on(*_pr_view_argv(5), stdout=json.dumps(_pr_view_payload(5, head="feat/x")))
        # The agent (authenticated as the viewer) opened a PR from the operator's branch with the token in the title.
        fake_gh.on(*_pr_view_argv(9), stdout=json.dumps(_pr_view_payload(9, head="ops", title="[harness abcd1234] TODO-7")))
        fake_gh.on(*_pr_view_argv(11), stdout=json.dumps(_pr_view_payload(11, head="main", title="[harness abcd1234]")))

        artifacts = self._discover(project_dir, sandbox, baseline, plan_sha)

        assert artifacts.issue_number == 7
        assert [pr.number for pr in artifacts.prs] == [5]
        assert artifacts.deletable_branches == (("feat/x", feat_sha),)
        assert artifacts.leftovers == (
            f"branch ops ({ops_sha[:7]}): no run provenance",
            f"branch stray ({stray_sha[:7]}): no run provenance",
            "pr #9: not attributable",
            "pr #11: head main pre-existing (not created by this run)",
        )
        # Discovery queries the recorded branch, the issue branch, and every provenance head.
        assert [argv[-1] for argv in fake_gh.gh_calls()[:4]] == [
            _RECORDED_PULLS_ARGV[-1], _ISSUE_BRANCH_PULLS_ARGV[-1], _PULLS_ARGV[-1], _SEARCH_ARGV[-1],
        ]

    def test_issue_branch_query_used_without_recorded_branch(self, fake_gh, tmp_path: Path):
        project_dir, sandbox, baseline = self._setup(tmp_path)
        plan_sha, _ = _run_branch(project_dir, _issue7(), "feat/harness-abcd1234")
        fake_gh.on(*_ISSUE_BRANCH_PULLS_ARGV, stdout=json.dumps([[{"number": 5}]]))
        fake_gh.on(*_SEARCH_ARGV, stdout=_search_page())
        fake_gh.on(
            *_pr_view_argv(5), stdout=json.dumps(_pr_view_payload(5, head="feat/harness-abcd1234"))
        )

        artifacts = self._discover(project_dir, sandbox, baseline, plan_sha)

        # issue.branch is also the provenance head: queried once.
        assert [argv[-1] for argv in fake_gh.gh_calls()[:2]] == [_ISSUE_BRANCH_PULLS_ARGV[-1], _SEARCH_ARGV[-1]]
        assert artifacts.deletable_branches == (("feat/harness-abcd1234", _real_git("rev-parse", "feat/harness-abcd1234", cwd=project_dir)),)
        assert [pr.number for pr in artifacts.prs] == [5]
        assert artifacts.leftovers == ()

    def test_protected_named_new_head_is_leftover(self, fake_gh, tmp_path: Path):
        project_dir, sandbox, baseline = self._setup(tmp_path)
        plan_sha, feat_sha = _run_branch(project_dir, _issue7(), "feat/x")
        _real_git("push", "-q", "origin", "feat/x:refs/heads/Master", cwd=project_dir)
        master_sha = _real_git("rev-parse", "feat/x", cwd=project_dir)
        fake_gh.on(*_ISSUE_BRANCH_PULLS_ARGV, stdout=json.dumps([[]]))
        fake_gh.on(*_PULLS_ARGV, stdout=json.dumps([[]]))
        fake_gh.on(*_SEARCH_ARGV, stdout=_search_page())

        artifacts = self._discover(project_dir, sandbox, baseline, plan_sha)

        assert artifacts.deletable_branches == (("feat/x", feat_sha),)
        assert artifacts.leftovers == (f"branch Master ({master_sha[:7]}): protected",)

    def test_incomplete_search_results_propagate(self, fake_gh, tmp_path: Path):
        project_dir, sandbox, baseline = self._setup(tmp_path)
        plan_sha, _ = _run_branch(project_dir, _issue7(), "feat/x")
        fake_gh.on(*_ISSUE_BRANCH_PULLS_ARGV, stdout=json.dumps([[]]))
        fake_gh.on(*_PULLS_ARGV, stdout=json.dumps([[]]))
        fake_gh.on(*_SEARCH_ARGV, stdout=_search_page(5, incomplete=True))

        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            self._discover(project_dir, sandbox, baseline, plan_sha)

        assert exc_info.value.code == "pr_discovery_incomplete"

    def test_origin_swap_does_not_widen_enumeration(self, fake_gh, tmp_path: Path):
        project_dir, sandbox, baseline = self._setup(tmp_path)
        plan_sha, _ = _run_branch(project_dir, _issue7(), "feat/x")
        other = tmp_path / "other"
        other.mkdir()
        other_bare = _make_bare_remote(other, {"README.md": "other\n"})
        _real_git("clone", "-q", f"file://{other_bare}", str(other / "work"), cwd=other)
        _push_new_branch(other / "work", "X", email="test@localhost")
        _real_git("remote", "set-url", "origin", f"file://{other_bare}", cwd=project_dir)
        fake_gh.on(*_ISSUE_BRANCH_PULLS_ARGV, stdout=json.dumps([[]]))
        fake_gh.on(*_PULLS_ARGV, stdout=json.dumps([[]]))
        fake_gh.on(*_SEARCH_ARGV, stdout=_search_page())

        artifacts = self._discover(project_dir, sandbox, baseline, plan_sha)

        assert [name for name, _ in artifacts.deletable_branches] == ["feat/x"]
        assert not any(" X " in line for line in artifacts.leftovers)
        assert artifacts.leftovers == ()

    def test_ls_remote_failure_fails_closed(self, fake_gh, tmp_path: Path):
        project_dir, sandbox, baseline = self._setup(tmp_path)
        broken = dataclasses.replace(sandbox, url=f"file://{tmp_path / 'missing.git'}")

        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            self._discover(project_dir, broken, baseline, "0" * 40)

        assert exc_info.value.code == "pr_discovery_incomplete"
        assert "git ls-remote failed" in exc_info.value.detail

    def test_provenance_dir_inside_clone_is_rejected(self, fake_gh, tmp_path: Path):
        project_dir, sandbox, baseline = self._setup(tmp_path)
        plan_sha, _ = _run_branch(project_dir, _issue7(), "feat/x")

        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            discover_remote_artifacts(
                project_dir, sandbox, issue=_issue7(), baseline=baseline, plan_sha=plan_sha,
                provenance_dir=project_dir / "provenance",
            )

        assert exc_info.value.detail == "provenance_dir must not be inside the clone"
        assert fake_gh.gh_calls() == []

    def test_provenance_root_containing_clone_is_rejected(self, fake_gh, tmp_path: Path):
        project_dir, sandbox, baseline = self._setup(tmp_path)
        plan_sha, _ = _run_branch(project_dir, _issue7(), "feat/x")

        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            discover_remote_artifacts(
                project_dir, sandbox, issue=_issue7(), baseline=baseline, plan_sha=plan_sha,
                provenance_dir=project_dir.parent,
            )

        assert exc_info.value.code == "pr_discovery_incomplete"
        assert exc_info.value.detail == "provenance_dir must not contain the clone"
        assert (project_dir / ".git" / "HEAD").is_file()
        assert fake_gh.gh_calls() == []


def _artifacts(*prs: PullRequest) -> RemoteArtifacts:
    return RemoteArtifacts(issue_number=7, prs=prs, deletable_branches=(), leftovers=())


class TestVerifyPullRequest:
    def test_single_open_pr_is_returned(self):
        pr = _pr(5)
        assert verify_pull_request(_artifacts(pr)) is pr

    def test_no_pr_is_pr_missing(self):
        with pytest.raises(PullRequestInvariantError) as exc_info:
            verify_pull_request(_artifacts())
        assert exc_info.value.code == "pr_missing"

    def test_two_prs_is_pr_ambiguous(self):
        with pytest.raises(PullRequestInvariantError) as exc_info:
            verify_pull_request(_artifacts(_pr(5), _pr(7, head_ref="feat/y")))
        assert exc_info.value.code == "pr_ambiguous"
        assert exc_info.value.detail == "#5, #7"

    def test_closed_pr_is_pr_closed(self):
        pr = dataclasses.replace(_pr(5), state="CLOSED")
        with pytest.raises(PullRequestInvariantError) as exc_info:
            verify_pull_request(_artifacts(pr))
        assert exc_info.value.code == "pr_closed"
        assert exc_info.value.detail == "#5"

    def test_merged_state_is_pr_merged(self):
        pr = dataclasses.replace(_pr(5), state="MERGED", merged=True)
        with pytest.raises(PullRequestInvariantError) as exc_info:
            verify_pull_request(_artifacts(pr))
        assert exc_info.value.code == "pr_merged"

    def test_merged_flag_with_open_state_is_pr_merged(self):
        pr = dataclasses.replace(_pr(5), merged=True)
        with pytest.raises(PullRequestInvariantError) as exc_info:
            verify_pull_request(_artifacts(pr))
        assert exc_info.value.code == "pr_merged"

    def test_pr_invariant_event_shape(self):
        exc = PullRequestInvariantError("pr_ambiguous", "#5, #7")
        assert pr_invariant_event(exc) == ("pr_invariant_failed", {"code": "pr_ambiguous", "detail": "#5, #7"})
        assert isinstance(exc, RuntimeError)
        assert str(exc) == "pr_ambiguous"

    def test_harness_result_remote_fields_default_and_accept_values(self):
        default = HarnessResult(exit_code=0, report_path=None, temp_dir=None, summary="ok")
        assert (default.issue_number, default.pr_numbers, default.cleanup_leftovers) == (None, (), ())
        full = HarnessResult(
            exit_code=0, report_path=None, temp_dir=None, summary="ok",
            issue_number=7, pr_numbers=(5,), cleanup_leftovers=("branch feat/z",),
        )
        assert (full.issue_number, full.pr_numbers, full.cleanup_leftovers) == (7, (5,), ("branch feat/z",))
