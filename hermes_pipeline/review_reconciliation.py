"""Idempotent independent-review and bounded remediation reconciliation."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .kanban_tasks import (
    KANBAN_QUERY_TIMEOUT,
    PHASE_TIMEOUT_CLEANUP_GRACE_SECONDS,
    _block_gate_task,
    _build_json_header,
    _external_agent_prompt_block,
    _external_client_delegation_block,
    _find_task_id_in_snapshot,
    _mark_gate_needs_input,
    _parse_task_id,
    _show_task_payload,
    complete_todo_kanban_task,
    get_todo_kanban_tasks,
)
from .result_contract import (
    ResultContractError,
    WorkerResult,
    load_validated_registration,
    parse_worker_result,
    sanitize_result_text,
    verify_read_only_review,
    verify_worker_git_result,
    verify_worker_git_topology,
)
from .state import _atomic_write_text

MAX_REVIEW_ROUNDS = 5
REVIEW_ACCEPTANCE_KEY = "review-acceptance"


class RetryableReviewRegistration(RuntimeError):
    """An idempotent dynamic-card create has an ambiguous remote outcome."""


def _pending_create_path(project_dir: Path, tick_id: str) -> Path:
    return project_dir / ".hermes" / "runs" / tick_id / "pending-review-create.json"


def _persist_pending_create(project_dir: Path, tick_id: str, key: str) -> Path:
    path = _pending_create_path(project_dir, tick_id)
    _atomic_write_text(
        path,
        json.dumps({"schema_version": 1, "tick_id": tick_id, "step_key": key})
        + "\n",
    )
    return path


def _clear_pending_create(path: Path, *, tick_id: str, key: str) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload == {"schema_version": 1, "tick_id": tick_id, "step_key": key}:
            path.unlink()
    except (OSError, json.JSONDecodeError):
        return


def _persist_accepted_head(state_dir: Path, tick_id: str, head_sha: str) -> None:
    _atomic_write_text(
        state_dir / "runs" / tick_id / "accepted-review-head",
        head_sha + "\n",
    )


def _accept_review(*, state_dir: Path, tick_id: str, head_sha: str,
                   tenant: str, acceptance) -> bool:
    _persist_accepted_head(state_dir, tick_id, head_sha)
    return acceptance.status == "done" or complete_todo_kanban_task(
        tenant, acceptance.task_id
    )


def _head(worktree: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=worktree, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise ResultContractError("git_verification_failed")
    return result.stdout.strip()


def _body(*, tick_id: str, todo_id: str, tenant: str, key: str, prompt: str) -> str:
    return _build_json_header(
        tick_id=tick_id, phase_key=key, todo_id=todo_id, project_slug=tenant
    ) + "\n" + prompt


def _create_task(
    *, project_dir: Path | None = None, tenant: str, tick_id: str, todo_id: str,
    key: str, title: str,
    prompt: str, worktree: Path, assignee: str | None, parent: str,
    prompt_client: str, gate: bool = False,
) -> str:
    project_dir = project_dir or worktree
    task_prompt = prompt if gate else (
        _external_client_delegation_block(prompt_client, timeout=1800, tools="")
        + _external_agent_prompt_block(prompt)
    )
    cmd = [
        "hermes", "kanban", "create", "--tenant", tenant, title,
        "--body", _body(
            tick_id=tick_id, todo_id=todo_id, tenant=tenant, key=key,
            prompt=task_prompt,
        ),
        "--workspace", f"dir:{worktree}", "--idempotency-key", f"{tick_id}:{key}",
        "--assignee", "-" if gate else (assignee or "default"),
        "--parent", parent, "--json",
    ]
    if not gate:
        cmd.extend([
            "--max-runtime", str(1800 + PHASE_TIMEOUT_CLEANUP_GRACE_SECONDS),
            "--max-retries", "1", "--goal", "--goal-max-turns", "20",
        ])
    marker = _persist_pending_create(project_dir, tick_id, key)
    task_id = _find_task_id_in_snapshot(
        tenant=tenant, tick_id=tick_id, phase_key=key
    )
    if task_id is None:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=KANBAN_QUERY_TIMEOUT,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise RetryableReviewRegistration(
                f"review task registration remains pending for {key}"
            ) from exc
        task_id = _parse_task_id(result.stdout) if result.returncode == 0 else None
    if task_id is None:
        raise RetryableReviewRegistration(
            f"review task registration remains pending for {key}"
        )
    if gate:
        try:
            _block_gate_task(task_id)
        except (RuntimeError, OSError) as exc:
            raise RetryableReviewRegistration(
                f"review gate registration remains pending for {key}"
            ) from exc
    _clear_pending_create(marker, tick_id=tick_id, key=key)
    return task_id


def _review_result(task_id: str, *, tick_id: str, todo_id: str, key: str,
                   worktree: Path, head_sha: str,
                   require_current: bool = True) -> WorkerResult:
    result = parse_worker_result(
        _show_task_payload(task_id), tick_id=tick_id, todo_id=todo_id,
        step_key=key, acceptance_criteria=(), allow_no_changes=True,
    )
    verify_read_only_review(
        worktree, result, head_sha=head_sha, require_current=require_current
    )
    return result


def _review_prompt(head_sha: str) -> str:
    return (
        "Perform a fresh, independent, read-only review in a new external session. "
        f"Review the complete branch at {head_sha}; do not modify the worktree. "
        "Return metadata.tpo_result.review with verdict clean or findings and bounded "
        "P0-P3 findings. The reported Git head must remain unchanged."
    )


def _implementation_head(*, tasks: dict, registration, tick_id: str) -> str:
    """Derive and revalidate the implementation head from the task result chain."""
    expected = registration.base_sha
    for task in registration.manifest.tasks:
        worker = tasks.get(f"plan:{task.id}")
        gate = tasks.get(f"validate:{task.id}")
        if worker is None or gate is None or worker.status != "done" or gate.status != "done":
            raise ResultContractError("review_prerequisite_incomplete")
        result = parse_worker_result(
            _show_task_payload(worker.task_id), tick_id=tick_id,
            todo_id=registration.todo_id, step_key=f"plan:{task.id}",
            acceptance_criteria=task.acceptance_criteria,
        )
        verify_worker_git_topology(
            registration.worktree, result.git, expected_parent_sha=expected
        )
        expected = result.git.resulting_head_sha
    return expected


def _ensure_initial_review(*, project_dir: Path, tasks: dict, registration, tenant: str,
                           tick_id: str) -> None:
    validation = [
        tasks.get(f"validate:{task.id}") for task in registration.manifest.tasks
    ]
    if not validation or any(task is None or task.status != "done" for task in validation):
        return
    parent = validation[-1].task_id
    head_sha = _implementation_head(tasks=tasks, registration=registration, tick_id=tick_id)
    review = tasks.get("review:0")
    if review is None:
        review_id = _create_task(
            project_dir=project_dir,
            tenant=tenant, tick_id=tick_id, todo_id=registration.todo_id,
            key="review:0", title="Independent review", prompt=_review_prompt(head_sha),
            worktree=registration.worktree,
            assignee=registration.review_assignee or registration.assignee, parent=parent,
            prompt_client=registration.prompt_client,
        )
    else:
        review_id = review.task_id
    if tasks.get(REVIEW_ACCEPTANCE_KEY) is None:
        _create_task(
            project_dir=project_dir,
            tenant=tenant, tick_id=tick_id, todo_id=registration.todo_id,
            key=REVIEW_ACCEPTANCE_KEY, title="Review acceptance gate",
            prompt="TPO completes this persistent gate only after a validated clean review.",
            worktree=registration.worktree, assignee=None, parent=review_id, gate=True,
            prompt_client=registration.prompt_client,
        )


def _ensure_round(*, project_dir: Path, round_number: int, parent: str, registration, tenant: str,
                  tick_id: str, tasks: dict, findings: tuple[dict[str, str], ...]) -> None:
    barrier_key = f"review:{round_number}"
    fix_key = f"review-fix:{round_number}"
    validation_key = f"fix-validation:{round_number}"
    residual = sanitize_result_text(json.dumps(findings, sort_keys=True), maximum=8000)
    barrier = tasks.get(barrier_key)
    barrier_id = barrier.task_id if barrier else _create_task(
        project_dir=project_dir,
        tenant=tenant, tick_id=tick_id, todo_id=registration.todo_id,
        key=barrier_key, title=f"Review round {round_number} barrier",
        prompt="Round registration barrier.", worktree=registration.worktree,
        assignee=None, parent=parent, gate=True,
        prompt_client=registration.prompt_client,
    )
    fix = tasks.get(fix_key)
    fix_id = fix.task_id if fix else _create_task(
        project_dir=project_dir,
        tenant=tenant, tick_id=tick_id, todo_id=registration.todo_id, key=fix_key,
        title=f"Fix review findings round {round_number}",
        prompt=f"Fix exactly these reviewed findings using TDD, then commit:\n{residual}",
        worktree=registration.worktree, assignee=registration.assignee, parent=barrier_id,
        prompt_client=registration.prompt_client,
    )
    validation = tasks.get(validation_key)
    validation.task_id if validation else _create_task(
        project_dir=project_dir,
        tenant=tenant, tick_id=tick_id, todo_id=registration.todo_id,
        key=validation_key, title=f"Validate review fixes round {round_number}",
        prompt="TPO validates fix metadata and Git evidence before completing this gate.",
        worktree=registration.worktree, assignee=None, parent=fix_id, gate=True,
        prompt_client=registration.prompt_client,
    )
    if barrier is None or barrier.status != "done":
        complete_todo_kanban_task(tenant, barrier_id)


def _ensure_rereview(*, project_dir: Path, round_number: int, validation_id: str, head_sha: str,
                     registration, tenant: str, tick_id: str, tasks: dict) -> None:
    key = f"re-review:{round_number}"
    if key in tasks:
        return
    _create_task(
        project_dir=project_dir,
        tenant=tenant, tick_id=tick_id, todo_id=registration.todo_id,
        key=key, title=f"Independent re-review round {round_number}",
        prompt=_review_prompt(head_sha), worktree=registration.worktree,
        assignee=registration.review_assignee or registration.assignee,
        parent=validation_id, prompt_client=registration.prompt_client,
    )


def reconcile_reviews(*, project_dir: Path, state_dir: Path, tenant: str,
                      tick_id: str, repo: str | None = None) -> bool:
    """Reconcile review state from authoritative Kanban cards and run metadata."""
    if not (state_dir / "runs" / tick_id / "registration.json").exists():
        return True
    registration = load_validated_registration(project_dir, state_dir, tick_id, repo=repo)
    if getattr(registration, "manifest", object()) is None:
        return True
    tasks = get_todo_kanban_tasks(tenant, tick_id)
    try:
        _ensure_initial_review(
            project_dir=project_dir, tasks=tasks, registration=registration,
            tenant=tenant, tick_id=tick_id
        )
    except RetryableReviewRegistration:
        return True
    tasks = get_todo_kanban_tasks(tenant, tick_id)
    acceptance = tasks.get(REVIEW_ACCEPTANCE_KEY)
    review = tasks.get("review:0")
    if acceptance is None or review is None or review.status != "done":
        return True
    try:
        expected_parent = _implementation_head(
            tasks=tasks, registration=registration, tick_id=tick_id
        )
        evidence = _review_result(
            review.task_id, tick_id=tick_id, todo_id=registration.todo_id,
            key="review:0", worktree=registration.worktree,
            head_sha=expected_parent, require_current="review:1" not in tasks,
        )
        assert evidence.review is not None
        if evidence.review.verdict == "clean":
            return _accept_review(
                state_dir=state_dir, tick_id=tick_id, head_sha=expected_parent,
                tenant=tenant, acceptance=acceptance,
            )
        findings = evidence.review.findings
        parent = review.task_id
        for round_number in range(1, MAX_REVIEW_ROUNDS + 1):
            tasks = get_todo_kanban_tasks(tenant, tick_id)
            rereview_key = f"re-review:{round_number}"
            fix_key = f"review-fix:{round_number}"
            validation_key = f"fix-validation:{round_number}"
            if fix_key not in tasks or validation_key not in tasks:
                _ensure_round(
                    project_dir=project_dir, round_number=round_number, parent=parent,
                    registration=registration,
                    tenant=tenant, tick_id=tick_id, tasks=tasks, findings=findings,
                )
                return True
            fix = tasks[fix_key]
            validation = tasks[validation_key]
            if fix is None or validation is None or fix.status != "done":
                return True
            fix_result = parse_worker_result(
                _show_task_payload(fix.task_id), tick_id=tick_id,
                todo_id=registration.todo_id, step_key=f"review-fix:{round_number}",
                acceptance_criteria=(),
            )
            verify_worker_git_topology(
                registration.worktree, fix_result.git,
                expected_parent_sha=expected_parent,
            )
            if validation.status != "done":
                verify_worker_git_result(
                    registration.worktree, fix_result.git,
                    expected_parent_sha=expected_parent,
                )
            expected_parent = fix_result.git.resulting_head_sha
            if validation.status != "done":
                if not complete_todo_kanban_task(tenant, validation.task_id):
                    return False
                return True
            _ensure_rereview(
                project_dir=project_dir, round_number=round_number,
                validation_id=validation.task_id,
                head_sha=expected_parent, registration=registration,
                tenant=tenant, tick_id=tick_id, tasks=tasks,
            )
            if rereview_key not in tasks:
                return True
            rereview = tasks[rereview_key]
            if rereview.status != "done":
                return True
            evidence = _review_result(
                rereview.task_id, tick_id=tick_id, todo_id=registration.todo_id,
                key=rereview_key, worktree=registration.worktree,
                head_sha=expected_parent,
                require_current=f"review:{round_number + 1}" not in tasks,
            )
            assert evidence.review is not None
            if evidence.review.verdict == "clean":
                return _accept_review(
                    state_dir=state_dir, tick_id=tick_id, head_sha=expected_parent,
                    tenant=tenant, acceptance=acceptance,
                )
            findings = evidence.review.findings
            parent = rereview.task_id
        diagnostic = sanitize_result_text(
            "Review remediation limit reached: " + json.dumps(findings, sort_keys=True),
            maximum=1000,
        )
        if not _mark_gate_needs_input(acceptance.task_id, diagnostic):
            return False
        return False
    except RetryableReviewRegistration:
        return True
    except (ResultContractError, RuntimeError, OSError) as exc:
        diagnostic = sanitize_result_text(
            f"TPO review reconciliation failed: {getattr(exc, 'code', type(exc).__name__)}",
            maximum=1000,
        )
        if not _mark_gate_needs_input(acceptance.task_id, diagnostic):
            return False
        return False
