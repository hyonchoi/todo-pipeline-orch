"""Idempotent independent-review and bounded remediation reconciliation."""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from .kanban_tasks import (
    KANBAN_QUERY_TIMEOUT,
    PHASE_TIMEOUT_CLEANUP_GRACE_SECONDS,
    _build_json_header,
    _external_agent_prompt_block,
    _external_client_delegation_block,
    _find_task_id_in_snapshot,
    _parse_task_id,
    _show_task_payload,
    get_todo_kanban_tasks,
)
from .result_contract import (
    ResultContractError,
    WorkerResult,
    load_validated_registration,
    parse_worker_result,
    render_result_template,
    sanitize_result_text,
    verify_read_only_review,
    verify_worker_git_result,
    verify_worker_git_topology,
)
from .state import _atomic_write_text

log = logging.getLogger(__name__)

MAX_REVIEW_ROUNDS = 5


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
    prompt: str, worktree: Path, assignee: str | None, parent: str | None = None,
    prompt_client: str,
) -> str:
    """Create one assigned worker card. ``parent`` omitted means immediately ready.

    Every card this module and delivery create is a real worker: it publishes
    the result template and its verdict is its own exit status.
    """
    project_dir = project_dir or worktree
    task_prompt = (
        _external_client_delegation_block(
            prompt_client, timeout=1800, tools="", expects_result_metadata=True,
        )
        + _external_agent_prompt_block(prompt)
    )
    cmd = [
        "hermes", "kanban", "create", "--tenant", tenant, title,
        "--body", _body(
            tick_id=tick_id, todo_id=todo_id, tenant=tenant, key=key,
            prompt=task_prompt,
        ),
        "--workspace", f"dir:{worktree}", "--idempotency-key", f"{tick_id}:{key}",
        "--assignee", assignee or "default",
        "--json",
        "--max-runtime", str(1800 + PHASE_TIMEOUT_CLEANUP_GRACE_SECONDS),
        "--max-retries", "1", "--goal", "--goal-max-turns", "20",
    ]
    if parent is not None:
        cmd.extend(["--parent", parent])
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


def _review_prompt(head_sha: str, *, tick_id: str, todo_id: str, step_key: str) -> str:
    return (
        "Perform a fresh, independent, read-only review in a new external session. "
        f"Review the complete branch at {head_sha}; do not modify the worktree.\n\n"
        + render_result_template(
            tick_id=tick_id,
            todo_id=todo_id,
            step_key=step_key,
            section="review",
            pinned_head_sha=head_sha,
            allow_no_changes=True,
        )
    )


def _implementation_head(*, tasks: dict, registration, tick_id: str) -> str:
    """Derive and revalidate the implementation head from the task result chain."""
    expected = registration.base_sha
    for task in registration.manifest.tasks:
        worker = tasks.get(f"plan:{task.id}")
        if worker is None or worker.status != "done":
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
    # The implementation chain is pure workers: the last one is the review's
    # parent, and its completion is the only trigger the review waits for. The
    # result reconciler has already validated the chain -- ``reconcile_reviews``
    # runs only after it reported progress -- and ``_implementation_head``
    # re-proves the topology below.
    workers = [
        tasks.get(f"plan:{task.id}") for task in registration.manifest.tasks
    ]
    if not workers or any(task is None or task.status != "done" for task in workers):
        return
    parent = workers[-1].task_id
    head_sha = _implementation_head(tasks=tasks, registration=registration, tick_id=tick_id)
    if tasks.get("review:0") is not None:
        return
    _create_task(
        project_dir=project_dir,
        tenant=tenant, tick_id=tick_id, todo_id=registration.todo_id,
        key="review:0", title="Independent review",
        prompt=_review_prompt(
            head_sha, tick_id=tick_id, todo_id=registration.todo_id,
            step_key="review:0",
        ),
        worktree=registration.worktree,
        assignee=registration.review_assignee or registration.assignee, parent=parent,
        prompt_client=registration.prompt_client,
    )


def _ensure_round(*, project_dir: Path, round_number: int, parent: str, registration, tenant: str,
                  tick_id: str, tasks: dict, findings: tuple[dict[str, str], ...]) -> None:
    fix_key = f"review-fix:{round_number}"
    if fix_key in tasks:
        return
    residual = sanitize_result_text(json.dumps(findings, sort_keys=True), maximum=8000)
    _create_task(
        project_dir=project_dir,
        tenant=tenant, tick_id=tick_id, todo_id=registration.todo_id, key=fix_key,
        title=f"Fix review findings round {round_number}",
        prompt=(
            f"Fix exactly these reviewed findings using TDD, then commit:\n{residual}\n\n"
            + render_result_template(
                tick_id=tick_id, todo_id=registration.todo_id, step_key=fix_key,
            )
        ),
        worktree=registration.worktree, assignee=registration.assignee, parent=parent,
        prompt_client=registration.prompt_client,
    )


def _ensure_rereview(*, project_dir: Path, round_number: int, fix_id: str, head_sha: str,
                     registration, tenant: str, tick_id: str, tasks: dict) -> None:
    key = f"re-review:{round_number}"
    if key in tasks:
        return
    _create_task(
        project_dir=project_dir,
        tenant=tenant, tick_id=tick_id, todo_id=registration.todo_id,
        key=key, title=f"Independent re-review round {round_number}",
        prompt=_review_prompt(
            head_sha, tick_id=tick_id, todo_id=registration.todo_id, step_key=key,
        ),
        worktree=registration.worktree,
        assignee=registration.review_assignee or registration.assignee,
        parent=fix_id, prompt_client=registration.prompt_client,
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
    review = tasks.get("review:0")
    if review is None or review.status != "done":
        return True
    try:
        expected_parent = _implementation_head(
            tasks=tasks, registration=registration, tick_id=tick_id
        )
        evidence = _review_result(
            review.task_id, tick_id=tick_id, todo_id=registration.todo_id,
            key="review:0", worktree=registration.worktree,
            head_sha=expected_parent, require_current="review-fix:1" not in tasks,
        )
        assert evidence.review is not None
        if evidence.review.verdict == "clean":
            _persist_accepted_head(state_dir, tick_id, expected_parent)
            return True
        findings = evidence.review.findings
        parent = review.task_id
        for round_number in range(1, MAX_REVIEW_ROUNDS + 1):
            tasks = get_todo_kanban_tasks(tenant, tick_id)
            rereview_key = f"re-review:{round_number}"
            fix_key = f"review-fix:{round_number}"
            if fix_key not in tasks:
                _ensure_round(
                    project_dir=project_dir, round_number=round_number, parent=parent,
                    registration=registration,
                    tenant=tenant, tick_id=tick_id, tasks=tasks, findings=findings,
                )
                return True
            fix = tasks[fix_key]
            if fix.status != "done":
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
            # The re-review card's existence is the record that this round's fix
            # was already validated against a live worktree: after it exists the
            # worktree has legitimately moved on, so only the topology holds.
            if rereview_key not in tasks:
                verify_worker_git_result(
                    registration.worktree, fix_result.git,
                    expected_parent_sha=expected_parent,
                )
            expected_parent = fix_result.git.resulting_head_sha
            _ensure_rereview(
                project_dir=project_dir, round_number=round_number,
                fix_id=fix.task_id,
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
                require_current=f"review-fix:{round_number + 1}" not in tasks,
            )
            assert evidence.review is not None
            if evidence.review.verdict == "clean":
                _persist_accepted_head(state_dir, tick_id, expected_parent)
                return True
            findings = evidence.review.findings
            parent = rereview.task_id
        log.error(
            "tick %s: review remediation limit reached: %s", tick_id,
            sanitize_result_text(json.dumps(findings, sort_keys=True), maximum=1000),
        )
        return False
    except RetryableReviewRegistration:
        return True
    except (ResultContractError, RuntimeError, OSError) as exc:
        log.error(
            "tick %s: review reconciliation failed: %s", tick_id,
            sanitize_result_text(
                getattr(exc, "code", type(exc).__name__), maximum=1000
            ),
        )
        return False
