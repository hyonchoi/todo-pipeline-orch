"""Profile authority regressions: keys, pinned definitions and ordered admission."""
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest
import yaml

from hermes_pipeline.kanban_tasks import all_phases_complete, planned_phase_keys
from hermes_pipeline.phases import load_phase_profile, resolve_profile_phases_path
from tests.test_run_registration import _embedded_issue, _register, _repo


def profile_file(tmp_path, keys=("design", "build", "audit", "docs", "publish")):
    roles = {"build": "implementation", "audit": "review", "publish": "delivery"}
    phases = [dict(phase_key=k, name=k, prompt=f"Pinned {k}", tools="Read", turns=10,
                   **({"role": roles[k]} if k in roles else {})) for k in keys]
    phases.append(dict(phase_key="human", name="Human", gate=True, terminal=True, kind="human_gate"))
    path = tmp_path / "phases.yaml"
    path.write_text(yaml.safe_dump(dict(requires_plan=True, phases=phases)))
    return path


def test_registration_pins_complete_ordered_native_profile(tmp_path):
    repo, _ = _repo(tmp_path)
    profile = load_phase_profile(resolve_profile_phases_path("native-sdd"))
    keys = tuple(p.phase_key for p in profile.phases if not p.gate)
    registration = _register(repo, issue=_embedded_issue(), plan_path=None, step_keys=keys,
                             phase_definitions=profile.phases)
    assert registration.schema_version == 6
    raw = json.loads((repo / ".hermes/runs/01TICK/registration.json").read_text())
    assert raw["phase_definitions"] == [asdict(p) for p in profile.phases]
    assert raw["step_keys"] == ["phase_4_development", "phase_5_review", "phase_8_finish_branch"]


def test_renamed_roles_and_extra_workers_are_all_registered(tmp_path):
    path = profile_file(tmp_path)
    assert planned_phase_keys(path, SimpleNamespace(manifest=object())) == (
        "design", "build", "audit", "docs", "publish")
    profile = load_phase_profile(path)
    assert [p.role for p in profile.phases] == ["worker", "implementation", "review", "worker", "delivery", "worker"]


@pytest.mark.parametrize("mutation", ["duplicate_key", "duplicate_role", "unknown_role"])
def test_invalid_profile_definitions_fail_before_registration(tmp_path, mutation):
    path = profile_file(tmp_path)
    raw = yaml.safe_load(path.read_text())
    if mutation == "duplicate_key":
        raw["phases"][1]["phase_key"] = "design"
    elif mutation == "duplicate_role":
        raw["phases"][0]["role"] = "implementation"
    else:
        raw["phases"][0]["role"] = "typo"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError):
        load_phase_profile(path)


def test_missing_deferred_card_is_not_complete(tmp_path, monkeypatch):
    path = tmp_path / "runs/tick/registration.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(dict(schema_version=6, step_keys=["build", "audit", "publish"])))
    monkeypatch.setattr("hermes_pipeline.kanban_tasks.get_todo_kanban_status", lambda *a: {"build": "done"})
    assert not all_phases_complete("board", "tick", state_dir=tmp_path)


def test_pinned_definitions_survive_profile_file_drift_and_retry(tmp_path):
    from hermes_pipeline.result_contract import load_validated_registration
    from hermes_pipeline.review_reconciliation import profile_phase

    repo, _ = _repo(tmp_path)
    path = profile_file(tmp_path)
    phases = load_phase_profile(path).phases
    first = _register(repo, issue=_embedded_issue(), plan_path=None, phase_definitions=phases)
    path.write_text("phases: []")
    changed = tuple(__import__("dataclasses").replace(p, prompt="MUTATED") for p in phases)
    retry = _register(repo, issue=_embedded_issue(), plan_path=None, phase_definitions=changed)
    assert retry == first
    validated = load_validated_registration(repo, repo / ".hermes", "01TICK", repo="acme/repo")
    assert profile_phase(validated, "audit")[1].prompt == "Pinned audit"
    with pytest.raises(Exception, match="profile_phase_missing"):
        profile_phase(validated, "phase_5_review")


@pytest.mark.parametrize("field,value", [("gate", "false"), ("timeout", True), ("timeout", 0),
                                           ("terminal", "true"), ("turns", -1)])
def test_pinned_definition_field_types_are_validated(tmp_path, field, value):
    from hermes_pipeline.phase_schedule import decode_definitions
    phases = [asdict(p) for p in load_phase_profile(profile_file(tmp_path)).phases]
    phases[0][field] = value
    with pytest.raises(ValueError):
        decode_definitions(phases)


def test_manifest_requires_reachable_implementation_role(tmp_path):
    from hermes_pipeline.run_registration import RunRegistrationError
    repo, _ = _repo(tmp_path)
    phases = load_phase_profile(profile_file(tmp_path, keys=("design",))).phases
    with pytest.raises(RunRegistrationError, match="implementation_role_missing"):
        _register(repo, issue=_embedded_issue(), plan_path=None, phase_definitions=phases)
    assert not (repo / ".hermes/runs/01TICK/registration.json").exists()


def schedule_fixture(tmp_path, monkeypatch, keys=("design", "build", "audit", "docs", "publish")):
    from contextlib import nullcontext
    from importlib import import_module

    from hermes_pipeline import phase_schedule as schedule

    # Load consumer aliases before patching their source modules, so lazy
    # imports cannot retain fixture fakes after monkeypatch restores them.
    import_module("hermes_pipeline.todos_completion")

    phases = load_phase_profile(profile_file(tmp_path, keys)).phases
    registration = SimpleNamespace(phase_definitions=phases, step_keys=tuple(p.phase_key for p in phases if not p.gate),
        base_sha="base", repository=tmp_path, worktree=tmp_path, todo_id="TODO-42", manifest=None,
        branch="task", prompt_client="codex", profile="custom", assignee="worker", review_assignee="reviewer")
    tasks, evidence, created = {}, {}, []
    current = ["base"]
    monkeypatch.setattr("hermes_pipeline.authority_result.locked_run_authority", lambda **kw: nullcontext())
    monkeypatch.setattr("hermes_pipeline.authority_result.require_authorized_result", lambda **kw: nullcontext())
    monkeypatch.setattr("hermes_pipeline.kanban_tasks.get_todo_kanban_tasks", lambda *a: tasks)
    monkeypatch.setattr("hermes_pipeline.result_contract._git", lambda *a: current[0])
    monkeypatch.setattr(schedule, "_promoted_result", lambda *a, key, **kw: evidence[key])
    def create(**kw):
        created.append(kw)
        tasks[kw["key"]] = SimpleNamespace(task_id=kw["key"], status="ready")
    monkeypatch.setattr("hermes_pipeline.review_reconciliation._create_task", create)
    def tick():
        return schedule.reconcile_schedule(project_dir=tmp_path, state_dir=tmp_path / ".hermes",
            tenant="board", tick_id="tick", registration=registration, repo="acme/repo")
    def complete(key):
        parent = current[0]
        current[0] += "-" + key
        tasks[key].status = "done"
        evidence[key] = ({"head_sha": current[0]}, {"base_sha": parent, "result_kind": "phase"})
    return registration, tasks, evidence, created, tick, complete


def test_schedule_advances_in_profile_order_with_renamed_roles_and_extra_workers(tmp_path, monkeypatch):
    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch)
    for index, key in enumerate(registration.step_keys):
        assert tick()
        assert [c["key"] for c in created] == list(registration.step_keys[:index + 1])
        assert key in created[-1]["prompt"]
        # Reentry while the active card runs creates neither another card nor execution.
        assert tick()
        assert len(created) == index + 1
        complete(key)
    assert tick()
    assert len(created) == len(registration.step_keys)


def test_schedule_without_review_or_delivery_creates_no_synthetic_workers(tmp_path, monkeypatch):
    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch, ("build", "docs"))
    assert tick()
    complete("build")
    assert tick()
    complete("docs")
    assert tick()
    assert [c["key"] for c in created] == ["build", "docs"]


def test_schedule_rejects_unvalidated_predecessor_and_out_of_order_cards(tmp_path, monkeypatch):
    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch)
    assert tick()
    tasks["design"].status = "done"
    assert not tick()  # Missing promoted result cannot authorize the next worker.
    assert len(created) == 1
    tasks["audit"] = SimpleNamespace(task_id="rogue", status="done")
    tasks["design"].status = "ready"
    assert not tick()
    assert len(created) == 1


def test_modern_supervisor_implementation_uses_actual_phase_entry_head(tmp_path, monkeypatch):
    from hermes_pipeline import _agent_supervisor as supervisor
    from hermes_pipeline.agent_execution import ExecutionStore
    from tests.test_run_registration import _git

    repo, base = _repo(tmp_path)
    phases = load_phase_profile(profile_file(tmp_path)).phases
    run = _register(repo, issue=_embedded_issue(), plan_path=None, phase_definitions=phases)
    _git(run.worktree, "commit", "--allow-empty", "-m", "planning worker commit")
    entry = _git(run.worktree, "rev-parse", "HEAD")
    assert entry != base
    monkeypatch.setattr(supervisor, "installed_entrypoint", lambda: "/installed/tpo")
    kwargs = dict(project_dir=repo, state_dir=repo / ".hermes", root=repo / ".hermes/agent-executions",
                  tick_id="01TICK", phase="build", prompt="Pinned build", client="codex", tools="Read",
                  worktree=run.worktree, timeout=30, todo_id="TODO-42")
    identity = supervisor.register_execution(**kwargs)
    pinned = ExecutionStore(kwargs["root"]).load(identity)["registration"]
    assert pinned["phase"] == "build"
    assert pinned["manifest"]["tasks"][0]["id"] == "task-1"
    assert pinned["result_contract"]["phase_role"] == "implementation"
    assert pinned["result_contract"]["base_sha"] == entry
    assert pinned["result_contract"]["result_kind"] == "worker"
    assert supervisor.register_execution(**kwargs) == identity
    assert ExecutionStore(kwargs["root"]).load(identity)["attempts"] == []
    supervisor.validate_registration(ExecutionStore(kwargs["root"]), identity)


@pytest.mark.parametrize("include_delivery", [False, True])
def test_real_promoted_results_drive_exact_profile_cards_and_phase_entry_heads(tmp_path, monkeypatch, include_delivery):
    """Real Git, registration, promotion and authority; fake only Hermes transport."""
    import subprocess
    from dataclasses import replace

    from hermes_pipeline import _agent_supervisor as supervisor
    from hermes_pipeline import kanban_tasks, review_reconciliation
    from hermes_pipeline.agent_checkpoint import ProgressJournal
    from hermes_pipeline.agent_execution import ExecutionStore
    from hermes_pipeline.result_contract import load_validated_registration
    from tests.test_result_contract import _result
    from tests.test_run_registration import _git

    repo, _ = _repo(tmp_path)
    keys = ("design", "build", "audit", "docs") + (("publish",) if include_delivery else ())
    phases = load_phase_profile(profile_file(tmp_path, keys)).phases
    run = _register(repo, issue=_embedded_issue(), plan_path=None, phase_definitions=phases)
    state = repo / ".hermes"
    store = ExecutionStore(state / "agent-executions")
    tasks, reports, headers = {}, {}, []
    monkeypatch.setattr(supervisor, "installed_entrypoint", lambda: "/installed/tpo")
    monkeypatch.setattr(kanban_tasks, "get_todo_kanban_tasks", lambda *a: tasks)
    monkeypatch.setattr(review_reconciliation, "_find_task_id_in_snapshot", lambda **kw: None)
    monkeypatch.setattr(kanban_tasks, "_show_task_payload", lambda task_id: reports[task_id])
    # Git and supervisor authority stay real; only GitHub read APIs are stubbed.
    from hermes_pipeline import todos_completion as completion
    monkeypatch.setattr(completion, "get_todo_kanban_tasks", lambda *a: tasks)
    monkeypatch.setattr(completion, "_show_task_payload", lambda task_id: reports[task_id])
    monkeypatch.setattr(completion, "_github_identity", lambda *a: ("acme/repo", "main"))
    monkeypatch.setattr(completion, "_verify_pr_identity", lambda *a, **kw: None)
    monkeypatch.setattr(completion, "_pr_view", lambda *a: dict(state="OPEN", url="https://github.com/acme/repo/pull/7",
        headRefName=run.branch, headRefOid=_git(run.worktree, "rev-parse", "HEAD")))
    monkeypatch.setattr(completion, "_remote_head", lambda *a: _git(run.worktree, "rev-parse", "HEAD"))
    monkeypatch.setattr(completion, "_check_state", lambda *a, **kw: "passed")
    real_run = subprocess.run
    def fake_hermes(argv, **kwargs):
        if argv[:3] != ["hermes", "kanban", "create"]:
            return real_run(argv, **kwargs)
        header = json.loads(argv[argv.index("--body") + 1].split("\n", 1)[0])
        key = header["phase_key"]
        task_id = f"t_{len(headers) + 1:08x}"
        headers.append(header)
        tasks[key] = kanban_tasks.KanbanTaskInfo(task_id, key, "ready", "TODO-42")
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"id": task_id}))
    monkeypatch.setattr(subprocess, "run", fake_hermes)
    def tick():
        return kanban_tasks.reconcile_plan_task_results(project_dir=repo, state_dir=state,
            tenant="board", tick_id="01TICK", repo="acme/repo")
    for phase in (p for p in phases if not p.gate):
        assert tick()
        assert headers[-1]["phase_key"] == phase.phase_key
        identity = headers[-1]["execution_id"]
        record = store.load(identity)
        parent = _git(run.worktree, "rev-parse", "HEAD")
        assert record["registration"]["result_contract"]["base_sha"] == parent
        assert record["registration"]["result_contract"]["phase_role"] == phase.role
        assert tick() and not store.load(identity)["attempts"]
        store.admit(identity)
        changed = []
        for index in range(2 if phase.role == "worker" else 1):
            filename = f"{phase.phase_key}-{index}.txt"
            (run.worktree / filename).write_text("phase change")
            _git(run.worktree, "add", filename)
            _git(run.worktree, "commit", "-m", f"{phase.phase_key} change")
            changed.append(filename)
        head = _git(run.worktree, "rev-parse", "HEAD")
        if phase.role == "implementation":
            journal = ProgressJournal(store, identity)
            journal.record_receipt(1, "task-1", head, kind="verification",
                evidence={"checks": [{"argv": ["pytest"], "exit_code": 0}]})
            journal.record_receipt(1, "task-1", head, kind="review",
                evidence={"reviewer": "independent", "receipt_id": "review-1", "outcome": "accepted"})
            checkpoint = journal.staging_directory(1) / "checkpoint.json"
            checkpoint.write_text(json.dumps(dict(version=1, execution_id=identity, generation=1,
                plan_identity=run.plan_hash, task_id="task-1", commit=head)))
            journal.promote(1, checkpoint.name)
        if phase.role == "worker":
            result = dict(schema_version=1, execution_id=identity, generation=1, tick_id="01TICK",
                todo_id="TODO-42", step_key=phase.phase_key, verdict="success", head_sha=head)
        else:
            result = _result(step_key=phase.phase_key, git=dict(expected_parent_sha=parent,
                resulting_head_sha=head, task_commit_sha=head, changed_files=changed),
                acceptance=[dict(criterion="Works", status="passed")] if phase.role == "implementation" else [])
        if phase.role == "delivery":
            result["delivery"] = dict(pr_url="https://github.com/acme/repo/pull/7", branch=run.branch,
                head_sha=head, checks=[dict(command="pytest", exit_code=0)])
        stage = supervisor.staging_directory(store, identity, 1)
        (stage / "result.json").write_text(json.dumps(result))
        supervisor.validated_result(store, identity, 1, promote=True)
        store.update_attempt(identity, 1, status="exited", exit_code=0, cleanup="confirmed")
        task = tasks[phase.phase_key]
        reports[task.task_id] = {"runs": [{"status": "completed", "metadata": {"tpo_result": result}}]}
        tasks[phase.phase_key] = replace(task, status="done")
    assert tick()
    assert [h["phase_key"] for h in headers] == list(keys)
    from hermes_pipeline.phase_schedule import validated_predecessor_head
    validated = load_validated_registration(repo, state, "01TICK", repo="acme/repo")
    assert validated_predecessor_head(validated, state_dir=state, tick_id="01TICK") == head
    assert len(headers) == len(keys)


def test_deferred_native_claude_implementation_retains_agent_grant(tmp_path, monkeypatch):
    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch)
    registration.profile = "native-sdd"
    registration.prompt_client = "claude"
    assert tick()
    assert "Agent" not in created[-1]["tools"].split(",")
    complete("design")
    assert tick()
    assert created[-1]["key"] == "build"
    assert "Agent" in created[-1]["tools"].split(",")


@pytest.mark.parametrize("pr_state", ["OPEN", "MERGED"])
@pytest.mark.parametrize("suffix_status,head,expected", [("ready", "a" * 40, True),
    ("done", "b" * 40, False)])
def test_delivery_does_not_close_before_or_beyond_profile_suffix(tmp_path, mocker, suffix_status, head, expected, pr_state):
    from hermes_pipeline import todos_completion as completion
    from hermes_pipeline.phases import Phase
    from tests.test_todos_completion import _finish_done_fixture, _view

    tasks = {"publish": SimpleNamespace(task_id="finish-id", status="done"),
             "after": SimpleNamespace(task_id="after-id", status=suffix_status)}
    state = _finish_done_fixture(tmp_path, mocker, tasks=tasks, view=_view(pr_state))
    registration = completion.load_validated_registration.return_value
    registration.phase_definitions = (Phase("publish", "Publish", role="delivery"), Phase("after", "After"))
    registration.step_keys = ("publish", "after")
    mocker.patch("hermes_pipeline.phase_schedule.validated_predecessor_head", return_value="a" * 40)
    mocker.patch.object(completion, "_show_task_payload", return_value={})
    mocker.patch.object(completion, "_check_state", return_value="passed")
    mocker.patch.object(completion, "_git", return_value=head)
    close = mocker.patch.object(completion, "close_issue_for_delivery")
    assert completion._reconcile_todo_completion_locked(project_dir=tmp_path, state_dir=state,
        tenant="board", tick_id="01TICK", repo="acme/repo", registration=registration) is expected
    close.assert_not_called()


@pytest.mark.parametrize("retry_definitions", ["omitted", "invalid"])
def test_schema6_retry_reuses_pins_before_reading_new_profile(tmp_path, retry_definitions):
    from dataclasses import replace
    repo, _ = _repo(tmp_path)
    phases = load_phase_profile(profile_file(tmp_path)).phases
    initial = _register(repo, issue=_embedded_issue(), plan_path=None, phase_definitions=phases)
    kwargs = {} if retry_definitions == "omitted" else {"phase_definitions": tuple(
        replace(p, role="worker") for p in phases)}
    retry = _register(repo, issue=_embedded_issue(), plan_path=None, **kwargs)
    assert retry == initial


def test_schema5_registration_binds_only_legacy_initial_worker(tmp_path, monkeypatch):
    from hermes_pipeline import _agent_supervisor as supervisor
    from hermes_pipeline.kanban_tasks import (
        bind_prepared_executions,
        prepare_todo_phases,
    )
    from hermes_pipeline.result_contract import load_validated_registration
    repo, _ = _repo(tmp_path)
    run = _register(repo, issue=_embedded_issue(), plan_path=None, step_keys=("phase_4_development",))
    authority = load_validated_registration(repo, repo / ".hermes", "01TICK", repo="acme/repo")
    phases = load_phase_profile(resolve_profile_phases_path("native-sdd"))
    prepared = prepare_todo_phases(todo_id="TODO-42", tick_id="01TICK", board_slug="board",
        phase_definitions=phases.phases, plan_source=authority.plan_source,
        plan_reference=authority.plan_reference, project_dir=repo, prompt_client="codex", profile_name="native-sdd")
    monkeypatch.setattr(supervisor, "installed_entrypoint", lambda: "/installed/tpo")
    bound = bind_prepared_executions(prepared, project_dir=repo, state_dir=repo / ".hermes",
        root=repo / ".hermes/agent-executions", tick_id="01TICK", worktree=run.worktree, todo_id="TODO-42")
    assert [p.phase_key for p in bound] == ["phase_4_development"]


def test_legacy_execution_reentry_preserves_record_without_role_field(tmp_path, monkeypatch):
    from hermes_pipeline import _agent_supervisor as supervisor
    from hermes_pipeline.agent_execution import ExecutionStore
    from tests.test_agent_supervisor import _committed_profile
    store, identity, worktree = _committed_profile(tmp_path, monkeypatch)
    path = store.root / identity / "record.json"
    raw = json.loads(path.read_text())
    raw["registration"]["result_contract"].pop("phase_role")
    path.write_text(json.dumps(raw))
    before = path.read_bytes()
    assert supervisor.register_execution(project_dir=worktree, state_dir=tmp_path / "control", root=store.root,
        tick_id="tick-test", phase="analysis", prompt="Exact prompt: $() `echo no`\x00\n",
        client="codex", tools="Bash", worktree=worktree, timeout=10, todo_id="TODO-1") == identity
    assert path.read_bytes() == before
    assert ExecutionStore(store.root).load(identity)["attempts"] == []


def test_delivery_admission_cannot_close_before_suffix_evidence_is_validated(tmp_path, monkeypatch):
    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch,
        ("build", "publish", "after"))
    registration.manifest = object()
    for key in registration.step_keys:
        tasks[key] = SimpleNamespace(task_id=key, status="done")
    evidence["build"] = ({"head_sha": "build-head"}, {"base_sha": "base", "result_kind": "phase"})
    evidence["publish"] = ({"head_sha": "publish-head"}, {"base_sha": "build-head", "result_kind": "phase"})
    verified = tmp_path / ".hermes/runs/tick/finish-verified"
    verified.parent.mkdir(parents=True)
    verified.touch()
    close_modes = []
    def delivery(**kwargs):
        close_modes.append(kwargs.get("allow_close", True))
        return True
    monkeypatch.setattr("hermes_pipeline.todos_completion._reconcile_todo_completion_locked", delivery)
    assert not tick()  # Board says done, but suffix has no promoted result.
    assert close_modes == [False]
    evidence["after"] = ({"head_sha": "publish-head"}, {"base_sha": "publish-head", "result_kind": "phase"})
    assert tick()
    assert close_modes == [False, False, True]


def test_delivery_validation_can_admit_missing_suffix_without_closing(tmp_path, monkeypatch):
    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch,
        ("build", "publish", "after"))
    assert tick()
    complete("build")
    assert tick()
    complete("publish")
    registration.manifest = object()
    close_modes = []
    def delivery(**kwargs):
        close_modes.append(kwargs.get("allow_close", True))
        marker = tmp_path / ".hermes/runs/tick/finish-verified"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return True
    monkeypatch.setattr("hermes_pipeline.todos_completion._reconcile_todo_completion_locked", delivery)
    assert tick()
    assert [c["key"] for c in created] == ["build", "publish", "after"]
    assert close_modes == [False]


def test_deferred_prompt_retains_snapshot_spec_references_and_decisions(tmp_path):
    from hermes_pipeline.result_contract import load_validated_registration
    from hermes_pipeline.review_reconciliation import render_profile_prompt
    from tests.test_run_registration import _git, _issue
    repo, _ = _repo(tmp_path)
    for name in ("spec.md", "reference.md"):
        (repo / "docs" / name).write_text("Pinned context")
    _git(repo, "add", "docs")
    _git(repo, "commit", "-m", "context")
    issue = _issue(body="### Spec\n\ndocs/spec.md\n\n### Reference\n\ndocs/reference.md\n\n### Security Review\n\nrequired\n\n" + _embedded_issue().body)
    phases = load_phase_profile(profile_file(tmp_path)).phases
    _register(repo, issue=issue, plan_path=None, phase_definitions=phases)
    registration = load_validated_registration(repo, repo / ".hermes", "01TICK", repo="acme/repo")
    prompt = render_profile_prompt(registration, phases[2], "pinned-profile", tick_id="01TICK", tenant="board", facts={})
    assert "docs/spec.md" in prompt
    assert "docs/reference.md" in prompt
    assert "Security Review" in prompt
    assert "- Security Review: required" in prompt
