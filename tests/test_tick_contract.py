"""Tests for the pipeline execution contract wired into the tick flow."""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hermes_pipeline.cli import _cmd_tick, _tick_project
from hermes_pipeline.config import CircuitBreakerConfig, Config
from hermes_pipeline.phases import PhasePromptRenderError
from tests.gh_fakes import (
    API_ARGV,
    ELIGIBLE_BODY,
    ORIGIN_ARGV,
    seed_project_issues,
    todo_payload,
)

REPO = "acme/repo"
PLAN_BODY = "### What\n\nTest\n\n### Plan\n\ndocs/plan.md\n\n### Branch\n\nfeat/todo-10\n"


@pytest.fixture(autouse=True)
def _github_todo_10(fake_gh):
    """Every tick reads TODOs from GitHub: serve #10 as the default candidate."""
    return seed_project_issues(fake_gh, [todo_payload(10, title="test")])


def _make_decision(picked):
    decision = MagicMock()
    decision.picked = picked
    decision.rationale = "test"
    decision.candidates_considered = []
    return decision


def _create_project(projects_dir, name):
    project_dir = projects_dir / name
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir


def _read_outcomes(project_state):
    return [
        SimpleNamespace(**json.loads(path.read_text()))
        for path in sorted((project_state / "outcomes").glob("*.json"))
        if not path.name.endswith("-phases.json")
    ]


def _run_project_tick(
    *,
    project_dir,
    config,
    tick_id,
    mocker,
    registration_side_effect=None,
    patch_registration=True,
    picked="TODO-10",
):
    cb = mocker.Mock()
    mocker.patch("hermes_pipeline.cli._make_circuit_breaker", return_value=cb)
    selection = mocker.patch(
        "hermes_pipeline.cli.run_selection",
        return_value=_make_decision(picked),
    )
    registration = None
    if patch_registration:
        registration = mocker.patch(
            "hermes_pipeline.run_registration.register_pinned_run",
            return_value=SimpleNamespace(worktree=project_dir / ".worktrees" / "todo-10"),
            side_effect=registration_side_effect,
        )
    _tick_project(
        project_dir=project_dir,
        project_slug=project_dir.name,
        project_state=project_dir / ".hermes",
        config=config,
        cb_cfg=CircuitBreakerConfig(),
        tick_id=tick_id,
        project_toml={},
    )
    selection.registration = registration
    selection.cb = cb
    return selection


class FakeArgs:
    def __init__(self, **kwargs):
        kwargs.setdefault("project", None)
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestTickContractAssignee:
    def test_tick_uses_contract_assignee(self, tmp_path, mocker):
        """register_todo_phases is called with the contract's assignee."""
        mocker.patch("hermes_pipeline.cli.run_selection", return_value=_make_decision("TODO-10"))
        mock_register = mocker.patch(
            "hermes_pipeline.kanban_tasks.create_prepared_todo_phases",
            return_value=["t_1"],
        )

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "reviewer-bot"\ncapabilities = ["Read", "Write", "Edit", "Bash"]\n'
        )

        config = Config(projects_dir=projects_dir, state_dir=tmp_path / "state")
        result = _cmd_tick(FakeArgs(), config)

        assert result == 0
        mock_register.assert_called_once()
        assert mock_register.call_args.kwargs["assignee"] == "reviewer-bot"

    def test_tick_no_contract_falls_back_to_pipeline_assignee(self, tmp_path, mocker):
        """No pipeline.toml -> verifies and falls back to assignee='pipeline'."""
        mocker.patch("hermes_pipeline.cli.run_selection", return_value=_make_decision("TODO-10"))
        mock_register = mocker.patch(
            "hermes_pipeline.kanban_tasks.create_prepared_todo_phases",
            return_value=["t_1"],
        )
        mock_run = mocker.patch("hermes_pipeline.cli._cli_sp.run")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")

        config = Config(projects_dir=projects_dir, state_dir=tmp_path / "state")
        result = _cmd_tick(FakeArgs(project="demo"), config)

        assert result == 0
        mock_register.assert_called_once()
        assert mock_register.call_args.kwargs["assignee"] == "pipeline"
        assert any(
            call.args[0] == ["hermes", "profile", "show", "pipeline"]
            for call in mock_run.call_args_list
        )


    def test_tick_no_contract_warns_when_pipeline_profile_missing(self, tmp_path, mocker, caplog):
        """Implicit fallback verifies the pipeline profile and warns if unavailable."""
        mocker.patch("hermes_pipeline.cli.run_selection", return_value=_make_decision("TODO-10"))
        mock_register = mocker.patch(
            "hermes_pipeline.kanban_tasks.create_prepared_todo_phases",
            return_value=["t_1"],
        )
        mock_run = mocker.patch("hermes_pipeline.cli._cli_sp.run")
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="profile not found")

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")

        config = Config(projects_dir=projects_dir, state_dir=tmp_path / "state")
        result = _cmd_tick(FakeArgs(project="demo"), config)

        assert result == 0
        mock_register.assert_called_once()
        assert mock_register.call_args.kwargs["assignee"] == "pipeline"
        assert "hermes profile show pipeline" in caplog.text

    def test_tick_no_contract_warns_when_hermes_missing(self, tmp_path, mocker, caplog):
        """Missing Hermes binary warns but does not skip the implicit fallback."""
        mocker.patch("hermes_pipeline.cli.run_selection", return_value=_make_decision("TODO-10"))
        mock_register = mocker.patch(
            "hermes_pipeline.kanban_tasks.create_prepared_todo_phases",
            return_value=["t_1"],
        )
        mocker.patch("hermes_pipeline.cli._cli_sp.run", side_effect=FileNotFoundError("hermes"))

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")

        config = Config(projects_dir=projects_dir, state_dir=tmp_path / "state")
        result = _cmd_tick(FakeArgs(project="demo"), config)

        assert result == 0
        mock_register.assert_called_once()
        assert mock_register.call_args.kwargs["assignee"] == "pipeline"
        assert "rc=127" in caplog.text

    def test_tick_capability_mismatch_skips_project_not_whole_scan(self, tmp_path, mocker):
        """A project with a capability-deficient contract is skipped, scan continues."""
        mocker.patch("hermes_pipeline.cli.run_selection", return_value=_make_decision("TODO-10"))
        mock_register = mocker.patch(
            "hermes_pipeline.kanban_tasks.create_prepared_todo_phases"
        )

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 2\ncapabilities = ["Read"]\n'
        )

        config = Config(projects_dir=projects_dir, state_dir=tmp_path / "state")
        result = _cmd_tick(FakeArgs(), config)

        assert result == 0  # scan-level result: per-project errors don't abort the scan
        mock_register.assert_not_called()

    def test_tick_stale_contract_version_skips_project(self, tmp_path, mocker):
        """A contract with a stale schema_version fails closed for that project."""
        mocker.patch("hermes_pipeline.cli.run_selection", return_value=_make_decision("TODO-10"))
        mock_register = mocker.patch(
            "hermes_pipeline.kanban_tasks.create_prepared_todo_phases"
        )

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text("schema_version = 99\n")

        config = Config(projects_dir=projects_dir, state_dir=tmp_path / "state")
        result = _cmd_tick(FakeArgs(), config)

        assert result == 0
        mock_register.assert_not_called()

    def test_tick_blocks_unverified_profile_before_registration(self, tmp_path, mocker):
        """Capability validation must load phases from the contract's declared
        profile, not the hardcoded gstack default — else a project running a
        non-gstack profile is checked against the wrong phase requirements."""
        run_selection = mocker.patch(
            "hermes_pipeline.cli.run_selection",
            return_value=_make_decision("TODO-10"),
        )
        mock_register = mocker.patch(
            "hermes_pipeline.kanban_tasks.create_prepared_todo_phases"
        )
        from hermes_pipeline import phases as phases_mod
        from hermes_pipeline.phases import resolve_profile_phases_path
        spy_load_profile = mocker.patch(
            "hermes_pipeline.phases.load_phase_profile",
            wraps=phases_mod.load_phase_profile,
        )

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "default"\n'
            'capabilities = ["Read", "Write", "Edit", "Bash"]\n'
            'profile = "agent-skills"\n'
        )

        config = Config(projects_dir=projects_dir, state_dir=tmp_path / "state")
        result = _cmd_tick(FakeArgs(), config)

        assert result == 0
        run_selection.assert_not_called()
        mock_register.assert_not_called()
        agent_skills_path = resolve_profile_phases_path("agent-skills")
        assert any(
            call.args and call.args[0] == agent_skills_path
            for call in spy_load_profile.call_args_list
        )


class TestPendingTaskCreateGate:
    def test_unreconciled_pending_task_create_skips_tick(self, tmp_path, mocker):
        """An unresolved create must block every later tick action."""
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        project_state.mkdir()
        (project_state / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\n'
            'capabilities = ["Read", "Write", "Edit", "Bash"]\n'
        )

        reconcile = mocker.patch(
            "hermes_pipeline.kanban_tasks.reconcile_pending_task_create",
            return_value=False,
        )
        prior_tick = mocker.patch(
            "hermes_pipeline.cli._read_prior_tick_id", return_value="01PRIOR"
        )
        all_complete = mocker.patch(
            "hermes_pipeline.cli.all_phases_complete", return_value=False
        )
        run_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        create = mocker.patch("hermes_pipeline.kanban_tasks.create_prepared_todo_phases")

        _tick_project(
            project_dir=project_dir,
            project_slug=project_dir.name,
            project_state=project_state,
            config=Config(),
            cb_cfg=CircuitBreakerConfig(),
            tick_id="01PENDING",
            project_toml={},
        )

        reconcile.assert_called_once_with(project_dir)
        prior_tick.assert_not_called()
        all_complete.assert_not_called()
        run_selection.assert_not_called()
        create.assert_not_called()

    def test_pending_task_create_reconciles_before_unverified_profile_gate(
        self, tmp_path, mocker
    ):
        """Cleanup recovery must not be blocked by unsupported profile metadata."""
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        project_state.mkdir()
        (project_state / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\n'
            'capabilities = ["Read", "Write", "Edit", "Bash"]\n'
            'profile = "agent-skills"\n'
        )

        reconcile = mocker.patch(
            "hermes_pipeline.kanban_tasks.reconcile_pending_task_create",
            return_value=False,
        )
        run_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        create = mocker.patch("hermes_pipeline.kanban_tasks.create_prepared_todo_phases")

        _tick_project(
            project_dir=project_dir,
            project_slug=project_dir.name,
            project_state=project_state,
            config=Config(prompt_client="codex"),
            cb_cfg=CircuitBreakerConfig(),
            tick_id="01PENDING",
            project_toml={},
        )

        reconcile.assert_called_once_with(project_dir)
        run_selection.assert_not_called()
        create.assert_not_called()

    def test_reconciled_pending_task_create_checks_prior_tick(self, tmp_path, mocker):
        """A reconciled create preserves the existing prior-tick gate."""
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        project_state.mkdir()
        (project_state / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\n'
            'capabilities = ["Read", "Write", "Edit", "Bash"]\n'
        )

        reconcile = mocker.patch(
            "hermes_pipeline.kanban_tasks.reconcile_pending_task_create",
            return_value=True,
        )
        prior_tick = mocker.patch(
            "hermes_pipeline.cli._read_prior_tick_id", return_value="01PRIOR"
        )
        all_complete = mocker.patch(
            "hermes_pipeline.cli.all_phases_complete", return_value=False
        )
        run_selection = mocker.patch("hermes_pipeline.cli.run_selection")

        _tick_project(
            project_dir=project_dir,
            project_slug=project_dir.name,
            project_state=project_state,
            config=Config(),
            cb_cfg=CircuitBreakerConfig(),
            tick_id="01PENDING",
            project_toml={},
        )

        reconcile.assert_called_once_with(project_dir)
        prior_tick.assert_called_once_with(project_state)
        all_complete.assert_called_once_with(
            project_dir.name, "01PRIOR", state_dir=project_state
        )
        run_selection.assert_not_called()


class TestPriorTickRegistrationValidation:
    def test_unsupported_prior_registration_logs_code_and_counts_no_progress(
        self, tmp_path, mocker, caplog
    ):
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        (project_state / "runs" / "01PRIOR").mkdir(parents=True)
        (project_state / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\n'
            'capabilities = ["Read", "Write", "Edit", "Bash"]\n'
        )
        (project_state / "runs" / "01PRIOR" / "registration.json").write_text(
            json.dumps({"schema_version": 1, "tick_id": "01PRIOR"})
        )
        mocker.patch("hermes_pipeline.kanban_tasks.reconcile_pending_task_create", return_value=True)
        mocker.patch("hermes_pipeline.cli._read_prior_tick_id", return_value="01PRIOR")
        mocker.patch("hermes_pipeline.ship.maybe_ship_ready")
        mocker.patch("hermes_pipeline.ship.read_sidecar", return_value=None)
        cb = mocker.Mock()
        mocker.patch("hermes_pipeline.cli._make_circuit_breaker", return_value=cb)
        all_complete = mocker.patch("hermes_pipeline.cli.all_phases_complete")
        run_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        caplog.set_level("ERROR", logger="hermes_pipeline.cli")

        _tick_project(
            project_dir=project_dir,
            project_slug=project_dir.name,
            project_state=project_state,
            config=Config(),
            cb_cfg=CircuitBreakerConfig(),
            tick_id="01NEXT",
            project_toml={},
        )

        cb.observe.assert_called_once_with(picked=None, counts_as_no_progress=True)
        all_complete.assert_not_called()
        run_selection.assert_not_called()
        assert any(
            "01PRIOR" in record.getMessage()
            and "registration_invalid" in record.getMessage()
            and "schema_version" in record.getMessage()
            for record in caplog.records
        )


class TestTickPromptPreparation:
    def test_tick_prepares_selected_profile_and_client_before_mutation(
        self, tmp_path, mocker
    ):
        events: list[tuple[str, object]] = []
        prepare = mocker.patch(
            "hermes_pipeline.kanban_tasks.prepare_todo_phases",
            side_effect=lambda **kwargs: events.append(("prepare", kwargs))
            or ["prepared"],
        )
        persist = mocker.patch(
            "hermes_pipeline.cli._persist_tick_id",
            side_effect=lambda *args, **kwargs: events.append(("persist", kwargs)),
        )
        create = mocker.patch(
            "hermes_pipeline.kanban_tasks.create_prepared_todo_phases",
            side_effect=lambda **kwargs: events.append(("create", kwargs))
            or ["task-1"],
        )
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        project_state.mkdir()
        (project_state / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\n'
            'capabilities = ["Read", "Write", "Edit", "Bash"]\n'
            'profile = "gstack"\n'
        )

        _run_project_tick(
            project_dir=project_dir,
            config=Config(prompt_client="codex"),
            tick_id="01CLIENT",
            mocker=mocker,
        )

        assert [name for name, _ in events] == ["prepare", "persist", "create"]
        prepare_kwargs = prepare.call_args.kwargs
        assert prepare_kwargs["prompt_client"] == "codex"
        assert "gstack" in str(prepare_kwargs["phases_path"])
        assert create.call_args.kwargs["prepared"] == ["prepared"]
        assert persist.call_count == 1

    def test_prompt_preparation_failure_leaves_registration_unmutated(
        self, tmp_path, mocker, caplog
    ):
        tick_id = "01PREPFAIL"
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        project_state.mkdir()
        (project_state / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\n'
            'capabilities = ["Read", "Write", "Edit", "Bash"]\n'
            'profile = "gstack"\n'
        )

        prepare = mocker.patch(
            "hermes_pipeline.kanban_tasks.prepare_todo_phases",
            side_effect=PhasePromptRenderError(
                "agent-skills:phase_7_ship: unknown field token=secret-value"
            ),
        )
        persist = mocker.patch("hermes_pipeline.cli._persist_tick_id")
        create = mocker.patch(
            "hermes_pipeline.kanban_tasks.create_prepared_todo_phases",
            return_value=["task-1"],
        )
        _run_project_tick(
            project_dir=project_dir,
            config=Config(prompt_client="codex"),
            tick_id=tick_id,
            mocker=mocker,
        )

        persist.assert_not_called()
        create.assert_not_called()
        assert not (project_state / "current_tick_id.txt").exists()
        outcomes = _read_outcomes(project_state)
        assert outcomes[-1].outcome == "failed_to_spawn"
        assert outcomes[-1].tick_id == tick_id
        assert outcomes[-1].detail == {
            "todo_id": "TODO-10",
            "reason": "phase_prompt_preparation_failed",
            "error_type": "PhasePromptRenderError",
        }
        assert "secret-value" not in caplog.text
        assert "secret-value" not in "".join(
            path.read_text() for path in (project_state / "outcomes").glob("*.json")
        )

        prepare.side_effect = None
        prepare.return_value = ["prepared"]
        _run_project_tick(
            project_dir=project_dir,
            config=Config(prompt_client="codex"),
            tick_id=tick_id,
            mocker=mocker,
        )
        create.assert_called_once()

    def test_consecutive_prompt_preparation_failures_advance_circuit_breaker(
        self, tmp_path, mocker
    ):
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        project_state.mkdir()
        (project_state / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\n'
            'capabilities = ["Read", "Write", "Edit", "Bash"]\n'
            'profile = "gstack"\n'
        )
        mocker.patch(
            "hermes_pipeline.cli.run_selection",
            return_value=_make_decision("TODO-10"),
        )
        mocker.patch(
            "hermes_pipeline.kanban_tasks.prepare_todo_phases",
            side_effect=PhasePromptRenderError(
                "agent-skills:phase_7_ship: unknown field"
            ),
        )
        send_alert = mocker.patch("hermes_pipeline.circuit._send_slack")
        cb_cfg = CircuitBreakerConfig(no_progress_threshold=2)

        for tick_id in ("01PREPFAIL1", "01PREPFAIL2"):
            _tick_project(
                project_dir=project_dir,
                project_slug=project_dir.name,
                project_state=project_state,
                config=Config(prompt_client="codex"),
                cb_cfg=cb_cfg,
                tick_id=tick_id,
                project_toml={},
            )

        circuit_state = json.loads((project_state / "circuit.json").read_text())
        assert circuit_state["consecutive_no_progress"] == 2
        send_alert.assert_called_once()

    def test_prompt_preparation_failure_survives_outcome_write_error(
        self, tmp_path, mocker, caplog
    ):
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        project_state.mkdir()
        (project_state / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\n'
            'capabilities = ["Read", "Write", "Edit", "Bash"]\n'
            'profile = "gstack"\n'
        )
        mocker.patch(
            "hermes_pipeline.kanban_tasks.prepare_todo_phases",
            side_effect=PhasePromptRenderError("invalid prompt"),
        )
        persist = mocker.patch("hermes_pipeline.cli._persist_tick_id")
        create = mocker.patch(
            "hermes_pipeline.kanban_tasks.create_prepared_todo_phases"
        )
        mocker.patch(
            "hermes_pipeline.decision.store.append_outcome",
            side_effect=OSError("disk full"),
        )

        _run_project_tick(
            project_dir=project_dir,
            config=Config(prompt_client="codex"),
            tick_id="01SIDECARFAIL",
            mocker=mocker,
        )

        persist.assert_not_called()
        create.assert_not_called()
        assert "failed to write outcome sidecar: error_type=OSError" in caplog.text
        assert "disk full" not in caplog.text


class TestTickPlanRequirement:
    @staticmethod
    def _configure_profile(project_dir, tmp_path, mocker):
        profile_dir = tmp_path / "native-sdd"
        profile_dir.mkdir()
        phases_path = profile_dir / "phases.yaml"
        phases_path.write_text(
            "requires_plan: true\n"
            "phases:\n"
            "  - phase_key: phase_4_development\n"
            "    name: Development\n"
            "    prompt: 'Implement {plan_path}'\n"
            "    tools: Read,Write,Edit,Bash\n"
            "    turns: 10\n"
        )
        (profile_dir / "prerequisites.yaml").write_text(
            "schema_version: 1\nprofile: native-sdd\nskills: []\n"
        )
        mocker.patch(
            "hermes_pipeline.phases.resolve_profile_phases_path",
            return_value=phases_path,
        )
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "default"\n'
            'capabilities = ["Read", "Write", "Edit", "Bash"]\n'
            'profile = "native-sdd"\n'
        )

    def test_missing_plan_is_filtered_before_selection_or_kanban(
        self, tmp_path, mocker, fake_gh
    ):
        tick_id = "01PLANFAIL"
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        project_state.mkdir()
        self._configure_profile(project_dir, tmp_path, mocker)
        seed_project_issues(fake_gh, [todo_payload(10, title="Test", body="### What\n\nTest\n")])
        prepare = mocker.patch("hermes_pipeline.kanban_tasks.prepare_todo_phases")
        create = mocker.patch(
            "hermes_pipeline.kanban_tasks.create_prepared_todo_phases"
        )

        selection = _run_project_tick(
            project_dir=project_dir,
            config=Config(prompt_client="codex"),
            tick_id=tick_id,
            mocker=mocker,
        )

        selection.assert_not_called()
        prepare.assert_not_called()
        create.assert_not_called()
        decision = json.loads(
            (project_state / "decisions" / f"{tick_id}.json").read_text()
        )
        assert decision["picked"] is None
        assert decision["blocked_reasons"] == {"TODO-10": "plan_invalid:missing"}

    def test_valid_plan_is_passed_to_prompt_preparation(self, tmp_path, mocker, fake_gh):
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        project_state.mkdir()
        self._configure_profile(project_dir, tmp_path, mocker)
        plan = project_dir / "docs" / "plan.md"
        plan.parent.mkdir()
        plan.write_text("# Plan\n")
        seed_project_issues(
            fake_gh,
            [todo_payload(10, title="Test", body=PLAN_BODY + "\n### Spec\n\ndocs/spec.md\n\n### Reference\n\ndocs/a.md, docs/b.md\n")],
        )
        prepare = mocker.patch(
            "hermes_pipeline.kanban_tasks.prepare_todo_phases",
            return_value=["prepared"],
        )
        mocker.patch(
            "hermes_pipeline.kanban_tasks.create_prepared_todo_phases",
            return_value=["task-1"],
        )

        _run_project_tick(
            project_dir=project_dir,
            config=Config(prompt_client="codex"),
            tick_id="01PLANOK",
            mocker=mocker,
        )

        assert prepare.call_args.kwargs["plan_path"] == "docs/plan.md"
        assert prepare.call_args.kwargs["spec_path"] == "docs/spec.md"
        assert prepare.call_args.kwargs["reference_paths"] == ("docs/a.md", "docs/b.md")

    def test_registration_precedes_kanban_and_uses_pinned_worktree(
        self, tmp_path, mocker, fake_gh
    ):
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        project_state.mkdir()
        self._configure_profile(project_dir, tmp_path, mocker)
        plan = project_dir / "docs" / "plan.md"
        plan.parent.mkdir()
        plan.write_text("# Plan\n")
        seed_project_issues(fake_gh, [todo_payload(10, title="Test", body=PLAN_BODY)])
        fake_gh.on(
            "gh", "issue", "edit",
            handler=lambda argv: (events.append("label") or (0, "", "")),
        )
        prepared = [SimpleNamespace(phase_key="task-1")]
        events = []
        mocker.patch(
            "hermes_pipeline.kanban_tasks.prepare_todo_phases",
            return_value=prepared,
        )
        create = mocker.patch(
            "hermes_pipeline.kanban_tasks.create_prepared_todo_phases",
            side_effect=lambda **kwargs: events.append("kanban") or ["task-1"],
        )

        def register(**kwargs):
            events.append("registration")
            return SimpleNamespace(worktree=project_dir / ".worktrees" / "todo-10")

        selection = _run_project_tick(
            project_dir=project_dir,
            config=Config(prompt_client="codex"),
            tick_id="01REGISTER",
            mocker=mocker,
            registration_side_effect=register,
        )

        registration = selection.registration
        registration.assert_called_once()
        selected_issue = registration.call_args.kwargs["selected_issue"]
        assert selected_issue.todo_id == "TODO-10" and selected_issue.number == 10
        assert registration.call_args.kwargs["repo"] == REPO
        assert registration.call_args.kwargs["plan_path"] == "docs/plan.md"
        assert registration.call_args.kwargs["step_keys"]
        assert list(registration.call_args.kwargs["step_keys"]) == ["task-1"]
        assert create.call_args.kwargs["project_dir"] == (
            project_dir / ".worktrees" / "todo-10"
        )
        # The claim label is the last step: only after the cards exist.
        assert events == ["registration", "kanban", "label"]
        assert fake_gh.gh_calls()[-1] == [
            "issue", "edit", "10", "--repo", REPO, "--add-label", "tpo:in-progress"
        ]
        selection.cb.observe.assert_called_once_with(picked="TODO-10", counts_as_no_progress=False)

    def test_requires_plan_passes_only_compiled_candidates_to_selection(
        self, tmp_path, mocker, fake_gh
    ):
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        project_state.mkdir()
        self._configure_profile(project_dir, tmp_path, mocker)
        docs = project_dir / "docs"
        docs.mkdir()
        (docs / "plan.md").write_text("# Legacy plan\n")
        seed_project_issues(
            fake_gh,
            [
                todo_payload(10, title="Eligible", body=PLAN_BODY),
                todo_payload(11, title="Missing plan", body="### Branch\n\nfeat/11\n"),
                todo_payload(12, title="Complete", body=PLAN_BODY, state="closed"),
            ],
        )
        mocker.patch(
            "hermes_pipeline.kanban_tasks.prepare_todo_phases",
            return_value=["prepared"],
        )
        mocker.patch(
            "hermes_pipeline.kanban_tasks.create_prepared_todo_phases",
            return_value=["task-1"],
        )

        selection = _run_project_tick(
            project_dir=project_dir,
            config=Config(prompt_client="codex"),
            tick_id="01FILTERED",
            mocker=mocker,
        )

        context = selection.call_args.kwargs["ctx"]
        assert "TODO-10" in context.selection_markdown
        assert "TODO-11" not in context.selection_markdown
        assert "TODO-12" not in context.selection_markdown
        assert context.candidate_ids == ("TODO-10",)
        assert selection.call_args.kwargs["eligible_todo_ids"] == frozenset({"TODO-10"})

    def test_requires_plan_with_zero_candidates_skips_selection_call(
        self, tmp_path, mocker, fake_gh
    ):
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        project_state.mkdir()
        self._configure_profile(project_dir, tmp_path, mocker)
        seed_project_issues(fake_gh, [todo_payload(10, title="Missing plan")])
        create = mocker.patch(
            "hermes_pipeline.kanban_tasks.create_prepared_todo_phases"
        )

        selection = _run_project_tick(
            project_dir=project_dir,
            config=Config(prompt_client="codex"),
            tick_id="01NONEELIGIBLE",
            mocker=mocker,
        )

        selection.assert_not_called()
        create.assert_not_called()
        assert (project_state / "outcomes" / "01NONEELIGIBLE-phases.json").exists()


class TestTickSelectionCandidates:
    """Non-plan profiles also hand the selector only compiled candidates."""

    def test_blocked_entries_never_reach_selection_context(self, tmp_path, mocker, fake_gh):
        project_dir = _create_project(tmp_path, "demo")
        (project_dir / ".hermes").mkdir()
        seed_project_issues(
            fake_gh,
            [
                todo_payload(10, title="Eligible"),
                todo_payload(11, title="On hold", labels=("tpo:todo", "ready-for-agent", "tpo:on-hold")),
                todo_payload(12, title="Waits on #11", blocked_by=1),
                todo_payload(13, title="Done", state="closed"),
            ],
        )
        mocker.patch(
            "hermes_pipeline.kanban_tasks.prepare_todo_phases",
            return_value=["prepared"],
        )
        mocker.patch(
            "hermes_pipeline.kanban_tasks.create_prepared_todo_phases",
            return_value=["task-1"],
        )

        selection = _run_project_tick(
            project_dir=project_dir,
            config=Config(prompt_client="codex"),
            tick_id="01NONPLAN",
            mocker=mocker,
        )

        context = selection.call_args.kwargs["ctx"]
        assert "TODO-10" in context.selection_markdown
        for blocked in ("TODO-11", "TODO-12", "TODO-13"):
            assert blocked not in context.selection_markdown
        assert context.candidate_ids == ("TODO-10",)
        assert selection.call_args.kwargs["eligible_todo_ids"] == frozenset({"TODO-10"})

    def test_zero_candidates_without_plan_skips_selection_call(self, tmp_path, mocker, fake_gh):
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        project_state.mkdir()
        seed_project_issues(
            fake_gh,
            [todo_payload(10, title="On hold", labels=("tpo:todo", "ready-for-agent", "tpo:on-hold"))],
        )
        create = mocker.patch("hermes_pipeline.kanban_tasks.create_prepared_todo_phases")

        selection = _run_project_tick(
            project_dir=project_dir,
            config=Config(prompt_client="codex"),
            tick_id="01NONPLANNONE",
            mocker=mocker,
        )

        selection.assert_not_called()
        create.assert_not_called()
        decision = json.loads((project_state / "decisions" / "01NONPLANNONE.json").read_text())
        assert decision["picked"] is None
        assert decision["rationale"] == "no_eligible_candidates"
        assert decision["blocked_reasons"] == {"TODO-10": "status_on_hold"}


def _registration_payload(project_dir, issue_number=10, *, body=ELIGIBLE_BODY):
    """A schema-2 registration pinning issue ``issue_number`` with ``body``."""
    from tests.gh_fakes import make_issue

    issue = make_issue(issue_number, repo=REPO, title="test", body=body)
    return {
        "schema_version": 2,
        "tick_id": "01PRIOR",
        "todo_id": issue.todo_id,
        "repository": str(project_dir),
        "base_sha": "a" * 40,
        "issue_number": issue.number,
        "issue_url": issue.url,
        "issue_snapshot": issue.snapshot,
        "selected_entry_hash": issue.entry_hash,
        "plan_path": "docs/plan.md",
        "plan_hash": "b" * 64,
        "branch": "feat/todo-10",
        "worktree": str(project_dir / ".worktrees" / "todo-10-test"),
        "profile": "native-sdd",
        "prompt_client": "codex",
        "assignee": "default",
        "review_assignee": None,
        "step_keys": ["task-1"],
    }


def _write_prior_registration(project_dir, tick_id="01PRIOR", **kwargs):
    project_state = project_dir / ".hermes"
    run_dir = project_state / "runs" / tick_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "registration.json").write_text(json.dumps(_registration_payload(project_dir, **kwargs)))
    (project_state / "current_tick_id.txt").write_text(tick_id)
    return run_dir


class TestTickGitHubSource:
    """GitHub Issues are the sole TODO source of a tick."""

    @pytest.mark.parametrize(
        ("setup", "code", "no_progress"),
        [
            (lambda gh: gh.on(*ORIGIN_ARGV, stdout="git@gitlab.com:a/b.git\n"), "origin_identity_invalid", False),
            (lambda gh: gh.on(*API_ARGV, "--paginate", "--slurp", raises=FileNotFoundError("gh")), "gh_missing", False),
            (lambda gh: gh.on(*API_ARGV, "--paginate", "--slurp", rc=1, stderr="To get started with GitHub CLI, please run: gh auth login"), "gh_auth", False),
            (lambda gh: gh.on(*API_ARGV, "--paginate", "--slurp", rc=1, stderr="connection reset"), "gh_unavailable", True),
            (lambda gh: gh.on(*API_ARGV, "--paginate", "--slurp", rc=1, stderr="API rate limit exceeded"), "gh_rate_limited", True),
        ],
        ids=["origin", "missing", "auth", "unavailable", "rate-limited"],
    )
    def test_tracker_errors_record_decision_and_classify_progress(
        self, tmp_path, mocker, fake_gh, caplog, setup, code, no_progress
    ):
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        project_state.mkdir()
        setup(fake_gh)
        create = mocker.patch("hermes_pipeline.kanban_tasks.create_prepared_todo_phases")

        with caplog.at_level("ERROR", logger="hermes_pipeline.cli"):
            selection = _run_project_tick(
                project_dir=project_dir, config=Config(prompt_client="codex"),
                tick_id="01TRACKER", mocker=mocker,
            )

        selection.assert_not_called()
        create.assert_not_called()
        selection.cb.observe.assert_called_once_with(picked=None, counts_as_no_progress=no_progress)
        decision = json.loads((project_state / "decisions" / "01TRACKER.json").read_text())
        assert decision["picked"] is None
        assert decision["rationale"] == f"tracker_error: {code}"
        assert not (project_state / "current_tick_id.txt").exists()
        errors = [r for r in caplog.records if r.levelname == "ERROR" and code in r.getMessage()]
        assert bool(errors) is (not no_progress)  # config faults are ERRORs, transient are not

    def test_in_flight_stale_and_active_registration_rules(self, tmp_path, mocker, fake_gh):
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        (project_state / "runs" / "01OLD").mkdir(parents=True)
        (project_state / "runs" / "01OLD" / "registration.json").write_text(
            json.dumps(_registration_payload(project_dir, 13))
        )
        claimed = ("tpo:todo", "ready-for-agent", "tpo:in-progress")
        seed_project_issues(
            fake_gh,
            [
                todo_payload(10, title="Eligible"),
                todo_payload(12, title="Kanban in-flight"),
                todo_payload(13, title="Registered", labels=claimed),
                todo_payload(14, title="Stale claim", labels=claimed),
            ],
        )
        mocker.patch("hermes_pipeline.decision.context.fetch_kanban_snapshot", return_value={"tasks": []})
        mocker.patch("hermes_pipeline.decision.context.build_in_flight", return_value=["TODO-12"])
        mocker.patch("hermes_pipeline.kanban_tasks.prepare_todo_phases", return_value=["prepared"])
        mocker.patch("hermes_pipeline.kanban_tasks.create_prepared_todo_phases", return_value=["task-1"])

        selection = _run_project_tick(
            project_dir=project_dir, config=Config(prompt_client="codex"),
            tick_id="01RULES", mocker=mocker,
        )

        context = selection.call_args.kwargs["ctx"]
        assert context.candidate_ids == ("TODO-10",)
        assert "#10 https://github.com/acme/repo/issues/10" in context.selection_markdown
        assert selection.call_args.kwargs["eligible_todo_ids"] == frozenset({"TODO-10"})

    def test_claimed_issue_is_unverified_when_kanban_is_unavailable(self, tmp_path, mocker, fake_gh):
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        project_state.mkdir()
        seed_project_issues(
            fake_gh,
            [todo_payload(10, title="Claimed", labels=("tpo:todo", "ready-for-agent", "tpo:in-progress"))],
        )
        mocker.patch("hermes_pipeline.decision.context.fetch_kanban_snapshot", return_value=None)

        selection = _run_project_tick(
            project_dir=project_dir, config=Config(prompt_client="codex"),
            tick_id="01NOKANBAN", mocker=mocker,
        )

        selection.assert_not_called()
        decision = json.loads((project_state / "decisions" / "01NOKANBAN.json").read_text())
        assert decision["rationale"] == "no_eligible_candidates"
        assert decision["blocked_reasons"] == {"TODO-10": "in_progress_unverified"}

    def test_label_failure_after_cards_is_only_a_warning(self, tmp_path, mocker, fake_gh, caplog):
        project_dir = _create_project(tmp_path, "demo")
        (project_dir / ".hermes").mkdir()
        fake_gh.on("gh", "issue", "edit", rc=1, stderr="HTTP 500")
        mocker.patch("hermes_pipeline.kanban_tasks.prepare_todo_phases", return_value=["prepared"])
        mocker.patch("hermes_pipeline.kanban_tasks.create_prepared_todo_phases", return_value=["task-1"])

        with caplog.at_level("WARNING", logger="hermes_pipeline.cli"):
            selection = _run_project_tick(
                project_dir=project_dir, config=Config(prompt_client="codex"),
                tick_id="01LABELFAIL", mocker=mocker,
            )

        selection.cb.observe.assert_called_once_with(picked="TODO-10", counts_as_no_progress=False)
        assert (project_dir / ".hermes" / "current_tick_id.txt").read_text().strip() == "01LABELFAIL"
        assert any(
            rec.levelname == "WARNING" and "tpo:in-progress" in rec.getMessage() and "gh_unavailable" in rec.getMessage()
            for rec in caplog.records
        )
        assert not any(rec.levelname == "ERROR" for rec in caplog.records)

    def test_pinned_issue_drift_blocks_delivery_and_skips_reconciliation(
        self, tmp_path, mocker, fake_gh
    ):
        project_dir = _create_project(tmp_path, "demo")
        _write_prior_registration(project_dir, body="### What\n\nOriginal\n")
        # Live issue: body changed and the claim label is missing (crash before labelling).
        seed_project_issues(fake_gh, [todo_payload(10, title="test", body="### What\n\nEdited\n")])
        flag = mocker.patch("hermes_pipeline.todos_completion.flag_issue_drift", return_value=False)
        results = mocker.patch("hermes_pipeline.kanban_tasks.reconcile_plan_task_results")
        mocker.patch("hermes_pipeline.ship.maybe_ship_ready")

        selection = _run_project_tick(
            project_dir=project_dir, config=Config(prompt_client="codex"),
            tick_id="01DRIFT", mocker=mocker,
        )

        assert flag.call_args.kwargs["code"] == "issue_drift"
        assert flag.call_args.kwargs["tick_id"] == "01PRIOR"
        assert flag.call_args.kwargs["repo"] == REPO
        results.assert_not_called()
        selection.assert_not_called()
        selection.cb.observe.assert_called_once_with(picked=None, counts_as_no_progress=True)
        # A drifted issue is never re-claimed: no label write, one live read.
        assert not any(call[:2] == ["issue", "edit"] for call in fake_gh.gh_calls())
        assert sum(1 for c in fake_gh.gh_calls() if c[-1] == f"repos/{REPO}/issues/10") == 1

    def test_unavailable_pinned_issue_warns_and_reconciliation_proceeds(
        self, tmp_path, mocker, fake_gh, caplog
    ):
        project_dir = _create_project(tmp_path, "demo")
        _write_prior_registration(project_dir)
        fake_gh.on(*API_ARGV, f"repos/{REPO}/issues/10", rc=1, stderr="HTTP 500")
        flag = mocker.patch("hermes_pipeline.todos_completion.flag_issue_drift")
        results = mocker.patch("hermes_pipeline.kanban_tasks.reconcile_plan_task_results", return_value=False)
        mocker.patch("hermes_pipeline.ship.maybe_ship_ready")

        with caplog.at_level("WARNING", logger="hermes_pipeline.cli"):
            selection = _run_project_tick(
                project_dir=project_dir, config=Config(prompt_client="codex"),
                tick_id="01UNAVAIL", mocker=mocker,
            )

        flag.assert_not_called()
        results.assert_called_once()
        selection.assert_not_called()
        assert "issue_unavailable:gh_unavailable" in caplog.text
        assert not any(call[:2] == ["issue", "edit"] for call in fake_gh.gh_calls())

    def test_unchanged_pinned_issue_keeps_reconciling(self, tmp_path, mocker, fake_gh):
        project_dir = _create_project(tmp_path, "demo")
        _write_prior_registration(project_dir)
        seed_project_issues(
            fake_gh,
            [todo_payload(10, title="test", labels=("tpo:todo", "ready-for-agent", "tpo:in-progress"))],
        )
        flag = mocker.patch("hermes_pipeline.todos_completion.flag_issue_drift")
        results = mocker.patch("hermes_pipeline.kanban_tasks.reconcile_plan_task_results", return_value=False)
        mocker.patch("hermes_pipeline.ship.maybe_ship_ready")

        _run_project_tick(
            project_dir=project_dir, config=Config(prompt_client="codex"),
            tick_id="01SAME", mocker=mocker,
        )

        flag.assert_not_called()
        results.assert_called_once()
        assert not any(call[:2] == ["issue", "edit"] for call in fake_gh.gh_calls())


def _git_project(tmp_path):
    """A real git repository with a tracked Plan so register_pinned_run can run unmocked."""
    project_dir = tmp_path / "demo"
    (project_dir / "docs").mkdir(parents=True)
    (project_dir / "docs" / "plan.md").write_text("# Plan\n")
    for command in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
        ("add", "docs/plan.md"),
        ("commit", "-qm", "base"),
    ):
        subprocess.run(["git", *command], cwd=project_dir, check=True, capture_output=True)
    (project_dir / ".hermes").mkdir()
    return project_dir


class TestTickCrashRecovery:
    def test_crash_between_registration_and_tick_persist_recovers_on_next_tick(
        self, tmp_path, mocker, fake_gh
    ):
        project_dir = _git_project(tmp_path)
        project_state = project_dir / ".hermes"
        TestTickPlanRequirement._configure_profile(project_dir, tmp_path, mocker)
        seed_project_issues(fake_gh, [todo_payload(10, title="test", body=PLAN_BODY)])
        mocker.patch(
            "hermes_pipeline.kanban_tasks.prepare_todo_phases",
            return_value=[SimpleNamespace(phase_key="task-1")],
        )
        create = mocker.patch(
            "hermes_pipeline.kanban_tasks.create_prepared_todo_phases", return_value=["task-1"]
        )
        from hermes_pipeline import cli as cli_module

        real_persist = cli_module._persist_tick_id
        attempts = []

        def crash_once(*args, **kwargs):
            if not attempts:
                attempts.append(1)
                raise OSError("disk full")
            return real_persist(*args, **kwargs)

        mocker.patch("hermes_pipeline.cli._persist_tick_id", side_effect=crash_once)

        with pytest.raises(OSError):
            _run_project_tick(
                project_dir=project_dir, config=Config(prompt_client="codex"),
                tick_id="01CRASH", mocker=mocker, patch_registration=False,
            )

        assert (project_state / "runs" / "01CRASH" / "registration.json").exists()
        create.assert_not_called()
        assert not any(call[:2] == ["issue", "edit"] for call in fake_gh.gh_calls())

        # Next tick: the orphaned registration is ownership proof — #10 is in_flight
        # (no worktree_mismatch loop); a different eligible issue proceeds instead.
        seed_project_issues(
            fake_gh,
            [
                todo_payload(10, title="test", body=PLAN_BODY),
                todo_payload(11, title="other", body=PLAN_BODY.replace("feat/todo-10", "feat/todo-11")),
            ],
        )
        selection = _run_project_tick(
            project_dir=project_dir, config=Config(prompt_client="codex"),
            tick_id="01RETRY", mocker=mocker, patch_registration=False, picked="TODO-11",
        )

        context = selection.call_args.kwargs["ctx"]
        assert context.candidate_ids == ("TODO-11",)
        assert (project_state / "runs" / "01RETRY" / "registration.json").exists()
        assert json.loads(
            (project_state / "runs" / "01RETRY" / "registration.json").read_text()
        )["issue_number"] == 11
        create.assert_called_once()
        assert (project_state / "current_tick_id.txt").read_text().strip() == "01RETRY"
        label_calls = [call for call in fake_gh.gh_calls() if call[:2] == ["issue", "edit"]]
        assert label_calls == [["issue", "edit", "11", "--repo", REPO, "--add-label", "tpo:in-progress"]]
        selection.cb.observe.assert_called_once_with(picked="TODO-11", counts_as_no_progress=False)

    def test_orphaned_registration_blocks_its_issue_even_after_new_commits(
        self, tmp_path, mocker, fake_gh
    ):
        project_dir = _git_project(tmp_path)
        project_state = project_dir / ".hermes"
        TestTickPlanRequirement._configure_profile(project_dir, tmp_path, mocker)
        seed_project_issues(fake_gh, [todo_payload(10, title="test", body=PLAN_BODY)])
        mocker.patch(
            "hermes_pipeline.kanban_tasks.prepare_todo_phases",
            return_value=[SimpleNamespace(phase_key="task-1")],
        )
        create = mocker.patch(
            "hermes_pipeline.kanban_tasks.create_prepared_todo_phases", return_value=["task-1"]
        )
        mocker.patch("hermes_pipeline.cli._persist_tick_id", side_effect=OSError("disk full"))
        with pytest.raises(OSError):
            _run_project_tick(
                project_dir=project_dir, config=Config(prompt_client="codex"),
                tick_id="01CRASH", mocker=mocker, patch_registration=False,
            )
        mocker.stopall()
        (project_dir / "docs" / "plan.md").write_text("# Plan v2\n")
        subprocess.run(["git", "commit", "-qam", "advance"], cwd=project_dir, check=True)
        mocker.patch(
            "hermes_pipeline.phases.resolve_profile_phases_path",
            return_value=tmp_path / "native-sdd" / "phases.yaml",
        )
        create = mocker.patch("hermes_pipeline.kanban_tasks.create_prepared_todo_phases")

        selection = _run_project_tick(
            project_dir=project_dir, config=Config(prompt_client="codex"),
            tick_id="01AFTER", mocker=mocker, patch_registration=False,
        )

        selection.assert_not_called()
        create.assert_not_called()
        decision = json.loads((project_state / "decisions" / "01AFTER.json").read_text())
        assert decision["blocked_reasons"] == {"TODO-10": "in_flight"}
        assert not (project_state / "runs" / "01AFTER").exists()


class TestTickFixRound1:
    """C2/C3/C4/C6/C7/C8 rulings."""

    @pytest.mark.parametrize("code", ["branch_invalid", "branch_exists", "plan_invalid", "authority_untracked"])
    def test_content_caused_registration_failure_demotes_issue(
        self, tmp_path, mocker, fake_gh, caplog, code
    ):
        from hermes_pipeline.run_registration import RunRegistrationError

        project_dir = _create_project(tmp_path, "demo")
        (project_dir / ".hermes").mkdir()
        TestTickPlanRequirement._configure_profile(project_dir, tmp_path, mocker)
        (project_dir / "docs").mkdir()
        (project_dir / "docs" / "plan.md").write_text("# Plan\n")
        seed_project_issues(fake_gh, [todo_payload(10, title="test", body=PLAN_BODY)])
        mocker.patch("hermes_pipeline.kanban_tasks.prepare_todo_phases", return_value=[SimpleNamespace(phase_key="t")])
        create = mocker.patch("hermes_pipeline.kanban_tasks.create_prepared_todo_phases")

        with caplog.at_level("ERROR", logger="hermes_pipeline.cli"):
            selection = _run_project_tick(
                project_dir=project_dir, config=Config(prompt_client="codex"), tick_id="01DEMOTE",
                mocker=mocker, registration_side_effect=RunRegistrationError(code, "x"),
            )

        create.assert_not_called()
        edits = [call for call in fake_gh.gh_calls() if call[:2] == ["issue", "edit"]]
        assert edits == [
            ["issue", "edit", "10", "--repo", REPO, "--remove-label", "ready-for-agent"],
            ["issue", "edit", "10", "--repo", REPO, "--add-label", "needs-info"],
        ]
        assert any(code in r.getMessage() and "needs-info" in r.getMessage() for r in caplog.records)
        selection.cb.observe.assert_called_once_with(picked=None, counts_as_no_progress=True)

    @pytest.mark.parametrize("code", ["git_error", "worktree_mismatch", "worktree_create_failed"])
    def test_infrastructure_registration_failure_does_not_demote(self, tmp_path, mocker, fake_gh, code):
        from hermes_pipeline.run_registration import RunRegistrationError

        project_dir = _create_project(tmp_path, "demo")
        (project_dir / ".hermes").mkdir()
        TestTickPlanRequirement._configure_profile(project_dir, tmp_path, mocker)
        (project_dir / "docs").mkdir()
        (project_dir / "docs" / "plan.md").write_text("# Plan\n")
        seed_project_issues(fake_gh, [todo_payload(10, title="test", body=PLAN_BODY)])
        mocker.patch("hermes_pipeline.kanban_tasks.prepare_todo_phases", return_value=[SimpleNamespace(phase_key="t")])

        _run_project_tick(
            project_dir=project_dir, config=Config(prompt_client="codex"), tick_id="01GITERR",
            mocker=mocker, registration_side_effect=RunRegistrationError(code, "x"),
        )

        assert not any(call[:2] == ["issue", "edit"] for call in fake_gh.gh_calls())

    def test_demotion_failure_is_a_warning(self, tmp_path, mocker, fake_gh, caplog):
        from hermes_pipeline.run_registration import RunRegistrationError

        project_dir = _create_project(tmp_path, "demo")
        (project_dir / ".hermes").mkdir()
        TestTickPlanRequirement._configure_profile(project_dir, tmp_path, mocker)
        (project_dir / "docs").mkdir()
        (project_dir / "docs" / "plan.md").write_text("# Plan\n")
        seed_project_issues(fake_gh, [todo_payload(10, title="test", body=PLAN_BODY)])
        fake_gh.on("gh", "issue", "edit", rc=1, stderr="HTTP 500")
        mocker.patch("hermes_pipeline.kanban_tasks.prepare_todo_phases", return_value=[SimpleNamespace(phase_key="t")])

        with caplog.at_level("WARNING", logger="hermes_pipeline.cli"):
            _run_project_tick(
                project_dir=project_dir, config=Config(prompt_client="codex"), tick_id="01DEMOTEFAIL",
                mocker=mocker, registration_side_effect=RunRegistrationError("plan_invalid", "x"),
            )

        assert any(r.levelname == "WARNING" and "needs-info" in r.getMessage() for r in caplog.records)

    def test_any_prompt_preparation_exception_is_recorded_not_raised(self, tmp_path, mocker):
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        project_state.mkdir()
        mocker.patch("hermes_pipeline.kanban_tasks.prepare_todo_phases", side_effect=ValueError("bad path"))
        create = mocker.patch("hermes_pipeline.kanban_tasks.create_prepared_todo_phases")

        selection = _run_project_tick(
            project_dir=project_dir, config=Config(prompt_client="codex"), tick_id="01PREPVAL", mocker=mocker,
        )

        create.assert_not_called()
        selection.cb.observe.assert_called_once_with(picked=None, counts_as_no_progress=True)
        outcomes = _read_outcomes(project_state)
        assert outcomes[-1].outcome == "failed_to_spawn"
        assert outcomes[-1].detail["reason"] == "phase_prompt_preparation_failed"
        assert outcomes[-1].detail["error_type"] == "ValueError"
        assert not (project_state / "current_tick_id.txt").exists()

    def test_branch_less_issue_is_blocked_before_registration(self, tmp_path, mocker, fake_gh):
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        project_state.mkdir()
        seed_project_issues(fake_gh, [todo_payload(10, title="test", body="### What\n\nNo branch\n")])

        selection = _run_project_tick(
            project_dir=project_dir, config=Config(prompt_client="codex"), tick_id="01NOBRANCH", mocker=mocker,
        )

        selection.assert_not_called()
        selection.registration.assert_not_called()
        decision = json.loads((project_state / "decisions" / "01NOBRANCH.json").read_text())
        assert decision["blocked_reasons"] == {"TODO-10": "branch_invalid"}

    def test_blocked_reconciler_counts_as_no_progress(self, tmp_path, mocker, fake_gh):
        project_dir = _create_project(tmp_path, "demo")
        _write_prior_registration(project_dir)
        seed_project_issues(
            fake_gh, [todo_payload(10, title="test", labels=("tpo:todo", "ready-for-agent", "tpo:in-progress"))]
        )
        results = mocker.patch("hermes_pipeline.kanban_tasks.reconcile_plan_task_results", return_value=False)
        mocker.patch("hermes_pipeline.ship.maybe_ship_ready")

        selection = _run_project_tick(
            project_dir=project_dir, config=Config(prompt_client="codex"), tick_id="01BLOCKED", mocker=mocker,
        )

        assert results.call_args.kwargs["repo"] == REPO  # C7: identity threaded, not re-resolved
        selection.cb.observe.assert_called_once_with(picked=None, counts_as_no_progress=True)
        assert fake_gh.calls.count(list(ORIGIN_ARGV)) == 1

    @pytest.mark.parametrize(("status_map", "observed"), [({}, True), ({"task-1": "running"}, False)])
    def test_in_flight_prior_tick_without_cards_is_a_stall(
        self, tmp_path, mocker, fake_gh, status_map, observed
    ):
        project_dir = _create_project(tmp_path, "demo")
        (project_dir / ".hermes").mkdir()
        (project_dir / ".hermes" / "current_tick_id.txt").write_text("01PRIOR")
        mocker.patch("hermes_pipeline.cli.all_phases_complete", return_value=False)
        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", return_value=status_map)
        mocker.patch("hermes_pipeline.ship.maybe_ship_ready")

        selection = _run_project_tick(
            project_dir=project_dir, config=Config(prompt_client="codex"), tick_id="01STALL", mocker=mocker,
        )

        selection.assert_not_called()
        if observed:
            selection.cb.observe.assert_called_once_with(picked=None, counts_as_no_progress=True)
        else:
            selection.cb.observe.assert_not_called()

    @pytest.mark.parametrize(
        ("live", "code"),
        [({"state": "closed"}, "issue_closed"),
         ({"labels": ("tpo:todo", "ready-for-agent", "tpo:on-hold")}, "issue_on_hold")],
    )
    def test_resume_fetches_once_and_never_relabels_a_closed_or_held_issue(
        self, tmp_path, mocker, fake_gh, live, code
    ):
        project_dir = _create_project(tmp_path, "demo")
        _write_prior_registration(project_dir)
        seed_project_issues(fake_gh, [todo_payload(10, title="test", **live)])
        flag = mocker.patch("hermes_pipeline.todos_completion.flag_issue_drift", return_value=False)
        mocker.patch("hermes_pipeline.ship.maybe_ship_ready")

        _run_project_tick(
            project_dir=project_dir, config=Config(prompt_client="codex"), tick_id="01HELD", mocker=mocker,
        )

        assert flag.call_args.kwargs["code"] == code
        fetches = [c for c in fake_gh.gh_calls() if c[:1] == ["api"] and c[-1] == f"repos/{REPO}/issues/10"]
        assert len(fetches) == 1
        assert not any(c[:2] == ["issue", "edit"] for c in fake_gh.gh_calls())

    def test_resume_relabel_logs_how_to_pause(self, tmp_path, mocker, fake_gh, caplog):
        project_dir = _create_project(tmp_path, "demo")
        _write_prior_registration(project_dir)
        mocker.patch("hermes_pipeline.kanban_tasks.reconcile_plan_task_results", return_value=False)
        mocker.patch("hermes_pipeline.ship.maybe_ship_ready")

        with caplog.at_level("INFO"):
            _run_project_tick(
                project_dir=project_dir, config=Config(prompt_client="codex"), tick_id="01RELABEL", mocker=mocker,
            )

        fetches = [c for c in fake_gh.gh_calls() if c[:1] == ["api"] and c[-1] == f"repos/{REPO}/issues/10"]
        assert len(fetches) == 1
        assert any(
            r.levelname == "INFO" and "re-added tpo:in-progress on #10" in r.getMessage()
            and "tpo:on-hold" in r.getMessage() for r in caplog.records
        )

    def test_kanban_fetch_failure_runs_a_single_kanban_list(self, tmp_path, mocker, fake_gh):
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        project_state.mkdir()
        seed_project_issues(
            fake_gh, [todo_payload(10, title="test", labels=("tpo:todo", "ready-for-agent", "tpo:in-progress"))]
        )
        run = mocker.patch(
            "hermes_pipeline.decision.context.subprocess.run", side_effect=FileNotFoundError("hermes")
        )

        selection = _run_project_tick(
            project_dir=project_dir, config=Config(prompt_client="codex"), tick_id="01ONEFETCH", mocker=mocker,
        )

        kanban_lists = [c for c in run.call_args_list if c.args[0][:3] == ["hermes", "kanban", "list"]]
        assert len(kanban_lists) == 1
        selection.assert_not_called()
        decision = json.loads((project_state / "decisions" / "01ONEFETCH.json").read_text())
        assert decision["blocked_reasons"] == {"TODO-10": "in_progress_unverified"}


class TestTickFixRound2:
    def test_drift_gate_failure_still_blocks_and_observes(self, tmp_path, mocker, fake_gh, caplog):
        project_dir = _create_project(tmp_path, "demo")
        _write_prior_registration(project_dir, body="### What\n\nOriginal\n")
        (project_dir / ".hermes" / "decisions").mkdir()
        (project_dir / ".hermes" / "decisions" / "01PRIOR.json").write_text("{}")
        seed_project_issues(fake_gh, [todo_payload(10, title="test", body="### What\n\nEdited\n")])
        mocker.patch(
            "hermes_pipeline.todos_completion.flag_issue_drift", side_effect=FileExistsError("decision")
        )
        results = mocker.patch("hermes_pipeline.kanban_tasks.reconcile_plan_task_results")
        mocker.patch("hermes_pipeline.ship.maybe_ship_ready")

        with caplog.at_level("ERROR", logger="hermes_pipeline.cli"):
            selection = _run_project_tick(
                project_dir=project_dir, config=Config(prompt_client="codex"), tick_id="01GATEFAIL", mocker=mocker,
            )

        results.assert_not_called()
        selection.cb.observe.assert_called_once_with(picked=None, counts_as_no_progress=True)
        messages = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
        assert any("delivery blocked" in m for m in messages)
        assert any("FileExistsError" in m for m in messages)

    def test_zero_card_drift_with_existing_decision_records_and_observes(
        self, tmp_path, mocker, fake_gh
    ):
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        _write_prior_registration(project_dir, body="### What\n\nOriginal\n")
        (project_state / "decisions").mkdir()
        (project_state / "decisions" / "01PRIOR.json").write_text("{}")
        seed_project_issues(fake_gh, [todo_payload(10, title="test", body="### What\n\nEdited\n")])
        mocker.patch(
            "hermes_pipeline.todos_completion.load_validated_registration",
            return_value=SimpleNamespace(todo_id="TODO-10", worktree=project_dir, prompt_client="codex"),
        )
        mocker.patch("hermes_pipeline.todos_completion.get_todo_kanban_tasks", return_value={})
        mocker.patch("hermes_pipeline.ship.maybe_ship_ready")

        selection = _run_project_tick(
            project_dir=project_dir, config=Config(prompt_client="codex"), tick_id="01ZEROCARD", mocker=mocker,
        )

        selection.cb.observe.assert_called_once_with(picked=None, counts_as_no_progress=True)
        record = json.loads((project_state / "decisions" / "01PRIOR-issue-drift.json").read_text())
        assert record["rationale"] == "tracker_error: issue_drift:issue_drift"


class TestResumeIssueCloseout:
    """After the PR merges, the resume tick closes the pinned issue and converges across ticks."""

    PR_URL = f"https://github.com/{REPO}/pull/7"

    def _remote_issue(self, fake_gh, *, lag_close=False, crash_after_close=False, edit_rc=0):
        from tests.gh_fakes import issue_payload

        remote = {"state": "open", "labels": ["tpo:todo", "tpo:in-progress"], "comments": [], "writes": []}
        seed_project_issues(fake_gh, [todo_payload(10, title="test")])
        fake_gh.on(*API_ARGV, f"repos/{REPO}/issues/10", handler=lambda argv: (
            0, json.dumps(issue_payload(10, title="test", body=ELIGIBLE_BODY, state=remote["state"],
                                        labels=remote["labels"])), ""
        ))
        fake_gh.on(*API_ARGV, "--paginate", "--slurp", f"repos/{REPO}/issues/10/comments",
                   handler=lambda argv: (0, json.dumps([[{"body": b} for b in remote["comments"]]]), ""))

        def comment(argv):
            with open(argv[argv.index("--body-file") + 1]) as handle:
                remote["comments"].append(handle.read())
            remote["writes"].append("comment")
            return 0, "", ""

        def close(argv):
            remote["writes"].append("close")
            if not remote.get("lag"):
                remote["state"] = "closed"
            if remote.get("crash"):
                remote["crash"] = False
                raise RuntimeError("crash after close")
            return 0, "", ""

        def edit(argv):
            remote["writes"].append("edit")
            if remote.get("edit_rc"):
                return remote["edit_rc"], "", "could not remove label"
            remote["labels"].remove("tpo:in-progress")
            return 0, "", ""

        remote.update(lag=lag_close, crash=crash_after_close, edit_rc=edit_rc)
        fake_gh.on("gh", "issue", "comment", handler=comment)
        fake_gh.on("gh", "issue", "close", handler=close)
        fake_gh.on("gh", "issue", "edit", handler=edit)
        return remote

    def _delivery_ready(self, mocker, project_dir, run_dir):
        (run_dir / "accepted-review-head").write_text("a" * 40)
        (run_dir / "delivery-authority.json").write_text(
            f'{{"base_branch":"main","origin_repository":"{REPO}"}}\n'
        )
        mocker.patch("hermes_pipeline.ship.maybe_ship_ready")
        mocker.patch("hermes_pipeline.kanban_tasks.reconcile_plan_task_results", return_value=True)
        mocker.patch("hermes_pipeline.review_reconciliation.reconcile_reviews", return_value=True)
        mocker.patch("hermes_pipeline.cli.all_phases_complete", return_value=False)
        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", return_value={"t": "running"})
        tc = "hermes_pipeline.todos_completion."
        mocker.patch(tc + "load_validated_registration", return_value=SimpleNamespace(
            todo_id="TODO-10", worktree=project_dir, branch="feat/todo-10", assignee="default",
            prompt_client="codex", issue_number=10, issue_url=f"https://github.com/{REPO}/issues/10",
            manifest=object(),
        ))
        mocker.patch(tc + "get_todo_kanban_tasks", return_value={
            "review-acceptance": SimpleNamespace(task_id="review", status="done"),
            "finish": SimpleNamespace(task_id="finish-id", status="done"),
            "human-gate": SimpleNamespace(task_id="human-id", status="blocked"),
        })
        mocker.patch(tc + "parse_worker_result", return_value=SimpleNamespace(
            delivery=SimpleNamespace(pr_url=self.PR_URL, branch="feat/todo-10", head_sha="a" * 40),
            git=SimpleNamespace(expected_parent_sha="a" * 40, resulting_head_sha="a" * 40,
                                task_commit_sha="a" * 40, changed_files=()),
        ))
        mocker.patch(tc + "_verify_finish")
        mocker.patch(tc + "_verify_pr_identity")
        mocker.patch(tc + "_github_identity", return_value=(REPO, "main"))
        mocker.patch(tc + "_pr_view", return_value={
            "state": "MERGED", "url": self.PR_URL, "headRefName": "feat/todo-10", "headRefOid": "a" * 40,
        })
        mocker.patch(tc + "_check_state", return_value="passed")
        return {
            "complete": mocker.patch(tc + "complete_todo_kanban_task", return_value=True),
            "mark": mocker.patch(tc + "_mark_gate_needs_input"),
            "flag": mocker.patch(tc + "flag_issue_drift"),
        }

    def _tick(self, project_dir, mocker, tick_id):
        return _run_project_tick(
            project_dir=project_dir, config=Config(prompt_client="codex"), tick_id=tick_id, mocker=mocker,
        )

    def test_propagation_lag_is_pending_then_the_next_tick_completes_the_gate(
        self, tmp_path, mocker, fake_gh, caplog
    ):
        project_dir = _create_project(tmp_path, "demo")
        run_dir = _write_prior_registration(project_dir)
        remote = self._remote_issue(fake_gh, lag_close=True)
        mocks = self._delivery_ready(mocker, project_dir, run_dir)

        self._tick(project_dir, mocker, "01LAG1")
        mocks["complete"].assert_not_called()
        assert (run_dir / "issue-close-started").exists()
        assert not (run_dir / "issue-closed").exists()

        remote["lag"] = False
        remote["state"] = "closed"  # the earlier close finally propagated
        with caplog.at_level("INFO", logger="hermes_pipeline.cli"):
            self._tick(project_dir, mocker, "01LAG2")

        mocks["flag"].assert_not_called()
        mocks["complete"].assert_called_once_with("demo", "human-id")
        assert (run_dir / "issue-closed").exists()
        assert "closeout in progress" in caplog.text
        assert remote["writes"].count("comment") == 1
        assert remote["writes"].count("close") == 1
        assert not any(c[:2] == ["issue", "edit"] and "--add-label" in c for c in fake_gh.gh_calls())

    def test_crash_after_close_converges_on_the_next_tick(self, tmp_path, mocker, fake_gh):
        project_dir = _create_project(tmp_path, "demo")
        run_dir = _write_prior_registration(project_dir)
        remote = self._remote_issue(fake_gh, crash_after_close=True)
        mocks = self._delivery_ready(mocker, project_dir, run_dir)

        with pytest.raises(RuntimeError):
            self._tick(project_dir, mocker, "01CRASH1")
        assert remote["state"] == "closed"
        assert not (run_dir / "issue-closed").exists()

        self._tick(project_dir, mocker, "01CRASH2")

        mocks["flag"].assert_not_called()
        mocks["complete"].assert_called_once_with("demo", "human-id")
        assert remote["writes"] == ["comment", "close", "edit"]
        assert len(remote["comments"]) == 1
        assert "tpo:in-progress" not in remote["labels"]

    def test_label_removal_failure_blocks_the_gate_then_recovers(self, tmp_path, mocker, fake_gh):
        project_dir = _create_project(tmp_path, "demo")
        run_dir = _write_prior_registration(project_dir)
        remote = self._remote_issue(fake_gh, edit_rc=1)
        mocks = self._delivery_ready(mocker, project_dir, run_dir)

        selection = self._tick(project_dir, mocker, "01LABEL1")
        mocks["complete"].assert_not_called()
        mocks["mark"].assert_called_once_with("human-id", "TPO delivery blocked: gh_rejected")
        selection.cb.observe.assert_called_once_with(picked=None, counts_as_no_progress=True)

        remote["edit_rc"] = 0
        self._tick(project_dir, mocker, "01LABEL2")

        mocks["flag"].assert_not_called()
        mocks["complete"].assert_called_once_with("demo", "human-id")
        assert remote["writes"] == ["comment", "close", "edit", "edit"]
        assert (run_dir / "issue-closed").exists()
