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
from .github_issues import (
    MAX_ISSUE_SNAPSHOT_CHARS,
    REGISTRATION_SCHEMA_VERSION,
    SUPPORTED_REGISTRATION_SCHEMA_VERSIONS,
    GitHubIssuesError,
    SnapshotFormatError,
    parse_issue_body,
    snapshot_hash,
    split_canonical_snapshot,
)
from .plan_manifest import PlanManifest, PlanReference, PlanSource, parse_plan_manifest

MAX_METADATA_BYTES = 64 * 1024
MAX_SUMMARY_LENGTH = 8 * 1024
MAX_COMMAND_LENGTH = 500
MAX_FINDINGS = 50
MAX_LOCATION_LENGTH = 256
MAX_FINDING_TEXT_LENGTH = 1000
SCHEMA_VERSION = 1
_SHA_RE = re.compile(r"[0-9a-f]{40}")
# A value that is wholly an unfilled template placeholder. Every placeholder the
# template renders is free of inner angle brackets, so excluding them keeps a
# legitimate wholly bracketed value -- "<script> tags ... <img onerror=x>" in a
# review finding -- from being read as an omission.
_PLACEHOLDER_RE = re.compile(r"<[^<>]*>")
# C0/DEL controls plus Unicode line/paragraph separators and bidi overrides.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]")
_LINE_BREAK_RE = re.compile(r"[\n\r\t\u2028\u2029]+")
_SECRET_RE = re.compile(
    r"(?i)(?:gh[pousr]_[A-Za-z0-9_]{20,}|(?:authorization|token|password|secret)\s*[:=]\s*\S+)"
)
_TOP_KEYS = {
    "schema_version",
    "tick_id",
    "todo_id",
    "step_key",
    "verdict",
    "external_session_id",
    "git",
    "tdd",
    "acceptance",
}
_OPTIONAL_TOP_KEYS = {"review", "delivery"}
# Nested key sets are shared by the validators below and by the template the
# worker-facing cards publish, so a contract change cannot silently stop being
# documented.
_GIT_KEYS = {
    "expected_parent_sha",
    "resulting_head_sha",
    "task_commit_sha",
    "changed_files",
}
_TDD_KEYS = {"red", "green", "refactor"}
_COMMAND_KEYS = {"command", "exit_code"}
_ACCEPTANCE_ENTRY_KEYS = {"criterion", "status"}
_REVIEW_KEYS = {"verdict", "findings"}
_FINDING_KEYS = {"priority", "location", "failure_scenario", "recommendation"}
_DELIVERY_KEYS = {"pr_url", "branch", "head_sha", "checks"}
_REVIEW_VERDICTS = ("clean", "findings")
_FINDING_PRIORITIES = ("P0", "P1", "P2", "P3")
# The heading a card body carries when it publishes the template below;
# card builders key the dispatcher's instruction off it so a card can never
# promise metadata it did not publish.
RESULT_TEMPLATE_HEADING = "Required result metadata:"
# Reported in the mandatory ``tdd`` block by a card the contract verifies as
# read-only, where no red-green-refactor cycle exists to report.
READ_ONLY_TDD_COMMAND = "read-only card: no TDD cycle"
READ_ONLY_TDD_EXIT_CODE = 127


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
    external_session_id: str
    git: GitResult
    red: CommandResult
    green: CommandResult
    refactor: CommandResult
    review: ReviewEvidence | None = None
    delivery: DeliveryEvidence | None = None


@dataclass(frozen=True)
class ReviewEvidence:
    verdict: str
    findings: tuple[dict[str, str], ...]


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


def sanitize_result_text(value: object, *, maximum: int) -> str:
    """Return bounded, display-safe diagnostics without credential material."""
    text = _CONTROL_RE.sub("", str(value))
    text = _LINE_BREAK_RE.sub(" ", text)
    text = _SECRET_RE.sub("[REDACTED]", text)
    return text[:maximum]


def _reject_unsafe_strings(value: object) -> None:
    if isinstance(value, str):
        if _CONTROL_RE.search(value) or _SECRET_RE.search(value):
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


def _bounded_string(
    value: object, *, maximum: int, code: str, allow_placeholder: bool = False
) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ResultContractError("size_limit" if isinstance(value, str) and len(value) > maximum else code)
    if _CONTROL_RE.search(value) or _SECRET_RE.search(value):
        raise ResultContractError("unsafe_metadata")
    if not allow_placeholder and _PLACEHOLDER_RE.fullmatch(value):
        raise ResultContractError(code, "unfilled template placeholder")
    return value


def _command(value: object, *, name: str) -> CommandResult:
    item = _mapping(value, code="invalid_tdd")
    _exact_keys(item, _COMMAND_KEYS, code="invalid_tdd")
    command = _bounded_string(item["command"], maximum=MAX_COMMAND_LENGTH, code="invalid_tdd")
    exit_code = item["exit_code"]
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise ResultContractError("invalid_tdd", name)
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
    summary = run.get("summary")
    if summary is not None:
        # Free-form diagnostics, not a template field, and discarded after
        # bounding: "<none>" is a legitimate summary.
        _bounded_string(
            summary, maximum=MAX_SUMMARY_LENGTH, code="invalid_summary",
            allow_placeholder=True,
        )
    metadata = _mapping(run.get("metadata"), code="missing_result")
    try:
        metadata_encoded = json.dumps(
            metadata, ensure_ascii=False, separators=(",", ":")
        ).encode()
    except (TypeError, ValueError) as exc:
        raise ResultContractError("malformed_result") from exc
    if len(metadata_encoded) > MAX_METADATA_BYTES:
        raise ResultContractError("size_limit", "metadata")
    _reject_unsafe_strings(metadata)
    _exact_keys(metadata, {"tpo_result"}, code="malformed_result")
    raw = _mapping(metadata.get("tpo_result"), code="missing_result")
    try:
        encoded = json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise ResultContractError("malformed_result") from exc
    if len(encoded) > MAX_METADATA_BYTES:
        raise ResultContractError("size_limit", "metadata")
    _reject_unsafe_strings(raw)
    if not _TOP_KEYS <= set(raw) or set(raw) - _TOP_KEYS - _OPTIONAL_TOP_KEYS:
        raise ResultContractError("malformed_result", "unexpected or missing fields")
    if raw["schema_version"] != SCHEMA_VERSION or raw["verdict"] != "success":
        raise ResultContractError("invalid_verdict")
    identities = (raw["tick_id"], raw["todo_id"], raw["step_key"])
    if identities != (tick_id, todo_id, step_key):
        raise ResultContractError("identity_mismatch")
    session = _bounded_string(raw["external_session_id"], maximum=256, code="invalid_session")

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

    tdd = _mapping(raw["tdd"], code="invalid_tdd")
    _exact_keys(tdd, _TDD_KEYS, code="invalid_tdd")
    red = _command(tdd["red"], name="red")
    green = _command(tdd["green"], name="green")
    refactor = _command(tdd["refactor"], name="refactor")
    if red.exit_code == 0 or green.exit_code != 0 or refactor.exit_code != 0:
        raise ResultContractError("invalid_tdd", "red/green/refactor exit codes")

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
    review_evidence = None
    if "review" in raw:
        _validate_review(raw["review"])
        review_raw = raw["review"]
        assert isinstance(review_raw, dict)
        findings = review_raw["findings"]
        assert isinstance(findings, list)
        review_evidence = ReviewEvidence(
            verdict=str(review_raw["verdict"]),
            findings=tuple(dict(item) for item in findings if isinstance(item, dict)),
        )
    delivery_evidence = None
    if "delivery" in raw:
        delivery_evidence = _validate_delivery(raw["delivery"])
    return WorkerResult(
        tick_id, todo_id, step_key, session, git_result, red, green, refactor,
        review_evidence, delivery_evidence,
    )


def verify_read_only_review(
    worktree: Path,
    result: WorkerResult,
    *,
    head_sha: str,
    require_current: bool = True,
) -> None:
    """Verify a review used an unchanged, clean pinned worktree."""
    if result.review is None:
        raise ResultContractError("invalid_review", "missing review evidence")
    if (
        result.git.expected_parent_sha != head_sha
        or result.git.resulting_head_sha != head_sha
        or result.git.task_commit_sha != head_sha
        or result.git.changed_files
    ):
        raise ResultContractError("review_changed_head")
    if not require_current:
        return
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0 or status.stdout:
        raise ResultContractError("review_dirty_worktree")


def _validate_review(value: object) -> None:
    review = _mapping(value, code="invalid_review")
    _exact_keys(review, _REVIEW_KEYS, code="invalid_review")
    verdict = review["verdict"]
    findings = review["findings"]
    if verdict not in set(_REVIEW_VERDICTS) or not isinstance(findings, list):
        raise ResultContractError("invalid_review")
    if len(findings) > MAX_FINDINGS or (verdict == "clean") != (not findings):
        raise ResultContractError("invalid_review")
    for finding in findings:
        item = _mapping(finding, code="invalid_review")
        _exact_keys(item, _FINDING_KEYS, code="invalid_review")
        if item["priority"] not in set(_FINDING_PRIORITIES):
            raise ResultContractError("invalid_review")
        _bounded_string(item["location"], maximum=MAX_LOCATION_LENGTH, code="invalid_review")
        _bounded_string(
            item["failure_scenario"], maximum=MAX_FINDING_TEXT_LENGTH, code="invalid_review"
        )
        _bounded_string(
            item["recommendation"], maximum=MAX_FINDING_TEXT_LENGTH, code="invalid_review"
        )


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


def _template_git(pinned_head_sha: str | None, allow_no_changes: bool) -> dict[str, object]:
    if pinned_head_sha is not None:
        # Read-only cards (review, finish) are pinned: every SHA is already known
        # and any changed file fails verification.
        return _require_keys(
            {
                "expected_parent_sha": pinned_head_sha,
                "resulting_head_sha": pinned_head_sha,
                "task_commit_sha": pinned_head_sha,
                "changed_files": [],
            },
            _GIT_KEYS,
            name="git",
        )
    return _require_keys(
        {
            "expected_parent_sha": "<40-hex SHA of HEAD before your commit>",
            "resulting_head_sha": "<40-hex SHA of HEAD after your commit>",
            "task_commit_sha": "<40-hex SHA of your commit; same as resulting_head_sha>",
            "changed_files": (
                [] if allow_no_changes else ["<repo-relative path your commit changed>"]
            ),
        },
        _GIT_KEYS,
        name="git",
    )


def _template_command(
    description: str, exit_code: int, *, literal: str | None = None
) -> dict[str, object]:
    return _require_keys(
        {
            "command": literal if literal is not None else f"<{description}>",
            "exit_code": exit_code,
        },
        _COMMAND_KEYS,
        name="command",
    )


def _template_review(*, clean: bool) -> dict[str, object]:
    findings = (
        []
        if clean
        else [
            _require_keys(
                {
                    "priority": f"<one of {', '.join(_FINDING_PRIORITIES)}>",
                    "location": "<repo-relative path:line of the reviewed code>",
                    "failure_scenario": "<concrete failure this defect causes>",
                    "recommendation": "<bounded fix recommendation>",
                },
                _FINDING_KEYS,
                name="finding",
            )
        ]
    )
    return _require_keys(
        {"verdict": _REVIEW_VERDICTS[0] if clean else _REVIEW_VERDICTS[1], "findings": findings},
        _REVIEW_KEYS,
        name="review",
    )


def _template_delivery(*, pinned_head_sha: str, branch: str) -> dict[str, object]:
    """Render the delivery section; both identities are registration facts."""
    return _require_keys(
        {
            "pr_url": "<https://github.com/OWNER/REPO/pull/NUMBER>",
            "branch": branch,
            "head_sha": pinned_head_sha,
            "checks": [_template_command("required repository gate you ran", 0)],
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
    pinned_head_sha: str | None = None,
    branch: str | None = None,
    allow_no_changes: bool = False,
) -> str:
    """Render the ``metadata.tpo_result`` template a worker-facing card publishes.

    The template is derived from the same constants ``parse_worker_result``
    enforces, with every pipeline-known value pre-filled, so the worker only
    supplies facts that it alone holds. ``section`` names the optional
    sub-object the card's reconciler requires (``review`` or ``delivery``);
    ``pinned_head_sha`` pre-fills the Git block for a read-only card.
    """
    if section is not None and section not in _OPTIONAL_TOP_KEYS:
        raise ResultContractError("unknown_result_section", str(section))
    if section == "delivery" and (pinned_head_sha is None or branch is None):
        raise ResultContractError("incomplete_result_section", "delivery")
    read_only_command = READ_ONLY_TDD_COMMAND if pinned_head_sha is not None else None
    template: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "tick_id": tick_id,
        "todo_id": todo_id,
        "step_key": step_key,
        "verdict": "success",
        "external_session_id": "<session id of the external client run>",
        "git": _template_git(pinned_head_sha, allow_no_changes),
        # A pinned card is verified read-only, so it has no TDD cycle to report:
        # the contract still requires the block, and a fixed sentinel keeps an
        # honest worker from inventing commands it never ran.
        "tdd": _require_keys(
            {
                "red": _template_command(
                    "exact failing test command",
                    # Non-zero satisfies the contract either way; 127 reads as
                    # "nothing ran" rather than "a test failed".
                    READ_ONLY_TDD_EXIT_CODE if read_only_command else 1,
                    literal=read_only_command,
                ),
                "green": _template_command(
                    "exact command that now passes", 0, literal=read_only_command
                ),
                "refactor": _template_command(
                    "exact command after refactoring", 0, literal=read_only_command
                ),
            },
            _TDD_KEYS,
            name="tdd",
        ),
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
    if section == "review":
        template[section] = _template_review(clean=True)
    elif section == "delivery":
        template[section] = _template_delivery(
            pinned_head_sha=pinned_head_sha, branch=branch
        )
    lines = [
        RESULT_TEMPLATE_HEADING,
        "Report metadata.tpo_result as exactly this object at the end of your "
        "output, replacing every <...> placeholder with the real value; the "
        "Hermes dispatcher that launched you closes the card with it verbatim. "
        "Add no other keys, drop none, and keep the pre-filled values verbatim.",
        "```json",
        json.dumps(template, indent=2, ensure_ascii=False),
        "```",
    ]
    if section == "review":
        # A dispatcher told to publish "exactly this object, never paraphrase"
        # would otherwise close a defect-bearing review as clean, because the
        # main fence carries the clean block.
        lines.append(
            'The "review" value above is the defect-free case. A review that '
            "found defects must not use it: replace the whole \"review\" value "
            f"with the object below (at most {MAX_FINDINGS} findings, each with "
            f"priority one of {', '.join(_FINDING_PRIORITIES)}), keeping the "
            'top-level "verdict" as "success":'
        )
        lines.append("```json")
        lines.append(json.dumps(_template_review(clean=False), indent=2, ensure_ascii=False))
        lines.append("```")
        lines.append(
            "Report a finding only for a defect still present in the code you "
            "reviewed, never one already fixed there: publish that second "
            'object as "review" whenever such a defect remains, and the clean '
            "block only when none does."
        )
    if section == "delivery":
        lines.append(
            "checks lists every required gate you ran, not just one, each with "
            "its exact command and real exit code -- replace the pre-filled 0 "
            "if a gate exited non-zero, and expect the card to be rejected, "
            "because delivery requires every gate to pass."
        )
    if pinned_head_sha is not None:
        lines.append(
            "This card must not change the worktree: leave changed_files empty "
            "and every SHA as shown."
        )
    elif not allow_no_changes:
        lines.append(
            "changed_files lists every repo-relative path in your commit, "
            "deduplicated, and must not be empty."
        )
    if pinned_head_sha is not None:
        lines.append(
            "This card runs no TDD cycle: keep the tdd block exactly as shown."
        )
    else:
        lines.append("red.exit_code must be non-zero and green/refactor must be 0.")
    lines.append(
        'The top-level "verdict" accepts only "success", and it is already filled '
        "in. If you cannot complete the task, do not report a result object at "
        "all: state the exact reason instead, so the dispatcher can block the "
        "card."
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
    if schema_version not in SUPPORTED_REGISTRATION_SCHEMA_VERSIONS:
        raise ResultContractError("registration_invalid", "unsupported schema_version")
    _exact_keys(
        registration,
        _REGISTRATION_V3_KEYS if schema_version == REGISTRATION_SCHEMA_VERSION else _REGISTRATION_KEYS,
        code="registration_invalid",
    )
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
        plan_bytes = _git_bytes(repository, "show", f"{base_sha}:{plan_path}")
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
        # gate was dropped still lists its ``validate:<id>`` keys and must keep
        # validating so it can be resumed.
        required_steps = {f"plan:{task.id}" for task in manifest.tasks}
        if not required_steps <= set(steps):
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
    )


def _git(cwd: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False, timeout=60
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
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise ResultContractError("git_verification_failed") from exc
    if result.returncode not in (0, 1):
        raise ResultContractError("git_verification_failed", args[0])
    return result.returncode == 0


def _git_bytes(cwd: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, check=False, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ResultContractError("registration_invalid") from exc
    if result.returncode != 0:
        raise ResultContractError("registration_invalid")
    return result.stdout


def verify_worker_git_result(
    worktree: Path, git: GitResult, *, expected_parent_sha: str
) -> None:
    """Verify immutable commit topology and changed paths in the pinned worktree."""
    verify_worker_git_topology(worktree, git, expected_parent_sha=expected_parent_sha)
    status = _git_bytes(
        worktree, "status", "--porcelain=v1", "--untracked-files=all", "-z"
    )
    if status:
        raise ResultContractError("worktree_dirty")
    actual_head = _git(worktree, "rev-parse", "HEAD")
    if actual_head != git.resulting_head_sha:
        raise ResultContractError("head_mismatch")


def verify_worker_git_topology(
    worktree: Path, git: GitResult, *, expected_parent_sha: str
) -> None:
    """Verify immutable commit topology and reachability from the branch head.

    Parentage, commit count and changed files all keep holding for a commit that
    ``git reset --hard`` has discarded: the object survives in the repository.
    Reachability from HEAD is the only fact that proves the reported work is on
    the branch this run delivers, so it is checked for every task, whether or
    not the stricter current-HEAD check in ``verify_worker_git_result`` applies.
    """
    if git.expected_parent_sha != expected_parent_sha:
        raise ResultContractError("parent_mismatch")
    if git.resulting_head_sha != git.task_commit_sha:
        raise ResultContractError("head_mismatch")
    parent = _git(worktree, "rev-parse", f"{git.task_commit_sha}^")
    if parent != expected_parent_sha:
        raise ResultContractError("parent_mismatch")
    count = _git(worktree, "rev-list", "--count", f"{expected_parent_sha}..{git.resulting_head_sha}")
    if count != "1":
        raise ResultContractError("commit_count_mismatch")
    changed = tuple(
        sorted(
            line
            for line in _git(
                worktree, "diff", "--name-only", expected_parent_sha, git.resulting_head_sha
            ).splitlines()
            if line
        )
    )
    if changed != tuple(sorted(git.changed_files)):
        raise ResultContractError("changed_files_mismatch")
    if not _git_predicate(
        worktree, "merge-base", "--is-ancestor", git.resulting_head_sha, "HEAD"
    ):
        raise ResultContractError("unreachable_commit")
