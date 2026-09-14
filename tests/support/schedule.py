"""Shared helpers for phase schedule testing."""

from types import SimpleNamespace

import yaml


def profile_file(tmp_path, keys=("design", "build", "audit", "docs", "publish")):
    """Create a test phase profile YAML file."""
    roles = {"build": "implementation", "audit": "review", "publish": "delivery"}
    phases = [dict(phase_key=k, name=k, prompt=f"Pinned {k}", tools="Read", turns=10,
                   **({"role": roles[k]} if k in roles else {})) for k in keys]
    phases.append(dict(phase_key="human", name="Human", gate=True, terminal=True, kind="human_gate"))
    path = tmp_path / "phases.yaml"
    path.write_text(yaml.safe_dump(dict(requires_plan=True, phases=phases)))
    return path


def schedule_fixture(tmp_path, monkeypatch, keys=("design", "build", "audit", "docs", "publish")):
    """Create a schedule fixture for testing phase scheduling.

    Returns (registration, tasks, evidence, created, tick, complete).
    """
    from contextlib import nullcontext
    from importlib import import_module

    from hermes_pipeline import phase_schedule as schedule
    from hermes_pipeline.phases import load_phase_profile

    # Load consumer aliases before patching their source modules, so lazy
    # imports cannot retain fixture fakes after monkeypatch restores them.
    import_module("hermes_pipeline.todos_completion")
    import_module("hermes_pipeline._agent_supervisor")
    import_module("hermes_pipeline.phase_recovery")

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
        tasks[kw["key"]] = SimpleNamespace(task_id=kw["key"], status="ready", generation=kw.get("generation", 1))
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
