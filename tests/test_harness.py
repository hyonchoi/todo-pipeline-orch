"""Unit tests for harness.py — live sandbox flow, preflight, convergence, monitor."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import shlex
import subprocess
import sys
import tomllib
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hermes_pipeline import harness as harness_mod
from hermes_pipeline.contract import ContractSchemaError
from hermes_pipeline.github_issues import (
    LABEL_VOCABULARY,
    GitHubIssuesError,
    compile_eligible_issues,
    gh_bin,
    issue_from_api,
    parse_issue_body,
    render_issue_body,
)
from hermes_pipeline.harness import (
    ConvergenceDetector,
    ConvergenceHaltError,
    GitHubPreflight,
    HarnessCleanupError,
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
    ShutdownReport,
    TickRegistration,
    _classify_error_class,
    _ConvergenceMonitor,
    _prune_retained_state,
    _validate_profile_prerequisites,
    assert_tick_id_unchanged,
    branch_has_run_provenance,
    cards_for_registered_keys,
    cleanup_remote,
    clone_sandbox,
    commit_plan,
    create_harness_issue,
    discover_candidate_prs,
    discover_remote_artifacts,
    fetch_pull_request,
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
    ready_issue_numbers,
    reconcile_created_issue,
    recover_pinned_registration,
    recover_tick_registration,
    resolve_sandbox_repo,
    run_harness,
    run_tick,
    sandbox_seed_check,
    shutdown_run,
    take_baseline,
    validate_live_profile,
    verify_pull_request,
    wait_for_issue_visible,
    write_project_contract,
)
from hermes_pipeline.phases import Phase, load_phases, resolve_profile_phases_path
from hermes_pipeline.plan_manifest import validate_plan_candidate
from tests.gh_fakes import API_ARGV, issue_payload, seed_project_issues, todo_payload


class TestPreflightCheck:
    def test_preflight_check_gh_not_found(self, monkeypatch):
        available = {"git", "hermes", "claude"}
        monkeypatch.setattr(
            "hermes_pipeline.harness.shutil.which",
            lambda executable: f"/bin/{executable}" if executable in available else None,
        )
        monkeypatch.delenv("TPO_GH_BIN", raising=False)
        with pytest.raises(RuntimeError, match="gh"):
            preflight_check()

    def test_preflight_check_git_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("PATH", "")
        with pytest.raises(RuntimeError, match="[Gg]it"):
            preflight_check()

    def test_preflight_check_hermes_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from hermes_pipeline.hermes_adapter import HermesDependencyError

        available = {"git", "gh"}
        monkeypatch.setattr(
            "hermes_pipeline.harness.shutil.which",
            lambda executable: f"/bin/{executable}" if executable in available else None,
        )
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


class TestHarnessProfileTopology:
    def test_unverified_profile_fails_before_preflight_or_workspace(self, mocker):
        preflight = mocker.patch("hermes_pipeline.harness.preflight_check")
        mkdtemp = mocker.patch("hermes_pipeline.harness.tempfile.mkdtemp")

        with pytest.raises(HarnessProfileError, match="unverified_prerequisites"):
            run_harness(
                fixture_name="happy-path",
                repo="acme/sandbox",
                loop=False,
                keep_dir=False,
                timeout=60,
                convergence_threshold=3,
                config=None,
                profile_name="agent-skills",
            )

        preflight.assert_not_called()
        mkdtemp.assert_not_called()

    def test_unsafe_profile_fails_before_preflight_or_workspace(self, mocker):
        from hermes_pipeline.phases import load_profile_prerequisites

        mocker.patch(
            "hermes_pipeline.phases.load_profile_prerequisites",
            return_value=load_profile_prerequisites("gstack"),
        )
        preflight = mocker.patch("hermes_pipeline.harness.preflight_check")
        mkdtemp = mocker.patch("hermes_pipeline.harness.tempfile.mkdtemp")

        with pytest.raises(HarnessProfileError, match="unsafe_terminal"):
            run_harness(
                fixture_name="happy-path",
                repo="acme/sandbox",
                loop=False,
                keep_dir=False,
                timeout=60,
                convergence_threshold=3,
                config=None,
                profile_name="native-sdd",
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
        assert excinfo.value.tick_id == "02TICK"

    def test_tick_not_persisted_and_picked_none_carry_no_tick_id(self, tmp_path: Path):
        state = tmp_path / ".hermes"
        with pytest.raises(HarnessTickError) as excinfo:
            recover_tick_registration(state, expected_issue=1)
        assert (excinfo.value.code, excinfo.value.tick_id) == ("tick_not_persisted", None)

        _write_tick_state(state, tick_id="01TICK", phases_outcome="picked_none")
        with pytest.raises(HarnessTickError) as excinfo:
            recover_tick_registration(state, expected_issue=1)
        assert (excinfo.value.code, excinfo.value.tick_id) == ("picked_none", None)

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

    def test_pre_persist_spawn_failure_without_tick_id_uses_filename(self, tmp_path: Path):
        outcomes = tmp_path / "outcomes"
        outcomes.mkdir()
        (outcomes / "03TICK.json").write_text(
            json.dumps({"outcome": "failed_to_spawn", "detail": _SPAWN_FAILURE_DETAIL}) + "\n"
        )

        with pytest.raises(HarnessTickError) as excinfo:
            recover_tick_registration(tmp_path, expected_issue=1)

        assert excinfo.value.code == "failed_to_spawn"
        assert excinfo.value.tick_id == "03TICK"

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
        assert excinfo.value.tick_id == "01TICK"

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
        assert excinfo.value.tick_id == "01TICK"
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
        assert excinfo.value.tick_id == "01TICK"


class TestCardsForRegisteredKeys:
    def test_unexpected_registration_has_no_tick_id_by_default(self):
        with pytest.raises(HarnessTickError) as excinfo:
            cards_for_registered_keys([], ["ghost"])
        assert (excinfo.value.code, excinfo.value.tick_id) == ("unexpected_registration", None)
        assert HarnessTickError("unexpected_registration", "ghost", tick_id="01TICK").tick_id == "01TICK"

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


def test_prune_retained_state_removes_only_safe_terminal_state(tmp_path):
    state_dir = tmp_path / ".hermes"
    state_dir.mkdir()
    (state_dir / "pipeline.toml").write_text("schema_version = 2\n")
    (state_dir / "pipeline_branch.txt").write_text("feat/mock\n")
    (state_dir / "unknown.json").write_text("{}\n")
    config_dir = tmp_path / "state"
    config_dir.mkdir()
    (config_dir / "tpo-config.yaml").write_text("state_dir: .hermes\n")
    (config_dir / "pipeline_locks").mkdir()

    empty_outcomes = state_dir / "outcomes"
    empty_outcomes.mkdir()
    empty_checkpoints = state_dir / "pipeline_checkpoints"
    empty_checkpoints.mkdir()
    evidence_dir = state_dir / "ready_for_review"
    evidence_dir.mkdir()
    (evidence_dir / "failure.json").write_text("{}\n")

    _prune_retained_state(state_dir, config_dir)

    assert (state_dir / "pipeline.toml").exists()
    assert (state_dir / "unknown.json").exists()
    # The branch pointer names the kept remote branch; it is retained on purpose.
    assert (state_dir / "pipeline_branch.txt").read_text() == "feat/mock\n"
    assert not (config_dir / "tpo-config.yaml").exists()
    assert (config_dir / "pipeline_locks").exists()
    assert not empty_outcomes.exists()
    assert not empty_checkpoints.exists()
    assert (evidence_dir / "failure.json").exists()


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

    def _run_poll(self, monkeypatch, mocker, status_sequence, tmp_path, cards=None):
        from hermes_pipeline.harness import (
            ConvergenceDetector,
            HarnessMonitor,
            _ConvergenceMonitor,
            poll_registered_phases,
        )

        monkeypatch.setattr("hermes_pipeline.harness.time.sleep", lambda *_a, **_kw: None)
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            side_effect=status_sequence,
        )
        if cards is None:
            keys = sorted({key for snapshot in status_sequence for key in snapshot})
            cards = [Phase(phase_key=key, name=key.upper()) for key in keys]

        log_path = tmp_path / "events.jsonl"
        base_monitor = HarnessMonitor(log_path)
        detector = ConvergenceDetector(threshold=99)
        monitor = _ConvergenceMonitor(base_monitor, detector, {})

        return poll_registered_phases(
            project_slug="proj",
            tick_id="tick-1",
            state_dir=tmp_path,
            todo_id="TODO-30",
            cards=cards,
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

    def test_initial_status_table_prints_before_polling(self, monkeypatch, mocker, tmp_path, caplog):
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

    def test_partial_terminal_snapshot_waits_for_complete_registered_cards(
        self, monkeypatch, mocker, tmp_path
    ):
        """Terminal statuses cannot complete a poll until every registered card is present."""
        status_sequence = [
            {"p1": "done"},
            {"p1": "done"},
            {"p1": "done", "p2": "done"},
            {"p1": "done", "p2": "done"},
        ]
        cards = [Phase(phase_key="p1", name="P1"), Phase(phase_key="p2", name="P2")]

        assert self._run_poll(
            monkeypatch, mocker, tmp_path=tmp_path, status_sequence=status_sequence, cards=cards
        )
        from hermes_pipeline import kanban_tasks

        assert kanban_tasks.get_todo_kanban_status.call_count == 4
        events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
        assert any(
            event["event_type"] == "phase_completed" and event["phase_key"] == "p2"
            for event in events
        )


_GSTACK_PHASES = load_phases(resolve_profile_phases_path("gstack"))
_LIVE_KEYS = tuple(phase.phase_key for phase in _GSTACK_PHASES[:2])


class _LiveRunStubs:
    """Stub every live building block ``run_harness`` orchestrates and record the call order.

    Each stub is looked up on the instance at call time, so a test overrides a
    step (``stubs.poll = ...``) before running the harness. ``order`` lists the
    step names in the sequence ``run_harness`` invoked them.
    """

    SANDBOX = SandboxRepo(repo="acme/sandbox", slug="sandbox", url="https://github.com/acme/sandbox.git")

    def __init__(self, monkeypatch, workspace: Path) -> None:
        self.workspace = workspace
        self.tmp_root = workspace.parent
        self.project_dir = workspace / "projects" / "sandbox"
        self.artifacts_dir = workspace / "artifacts"
        self.order: list[str] = []
        self.args: dict[str, tuple] = {}
        self.kwargs: dict[str, dict] = {}
        self.issue = _harness_issue(42, "tok00000")
        self.baseline = _baseline()
        self.registration = TickRegistration(tick_id="tick-1", todo_id="TODO-42", phase_keys=_LIVE_KEYS)
        self.pr = _pr(11, head_ref=self.issue.branch)
        self.artifacts = RemoteArtifacts(issue_number=42, prs=(self.pr,), deletable_branches=(), leftovers=())
        self.shutdown_report = ShutdownReport(
            tick_id="tick-1", kanban_quiescent=True, remote_all_ok=True, leftovers=(), branch_deletion_skipped=False
        )
        self.status_map: dict[str, str] = {}
        # Overridable steps.
        self.clone = lambda sandbox, project_dir, branch=None: (project_dir / ".hermes").mkdir(parents=True)
        self.wait_visible = lambda *_a, **_k: None
        self.recover = lambda *_a, **_k: self.registration
        self.tick = lambda *_a, **_k: 0
        self.current_tick_id: str | None = None
        self.poll = lambda **_k: True
        self.verify = lambda artifacts: self.pr

        self.real_mkdtemp = real_mkdtemp = harness_mod.tempfile.mkdtemp
        self.snapshot: list[dict] | None = None
        monkeypatch.delenv("TPO_GH_BIN", raising=False)
        monkeypatch.setattr(
            "hermes_pipeline.kanban_tasks._list_task_snapshot", lambda _tenant: self.snapshot
        )

        def workspace_mkdtemp(prefix=None, dir=None, **kwargs):
            # Only the workspace allocation (under the harness tmp root) is redirected.
            if dir is not None and Path(dir) == self.tmp_root:
                return str(workspace)
            return real_mkdtemp(prefix=prefix, dir=dir, **kwargs)

        monkeypatch.setattr(harness_mod, "_harness_tmp_root", lambda: self.tmp_root)
        monkeypatch.setattr(harness_mod.tempfile, "mkdtemp", workspace_mkdtemp)
        real_prune = harness_mod._prune_retained_state
        self._stub(monkeypatch, "_prune_retained_state", lambda *a, **k: real_prune(*a, **k))
        monkeypatch.setattr(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status", lambda *_a, **_k: dict(self.status_map)
        )
        self._stub(monkeypatch, "resolve_sandbox_repo", lambda repo, env=None: self.SANDBOX)
        self._stub(monkeypatch, "_run_token", lambda: "tok00000")
        self._stub(monkeypatch, "preflight_check", lambda **_k: None)
        self._stub(
            monkeypatch, "github_preflight",
            lambda *_a, **_k: GitHubPreflight(viewer="octocat", default_branch="main", permission="WRITE"),
        )
        self._stub(monkeypatch, "_kanban_preflight", lambda **_k: None)
        self._stub(monkeypatch, "clone_sandbox", lambda *a, **k: self.clone(*a, **k))
        self._stub(monkeypatch, "sandbox_seed_check", lambda *_a, **_k: None)
        self._stub(monkeypatch, "take_baseline", lambda *_a, **_k: self.baseline)
        self._stub(monkeypatch, "write_project_contract", lambda *_a, **_k: None)
        self._stub(monkeypatch, "create_harness_issue", lambda *_a, **_k: self.issue)
        self._stub(monkeypatch, "commit_plan", lambda *_a, **_k: "a" * 40)
        self._stub(monkeypatch, "wait_for_issue_visible", lambda *a, **k: self.wait_visible(*a, **k))
        self._stub(monkeypatch, "read_current_tick_id", lambda *_a, **_k: self.current_tick_id)
        self._stub(monkeypatch, "run_tick", lambda *a, **k: self.tick(*a, **k))
        self._stub(monkeypatch, "recover_tick_registration", lambda *a, **k: self.recover(*a, **k))
        self._stub(monkeypatch, "poll_registered_phases", lambda **k: self.poll(**k))
        self._stub(monkeypatch, "discover_remote_artifacts", lambda *_a, **_k: self.artifacts)
        self._stub(monkeypatch, "verify_pull_request", lambda artifacts, **_k: self.verify(artifacts))
        self._stub(monkeypatch, "shutdown_run", lambda *_a, **_k: self.shutdown_report)

    def _stub(self, monkeypatch, name: str, fn) -> None:
        def recorded(*args, **kwargs):
            self.order.append(name)
            self.args[name] = args
            self.kwargs[name] = kwargs
            return fn(*args, **kwargs)

        monkeypatch.setattr(harness_mod, name, recorded)

    def events(self) -> list[dict]:
        log_path = self.artifacts_dir / "events.jsonl"
        if not log_path.exists():
            return []
        return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]

    def run(self, **overrides):
        kwargs = dict(
            fixture_name="happy-path", repo="acme/sandbox", loop=False, keep_dir=False,
            timeout=60, convergence_threshold=3, config=None,
        )
        kwargs.update(overrides)
        return run_harness(**kwargs)


_LIVE_HAPPY_ORDER = [
    "resolve_sandbox_repo", "_run_token", "preflight_check", "github_preflight", "_kanban_preflight",
    "clone_sandbox", "sandbox_seed_check", "take_baseline", "write_project_contract",
    "create_harness_issue", "commit_plan", "wait_for_issue_visible", "read_current_tick_id", "run_tick",
    "recover_tick_registration", "poll_registered_phases", "discover_remote_artifacts",
    "verify_pull_request", "shutdown_run",
]


class TestRunHarness:
    """``run_harness`` orchestrates the live sandbox flow and always shuts down once its issue exists."""

    @pytest.fixture
    def live(self, monkeypatch, tmp_path):
        return _LiveRunStubs(monkeypatch, tmp_path / "hermes-tmp" / "harness-run")

    def test_workspace_is_allocated_under_the_harness_tmp_root(self, live, monkeypatch, tmp_path):
        monkeypatch.setattr(harness_mod.tempfile, "mkdtemp", live.real_mkdtemp)  # real allocation
        live.tmp_root.mkdir(parents=True, exist_ok=True)

        result = live.run(keep_dir=True)

        assert result.temp_dir is not None
        assert result.temp_dir.parent == live.tmp_root
        assert result.temp_dir.name.startswith("harness-")
        assert not Path("~/.hermes/tmp").expanduser().joinpath(result.temp_dir.name).exists()
        assert live.workspace.exists() is False  # the stubbed path was never used

    def test_happy_path_orders_live_steps_and_reports(self, live, capsys):
        seen_at_recheck: dict[str, list] = {}

        def wait_visible(project_dir, sandbox, *, issue_number, **_kwargs):
            seen_at_recheck["events"] = live.events()
            seen_at_recheck["exclude"] = issue_number

        live.wait_visible = wait_visible
        live.status_map = dict.fromkeys(_LIVE_KEYS, "done")

        result = live.run()

        assert live.order == _LIVE_HAPPY_ORDER
        assert result.exit_code == 0
        assert live.workspace.parent == live.tmp_root
        assert "_prune_retained_state" not in live.order
        assert result.issue_number == 42
        assert result.pr_numbers == (11,)
        assert result.cleanup_leftovers == ()
        assert result.report_path == live.artifacts_dir / "reports" / "report.json"
        assert result.temp_dir is None
        assert not live.workspace.exists()

        # run_started precedes the quiescence re-check and carries the live identity.
        (started,) = [event for event in seen_at_recheck["events"] if event["event_type"] == "run_started"]
        assert started["repo"] == "acme/sandbox"
        assert started["issue_number"] == 42
        assert started["run_token"] == "tok00000"
        assert started["fixture_name"] == "happy-path"
        assert started["profile"] == "gstack"
        assert started["kanban_mode"] == "hermes"
        assert seen_at_recheck["exclude"] == 42

        assert live.args["clone_sandbox"] == (live.SANDBOX, live.project_dir)
        assert live.kwargs["clone_sandbox"] == {"branch": "main"}
        assert live.args["sandbox_seed_check"] == (live.project_dir, live.SANDBOX)
        assert live.kwargs["take_baseline"] == {"viewer": "octocat", "default_branch": "main"}
        assert live.args["write_project_contract"] == (live.project_dir, "gstack")
        assert live.kwargs["create_harness_issue"] == {"run_token": "tok00000", "baseline": live.baseline}
        assert live.args["commit_plan"] == (live.project_dir, live.issue)
        assert live.args["run_tick"] == ("sandbox",)
        assert live.kwargs["run_tick"] == {
            "cwd": live.workspace, "log_path": live.artifacts_dir / "tick.log", "timeout": 60,
        }
        assert live.args["recover_tick_registration"] == (live.project_dir / ".hermes",)
        assert live.kwargs["recover_tick_registration"] == {
            "expected_issue": 42, "tick_log": live.artifacts_dir / "tick.log", "previous_tick_id": None,
        }
        poll = live.kwargs["poll_registered_phases"]
        assert poll["project_slug"] == "sandbox"
        assert poll["tick_id"] == "tick-1"
        assert poll["todo_id"] == "TODO-42"
        assert poll["state_dir"] == live.project_dir / ".hermes"
        assert poll["cards"] == list(_GSTACK_PHASES[:2])
        assert live.kwargs["discover_remote_artifacts"] == {
            "issue": live.issue, "baseline": live.baseline, "plan_sha": "a" * 40,
            "provenance_dir": live.artifacts_dir / "provenance",
        }
        assert live.args["verify_pull_request"] == (live.artifacts,)
        assert live.kwargs["verify_pull_request"] == {"default_branch": "main"}
        assert live.args["shutdown_run"] == (live.project_dir, live.SANDBOX)
        assert live.kwargs["shutdown_run"] == {
            "issue": live.issue, "baseline": live.baseline, "plan_sha": "a" * 40,
            "tick_id": "tick-1", "expected_phase_keys": _LIVE_KEYS,
            "provenance_dir": live.artifacts_dir / "provenance",
            "staging_root": live.artifacts_dir / "staging", "keep_remote": False,
            "assume_workers_may_exist": False,
        }
        assert "repo=acme/sandbox issue=#42 pr=#11" in capsys.readouterr().out
        assert "profile=gstack" in result.summary

    def test_kanban_summary_line_reads_the_archived_snapshot(self, live, capsys):
        live.snapshot = [
            {"id": "t1", "status": "archived",
             "body": json.dumps({"tick_id": "tick-1", "phase_key": _LIVE_KEYS[0]}) + "\nbody"},
            {"id": "t2", "status": "done",
             "body": json.dumps({"tick_id": "tick-1", "phase_key": _LIVE_KEYS[1]}) + "\nbody"},
            {"id": "t9", "status": "running",
             "body": json.dumps({"tick_id": "other", "phase_key": _LIVE_KEYS[0]}) + "\nbody"},
        ]
        live.status_map = {}  # get_todo_kanban_status omits archived cards

        live.run()

        out = capsys.readouterr().out
        assert f"phases={{'{_LIVE_KEYS[0]}': 'archived', '{_LIVE_KEYS[1]}': 'done'}}" in out

    def test_kanban_summary_line_falls_back_to_empty_when_snapshot_unreadable(self, live, capsys):
        live.snapshot = None

        live.run()

        assert "phases={}" in capsys.readouterr().out

    def test_issue_creation_left_uncertain_retains_workspace(self, live, monkeypatch):
        def create(*_a, **_k):
            raise HarnessRemoteCleanupError("issue_unreconciled", "create failed; listing unavailable")

        live._stub(monkeypatch, "create_harness_issue", create)

        with pytest.raises(HarnessRemoteCleanupError, match="issue_unreconciled"):
            live.run()

        assert "shutdown_run" not in live.order
        assert live.workspace.exists()

    def test_gh_override_is_forbidden_before_any_remote_step(self, live, monkeypatch):
        monkeypatch.setenv("TPO_GH_BIN", "/opt/fake/gh")

        with pytest.raises(HarnessPreflightError) as exc_info:
            live.run()

        assert exc_info.value.code == "gh_override_forbidden"
        assert "TPO_GH_BIN" in exc_info.value.detail
        assert live.order == []
        assert not live.workspace.exists()

    def test_tick_not_persisted_log_tail_is_debug_only(self, live, caplog):
        def recover(*_a, **_k):
            raise HarnessTickError("tick_not_persisted", "SECRET-TOKEN-IN-TAIL")

        live.recover = recover

        with caplog.at_level(logging.DEBUG, logger="hermes_pipeline.harness"):
            result = live.run()

        assert result.exit_code == 1
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("tick_not_persisted" in m and "artifacts/tick.log" in m for m in errors)
        assert not any("SECRET-TOKEN-IN-TAIL" in m for m in errors)
        debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("SECRET-TOKEN-IN-TAIL" in m for m in debug)

    def test_unexpected_registration_after_recovery_shuts_down_the_recovered_tick(self, live, monkeypatch):
        def cards(*_a, **_k):
            raise HarnessTickError("unexpected_registration", "plan:zzz")

        live._stub(monkeypatch, "cards_for_registered_keys", cards)

        result = live.run()

        assert result.exit_code == 1
        assert result.summary.startswith("[unexpected_registration] ")
        assert "poll_registered_phases" not in live.order
        assert live.kwargs["shutdown_run"]["tick_id"] == "tick-1"
        assert live.kwargs["shutdown_run"]["expected_phase_keys"] is None
        assert live.kwargs["shutdown_run"]["assume_workers_may_exist"] is False

    def test_unknown_fixture_fails_before_any_step(self, live):
        with pytest.raises(HarnessPreflightError) as exc_info:
            live.run(fixture_name="sad-path")

        assert exc_info.value.code == "unknown_fixture"
        assert exc_info.value.detail == "sad-path"
        assert live.order == []
        assert not live.workspace.exists()

    def test_gh_bypass_removed_preflight_requires_gh(self, monkeypatch):
        available = {"git", "hermes", "claude"}
        monkeypatch.setattr(
            "hermes_pipeline.harness.shutil.which",
            lambda executable: f"/bin/{executable}" if executable in available else None,
        )
        monkeypatch.setenv("TPO_GH_BIN", "/fixture/bin/gh")
        with pytest.raises(RuntimeError, match="gh"):
            preflight_check()

    def test_other_ready_issue_at_recheck_shuts_down_without_tick(self, live):
        def wait_visible(*_a, **_k):
            raise HarnessPreflightError("sandbox_not_quiescent", "#12, #15")

        live.wait_visible = wait_visible

        with pytest.raises(HarnessPreflightError) as exc_info:
            live.run()

        assert exc_info.value.code == "sandbox_not_quiescent"
        assert exc_info.value.detail == "#12, #15"
        assert "run_tick" not in live.order
        assert live.order[-1] == "shutdown_run"
        assert live.kwargs["shutdown_run"]["tick_id"] is None
        assert live.kwargs["shutdown_run"]["expected_phase_keys"] is None
        assert not live.workspace.exists()

    def test_tick_error_becomes_exit_1_after_shutdown(self, live):
        def recover(*_a, **_k):
            raise HarnessTickError("failed_to_spawn", "spawn detail", tick_id="T")

        live.recover = recover

        result = live.run()

        assert result.exit_code == 1
        assert result.summary.startswith("[failed_to_spawn] ")
        assert "poll_registered_phases" not in live.order
        assert "discover_remote_artifacts" not in live.order
        assert live.kwargs["shutdown_run"]["tick_id"] == "T"
        assert live.kwargs["shutdown_run"]["expected_phase_keys"] is None
        assert result.issue_number == 42
        assert result.pr_numbers == ()

    def test_picked_none_without_tick_id_shuts_down_issue_only(self, live):
        def recover(*_a, **_k):
            raise HarnessTickError("picked_none", "01TICK")

        live.recover = recover

        result = live.run()

        assert result.exit_code == 1
        assert "[picked_none]" in result.summary
        assert live.kwargs["shutdown_run"]["tick_id"] is None

    def test_poll_timeout_emits_event_then_shuts_down(self, live):
        lifecycle: list[str] = []

        def cooperative_poll(**kwargs):
            lifecycle.append("poll_started")
            assert kwargs["cancel_event"].wait(5)
            lifecycle.append("poll_stopped")
            return False

        live.poll = cooperative_poll
        live.status_map = {_LIVE_KEYS[0]: "running"}

        result = live.run(timeout=1, keep_dir=True)

        assert result.exit_code == 1
        assert lifecycle == ["poll_started", "poll_stopped"]
        assert result.summary.startswith("[overall timeout after 1s] ")
        timed_out = [event for event in live.events() if event["event_type"] == "phase_timed_out"]
        assert [event["phase_key"] for event in timed_out] == [_LIVE_KEYS[0]]
        assert "discover_remote_artifacts" not in live.order
        assert live.order[-1] == "shutdown_run"
        assert live.kwargs["shutdown_run"]["tick_id"] == "tick-1"
        assert live.kwargs["shutdown_run"]["keep_remote"] is True
        assert "_prune_retained_state" not in live.order
        assert (live.workspace / "state" / "tpo-config.yaml").exists()

    def test_timeout_is_exit_1_even_if_the_worker_reported_success(self, live, monkeypatch):
        live._stub(monkeypatch, "_run_with_timeout", lambda *_a, **_k: (True, True, {}))

        result = live.run()

        assert result.exit_code == 1
        assert result.summary.startswith("[overall timeout after 60s] ")
        assert "discover_remote_artifacts" not in live.order

    def test_pr_invariant_failure_is_exit_1_and_still_shuts_down(self, live):
        def verify(_artifacts):
            raise PullRequestInvariantError("pr_missing", "issue #42 produced no attributable PR")

        live.verify = verify

        result = live.run(keep_dir=True)

        assert result.exit_code == 1
        assert result.pr_numbers == ()
        assert "[pr_missing]" in result.summary
        failed = [event for event in live.events() if event["event_type"] == "pr_invariant_failed"]
        assert failed and failed[0]["code"] == "pr_missing"
        assert live.order.index("shutdown_run") < live.order.index("_prune_retained_state")

    def test_shutdown_not_quiescent_retains_workspace_and_raises(self, live):
        live.shutdown_report = ShutdownReport(
            tick_id="tick-1", kanban_quiescent=False, remote_all_ok=False,
            leftovers=("kanban not quiescent for tick tick-1",), branch_deletion_skipped=True,
        )

        with pytest.raises(HarnessCleanupError) as exc_info:
            live.run()

        assert exc_info.type is HarnessCleanupError
        assert "kanban not quiescent for tick tick-1" in str(exc_info.value)
        assert str(live.workspace) in str(exc_info.value)
        assert live.workspace.exists()

    def test_remote_cleanup_incomplete_raises_and_retains_workspace(self, live):
        live.shutdown_report = ShutdownReport(
            tick_id="tick-1", kanban_quiescent=True, remote_all_ok=False,
            leftovers=("pr #11: close failed (gh_unavailable)",), branch_deletion_skipped=False,
        )

        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            live.run()

        assert exc_info.value.code == "cleanup_incomplete"
        assert "pr #11: close failed" in exc_info.value.detail
        assert f"workspace retained at {live.workspace}" in exc_info.value.detail
        assert live.workspace.exists()

    def test_keep_dir_keeps_remote_and_workspace(self, live):
        kept = "kept remote artifacts for issue #42 in acme/sandbox (run tok00000)"
        live.shutdown_report = ShutdownReport(
            tick_id="tick-1", kanban_quiescent=True, remote_all_ok=True, leftovers=(kept,),
            branch_deletion_skipped=True,
        )

        def tick(*_a, **_k):
            (live.project_dir / ".hermes" / "pipeline_branch.txt").write_text("feat/harness-tok00000\n")
            return 0

        live.tick = tick

        result = live.run(keep_dir=True)

        assert result.exit_code == 0
        assert live.kwargs["shutdown_run"]["keep_remote"] is True
        assert result.temp_dir == live.workspace
        assert result.cleanup_leftovers == (kept,)
        assert live.workspace.exists()
        assert not (live.workspace / "state" / "tpo-config.yaml").exists()
        assert (live.project_dir / ".hermes" / "pipeline_branch.txt").read_text() == "feat/harness-tok00000\n"
        assert live.args["_prune_retained_state"] == (live.project_dir / ".hermes", live.workspace / "state")
        finished = [event for event in live.events() if event["event_type"] == "run_finished"]
        assert finished[0]["issue_number"] == 42
        assert finished[0]["pr_numbers"] == [11]
        assert finished[0]["leftovers"] == [kept]

    def test_failure_before_issue_exists_skips_shutdown(self, live):
        def clone(*_a, **_k):
            raise HarnessPreflightError("git_error", "clone failed")

        live.clone = clone

        with pytest.raises(HarnessPreflightError, match="git_error"):
            live.run()

        assert "create_harness_issue" not in live.order
        assert "shutdown_run" not in live.order
        assert not live.workspace.exists()

    def test_poll_exception_runs_shutdown_and_retains_when_not_quiescent(self, live):
        def poll(**_k):
            raise RuntimeError("boom")

        live.poll = poll
        live.shutdown_report = ShutdownReport(
            tick_id="tick-1", kanban_quiescent=False, remote_all_ok=False, leftovers=("left",),
            branch_deletion_skipped=True,
        )

        with pytest.raises(RuntimeError, match="boom"):
            live.run()

        assert live.order[-1] == "shutdown_run"
        assert live.kwargs["shutdown_run"]["tick_id"] == "tick-1"
        assert live.workspace.exists()

    def test_poll_exception_with_clean_shutdown_removes_workspace(self, live):
        def poll(**_k):
            raise RuntimeError("boom")

        live.poll = poll

        with pytest.raises(RuntimeError, match="boom"):
            live.run()

        assert live.order[-1] == "shutdown_run"
        assert not live.workspace.exists()

    def test_loop_snapshots_live_beside_artifacts(self, live):
        live.run(loop=True, keep_dir=True)

        assert (live.artifacts_dir / "happy-path-report.1.json").exists()
        assert not (live.project_dir / "happy-path-report.1.json").exists()

    def test_prompt_client_flows_to_preflight_and_isolated_config(self, live):
        from types import SimpleNamespace

        seen: dict[str, str] = {}

        def tick(*_a, **_k):
            seen["config"] = Path(os.environ["TPO_CONFIG_FILE"]).read_text()
            return 0

        live.tick = tick
        live.run(keep_dir=True, config=SimpleNamespace(prompt_client="codex"))

        assert live.kwargs["preflight_check"]["prompt_client"] == "codex"
        assert live.kwargs["preflight_check"]["profile_name"] == "gstack"
        assert live.kwargs["preflight_check"]["prerequisites"].profile == "gstack"
        config = yaml.safe_load(seen["config"])
        assert config == {
            "state_dir": str(live.workspace / "state"),
            "projects_dir": str(live.workspace / "projects"),
            "prompt_client": "codex",
        }

    def test_keyboard_interrupt_retains_workspace_without_remote_ops(self, live, caplog):
        def tick(*_a, **_k):
            raise KeyboardInterrupt

        live.tick = tick

        with caplog.at_level(logging.ERROR, logger="hermes_pipeline.harness"), pytest.raises(KeyboardInterrupt):
            live.run()

        assert "shutdown_run" not in live.order
        assert live.workspace.exists()
        (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
        message = record.getMessage()
        for pointer in (
            "issue #42", "acme/sandbox", "tok00000", str(live.workspace),
            "gh issue close 42 --repo acme/sandbox", "gh pr list --repo acme/sandbox", "git ls-remote --heads",
        ):
            assert pointer in message, pointer

    def test_tick_timeout_with_new_tick_id_shuts_down_that_tick(self, live):
        def tick(*_a, **_k):
            live.current_tick_id = "T2"
            raise HarnessTickError("tick_timeout", "60s")

        live.tick = tick

        result = live.run()

        assert result.exit_code == 1
        assert result.summary.startswith("[tick_timeout] ")
        assert "recover_tick_registration" not in live.order
        assert live.kwargs["shutdown_run"]["tick_id"] == "T2"
        assert live.kwargs["shutdown_run"]["expected_phase_keys"] is None
        assert live.kwargs["shutdown_run"]["assume_workers_may_exist"] is False
        assert not live.workspace.exists()

    def test_tick_timeout_without_tick_id_is_not_provably_idle(self, live):
        def tick(*_a, **_k):
            raise HarnessTickError("tick_timeout", "60s")

        live.tick = tick
        live.shutdown_report = ShutdownReport(
            tick_id=None, kanban_quiescent=False, remote_all_ok=False,
            leftovers=("tick timed out before a tick id was persisted",), branch_deletion_skipped=True,
        )

        with pytest.raises(HarnessCleanupError) as exc_info:
            live.run()

        assert live.kwargs["shutdown_run"]["tick_id"] is None
        assert live.kwargs["shutdown_run"]["assume_workers_may_exist"] is True
        assert "tick timed out before a tick id was persisted" in str(exc_info.value)
        assert "for an unknown tick" in str(exc_info.value)
        assert "tick None" not in str(exc_info.value)
        assert live.workspace.exists()

    def test_unexpected_tick_runner_error_is_treated_like_a_tick_failure(self, live):
        def tick(*_a, **_k):
            raise OSError("no python")

        live.tick = tick
        live.current_tick_id = None
        live.shutdown_report = ShutdownReport(
            tick_id=None, kanban_quiescent=False, remote_all_ok=False,
            leftovers=("tick id unknown after a tick failure",), branch_deletion_skipped=True,
        )

        with pytest.raises(HarnessCleanupError, match="tick id unknown"):
            live.run()

        assert live.kwargs["shutdown_run"]["tick_id"] is None
        assert live.kwargs["shutdown_run"]["assume_workers_may_exist"] is True
        assert live.workspace.exists()

    def test_shutdown_exception_is_chained_to_the_pending_failure(self, live, monkeypatch):
        def poll(**_k):
            raise RuntimeError("boom")

        def shutdown(*_a, **_k):
            raise OSError("disk")

        live.poll = poll
        live._stub(monkeypatch, "shutdown_run", shutdown)

        with pytest.raises(OSError, match="disk") as exc_info:
            live.run()

        assert isinstance(exc_info.value.__cause__, RuntimeError)
        assert str(exc_info.value.__cause__) == "boom"
        assert live.workspace.exists()

    def test_shutdown_interrupt_chains_the_pending_failure(self, live, monkeypatch):
        def poll(**_k):
            raise RuntimeError("boom")

        def shutdown(*_a, **_k):
            raise KeyboardInterrupt

        live.poll = poll
        live._stub(monkeypatch, "shutdown_run", shutdown)

        with pytest.raises(KeyboardInterrupt) as exc_info:
            live.run()

        assert isinstance(exc_info.value.__cause__, RuntimeError)
        assert live.workspace.exists()

    def test_shutdown_exception_without_pending_failure_retains_workspace(self, live, monkeypatch):
        def shutdown(*_a, **_k):
            raise OSError("disk")

        live._stub(monkeypatch, "shutdown_run", shutdown)

        with pytest.raises(OSError, match="disk"):
            live.run()

        assert live.workspace.exists()

    def test_convergence_halt_is_a_named_failure(self, live):
        def poll(**_k):
            raise ConvergenceHaltError("3 consecutive failures")

        live.poll = poll

        result = live.run()

        assert result.exit_code == 1
        assert result.summary.startswith("[convergence_halt] ")

    def test_poll_cancellation_error_message_carries_leftovers(self, live, monkeypatch):
        from hermes_pipeline.harness import PollCancellationError

        def stuck(*_a, **_k):
            raise PollCancellationError("poll worker did not stop after cooperative cancellation")

        live._stub(monkeypatch, "_run_with_timeout", stuck)
        live.shutdown_report = ShutdownReport(
            tick_id="tick-1", kanban_quiescent=True, remote_all_ok=True, leftovers=("left-x",),
            branch_deletion_skipped=False,
        )

        with pytest.raises(HarnessCleanupError) as exc_info:
            live.run()

        assert "did not stop" in str(exc_info.value)
        assert "left-x" in str(exc_info.value)
        assert live.workspace.exists()

    def test_pending_exception_carries_shutdown_leftovers_as_a_note(self, live):
        def poll(**_k):
            raise RuntimeError("boom")

        live.poll = poll
        live.shutdown_report = ShutdownReport(
            tick_id="tick-1", kanban_quiescent=True, remote_all_ok=False, leftovers=("left-y",),
            branch_deletion_skipped=False,
        )

        with pytest.raises(RuntimeError, match="boom") as exc_info:
            live.run()

        assert any("left-y" in note for note in getattr(exc_info.value, "__notes__", []))
        assert live.workspace.exists()


class TestRunWithTimeoutWorker:
    def test_worker_system_exit_becomes_poll_cancellation(self):
        from hermes_pipeline.harness import PollCancellationError, _run_with_timeout

        def fn():
            raise SystemExit(3)

        with pytest.raises(PollCancellationError, match="poll worker exited: SystemExit") as exc_info:
            _run_with_timeout(fn, timeout=5)

        assert isinstance(exc_info.value.__cause__, SystemExit)

    def test_worker_keyboard_interrupt_propagates(self):
        from hermes_pipeline.harness import _run_with_timeout

        def fn():
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            _run_with_timeout(fn, timeout=5)


class TestKanbanPreflight:
    @patch("hermes_pipeline.harness.subprocess.run")
    def test_preflight_failure_raises(self, mock_run):
        from hermes_pipeline.harness import KanbanPreflightError, _kanban_preflight

        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not authenticated")

        with pytest.raises(KanbanPreflightError, match="hermes login"):
            _kanban_preflight(tenant="sandbox")

    @patch("hermes_pipeline.harness.subprocess.run")
    def test_preflight_timeout_raises_actionable_error(self, mock_run):
        import subprocess

        from hermes_pipeline.harness import KanbanPreflightError, _kanban_preflight

        mock_run.side_effect = subprocess.TimeoutExpired(["hermes", "kanban", "list"], 15)

        with pytest.raises(KanbanPreflightError, match="timed out.*15s"):
            _kanban_preflight(tenant="sandbox")


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


class TestPollRegisteredPhaseTransitions:
    """Transition and event semantics of poll_registered_phases() on a registered card set."""

    @staticmethod
    def _monitor(tmp_path, threshold=3):
        from hermes_pipeline.harness import (
            ConvergenceDetector,
            HarnessMonitor,
            _ConvergenceMonitor,
        )

        detector = ConvergenceDetector(threshold=threshold)
        monitor = _ConvergenceMonitor(HarnessMonitor(tmp_path / "events.jsonl"), detector, {})
        return monitor, detector

    @staticmethod
    def _events(tmp_path):
        lines = (tmp_path / "events.jsonl").read_text().strip().splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    @staticmethod
    def _poll(tmp_path, monitor, detector, cards, **overrides):
        from hermes_pipeline.harness import poll_registered_phases

        kwargs = dict(
            project_slug="demo", tick_id="01TICK",
            state_dir=tmp_path / ".hermes", todo_id="TODO-1",
            cards=cards,
            monitor=monitor, detector=detector, poll_interval=0.1,
        )
        kwargs.update(overrides)
        return poll_registered_phases(**kwargs)

    def test_cancellation_interrupts_poll_wait_without_writing_outcomes(self, tmp_path, mocker):
        import threading

        cancel_event = threading.Event()

        def _initial_status(*_args, **_kwargs):
            cancel_event.set()
            return {"phase_2_autoplan": "running"}

        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=_initial_status)
        sleep = mocker.patch("hermes_pipeline.harness.time.sleep")
        observe = mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        monitor, detector = self._monitor(tmp_path)

        result = self._poll(
            tmp_path, monitor, detector, [Phase(phase_key="phase_2_autoplan", name="Autoplan")],
            tick_id="01CANCEL", cancel_event=cancel_event,
        )

        assert result is False
        sleep.assert_not_called()
        observe.assert_not_called()

    def test_emits_phase_failed_event_on_kanban_failure(self, tmp_path, mocker):
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=[
            {"phase_2_autoplan": "running"},
            {"phase_2_autoplan": "failed"},
        ])
        monitor, detector = self._monitor(tmp_path)

        result = self._poll(tmp_path, monitor, detector, [Phase(phase_key="phase_2_autoplan", name="Autoplan")])

        assert result is False
        failed = [e for e in self._events(tmp_path) if e["event_type"] == "phase_failed"]
        assert len(failed) == 1
        assert failed[0]["phase_key"] == "phase_2_autoplan"

    def test_emits_phase_blocked_event_on_kanban_block(self, tmp_path, mocker):
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=[
            {"phase_4_development": "running"},
            {"phase_4_development": "blocked"},
            {"phase_4_development": "blocked"},
        ])
        monitor, detector = self._monitor(tmp_path)

        result = self._poll(
            tmp_path, monitor, detector,
            [Phase(phase_key="phase_4_development", name="Phase 4: Development", prompt="", tools="", turns=0)],
        )

        assert result is False
        blocked = [e for e in self._events(tmp_path) if e["event_type"] == "phase_blocked"]
        assert len(blocked) == 1
        assert blocked[0]["phase_key"] == "phase_4_development"

    def test_convergence_halt_stops_polling(self, tmp_path, mocker):
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
        monitor, detector = self._monitor(tmp_path)
        cards = [Phase(phase_key=key, name=key.upper()) for key in ("p1", "p2", "p3")]

        result = self._poll(tmp_path, monitor, detector, cards)

        assert result is False
        mock_observe.assert_called_once()

    def test_auto_completes_blocked_gates(self, tmp_path, mocker):
        # Record what had already been emitted at each auto-complete call: the
        # gate hook must run *after* the phase_completed event for that key, so
        # the event log never lags the board mutation.
        emitted_at_call = []

        def _record(*_args, completed_phase_key, **_kwargs):
            written = (tmp_path / "events.jsonl").exists()
            last = [e["event_type"] for e in self._events(tmp_path)][-1:] if written else []
            emitted_at_call.append((completed_phase_key, last))

        mock_auto = mocker.patch(
            "hermes_pipeline.harness._auto_complete_gate_tasks", side_effect=_record
        )
        call_count = [0]

        def fake_status(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"phase_2_autoplan": "running", "phase_2b_plan_gate": "blocked"}
            return {"phase_2_autoplan": "done", "phase_2b_plan_gate": "done"}

        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=fake_status)
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        monitor, detector = self._monitor(tmp_path)
        cards = [
            Phase(phase_key="phase_2_autoplan", name="Autoplan"),
            Phase(phase_key="phase_2b_plan_gate", name="Plan gate", gate=True),
        ]

        self._poll(tmp_path, monitor, detector, cards)

        mock_auto.assert_any_call("demo", "01TICK", completed_phase_key="phase_2_autoplan", phases=cards)
        mock_auto.assert_any_call("demo", "01TICK", completed_phase_key="phase_2b_plan_gate", phases=cards)
        assert mock_auto.call_count == 2
        assert emitted_at_call == [
            ("phase_2_autoplan", ["phase_completed"]),
            ("phase_2b_plan_gate", ["phase_completed"]),
        ]

    def test_emits_phase_failed_when_ready_transitions_directly_to_failed(self, tmp_path, mocker):
        """Regression: a phase can jump straight from ready/blocked to failed
        without ever passing through running. Prior to this fix, such a
        transition was silently absorbed by the terminal-status check without
        emitting phase_failed or being seen by the convergence detector."""
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status", return_value={"phase_2_autoplan": "failed"}
        )
        monitor, detector = self._monitor(tmp_path)

        result = self._poll(tmp_path, monitor, detector, [Phase(phase_key="phase_2_autoplan", name="Autoplan")])

        assert result is False
        failed = [e for e in self._events(tmp_path) if e["event_type"] == "phase_failed"]
        assert len(failed) == 1
        assert failed[0]["phase_key"] == "phase_2_autoplan"

    def test_poll_interval_backs_off_and_resets_on_change(self, tmp_path, mocker):
        """Regression: fixed 5s poll interval added constant load for
        long-running phases. Interval should grow while status is unchanged
        and reset when a transition occurs."""
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
        monitor, detector = self._monitor(tmp_path)

        self._poll(
            tmp_path, monitor, detector, [Phase(phase_key="phase_2_autoplan", name="Autoplan")],
            poll_interval=1.0, max_poll_interval=10.0,
        )

        # Unchanged status ("running" repeated) should back off between polls 2-3.
        assert sleep_calls[2] > sleep_calls[1]
        # Transition to "done" grows the interval for that poll (backoff is only
        # reset for the *next* sleep, after the transition is observed).
        assert sleep_calls[3] > sleep_calls[2]
        # First two sleeps stay at the base interval: poll 1 sees the initial
        # empty->running "change" and resets before poll 2 fires.
        assert sleep_calls[0] == sleep_calls[1] == 1.0


class TestPollPinnedRun:
    """poll_pinned_run(): settle-only poller for requires_plan (native-sdd) runs.

    Gates are controlled by ``tpo tick`` reconcilers, so the poller never
    completes cards; it only emits transitions and reports the settled map.
    """

    _monitor = staticmethod(TestPollRegisteredPhaseTransitions._monitor)
    _events = staticmethod(TestPollRegisteredPhaseTransitions._events)

    @staticmethod
    def _poll(monitor, detector, step_keys, **overrides):
        from hermes_pipeline.harness import poll_pinned_run

        kwargs = dict(
            project_slug="demo", tick_id="01TICK", todo_id="TODO-1",
            step_keys=step_keys, monitor=monitor, detector=detector, poll_interval=0.1,
        )
        kwargs.update(overrides)
        return poll_pinned_run(**kwargs)

    @staticmethod
    def _forbid_auto_complete(mocker):
        def _boom(*_args, **_kwargs):
            raise AssertionError("poll_pinned_run must never complete cards")

        return mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks", side_effect=_boom)

    class _ScriptExhausted(BaseException):
        """Not an Exception: poll_pinned_run's ``except Exception: continue``
        must not be able to swallow the end-of-script signal."""

    class _Waiter:
        """Stand-in cancel_event: records requested intervals, never blocks.

        The poller waits on ``cancel_event`` instead of ``time.sleep`` whenever
        one is supplied, so this doubles as the backoff recorder.
        """

        def __init__(self):
            self.waits: list[float] = []
            self._set = False

        def set(self):
            self._set = True

        def is_set(self):
            return self._set

        def wait(self, timeout=None):
            self.waits.append(timeout)
            return self._set

    @classmethod
    def _fake_status(cls, mocker, snapshots, waiter=None, before_call=None):
        """Patch get_todo_kanban_status with a *self-bounding* scripted fake.

        A bare ``side_effect`` list is unsafe here: the exhausted list raises
        StopIteration, which the poller's ``except Exception: continue`` swallows,
        so a mutant that stops settling would spin forever instead of failing.
        This fake instead cancels the poll when the script runs out, and escalates
        to a BaseException if the poller ignores the cancel.
        """
        waiter = waiter if waiter is not None else cls._Waiter()
        remaining = list(snapshots)
        state = {"last": {}, "overruns": 0}

        def _next(*_args, **_kwargs):
            if before_call is not None:
                before_call()
            if remaining:
                state["last"] = remaining.pop(0)
                return state["last"]
            state["overruns"] += 1
            if state["overruns"] > 2:
                raise cls._ScriptExhausted(
                    "poll_pinned_run kept polling after the scripted snapshots ran out "
                    "and after cancel_event was set"
                )
            waiter.set()
            return state["last"]

        status = mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=_next
        )
        return status, waiter

    def _run(self, tmp_path, mocker, snapshots, step_keys, **overrides):
        self._forbid_auto_complete(mocker)
        mocker.patch("time.sleep")
        status, waiter = self._fake_status(mocker, snapshots)
        monitor, detector = self._monitor(tmp_path)
        overrides.setdefault("cancel_event", waiter)
        result = self._poll(monitor, detector, step_keys, **overrides)
        return result, status

    def test_settles_when_every_card_done(self, tmp_path, mocker):
        final = {"plan": "done", "validate:plan": "done"}
        result, _ = self._run(tmp_path, mocker, [final, final], ["plan", "validate:plan"])
        assert result == final

    def test_settles_when_gate_blocked(self, tmp_path, mocker):
        final = {"plan": "done", "human-gate": "blocked"}
        result, _ = self._run(tmp_path, mocker, [final, final], ["plan", "human-gate"])
        assert result == final

    def test_settles_with_failed_card(self, tmp_path, mocker):
        final = {"plan": "failed", "validate:plan": "done"}
        # The initial fetch seeds the baseline, so the failure has to happen
        # *after* it for phase_failed to be emitted.
        snapshots = [{"plan": "running", "validate:plan": "running"}, final]
        result, _ = self._run(tmp_path, mocker, snapshots, ["plan", "validate:plan"])
        assert result == final
        failed = [e for e in self._events(tmp_path) if e["event_type"] == "phase_failed"]
        assert [e["phase_key"] for e in failed] == ["plan"]

    @pytest.mark.parametrize("pending", ["ready", "running", "todo"])
    def test_keeps_polling_while_any_card_pending(self, tmp_path, mocker, pending):
        snapshots = [
            {"plan": "done", "validate:plan": pending},
            {"plan": "done", "validate:plan": pending},
            {"plan": "done", "validate:plan": pending},
            {"plan": "done", "validate:plan": "done"},
        ]
        result, status = self._run(tmp_path, mocker, snapshots, ["plan", "validate:plan"])
        assert result == {"plan": "done", "validate:plan": "done"}
        assert status.call_count == 4

    def test_does_not_settle_while_step_key_missing(self, tmp_path, mocker):
        snapshots = [
            {"plan": "done"},
            {"plan": "done"},
            {"plan": "done"},
            {"plan": "done", "validate:plan": "done"},
        ]
        result, status = self._run(tmp_path, mocker, snapshots, ["plan", "validate:plan"])
        assert result == {"plan": "done", "validate:plan": "done"}
        assert status.call_count == 4

    def test_does_not_settle_on_empty_map(self, tmp_path, mocker):
        snapshots = [{}, {}, {}, {"plan": "done"}]
        result, status = self._run(tmp_path, mocker, snapshots, ["plan"])
        assert result == {"plan": "done"}
        assert status.call_count == 4

    def test_emits_transitions_for_dynamic_key(self, tmp_path, mocker):
        snapshots = [
            {"plan": "ready"},
            {"plan": "running"},
            {"plan": "done", "review:0": "running"},
            {"plan": "done", "review:0": "done"},
        ]
        result, _ = self._run(tmp_path, mocker, snapshots, ["plan"])
        assert result == {"plan": "done", "review:0": "done"}
        events = [(e["event_type"], e["phase_key"]) for e in self._events(tmp_path)]
        assert events == [
            ("phase_started", "plan"),
            ("phase_completed", "plan"),
            ("phase_started", "review:0"),
            ("phase_completed", "review:0"),
        ]

    def test_never_calls_auto_complete_gate_tasks(self, tmp_path, mocker):
        snapshots = [
            {"plan": "running", "validate:plan": "blocked"},
            {"plan": "running", "validate:plan": "blocked"},
            # The tick reconciler, not the poller, moves the gate on after plan is done.
            {"plan": "done", "validate:plan": "ready"},
            {"plan": "done", "validate:plan": "done"},
        ]
        self._forbid_auto_complete(mocker)
        mocker.patch("time.sleep")
        _, waiter = self._fake_status(mocker, snapshots)
        monitor, detector = self._monitor(tmp_path)
        from hermes_pipeline import harness

        result = self._poll(monitor, detector, ["plan", "validate:plan"], cancel_event=waiter)
        assert result == {"plan": "done", "validate:plan": "done"}
        harness._auto_complete_gate_tasks.assert_not_called()

    def test_cancel_before_first_poll_returns_empty_map(self, tmp_path, mocker):
        import threading

        cancel_event = threading.Event()

        def _initial_status(*_args, **_kwargs):
            cancel_event.set()
            return {"plan": "running"}

        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=_initial_status)
        sleep = mocker.patch("hermes_pipeline.harness.time.sleep")
        monitor, detector = self._monitor(tmp_path)

        result = self._poll(monitor, detector, ["plan"], cancel_event=cancel_event)

        assert result == {}
        sleep.assert_not_called()

    def test_cancel_after_populated_poll_returns_empty_map(self, tmp_path, mocker):
        """Cancellation is not a result: the last observed map is discarded, so a
        cancelled poll can never be mistaken for a settled one."""
        self._forbid_auto_complete(mocker)
        sleep = mocker.patch("hermes_pipeline.harness.time.sleep")
        populated = {"plan": "running", "validate:plan": "done"}
        # Script ends on a non-settling but populated poll; the fake then cancels.
        status, waiter = self._fake_status(mocker, [{"plan": "ready"}, populated])
        monitor, detector = self._monitor(tmp_path)

        result = self._poll(monitor, detector, ["plan", "validate:plan"], cancel_event=waiter)

        assert waiter.is_set()
        assert status.call_count >= 2  # initial fetch plus at least one populated poll
        assert result == {}
        sleep.assert_not_called()

    def test_convergence_halt_stops_polling(self, tmp_path, mocker):
        # Mirrors TestPollRegisteredPhaseTransitions.test_convergence_halt_stops_polling:
        # the detector trips on the third consecutive failure and the poller stops
        # even though p3 is still pending.
        snapshots = [
            {"p1": "running", "p2": "ready", "p3": "ready"},
            {"p1": "failed", "p2": "running", "p3": "ready"},
            {"p1": "failed", "p2": "failed", "p3": "running"},
            {"p1": "failed", "p2": "failed", "p3": "failed"},
        ]
        result, status = self._run(tmp_path, mocker, snapshots, ["p1", "p2", "p3"])
        assert result == {"p1": "failed", "p2": "failed", "p3": "failed"}
        assert status.call_count == 4
        assert len([e for e in self._events(tmp_path) if e["event_type"] == "phase_failed"]) == 3

    def test_poll_interval_backs_off_and_resets_on_change(self, tmp_path, mocker):
        self._forbid_auto_complete(mocker)
        # The poller waits on cancel_event rather than time.sleep, so the waiter
        # records the backoff schedule.
        snapshots = [{"plan": "ready"}] + [{"plan": "running"}] * 4 + [{"plan": "done"}]
        _, waiter = self._fake_status(mocker, snapshots)
        monitor, detector = self._monitor(tmp_path)

        self._poll(
            monitor, detector, ["plan"],
            poll_interval=1.0, max_poll_interval=10.0, cancel_event=waiter,
        )

        waits = waiter.waits
        # ready -> running on the first poll resets the interval for the second.
        assert waits[0] == waits[1] == 1.0
        assert waits[2] > waits[1]
        assert waits[3] > waits[2]

    def test_tracks_current_phase_key_across_the_run(self, tmp_path, mocker):
        """current_phase_key drives partial reports on overall-timeout: it must
        name the in-flight phase mid-poll and clear once that phase completes."""
        self._forbid_auto_complete(mocker)
        monitor, detector = self._monitor(tmp_path)
        observed: list[str | None] = []
        snapshots = [
            {"plan": "ready"},
            {"plan": "running"},
            {"plan": "running"},
            {"plan": "done"},
        ]
        _, waiter = self._fake_status(
            mocker, snapshots, before_call=lambda: observed.append(monitor.current_phase_key)
        )

        result = self._poll(monitor, detector, ["plan"], cancel_event=waiter)

        assert result == {"plan": "done"}
        # Sampled just before each fetch: nothing in flight for the initial fetch
        # and the first poll, then "plan" while it runs.
        assert observed == [None, None, "plan", "plan"]
        assert monitor.current_phase_key is None

    def test_clears_current_phase_key_when_in_flight_card_settles_silently(self, tmp_path, mocker):
        """Defensive (R-H2.2): "archived" is terminal but the shared emitter has
        no branch for it, so a running -> archived card emits nothing. Left alone,
        current_phase_key would keep naming it and an overall-timeout partial
        report would blame a phase that already settled."""
        self._forbid_auto_complete(mocker)
        monitor, detector = self._monitor(tmp_path)
        snapshots = [{"plan": "ready"}, {"plan": "running"}, {"plan": "archived"}]
        _, waiter = self._fake_status(mocker, snapshots)

        result = self._poll(monitor, detector, ["plan"], cancel_event=waiter)

        assert result == {"plan": "archived"}
        # No transition event exists for the silent settle...
        assert [e["event_type"] for e in self._events(tmp_path)] == ["phase_started"]
        # ...but nothing is still in flight at settle.
        assert monitor.current_phase_key is None

    def test_second_call_does_not_replay_events_from_earlier_ticks(self, tmp_path, mocker):
        """Regression: per-tick calls share one monitor/detector. Cards already
        terminal at the initial fetch must not be re-emitted or re-recorded."""
        self._forbid_auto_complete(mocker)
        first = {"plan": "failed", "build": "failed"}
        second = {"plan": "failed", "build": "failed", "review:0": "done"}
        _, waiter = self._fake_status(
            mocker, [{"plan": "running", "build": "running"}, first, first, second]
        )
        monitor, detector = self._monitor(tmp_path, threshold=3)

        assert self._poll(monitor, detector, ["plan", "build"], cancel_event=waiter) == first
        events_after_first = self._events(tmp_path)
        assert [(e["event_type"], e["phase_key"]) for e in events_after_first] == [
            ("phase_failed", "plan"),
            ("phase_failed", "build"),
        ]
        assert not detector.should_halt()

        assert self._poll(monitor, detector, ["plan", "build", "review:0"], cancel_event=waiter) == second

        new_events = self._events(tmp_path)[len(events_after_first):]
        assert [(e["event_type"], e["phase_key"]) for e in new_events] == [("phase_completed", "review:0")]
        # Replaying the two earlier failures would have tripped the threshold-3
        # detector on this second call.
        assert not detector.should_halt()

    def test_transition_between_initial_fetch_and_first_snapshot_is_emitted(self, tmp_path, mocker):
        snapshots = [{"plan": "running"}, {"plan": "done"}]
        result, _ = self._run(tmp_path, mocker, snapshots, ["plan"])
        assert result == {"plan": "done"}
        assert [(e["event_type"], e["phase_key"]) for e in self._events(tmp_path)] == [
            ("phase_completed", "plan"),
        ]

    def test_warns_once_per_poll_on_unknown_status(self, tmp_path, mocker, caplog):
        import logging

        snapshots = [
            {"plan": "unknown", "validate:plan": "unknown"},
            {"plan": "unknown", "validate:plan": "unknown"},
            {"plan": "done", "validate:plan": "done"},
        ]
        with caplog.at_level(logging.WARNING, logger="hermes_pipeline.harness"):
            result, _ = self._run(tmp_path, mocker, snapshots, ["plan", "validate:plan"])
        assert result == {"plan": "done", "validate:plan": "done"}
        warnings = [r for r in caplog.records if "unknown" in r.getMessage()]
        assert len(warnings) == 1
        assert "plan" in warnings[0].getMessage()

    def test_empty_step_keys_rejected(self, tmp_path, mocker):
        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", return_value={})
        monitor, detector = self._monitor(tmp_path)
        with pytest.raises(ValueError):
            self._poll(monitor, detector, [])


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

    def test_rejects_owner_longer_than_39_chars_even_with_hyphens(self):
        owner = "a" + "-a" * 38  # 77 chars: alternating hyphens must not defeat the 39-char cap
        assert len(owner) == 77
        with pytest.raises(HarnessPreflightError) as exc_info:
            resolve_sandbox_repo(f"{owner}/repo", {})
        assert exc_info.value.code == "invalid_repo"
        assert resolve_sandbox_repo(f"{'a' + '-a' * 19}/repo", {}).repo.startswith("a-a")

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


def _seed_bare_remote(
    tmp_path: Path, *, seed_paths: tuple[str, ...], files: dict[str, str] | None = None
) -> tuple[Path, str]:
    """Create a bare ``main`` remote seeded with *seed_paths* (placeholder content), the
    sandbox ``.gitignore``, and *files*; return (bare, head sha)."""
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
    contents = {rel: f"# {rel}\n" for rel in seed_paths}
    contents.setdefault(".gitignore", harness_mod._SANDBOX_GITIGNORE)
    contents.update(files or {})
    for rel, content in contents.items():
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
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

        assert _split_git_argv(calls[0]["argv"]) == (
            str(project_dir.parent.resolve()), True, ["clone", "--template=", "--", self.sandbox.url, str(project_dir)]
        )
        assert calls[0]["cwd"] == tmp_path
        assert calls[0]["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert [_split_git_argv(c["argv"]) for c in calls[1:]] == [
            (str(project_dir.resolve()), False, ["config", "user.email", "test@localhost"]),
            (str(project_dir.resolve()), False, ["config", "user.name", "TPO Harness"]),
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

        assert _split_git_argv(calls[0]) == (
            str(project_dir.parent.resolve()), True,
            ["clone", "--branch", "develop", "--template=", "--", self.sandbox.url, str(project_dir)],
        )

    def test_clone_parent_mkdir_failure_is_git_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "hermes_pipeline.harness._git", lambda argv, **kw: pytest.fail("git must not run")
        )
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory\n")

        with pytest.raises(HarnessPreflightError) as exc_info:
            clone_sandbox(self.sandbox, blocker / "sandbox")

        assert exc_info.value.code == "git_error"
        assert "blocker" in exc_info.value.detail

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
            rest = _split_git_argv(argv)[2]
            assert rest[:2] == ["cat-file", "-t"]
            kind = "tree" if rest[2] == "HEAD:tests/__init__.py" else "blob"
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
    @pytest.mark.parametrize(
        "gitignore", [None, "# no runtime ignores\n__pycache__/\n"], ids=["absent", "lacks_hermes"]
    )
    def test_seed_check_requires_gitignore_with_hermes_rule(self, tmp_path, gitignore):
        files = {".gitignore": gitignore} if gitignore is not None else {".gitignore": ""}
        bare, _sha = _seed_bare_remote(tmp_path, seed_paths=_ALL_SEED_PATHS, files=files)
        sandbox = dataclasses.replace(self.sandbox, url=f"file://{bare}")
        project_dir = tmp_path / "sandbox"
        clone_sandbox(sandbox, project_dir)
        if gitignore is None:
            subprocess.run(["git", "rm", "-q", ".gitignore"], cwd=project_dir, check=True, capture_output=True)
            subprocess.run(
                ["git", "-c", "user.email=t@l", "-c", "user.name=t", "commit", "-qm", "drop ignore"],
                cwd=project_dir, check=True, capture_output=True,
            )

        with pytest.raises(HarnessPreflightError) as exc_info:
            sandbox_seed_check(project_dir, sandbox)

        assert exc_info.value.code == "sandbox_not_seeded"
        assert ".gitignore" in exc_info.value.detail
        assert ".hermes/" in exc_info.value.detail
        assert "tpo test --repo acme/sandbox --init-sandbox" in exc_info.value.detail

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
        assert [_split_git_argv(argv)[1:] for argv in seen] == [
            (True, ["ls-remote", "--heads", "--", "https://github.com/acme/sandbox.git"])
        ]
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

    def test_unknown_profile_fails_before_touching_fs(self, tmp_path):
        target = tmp_path / "clone"

        with pytest.raises(ContractSchemaError):
            write_project_contract(target, "no-such-profile")

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

    def test_gh_override_is_forbidden_before_any_step(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TPO_GH_BIN", "/opt/fake/gh")
        monkeypatch.setattr("hermes_pipeline.harness._git", lambda *a, **k: pytest.fail("git must not run"))
        monkeypatch.setattr(
            "hermes_pipeline.github_issues.check_auth", lambda *a, **k: pytest.fail("gh must not run")
        )

        with pytest.raises(HarnessPreflightError) as exc_info:
            init_sandbox(self.sandbox, tmp_path / "workspace")

        assert exc_info.value.code == "gh_override_forbidden"
        assert "TPO_GH_BIN" in exc_info.value.detail
        assert not (tmp_path / "workspace").exists()

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
    def test_empty_path_commits_even_when_clone_config_requires_signing(self, fake_gh, tmp_path, monkeypatch):
        # Global config is disabled by ``_git_env``; repo-LOCAL config still applies, so the
        # ``-c commit.gpgsign=false`` pin is what keeps the seed commit unsigned.
        bare = _make_bare_remote(tmp_path, {})
        sandbox = dataclasses.replace(self.sandbox, url=f"file://{bare}")
        self._serve_gh(fake_gh, bare)
        real_git = harness_mod._git

        def signing_clone_git(argv, **kwargs):
            result = real_git(argv, **kwargs)
            if harness_mod._git_verb(argv[1:]) == "clone":
                _real_git("config", "--local", "commit.gpgsign", "true", cwd=Path(argv[-1]))
                _real_git("config", "--local", "user.signingkey", "0000DEAD", cwd=Path(argv[-1]))
            return result

        monkeypatch.setattr(harness_mod, "_git", signing_clone_git)

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
            verb = harness_mod._git_verb(argv[1:])
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

        (init_argv,) = [c for c in calls if harness_mod._git_verb(c[1:]) == "init"]
        assert _split_git_argv(init_argv)[2] == ["init", "-q", "-b", "main", "--template="]
        pushes = [c for c in calls if harness_mod._git_verb(c[1:]) == "push"]
        assert [_split_git_argv(c)[1:] for c in pushes] == [(True, ["push", "origin", "HEAD:refs/heads/main"])]
        first = _split_git_argv(calls[0])
        assert first[1] is True and first[2][:3] == ["ls-remote", "--heads", "--tags"]
        assert all(_split_git_argv(c)[2][:2] != ["add", "-A"] for c in calls)

    def test_non_empty_path_push_argv_targets_default_branch(self, fake_gh, tmp_path, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(
            "hermes_pipeline.harness._git",
            self._seam(calls, ls_remote="abc\trefs/heads/develop\n", head="develop", tracked=".github/x\n"),
        )
        self._serve_gh(fake_gh, None, default_branch="develop")

        assert init_sandbox(self.sandbox, tmp_path / "workspace") == "seeded"

        pushes = [c for c in calls if harness_mod._git_verb(c[1:]) == "push"]
        assert [_split_git_argv(c)[1:] for c in pushes] == [(True, ["push", "origin", "HEAD:refs/heads/develop"])]
        clone = next(c for c in calls if harness_mod._git_verb(c[1:]) == "clone")
        credentialed, rest = _split_git_argv(clone)[1:]
        assert credentialed is True and rest[:3] == ["clone", "--branch", "develop"]
        adds = [_split_git_argv(c)[2] for c in calls if harness_mod._git_verb(c[1:]) == "add"]
        assert adds and all(c[1:3] == ["-f", "--"] for c in adds)
        assert ["add", "-A"] not in adds

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
    @pytest.mark.parametrize("number", [0, -1, True])
    def test_non_positive_number_rejected(self, number):
        with pytest.raises(ValueError):
            HarnessIssue(
                number=number, todo_id="TODO-1", branch="feat/harness-tok00000",
                plan_path="docs/harness/tok00000-plan.md", title="[harness tok00000] x", run_token="tok00000",
            )

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
    def test_commit_plan_commits_when_clone_config_requires_signing(self, tmp_path):
        project_dir, _ = _seeded_clone(tmp_path)
        _real_git("config", "--local", "commit.gpgsign", "true", cwd=project_dir)
        _real_git("config", "--local", "user.signingkey", "0000DEAD", cwd=project_dir)

        sha = commit_plan(project_dir, _harness_issue(42))

        assert sha == _real_git("rev-parse", "HEAD", cwd=project_dir)
        assert _real_git("log", "-1", "--format=%G?", cwd=project_dir) == "N"

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
        assert _split_git_argv(commit_argv)[2][:2] == ["-c", "commit.gpgsign=false"]
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
            "hermes_pipeline.kanban_tasks.create_prepared_todo_phases",
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
            todo_id="TODO-1", cards=cards,
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
            "hermes_pipeline.kanban_tasks.create_prepared_todo_phases",
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
            todo_id="TODO-1", cards=cards,
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
                todo_id="TODO-1", cards=[],
                monitor=monitor, detector=detector,
            )
        status.assert_not_called()

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
    def _ok(self, pr: PullRequest, provenance: bool = True) -> bool:
        return is_attributable_pr(
            pr, baseline=_PREDICATE_BASELINE, provenance_of_head=provenance
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
        assert ancestry and all(_split_git_argv(argv)[2][:2] == ["-c", "core.useReplaceRefs=false"] for argv in ancestry)

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

    @pytest.mark.parametrize("silent", ["pulls", "search"])
    def test_empty_listing_output_fails_closed(self, fake_gh, tmp_path: Path, silent):
        fake_gh.on(*_PULLS_ARGV, stdout="" if silent == "pulls" else json.dumps([[]]))
        fake_gh.on(*_SEARCH_ARGV, stdout="" if silent == "search" else _search_page())

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

    def test_clone_local_insteadof_cannot_hide_heads_from_discovery(self, fake_gh, tmp_path: Path):
        project_dir, sandbox, baseline = self._setup(tmp_path)
        plan_sha, feat_sha = _run_branch(project_dir, _issue7(), "feat/harness-abcd1234")
        # The agent plants a clone-local rewrite of the sandbox URL to an empty decoy remote.
        decoy = tmp_path / "decoy.git"
        decoy.mkdir()
        _real_git("init", "-q", "--bare", cwd=decoy)
        _real_git("config", "--local", f"url.file://{decoy}.insteadOf", sandbox.url, cwd=project_dir)
        fake_gh.on(*_ISSUE_BRANCH_PULLS_ARGV, stdout=json.dumps([[]]))
        fake_gh.on(*_SEARCH_ARGV, stdout=_search_page())

        artifacts = self._discover(project_dir, sandbox, baseline, plan_sha)

        assert artifacts.deletable_branches == (("feat/harness-abcd1234", feat_sha),)

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
        assert verify_pull_request(_artifacts(pr), default_branch="main") is pr

    def test_no_pr_is_pr_missing(self):
        with pytest.raises(PullRequestInvariantError) as exc_info:
            verify_pull_request(_artifacts(), default_branch="main")
        assert exc_info.value.code == "pr_missing"

    def test_two_prs_is_pr_ambiguous(self):
        with pytest.raises(PullRequestInvariantError) as exc_info:
            verify_pull_request(_artifacts(_pr(5), _pr(7, head_ref="feat/y")), default_branch="main")
        assert exc_info.value.code == "pr_ambiguous"
        assert exc_info.value.detail == "#5, #7"

    def test_closed_pr_is_pr_closed(self):
        pr = dataclasses.replace(_pr(5), state="CLOSED")
        with pytest.raises(PullRequestInvariantError) as exc_info:
            verify_pull_request(_artifacts(pr), default_branch="main")
        assert exc_info.value.code == "pr_closed"
        assert exc_info.value.detail == "#5"

    def test_merged_state_is_pr_merged(self):
        pr = dataclasses.replace(_pr(5), state="MERGED", merged=True)
        with pytest.raises(PullRequestInvariantError) as exc_info:
            verify_pull_request(_artifacts(pr), default_branch="main")
        assert exc_info.value.code == "pr_merged"

    def test_merged_flag_with_open_state_is_pr_merged(self):
        pr = dataclasses.replace(_pr(5), merged=True)
        with pytest.raises(PullRequestInvariantError) as exc_info:
            verify_pull_request(_artifacts(pr), default_branch="main")
        assert exc_info.value.code == "pr_merged"

    def test_wrong_base_is_pr_wrong_base(self):
        pr = dataclasses.replace(_pr(5), base_ref="develop")
        with pytest.raises(PullRequestInvariantError) as exc_info:
            verify_pull_request(_artifacts(pr), default_branch="main")
        assert exc_info.value.code == "pr_wrong_base"
        assert exc_info.value.detail == "#5 -> develop"

    def test_base_check_is_casefolded_and_follows_the_state_checks(self):
        assert verify_pull_request(_artifacts(dataclasses.replace(_pr(5), base_ref="Main")), default_branch="main").base_ref == "Main"
        closed = dataclasses.replace(_pr(5), state="CLOSED", base_ref="develop")
        with pytest.raises(PullRequestInvariantError) as exc_info:
            verify_pull_request(_artifacts(closed), default_branch="main")
        assert exc_info.value.code == "pr_closed"

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


def _remote_has_branch(sandbox: SandboxRepo, name: str) -> bool:
    bare = Path(sandbox.url.removeprefix("file://"))
    return bool(_real_git("ls-remote", "--heads", str(bare), name, cwd=bare))


def _delete_remote_branch(sandbox: SandboxRepo, name: str) -> None:
    bare = Path(sandbox.url.removeprefix("file://"))
    _real_git("update-ref", "-d", f"refs/heads/{name}", cwd=bare)


_ISSUE_CLOSE_ARGV = ("gh", "issue", "close", "7", "--repo", "acme/sandbox", "--reason", "completed")
_PR_CLOSE_ARGV = ("gh", "pr", "close", "5", "--repo", "acme/sandbox", "--comment", "Closed by tpo test cleanup.")


def _credential_argv() -> list[str]:
    """The gh credential-helper pair ``_run_git`` injects, evaluated at assertion time."""
    return ["-c", "credential.helper=", "-c", f"credential.helper=!{shlex.quote(gh_bin())} auth git-credential"]


def _split_git_argv(argv: list[str]) -> tuple[str, bool, list[str]]:
    """``(safe_directory, credentialed, rest)`` for a recorded ``_run_git`` argv."""
    assert argv[:2] == ["git", "-c"] and argv[2].startswith("safe.directory="), argv
    safe_dir = argv[2].removeprefix("safe.directory=")
    rest = argv[3:]
    credentialed = rest[:4] == _credential_argv()
    if credentialed:
        rest = rest[4:]
    return safe_dir, credentialed, rest


def _lease_delete_rest(url: str, name: str, sha: str) -> list[str]:
    return [
        "-c", f"core.hooksPath={os.devnull}",
        "push", f"--force-with-lease=refs/heads/{name}:{sha}", "--", url, f":refs/heads/{name}",
    ]


def _assert_lease_refusal(leftover: str, url: str, name: str, sha: str) -> None:
    """The leftover names the stale lease and prescribes only lease-preserving commands."""
    assert leftover.startswith(f"branch {name} ({sha[:7]}): delete refused (git_error: git push failed: ")
    assert "[rejected]" in leftover and "stale info" in leftover
    assert leftover.endswith(
        f"); inspect: git ls-remote --heads -- {url} {name}; then if safe:"
        f" git push --force-with-lease=refs/heads/{name}:{sha} -- {url} :refs/heads/{name}"
    )
    assert "--delete" not in leftover


class _GitRecorder:
    """``_git`` seam that records argv + kwargs and (optionally) a shared timeline, then runs git."""

    def __init__(self, timeline: list[str] | None = None) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.timeline = timeline

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        if self.timeline is not None:
            self.timeline.append(f"git {harness_mod._git_verb(argv[1:])}")
        return subprocess.run(argv, **kwargs)

    def by_verb(self, verb: str) -> list[tuple[list[str], dict]]:
        return [(argv, kw) for argv, kw in self.calls if harness_mod._git_verb(argv[1:]) == verb]


@pytest.mark.real_git
class TestCleanupRemote:
    def _setup(self, tmp_path: Path, fake_gh, *branches: str) -> tuple[Path, SandboxRepo, dict[str, str]]:
        project_dir, sandbox = _seeded_clone(tmp_path)
        foreign = _foreign_clone(tmp_path, sandbox)
        shas = {name: _push_new_branch(foreign, name) for name in (branches or ("feat/x",))}
        fake_gh.on(*_ISSUE_CLOSE_ARGV)
        fake_gh.on(*_PR_CLOSE_ARGV)
        return project_dir, sandbox, shas

    def _cleanup(self, project_dir: Path, sandbox: SandboxRepo, artifacts: RemoteArtifacts, staging_root: Path):
        return cleanup_remote(project_dir, sandbox, artifacts, staging_root=staging_root)

    def test_happy_path_closes_issue_and_pr_and_deletes_branch(self, fake_gh, tmp_path: Path):
        project_dir, sandbox, shas = self._setup(tmp_path, fake_gh)
        artifacts = RemoteArtifacts(
            issue_number=7, prs=(_pr(5),), deletable_branches=(("feat/x", shas["feat/x"]),), leftovers=()
        )

        result = self._cleanup(project_dir, sandbox, artifacts, tmp_path / "staging")

        assert result == (True, ())
        assert fake_gh.gh_calls() == [list(_ISSUE_CLOSE_ARGV[1:]), list(_PR_CLOSE_ARGV[1:])]
        assert not _remote_has_branch(sandbox, "feat/x")
        assert not any((tmp_path / "staging").iterdir())

    def test_operations_run_issue_then_prs_then_branches(self, fake_gh, tmp_path: Path, monkeypatch):
        project_dir, sandbox, shas = self._setup(tmp_path, fake_gh)
        timeline: list[str] = []

        def gh_handler(argv):
            timeline.append(" ".join(argv[1:3]))
            return 0, "", ""

        fake_gh.on(*_ISSUE_CLOSE_ARGV, handler=gh_handler)
        fake_gh.on(*_PR_CLOSE_ARGV, handler=gh_handler)
        monkeypatch.setattr(harness_mod, "_git", _GitRecorder(timeline))
        artifacts = RemoteArtifacts(
            issue_number=7, prs=(_pr(5),), deletable_branches=(("feat/x", shas["feat/x"]),), leftovers=()
        )

        assert self._cleanup(project_dir, sandbox, artifacts, tmp_path / "staging") == (True, ())

        assert timeline == ["git init", "issue close", "pr close", "git push"]

    def test_multiple_artifacts_are_each_handled_independently(self, fake_gh, tmp_path: Path):
        project_dir, sandbox, shas = self._setup(tmp_path, fake_gh, "feat/x", "feat/y")
        fake_gh.on("gh", "pr", "close", "6", "--repo", "acme/sandbox", "--comment", "Closed by tpo test cleanup.")
        bare = Path(sandbox.url.removeprefix("file://"))
        _advance_remote(bare, tmp_path, "feat/y")
        artifacts = RemoteArtifacts(
            issue_number=7, prs=(_pr(5), _pr(6, head_ref="feat/y")),
            deletable_branches=(("feat/x", shas["feat/x"]), ("feat/y", shas["feat/y"])), leftovers=(),
        )

        all_ok, leftovers = self._cleanup(project_dir, sandbox, artifacts, tmp_path / "staging")

        assert all_ok is False
        assert len(leftovers) == 1
        _assert_lease_refusal(leftovers[0], sandbox.url, "feat/y", shas["feat/y"])
        assert [argv[:3] for argv in fake_gh.gh_calls()] == [
            ["issue", "close", "7"], ["pr", "close", "5"], ["pr", "close", "6"],
        ]
        assert not _remote_has_branch(sandbox, "feat/x")
        assert _remote_has_branch(sandbox, "feat/y")

    def test_lease_refuses_delete_when_remote_tip_moved(self, fake_gh, tmp_path: Path):
        project_dir, sandbox, shas = self._setup(tmp_path, fake_gh)
        sha = shas["feat/x"]
        bare = Path(sandbox.url.removeprefix("file://"))
        _advance_remote(bare, tmp_path, "feat/x")
        artifacts = RemoteArtifacts(issue_number=7, prs=(), deletable_branches=(("feat/x", sha),), leftovers=())

        all_ok, leftovers = self._cleanup(project_dir, sandbox, artifacts, tmp_path / "staging")

        assert all_ok is False
        assert len(leftovers) == 1
        _assert_lease_refusal(leftovers[0], sandbox.url, "feat/x", sha)
        assert _remote_has_branch(sandbox, "feat/x")

    def test_already_deleted_branch_is_success(self, fake_gh, tmp_path: Path):
        project_dir, sandbox, shas = self._setup(tmp_path, fake_gh)
        _delete_remote_branch(sandbox, "feat/x")
        artifacts = RemoteArtifacts(
            issue_number=7, prs=(), deletable_branches=(("feat/x", shas["feat/x"]),), leftovers=()
        )

        assert self._cleanup(project_dir, sandbox, artifacts, tmp_path / "staging") == (True, ())

    def test_leftover_url_userinfo_is_scrubbed(self, fake_gh, tmp_path: Path):
        project_dir, sandbox, shas = self._setup(tmp_path, fake_gh)
        sha = shas["feat/x"]
        bare = Path(sandbox.url.removeprefix("file://"))
        _advance_remote(bare, tmp_path, "feat/x")
        # Pretend the URL carried a token; the push still fails (stale) and the leftover must not echo it.
        tainted = dataclasses.replace(sandbox, url=f"file://x-access-token:ghs_SECRET@localhost{bare}")
        artifacts = RemoteArtifacts(issue_number=7, prs=(), deletable_branches=(("feat/x", sha),), leftovers=())

        all_ok, leftovers = self._cleanup(project_dir, tainted, artifacts, tmp_path / "staging")

        assert all_ok is False
        assert "ghs_SECRET" not in leftovers[0]
        assert f"file://***@localhost{bare}" in leftovers[0]

    def test_issue_close_failure_is_a_leftover_and_other_steps_run(self, fake_gh, tmp_path: Path):
        project_dir, sandbox, shas = self._setup(tmp_path, fake_gh)
        fake_gh.on(*_ISSUE_CLOSE_ARGV, rc=1, stderr="HTTP 500")
        artifacts = RemoteArtifacts(
            issue_number=7, prs=(_pr(5),), deletable_branches=(("feat/x", shas["feat/x"]),), leftovers=()
        )

        all_ok, leftovers = self._cleanup(project_dir, sandbox, artifacts, tmp_path / "staging")

        assert all_ok is False
        assert leftovers == ("issue #7: close failed (gh_unavailable); run: gh issue close 7 --repo acme/sandbox",)
        assert list(_PR_CLOSE_ARGV[1:]) in fake_gh.gh_calls()
        assert not _remote_has_branch(sandbox, "feat/x")

    def test_pr_close_failure_is_a_leftover_and_branch_still_deleted(self, fake_gh, tmp_path: Path):
        project_dir, sandbox, shas = self._setup(tmp_path, fake_gh)
        fake_gh.on(*_PR_CLOSE_ARGV, rc=1, stderr="gh: Must have admin rights to Repository. (HTTP 403)")
        artifacts = RemoteArtifacts(
            issue_number=7, prs=(_pr(5),), deletable_branches=(("feat/x", shas["feat/x"]),), leftovers=()
        )

        all_ok, leftovers = self._cleanup(project_dir, sandbox, artifacts, tmp_path / "staging")

        assert all_ok is False
        assert leftovers == ("pr #5: close failed (gh_auth); run: gh pr close 5 --repo acme/sandbox",)
        assert not _remote_has_branch(sandbox, "feat/x")

    def test_closed_and_merged_prs_are_not_closed_again(self, fake_gh, tmp_path: Path):
        project_dir, sandbox, _ = self._setup(tmp_path, fake_gh)
        prs = (
            dataclasses.replace(_pr(5), state="CLOSED"),
            dataclasses.replace(_pr(6), state="MERGED", merged=True),
            dataclasses.replace(_pr(8), merged=True),
        )
        artifacts = RemoteArtifacts(issue_number=7, prs=prs, deletable_branches=(), leftovers=())

        result = self._cleanup(project_dir, sandbox, artifacts, tmp_path / "staging")

        assert result == (True, ())
        assert not any(argv[:2] == ["pr", "close"] for argv in fake_gh.gh_calls())

    def test_discovery_leftovers_pass_through_without_flipping_all_ok(self, fake_gh, tmp_path: Path):
        project_dir, sandbox, _ = self._setup(tmp_path, fake_gh)
        artifacts = RemoteArtifacts(
            issue_number=7, prs=(), deletable_branches=(),
            leftovers=("pr #9: not attributable", "branch ops (abc1234): no run provenance"),
        )

        result = self._cleanup(project_dir, sandbox, artifacts, tmp_path / "staging")

        assert result == (True, ("branch ops (abc1234): no run provenance", "pr #9: not attributable"))

    def test_staging_root_failure_raises_before_any_remote_change(self, fake_gh, tmp_path: Path):
        project_dir, sandbox, shas = self._setup(tmp_path, fake_gh)
        staging_root = tmp_path / "staging"
        staging_root.write_text("not a directory\n")
        artifacts = RemoteArtifacts(
            issue_number=7, prs=(_pr(5),), deletable_branches=(("feat/x", shas["feat/x"]),), leftovers=()
        )

        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            self._cleanup(project_dir, sandbox, artifacts, staging_root)

        assert exc_info.value.code == "cleanup_staging_failed"
        assert fake_gh.gh_calls() == []
        assert _remote_has_branch(sandbox, "feat/x")

    def test_staging_root_inside_clone_is_rejected_before_any_remote_change(self, fake_gh, tmp_path: Path):
        project_dir, sandbox, shas = self._setup(tmp_path, fake_gh)
        artifacts = RemoteArtifacts(
            issue_number=7, prs=(_pr(5),), deletable_branches=(("feat/x", shas["feat/x"]),), leftovers=()
        )

        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            self._cleanup(project_dir, sandbox, artifacts, project_dir / "staging")

        assert exc_info.value.code == "cleanup_staging_failed"
        assert fake_gh.gh_calls() == []
        assert _remote_has_branch(sandbox, "feat/x")

    def test_delete_push_argv_and_cwd_are_pinned(self, fake_gh, tmp_path: Path, monkeypatch):
        project_dir, sandbox, shas = self._setup(tmp_path, fake_gh)
        sha = shas["feat/x"]
        recorder = _GitRecorder()
        monkeypatch.setattr(harness_mod, "_git", recorder)
        artifacts = RemoteArtifacts(issue_number=7, prs=(), deletable_branches=(("feat/x", sha),), leftovers=())

        assert self._cleanup(project_dir, sandbox, artifacts, tmp_path / "staging") == (True, ())

        ((push_argv, push_kwargs),) = recorder.by_verb("push")
        cwd = Path(push_kwargs["cwd"])
        assert _split_git_argv(push_argv) == (str(cwd.resolve()), True, _lease_delete_rest(sandbox.url, "feat/x", sha))
        assert cwd.parent == tmp_path / "staging" and cwd.name.startswith("prov-")
        assert cwd.resolve() != project_dir.resolve()

    def test_non_lease_push_failure_is_reported_even_when_branch_is_gone(self, fake_gh, tmp_path: Path, monkeypatch):
        project_dir, sandbox, shas = self._setup(tmp_path, fake_gh)
        _delete_remote_branch(sandbox, "feat/x")
        real_git = harness_mod._git

        def unreachable_push(argv, **kwargs):
            if harness_mod._git_verb(argv[1:]) == "push":
                return subprocess.CompletedProcess(argv, 128, "", "fatal: unable to access 'x': Could not resolve host")
            return real_git(argv, **kwargs)

        monkeypatch.setattr(harness_mod, "_git", unreachable_push)
        artifacts = RemoteArtifacts(
            issue_number=7, prs=(), deletable_branches=(("feat/x", shas["feat/x"]),), leftovers=()
        )

        all_ok, leftovers = self._cleanup(project_dir, sandbox, artifacts, tmp_path / "staging")

        assert all_ok is False
        assert len(leftovers) == 1
        assert leftovers[0].startswith(
            f"branch feat/x ({shas['feat/x'][:7]}): delete refused (git_error: git push failed: fatal: unable to access"
        )

    def test_push_env_ignores_ambient_git_config(self, fake_gh, tmp_path: Path, monkeypatch):
        project_dir, sandbox, shas = self._setup(tmp_path, fake_gh)
        monkeypatch.setenv("GIT_CONFIG_COUNT", "0")
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.hooksPath=/nope'")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "operator.gitconfig"))
        monkeypatch.setenv("GIT_TEMPLATE_DIR", str(tmp_path / "tpl"))
        monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -o ProxyCommand=evil")
        monkeypatch.setenv("GIT_SSH", "/tmp/evil-ssh")
        monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
        recorder = _GitRecorder()
        monkeypatch.setattr(harness_mod, "_git", recorder)
        artifacts = RemoteArtifacts(
            issue_number=7, prs=(), deletable_branches=(("feat/x", shas["feat/x"]),), leftovers=()
        )

        self._cleanup(project_dir, sandbox, artifacts, tmp_path / "staging")

        ((_, push_kwargs),) = recorder.by_verb("push")
        env = push_kwargs["env"]
        assert {key for key in env if key.startswith("GIT_CONFIG")} == {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"}
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["LC_ALL"] == "C"
        assert not {"GIT_TEMPLATE_DIR", "GIT_SSH_COMMAND", "GIT_SSH"} & env.keys()
        assert not _remote_has_branch(sandbox, "feat/x")

    def test_ambient_insteadof_cannot_redirect_delete_to_a_decoy(self, fake_gh, tmp_path: Path, monkeypatch):
        project_dir, sandbox, shas = self._setup(tmp_path, fake_gh)
        bare = Path(sandbox.url.removeprefix("file://"))
        decoy = tmp_path / "decoy.git"
        _real_git("clone", "-q", "--bare", str(bare), str(decoy), cwd=tmp_path)
        decoy_url = f"file://{decoy}"
        assert _real_git("ls-remote", "--heads", decoy_url, "feat/x", cwd=tmp_path)
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{decoy_url}.insteadOf")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", sandbox.url)
        artifacts = RemoteArtifacts(
            issue_number=7, prs=(), deletable_branches=(("feat/x", shas["feat/x"]),), leftovers=()
        )

        assert self._cleanup(project_dir, sandbox, artifacts, tmp_path / "staging") == (True, ())

        assert not _remote_has_branch(sandbox, "feat/x")
        assert _real_git("ls-remote", "--heads", decoy_url, "feat/x", cwd=tmp_path).endswith("refs/heads/feat/x")

    def test_ambient_template_hooks_do_not_run_in_staging_repo(self, fake_gh, tmp_path: Path, monkeypatch):
        project_dir, sandbox, shas = self._setup(tmp_path, fake_gh)
        sentinel = tmp_path / "hook-ran"
        hooks = tmp_path / "tpl" / "hooks"
        hooks.mkdir(parents=True)
        hook = hooks / "pre-push"
        hook.write_text(f"#!/bin/sh\ntouch {sentinel}\nexit 1\n")
        hook.chmod(0o755)
        monkeypatch.setenv("GIT_TEMPLATE_DIR", str(tmp_path / "tpl"))
        artifacts = RemoteArtifacts(
            issue_number=7, prs=(), deletable_branches=(("feat/x", shas["feat/x"]),), leftovers=()
        )

        assert self._cleanup(project_dir, sandbox, artifacts, tmp_path / "staging") == (True, ())

        assert not sentinel.exists()
        assert not _remote_has_branch(sandbox, "feat/x")


class TestRemoteArtifactsValidation:
    def test_valid_shape_is_accepted(self):
        RemoteArtifacts(issue_number=7, prs=(), deletable_branches=(("feat/x", "a" * 40),), leftovers=())

    @pytest.mark.parametrize("number", [0, -1, True, "7"])
    def test_issue_number_must_be_a_positive_int(self, number):
        with pytest.raises(ValueError):
            RemoteArtifacts(issue_number=number, prs=(), deletable_branches=(), leftovers=())

    @pytest.mark.parametrize("sha", ["abc", "A" * 40, "g" * 40, " " + "a" * 39])
    def test_sha_must_be_forty_hex(self, sha):
        with pytest.raises(ValueError):
            RemoteArtifacts(issue_number=7, prs=(), deletable_branches=(("feat/x", sha),), leftovers=())

    @pytest.mark.parametrize(
        "name",
        ["master", "Main", "MASTER", "feat x", "a:b", "a*b", "a..b", "-x", "refs/heads/x", "x/refs/y", "",
         "--force-with-lease=refs/heads/main:0000"],
    )
    def test_branch_name_is_syntactically_screened(self, name):
        with pytest.raises(ValueError):
            RemoteArtifacts(issue_number=7, prs=(), deletable_branches=((name, "a" * 40),), leftovers=())


class TestRunGitHardening:
    @pytest.mark.real_git
    def test_network_verbs_get_gh_credential_helper_and_a_config_reset(self, tmp_path: Path, monkeypatch):
        recorder = _GitRecorder()
        monkeypatch.setattr(harness_mod, "_git", recorder)
        project_dir, _ = _seeded_clone(tmp_path)

        ((clone_argv, clone_kwargs),) = recorder.by_verb("clone")
        safe_dir, credentialed, rest = _split_git_argv(clone_argv)
        assert (safe_dir, credentialed, rest[0]) == (str(Path(clone_kwargs["cwd"]).resolve()), True, "clone")
        ((config_argv, config_kwargs), *_) = recorder.by_verb("config")
        safe_dir, credentialed, rest = _split_git_argv(config_argv)
        assert (safe_dir, credentialed, rest[0]) == (str(Path(config_kwargs["cwd"]).resolve()), False, "config")
        assert (project_dir / "pyproject.toml").exists()

    @pytest.mark.real_git
    def test_credential_helper_quotes_gh_bin_with_spaces(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("TPO_GH_BIN", "/opt/g h/gh")
        recorder = _GitRecorder()
        monkeypatch.setattr(harness_mod, "_git", recorder)
        _seeded_clone(tmp_path)

        ((clone_argv, _),) = recorder.by_verb("clone")
        assert "credential.helper=!'/opt/g h/gh' auth git-credential" in clone_argv
        assert _split_git_argv(clone_argv)[1] is True

    def test_git_verb_skips_injected_config_pairs(self):
        argv = ["-c", "safe.directory=/x", *_credential_argv(), "-c", "core.hooksPath=/dev/null", "push", "x"]
        assert harness_mod._git_verb(argv) == "push"

    def test_timeout_is_a_git_error(self, tmp_path: Path, monkeypatch):
        def hanging_git(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 300.0))

        monkeypatch.setattr(harness_mod, "_git", hanging_git)
        with pytest.raises(HarnessPreflightError) as exc_info:
            harness_mod._run_git(["ls-remote", "--heads", "--", "file:///nowhere"], cwd=tmp_path, timeout=1.5)
        assert exc_info.value.code == "git_error"
        assert exc_info.value.detail == "git ls-remote timed out after 1.5s"

    @pytest.mark.real_git
    def test_safe_directory_is_the_resolved_cwd(self, tmp_path: Path, monkeypatch):
        target = tmp_path / "real"
        target.mkdir()
        link = tmp_path / "link"
        link.symlink_to(target, target_is_directory=True)
        recorder = _GitRecorder()
        monkeypatch.setattr(harness_mod, "_git", recorder)

        harness_mod._run_git(["init", "-q", "--bare"], cwd=link)

        ((argv, _),) = recorder.by_verb("init")
        assert _split_git_argv(argv)[0] == str(target.resolve())

    @pytest.mark.real_git
    def test_seam_receives_timeout(self, tmp_path: Path, monkeypatch):
        recorder = _GitRecorder()
        monkeypatch.setattr(harness_mod, "_git", recorder)
        harness_mod._run_git(["init", "-q", "--bare"], cwd=tmp_path)
        ((_, kwargs),) = recorder.by_verb("init")
        assert kwargs["timeout"] == 300.0

    @pytest.mark.real_git
    def test_provenance_dir_uses_empty_template_and_screens_hooks_and_config(self, tmp_path: Path, monkeypatch):
        hooks = tmp_path / "tpl" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "pre-push").write_text("#!/bin/sh\nexit 1\n")
        (hooks / "pre-push").chmod(0o755)
        monkeypatch.setenv("GIT_TEMPLATE_DIR", str(tmp_path / "tpl"))
        recorder = _GitRecorder()
        monkeypatch.setattr(harness_mod, "_git", recorder)

        work = harness_mod._ensure_provenance_dir(tmp_path / "prov")

        ((init_argv, _),) = recorder.by_verb("init")
        assert "--template=" in init_argv
        assert not (work / "hooks" / "pre-push").exists()
        config = (work / "config").read_text()
        assert not any(token in config.lower() for token in ("include", "hookspath", "insteadof"))

    def test_provenance_dir_rejects_planted_hook_or_config(self, tmp_path: Path, monkeypatch):
        def planting_git(argv, **kwargs):
            work = Path(kwargs["cwd"])
            (work / "hooks").mkdir(parents=True)
            hook = work / "hooks" / "pre-push"
            hook.write_text("#!/bin/sh\n")
            hook.chmod(0o755)
            (work / "config").write_text("[core]\n\tbare = true\n[url \"file:///decoy\"]\n\tinsteadOf = x\n")
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(harness_mod, "_git", planting_git)
        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            harness_mod._ensure_provenance_dir(tmp_path / "prov")
        assert exc_info.value.code == "pr_discovery_incomplete"
        assert "hooks/pre-push" in exc_info.value.detail
        assert "config:insteadof" in exc_info.value.detail
        assert list((tmp_path / "prov").iterdir()) == []

    def test_provenance_root_mkdir_failure_is_pr_discovery_incomplete(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(harness_mod, "_git", lambda argv, **kw: pytest.fail("git must not run"))
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory\n")

        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            harness_mod._ensure_provenance_dir(blocker / "prov")

        assert exc_info.value.code == "pr_discovery_incomplete"
        assert "blocker" in exc_info.value.detail

    def test_provenance_check_wraps_os_errors(self, tmp_path: Path, monkeypatch):
        def failing(_root):
            raise OSError("disk full")

        monkeypatch.setattr(harness_mod, "_ensure_provenance_dir", failing)

        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            harness_mod.branch_has_run_provenance(
                tmp_path / "clone", _SANDBOX, name="feat/x", tip_sha="a" * 40, plan_sha="b" * 40,
                default_branch="main", provenance_dir=tmp_path / "prov",
            )

        assert exc_info.value.code == "pr_discovery_incomplete"
        assert exc_info.value.detail.startswith("branch feat/x: ")
        assert "disk full" in exc_info.value.detail

    @pytest.mark.parametrize("line", ["[include]\n\tpath = /x\n", "[core]\n\thooksPath = /x\n"])
    def test_provenance_dir_rejects_include_and_hookspath_config(self, tmp_path: Path, monkeypatch, line):
        def planting_git(argv, **kwargs):
            (Path(kwargs["cwd"]) / "config").write_text("[core]\n\tbare = true\n" + line)
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(harness_mod, "_git", planting_git)
        with pytest.raises(HarnessRemoteCleanupError) as exc_info:
            harness_mod._ensure_provenance_dir(tmp_path / "prov")
        assert exc_info.value.detail.startswith("fresh provenance dir carries config:")


class TestListRunIssuesNumberScreening:
    def test_bool_and_non_positive_numbers_are_skipped(self, fake_gh, tmp_path: Path):
        page = [
            {"number": True, "title": _HARNESS_TITLE},
            {"number": 0, "title": _HARNESS_TITLE},
            {"number": -3, "title": _HARNESS_TITLE},
            {"number": 12, "title": _HARNESS_TITLE},
        ]
        fake_gh.on(*_LIST_ARGV, stdout=json.dumps([page]))
        sandbox = SandboxRepo(repo="acme/sandbox", slug="sandbox", url="https://github.com/acme/sandbox.git")

        assert harness_mod._list_run_issues(tmp_path, sandbox, run_token=_RUN_TOKEN, baseline=_baseline()) == [12]



def _kanban_task(tick_id: str, phase_key: str, status: str) -> dict[str, object]:
    return {
        "id": f"task-{phase_key}",
        "status": status,
        "body": json.dumps({"tick_id": tick_id, "phase_key": phase_key, "todo_id": "TODO-7"}) + "\nbody",
    }


class TestShutdownRun:
    """Fail-closed shutdown: destructive remote cleanup only after proven kanban quiescence (R-11.1)."""

    _SANDBOX = SandboxRepo(repo="acme/sandbox", slug="sandbox", url="https://github.com/acme/sandbox.git")
    _KEYS = ("impl", "review")

    def _run(self, tmp_path: Path, tick_id: str | None = "tick-1", **overrides):
        clock = {"t": 0.0}
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock["t"] += seconds

        kwargs = dict(
            issue=_harness_issue(7),
            baseline=_baseline(),
            plan_sha="a" * 40,
            tick_id=tick_id,
            expected_phase_keys=self._KEYS,
            provenance_dir=tmp_path / "prov",
            staging_root=tmp_path / "staging",
            quiescence_timeout=30.0,
            poll_interval=5.0,
            sleep=fake_sleep,
            now=lambda: clock["t"],
        )
        kwargs.update(overrides)
        report = shutdown_run(tmp_path / "clone", self._SANDBOX, **kwargs)
        return report, sleeps

    @pytest.fixture
    def stubs(self):
        artifacts = RemoteArtifacts(issue_number=7, prs=(), deletable_branches=(), leftovers=())
        terminal = [_kanban_task("tick-1", "impl", "archived"), _kanban_task("tick-1", "review", "done")]
        with (
            patch.object(harness_mod, "_cancel_registered_tasks", return_value=True) as cancel,
            patch("hermes_pipeline.kanban_tasks._list_task_snapshot", return_value=terminal) as snapshot,
            patch.object(harness_mod, "discover_remote_artifacts", return_value=artifacts) as discover,
            patch.object(harness_mod, "cleanup_remote", return_value=(True, ())) as cleanup,
            patch("hermes_pipeline.github_issues.close_issue") as close,
        ):
            yield MagicMock(cancel=cancel, snapshot=snapshot, discover=discover, cleanup=cleanup, close=close)

    def test_unknown_tick_with_possible_workers_closes_issue_and_reports_not_quiescent(
        self, stubs, tmp_path: Path, caplog
    ):
        with caplog.at_level(logging.WARNING, logger="hermes_pipeline.harness"):
            report, sleeps = self._run(tmp_path, tick_id=None, assume_workers_may_exist=True)

        assert report.tick_id is None
        assert report.kanban_quiescent is False
        assert report.remote_all_ok is False
        assert report.branch_deletion_skipped is True
        assert any("tick id unknown" in leftover and "hermes kanban list --tenant sandbox" in leftover
                   for leftover in report.leftovers)
        stubs.close.assert_called_once_with(tmp_path / "clone", 7, repo="acme/sandbox")
        stubs.cancel.assert_not_called()
        stubs.discover.assert_not_called()
        stubs.cleanup.assert_not_called()
        assert sleeps == []

    def test_unknown_tick_with_possible_workers_and_keep_remote_touches_nothing(self, stubs, tmp_path: Path):
        report, _sleeps = self._run(tmp_path, tick_id=None, assume_workers_may_exist=True, keep_remote=True)

        assert report.kanban_quiescent is False
        assert report.remote_all_ok is False
        stubs.close.assert_not_called()
        stubs.cancel.assert_not_called()
        assert any("kept remote artifacts" in leftover for leftover in report.leftovers)

    def test_no_tick_id_closes_issue_only(self, stubs, tmp_path: Path, caplog):
        with caplog.at_level(logging.INFO, logger="hermes_pipeline.harness"):
            report, sleeps = self._run(tmp_path, tick_id=None)

        assert report == ShutdownReport(
            tick_id=None, kanban_quiescent=True, remote_all_ok=True, leftovers=(), branch_deletion_skipped=True
        )
        stubs.close.assert_called_once_with(tmp_path / "clone", 7, repo="acme/sandbox")
        stubs.cancel.assert_not_called()
        stubs.snapshot.assert_not_called()
        stubs.discover.assert_not_called()
        stubs.cleanup.assert_not_called()
        assert sleeps == []
        assert any("no tick registered" in r.getMessage() for r in caplog.records)

    def test_empty_snapshot_forever_is_not_quiescent(self, stubs, tmp_path: Path, caplog):
        stubs.snapshot.return_value = []

        with caplog.at_level(logging.WARNING, logger="hermes_pipeline.harness"):
            report, sleeps = self._run(tmp_path)

        assert any(
            r.levelno == logging.WARNING and "cleanup skipped" in r.getMessage() for r in caplog.records
        )

        assert report.kanban_quiescent is False
        assert report.remote_all_ok is False
        assert report.branch_deletion_skipped is True
        assert sleeps == [5.0] * 6
        assert stubs.snapshot.call_count == 7
        assert (
            "kanban not quiescent for tick tick-1 (issue #7 in acme/sandbox, run tok00000);"
            " branch/PR cleanup skipped; inspect: hermes kanban list --tenant sandbox --archived;"
            " gh pr list --repo acme/sandbox; git ls-remote --heads -- https://github.com/acme/sandbox.git"
        ) in report.leftovers
        stubs.discover.assert_not_called()
        stubs.cleanup.assert_not_called()
        stubs.close.assert_called_once_with(tmp_path / "clone", 7, repo="acme/sandbox")

    def test_terminal_including_archived_after_two_polls_then_cleanup(self, stubs, tmp_path: Path):
        running = [_kanban_task("tick-1", "impl", "running"), _kanban_task("tick-1", "review", "backlog")]
        stubs.snapshot.side_effect = [running, running, stubs.snapshot.return_value]

        report, sleeps = self._run(tmp_path)

        assert report == ShutdownReport(
            tick_id="tick-1", kanban_quiescent=True, remote_all_ok=True, leftovers=(), branch_deletion_skipped=False
        )
        assert sleeps == [5.0, 5.0]
        stubs.cancel.assert_called_once_with(project_slug="sandbox", tick_id="tick-1", project_dir=tmp_path / "clone")
        stubs.snapshot.assert_called_with("sandbox")
        stubs.discover.assert_called_once_with(
            tmp_path / "clone", self._SANDBOX, issue=_harness_issue(7), baseline=_baseline(),
            plan_sha="a" * 40, provenance_dir=tmp_path / "prov",
        )
        stubs.cleanup.assert_called_once_with(
            tmp_path / "clone", self._SANDBOX, stubs.discover.return_value,
            staging_root=tmp_path / "staging", log=harness_mod.log,
        )

    def test_other_ticks_cards_are_ignored(self, stubs, tmp_path: Path):
        stubs.snapshot.return_value = [
            *stubs.snapshot.return_value, _kanban_task("tick-0", "impl", "running"),
        ]

        report, _ = self._run(tmp_path)

        assert report.kanban_quiescent is True

    def test_missing_expected_key_is_not_quiescent(self, stubs, tmp_path: Path):
        stubs.snapshot.return_value = [_kanban_task("tick-1", "impl", "archived")]

        report, _ = self._run(tmp_path)

        assert report.kanban_quiescent is False
        stubs.cleanup.assert_not_called()
        stubs.close.assert_called_once()

    def test_unknown_expected_keys_requires_only_nonempty_terminal(self, stubs, tmp_path: Path):
        stubs.snapshot.return_value = [_kanban_task("tick-1", "impl", "archived")]

        report, _ = self._run(tmp_path, expected_phase_keys=None)

        assert report.kanban_quiescent is True

    def test_cancel_not_confirmed_skips_polling(self, stubs, tmp_path: Path):
        stubs.cancel.return_value = False

        report, sleeps = self._run(tmp_path)

        assert report.kanban_quiescent is False
        assert report.branch_deletion_skipped is True
        assert sleeps == []
        stubs.snapshot.assert_not_called()
        stubs.cleanup.assert_not_called()
        stubs.close.assert_called_once()

    def test_snapshot_query_error_keeps_polling_to_deadline(self, stubs, tmp_path: Path, caplog):
        stubs.snapshot.side_effect = RuntimeError("hermes down")

        with caplog.at_level(logging.WARNING, logger="hermes_pipeline.harness"):
            report, sleeps = self._run(tmp_path)

        assert report.kanban_quiescent is False
        assert sum(sleeps) >= 30.0
        assert stubs.snapshot.call_count == len(sleeps) + 1
        assert any("hermes down" in r.getMessage() for r in caplog.records)
        stubs.cleanup.assert_not_called()

    def test_keep_remote_cancels_kanban_but_skips_remote_ops(self, stubs, tmp_path: Path):
        report, _ = self._run(tmp_path, keep_remote=True)

        assert report.kanban_quiescent is True
        assert report.remote_all_ok is True
        assert report.branch_deletion_skipped is True
        assert report.leftovers == ("kept remote artifacts for issue #7 in acme/sandbox (run tok00000)",)
        stubs.cancel.assert_called_once()
        stubs.discover.assert_not_called()
        stubs.cleanup.assert_not_called()
        stubs.close.assert_not_called()

    def test_keep_remote_with_no_tick_id_performs_no_remote_op(self, stubs, tmp_path: Path):
        report, _ = self._run(tmp_path, tick_id=None, keep_remote=True)

        assert report == ShutdownReport(
            tick_id=None, kanban_quiescent=True, remote_all_ok=True,
            leftovers=("kept remote artifacts for issue #7 in acme/sandbox (run tok00000)",),
            branch_deletion_skipped=True,
        )
        stubs.close.assert_not_called()
        stubs.cancel.assert_not_called()
        stubs.discover.assert_not_called()
        stubs.cleanup.assert_not_called()

    def test_keep_remote_not_quiescent_reports_remote_not_ok(self, stubs, tmp_path: Path):
        stubs.cancel.return_value = False

        report, _ = self._run(tmp_path, keep_remote=True)

        assert report.kanban_quiescent is False
        assert report.remote_all_ok is False
        assert "kept remote artifacts for issue #7 in acme/sandbox (run tok00000)" in report.leftovers
        stubs.cancel.assert_called_once()
        stubs.close.assert_not_called()

    @pytest.mark.parametrize(
        ("exc", "leftover"),
        [
            (HarnessRemoteCleanupError("pr_discovery_incomplete", "x"), "pr_discovery_incomplete: x"),
            (ValueError("bad sha"), "ValueError: bad sha"),
            (OSError("disk"), "OSError: disk"),
        ],
    )
    def test_discovery_error_closes_issue_once_and_skips_cleanup(self, stubs, tmp_path: Path, exc, leftover, caplog):
        stubs.discover.side_effect = exc

        with caplog.at_level(logging.WARNING, logger="hermes_pipeline.harness"):
            report, _ = self._run(tmp_path)

        assert report.kanban_quiescent is True
        assert report.remote_all_ok is False
        assert report.branch_deletion_skipped is True
        assert (
            f"{leftover}; branch/PR cleanup skipped; inspect: gh pr list --repo acme/sandbox;"
            " git ls-remote --heads -- https://github.com/acme/sandbox.git"
        ) in report.leftovers
        assert any(r.levelno == logging.WARNING and leftover in r.getMessage() for r in caplog.records)
        stubs.cleanup.assert_not_called()
        stubs.close.assert_called_once_with(tmp_path / "clone", 7, repo="acme/sandbox")

    def test_issue_close_failure_on_fallback_path_is_a_leftover(self, stubs, tmp_path: Path):
        stubs.discover.side_effect = HarnessRemoteCleanupError("pr_discovery_incomplete", "x")
        stubs.close.side_effect = GitHubIssuesError("gh_failed", "boom")

        report, _ = self._run(tmp_path)

        assert report.remote_all_ok is False
        assert "issue #7: close failed (gh_failed); run: gh issue close 7 --repo acme/sandbox" in report.leftovers

    def test_issue_close_value_error_is_invalid_leftover(self, stubs, tmp_path: Path):
        stubs.discover.side_effect = HarnessRemoteCleanupError("pr_discovery_incomplete", "x")
        stubs.close.side_effect = ValueError("bad number")

        report, _ = self._run(tmp_path)

        assert "issue #7: close failed (invalid); run: gh issue close 7 --repo acme/sandbox" in report.leftovers

    def test_late_pr_leftovers_pass_through(self, stubs, tmp_path: Path):
        stubs.cleanup.return_value = (False, ("pr #9: close failed (gh_failed); run: gh pr close 9 --repo acme/sandbox",))

        report, _ = self._run(tmp_path)

        assert report.remote_all_ok is False
        assert report.branch_deletion_skipped is False
        assert report.leftovers == ("pr #9: close failed (gh_failed); run: gh pr close 9 --repo acme/sandbox",)

    @pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
    def test_interrupt_during_wait_is_logged_then_reraised(self, stubs, tmp_path: Path, caplog, interrupt):
        stubs.snapshot.return_value = []

        def interrupting_sleep(_seconds: float) -> None:
            raise interrupt

        with caplog.at_level(logging.ERROR, logger="hermes_pipeline.harness"), pytest.raises(interrupt):
            self._run(tmp_path, sleep=interrupting_sleep)

        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any("#7" in m and "acme/sandbox" in m and "tok00000" in m for m in errors)
        stubs.cleanup.assert_not_called()

    @pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
    def test_interrupt_during_cleanup_is_logged_then_reraised(self, stubs, tmp_path: Path, caplog, interrupt):
        stubs.cleanup.side_effect = interrupt

        with caplog.at_level(logging.ERROR, logger="hermes_pipeline.harness"), pytest.raises(interrupt):
            self._run(tmp_path)

        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any("#7" in m and "acme/sandbox" in m and "tok00000" in m for m in errors)
        stubs.close.assert_not_called()

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"poll_interval": 0.0}, {"poll_interval": -1.0}, {"quiescence_timeout": -1.0},
            {"poll_interval": float("inf")}, {"poll_interval": float("nan")},
            {"quiescence_timeout": float("inf")}, {"quiescence_timeout": float("nan")},
        ],
    )
    def test_invalid_timing_rejected(self, stubs, tmp_path: Path, kwargs):
        with pytest.raises(ValueError):
            self._run(tmp_path, **kwargs)
        stubs.cancel.assert_not_called()

    def test_sleep_is_clamped_to_remaining_deadline(self, stubs, tmp_path: Path):
        stubs.snapshot.return_value = []

        report, sleeps = self._run(tmp_path, poll_interval=60.0, quiescence_timeout=30.0)

        assert report.kanban_quiescent is False
        assert sleeps == [30.0]

    @pytest.mark.parametrize("live_first", [True, False])
    def test_duplicate_phase_key_folds_to_worst_status(self, stubs, tmp_path: Path, live_first):
        live = {**_kanban_task("tick-1", "impl", "running"), "id": "task-impl-dup"}
        archived = _kanban_task("tick-1", "impl", "archived")
        review = _kanban_task("tick-1", "review", "done")
        stubs.snapshot.return_value = [live, archived, review] if live_first else [archived, live, review]

        report, _ = self._run(tmp_path)

        assert report.kanban_quiescent is False
        stubs.cleanup.assert_not_called()

    def test_archived_status_map_reads_archived_snapshot(self):
        tasks = [
            _kanban_task("tick-1", "impl", "archived"), _kanban_task("tick-1", "impl", "running"),
            _kanban_task("tick-1", "review", "done"), _kanban_task("tick-1", "review", "archived"),
            _kanban_task("tick-9", "impl", "running"), {"id": "x", "status": "running", "body": "not json"},
            {**_kanban_task("tick-1", "gate", "done"), "status": None},
            {k: v for k, v in _kanban_task("tick-1", "ship", "done").items() if k != "status"},
        ]
        with patch("hermes_pipeline.kanban_tasks._list_task_snapshot", return_value=tasks) as snap:
            assert harness_mod._archived_status_map("sandbox", "tick-1") == {
                "impl": "running", "review": "done", "gate": "unknown", "ship": "unknown",
            }
        snap.assert_called_once_with("sandbox")
        with patch("hermes_pipeline.kanban_tasks._list_task_snapshot", return_value=None):
            assert harness_mod._archived_status_map("sandbox", "tick-1") is None

    def test_card_without_status_is_not_quiescent(self, stubs, tmp_path: Path):
        stubs.snapshot.return_value = [
            _kanban_task("tick-1", "impl", "archived"),
            {k: v for k, v in _kanban_task("tick-1", "review", "done").items() if k != "status"},
        ]

        report, _ = self._run(tmp_path)

        assert report.kanban_quiescent is False
        stubs.cleanup.assert_not_called()

    def test_long_cleanup_error_reason_is_bounded(self, stubs, tmp_path: Path):
        stubs.discover.side_effect = ValueError("x" * 5000)

        report, _ = self._run(tmp_path)

        reason = report.leftovers[0]
        assert reason.startswith("ValueError: xxx")
        assert reason.endswith("git ls-remote --heads -- https://github.com/acme/sandbox.git")
        assert "x" * harness_mod._ERROR_MESSAGE_MAX not in reason

    def test_stalled_clock_cannot_spin_forever(self, stubs, tmp_path: Path):
        stubs.snapshot.return_value = []
        sleeps: list[float] = []

        report = shutdown_run(
            tmp_path / "clone", self._SANDBOX, issue=_harness_issue(7), baseline=_baseline(), plan_sha="a" * 40,
            tick_id="tick-1", expected_phase_keys=self._KEYS, provenance_dir=tmp_path / "prov",
            staging_root=tmp_path / "staging", quiescence_timeout=30.0, poll_interval=5.0,
            sleep=sleeps.append, now=lambda: 0.0,
        )

        assert report.kanban_quiescent is False
        assert len(sleeps) == 7  # ceil(30 / 5) + 1 iterations, one sleep each
        assert stubs.snapshot.call_count == 7

    def test_real_snapshot_reader_failing_subprocess_is_not_quiescent(self, tmp_path: Path):
        artifacts = RemoteArtifacts(issue_number=7, prs=(), deletable_branches=(), leftovers=())
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        with (
            patch.object(harness_mod, "_cancel_registered_tasks", return_value=True),
            patch("hermes_pipeline.kanban_tasks.subprocess.run", return_value=failed) as run,
            patch.object(harness_mod, "discover_remote_artifacts", return_value=artifacts),
            patch.object(harness_mod, "cleanup_remote", return_value=(True, ())) as cleanup,
            patch("hermes_pipeline.github_issues.close_issue"),
        ):
            report, _ = self._run(tmp_path)

        assert report.kanban_quiescent is False
        assert run.call_args.args[0] == ["hermes", "kanban", "list", "--tenant", "sandbox", "--archived", "--json"]
        cleanup.assert_not_called()

    def test_real_snapshot_reader_success_is_quiescent(self, tmp_path: Path):
        artifacts = RemoteArtifacts(issue_number=7, prs=(), deletable_branches=(), leftovers=())
        stdout = json.dumps([_kanban_task("tick-1", "impl", "archived"), _kanban_task("tick-1", "review", "done")])
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")
        with (
            patch.object(harness_mod, "_cancel_registered_tasks", return_value=True),
            patch("hermes_pipeline.kanban_tasks.subprocess.run", return_value=ok) as run,
            patch.object(harness_mod, "discover_remote_artifacts", return_value=artifacts),
            patch.object(harness_mod, "cleanup_remote", return_value=(True, ())) as cleanup,
            patch("hermes_pipeline.github_issues.close_issue"),
        ):
            report, sleeps = self._run(tmp_path)

        assert report.kanban_quiescent is True
        assert report.branch_deletion_skipped is False
        assert sleeps == []
        assert run.call_args.args[0] == ["hermes", "kanban", "list", "--tenant", "sandbox", "--archived", "--json"]
        cleanup.assert_called_once()

    def test_zero_timeout_checks_exactly_once(self, stubs, tmp_path: Path):
        stubs.snapshot.return_value = []

        report, sleeps = self._run(tmp_path, quiescence_timeout=0.0)

        assert report.kanban_quiescent is False
        assert sleeps == []
        assert stubs.snapshot.call_count == 1


class TestWaitForIssueVisible:
    """wait_for_issue_visible: the run's issue must appear in the ready listing before the tick.

    GitHub's label-filtered issue listing lags a fresh create by seconds (observed live:
    the tick saw an empty listing two seconds after ``gh issue create``), so the barrier
    polls the same listing the production tick uses until exactly our issue is ready.
    """

    def _clock(self):
        state = {"t": 0.0, "sleeps": []}

        def sleep(seconds):
            state["sleeps"].append(seconds)
            state["t"] += seconds

        return state, sleep, lambda: state["t"]

    def test_returns_once_the_issue_is_listed(self, monkeypatch, tmp_path):
        listings = iter([(), (), (7,)])
        calls = []

        def ready(project_dir, sandbox):
            calls.append((project_dir, sandbox))
            return next(listings)

        monkeypatch.setattr(harness_mod, "ready_issue_numbers", ready)
        state, sleep, now = self._clock()

        wait_for_issue_visible(tmp_path, SANDBOX, issue_number=7, timeout=60.0, poll_interval=2.0, sleep=sleep, now=now)

        assert state["sleeps"] == [2.0, 2.0]
        assert calls == [(tmp_path, SANDBOX)] * 3

    def test_times_out_when_the_issue_never_appears(self, monkeypatch, tmp_path):
        monkeypatch.setattr(harness_mod, "ready_issue_numbers", lambda *_a, **_k: ())
        state, sleep, now = self._clock()

        with pytest.raises(HarnessPreflightError) as exc_info:
            wait_for_issue_visible(tmp_path, SANDBOX, issue_number=7, timeout=6.0, poll_interval=2.0, sleep=sleep, now=now)

        assert exc_info.value.code == "issue_not_visible"
        assert "#7" in exc_info.value.detail and SANDBOX.repo in exc_info.value.detail
        assert state["sleeps"] == [2.0, 2.0, 2.0]

    def test_another_ready_issue_alongside_ours_is_not_quiescent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(harness_mod, "ready_issue_numbers", lambda *_a, **_k: (7, 15))
        state, sleep, now = self._clock()

        with pytest.raises(HarnessPreflightError) as exc_info:
            wait_for_issue_visible(tmp_path, SANDBOX, issue_number=7, timeout=60.0, poll_interval=2.0, sleep=sleep, now=now)

        assert exc_info.value.code == "sandbox_not_quiescent"
        assert exc_info.value.detail == "#15"
        assert state["sleeps"] == []

    def test_sleep_is_clamped_to_the_deadline(self, monkeypatch, tmp_path):
        monkeypatch.setattr(harness_mod, "ready_issue_numbers", lambda *_a, **_k: ())
        state, sleep, now = self._clock()

        with pytest.raises(HarnessPreflightError):
            wait_for_issue_visible(tmp_path, SANDBOX, issue_number=7, timeout=30.0, poll_interval=60.0, sleep=sleep, now=now)

        assert state["sleeps"] == [30.0]

    @pytest.mark.parametrize("kwargs", [{"poll_interval": 0.0}, {"poll_interval": -1.0}, {"timeout": -1.0}])
    def test_invalid_timing_is_rejected_before_listing(self, monkeypatch, tmp_path, kwargs):
        monkeypatch.setattr(harness_mod, "ready_issue_numbers", lambda *_a, **_k: pytest.fail("listed"))
        params = {"timeout": 10.0, "poll_interval": 1.0, **kwargs}

        with pytest.raises(ValueError):
            wait_for_issue_visible(tmp_path, SANDBOX, issue_number=7, sleep=lambda _s: None, now=lambda: 0.0, **params)

    def test_ready_issue_numbers_uses_the_production_listing(self, fake_gh, tmp_path):
        _seed_github(fake_gh, [todo_payload(12, labels=["tpo:todo", "ready-for-agent"]), todo_payload(15, labels=["tpo:todo"])])

        assert ready_issue_numbers(tmp_path, SANDBOX) == (12,)
        assert other_ready_issues(tmp_path, SANDBOX, exclude_issue=12) == ()


# ---------------------------------------------------------------------------
# Plan-pinned registration recovery (H1)
# ---------------------------------------------------------------------------

_PINNED_REPO = "acme/sandbox"
_PINNED_ISSUE = 42
_PINNED_TICK = "01PINNED"
_PINNED_PLAN = "docs/harness/tok00000-plan.md"
_PINNED_BRANCH = "feat/pinned-run"
_PINNED_STEPS = ("plan:task-1", "validate:task-1")


def _pinned_manifest(todo_id: str) -> str:
    payload = {
        "schema_version": 1,
        "todo_id": todo_id,
        "tasks": [
            {
                "id": "task-1",
                "title": "Pinned task",
                "instructions": "Implement the change.",
                "acceptance_criteria": ["Change is observable."],
                "verification": ["uv run pytest tests/test_change.py"],
                "commit_message": "feat: change",
            }
        ],
    }
    return f"# Plan\n\n```json tpo-plan\n{json.dumps(payload)}\n```\n"


@dataclasses.dataclass
class _PinnedFixture:
    project_dir: Path
    state: Path
    issue: HarnessIssue
    plan_sha: str
    plan_text: str
    worktree: Path
    run_dir: Path

    @property
    def registration_path(self) -> Path:
        return self.run_dir / "registration.json"

    def edit_registration(self, **changes: object) -> None:
        payload = json.loads(self.registration_path.read_text())
        payload.update(changes)
        self.registration_path.write_text(json.dumps(payload))

    def write_sentinel(self, keys: object) -> None:
        outcomes = self.worktree / ".hermes" / "outcomes"
        outcomes.mkdir(parents=True, exist_ok=True)
        (outcomes / "expected-phases.json").write_text(json.dumps(keys))

    def recover(self, **overrides: object) -> TickRegistration:
        kwargs: dict[str, object] = {
            "issue": self.issue,
            "repo": _PINNED_REPO,
            "plan_sha": self.plan_sha,
            "plan_text": self.plan_text,
        }
        kwargs.update(overrides)
        return recover_pinned_registration(self.project_dir, self.state, **kwargs)


def _pinned_registration(tmp_path: Path, *, issue: int = _PINNED_ISSUE) -> _PinnedFixture:
    """A real ``register_pinned_run`` against a temp clone whose HEAD commits the Plan."""
    from hermes_pipeline.run_registration import register_pinned_run

    project_dir = tmp_path / "clone"
    (project_dir / _PINNED_PLAN).parent.mkdir(parents=True)
    _real_git("init", "-q", "-b", "main", cwd=project_dir)
    _real_git("config", "user.email", "harness@example.com", cwd=project_dir)
    _real_git("config", "user.name", "Harness", cwd=project_dir)
    plan_text = _pinned_manifest(f"TODO-{issue}")
    (project_dir / _PINNED_PLAN).write_text(plan_text)
    _real_git("add", ".", cwd=project_dir)
    _real_git("commit", "-qm", "plan", cwd=project_dir)
    _real_git("remote", "add", "origin", f"https://github.com/{_PINNED_REPO}.git", cwd=project_dir)
    plan_sha = _real_git("rev-parse", "HEAD", cwd=project_dir)

    body = f"### Plan\n\n{_PINNED_PLAN}\n\n### Branch\n\n{_PINNED_BRANCH}\n"
    payload = issue_payload(
        issue,
        title="Pinned run",
        body=body,
        html_url=f"https://github.com/{_PINNED_REPO}/issues/{issue}",
    )
    state = project_dir / ".hermes"
    registration = register_pinned_run(
        project_dir=project_dir,
        state_dir=state,
        tick_id=_PINNED_TICK,
        selected_issue=issue_from_api(payload, repo=_PINNED_REPO),
        plan_path=_PINNED_PLAN,
        profile="native-sdd",
        prompt_client="claude",
        assignee="pipeline",
        review_assignee=None,
        step_keys=_PINNED_STEPS,
        repo=_PINNED_REPO,
    )
    _write_tick_state(state, tick_id=_PINNED_TICK, expected_phases=None)
    fixture = _PinnedFixture(
        project_dir=project_dir,
        state=state,
        issue=HarnessIssue(
            number=issue,
            todo_id=f"TODO-{issue}",
            branch=_PINNED_BRANCH,
            plan_path=_PINNED_PLAN,
            title="Pinned run",
            run_token="tok00000",
        ),
        plan_sha=plan_sha,
        plan_text=plan_text,
        worktree=registration.worktree,
        run_dir=state / "runs" / _PINNED_TICK,
    )
    fixture.write_sentinel(list(_PINNED_STEPS))
    return fixture


@pytest.mark.real_git
class TestRecoverPinnedRegistration:
    def test_happy_path_returns_pinned_registration(self, tmp_path: Path):
        fx = _pinned_registration(tmp_path)

        reg = fx.recover()

        assert reg.tick_id == _PINNED_TICK
        assert reg.todo_id == f"TODO-{_PINNED_ISSUE}"
        assert reg.phase_keys == _PINNED_STEPS
        assert reg.worktree == fx.worktree
        assert reg.branch == _PINNED_BRANCH
        assert reg.base_sha == fx.plan_sha
        assert reg.run_dir == fx.run_dir
        assert reg.pinned is True

    def test_missing_registration_is_invalid(self, tmp_path: Path):
        fx = _pinned_registration(tmp_path)
        fx.registration_path.unlink()

        with pytest.raises(HarnessTickError) as excinfo:
            fx.recover()

        assert excinfo.value.code == "registration_invalid"
        assert excinfo.value.detail == "registration_invalid"
        assert excinfo.value.tick_id == _PINNED_TICK

    def test_worktree_outside_repository_is_invalid(self, tmp_path: Path):
        fx = _pinned_registration(tmp_path)
        outside = tmp_path / "elsewhere" / fx.worktree.name
        fx.edit_registration(worktree=str(outside))

        with pytest.raises(HarnessTickError) as excinfo:
            fx.recover()

        assert excinfo.value.code == "registration_invalid"
        assert excinfo.value.detail == "registration_invalid"

    def test_worktree_on_wrong_branch_is_invalid(self, tmp_path: Path):
        fx = _pinned_registration(tmp_path)
        _real_git("checkout", "-q", "-b", "feat/other", cwd=fx.worktree)

        with pytest.raises(HarnessTickError) as excinfo:
            fx.recover()

        assert excinfo.value.code == "registration_invalid"

    def test_altered_plan_hash_is_invalid(self, tmp_path: Path):
        fx = _pinned_registration(tmp_path)
        fx.edit_registration(plan_hash="0" * 64)

        with pytest.raises(HarnessTickError) as excinfo:
            fx.recover()

        assert excinfo.value.code == "registration_invalid"

    def test_contract_detail_is_surfaced(self, tmp_path: Path):
        fx = _pinned_registration(tmp_path)
        fx.edit_registration(schema_version=99)

        with pytest.raises(HarnessTickError) as excinfo:
            fx.recover()

        assert excinfo.value.code == "registration_invalid"
        assert excinfo.value.detail == "registration_invalid: unsupported schema_version"

    def test_forged_plan_via_replace_ref_is_plan_mismatch(self, tmp_path: Path):
        # The clone is agent-controlled: a replace ref makes ``git show
        # <plan_sha>:<plan_path>`` return forged Plan bytes, so the contract's
        # own hash check passes against the forgery. The harness must still pin
        # the hash to the Plan text it committed itself.
        fx = _pinned_registration(tmp_path)
        forged = fx.plan_text + "\n<!-- forged -->\n"
        (fx.project_dir / _PINNED_PLAN).write_text(forged)
        _real_git("commit", "-qam", "forged", cwd=fx.project_dir)
        forged_sha = _real_git("rev-parse", "HEAD", cwd=fx.project_dir)
        _real_git("replace", "-f", fx.plan_sha, forged_sha, cwd=fx.project_dir)
        shown = _real_git("show", f"{fx.plan_sha}:{_PINNED_PLAN}", cwd=fx.project_dir)
        assert "forged" in shown
        true_hash = json.loads(fx.registration_path.read_text())["plan_hash"]
        fx.edit_registration(plan_hash=hashlib.sha256(forged.encode()).hexdigest())

        with pytest.raises(HarnessTickError) as excinfo:
            fx.recover()

        assert excinfo.value.code == "registration_plan_mismatch"
        assert excinfo.value.tick_id == _PINNED_TICK

        # Race variant: the true hash is restored while the replace ref stays in
        # place, so the contract's own check must fail against the forged bytes.
        fx.edit_registration(plan_hash=true_hash)

        with pytest.raises(HarnessTickError) as excinfo:
            fx.recover()

        assert excinfo.value.code == "registration_invalid"
        assert excinfo.value.tick_id == _PINNED_TICK

    def test_branch_mismatch_is_unexpected(self, tmp_path: Path):
        fx = _pinned_registration(tmp_path)

        with pytest.raises(HarnessTickError) as excinfo:
            fx.recover(issue=dataclasses.replace(fx.issue, branch="feat/other"))

        assert excinfo.value.code == "unexpected_registration"
        assert excinfo.value.detail == f"branch {_PINNED_BRANCH} != feat/other"
        assert excinfo.value.tick_id == _PINNED_TICK

    def test_plan_path_mismatch_is_unexpected(self, tmp_path: Path):
        fx = _pinned_registration(tmp_path)
        other = "docs/harness/other000-plan.md"

        with pytest.raises(HarnessTickError) as excinfo:
            fx.recover(issue=dataclasses.replace(fx.issue, plan_path=other))

        assert excinfo.value.code == "unexpected_registration"
        assert excinfo.value.detail == f"plan_path {_PINNED_PLAN} != {other}"
        assert excinfo.value.tick_id == _PINNED_TICK

    def test_sentinel_mismatch_detail_is_capped(self, tmp_path: Path):
        fx = _pinned_registration(tmp_path)
        fx.write_sentinel([f"phase:{'x' * 200}-{i}" for i in range(20)])

        with pytest.raises(HarnessTickError) as excinfo:
            fx.recover()

        assert excinfo.value.code == "unexpected_registration"
        assert len(excinfo.value.detail) <= harness_mod._ERROR_MESSAGE_MAX

    def test_malformed_base_sha_is_invalid(self, tmp_path: Path):
        fx = _pinned_registration(tmp_path)
        fx.edit_registration(base_sha="not-a-sha")

        with pytest.raises(HarnessTickError) as excinfo:
            fx.recover(plan_sha="not-a-sha")

        assert excinfo.value.code == "registration_invalid"

    def test_issue_mismatch_is_unexpected(self, tmp_path: Path):
        fx = _pinned_registration(tmp_path)

        with pytest.raises(HarnessTickError) as excinfo:
            fx.recover(issue=dataclasses.replace(fx.issue, number=_PINNED_ISSUE + 1))

        assert excinfo.value.code == "unexpected_registration"
        assert excinfo.value.detail == f"issue {_PINNED_ISSUE}"
        assert excinfo.value.tick_id == _PINNED_TICK

    def test_base_sha_mismatch(self, tmp_path: Path):
        fx = _pinned_registration(tmp_path)
        other = "f" * 40

        with pytest.raises(HarnessTickError) as excinfo:
            fx.recover(plan_sha=other)

        assert excinfo.value.code == "registration_base_mismatch"
        assert excinfo.value.detail == f"{fx.plan_sha} != {other}"
        assert excinfo.value.tick_id == _PINNED_TICK

    @pytest.mark.parametrize(
        "sentinel",
        [
            ["plan:task-1"],
            ["plan:task-1", "validate:task-1", "extra"],
            ["plan:task-1", "validate:task-1", "plan:task-1"],
        ],
        ids=["subset", "superset", "duplicate"],
    )
    def test_sentinel_keys_must_match_step_keys(self, tmp_path: Path, sentinel):
        fx = _pinned_registration(tmp_path)
        fx.write_sentinel(sentinel)

        with pytest.raises(HarnessTickError) as excinfo:
            fx.recover()

        assert excinfo.value.code == "unexpected_registration"
        assert excinfo.value.tick_id == _PINNED_TICK

    def test_sentinel_read_from_worktree_not_clone(self, tmp_path: Path):
        fx = _pinned_registration(tmp_path)
        (fx.state / "outcomes" / "expected-phases.json").write_text(json.dumps(list(_PINNED_STEPS)))
        (fx.worktree / ".hermes" / "outcomes" / "expected-phases.json").unlink()

        with pytest.raises(HarnessTickError) as excinfo:
            fx.recover()

        assert excinfo.value.code == "expected_phases_missing"
        assert excinfo.value.tick_id == _PINNED_TICK

    def test_tick_not_persisted_propagates(self, tmp_path: Path):
        fx = _pinned_registration(tmp_path)
        log = tmp_path / "tick.log"
        log.write_text("stderr tail\n")

        with pytest.raises(HarnessTickError) as excinfo:
            fx.recover(previous_tick_id=_PINNED_TICK, tick_log=log)

        assert excinfo.value.code == "tick_not_persisted"
        assert "stderr tail" in excinfo.value.detail

    def test_picked_none_propagates(self, tmp_path: Path):
        fx = _pinned_registration(tmp_path)
        (fx.state / "outcomes" / f"{_PINNED_TICK}-phases.json").write_text(
            json.dumps({"outcome": "picked_none"}) + "\n"
        )

        with pytest.raises(HarnessTickError) as excinfo:
            fx.recover()

        assert excinfo.value.code == "picked_none"


class TestAssertTickIdUnchanged:
    def test_unchanged_is_ok(self, tmp_path: Path):
        (tmp_path / "current_tick_id.txt").write_text("01TICK\n")

        assert assert_tick_id_unchanged(tmp_path, expected="01TICK") is None

    def test_changed_is_unexpected_selection(self, tmp_path: Path):
        (tmp_path / "current_tick_id.txt").write_text("02TICK\n")

        with pytest.raises(HarnessTickError) as excinfo:
            assert_tick_id_unchanged(tmp_path, expected="01TICK")

        assert excinfo.value.code == "unexpected_selection"
        assert excinfo.value.detail == "02TICK != 01TICK"
        assert excinfo.value.tick_id == "01TICK"

    @pytest.mark.parametrize("content", [None, " \n"], ids=["absent", "blank"])
    def test_missing_is_unexpected_selection(self, tmp_path: Path, content):
        if content is not None:
            (tmp_path / "current_tick_id.txt").write_text(content)

        with pytest.raises(HarnessTickError) as excinfo:
            assert_tick_id_unchanged(tmp_path, expected="01TICK")

        assert excinfo.value.code == "unexpected_selection"
        assert excinfo.value.detail == "missing"
        assert excinfo.value.tick_id == "01TICK"
