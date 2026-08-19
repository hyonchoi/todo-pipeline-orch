"""Verified PR handoff, deterministic TODO closeout, and human merge gate."""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
from pathlib import Path

from .kanban_tasks import (
    _mark_gate_needs_input,
    _show_task_payload,
    complete_todo_kanban_task,
    get_todo_kanban_tasks,
)
from .result_contract import (
    ResultContractError,
    load_validated_registration,
    parse_worker_result,
    sanitize_result_text,
    verify_worker_git_result,
)
from .review_reconciliation import REVIEW_ACCEPTANCE_KEY, _create_task
from .state import _atomic_write_text

FINISH_KEY = "finish"
CLOSEOUT_KEY = "closeout"
HUMAN_GATE_KEY = "human-gate"


def _git(worktree: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=worktree, capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise ResultContractError("git_verification_failed", args[0]) from exc
    if result.returncode != 0:
        raise ResultContractError("git_verification_failed", args[0])
    return result.stdout.strip()


def _git_bytes(worktree: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", *args], cwd=worktree, capture_output=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ResultContractError("git_verification_failed", args[0]) from exc
    if result.returncode != 0:
        raise ResultContractError("git_verification_failed", args[0])
    return result.stdout


def _pr_view(worktree: Path, pr_url: str) -> dict[str, str]:
    try:
        result = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json",
             "state,url,headRefName,headRefOid,baseRefName,headRepository"],
            cwd=worktree, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise ResultContractError("pr_unavailable") from exc
    if result.returncode != 0:
        raise ResultContractError("pr_missing")
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ResultContractError("pr_invalid") from exc
    if not isinstance(raw, dict):
        raise ResultContractError("pr_invalid")
    return raw


def _remote_head(worktree: Path, branch: str) -> str:
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
            cwd=worktree, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise ResultContractError("remote_unavailable") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise ResultContractError("remote_branch_missing")
    return result.stdout.split()[0]


def _check_state(worktree: Path, pr_url: str) -> str:
    try:
        result = subprocess.run(
            ["gh", "pr", "checks", pr_url, "--json", "state"], cwd=worktree,
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise ResultContractError("checks_unavailable") from exc
    if result.returncode == 8:
        return "pending"
    try:
        checks = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ResultContractError("checks_unavailable") from exc
    if not isinstance(checks, list):
        raise ResultContractError("checks_unavailable")
    if not checks:
        if result.returncode == 0:
            return "passed"
        raise ResultContractError("checks_unavailable")
    states = {item.get("state") for item in checks if isinstance(item, dict)}
    if states <= {"SUCCESS", "SKIPPED"}:
        return "passed"
    if states & {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}:
        return "failed"
    return "pending"


def _github_identity(worktree: Path) -> tuple[str, str]:
    remote = _git(worktree, "remote", "get-url", "origin")
    match = re.search(r"github\.com[/:]([^/]+/[^/]+?)(?:\.git)?$", remote)
    if match is None:
        raise ResultContractError("origin_identity_invalid")
    repository = match.group(1).removesuffix(".git")
    symbolic = _git(worktree, "symbolic-ref", "refs/remotes/origin/HEAD")
    prefix = "refs/remotes/origin/"
    if not symbolic.startswith(prefix):
        raise ResultContractError("base_branch_invalid")
    return repository, symbolic.removeprefix(prefix)


def _verify_pr_identity(worktree: Path, view: dict, *, branch: str) -> None:
    repository, base_branch = _github_identity(worktree)
    head_repository = view.get("headRepository")
    if isinstance(head_repository, dict):
        actual_repository = head_repository.get("nameWithOwner")
    else:
        actual_repository = head_repository
    if (
        view.get("headRefName") != branch
        or view.get("baseRefName") != base_branch
        or actual_repository != repository
    ):
        raise ResultContractError("pr_identity_mismatch")


def _accepted_head(state_dir: Path, tick_id: str) -> str:
    try:
        value = (state_dir / "runs" / tick_id / "accepted-review-head").read_text().strip()
    except (OSError, UnicodeError) as exc:
        raise ResultContractError("accepted_review_head_missing") from exc
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ResultContractError("accepted_review_head_invalid")
    return value


def _delivery_authority(state_dir: Path, tick_id: str, worktree: Path,
                        *, create: bool = False) -> tuple[str, str]:
    path = state_dir / "runs" / tick_id / "delivery-authority.json"
    if create and not path.exists():
        repository, base_branch = _github_identity(worktree)
        _atomic_write_text(path, json.dumps({
            "origin_repository": repository, "base_branch": base_branch,
        }, sort_keys=True) + "\n")
    try:
        raw = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResultContractError("delivery_authority_invalid") from exc
    if set(raw) != {"origin_repository", "base_branch"} or not all(
        isinstance(value, str) and value for value in raw.values()
    ):
        raise ResultContractError("delivery_authority_invalid")
    return raw["origin_repository"], raw["base_branch"]


def _verify_finish(worktree: Path, result, accepted_head: str,
                   *, require_current: bool) -> None:
    git = result.git
    if (
        git.expected_parent_sha != accepted_head
        or git.resulting_head_sha != accepted_head
        or git.task_commit_sha != accepted_head
        or git.changed_files
    ):
        raise ResultContractError("finish_review_head_mismatch")
    if require_current and (
        _git(worktree, "rev-parse", "HEAD") != accepted_head
        or _git(worktree, "status", "--porcelain=v1", "--untracked-files=all")
    ):
        raise ResultContractError("finish_review_head_mismatch")


def _verify_closeout_transition(worktree: Path, result, *, todo_id: str,
                                pr_number: int, date: str) -> None:
    from .todos_md import TodoCompletionError, complete_todo_text

    try:
        parent_text = _git_bytes(
            worktree, "show", f"{result.git.expected_parent_sha}:TODOS.md"
        ).decode("utf-8")
        result_text = _git_bytes(
            worktree, "show", f"{result.git.resulting_head_sha}:TODOS.md"
        ).decode("utf-8")
        expected = complete_todo_text(
            parent_text, todo_id, pr_number=pr_number, date=date
        )
    except (TodoCompletionError, UnicodeError) as exc:
        raise ResultContractError("closeout_transition_invalid") from exc
    if result_text != expected:
        raise ResultContractError("closeout_transition_invalid")


def _block(tasks: dict, key: str, code: str) -> bool:
    task = tasks.get(key)
    if task is not None:
        _mark_gate_needs_input(
            task.task_id,
            sanitize_result_text(f"TPO delivery blocked: {code}", maximum=1000),
        )
    return False


def _needs_input(*, tasks: dict, registration, tenant: str, tick_id: str,
                 parent: str, code: str) -> bool:
    gate = tasks.get(HUMAN_GATE_KEY)
    gate_id = gate.task_id if gate is not None else _create_task(
        tenant=tenant, tick_id=tick_id, todo_id=registration.todo_id,
        key=HUMAN_GATE_KEY, title="Human delivery intervention",
        prompt="TPO detected immutable delivery drift; a human must inspect it.",
        worktree=registration.worktree, assignee=None, parent=parent,
        prompt_client=registration.prompt_client, gate=True,
    )
    _mark_gate_needs_input(
        gate_id, sanitize_result_text(f"TPO delivery blocked: {code}", maximum=1000)
    )
    return False


def _human_merge_gate(*, tasks: dict, registration, tenant: str,
                      tick_id: str, parent: str) -> str:
    gate = tasks.get(HUMAN_GATE_KEY)
    if gate is not None:
        return gate.task_id
    return _create_task(
        tenant=tenant, tick_id=tick_id, todo_id=registration.todo_id,
        key=HUMAN_GATE_KEY, title="Human merge gate",
        prompt="Waiting for a human to merge the exact verified pull request.",
        worktree=registration.worktree, assignee=None, parent=parent,
        prompt_client=registration.prompt_client, gate=True,
    )


def reconcile_todo_completion(
    *, project_dir: Path, state_dir: Path, tenant: str, tick_id: str
) -> bool:
    """Reconcile delivery from Kanban/GitHub facts; never merge or repair drift."""
    registration_path = state_dir / "runs" / tick_id / "registration.json"
    if not registration_path.exists():
        return True
    registration = load_validated_registration(project_dir, state_dir, tick_id)
    tasks = get_todo_kanban_tasks(tenant, tick_id)
    acceptance = tasks.get(REVIEW_ACCEPTANCE_KEY)
    if acceptance is None or acceptance.status != "done":
        return True

    finish = tasks.get(FINISH_KEY)
    if finish is None:
        head = _accepted_head(state_dir, tick_id)
        _delivery_authority(state_dir, tick_id, registration.worktree, create=True)
        _create_task(
            tenant=tenant, tick_id=tick_id, todo_id=registration.todo_id,
            key=FINISH_KEY, title="Verify, push, and open pull request",
            prompt=(
                "Run every required repository gate on the clean reviewed head, then "
                "push the registered branch and create or update its pull request. "
                "Do not merge. Close with metadata.tpo_result.delivery containing the "
                f"PR URL, branch {registration.branch}, exact head SHA, and checks. "
                f"The expected parent is {head}."
            ),
            worktree=registration.worktree, assignee=registration.assignee,
            parent=acceptance.task_id, prompt_client=registration.prompt_client,
        )
        return True
    if finish.status != "done":
        return True

    try:
        payload = parse_worker_result(
            _show_task_payload(finish.task_id), tick_id=tick_id,
            todo_id=registration.todo_id, step_key=FINISH_KEY,
            acceptance_criteria=(), allow_no_changes=True,
        )
        if payload.delivery is None or payload.delivery.branch != registration.branch:
            raise ResultContractError("invalid_delivery")
        accepted_head = _accepted_head(state_dir, tick_id)
        _verify_finish(
            registration.worktree, payload, accepted_head,
            require_current=CLOSEOUT_KEY not in tasks,
        )
        delivery = payload.delivery
        if delivery.head_sha != payload.git.resulting_head_sha:
            raise ResultContractError("delivery_head_mismatch")
        authority = _delivery_authority(state_dir, tick_id, registration.worktree)
        if _github_identity(registration.worktree) != authority:
            raise ResultContractError("delivery_authority_drift")
        if CLOSEOUT_KEY not in tasks:
            view = _pr_view(registration.worktree, delivery.pr_url)
            if view.get("url") != delivery.pr_url:
                raise ResultContractError("pr_identity_mismatch")
            _verify_pr_identity(
                registration.worktree, view, branch=registration.branch,
            )
            if view.get("state") != "OPEN" or view.get("headRefOid") != delivery.head_sha:
                raise ResultContractError("pr_head_drift")
            if _remote_head(registration.worktree, registration.branch) != delivery.head_sha:
                raise ResultContractError("remote_head_drift")
    except ResultContractError as exc:
        return _needs_input(
            tasks=tasks, registration=registration, tenant=tenant, tick_id=tick_id,
            parent=finish.task_id, code=exc.code,
        )

    closeout = tasks.get(CLOSEOUT_KEY)
    if closeout is None:
        pr_number = int(re.search(r"/pull/(\d+)$", delivery.pr_url).group(1))
        date = dt.datetime.now(dt.UTC).date().isoformat()
        _atomic_write_text(
            state_dir / "runs" / tick_id / "closeout-date", date + "\n"
        )
        _create_task(
            tenant=tenant, tick_id=tick_id, todo_id=registration.todo_id,
            key=CLOSEOUT_KEY, title="Close TODO through todos-manager",
            prompt=(
                "Invoke the bundled deterministic todos-manager completion backend: "
                f"uv run tpo todos complete --project-root . --todo {registration.todo_id} "
                f"--pr {pr_number} --date {date}. Commit only TODOS.md in its own "
                "atomic commit, push it, and close with structured result metadata. "
                "Do not merge or repair remote drift."
            ), worktree=registration.worktree, assignee=registration.assignee,
            parent=finish.task_id, prompt_client=registration.prompt_client,
        )
        return True
    if closeout.status != "done":
        return True

    try:
        closeout_result = parse_worker_result(
            _show_task_payload(closeout.task_id), tick_id=tick_id,
            todo_id=registration.todo_id, step_key=CLOSEOUT_KEY,
            acceptance_criteria=(),
        )
        verify_worker_git_result(
            registration.worktree, closeout_result.git,
            expected_parent_sha=delivery.head_sha,
        )
        if closeout_result.git.changed_files != ("TODOS.md",):
            raise ResultContractError("closeout_changed_files")
        pr_number = int(re.search(r"/pull/(\d+)$", delivery.pr_url).group(1))
        try:
            closeout_date = (
                state_dir / "runs" / tick_id / "closeout-date"
            ).read_text().strip()
        except (OSError, UnicodeError) as exc:
            raise ResultContractError("closeout_date_missing") from exc
        _verify_closeout_transition(
            registration.worktree, closeout_result,
            todo_id=registration.todo_id, pr_number=pr_number, date=closeout_date,
        )
        view = _pr_view(registration.worktree, delivery.pr_url)
        _verify_pr_identity(
            registration.worktree, view, branch=registration.branch,
        )
        if view.get("state") == "MERGED":
            if view.get("headRefOid") != closeout_result.git.resulting_head_sha:
                raise ResultContractError("pr_head_drift")
            gate_id = _human_merge_gate(
                tasks=tasks, registration=registration, tenant=tenant,
                tick_id=tick_id, parent=closeout.task_id,
            )
            checks = _check_state(registration.worktree, delivery.pr_url)
            if checks == "failed":
                raise ResultContractError("required_checks_failed")
            if checks == "pending":
                return True
            return complete_todo_kanban_task(tenant, gate_id)
        remote_head = _remote_head(registration.worktree, registration.branch)
        if remote_head != closeout_result.git.resulting_head_sha:
            raise ResultContractError("remote_head_drift")
        if view.get("headRefOid") != remote_head:
            raise ResultContractError("pr_head_drift")
    except ResultContractError as exc:
        return _needs_input(
            tasks=tasks, registration=registration, tenant=tenant, tick_id=tick_id,
            parent=closeout.task_id, code=exc.code,
        )

    gate = tasks.get(HUMAN_GATE_KEY)
    if gate is None:
        gate_id = _human_merge_gate(
            tasks=tasks, registration=registration, tenant=tenant,
            tick_id=tick_id, parent=closeout.task_id,
        )
        _mark_gate_needs_input(gate_id, f"Human merge required: {delivery.pr_url}")
        return True

    try:
        view = _pr_view(registration.worktree, delivery.pr_url)
        _verify_pr_identity(
            registration.worktree, view, branch=registration.branch,
        )
    except ResultContractError as exc:
        return _block(tasks, HUMAN_GATE_KEY, exc.code)
    if view.get("state") == "MERGED":
        if view.get("headRefOid") != closeout_result.git.resulting_head_sha:
            return _block(tasks, HUMAN_GATE_KEY, "pr_head_drift")
        try:
            checks = _check_state(registration.worktree, delivery.pr_url)
        except ResultContractError as exc:
            return _block(tasks, HUMAN_GATE_KEY, exc.code)
        if checks == "failed":
            return _block(tasks, HUMAN_GATE_KEY, "required_checks_failed")
        if checks == "pending":
            return True
        return complete_todo_kanban_task(tenant, gate.task_id)
    if view.get("state") != "OPEN" or view.get("headRefName") != registration.branch:
        return _block(tasks, HUMAN_GATE_KEY, "pull_request_closed_or_drifted")
    if view.get("headRefOid") != closeout_result.git.resulting_head_sha:
        return _block(tasks, HUMAN_GATE_KEY, "pr_head_drift")
    try:
        checks = _check_state(registration.worktree, delivery.pr_url)
    except ResultContractError as exc:
        return _block(tasks, HUMAN_GATE_KEY, exc.code)
    if checks == "failed":
        return _block(tasks, HUMAN_GATE_KEY, "required_checks_failed")
    if checks == "pending":
        return True
    return True
