"""Tests for the pipeline execution contract wired into the tick flow."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from hermes_pipeline.cli import _cmd_tick, _tick_project
from hermes_pipeline.config import CircuitBreakerConfig, Config
from hermes_pipeline.phases import PhasePromptRenderError


def _make_decision(picked):
    decision = MagicMock()
    decision.picked = picked
    decision.rationale = "test"
    decision.candidates_considered = []
    return decision


def _create_project(projects_dir, name):
    project_dir = projects_dir / name
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "TODOS.md").write_text("# TODOS\n\n- [ ] TODO-10: test\n")
    return project_dir


def _read_outcomes(project_state):
    return [
        SimpleNamespace(**json.loads(path.read_text()))
        for path in sorted((project_state / "outcomes").glob("*.json"))
        if not path.name.endswith("-phases.json")
    ]


def _run_project_tick(*, project_dir, config, tick_id, mocker):
    cb = mocker.Mock()
    mocker.patch("hermes_pipeline.cli._make_circuit_breaker", return_value=cb)
    selection = mocker.patch(
        "hermes_pipeline.cli.run_selection",
        return_value=_make_decision("TODO-10"),
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
        result = _cmd_tick(FakeArgs(), config)

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
        result = _cmd_tick(FakeArgs(), config)

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
        result = _cmd_tick(FakeArgs(), config)

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
        self, tmp_path, mocker
    ):
        tick_id = "01PLANFAIL"
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        project_state.mkdir()
        self._configure_profile(project_dir, tmp_path, mocker)
        (project_dir / "TODOS.md").write_text(
            "# TODOS\n\n- [ ] **TODO-10: Test**\n"
        )
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

    def test_valid_plan_is_passed_to_prompt_preparation(self, tmp_path, mocker):
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        project_state.mkdir()
        self._configure_profile(project_dir, tmp_path, mocker)
        plan = project_dir / "docs" / "plan.md"
        plan.parent.mkdir()
        plan.write_text("# Plan\n")
        (project_dir / "TODOS.md").write_text(
            "# TODOS\n\n"
            "- [ ] **TODO-10: Test** — summary\n"
            "  - **Plan:** docs/plan.md\n"
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

    def test_requires_plan_passes_only_compiled_candidates_to_selection(
        self, tmp_path, mocker
    ):
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        project_state.mkdir()
        self._configure_profile(project_dir, tmp_path, mocker)
        docs = project_dir / "docs"
        docs.mkdir()
        (docs / "plan.md").write_text("# Legacy plan\n")
        (project_dir / "TODOS.md").write_text(
            "# TODOS\n\n"
            "- [ ] **TODO-10: Eligible**\n  - **Plan:** docs/plan.md\n"
            "- [ ] **TODO-11: Missing plan**\n"
            "- [x] **TODO-12: Complete**\n  - **Plan:** docs/plan.md\n"
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
        assert "TODO-10" in context.todos_md
        assert "TODO-11" not in context.todos_md
        assert "TODO-12" not in context.todos_md
        assert selection.call_args.kwargs["eligible_todo_ids"] == frozenset({"TODO-10"})

    def test_requires_plan_with_zero_candidates_skips_selection_call(
        self, tmp_path, mocker
    ):
        project_dir = _create_project(tmp_path, "demo")
        project_state = project_dir / ".hermes"
        project_state.mkdir()
        self._configure_profile(project_dir, tmp_path, mocker)
        (project_dir / "TODOS.md").write_text(
            "# TODOS\n\n- [ ] **TODO-10: Missing plan**\n"
        )
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
