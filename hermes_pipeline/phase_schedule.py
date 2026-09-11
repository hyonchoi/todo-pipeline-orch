"""The selected profile is the ordered authority for modern registered runs."""
from __future__ import annotations

from dataclasses import fields

from .phases import Phase


def validate_definitions(phases) -> None:
    if not phases:
        raise ValueError("phase definitions missing")
    keys, roles = set(), set()
    for phase in phases:
        if not isinstance(phase.phase_key, str) or not phase.phase_key or phase.phase_key in keys:
            raise ValueError("phase keys must be nonempty and unique")
        keys.add(phase.phase_key)
        if (any(not isinstance(getattr(phase, field), str) for field in ("name", "prompt", "tools"))
                or not phase.name or type(phase.gate) is not bool or type(phase.terminal) is not bool
                or type(phase.compile_plan_tasks) is not bool
                or type(phase.timeout) is not int or phase.timeout <= 0
                or type(phase.turns) is not int or phase.turns < 0
                or (phase.kind is not None and not isinstance(phase.kind, str))
                or phase.kind not in {None, "worker", "controller_gate", "human_gate"}):
            raise ValueError("invalid phase definition fields")
        if not isinstance(phase.role, str) or phase.role not in {"worker", "implementation", "review", "delivery"}:
            raise ValueError("unknown phase role")
        if phase.role != "worker":
            if phase.gate or phase.role in roles:
                raise ValueError("phase roles must name a unique worker")
            roles.add(phase.role)


def decode_definitions(raw) -> tuple[Phase, ...]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError("phase definitions missing")
    expected = {f.name for f in fields(Phase)}
    if any(not isinstance(p, dict) or set(p) != expected for p in raw):
        raise ValueError("invalid pinned phase definition")
    phases = tuple(Phase(**p) for p in raw)
    validate_definitions(phases)
    return phases


def worker_phases(phases) -> tuple[Phase, ...]:
    # Gates have no worker. A terminal human boundary ends execution.
    result = []
    for phase in phases:
        if phase.gate:
            if phase.terminal:
                break
            continue
        result.append(phase)
    return tuple(result)


def phase_tools(phase, *, profile: str, prompt_client: str) -> str:
    """Retain the native Claude implementation grant at either admission path."""
    if (profile == "native-sdd" and prompt_client == "claude" and phase.role == "implementation"
            and "Agent" not in phase.tools.split(",")):
        return phase.tools + ",Agent"
    return phase.tools


def role_key(registration, role: str) -> str | None:
    phases = getattr(registration, "phase_definitions", ())
    if phases:
        return next((p.phase_key for p in worker_phases(phases) if p.role == role), None)
    # Explicit compatibility adapter for registrations through schema 5.
    return {"implementation": "phase_4_development", "review": "review:0", "delivery": "finish"}.get(role)


def phase_role(registration, key: str) -> str:
    phases = getattr(registration, "phase_definitions", ())
    if phases:
        return next((p.role for p in worker_phases(phases) if p.phase_key == key), "worker")
    return next((r for r in ("implementation", "review", "delivery") if role_key(registration, r) == key), "worker")


def execution_keys(registration) -> tuple[str, ...]:
    if getattr(registration, "phase_definitions", ()):
        return registration.step_keys
    return tuple(dict.fromkeys((*getattr(registration, "step_keys", ("phase_4_development",)), "review:0", "finish")))


def _promoted_result(registration, *, state_dir, tick_id, key):
    import json
    from pathlib import Path

    from ._agent_supervisor import execution_id
    from .agent_execution import ExecutionStore, _safe_read
    from .result_contract import parse_worker_result

    store = ExecutionStore(state_dir / "agent-executions")
    identity = execution_id(tick_id, key)
    record = store.load(identity)
    attempt = record["attempts"][-1]
    contract = record["registration"]["result_contract"]
    with store._directory_handle(identity) as directory:
        raw = json.loads(_safe_read(Path(f"result-{attempt['generation']}.json"), directory_fd=directory))
    if contract["result_kind"] == "phase":
        return raw, contract
    result = parse_worker_result(
        {"runs": [{"status": "completed", "metadata": {"tpo_result": raw}}]},
        tick_id=tick_id, todo_id=registration.todo_id, step_key=key,
        acceptance_criteria=tuple(contract["acceptance"]),
        allow_no_changes=not bool(contract["expected_commits"]),
    )
    return result, contract


def validated_predecessor_head(registration, *, state_dir, tick_id, stop_key=None):
    """Re-prove every phase-entry/result link using promoted supervisor evidence."""
    from .authority_result import require_authorized_result
    from .result_contract import ResultContractError

    head = registration.base_sha
    for phase in worker_phases(registration.phase_definitions):
        if phase.phase_key == stop_key:
            break
        result, contract = _promoted_result(registration, state_dir=state_dir, tick_id=tick_id,
                                           key=phase.phase_key)
        with require_authorized_result(registration=registration, state_dir=state_dir,
                                       tick_id=tick_id, step_key=phase.phase_key, result=result):
            if contract["base_sha"] != head:
                raise ResultContractError("phase_parent_mismatch")
            head = result["head_sha"] if isinstance(result, dict) else result.git.resulting_head_sha
    return head


def reconcile_schedule(*, project_dir, state_dir, tenant, tick_id, registration, repo=None):
    """Admit at most one missing worker, only after validated declared predecessors."""
    from .authority_result import (
        RunAuthorityBusy,
        locked_run_authority,
        require_authorized_result,
    )
    from .kanban_tasks import (
        _clear_validation_blocked,
        _record_validation_blocked,
        _show_task_payload,
        get_todo_kanban_tasks,
    )
    from .result_contract import (
        ResultContractError,
        _git,
        parse_worker_result,
        render_result_template,
    )
    from .review_reconciliation import (
        RetryableReviewRegistration,
        _create_task,
        _persist_accepted_head,
        render_profile_prompt,
    )
    from .todos_completion import _delivery_authority, _reconcile_todo_completion_locked

    key = ""
    try:
        with locked_run_authority(registration=registration, state_dir=state_dir, tick_id=tick_id):
            tasks = get_todo_kanban_tasks(tenant, tick_id)
            head = registration.base_sha
            parent = None
            phases = worker_phases(registration.phase_definitions)
            for index, phase in enumerate(phases):
                key = phase.phase_key
                worker = tasks.get(key)
                if worker is None:
                    if any(p.phase_key in tasks for p in phases[index + 1:]):
                        raise ResultContractError("phase_order_mismatch")
                    if _git(registration.worktree, "rev-parse", "HEAD") != head:
                        raise ResultContractError("head_mismatch")
                    if phase.role == "delivery" and registration.manifest is not None:
                        _delivery_authority(state_dir, tick_id, registration.worktree, repo=repo, create=True)
                    from .result_contract import manifest_acceptance_criteria
                    criteria = (manifest_acceptance_criteria(registration.manifest)
                                if phase.role == "implementation" and registration.manifest else ())
                    _create_task(
                        project_dir=project_dir, tenant=tenant, tick_id=tick_id, todo_id=registration.todo_id,
                        key=key, title=phase.name,
                        prompt=render_profile_prompt(registration, phase, "pinned-profile", tick_id=tick_id,
                            tenant=tenant, facts={"branch": registration.branch, "reviewed_head_sha": head,
                                                 "accepted_review_head_sha": head}),
                        result_template=render_result_template(tick_id=tick_id, todo_id=registration.todo_id,
                            step_key=key, acceptance_criteria=criteria, allow_no_changes=not bool(criteria),
                            **({"section": "delivery", "branch": registration.branch}
                               if phase.role == "delivery" and registration.manifest else {})),
                        worktree=registration.worktree,
                        assignee=(registration.review_assignee or registration.assignee)
                                 if phase.role == "review" else registration.assignee,
                        parent=parent, prompt_client=registration.prompt_client,
                        tools=phase_tools(phase, profile=registration.profile,
                                          prompt_client=registration.prompt_client),
                        turns=phase.turns, timeout=phase.timeout,
                    )
                    return True
                if worker.status != "done":
                    if any(p.phase_key in tasks for p in phases[index + 1:]):
                        raise ResultContractError("phase_order_mismatch")
                    return True
                result, contract = _promoted_result(registration, state_dir=state_dir, tick_id=tick_id, key=key)
                # Worker contracts retain the existing Kanban/promoted-result identity check.
                if contract["result_kind"] == "worker":
                    reported = parse_worker_result(_show_task_payload(worker.task_id), tick_id=tick_id,
                        todo_id=registration.todo_id, step_key=key,
                        acceptance_criteria=tuple(contract["acceptance"]),
                        allow_no_changes=not bool(contract["expected_commits"]))
                    if reported != result:
                        raise ResultContractError("supervisor_result_unconfirmed")
                with require_authorized_result(registration=registration, state_dir=state_dir,
                                               tick_id=tick_id, step_key=key, result=result):
                    if contract["base_sha"] != head:
                        raise ResultContractError("phase_parent_mismatch")
                    head = result["head_sha"] if isinstance(result, dict) else result.git.resulting_head_sha
                    if phase.role == "review" and registration.manifest:
                        _persist_accepted_head(state_dir, tick_id, head)
                if phase.role == "delivery" and registration.manifest:
                    if not _reconcile_todo_completion_locked(project_dir=project_dir, state_dir=state_dir,
                            tenant=tenant, tick_id=tick_id, repo=repo, registration=registration, allow_close=False):
                        return False
                    if not (state_dir / "runs" / tick_id / "finish-verified").exists():
                        return True
                parent = worker.task_id
            if registration.manifest and role_key(registration, "delivery") is not None:
                if not _reconcile_todo_completion_locked(project_dir=project_dir, state_dir=state_dir,
                        tenant=tenant, tick_id=tick_id, repo=repo, registration=registration):
                    return False
            _clear_validation_blocked(state_dir, tick_id)
            return True
    except (RunAuthorityBusy, RetryableReviewRegistration):
        return True
    except (ResultContractError, RuntimeError, OSError, ValueError, KeyError, IndexError) as exc:
        code = getattr(exc, "code", "phase_result_unconfirmed")
        _record_validation_blocked(state_dir, tick_id=tick_id, step_key=key, code=code, reason=code)
        return False
