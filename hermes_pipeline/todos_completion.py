"""Verified PR handoff, human merge gate, and idempotent GitHub issue closeout."""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Literal

from . import github_issues
from .github_issues import IN_PROGRESS_LABEL, parse_github_remote
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
)
from .review_reconciliation import (
    REVIEW_ACCEPTANCE_KEY,
    RetryableReviewRegistration,
    _create_task,
)
from .state import _atomic_write_text

log = logging.getLogger(__name__)

FINISH_KEY = "finish"
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


def _pr_view(worktree: Path, pr_url: str) -> dict[str, str]:
    try:
        result = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json",
             "state,url,headRefName,headRefOid,baseRefName,headRepository,baseRepository"],
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
    repository = parse_github_remote(remote)
    if repository is None:
        raise ResultContractError("origin_identity_invalid")
    symbolic = _git(worktree, "symbolic-ref", "refs/remotes/origin/HEAD")
    prefix = "refs/remotes/origin/"
    if not symbolic.startswith(prefix):
        raise ResultContractError("base_branch_invalid")
    return repository, symbolic.removeprefix(prefix)


def _name_with_owner(value: object) -> str | None:
    if isinstance(value, dict):
        value = value.get("nameWithOwner")
    return value if isinstance(value, str) else None


def _verify_pr_identity(worktree: Path, view: dict, *, branch: str, repo: str) -> None:
    repository, base_branch = _github_identity(worktree)
    base_repository = _name_with_owner(view.get("baseRepository"))
    if (
        view.get("headRefName") != branch
        or view.get("baseRefName") != base_branch
        or _name_with_owner(view.get("headRepository")) != repository
        or base_repository is None
        or base_repository.lower() != repo.lower()
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
                        *, repo: str, create: bool = False) -> tuple[str, str]:
    """Pinned origin/base for the run; both must match the project ``repo``."""
    path = state_dir / "runs" / tick_id / "delivery-authority.json"
    if create and not path.exists():
        repository, base_branch = _github_identity(worktree)
        if repository.lower() != repo.lower():
            raise ResultContractError("delivery_authority_drift")
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
    if raw["origin_repository"].lower() != repo.lower():
        raise ResultContractError("delivery_authority_drift")
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


def _block(gate_id: str, code: str) -> bool:
    _mark_gate_needs_input(
        gate_id, sanitize_result_text(f"TPO delivery blocked: {code}", maximum=1000)
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


def flag_issue_drift(
    *, project_dir: Path, state_dir: Path, tenant: str, tick_id: str, code: str,
    repo: str | None = None,
) -> bool:
    """Block delivery on pinned-issue drift by marking the human gate ``needs_input``.

    Creates the gate (parented to an existing card of the tick) when absent.
    Without any card there is nothing to gate; the drift is logged and persisted
    as a ``tracker_error`` decision so ``tpo status`` surfaces it. Always returns
    False.
    """
    registration = load_validated_registration(project_dir, state_dir, tick_id, repo=repo)
    tasks = get_todo_kanban_tasks(tenant, tick_id)
    if not tasks:
        from .decision import record_tracker_error

        log.warning(
            "tick %s: pinned issue drift (%s) but no kanban card exists to gate",
            tick_id, code,
        )
        try:
            # The tick's own decision file is write-once and already exists, so
            # the drift record lives under its own key.
            record_tracker_error(
                state_dir=state_dir, tick_id=f"{tick_id}-issue-drift", project_slug=tenant,
                code=f"issue_drift:{code}", counts_as_no_progress=True,
            )
        except FileExistsError:
            log.debug("tick %s: issue drift decision already recorded", tick_id)
        return False
    parent = next(iter(tasks.values())).task_id
    return _needs_input(
        tasks=tasks, registration=registration, tenant=tenant, tick_id=tick_id,
        parent=parent, code=code,
    )


def _run_marker(state_dir: Path, tick_id: str, name: str) -> Path:
    return state_dir / "runs" / tick_id / name


def reconcile_todo_completion(
    *, project_dir: Path, state_dir: Path, tenant: str, tick_id: str, repo: str,
) -> bool:
    """Reconcile delivery from Kanban/GitHub facts; never merge or repair drift.

    ``repo`` is the project's ``origin`` identity resolved by the caller; every
    PR and issue fact is bound to it.
    """
    registration_path = state_dir / "runs" / tick_id / "registration.json"
    if not registration_path.exists():
        return True
    registration = load_validated_registration(project_dir, state_dir, tick_id, repo=repo)
    if getattr(registration, "manifest", object()) is None:
        return True
    tasks = get_todo_kanban_tasks(tenant, tick_id)
    acceptance = tasks.get(REVIEW_ACCEPTANCE_KEY)
    if acceptance is None or acceptance.status != "done":
        return True

    finish = tasks.get(FINISH_KEY)
    if finish is None:
        head = _accepted_head(state_dir, tick_id)
        _delivery_authority(state_dir, tick_id, registration.worktree, repo=repo, create=True)
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

    finish_verified = _run_marker(state_dir, tick_id, "finish-verified")
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
            require_current=not finish_verified.exists(),
        )
        if not finish_verified.exists():
            _atomic_write_text(finish_verified, accepted_head + "\n")
        delivery = payload.delivery
        if delivery.head_sha != payload.git.resulting_head_sha:
            raise ResultContractError("delivery_head_mismatch")
        authority = _delivery_authority(state_dir, tick_id, registration.worktree, repo=repo)
        if _github_identity(registration.worktree) != authority:
            raise ResultContractError("delivery_authority_drift")
        pr_match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", delivery.pr_url)
        if pr_match is None or pr_match.group(1).lower() != repo.lower():
            raise ResultContractError("pr_identity_mismatch")
        pr_number = int(pr_match.group(2))
    except ResultContractError as exc:
        return _needs_input(
            tasks=tasks, registration=registration, tenant=tenant, tick_id=tick_id,
            parent=finish.task_id, code=exc.code,
        )

    gate = tasks.get(HUMAN_GATE_KEY)
    if gate is None:
        try:
            view = _pr_view(registration.worktree, delivery.pr_url)
            if view.get("url") != delivery.pr_url:
                raise ResultContractError("pr_identity_mismatch")
            _verify_pr_identity(
                registration.worktree, view, branch=registration.branch, repo=repo,
            )
            if (
                view.get("state") not in ("OPEN", "MERGED")
                or view.get("headRefOid") != delivery.head_sha
            ):
                raise ResultContractError("pr_head_drift")
            if view.get("state") == "OPEN" and (
                _remote_head(registration.worktree, registration.branch) != delivery.head_sha
            ):
                raise ResultContractError("remote_head_drift")
        except ResultContractError as exc:
            return _needs_input(
                tasks=tasks, registration=registration, tenant=tenant, tick_id=tick_id,
                parent=finish.task_id, code=exc.code,
            )
        try:
            gate_id = _human_merge_gate(
                tasks=tasks, registration=registration, tenant=tenant,
                tick_id=tick_id, parent=finish.task_id,
            )
        except RetryableReviewRegistration:
            log.warning("tick %s: human-gate registration remains pending; retrying", tick_id)
            return False
        if view.get("state") != "MERGED":
            _mark_gate_needs_input(
                gate_id,
                sanitize_result_text(f"Human merge required: {delivery.pr_url}", maximum=1000),
            )
            return True
    else:
        gate_id = gate.task_id
        try:
            view = _pr_view(registration.worktree, delivery.pr_url)
            _verify_pr_identity(
                registration.worktree, view, branch=registration.branch, repo=repo,
            )
        except ResultContractError as exc:
            return _block(gate_id, exc.code)
        if view.get("state") != "MERGED" and (
            view.get("state") != "OPEN" or view.get("headRefName") != registration.branch
        ):
            return _block(gate_id, "pull_request_closed_or_drifted")
        if view.get("headRefOid") != delivery.head_sha:
            return _block(gate_id, "pr_head_drift")

    try:
        checks = _check_state(registration.worktree, delivery.pr_url)
    except ResultContractError as exc:
        return _block(gate_id, exc.code)
    if checks == "failed":
        return _block(gate_id, "required_checks_failed")
    if checks == "pending" or view.get("state") != "MERGED":
        return True

    try:
        outcome = close_issue_for_delivery(
            project_dir=project_dir, state_dir=state_dir, tick_id=tick_id,
            issue_number=registration.issue_number, pr_number=pr_number,
            pr_url=delivery.pr_url, repo=repo,
        )
    except github_issues.GitHubIssuesError as exc:
        return _block(gate_id, exc.code)
    if outcome == "pending":
        return True
    return complete_todo_kanban_task(tenant, gate_id)


COMPLETION_MARKER = "<!-- tpo-completed tick={tick_id} pr={pr_number} -->"
_COMPLETION_MARKER_RE = re.compile(r"<!-- tpo-completed tick=\S+ pr=(\d+) -->")
# Written next to the immutable registration; ``registration_state`` reads ``issue-closed``.
CLOSE_STARTED_MARKER = "issue-close-started"
COMMENTED_MARKER = "issue-commented"
CLOSED_MARKER = "issue-closed"


def close_issue_for_delivery(
    *, project_dir: Path, state_dir: Path, tick_id: str, issue_number: int,
    pr_number: int, pr_url: str, repo: str, date: str | None = None,
    force: bool = False,
) -> Literal["closed", "pending"]:
    """Idempotently close the delivered issue; safe to re-enter every tick.

    Refuses (``GitHubIssuesError``) an issue closed as ``not_planned``
    (``issue_not_planned``) or one already carrying a completion marker for a
    different PR (``completion_conflict``, overridable with ``force``). Steps,
    each skipped when GitHub already shows its effect: post one ``Completed:``
    comment carrying ``COMPLETION_MARKER`` (matched on the exact ``tick``/``pr``
    pair), ``gh issue close``, remove ``tpo:in-progress``. A re-fetch then
    decides: closed, marker comment present, label gone → ``"closed"``;
    otherwise ``"pending"`` (propagation lag; retry next tick). Other
    ``GitHubIssuesError`` propagate so the caller can block its gate.

    Run markers (written only when ``runs/<tick_id>`` exists — a manual
    ``tick_id`` has none): ``issue-close-started`` before the first remote
    mutation, so a later ``issue_closed`` drift verdict is recognised as this
    closeout in progress; ``issue-commented`` after the comment is accepted, so
    a lagging comment listing can never cause a second comment (the file then
    also satisfies the marker postcondition); ``issue-closed`` on success.
    ``registration_state`` maps ``issue-closed`` to ``delivered`` (this is its
    only production writer) and an operator-created ``abandoned`` file
    (``touch runs/<tick>/abandoned``) to ``abandoned``; otherwise the run is
    ``active`` and keeps its pinned issue out of eligibility.
    """
    marker = COMPLETION_MARKER.format(tick_id=tick_id, pr_number=pr_number)
    run_dir = state_dir / "runs" / tick_id
    has_run = run_dir.is_dir()

    def marker_present(bodies) -> bool:
        return (has_run and (run_dir / COMMENTED_MARKER).exists()) or any(
            marker in body for body in bodies
        )

    live = github_issues.fetch_issue(project_dir, issue_number, repo=repo)
    if live.state == "closed" and live.state_reason == "not_planned":
        raise github_issues.GitHubIssuesError("issue_not_planned", "issue close")
    comments = github_issues.list_comment_bodies(project_dir, issue_number, repo=repo)
    if not force:
        for body in comments:
            for other in _COMPLETION_MARKER_RE.findall(body):
                if int(other) != pr_number:
                    raise github_issues.GitHubIssuesError("completion_conflict", "issue comment")
    if has_run:
        _atomic_write_text(run_dir / CLOSE_STARTED_MARKER, f"pr={pr_number}\n")
    if not marker_present(comments):
        date = date or dt.datetime.now(dt.UTC).date().isoformat()
        github_issues.add_comment(
            project_dir, issue_number,
            f"Completed: PR #{pr_number} {pr_url}, {date}\n{marker}", repo=repo,
        )
        if has_run:
            _atomic_write_text(run_dir / COMMENTED_MARKER, f"pr={pr_number}\n")
    if live.state == "open":
        github_issues.close_issue(project_dir, issue_number, repo=repo)
    if IN_PROGRESS_LABEL in live.labels:
        github_issues.remove_label(project_dir, issue_number, IN_PROGRESS_LABEL, repo=repo)

    live = github_issues.fetch_issue(project_dir, issue_number, repo=repo)
    comment_present = marker_present(
        github_issues.list_comment_bodies(project_dir, issue_number, repo=repo)
    )
    if live.state == "open" or not comment_present or IN_PROGRESS_LABEL in live.labels:
        return "pending"
    if has_run:
        _atomic_write_text(run_dir / CLOSED_MARKER, f"pr={pr_number}\n")
    return "closed"
