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
    render_result_template,
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


def _pr_view(worktree: Path, pr_url: str) -> dict[str, object]:
    try:
        result = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json",
             "state,url,headRefName,headRefOid,baseRefName,headRepository,isCrossRepository"],
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


# Check-state vocabulary, transcribed from gh 2.89.0
# `pkg/cmd/pr/checks/aggregate.go`, which buckets every state it knows:
# SUCCESS -> pass; SKIPPED, NEUTRAL -> skipping; ERROR, FAILURE, TIMED_OUT,
# ACTION_REQUIRED -> fail; CANCELLED -> cancel; everything else -> pending.
#
# We reuse gh's green and red buckets and diverge deliberately on three states,
# because gh's buckets serve a watch loop a human is staring at while ours
# decides, unattended and unbounded, whether to close a delivered issue:
#
#   CANCELLED       gh calls it non-blocking. We fail it. A cancelled required
#                   check is not evidence that it passed, and this divergence
#                   fails CLOSED (a human is asked), so it is the safe one.
#   STALE           gh buckets both as pending only because its default arm
#   STARTUP_FAILURE catches every unlisted state. Neither is transient: a stale
#                   check will not re-run on its own and a startup failure has
#                   already ended. `pending` here means "return True every tick,
#                   forever, silently", so they fail closed too.
#
# States absent from all three sets (a state gh grows after this was written)
# are unreadable evidence, not a silent pass and not a silent wait: see
# `_classify_check_states`.
_CHECKS_GREEN = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
_CHECKS_RED = frozenset({
    "ERROR", "FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "CANCELLED",
    "STALE", "STARTUP_FAILURE",
})
# `""` is in here, not an unknown: `aggregate.go` derives `state` from the
# conclusion once `status == "COMPLETED"`, so a check run completed with a null
# conclusion -- the brief window before the conclusion lands, and what a deleted
# or expired run degrades to -- exports as the empty string. gh's own default arm
# buckets it pending; raising instead would summon a human for something that
# resolves itself on the next tick.
_CHECKS_TRANSIENT = frozenset({
    "EXPECTED", "REQUESTED", "WAITING", "QUEUED", "PENDING", "IN_PROGRESS", "",
})

# gh's message for an empty status-check rollup on the head commit
# (`checks.go`: `no checks reported on the '%s' branch`). Matched as an anchored
# prefix of the first stderr line, not as a substring: `gh pr checks <arg>` echoes
# its argument back in `no pull requests found for branch "<arg>"`, so a bare
# substring test lets a crafted `pr_url` mint this signal for itself. That is
# unreachable today only because `result_contract` pins `pr_url` to a strict
# GitHub URL two modules away, and an approval predicate should not lean on a
# guarantee enforced somewhere else. The branch name is interpolated, so the
# match stops at the opening quote.
_NO_CHECKS_STDERR_PREFIX = "no checks reported on the '"


def _classify_check_states(states: set[str]) -> str:
    """Reduce one PR's check states to ``passed`` / ``failed`` / ``pending``.

    Raises ``checks_unavailable`` for any state outside the vocabulary above: a
    state gh grows after this was written is unreadable evidence, so it is neither
    a silent pass nor an unbounded silent wait.

    The empty-set arm is defence in depth, NOT the fix for the old
    ``set() <= {"SUCCESS", "SKIPPED"}`` -> ``passed`` defect. What fixes that is
    the strict per-item loop in ``_check_state``, which refuses an unreadable
    entry outright instead of dropping it and shrinking the set. Given that loop,
    ``states`` can only be empty when ``checks`` was empty, which returns earlier;
    this arm exists so a future caller cannot reintroduce the defect by filtering.
    """
    if not states or not states <= (_CHECKS_GREEN | _CHECKS_RED | _CHECKS_TRANSIENT):
        raise ResultContractError("checks_unavailable")
    if states & _CHECKS_RED:
        return "failed"
    if states & _CHECKS_TRANSIENT:
        return "pending"
    return "passed"


def _rollup_is_honestly_empty(worktree: Path, *, repo: str, head_sha: str) -> bool:
    """True only when *head_sha* really has no check suites and no statuses.

    gh raises `no checks reported on the '<branch>' branch` when the head commit's
    ``statusCheckRollup`` is EMPTY (`checks.go`:
    ``if len(statusCheckRollup.Nodes) == 0``). That is not the same claim as "this
    repository has no CI", and at least six conditions produce it: no CI at all;
    the rollup not yet populated after a push; Actions disabled on the repository;
    a fork pull request whose workflows await maintainer approval; every workflow
    path-filtered out of the diff; and a workflow *startup failure*, where a run
    is created, concludes ``failure``, and produces zero jobs. Only the first
    means "no gate to pass"; the rest mean "the gate did not report".

    The startup failure is the one that must never be read as green here, because
    TPO's own workers edit repository files including ``.github/workflows/*``: a
    worker that breaks the workflow file deletes CI and produces exactly this
    shape. (Note it never reaches the state vocabulary as ``STARTUP_FAILURE``; it
    arrives as this error instead.) Nor is the post-merge framing a rescue --
    ``pull_request`` workflows do not re-run after a merge, so a head commit that
    never got a check run never will. The false green would be permanent.

    Live repro this exists for: ``yehiashouman/WearExerciseManager#4``, MERGED at
    ``4c14b532d7da2a99a9e3b337fece90a5336fdc43``, whose repository does have
    ``.github/workflows/android.yml`` on ``pull_request``. gh reports no checks;
    ``check-suites`` reports ``total_count: 1`` (with ``latest_check_runs_count:
    0``) and ``status`` reports ``total_count: 0``.

    So the absence is corroborated against the commit itself before it is allowed
    to mean green, using the two REST endpoints that count the underlying objects
    rather than the rollup. Both must be zero. Read-only, and any failure to
    establish that -- a non-zero exit, an unparseable count, a timeout -- raises
    ``checks_unavailable`` rather than falling through to an approval: an error
    while proving a negative is not proof of the negative.
    """
    for endpoint in (
        f"repos/{repo}/commits/{head_sha}/check-suites",
        f"repos/{repo}/commits/{head_sha}/status",
    ):
        try:
            result = subprocess.run(
                ["gh", "api", endpoint, "--jq", ".total_count"], cwd=worktree,
                capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
            raise ResultContractError("checks_unavailable") from exc
        if result.returncode != 0:
            raise ResultContractError("checks_unavailable")
        try:
            total = int((result.stdout or "").strip())
        except ValueError as exc:
            raise ResultContractError("checks_unavailable") from exc
        if total != 0:
            return False
    return True


def _check_state(worktree: Path, pr_url: str, *, repo: str, head_sha: str) -> str:
    """Classify `gh pr checks --json state` for the post-merge delivery gate.

    ``repo`` and ``head_sha`` identify the commit whose checks these are, and are
    used only to corroborate an empty rollup. The caller has already verified both
    against the live pull request (``_verify_pr_identity`` pins the PR to ``repo``;
    ``view["headRefOid"] == delivery.head_sha`` is asserted, as ``pr_head_drift``,
    on every path reaching here), so neither is re-derived loosely.

    Three facts about gh 2.89.0 shape this, each confirmed against the live CLI:

    * ``--json`` short-circuits the exit-code logic. ``checksRun`` returns
      ``opts.Exporter.Write(...)`` *before* the tail that maps failures to
      ``SilentError`` (exit 1) and pending runs to ``PendingError`` (exit 8), so
      with ``--json`` gh exits 0 whatever the checks say. (A PR with FAILURE and
      IN_PROGRESS runs exits 0 with ``--json`` and 1 without.) There is no exit-8
      branch to write: the one removed from here could never fire.
    * An EMPTY status-check rollup on the head commit fails earlier, inside
      ``populateStatusChecks``: exit 1, EMPTY stdout, and
      ``no checks reported on the '<branch>' branch`` on stderr. It never emits
      ``[]`` with exit 0, so the old code's ``json.loads("")`` raised
      ``checks_unavailable`` and wedged the gate permanently -- the issue never
      closed, ``registration_state`` stayed ``active``, and the TODO stayed
      ineligible forever. That signal is necessary but NOT sufficient for green;
      see ``_rollup_is_honestly_empty``.
    * Every other nonzero exit (auth, network, deleted PR, unknown JSON field)
      also leaves stdout empty. A nonzero exit therefore never carries a payload
      worth classifying, and one that somehow did could otherwise let a stray
      ``[{"state": "SUCCESS"}]`` approve a delivery gh had just errored on.

    This DELIBERATELY DIFFERS from ``ship.ci_is_green``, which still answers green
    for an empty rollup with no corroboration ("treated as green so approve does
    not deadlock on repos without required checks"). That rule is unsound for the
    same reason it was unsound here -- an empty rollup has at least six causes and
    only one of them is "no gate to pass" -- and ``ship`` is the side that should
    move. Until it does, the divergence is intentional: do not "restore" it by
    deleting the corroboration below.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "checks", pr_url, "--json", "state"], cwd=worktree,
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise ResultContractError("checks_unavailable") from exc
    if result.returncode != 0:
        if (result.stdout or "").strip() or not (
            (result.stderr or "").lstrip().startswith(_NO_CHECKS_STDERR_PREFIX)
        ):
            raise ResultContractError("checks_unavailable")
        if _rollup_is_honestly_empty(worktree, repo=repo, head_sha=head_sha):
            return "passed"
        raise ResultContractError("checks_unavailable")
    try:
        checks = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ResultContractError("checks_unavailable") from exc
    # The list check guards the emptiness shortcut below, so the two are written
    # as one decision: without it `json.loads` returning `0`, `null` or `{}` is
    # falsy and would be approved as "no checks at all", and `5` or `true` would
    # reach the iteration and escape as a bare `TypeError` rather than a block.
    if not isinstance(checks, list):
        raise ResultContractError("checks_unavailable")
    if not checks:
        # "No checks at all" makes the same claim as an empty rollup, so it earns
        # the same corroboration. gh 2.89.0 cannot reach here -- `populateStatusChecks`
        # errors on an empty rollup before the exporter runs -- but erroring on
        # empty output is a known `--json` wart and normalising it to `[]` with
        # exit 0 is the natural upstream fix. Approving uncorroborated here would
        # lean on a guarantee enforced in a Go binary this repository does not
        # control, which is exactly what the anchored sentinel above refuses to do.
        if _rollup_is_honestly_empty(worktree, repo=repo, head_sha=head_sha):
            return "passed"
        raise ResultContractError("checks_unavailable")
    # Strict, per item: one entry we cannot read makes the whole answer
    # unreadable. Filtering such entries out instead is how the old code turned
    # a payload of non-dicts into an empty -- and therefore "green" -- state set.
    states: set[str] = set()
    for item in checks:
        state = item.get("state") if isinstance(item, dict) else None
        if not isinstance(state, str):
            raise ResultContractError("checks_unavailable")
        states.add(state)
    return _classify_check_states(states)


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
    """Pin the PR to our branch, origin's base branch, and the project repository.

    ``gh pr view`` exposes no ``baseRepository`` field, so the base repository is
    established transitively: ``isCrossRepository`` is false exactly when head and
    base repositories are the same, so head == origin plus not-cross-repository
    means base == origin, and origin is then matched against ``repo``. A missing
    or non-boolean ``isCrossRepository`` fails closed.

    Repository names compare case-insensitively throughout: ``repository`` carries
    whatever case the operator typed into the origin remote URL, while
    ``headRepository.nameWithOwner`` carries GitHub's canonical case.
    """
    repository, base_branch = _github_identity(worktree)
    head_repository = _name_with_owner(view.get("headRepository"))
    if (
        view.get("headRefName") != branch
        or view.get("baseRefName") != base_branch
        or head_repository is None
        or head_repository.lower() != repository.lower()
        or view.get("isCrossRepository") is not False
        # Defence in depth: unreachable in production, since _delivery_authority
        # pins origin == repo and reconcile_todo_completion re-checks the pin
        # before calling here. Kept as a fail-closed backstop.
        or repository.lower() != repo.lower()
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
                f"Do not merge. The expected parent is {head}.\n\n"
                + render_result_template(
                    tick_id=tick_id,
                    todo_id=registration.todo_id,
                    step_key=FINISH_KEY,
                    section="delivery",
                    pinned_head_sha=head,
                    branch=registration.branch,
                    allow_no_changes=True,
                )
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
        checks = _check_state(
            registration.worktree, delivery.pr_url,
            repo=repo, head_sha=delivery.head_sha,
        )
    except ResultContractError as exc:
        return _block(gate_id, exc.code)
    if checks == "failed":
        # Not "required_checks_failed": `gh pr checks` runs without `--required`,
        # so this counts advisory checks too. Passing `--required` instead would
        # be worse. gh 2.89.0 carries a SECOND format string for that mode,
        # `no required checks reported on the '%s' branch`, so a repository with
        # CI but no branch protection -- no check is marked required -- would get
        # that message for every PR. It does not match the anchored
        # `_NO_CHECKS_STDERR_PREFIX`, which is correct: `_check_state` would raise
        # `checks_unavailable` immediately, never reaching corroboration, and the
        # gate would block forever, reinstating the wedge this was just fixed for.
        # We measure every check and name the code for what we measured.
        return _block(gate_id, "pr_checks_failed")
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
_COMPLETION_TICK_RE = re.compile(r"<!-- tpo-completed tick=([A-Za-z0-9_-]+) pr=\d+ -->")
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
    # Markers only count when TPO wrote them: anyone can paste the HTML comment,
    # so a foreign copy must neither satisfy dedup nor conflict. Ownership is the
    # current token login, or a marker whose ``tick=`` names a local run (the
    # login may have rotated since that run commented).
    runs_dir = state_dir / "runs"
    login_cache: list[str | None] = []

    def current_login() -> str | None:
        """Resolved once, and only when a remote marker must be attributed."""
        if not login_cache:
            try:
                login_cache.append(github_issues.current_login(project_dir))
            except github_issues.GitHubIssuesError as exc:
                if exc.code != "gh_auth":
                    raise
                log.warning(
                    "gh api user failed (%s; token lacks read:user?) — "
                    "attributing completion markers by local run directory only",
                    exc.code,
                )
                login_cache.append(None)
        return login_cache[0]

    def recorded_login(tick: str) -> str | None:
        """Login stamped into ``runs/<tick>/issue-commented``; None for legacy ``pr=N`` files."""
        try:
            text = (runs_dir / tick / COMMENTED_MARKER).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return None
        return None if not text or text.startswith("pr=") else text

    def owns_local_tick(author: str, tick: str) -> bool:
        if not (runs_dir / tick).is_dir():
            return False
        recorded = recorded_login(tick)
        return recorded is None or author == recorded

    def own_bodies(comments) -> list[str]:
        owned: list[str] = []
        for author, body in comments:
            ticks = _COMPLETION_TICK_RE.findall(body)
            if not ticks:
                continue
            if any(owns_local_tick(author, tick) for tick in ticks):
                owned.append(body)
            elif author and author == current_login():
                owned.append(body)
        return owned

    def marker_present(comments) -> bool:
        return (has_run and (run_dir / COMMENTED_MARKER).exists()) or any(
            marker in body for body in own_bodies(comments)
        )

    live = github_issues.fetch_issue(project_dir, issue_number, repo=repo)
    if live.state == "closed" and live.state_reason == "not_planned":
        raise github_issues.GitHubIssuesError("issue_not_planned", "issue close")
    comments = github_issues.list_comments(project_dir, issue_number, repo=repo)
    if not force:
        for body in own_bodies(comments):
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
            # Breadcrumb records who commented so ownership survives a token rotation.
            _atomic_write_text(run_dir / COMMENTED_MARKER, f"{current_login() or ''}\n")
    if live.state == "open":
        github_issues.close_issue(project_dir, issue_number, repo=repo)
    if IN_PROGRESS_LABEL in live.labels:
        github_issues.remove_label(project_dir, issue_number, IN_PROGRESS_LABEL, repo=repo)

    live = github_issues.fetch_issue(project_dir, issue_number, repo=repo)
    comment_present = marker_present(
        github_issues.list_comments(project_dir, issue_number, repo=repo)
    )
    if live.state == "open" or not comment_present or IN_PROGRESS_LABEL in live.labels:
        return "pending"
    if has_run:
        _atomic_write_text(run_dir / CLOSED_MARKER, f"pr={pr_number}\n")
    return "closed"
