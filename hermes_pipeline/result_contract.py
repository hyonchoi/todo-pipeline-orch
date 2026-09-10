"""Strict worker-result parsing and independently checked Git evidence."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import github_issues
from .agent_git import run_git
from .config import AgentPolicyMode
from .github_issues import (
    MAX_ISSUE_SNAPSHOT_CHARS,
    SUPPORTED_REGISTRATION_SCHEMA_VERSIONS,
    GitHubIssuesError,
    SnapshotFormatError,
    parse_issue_body,
    snapshot_hash,
    split_canonical_snapshot,
)
from .phases import IMPLEMENTATION_KEY
from .plan_manifest import PlanManifest, PlanReference, PlanSource, parse_plan_manifest

MAX_METADATA_BYTES = 64 * 1024
MAX_COMMAND_LENGTH = 500
SCHEMA_VERSION = 1
_SHA_RE = re.compile(r"[0-9a-f]{40}")
# A value that is wholly an unfilled template placeholder. Every placeholder the
# template renders is free of inner angle brackets, so excluding them keeps a
# legitimate wholly bracketed value -- "<script> tags ... <img onerror=x>" in a
# reported command or path -- from being read as an omission.
_PLACEHOLDER_RE = re.compile(r"<[^<>]*>")
# C0/DEL controls plus Unicode line/paragraph separators and bidi overrides.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]")
_LINE_BREAK_RE = re.compile(r"[\n\r\t\u2028\u2029]+")
# The single credential pattern for the whole package. ``todos_create`` used to
# carry a second, divergent one; two redactors with different coverage meant the
# load-bearing one was the weaker, so this constant is the union of both and
# every caller shares it. It is used two ways: as the substitution pattern in
# ``sanitize_result_text``, and as a rejection predicate for reported metadata,
# so an alternative added here also widens what ``unsafe_metadata`` refuses.
SECRET_RE = re.compile(
    r"(?i)(?:"
    # GitHub personal-access, OAuth, user-to-server and refresh tokens.
    r"gh[pousr]_[A-Za-z0-9_]{20,}"
    # ``gh auth login`` issues fine-grained PATs by default and their prefix is
    # NOT covered by ``gh[pousr]_`` above (``github_pat_`` has an ``i`` where
    # that class wants one of ``pousr``). This package drives ``gh`` and ``git``
    # throughout, so this is the shape a subprocess error most likely echoes.
    r"|github_pat_[A-Za-z0-9_]{20,}"
    # A named credential and its value. ``bearer`` is consumed explicitly
    # because a bare ``\S+`` stops at the space after it and leaves the token
    # itself in the clear -- which is the exact shape of an HTTP auth header.
    r"|(?:authorization|access[-_]?token|refresh[-_]?token|token|password|passwd"
    r"|secret|credential|api[-_]?key|access[-_]?key|private[-_]?key)"
    r"\s*[:=]\s*(?:bearer\s+)?\S+"
    # ``Authorization: Bearer <token>`` reached by any other wording.
    r"|bearer\s+[A-Za-z0-9._~+/-]{8,}={0,2}"
    # Credentials in a URL's userinfo carry no keyword at all. The lookaround
    # keeps the scheme and host readable so the diagnostic stays useful.
    r"|(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"
    # Vendor keys that carry their own prefix, so they appear with no keyword
    # and no delimiter in front of them. The AWS alternative opts out of the
    # global ``(?i)`` because its identifiers are upper-case by construction and
    # a case-blind match would fire on ordinary lower-case words.
    r"|sk-ant-[A-Za-z0-9_-]{16,}"
    r"|(?-i:(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16})"
    # ``.netrc`` is space-separated, so the ``[:=]`` branch cannot see it.
    # Requiring both keywords adjacent keeps prose that merely mentions a login
    # or a password from matching, and leaves the ``machine`` host readable.
    r"|\blogin\s+\S+\s+password\s+\S+"
    # A PEM private key: the base64 body is the credential, so the whole block
    # goes and not just its header. ``sanitize_result_text`` collapses newlines
    # before substituting, so a block arrives here on one line; used as a
    # rejection predicate the header alone still matches, which is all that
    # branch needs.
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----(?s:.*?-----END [A-Z ]*PRIVATE KEY-----)?"
    r")"
)
_TOP_KEYS = {
    "schema_version",
    "tick_id",
    "todo_id",
    "step_key",
    "verdict",
    "git",
    "acceptance",
}
_OPTIONAL_TOP_KEYS = {
    "delivery",
    # Parse-and-ignore, for in-flight cards only. A review card written before
    # the review-round machinery was removed published a ``review`` section and
    # has a 2400s timeout, so a card can still be open across the upgrade. The
    # exact-key check below turns an unexpected key into ``malformed_result``,
    # which no card can ever recover from -- ``reconcile_reviews`` would return
    # False every tick forever with nothing blocked. Nothing reads, validates
    # or echoes the value; it is tolerated so an open card can land. Drop this
    # entry (and bump ``SCHEMA_VERSION`` in the same change, because that is
    # the release where acceptance genuinely narrows) in the release after the
    # one this comment ships in -- 0.15.0 -- by which point no card created
    # under the old contract can still be open.
    "review",
}
# Sections ``render_result_template`` knows how to publish. Deliberately
# narrower than ``_OPTIONAL_TOP_KEYS``: ``review`` is parsed and ignored, never
# rendered, so asking for it must stay an error rather than silently emitting a
# template with no such section in it.
_RENDERABLE_SECTIONS = {"delivery"}
# Nested key sets are shared by the validators below and by the template the
# worker-facing cards publish, so a contract change cannot silently stop being
# documented.
_GIT_KEYS = {
    "expected_parent_sha",
    "resulting_head_sha",
    "task_commit_sha",
    "changed_files",
}
_COMMAND_KEYS = {"command", "exit_code"}
_ACCEPTANCE_ENTRY_KEYS = {"criterion", "status"}
_DELIVERY_KEYS = {"pr_url", "branch", "head_sha", "checks"}
# The heading a card body carries when it publishes the template below;
# card builders key the dispatcher's instruction off it so a card can never
# promise metadata it did not publish.
RESULT_TEMPLATE_HEADING = "Required result metadata:"


class ResultContractError(RuntimeError):
    """Worker evidence is missing, malformed, or inconsistent."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int


@dataclass(frozen=True)
class GitResult:
    expected_parent_sha: str
    resulting_head_sha: str
    task_commit_sha: str
    changed_files: tuple[str, ...]


@dataclass(frozen=True)
class WorkerResult:
    tick_id: str
    todo_id: str
    step_key: str
    git: GitResult
    delivery: DeliveryEvidence | None = None


@dataclass(frozen=True)
class DeliveryEvidence:
    pr_url: str
    branch: str
    head_sha: str
    checks: tuple[CommandResult, ...]


@dataclass(frozen=True)
class ValidatedRegistration:
    todo_id: str
    repository: Path
    base_sha: str
    issue_number: int
    issue_url: str
    plan_path: str | None
    branch: str
    worktree: Path
    step_keys: tuple[str, ...]
    manifest: PlanManifest | None
    assignee: str
    review_assignee: str | None
    prompt_client: str
    # The hash verified against the Plan bytes the contract itself read, whether
    # from ``base_sha:plan_path`` or the embedded artifact. A caller holding the
    # Plan text independently can re-pin it without re-reading the
    # (worker-writable) registration file. Declared before the defaulted fields
    # below: a field without a default cannot follow one that has it.
    plan_hash: str
    plan_source_kind: str = "legacy_path"
    plan_reference: PlanReference | None = None
    plan_source: PlanSource | None = None
    # The phase profile pinned when the run registered. The review and delivery
    # reconcilers render their cards from this profile's prompts, so it must be
    # the run's own profile, not whatever the project config says now.
    profile: str = ""
    agent_policy_mode: AgentPolicyMode = "inherit"
    supervised_execution: bool = False


def sanitize_result_text(value: object, *, maximum: int) -> str:
    """Return bounded, display-safe diagnostics without credential material."""
    text = _CONTROL_RE.sub("", str(value))
    text = _LINE_BREAK_RE.sub(" ", text)
    text = SECRET_RE.sub("[REDACTED]", text)
    return text[:maximum]


def _reject_unsafe_strings(value: object) -> None:
    # The key branch below no longer decides anything: every dict this function
    # is now handed is ``_exact_keys``-checked (``tpo_result`` at every level,
    # and the registration mapping before its own scan), and an unknown
    # top-level ``tpo_result`` key raises ``malformed_result``, so an unsafe key
    # always rejects anyway -- dropping the branch would change only which code
    # is raised, never accept-vs-reject. Removing the enclosing-envelope scan
    # removed the only caller that ever saw arbitrary keys. Kept rather than
    # deleted: a future caller passing looser data should not silently lose key
    # coverage, and the code it does decide is pinned by the
    # ``unsafe-top-level-key`` case of
    # ``test_parse_rejects_secrets_and_controls_at_any_metadata_depth``.
    if isinstance(value, str):
        if _CONTROL_RE.search(value) or SECRET_RE.search(value):
            raise ResultContractError("unsafe_metadata")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_unsafe_strings(key)
            _reject_unsafe_strings(item)
    elif isinstance(value, list):
        for item in value:
            _reject_unsafe_strings(item)


def _mapping(value: object, *, code: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ResultContractError(code)
    return value


def _exact_keys(value: dict[str, object], keys: set[str], *, code: str) -> None:
    if set(value) != keys:
        raise ResultContractError(code, "unexpected or missing fields")


def _bounded_string(value: object, *, maximum: int, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ResultContractError("size_limit" if isinstance(value, str) and len(value) > maximum else code)
    if _CONTROL_RE.search(value) or SECRET_RE.search(value):
        raise ResultContractError("unsafe_metadata")
    if _PLACEHOLDER_RE.fullmatch(value):
        raise ResultContractError(code, "unfilled template placeholder")
    return value


def _command(value: object, *, name: str) -> CommandResult:
    item = _mapping(value, code="invalid_command")
    _exact_keys(item, _COMMAND_KEYS, code="invalid_command")
    command = _bounded_string(
        item["command"], maximum=MAX_COMMAND_LENGTH, code="invalid_command"
    )
    exit_code = item["exit_code"]
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise ResultContractError("invalid_command", name)
    return CommandResult(command, exit_code)


def _successful_runs(payload: dict[str, object]) -> list[dict[str, object]]:
    runs = payload.get("runs")
    if not isinstance(runs, list):
        raise ResultContractError("malformed_payload")
    successful: list[dict[str, object]] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        status = run.get("status")
        outcome = run.get("outcome")
        if status in {"success", "succeeded", "completed", "done"} or outcome in {
            "success",
            "succeeded",
            "completed",
            "done",
        } or (
            status is None and run.get("exit_code") == 0
        ):
            successful.append(run)
    if not successful:
        raise ResultContractError("missing_successful_run")
    return successful


def manifest_acceptance_criteria(manifest) -> tuple[str, ...]:
    """Every Plan task's acceptance criteria, concatenated in Plan order.

    One card implements the whole Plan, so its single report answers for every
    task's criteria. The order is the Plan's own, which is the order
    ``render_result_template`` prefills and ``parse_worker_result`` demands back.
    """
    return tuple(
        criterion
        for task in manifest.tasks
        for criterion in task.acceptance_criteria
    )


def parse_worker_result(
    payload: object,
    *,
    tick_id: str,
    todo_id: str,
    step_key: str,
    acceptance_criteria: tuple[str, ...],
    allow_no_changes: bool = False,
) -> WorkerResult:
    """Parse ``metadata.tpo_result`` from the final successful Hermes run."""
    envelope = _mapping(payload, code="malformed_payload")
    run = _successful_runs(envelope)[-1]
    # ``run["summary"]`` is deliberately not read, bounded, or scanned. Hermes
    # requires a closing summary and stores it verbatim, but it is free-form
    # dispatcher diagnostics that nothing in TPO consumes or echoes, so any
    # check on it can only reject: a closed run's data is immutable and a
    # rejection opens no card, so ANSI colour, a "token: expired" log line, or
    # an over-long tail would wedge the step forever. Diagnostics that TPO does
    # surface are sanitized at the point of use with ``sanitize_result_text``.
    metadata = _mapping(run.get("metadata"), code="missing_result")
    try:
        metadata_encoded = json.dumps(
            metadata, ensure_ascii=False, separators=(",", ":")
        ).encode()
    except (TypeError, ValueError) as exc:
        raise ResultContractError("malformed_result") from exc
    if len(metadata_encoded) > MAX_METADATA_BYTES:
        raise ResultContractError("size_limit", "metadata")
    # Do not restore an exactness (or subset) check on this mapping, nor an
    # unsafe-string scan over it. The worker supplies the whole envelope, and
    # live runs routinely carry extra worker-authored keys beside ``tpo_result``
    # (``notes``, ``findings``, ``commit_message``, ...), so any key check here
    # stalls every step -- and so did scanning them: a sibling this function
    # never reads, and that nothing anywhere echoes into a log, notification,
    # report, comment, issue or card, once rejected the whole result forever.
    # Those siblings are untrusted and are simply never read; the trust comes
    # from ``tpo_result``, which is exact-key-checked and scanned at every level
    # below, while the size bound above -- a resource guard, not a content one
    # -- still covers the whole mapping. ``worker_session_id`` is the one key
    # Hermes stamps, but only on its own tool path and without verification on
    # the plain CLI path, so it is forgeable and is never an authenticity
    # signal.
    raw = _mapping(metadata.get("tpo_result"), code="missing_result")
    try:
        encoded = json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise ResultContractError("malformed_result") from exc
    if len(encoded) > MAX_METADATA_BYTES:
        raise ResultContractError("size_limit", "metadata")
    # ``acceptance`` is exempt for the same reason ``issue_snapshot`` is in
    # ``load_validated_registration``: it is hash-pinned authority content, not
    # agent metadata. Every criterion must equal ``acceptance_criteria``
    # exactly, and those come from the Plan manifest that TPO itself renders
    # into the worker-facing card -- so the text is TPO's own, scanning it
    # protects nothing, and scanning it made any TODO whose criteria mention a
    # token, password, authorization header or secret impossible to complete.
    # The equality check below is what actually constrains its content, and the
    # Plan manifest bounds each criterion to ``MAX_CRITERION_LENGTH`` and
    # rejects the same control characters ``_CONTROL_RE`` does -- its rule is
    # pinned identical to this module's, because this exemption makes it the
    # only filter left for Trojan-Source-style criteria -- so only Plan-authored
    # text can survive. Everything else here stays scanned: those values are
    # consumed and compared.
    #
    # This exemption is only correct while the enclosing-envelope scan above
    # stays deleted: that scan recursed transitively into
    # ``metadata["tpo_result"]["acceptance"]``, so restoring it would silently
    # undo the exemption and bring the wedge back. The two loosenings are one
    # change, not two.
    #
    # ``review`` is exempt for a different reason: it is the in-flight-only
    # key described on ``_OPTIONAL_TOP_KEYS``, never read and never echoed, so
    # scanning it protects nothing and can only reject -- and rejecting is the
    # precise failure the tolerance exists to avoid, since a review section's
    # findings prose is exactly the sort of text that mentions a token or an
    # authorization header.
    _reject_unsafe_strings({
        key: value for key, value in raw.items()
        if key not in ("acceptance", "review")
    })
    if not _TOP_KEYS <= set(raw) or set(raw) - _TOP_KEYS - _OPTIONAL_TOP_KEYS:
        raise ResultContractError("malformed_result", "unexpected or missing fields")
    if raw["schema_version"] != SCHEMA_VERSION or raw["verdict"] != "success":
        raise ResultContractError("invalid_verdict")
    identities = (raw["tick_id"], raw["todo_id"], raw["step_key"])
    if identities != (tick_id, todo_id, step_key):
        raise ResultContractError("identity_mismatch")

    git = _mapping(raw["git"], code="invalid_git")
    _exact_keys(git, _GIT_KEYS, code="invalid_git")
    shas = [git[key] for key in ("expected_parent_sha", "resulting_head_sha", "task_commit_sha")]
    if not all(isinstance(sha, str) and _SHA_RE.fullmatch(sha) for sha in shas):
        raise ResultContractError("invalid_git")
    if shas[1] != shas[2]:
        raise ResultContractError("invalid_git", "head and task commit differ")
    files = git["changed_files"]
    if (
        not isinstance(files, list)
        or (not files and not allow_no_changes)
        or len(files) != len(set(files))
    ):
        raise ResultContractError("invalid_git", "changed_files")
    for filename in files:
        if not isinstance(filename, str) or not filename or len(filename) > 500:
            raise ResultContractError("invalid_git", "changed_files")
        if _PLACEHOLDER_RE.fullmatch(filename):
            raise ResultContractError("invalid_git", "unfilled template placeholder")
        path = PurePosixPath(filename)
        if path.is_absolute() or ".." in path.parts or "\\" in filename:
            raise ResultContractError("invalid_git", "unsafe changed file")
    git_result = GitResult(shas[0], shas[1], shas[2], tuple(files))

    acceptance = raw["acceptance"]
    if not isinstance(acceptance, list) or len(acceptance) != len(acceptance_criteria):
        raise ResultContractError("invalid_acceptance")
    observed: list[str] = []
    for item in acceptance:
        entry = _mapping(item, code="invalid_acceptance")
        _exact_keys(entry, _ACCEPTANCE_ENTRY_KEYS, code="invalid_acceptance")
        if entry["status"] != "passed" or not isinstance(entry["criterion"], str):
            raise ResultContractError("invalid_acceptance")
        observed.append(entry["criterion"])
    if tuple(observed) != acceptance_criteria:
        raise ResultContractError("invalid_acceptance", "criteria mismatch")
    delivery_evidence = None
    if "delivery" in raw:
        delivery_evidence = _validate_delivery(raw["delivery"])
    return WorkerResult(tick_id, todo_id, step_key, git_result, delivery_evidence)


def _real_changed_paths(cwd: Path, first: str, second: str) -> tuple[str, ...]:
    """The repository's own diff paths for ``first..second``, sorted.

    ``-z`` is the load-bearing flag, and it is load-bearing TWICE over. Under
    ``-z`` git does not munge pathnames at all (its own words) and terminates
    each with a NUL, so the flag both suppresses C-quoting and removes the need
    for a delimiter no path can contain.

    Without it, ``diff --name-only`` C-quotes any "unusual" path. Two kinds
    qualify, and ``parse_worker_result`` rejects any reported path containing a
    backslash as unsafe, so for both kinds the quoted listing is unreportable
    while the real name never equals it -- a permanent
    ``changed_files_mismatch`` with no value the worker could have reported
    instead:

    * non-ASCII bytes -- ``"caf\\303\\251.txt"`` for ``café.txt``;
    * a control character -- ``"docs/two\\nline.md"``. Such a path is
      reportable (``_CONTROL_RE`` matches none of ``\n``, ``\r``, ``\t``, and
      the ``changed_files`` validator rejects only a backslash, an absolute
      path and ``..``), and it also defeats any line-delimited listing.

    ``-c core.quotePath=false`` is NOT a second load-bearing flag and does not
    divide the cases with ``-z``: verified against git 2.50, ``-z`` suppresses
    quoting on its own and ``core.quotePath`` changes nothing while it is set,
    so no test can kill this flag alone. It is kept only so that dropping
    ``-z`` degrades to the non-ASCII case still working rather than to silent
    corruption. ``core.quotePath`` governs non-ASCII bytes ONLY, so it is not a
    substitute for ``-z``: swapping ``-z`` for ``quotePath=false`` plus
    line-splitting still corrupts the control-character case, which is what
    ``test_a_commit_touching_a_path_with_a_control_character_can_be_reported``
    pins.

    ``--name-only`` reports only a rename's destination path, never its source;
    that is the set the contract asks the worker for.
    """
    raw = _git_bytes(
        cwd, "-c", "core.quotePath=false", "diff", "--name-only", "-z", first, second,
    )
    return tuple(
        sorted(
            name
            for name in raw.decode("utf-8", "surrogateescape").split("\0")
            if name
        )
    )


def _require_real_commits(cwd: Path, *shas: str) -> None:
    """Reject a reported SHA that names no commit, before any topology query.

    A worker can report a perfectly valid-shaped 40-hex SHA that simply does not
    exist. Every topology query then exits 128 -- ``merge-base --is-ancestor``,
    ``rev-parse <sha>^`` and ``rev-list --count`` all refuse an unknown object --
    and every git-failure helper here collapses that into
    ``git_verification_failed``, whose meaning is "git could not answer".
    ``_verify_finish`` passes that code through on purpose so an operator reads
    a broken worktree as broken, so a fabricated report was being attributed to
    broken infrastructure rather than to the worker. Proving existence first
    keeps ``git_verification_failed`` meaning only what it says.

    ``rev-parse --verify --quiet <sha>^{commit}`` and not ``cat-file -e <sha>``:
    a tree's or blob's own SHA is a valid object, so ``cat-file -e`` answers yes
    for it while no commit by that name exists. Nor ``cat-file -e
    <sha>^{commit}``, which exits 128 for an unknown name and so would raise the
    very code this check exists to avoid; ``rev-parse --verify --quiet`` exits 1,
    which ``_git_predicate`` reads as a clean "no" and reserves >= 2 for a git
    that genuinely cannot run.
    """
    for sha in dict.fromkeys(shas):
        if not _git_predicate(
            cwd, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"
        ):
            raise ResultContractError("unknown_commit", sha)


def _on_first_parent_mainline(cwd: Path, anchor: str, commit: str) -> bool:
    """True when ``commit`` is ``anchor`` itself or on HEAD's first-parent line.

    ``merge-base --is-ancestor <commit> HEAD`` is not this predicate: a merge's
    SECOND parent satisfies it. A commit built off ``anchor`` with arbitrary
    content and merged in as a side parent was therefore accepted as "reachable"
    even though it never sat on the branch mainline the run delivers -- verified
    against a real merge commit, where ``is-ancestor`` answers yes for the side
    parent and ``rev-list --first-parent`` omits it. Walking first parents only
    follows the mainline, which is the history a reviewer and a human merge gate
    actually see.
    """
    if commit == anchor:
        return True
    return commit in {
        line
        for line in _git(
            cwd, "rev-list", "--first-parent", f"{anchor}..HEAD"
        ).splitlines()
        if line
    }


def verify_optional_single_commit(
    worktree: Path,
    git: GitResult,
    *,
    expected_parent_sha: str,
    require_current: bool = True,
) -> None:
    """Verify a card that the phase profile allows to add at most one commit.

    The profile's ``phase_5_review`` applies its own findings as one review-fix
    commit and ``phase_8_finish_branch`` commits release/PR metadata as one
    separate commit; both may also legitimately add nothing. So the topology
    this proves is "``expected_parent_sha`` is still the anchor, and HEAD is it
    or exactly one commit past it" -- not the equality
    ``verify_worker_git_topology`` demands of a Plan task, which must produce
    exactly one commit, and not the frozen head the deleted read-only-review
    check demanded.

    Everything else the stricter checks prove is preserved: the reported parent
    must be the anchor we computed, the reported commit must really have that
    parent, its real diff must match ``changed_files``, and the reported head
    must be on HEAD's first-parent mainline -- mainline membership being the
    only fact that survives a ``git reset --hard`` of the reported commit.

    ``git.resulting_head_sha == git.task_commit_sha`` is not re-checked here:
    the only ``GitResult`` any production caller can hold came from
    ``parse_worker_result``, which rejects that pair as ``invalid_git`` before
    a value is ever constructed, so this function has no path that can observe
    them differing.
    """
    if git.expected_parent_sha != expected_parent_sha:
        raise ResultContractError("parent_mismatch")
    _require_real_commits(worktree, git.resulting_head_sha, git.task_commit_sha)
    if not _git_predicate(
        worktree, "merge-base", "--is-ancestor", expected_parent_sha,
        git.resulting_head_sha,
    ):
        raise ResultContractError("parent_mismatch")
    count = _git(
        worktree, "rev-list", "--count",
        f"{expected_parent_sha}..{git.resulting_head_sha}",
    )
    if count not in ("0", "1"):
        raise ResultContractError("commit_count_mismatch")
    if count == "0":
        if git.resulting_head_sha != expected_parent_sha or git.changed_files:
            raise ResultContractError("changed_files_mismatch")
    else:
        parent = _git(worktree, "rev-parse", f"{git.task_commit_sha}^")
        if parent != expected_parent_sha:
            raise ResultContractError("parent_mismatch")
        changed = _real_changed_paths(
            worktree, expected_parent_sha, git.resulting_head_sha
        )
        if changed != tuple(sorted(git.changed_files)):
            raise ResultContractError("changed_files_mismatch")
    if not _on_first_parent_mainline(
        worktree, expected_parent_sha, git.resulting_head_sha
    ):
        raise ResultContractError("unreachable_commit")
    if not require_current:
        return
    if _git(worktree, "rev-parse", "HEAD") != git.resulting_head_sha:
        raise ResultContractError("head_mismatch")
    if _git_bytes(worktree, "status", "--porcelain=v1", "--untracked-files=all", "-z"):
        raise ResultContractError("worktree_dirty")


def _validate_delivery(value: object) -> DeliveryEvidence:
    delivery = _mapping(value, code="invalid_delivery")
    _exact_keys(delivery, _DELIVERY_KEYS, code="invalid_delivery")
    pr_url = _bounded_string(delivery["pr_url"], maximum=1000, code="invalid_delivery")
    if not re.fullmatch(
        r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/\d+", pr_url
    ):
        raise ResultContractError("invalid_delivery")
    _bounded_string(delivery["branch"], maximum=256, code="invalid_delivery")
    if not isinstance(delivery["head_sha"], str) or not _SHA_RE.fullmatch(
        delivery["head_sha"]
    ):
        raise ResultContractError("invalid_delivery")
    checks = delivery["checks"]
    if not isinstance(checks, list) or not checks or len(checks) > 50:
        raise ResultContractError("invalid_delivery")
    parsed_checks = tuple(_command(check, name="delivery check") for check in checks)
    if any(check.exit_code != 0 for check in parsed_checks):
        raise ResultContractError("invalid_delivery", "failed check")
    return DeliveryEvidence(pr_url, str(delivery["branch"]), str(delivery["head_sha"]), parsed_checks)


def _require_keys(values: dict[str, object], keys: set[str], *, name: str) -> dict[str, object]:
    """Fail loudly when the published template drifts from the parsed contract."""
    if set(values) != keys:
        raise ResultContractError("template_out_of_date", name)
    return values


def _template_git(allow_no_changes: bool) -> dict[str, object]:
    """Render the git block every card must fill from the worktree it owns.

    No card pins its SHAs any more. The phase profile lets ``phase_5_review``
    and ``phase_8_finish_branch`` add one commit each, so their heads are not
    knowable when the card is written; ``allow_no_changes`` only says that
    adding nothing is also legitimate.

    ``changed_files`` is a placeholder in both modes and never a pre-filled
    ``[]``. The template's standing instruction is to replace every ``<...>``
    placeholder and to "keep the pre-filled values verbatim", so an obedient
    worker keeps a pre-filled empty list -- and both cards ``allow_no_changes``
    covers are cards the profile *mandates* a commit from (``phase_5_review``
    whenever it has a finding to apply, ``phase_8_finish_branch`` always: "commit
    those as one separate atomic commit"). The real diff is then non-empty, the
    report says ``[]``, and ``verify_optional_single_commit`` raises
    ``changed_files_mismatch`` -- a fully compliant worker rejected, and for the
    finish card that means delivery could never complete at all. The
    conditional belongs in the placeholder's own words, not in a value the
    worker is told not to touch.
    """
    return _require_keys(
        {
            "expected_parent_sha": "<40-hex SHA of HEAD before the task commit>",
            "resulting_head_sha": "<40-hex SHA of HEAD after the task commit>",
            "task_commit_sha": "<40-hex SHA of the task commit; same as resulting_head_sha>",
            "changed_files": [
                "<repo-relative path the commit changed, or drop this entry"
                " entirely if no commit was made>"
                if allow_no_changes
                else "<repo-relative path the task commit changed>"
            ],
        },
        _GIT_KEYS,
        name="git",
    )


def _template_command(description: str, exit_code: int) -> dict[str, object]:
    return _require_keys(
        {
            "command": f"<{description}>",
            "exit_code": exit_code,
        },
        _COMMAND_KEYS,
        name="command",
    )


def _template_delivery(*, branch: str) -> dict[str, object]:
    """Render the delivery section; the branch is a registration fact."""
    return _require_keys(
        {
            # A description, like every other placeholder. Brackets wrapping
            # the URL's own shape ("<https://github.com/OWNER/REPO/pull/N>")
            # invite a worker to fill the parts in place and keep the
            # brackets, which the contract rejects as an unfilled placeholder.
            "pr_url": "<URL of the pull request the external client opened>",
            "branch": branch,
            "head_sha": (
                "<40-hex SHA the branch was pushed at; same as resulting_head_sha>"
            ),
            "checks": [_template_command("required repository gate the client ran", 0)],
        },
        _DELIVERY_KEYS,
        name="delivery",
    )


def render_result_template(
    *,
    tick_id: str,
    todo_id: str,
    step_key: str,
    acceptance_criteria: tuple[str, ...] = (),
    section: str | None = None,
    branch: str | None = None,
    allow_no_changes: bool = False,
) -> str:
    """Render the ``metadata.tpo_result`` template a card's delegation block publishes.

    The template is addressed to the Hermes dispatcher, not to the external
    client: the dispatcher launched the process and closes the card, so it is
    the only party that can write ``metadata.tpo_result`` at all. It is derived
    from the same constants ``parse_worker_result`` enforces, with every
    pipeline-known value pre-filled, so the dispatcher only supplies facts it
    can observe -- the Git topology of the worktree it owns, and what the client
    reported. ``section`` names the optional sub-object the card's reconciler
    requires (only ``delivery`` now that a review carries no findings object).
    """
    if section is not None and section not in _RENDERABLE_SECTIONS:
        raise ResultContractError("unknown_result_section", str(section))
    if section == "delivery" and branch is None:
        raise ResultContractError("incomplete_result_section", "delivery")
    template: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "tick_id": tick_id,
        "todo_id": todo_id,
        "step_key": step_key,
        "verdict": "success",
        "git": _template_git(allow_no_changes),
        "acceptance": [
            _require_keys(
                {"criterion": criterion, "status": "passed"},
                _ACCEPTANCE_ENTRY_KEYS,
                name="acceptance",
            )
            for criterion in acceptance_criteria
        ],
    }
    _require_keys(template, _TOP_KEYS, name="tpo_result")
    if section == "delivery":
        template[section] = _template_delivery(branch=branch)
    lines = [
        RESULT_TEMPLATE_HEADING,
        "Close this card with metadata.tpo_result set to exactly this object, "
        "replacing every <...> placeholder with the real value. Add no other "
        "keys, drop none, and keep the pre-filled values verbatim.",
        "```json",
        json.dumps(template, indent=2, ensure_ascii=False),
        "```",
    ]
    if section == "delivery":
        lines.append(
            "checks lists every required gate the external client reported "
            "running, not just one, each with its exact command and real exit "
            "code -- replace the pre-filled 0 if a gate exited non-zero, and "
            "expect the card to be rejected, because delivery requires every "
            "gate to pass."
        )
    # Always stated, in both modes. Suppressing it whenever a card may
    # legitimately add nothing left the one field whose correct value is
    # conditional as the one field with no prose explaining the condition.
    lines.append(
        "changed_files lists every repo-relative path in the commit that was "
        "made, deduplicated, exactly as the repository records it -- and is []"
        " only when no commit was made"
        + (
            "."
            if allow_no_changes
            else ", which this card does not permit: it must name the task "
            "commit's paths and must not be empty."
        )
    )
    return "\n".join(lines) + "\n"


_REGISTRATION_KEYS = {
    "schema_version",
    "tick_id",
    "todo_id",
    "repository",
    "base_sha",
    "issue_number",
    "issue_url",
    "issue_snapshot",
    "selected_entry_hash",
    "plan_path",
    "plan_hash",
    "branch",
    "worktree",
    "profile",
    "prompt_client",
    "assignee",
    "review_assignee",
    "step_keys",
}
_REGISTRATION_V3_KEYS = _REGISTRATION_KEYS | {"plan_source_kind", "plan_artifact"}
_REGISTRATION_V4_KEYS = _REGISTRATION_V3_KEYS | {"agent_policy_mode"}


def load_validated_registration(
    project_dir: Path, state_dir: Path, tick_id: str, *, repo: str | None = None
) -> ValidatedRegistration:
    """Load exact registration authority and verify it against its pinned issue snapshot.

    ``repo`` defaults to the live ``origin`` identity; tests inject it.
    """
    path = state_dir / "runs" / tick_id / "registration.json"
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResultContractError("registration_invalid") from exc
    registration = _mapping(raw, code="registration_invalid")
    schema_version = registration.get("schema_version")
    if type(schema_version) is not int or schema_version not in SUPPORTED_REGISTRATION_SCHEMA_VERSIONS:
        raise ResultContractError("registration_invalid", "unsupported schema_version")
    _exact_keys(
        registration,
        {2: _REGISTRATION_KEYS, 3: _REGISTRATION_V3_KEYS, 4: _REGISTRATION_V4_KEYS, 5: _REGISTRATION_V4_KEYS}[schema_version],
        code="registration_invalid",
    )
    agent_policy_mode: AgentPolicyMode = "inherit"
    if schema_version == 4:
        if (
            registration["agent_policy_mode"] != "delegated"
            or registration["profile"] != "native-sdd"
        ):
            raise ResultContractError("registration_invalid", "agent policy mode")
        agent_policy_mode = "delegated"
    elif schema_version == 5:
        mode = registration["agent_policy_mode"]
        if mode not in ("inherit", "delegated") or (mode == "delegated" and registration["profile"] != "native-sdd"):
            raise ResultContractError("registration_invalid", "agent policy mode")
        agent_policy_mode = mode
    # The issue snapshot is hash-pinned authority content, not agent metadata:
    # bound its size instead of scanning it for secret-like text.
    _reject_unsafe_strings({key: value for key, value in registration.items() if key != "issue_snapshot"})
    if (
        not isinstance(registration["issue_snapshot"], str)
        or len(registration["issue_snapshot"]) > MAX_ISSUE_SNAPSHOT_CHARS
    ):
        raise ResultContractError("registration_invalid", "issue snapshot size")
    if registration["tick_id"] != tick_id:
        raise ResultContractError("registration_invalid")
    string_keys = (
        "todo_id",
        "repository",
        "base_sha",
        "issue_url",
        "issue_snapshot",
        "selected_entry_hash",
        "plan_hash",
        "branch",
        "worktree",
        "profile",
        "prompt_client",
        "assignee",
    )
    if not all(isinstance(registration[key], str) and registration[key] for key in string_keys):
        raise ResultContractError("registration_invalid")
    if schema_version == 2:
        if not isinstance(registration["plan_path"], str) or not registration["plan_path"]:
            raise ResultContractError("registration_invalid")
        plan_source_kind = "legacy_path"
    else:
        plan_source_kind = registration["plan_source_kind"]
        if plan_source_kind not in ("embedded", "legacy_path"):
            raise ResultContractError("registration_invalid")
        if plan_source_kind == "legacy_path" and (
            not isinstance(registration["plan_path"], str)
            or not registration["plan_path"]
        ):
            raise ResultContractError("registration_invalid")
        expected_path = None if plan_source_kind == "embedded" else registration["plan_path"]
        expected_artifact = "plan.md" if plan_source_kind == "embedded" else None
        if registration["plan_path"] != expected_path or registration["plan_artifact"] != expected_artifact:
            raise ResultContractError("registration_invalid")
    issue_number = registration["issue_number"]
    if type(issue_number) is not int or issue_number <= 0:
        raise ResultContractError("registration_invalid")
    if registration["review_assignee"] is not None and (
        not isinstance(registration["review_assignee"], str)
        or not registration["review_assignee"]
    ):
        raise ResultContractError("registration_invalid")
    # ``profile`` selects a bundled data directory below; keep it to a plain
    # profile name so a tampered registration cannot walk out of it.
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", registration["profile"]):
        raise ResultContractError("registration_invalid", "profile")
    if not _SHA_RE.fullmatch(registration["base_sha"]) or not re.fullmatch(
        r"[0-9a-f]{64}", registration["selected_entry_hash"]
    ) or not re.fullmatch(r"[0-9a-f]{64}", registration["plan_hash"]):
        raise ResultContractError("registration_invalid")
    steps = registration["step_keys"]
    if (
        not isinstance(steps, list)
        or not steps
        or len(steps) > 200
        or not all(isinstance(step, str) and 0 < len(step) <= 256 for step in steps)
        or len(steps) != len(set(steps))
    ):
        raise ResultContractError("registration_invalid")

    repository = Path(registration["repository"]).resolve()
    worktree = Path(registration["worktree"]).resolve()
    if repository != project_dir.resolve() or worktree.parent != repository / ".worktrees":
        raise ResultContractError("registration_invalid")
    common = Path(_git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    branch = _git(worktree, "branch", "--show-current")
    if common != (repository / ".git").resolve() or branch != registration["branch"]:
        raise ResultContractError("registration_invalid")

    plan_path = registration["plan_path"]
    if plan_source_kind == "embedded":
        # Runtime consumers switch to this verified artifact in Task 3.  The
        # reader nevertheless validates v3 authority now so mixed-version
        # active runs remain inspectable.
        from .run_registration import RunRegistrationError, _read_verified_artifact

        try:
            plan_bytes = _read_verified_artifact(
                path.parent / "plan.md", registration["plan_hash"]
            )
        except RunRegistrationError as exc:
            raise ResultContractError("registration_invalid", "plan artifact") from exc
    else:
        assert isinstance(plan_path, str)
        relative = PurePosixPath(plan_path)
        if relative.is_absolute() or ".." in relative.parts or "\\" in plan_path:
            raise ResultContractError("registration_invalid")
        base_sha = registration["base_sha"]
        try:
            plan_bytes = _git_bytes(repository, "show", f"{base_sha}:{plan_path}")
        except ResultContractError as exc:
            # Here a git failure really is a registration claim being false:
            # the registration names a Plan path the pinned base commit does
            # not carry. Every other ``_git_bytes`` caller means "git could
            # not answer", which is why the helper's own code is now
            # ``git_verification_failed``.
            raise ResultContractError("registration_invalid", "plan path") from exc
    base_sha = registration["base_sha"]
    if hashlib.sha256(plan_bytes).hexdigest() != registration["plan_hash"]:
        raise ResultContractError("registration_invalid")
    try:
        manifest = parse_plan_manifest(
            plan_bytes.decode("utf-8"), expected_todo_id=registration["todo_id"]
        )
    except (UnicodeError, ValueError) as exc:
        raise ResultContractError("registration_invalid") from exc
    snapshot = registration["issue_snapshot"]
    if snapshot_hash(snapshot) != registration["selected_entry_hash"]:
        raise ResultContractError("registration_invalid", "issue snapshot hash")
    try:
        snapshot_repo, number, title, body = split_canonical_snapshot(snapshot)
    except SnapshotFormatError as exc:
        raise ResultContractError("registration_invalid", "issue snapshot") from exc
    if repo is None:
        try:
            repo = github_issues.repository_identity(project_dir)
        except GitHubIssuesError as exc:
            raise ResultContractError("git_verification_failed", "remote") from exc
    if (
        snapshot_repo.lower() != repo.lower()
        or number != issue_number
        or registration["todo_id"] != f"TODO-{number}"
        or registration["issue_url"].lower()
        != f"https://github.com/{snapshot_repo}/issues/{number}".lower()
    ):
        raise ResultContractError("registration_invalid", "issue identity")
    if plan_source_kind == "embedded":
        try:
            pinned_source = github_issues.embedded_plan_source(
                body + "\n", expected_todo_id=registration["todo_id"]
            )
        except ValueError as exc:
            raise ResultContractError("registration_invalid", "embedded Plan") from exc
        if (
            pinned_source is None
            or pinned_source.plan_hash != registration["plan_hash"]
            or pinned_source.document.encode("utf-8") != plan_bytes
        ):
            raise ResultContractError("registration_invalid", "embedded Plan authority")
    sections = parse_issue_body(body)
    title_slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    expected_worktree = (
        repository / ".worktrees" / f"todo-{number}-{title_slug}"[:100].rstrip("-")
    ).resolve()
    if (
        worktree != expected_worktree
        or (
            plan_source_kind == "legacy_path"
            and github_issues.first_lines(sections.get("Plan", ())) != (plan_path,)
        )
        or github_issues.first_lines(sections.get("Branch", ())) != (registration["branch"],)
    ):
        raise ResultContractError("registration_invalid", "issue fields")
    if manifest is not None:
        # Subset, not equality: a run registered before the per-task controller
        # gate was dropped still lists its ``validate:<id>`` keys, and one
        # registered before the per-Plan-task fan-out was deleted still lists
        # its ``plan:<id>`` keys. Extra keys keep loading so a run that is only
        # ahead of its registration format can still be resumed.
        #
        # A pre-fan-out-deletion registration does NOT load, and cannot: the
        # implementation card's key is new rather than dropped, so the subset
        # check fails and the run is rejected at the trust boundary
        # (``registration_invalid``) instead of being verified against a card
        # shape that no longer exists. That is a fail-closed break, stated in
        # the major release note.
        if IMPLEMENTATION_KEY not in set(steps):
            raise ResultContractError("registration_invalid")
    plan_reference_value = (
        str((path.parent / "plan.md").resolve())
        if plan_source_kind == "embedded"
        else str(plan_path)
    )
    resolved_source = PlanSource(
        plan_source_kind,
        plan_bytes.decode("utf-8"),
        registration["plan_hash"],
        manifest,
        plan_path,
    )
    plan_reference = PlanReference(plan_reference_value, resolved_source)
    return ValidatedRegistration(
        registration["todo_id"],
        repository,
        base_sha,
        issue_number,
        registration["issue_url"],
        plan_path,
        registration["branch"],
        worktree,
        tuple(steps),
        manifest,
        registration["assignee"],
        registration["review_assignee"],
        registration["prompt_client"],
        registration["plan_hash"],
        plan_source_kind,
        plan_reference,
        resolved_source,
        registration["profile"],
        agent_policy_mode,
        schema_version >= 5,
    )


def _git(cwd: Path, *args: str) -> str:
    try:
        result = run_git(
            cwd, args, capture_output=True, text=True, check=False, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise ResultContractError("git_verification_failed") from exc
    if result.returncode != 0:
        raise ResultContractError("git_verification_failed", args[0])
    return result.stdout.strip()


def _git_predicate(cwd: Path, *args: str) -> bool:
    """Run a git query whose exit code 1 is an answer, not a failure.

    ``_git`` treats every non-zero exit as ``git_verification_failed``, which
    would collapse "false" (1) into "git is broken" (>= 2).
    """
    try:
        result = run_git(
            cwd, args, capture_output=True, text=True, check=False, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise ResultContractError("git_verification_failed") from exc
    if result.returncode not in (0, 1):
        raise ResultContractError("git_verification_failed", args[0])
    return result.returncode == 0


def _git_bytes(cwd: Path, *args: str) -> bytes:
    """``_git`` for output that must not be decoded or stripped.

    Raises ``git_verification_failed``, like every other git-failure helper in
    this module. It used to raise ``registration_invalid``, which was wrong for
    every caller but one: the worktree-clean checks run it long after the
    registration was validated, so a broken git surfaced as
    ``finish_review_head_mismatch: registration_invalid`` -- blaming the worker
    for delivering the wrong history AND the registration for being corrupt,
    neither of which had happened. The one caller for which
    ``registration_invalid`` is the right claim (``git show <base>:<plan>``,
    where a failure means the registration names a path the base commit does
    not carry) translates it at its own call site.
    """
    try:
        result = run_git(
            cwd, args, capture_output=True, check=False, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ResultContractError("git_verification_failed") from exc
    if result.returncode != 0:
        raise ResultContractError("git_verification_failed", args[0])
    return result.stdout


def verify_worker_git_result(
    worktree: Path, git: GitResult, *, expected_parent_sha: str,
    expected_commits: int = 1,
) -> None:
    """Verify immutable commit topology and changed paths in the pinned worktree."""
    verify_worker_git_topology(
        worktree, git, expected_parent_sha=expected_parent_sha,
        expected_commits=expected_commits,
    )
    status = _git_bytes(
        worktree, "status", "--porcelain=v1", "--untracked-files=all", "-z"
    )
    if status:
        raise ResultContractError("worktree_dirty")
    actual_head = _git(worktree, "rev-parse", "HEAD")
    if actual_head != git.resulting_head_sha:
        raise ResultContractError("head_mismatch")


def verify_worker_git_topology(
    worktree: Path, git: GitResult, *, expected_parent_sha: str,
    expected_commits: int = 1,
) -> None:
    """Verify immutable commit topology and reachability from the branch head.

    Parentage, commit count and changed files all keep holding for a commit that
    ``git reset --hard`` has discarded: the object survives in the repository.
    Membership of HEAD's first-parent mainline is the only fact that proves the
    reported work is on the branch this run delivers, so it is checked for every
    card, whether or not the stricter current-HEAD check in
    ``verify_worker_git_result`` applies. It is first-parent and not
    ``merge-base --is-ancestor`` for the reason ``_on_first_parent_mainline``
    records: a side merge parent satisfies ancestry without ever being on the
    mainline, and this function's own anchor would otherwise bless such a commit
    as implementation work.

    ``expected_commits`` is how many commits the phase profile obliges the card
    to make. It is 1 for a card the profile allows one commit, and it is the
    Plan's task count for the implementation card, whose profile prompt says
    "Stage explicit files or hunks and create exactly one atomic commit per Plan
    task" -- N tasks, therefore exactly N commits, no looser. The bound is
    expressed twice over, and both halves are needed:

    * ``rev-parse <head>~N == anchor`` -- walking first parents N times from the
      reported head lands exactly on the anchor. At N=1 this is literally the
      ``<head>^ == anchor`` check it replaces, so no existing caller loosens.
    * ``rev-list --count anchor..head == N`` -- and nothing else is reachable.
      Together these forbid a merge: a second parent bringing in anything not
      already reachable from the anchor raises the count above N, while the
      ``~N`` walk pins the mainline length. ``merge-base --is-ancestor`` was
      considered and rejected as the generalisation: with a count of N it admits
      a merge whose parents are both reachable from the anchor, which
      ``<head>~N`` refuses.

    The count is checked BEFORE the ``~N`` walk, and the order is load-bearing:
    ``rev-parse`` is fatal on a history shorter than N, and every git failure
    here collapses into ``git_verification_failed`` ("git could not answer"). An
    implementation card that made one commit for a three-task Plan is the most
    likely real failure of this check, and it must read as
    ``commit_count_mismatch`` -- the worker's fault -- not as broken
    infrastructure.
    """
    if git.expected_parent_sha != expected_parent_sha:
        raise ResultContractError("parent_mismatch")
    if git.resulting_head_sha != git.task_commit_sha:
        raise ResultContractError("head_mismatch")
    _require_real_commits(worktree, git.resulting_head_sha, git.task_commit_sha)
    count = _git(worktree, "rev-list", "--count", f"{expected_parent_sha}..{git.resulting_head_sha}")
    if count != str(expected_commits):
        raise ResultContractError("commit_count_mismatch")
    parent = _git(worktree, "rev-parse", f"{git.task_commit_sha}~{expected_commits}")
    if parent != expected_parent_sha:
        raise ResultContractError("parent_mismatch")
    changed = _real_changed_paths(
        worktree, expected_parent_sha, git.resulting_head_sha
    )
    if changed != tuple(sorted(git.changed_files)):
        raise ResultContractError("changed_files_mismatch")
    if not _on_first_parent_mainline(
        worktree, expected_parent_sha, git.resulting_head_sha
    ):
        raise ResultContractError("unreachable_commit")
