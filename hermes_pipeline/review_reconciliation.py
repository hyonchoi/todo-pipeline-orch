"""Idempotent independent-review reconciliation against the phase profile."""
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
    load_validated_registration,
    parse_worker_result,
    render_result_template,
    sanitize_result_text,
    verify_optional_single_commit,
    verify_worker_git_topology,
)
from .state import _atomic_write_text

log = logging.getLogger(__name__)

REVIEW_KEY = "review:0"
REVIEW_PHASE_KEY = "phase_5_review"


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
    """Write the accepted review head once; afterwards only confirm it.

    This file is the anchor every later check measures against: delivery's
    ``_verify_finish`` bounds HEAD to it, and its presence is what relaxes
    ``require_current`` on the review's own re-verification. Rewriting it every
    tick the review card reads ``done`` let the anchor be re-derived from a
    mutable card report under the relaxed check -- so a report that changed
    after the first acceptance could move the very reference point that was
    supposed to pin it. Once written, a differing report is a hard stop, not a
    new anchor.
    """
    path = _accepted_head_path(state_dir, tick_id)
    try:
        recorded = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        recorded = ""
    if recorded:
        if recorded != head_sha:
            raise ResultContractError("accepted_review_head_conflict")
        return
    _atomic_write_text(path, head_sha + "\n")


def _body(*, tick_id: str, todo_id: str, tenant: str, key: str, prompt: str) -> str:
    return _build_json_header(
        tick_id=tick_id, phase_key=key, todo_id=todo_id, project_slug=tenant
    ) + "\n" + prompt


def _create_task(
    *, project_dir: Path, tenant: str, tick_id: str, todo_id: str,
    key: str, title: str,
    prompt: str, result_template: str, worktree: Path, assignee: str | None,
    parent: str | None = None,
    prompt_client: str,
    tools: str, turns: int, timeout: int,
) -> str:
    """Create one assigned worker card. ``parent`` omitted means immediately ready.

    Every card this module and delivery create is a real worker: it publishes
    the result template and its verdict is its own exit status. ``prompt`` is
    the work instruction the external client receives verbatim; the dispatcher's
    ``result_template`` stays outside that delimited block.

    ``tools``, ``turns`` and ``timeout`` come from the phase profile the card
    renders, never from a default here: the profile's ``phase_5_review`` needs
    write tools to make its own review-fix commit, and a hardcoded empty tool
    set is what silently made TPO's review read-only.

    ``project_dir`` is required and has no ``or worktree`` fallback. It is the
    clone whose ``.hermes/runs/<tick_id>/`` the pending-create marker lives in,
    and that directory exists only because the run registered there. Defaulting
    it to the worktree pointed the marker at ``<worktree>/.hermes/runs/...`` --
    a directory a fresh ``git worktree add`` never has and ``.hermes`` being
    gitignored never will -- so the write raised ``FileNotFoundError`` and the
    finish card could not be created at all. Every caller has the clone in
    scope; ``load_validated_registration`` validated the registration's
    containment against exactly this value.
    """
    task_prompt = (
        _external_client_delegation_block(
            prompt_client, timeout=timeout, tools=tools,
            result_template=result_template,
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
        "--max-runtime", str(timeout + PHASE_TIMEOUT_CLEANUP_GRACE_SECONDS),
        "--max-retries", "1", "--goal", "--goal-max-turns", str(turns),
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


def _accepted_head_path(state_dir: Path, tick_id: str) -> Path:
    return state_dir / "runs" / tick_id / "accepted-review-head"


def profile_phase(registration, phase_key: str):
    """Return ``(phases_path, phase)`` for one phase of the run's pinned profile.

    The phase profile is the specification: its prompt, tools, turn budget and
    timeout are what a reconciler-created card must carry. Resolving it from the
    registration -- not from the project's current config -- keeps a profile
    switch mid-run from changing the run already in flight.
    """
    from .contract import ContractSchemaError
    from .phases import load_phases, resolve_profile_phases_path

    try:
        phases_path = resolve_profile_phases_path(registration.profile)
        phases = load_phases(phases_path)
    except (ContractSchemaError, OSError, ValueError) as exc:
        raise ResultContractError("profile_unavailable", phase_key) from exc
    for phase in phases:
        if phase.phase_key == phase_key:
            return phases_path, phase
    raise ResultContractError("profile_phase_missing", phase_key)


def render_profile_prompt(
    registration, phase, phases_path, *, tick_id: str, tenant: str,
    facts: dict[str, str],
) -> str:
    """Render one profile phase prompt as the card's delimited work instruction.

    Per-card facts (the reviewed head, the branch) travel in the non-templated
    pipeline-context header ``_render_phase_prompt`` prepends. Nothing is
    appended after the profile's own words, so the delimited block a card hands
    the external client is the profile prompt and nothing else -- which is the
    only way the live harness can test the profile at all.
    """
    from .phases import _render_phase_prompt

    plan_reference = getattr(registration, "plan_reference", None)
    return _render_phase_prompt(
        phase.prompt,
        todo_id=registration.todo_id,
        tick_id=tick_id,
        project_slug=tenant,
        plan_path=plan_reference.value if plan_reference is not None else None,
        plan_hash=getattr(registration, "plan_hash", None),
        prompt_client=registration.prompt_client,
        template_source=f"{phases_path}:{phase.phase_key}",
        context_facts=facts,
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
    if tasks.get(REVIEW_KEY) is not None:
        return
    phases_path, phase = profile_phase(registration, REVIEW_PHASE_KEY)
    _create_task(
        project_dir=project_dir,
        tenant=tenant, tick_id=tick_id, todo_id=registration.todo_id,
        key=REVIEW_KEY, title=phase.name,
        prompt=render_profile_prompt(
            registration, phase, phases_path, tick_id=tick_id, tenant=tenant,
            facts={
                "reviewed_head_sha": head_sha,
                "branch": registration.branch,
            },
        ),
        result_template=render_result_template(
            tick_id=tick_id, todo_id=registration.todo_id, step_key=REVIEW_KEY,
            allow_no_changes=True,
        ),
        worktree=registration.worktree,
        assignee=registration.review_assignee or registration.assignee, parent=parent,
        prompt_client=registration.prompt_client,
        tools=phase.tools, turns=phase.turns, timeout=phase.timeout,
    )


#: Step keys only a pre-upgrade board can carry. The bounded remediation rounds
#: were removed with the review-round machinery, so nothing creates these any
#: more -- ``review:0`` is the whole of review now.
_LEGACY_ROUND_PREFIXES = ("review-fix:", "re-review:", "fix-validation:")


def _assert_no_legacy_review_rounds(tasks: dict) -> None:
    """Refuse a board that predates the removal of the review rounds.

    A run whose old ``review:0`` returned findings got a ``review-fix:<n>`` card,
    and THAT card made the fix commit -- ``review:0`` itself was read-only, so
    its report ends at the implementation head. Such a run also never wrote
    ``accepted-review-head``, because the old machinery recorded it only once
    re-review passed.

    Every tick after the upgrade then reconciles that board identically:
    ``_ensure_initial_review`` returns early because ``review:0`` exists,
    ``require_current`` is True because no accepted head was recorded, and
    ``verify_optional_single_commit`` demands ``HEAD ==`` the head ``review:0``
    reported -- the head ``review-fix:<n>`` moved past. The result is
    ``head_mismatch``, forever, blaming the worker's topology for what is purely
    a discontinuity across the upgrade.

    Naming the cause does not unwedge the run, and nothing here tries to: such
    a tick must be abandoned and the TODO re-selected (see
    ``docs/howto-debugging-and-recovery.md``). What it does is stop an operator
    chasing a topology bug that is not there.
    """
    legacy = sorted(key for key in tasks if key.startswith(_LEGACY_ROUND_PREFIXES))
    if legacy:
        raise ResultContractError(
            "review_round_upgrade_discontinuity", ", ".join(legacy)
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
    try:
        # Before anything is measured: a legacy round card means this board
        # cannot be reconciled by this module at all, and every measurement
        # below would misattribute that to the worker.
        _assert_no_legacy_review_rounds(tasks)
        review = tasks.get(REVIEW_KEY)
        if review is None or review.status != "done":
            return True
        expected_parent = _implementation_head(
            tasks=tasks, registration=registration, tick_id=tick_id
        )
        result = parse_worker_result(
            _show_task_payload(review.task_id), tick_id=tick_id,
            todo_id=registration.todo_id, step_key=REVIEW_KEY,
            acceptance_criteria=(), allow_no_changes=True,
        )
        # The profile's reviewer applies its own findings as one review-fix
        # commit, so the reviewed head may legitimately have advanced by one.
        # It may advance by no more than that, and it must still descend from
        # the implementation chain this reconciler recomputed -- that is what
        # keeps the accepted head an anchor rather than a worker's claim.
        accepted = _accepted_head_path(state_dir, tick_id)
        verify_optional_single_commit(
            registration.worktree, result.git,
            expected_parent_sha=expected_parent,
            require_current=not accepted.exists(),
        )
        # Record the head the review actually left behind, not the head it
        # started from: a review-fix commit is part of the reviewed work, and
        # delivery anchors to what was blessed.
        _persist_accepted_head(state_dir, tick_id, result.git.resulting_head_sha)
        return True
    except (ResultContractError, RuntimeError, OSError) as exc:
        log.error(
            "tick %s: review reconciliation failed: %s", tick_id,
            sanitize_result_text(
                getattr(exc, "code", type(exc).__name__), maximum=1000
            ),
        )
        return False
