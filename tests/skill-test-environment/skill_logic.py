"""Pure-Python implementation of todos-manager skill structural logic.

Serves as both test oracle and golden-file generator.
"""

import fcntl
import json
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path


def load_attachment_policy(skill_root: Path | None = None) -> dict:
    """Parse the executable policy embedded in the authoritative skill Markdown."""
    root = skill_root or files("hermes_pipeline.data").joinpath(
        "skills", "todos-manager"
    )
    source = root / "sections" / "document-attachments.md"
    text = source.read_text(encoding="utf-8")
    match = re.search(
        r"```json todos-manager-attachment-policy\n(.*?)\n```",
        text,
        re.DOTALL,
    )
    if match is None:
        raise RuntimeError("authoritative attachment policy block is missing")
    for relative in ("SKILL.md", "sections/auto-research.md", "sections/revise.md"):
        route = (root / relative).read_text(encoding="utf-8")
        if "sections/document-attachments.md" not in route:
            raise RuntimeError(f"{relative} is disconnected from attachment policy")
    return json.loads(match.group(1))


ATTACHMENT_POLICY = load_attachment_policy()
ATTACHMENT_FIELD_LINE_RE = re.compile(
    r"^  -[ \t]+\*\*(?:Plan|Spec|Reference):\*\*(?:[ \t]+.*)?$"
)
TOP_LEVEL_SECTION_RE = re.compile(r"^##(?:[ \t]+|$)")


@dataclass(frozen=True)
class AttachmentCandidate:
    path: str
    roles: tuple[str, ...]
    relevance_reason: str
    source: str
    validation: str


@dataclass(frozen=True)
class AttachmentSelection:
    plan: str | None = None
    spec: str | None = None
    references: tuple[str, ...] = ()


class AttachmentValidationError(ValueError):
    def __init__(self, message: str, *, defect: str):
        super().__init__(message)
        self.defect = defect


@dataclass(frozen=True)
class AttachmentAuditFinding:
    todo_id: str
    role: str
    stored_path: str
    defect: str


@dataclass(frozen=True)
class AttachmentDiscoveryResult:
    candidates: tuple[AttachmentCandidate, ...]
    reads: int
    searches: int
    exhausted: bool
    skipped_source: str | None
    errors: tuple[str, ...] = ()


def validate_attachment_path(
    repository: Path,
    raw_path: str,
    *,
    reference_input: bool = False,
) -> str:
    """Validate and normalize one attachment candidate.

    A comma is rejected only at the boundary where one Reference candidate is
    supplied. Once stored, commas unconditionally delimit separate paths.
    """
    if reference_input and "," in raw_path:
        raise AttachmentValidationError(
            "Reference path contains a comma.",
            defect="contains a literal comma",
        )
    lexical = Path(raw_path)
    if lexical.is_absolute():
        raise AttachmentValidationError(
            "Attachment path must be repository-relative.",
            defect=ATTACHMENT_POLICY["errors"]["absolute"],
        )

    root = repository.resolve()
    unresolved = root / lexical
    is_symlink = unresolved.is_symlink()
    resolved = unresolved.resolve()
    if not resolved.is_relative_to(root):
        if is_symlink:
            raise AttachmentValidationError(
                "Attachment path is a symlink that resolves outside the repository.",
                defect=ATTACHMENT_POLICY["errors"]["symlink_outside"],
            )
        raise AttachmentValidationError(
            "Attachment path resolves outside the repository.",
            defect=ATTACHMENT_POLICY["errors"]["outside"],
        )
    if not resolved.exists():
        raise AttachmentValidationError(
            "Attachment path does not exist.",
            defect=ATTACHMENT_POLICY["errors"]["missing"],
        )
    if resolved.is_dir():
        raise AttachmentValidationError(
            "Attachment path is a directory, not a regular file.",
            defect=ATTACHMENT_POLICY["errors"]["directory"],
        )
    if not resolved.is_file():
        raise AttachmentValidationError(
            "Attachment path is not a regular file.",
            defect=ATTACHMENT_POLICY["errors"]["not_regular"],
        )
    normalized = resolved.relative_to(root).as_posix()
    if reference_input and "," in normalized:
        raise AttachmentValidationError(
            "Reference path contains a comma.",
            defect="contains a literal comma",
        )
    return normalized


def parse_stored_references(value: str) -> tuple[str, ...]:
    """Parse canonical Reference storage; every comma is a separator."""
    return tuple(part.strip() for part in value.split(ATTACHMENT_POLICY["reference_separator"]))


def audit_attachment_fields(
    repository: Path,
    todo_id: str,
    fields: dict[str, str],
) -> list[AttachmentAuditFinding]:
    """Return attachment defects without mutating the supplied entry fields."""
    findings: list[AttachmentAuditFinding] = []
    for role in ("Plan", "Spec"):
        stored = fields.get(role)
        if not stored:
            continue
        try:
            validate_attachment_path(repository, stored)
        except AttachmentValidationError as exc:
            findings.append(
                AttachmentAuditFinding(todo_id, role, stored, exc.defect)
            )

    reference_value = fields.get("Reference")
    if reference_value:
        for stored in parse_stored_references(reference_value):
            if not stored:
                findings.append(
                    AttachmentAuditFinding(
                        todo_id,
                        "Reference",
                        "",
                        ATTACHMENT_POLICY["errors"]["empty_reference"],
                    )
                )
                continue
            try:
                validate_attachment_path(repository, stored)
            except AttachmentValidationError as exc:
                findings.append(
                    AttachmentAuditFinding(todo_id, "Reference", stored, exc.defect)
                )
    return findings


_MUTATION_VERB_PATTERN = (
    r"(?:add|build|change|configure|create|delete|edit|implement|integrate|"
    r"migrate|modify|move|refactor|remove|rename|replace|update|write)"
)
_MUTATION_WORD_RE = re.compile(rf"\b{_MUTATION_VERB_PATTERN}\b", re.IGNORECASE)
_MUTATION_START_RE = re.compile(rf"^{_MUTATION_VERB_PATTERN}\b", re.IGNORECASE)
_TASK_HEADING_RE = re.compile(
    r"^###[ \t]+Task[ \t]+\d+[ \t]*:[ \t]*(?:\*\*)?(?P<title>.*)$",
    re.IGNORECASE,
)
_TASK_BOUNDARY_RE = re.compile(r"^#{1,3}(?:[ \t]+|$)")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_TARGET_DECLARATION_RE = re.compile(
    r"^[ ]{0,3}(?:[-*][ \t]+)?(?:\*\*)?Targets?"
    r"(?::(?:\*\*)?|\*\*:)[ \t]*(?P<value>.+)$",
    re.IGNORECASE,
)
_MARKDOWN_HEADING_RE = re.compile(
    r"^(?P<marks>#{1,6})[ \t]+(?P<title>.*)$"
)
_MARKDOWN_LIST_ITEM_RE = re.compile(
    r"^(?:(?P<ordered>\d+[.)])|(?P<bullet>[-+*])"
    r"(?:[ \t]+\[(?P<checked>[ xX])\])?)[ \t]+(?P<text>.+)$"
)
_MARKDOWN_STEP_PREFIX_RE = re.compile(
    r"^(?:\*\*)?Step\s+\d+\s*[:.)-]\s*",
    re.IGNORECASE,
)
_VERIFICATION_ACTION_RE = re.compile(
    r"^(?:check|confirm|lint|test|validate|verify)\b",
    re.IGNORECASE,
)
_RUN_VERIFICATION_RE = re.compile(
    r"^(?:execute|run)\b",
    re.IGNORECASE,
)
_VERIFICATION_NOUN_RE = re.compile(
    r"\b(?:build|checks?|lint(?:ing)?|tests?|verification)\b",
    re.IGNORECASE,
)
_COMMAND_INTENT_RE = re.compile(
    r"^(?:build|check|confirm|execute|lint|run|test|validate|verify)\b",
    re.IGNORECASE,
)
_NEGATED_AUXILIARY_PATTERN = (
    r"(?:cannot|[a-z]+n(?:'|\N{RIGHT SINGLE QUOTATION MARK})t|"
    r"(?:am|are|can|could|did|do|does|had|has|have|"
    r"is|may|might|must|need|shall|should|was|were|will|would)[ \t]+not)"
)
_NEGATED_AUXILIARY_RE = re.compile(
    rf"^{_NEGATED_AUXILIARY_PATTERN}\b",
    re.IGNORECASE,
)
_NON_EXECUTION_RE = re.compile(
    r"^(?:do\s+not|don't|never|no|omit|skip|without)\b"
    r"|^(?:execute|run)\s+no\b"
    rf"|\b{_NEGATED_AUXILIARY_PATTERN}(?:[ \t]+[a-z]+){{0,2}}[ \t]+"
    r"(?:be[ \t]+)?(?:execut(?:e|ed|ing)|perform(?:ed|ing)?|"
    r"run|running|required|needed|necessary)\b",
    re.IGNORECASE,
)
_DESCRIPTIVE_VERIFICATION_STATE_RE = re.compile(
    r"^(?:check|lint|test)(?:[ \t]+[^\s`.,:;]+){1,5}[ \t]+"
    r"(?:are|can|could|has|have|is|may|might|must|shall|should|was|were|will|would)\b",
    re.IGNORECASE,
)
_VERIFICATION_NOUN_SUBJECT_RE = re.compile(
    r"^(?:lint(?:ing)?[ \t]+(?:policy|rules?|status)|"
    r"test(?:ing)?[ \t]+(?:coverage|results?|status|suite))\b",
    re.IGNORECASE,
)
_VERIFICATION_SECTION_RE = re.compile(
    r"^(?:acceptance(?:\s+criteria)?|build\s+verification|checks?|lint(?:ing)?|"
    r"tests?(?:\s+commands?)?|verification)(?:\s+steps?)?$",
    re.IGNORECASE,
)
_ACCEPTANCE_TOPIC_FRAGMENT_RE = re.compile(
    r"^(?:acceptance(?:[ \t]+criteria)?|checks?|tests?|testing|"
    r"verification(?:[ \t]+steps?)?)$",
    re.IGNORECASE,
)
_ACCEPTANCE_OUTCOME_RE = re.compile(
    r"\b(?:accepts?|allows?|are|blocks?|bounds?|can|cannot|completes?|"
    r"contains?|creates?|does?|exists?|exits?|fails?|has|have|is|must|"
    r"passes?|rejects?|remains?|returns?|should|stays?|succeeds?|"
    r"will|would)\b"
    r"|\bnever[ \t]+[a-z][a-z0-9_-]*\b"
    r"|(?:<=|>=|==|!=|<|>|\bat[ \t]+(?:least|most)\b|"
    r"\bno[ \t]+more[ \t]+than\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _TaskSection:
    title: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class _MarkdownLine:
    raw: str
    text: str
    list_kind: str | None = None
    heading_level: int | None = None
    heading_title: str | None = None


@dataclass(frozen=True)
class _PlanSignals:
    mutation: bool = False
    target: bool = False
    verification: bool = False


def _fence_opener(line: str) -> tuple[str, int] | None:
    """Parse a complete column-zero Markdown fence opening line."""
    opening = re.fullmatch(
        r"(?P<marker>`{3,}|~{3,})(?P<info>[^\r\n]*)(?:\r?\n)?",
        line,
    )
    if opening is None:
        return None
    marker = opening.group("marker")
    if marker.startswith("`") and "`" in opening.group("info"):
        return None
    return marker[0], len(marker)


def _without_fenced_code(text: str) -> str:
    """Return Markdown text with column-zero fenced examples removed."""
    outside: list[str] = []
    fence_marker: str | None = None
    fence_length = 0
    for line in text.splitlines(keepends=True):
        if fence_marker is None:
            opener = _fence_opener(line)
            if opener is None:
                outside.append(line)
                continue
            fence_marker, fence_length = opener
        elif re.fullmatch(
            rf"{re.escape(fence_marker)}{{{fence_length},}}[ \t]*(?:\r?\n)?",
            line,
        ):
            fence_marker = None
            fence_length = 0
        outside.append("\n" if line.endswith(("\n", "\r\n")) else "")
    return "".join(outside)


def _partition_task_sections(text: str) -> tuple[tuple[_TaskSection, ...], str]:
    """Separate real Task sections from non-Task document content."""
    task_sections: list[_TaskSection] = []
    non_task_lines: list[str] = []
    current_title: str | None = None
    current_lines: list[str] = []

    for line in text.splitlines():
        task_match = _TASK_HEADING_RE.match(line)
        if task_match is not None:
            if current_title is not None:
                task_sections.append(_TaskSection(current_title, tuple(current_lines)))
            current_title = task_match.group("title").rstrip("*").rstrip()
            current_lines = [current_title]
            non_task_lines.append("")
            continue

        if current_title is not None and _TASK_BOUNDARY_RE.match(line):
            task_sections.append(_TaskSection(current_title, tuple(current_lines)))
            current_title = None
            current_lines = []

        if current_title is None:
            non_task_lines.append(line)
        else:
            current_lines.append(line)
            non_task_lines.append("")

    if current_title is not None:
        task_sections.append(_TaskSection(current_title, tuple(current_lines)))

    return tuple(task_sections), "\n".join(non_task_lines)


def _is_verification_command(command: str) -> bool:
    """Return whether an inline command expresses verification intent."""
    tokens = command.strip().split()
    if not tokens:
        return False
    if len(tokens) == 1 and re.search(r"[/\\]|\.[A-Za-z0-9_-]+$", tokens[0]):
        return False
    if len(tokens) == 1 and re.fullmatch(
        r"[A-Z][A-Za-z0-9_]*(?:(?:::|\.)[A-Za-z0-9_]+)*",
        tokens[0],
    ):
        return False

    exact_intents = {
        "build",
        "check",
        "checks",
        "checking",
        "lint",
        "linting",
        "test",
        "tests",
        "testing",
        "verification",
        "verify",
    }
    for token in tokens:
        if "/" in token or "\\" in token:
            continue
        if re.search(r"\.[A-Za-z0-9_-]+$", token):
            continue
        parts = re.split(r"[-_.:=]+", token.strip("'\"`$()[]{}<> ").lower())
        if any(
            part in exact_intents or part.endswith(("test", "check", "lint"))
            for part in parts
            if part
        ):
            return True
    return False


def _is_concrete_implementation_target(value: str) -> bool:
    target = value.strip().strip("`").rstrip(".,;")
    if not target or _is_verification_command(target):
        return False
    return bool(
        re.search(r"[/\\]|\.[A-Za-z0-9_-]+(?:\b|$)", target)
        or re.search(r"\S+[ \t]+\S+", target)
        or re.fullmatch(r"[A-Z][A-Za-z0-9_]*(?:(?:::|\.)[A-Za-z0-9_]+)*", target)
    )


def _markdown_lines(text: str) -> tuple[_MarkdownLine, ...]:
    """Parse non-code Markdown lines into the states used by Plan signals."""
    parsed: list[_MarkdownLine] = []
    for raw_line in text.splitlines():
        if raw_line.startswith("\t") or len(raw_line) - len(raw_line.lstrip(" ")) >= 4:
            continue

        raw = raw_line.lstrip(" ")
        heading = _MARKDOWN_HEADING_RE.match(raw)
        if heading is not None:
            title = heading.group("title").strip()
            parsed.append(
                _MarkdownLine(
                    raw=raw,
                    text=title,
                    heading_level=len(heading.group("marks")),
                    heading_title=title,
                )
            )
            continue

        item = _MARKDOWN_LIST_ITEM_RE.match(raw)
        list_kind: str | None = None
        text_value = raw
        if item is not None:
            text_value = item.group("text")
            if item.group("ordered") is not None:
                list_kind = "ordered"
            elif item.group("checked") is not None:
                list_kind = "checklist"
            else:
                list_kind = "bullet"
        text_value = _MARKDOWN_STEP_PREFIX_RE.sub("", text_value)
        parsed.append(
            _MarkdownLine(
                raw=raw,
                text=text_value.strip("* \t"),
                list_kind=list_kind,
            )
        )
    return tuple(parsed)


def _is_verification_section(title: str) -> bool:
    normalized = _INLINE_CODE_RE.sub("", title).strip("*#: \t")
    return _VERIFICATION_SECTION_RE.fullmatch(normalized) is not None


def _verification_section_kind(line: _MarkdownLine) -> str | None:
    if line.heading_title is None:
        return None
    normalized = _INLINE_CODE_RE.sub("", line.heading_title).strip("*#: \t")
    if line.heading_level == 4 and normalized.casefold() == "acceptance criteria":
        return "acceptance"
    if _is_verification_section(line.heading_title):
        return "verification"
    return None


def _is_affirmative_verification_action(
    line: _MarkdownLine,
    *,
    in_verification_section: bool,
) -> bool:
    action = _INLINE_CODE_RE.sub("", line.text).strip("* \t")
    command_spans = tuple(
        code_span.group(1)
        for code_span in _INLINE_CODE_RE.finditer(line.text)
    )
    has_command = any(_is_verification_command(command) for command in command_spans)

    action_match = _VERIFICATION_ACTION_RE.match(action)
    if action_match is not None:
        remainder = action[action_match.end() :].strip()
        if (
            remainder
            and _NEGATED_AUXILIARY_RE.match(remainder) is None
            and not re.match(r"^(?:never|no|without)\b", remainder, re.IGNORECASE)
            and _VERIFICATION_NOUN_SUBJECT_RE.match(action) is None
        ):
            return True
        if not remainder and has_command:
            return True

    run_match = _RUN_VERIFICATION_RE.match(action)
    if run_match is not None:
        remainder = action[run_match.end() :].strip()
        if re.match(r"^(?:never|no|without)\b", remainder, re.IGNORECASE):
            return False
        if _NEGATED_AUXILIARY_RE.match(remainder) is not None:
            return False
        return has_command or _VERIFICATION_NOUN_RE.search(action) is not None
    if has_command and _COMMAND_INTENT_RE.search(action) is not None:
        return True

    if _NON_EXECUTION_RE.search(action) is not None:
        return False
    if _DESCRIPTIVE_VERIFICATION_STATE_RE.search(action) is not None:
        return False

    command_only = not action.strip(" .:;,-")
    if has_command and command_only and (
        line.list_kind is not None or in_verification_section
    ):
        return True
    return False


def _is_concrete_acceptance_item(line: _MarkdownLine) -> bool:
    if line.list_kind is None:
        return False
    criterion = _INLINE_CODE_RE.sub(" command ", line.text).strip("* \t .:;,-")
    if _NON_EXECUTION_RE.search(criterion) is not None:
        return False
    if _ACCEPTANCE_TOPIC_FRAGMENT_RE.fullmatch(criterion) is not None:
        return False
    return _ACCEPTANCE_OUTCOME_RE.search(criterion) is not None


def _line_has_concrete_implementation_target(
    line: _MarkdownLine,
    *,
    allow_target_declarations: bool,
    allow_action_heading: bool,
    in_verification_section: bool,
) -> bool:
    if in_verification_section or (
        line.heading_title is not None and not allow_action_heading
    ):
        return False

    if allow_target_declarations:
        declaration = _TARGET_DECLARATION_RE.match(line.raw)
        if declaration is not None:
            value = declaration.group("value")
            candidates = _INLINE_CODE_RE.findall(value) or [value]
            return any(_is_concrete_implementation_target(item) for item in candidates)

    if _is_affirmative_verification_action(
        line,
        in_verification_section=False,
    ):
        return False
    for code_span in _INLINE_CODE_RE.finditer(line.text):
        prefix = line.text[: code_span.start()]
        if _MUTATION_WORD_RE.search(prefix) is None:
            continue
        if _is_concrete_implementation_target(code_span.group(1)):
            return True
    return False


def _plan_signals(
    text: str,
    *,
    task_title: str | None = None,
    allow_target_declarations: bool = False,
    allow_acceptance_criteria: bool = False,
) -> _PlanSignals:
    lines = _markdown_lines(text)
    mutation = bool(task_title and _MUTATION_START_RE.match(task_title))
    target = False
    verification = False

    if task_title is not None:
        title_line = _MarkdownLine(raw=task_title, text=task_title)
        target = _line_has_concrete_implementation_target(
            title_line,
            allow_target_declarations=False,
            allow_action_heading=False,
            in_verification_section=False,
        )

    section_kind: str | None = None
    for line in lines:
        action_heading = False
        if line.heading_title is not None:
            next_section_kind = (
                _verification_section_kind(line)
                if allow_target_declarations or allow_acceptance_criteria
                else None
            )
            section_kind = next_section_kind
            action_heading = bool(
                task_title is not None
                and line.heading_level == 4
                and next_section_kind is None
            )
            if not action_heading:
                continue

        in_verification_section = section_kind is not None
        if (
            not in_verification_section
            and (
                line.list_kind in {"checklist", "ordered"}
                or action_heading
            )
            and _MUTATION_START_RE.match(line.text)
        ):
            mutation = True

        if _line_has_concrete_implementation_target(
            line,
            allow_target_declarations=allow_target_declarations,
            allow_action_heading=action_heading,
            in_verification_section=in_verification_section,
        ):
            target = True

        if _TARGET_DECLARATION_RE.match(line.raw) is not None:
            continue
        if _is_affirmative_verification_action(
            line,
            in_verification_section=in_verification_section,
        ):
            verification = True
        elif (
            allow_acceptance_criteria
            and section_kind == "acceptance"
            and _is_concrete_acceptance_item(line)
        ):
            verification = True

    return _PlanSignals(mutation, target, verification)


def _task_is_semantic_plan(task: _TaskSection) -> bool:
    task_body = "\n".join(task.lines[1:])
    signals = _plan_signals(
        task_body,
        task_title=task.title,
        allow_target_declarations=True,
        allow_acceptance_criteria=True,
    )
    return signals.mutation and signals.target and signals.verification


def classify_attachment_document(relative_path: str, text: str) -> tuple[str, ...]:
    """Classify a relevant document by its strongest attachment roles."""
    normalized = relative_path.replace("\\", "/")
    lower_text = text.lower()
    semantic_text = _without_fenced_code(text)
    recognized_plan = (
        normalized.startswith("docs/gstack/")
        and any(marker in lower_text for marker in ("status: approved", "verdict:"))
    ) or normalized.startswith("docs/superpowers/plans/")
    task_sections, non_task_text = _partition_task_sections(semantic_text)
    task_plan = any(_task_is_semantic_plan(task) for task in task_sections)
    fallback_signals = _plan_signals(non_task_text)
    fallback_plan = (
        fallback_signals.mutation
        and fallback_signals.target
        and fallback_signals.verification
    )
    plan = recognized_plan or task_plan or fallback_plan
    spec = bool(
        re.search(r"(?im)^#{2,4}\s+Outcome\b", text)
        and re.search(
            r"(?im)^#{2,4}\s+(?:Acceptance(?: criteria)?|Requirements?|"
            r"Compatibility and completion|[^\n#]*\bContract)\b",
            text,
        )
    )
    roles: list[str] = []
    if plan:
        roles.append("Plan")
    if spec:
        roles.append("Spec")
    if not roles:
        roles.append("Reference")
    return tuple(roles)


def _attachment_path_is_excluded(relative_path: str) -> bool:
    parts = Path(relative_path).parts
    excluded_parts = set(ATTACHMENT_POLICY["excluded_parts"])
    return any(part.lower() in excluded_parts for part in parts)


def discover_attachment_candidates(
    repository: Path,
    *,
    explicit_paths: tuple[str, ...] = (),
    git_paths: tuple[str, ...] = (),
    search_paths: tuple[str, ...] = (),
    search_batches: tuple[tuple[str, ...], ...] = (),
    todo_id: str | None = None,
    subject_terms: tuple[str, ...] = (),
    target_paths: tuple[str, ...] = (),
    title_summary: str = "",
    reads_used: int = 0,
    searches_used: int = 0,
    read_limit: int = ATTACHMENT_POLICY["read_limit"],
    search_limit: int = ATTACHMENT_POLICY["search_limit"],
    candidate_limit: int = ATTACHMENT_POLICY["candidate_limit"],
) -> AttachmentDiscoveryResult:
    """Execute bounded discovery in explicit, Git, then search precedence."""
    candidates: list[AttachmentCandidate] = []
    errors: list[str] = []
    seen: set[str] = set()
    reads = reads_used
    searches = searches_used
    exhausted = False
    skipped_source: str | None = None
    if search_paths:
        search_batches = (search_paths, *search_batches)
    source_names = ATTACHMENT_POLICY["sources"]
    sources = ((source_names[0], (explicit_paths,)), (source_names[1], (git_paths,)), (source_names[2], search_batches))
    lowered_targets = tuple(path.lower() for path in target_paths)
    close_scope_policy = ATTACHMENT_POLICY["close_scope"]
    generic_terms = set(close_scope_policy["generic_terms"])
    scope_terms = {
        term.lower()
        for term in re.findall(r"[A-Za-z0-9]{4,}", title_summary)
        if term.lower() not in generic_terms
    }
    canonical_todo_id = (
        re.fullmatch(r"TODO-[1-9]\d*", todo_id, re.IGNORECASE)
        if todo_id is not None
        else None
    )
    todo_id_pattern = (
        re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(canonical_todo_id.group(0))}"
            r"(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        if canonical_todo_id is not None
        else None
    )
    for source, batches in sources:
        for batch in batches:
            if source == source_names[2]:
                if searches >= search_limit:
                    exhausted = True
                    skipped_source = source
                    break
                searches += 1
            for raw_path in batch:
                if len(candidates) >= candidate_limit:
                    skipped_source = source
                    break
                if _attachment_path_is_excluded(raw_path):
                    continue
                if reads >= read_limit:
                    exhausted = True
                    skipped_source = source
                    break
                try:
                    normalized = validate_attachment_path(repository, raw_path)
                except AttachmentValidationError as exc:
                    errors.append(f"{raw_path}: {exc}")
                    continue
                reads += 1
                if normalized in seen:
                    continue
                text = (repository.resolve() / normalized).read_text(encoding="utf-8")
                haystack = f"{normalized}\n{text}".lower()
                document_terms = set(re.findall(r"[a-z0-9]{4,}", haystack))
                close_scope = (
                    len(scope_terms & document_terms)
                    >= close_scope_policy["minimum_specific_term_overlap"]
                )
                relevant = (
                    ("explicit" in ATTACHMENT_POLICY["relevance"] and source == source_names[0])
                    or (
                        "todo-id" in ATTACHMENT_POLICY["relevance"]
                        and todo_id_pattern is not None
                        and todo_id_pattern.search(haystack) is not None
                    )
                    or ("close-scope" in ATTACHMENT_POLICY["relevance"] and close_scope)
                    or ("concrete-target-overlap" in ATTACHMENT_POLICY["relevance"] and any(target in haystack for target in lowered_targets))
                )
                if not relevant:
                    continue
                roles = classify_attachment_document(normalized, text)
                if "Reference" in roles:
                    try:
                        validate_attachment_path(
                            repository,
                            raw_path,
                            reference_input=True,
                        )
                    except AttachmentValidationError as exc:
                        errors.append(f"{raw_path}: {exc}")
                        continue
                reason = "explicit task context" if source == source_names[0] else "strong relevance"
                candidates.append(AttachmentCandidate(normalized, roles, reason, source, "valid"))
                seen.add(normalized)
            if len(candidates) >= candidate_limit:
                skipped_source = source
                break
        if skipped_source is not None:
            break

    return AttachmentDiscoveryResult(
        candidates=tuple(candidates),
        reads=reads,
        searches=searches,
        exhausted=exhausted,
        skipped_source=skipped_source,
        errors=tuple(errors),
    )


class AttachmentWorkflow:
    """Deterministic interaction model for add/revise attachment gates."""

    def __init__(
        self,
        repository: Path,
        *,
        command: str,
        candidates: tuple[AttachmentCandidate, ...] = (),
        existing: AttachmentSelection = AttachmentSelection(),
        todos_path: Path | None = None,
        todo_id: str | None = None,
    ):
        if command not in {"add", "revise"}:
            raise ValueError("command must be add or revise")
        self.repository = repository
        self.command = command
        self.candidates = candidates
        self._plan = existing.plan
        self._spec = existing.spec
        self._references = list(existing.references)
        self._existing = existing
        self.todos_path = todos_path
        self.todo_id = todo_id
        self._references_changed = False
        self._none_roles: set[str] = set()
        self._combined_selected = False
        self._confirmed = False
        self.discovery_runs = 1
        existing_fields = {
            role: value
            for role, value in (
                ("Plan", existing.plan),
                ("Spec", existing.spec),
                ("Reference", ", ".join(existing.references)),
            )
            if value
        }
        self.warnings = audit_attachment_fields(
            repository,
            "existing TODO",
            existing_fields,
        )

    @property
    def selection(self) -> AttachmentSelection:
        return AttachmentSelection(
            plan=self._plan,
            spec=self._spec,
            references=tuple(self._references),
        )

    def _role_candidates(self, role: str) -> list[AttachmentCandidate]:
        return [candidate for candidate in self.candidates if role in candidate.roles]

    def role_state(self, role: str) -> str:
        current = self._plan if role == "Plan" else self._spec if role == "Spec" else None
        existing = (
            self._existing.plan
            if role == "Plan"
            else self._existing.spec
            if role == "Spec"
            else self._existing.references
        )
        if existing and (role != "Reference" and current == existing):
            return "preserved"
        if role == "Reference" and existing and not self._references_changed:
            return "preserved"
        if role == "Reference" and self._references_changed:
            return "selected"
        if role in self._none_roles:
            return "none detected"
        if current or (role == "Reference" and self._references):
            return "selected"
        count = len(self._role_candidates(role))
        if count == 0:
            return "none detected"
        if count == 1:
            return "suggested"
        return "unresolved"

    def _changed(self) -> None:
        self._confirmed = False

    @staticmethod
    def _confirmation_action(candidate_count: int) -> str:
        cardinality = (
            "zero" if candidate_count == 0 else "one" if candidate_count == 1 else "multiple"
        )
        action = ATTACHMENT_POLICY["confirmation"][cardinality]
        if action not in {"none", "explicit-selection"}:
            raise RuntimeError(
                f"unsupported attachment confirmation action: {action}"
            )
        return action

    def select_candidate(self, role: str, number: int) -> None:
        candidates = self._role_candidates(role)
        if number < 1 or number > len(candidates):
            raise ValueError(f"invalid {role} candidate number")
        path = candidates[number - 1].path
        if role in {"Plan", "Spec"} and path in self._references:
            raise ValueError(f"{role} path is already present in Reference")
        if role == "Plan":
            self._plan = path
        elif role == "Spec":
            self._spec = path
        else:
            self.append_reference(path)
            return
        self._none_roles.discard(role)
        self._changed()

    def select_manual(self, role: str, raw_path: str) -> None:
        if role == "Reference":
            self.append_reference(raw_path)
            return
        normalized = validate_attachment_path(self.repository, raw_path)
        if role in {"Plan", "Spec"} and normalized in self._references:
            raise ValueError(f"{role} path is already present in Reference")
        if role == "Plan":
            self._plan = normalized
        elif role == "Spec":
            self._spec = normalized
        else:
            raise ValueError(f"unknown attachment role: {role}")
        self._none_roles.discard(role)
        self._changed()

    def choose_none(self, role: str) -> None:
        if role == "Plan":
            self._plan = None
        elif role == "Spec":
            self._spec = None
        else:
            self._references = []
            self._references_changed = self._references != list(
                self._existing.references
            )
        self._none_roles.add(role)
        self._changed()

    def attach_combined(self, number: int) -> None:
        combined = [
            candidate
            for candidate in self.candidates
            if {"Plan", "Spec"}.issubset(candidate.roles)
        ]
        if number < 1 or number > len(combined):
            raise ValueError("invalid combined candidate number")
        path = combined[number - 1].path
        if path in self._references:
            raise ValueError("Plan path is already present in Reference")
        self._plan = path
        self._spec = path
        self._combined_selected = True
        self._changed()

    def replace(self, role: str, raw_path: str) -> None:
        if self.command != "revise" or role not in {"Plan", "Spec"}:
            raise ValueError("replace applies only to Plan or Spec during revise")
        self.select_manual(role, raw_path)

    def remove(self, role: str) -> None:
        if self.command != "revise" or role not in {"Plan", "Spec"}:
            raise ValueError("remove applies only to Plan or Spec during revise")
        if role == "Plan":
            self._plan = None
        else:
            self._spec = None
        self._none_roles.add(role)
        self._changed()

    def append_reference(self, raw_path: str) -> None:
        normalized = validate_attachment_path(
            self.repository,
            raw_path,
            reference_input=True,
        )
        if normalized in {self._plan, self._spec}:
            raise ValueError("Reference path matches Plan or Spec")
        if normalized not in self._references:
            self._references.append(normalized)
            self._references_changed = True
        self._changed()

    def remove_reference(self, stored_path: str) -> None:
        self._references = [
            reference for reference in self._references if reference != stored_path
        ]
        self._references_changed = self._references != list(self._existing.references)
        self._changed()

    def confirm(self) -> AttachmentSelection:
        combined = [
            candidate
            for candidate in self.candidates
            if {"Plan", "Spec"}.issubset(candidate.roles)
        ]
        if (
            combined
            and not self._combined_selected
            and self._plan is None
            and self._spec is None
            and self._confirmation_action(len(combined)) == "explicit-selection"
        ):
            raise ValueError("combined Plan and Spec choice requires explicit selection")

        for role in ATTACHMENT_POLICY["fields"]:
            selected = (
                self._plan is not None
                if role == "Plan"
                else self._spec is not None
                if role == "Spec"
                else bool(self._references)
            )
            if selected or role in self._none_roles:
                continue
            candidate_count = len(self._role_candidates(role))
            if self._confirmation_action(candidate_count) != "explicit-selection":
                continue
            if candidate_count > 1:
                raise ValueError(f"{role} is unresolved")
            raise ValueError(f"{role} requires explicit selection")
        self._confirmed = True
        return self.selection

    def finish(
        self,
        *,
        approved: bool,
        writer: Callable[[AttachmentSelection], None] | None = None,
    ) -> bool:
        if not self._confirmed:
            raise RuntimeError("attachment confirmation is required before preview approval")
        if not approved:
            return False
        if writer is not None:
            writer(self.selection)
        elif self.todos_path is not None and self.todo_id is not None:
            apply_attachment_selection_to_todo(
                self.todos_path, self.todo_id, self.selection, approved=True
            )
        else:
            raise RuntimeError("writer or TODO target is required")
        return True


def apply_attachment_selection_to_todo(
    todos_path: Path,
    todo_id: str,
    selection: AttachmentSelection,
    *,
    approved: bool,
) -> bool:
    """Apply confirmed attachments to one real TODO Markdown entry."""
    if not approved:
        return False

    def transform(current: str) -> str:
        wanted = int(todo_id.removeprefix("TODO-"))
        document_lines = current.splitlines(keepends=True)
        entries_spans = [
            (start, end)
            for heading, start, end in _section_spans(document_lines)
            if heading == "## Entries"
        ]
        if len(entries_spans) != 1:
            raise ValueError("TODOS.md must contain exactly one ## Entries section")
        entries_start, entries_end = entries_spans[0]
        matching_start = next(
            (
                index
                for index in range(entries_start, entries_end)
                if (
                    match := ENTRY_HEADER_RE.match(
                        _line_without_ending(document_lines[index])
                    )
                )
                and int(match.group(2)) == wanted
            ),
            None,
        )
        if matching_start is None:
            raise ValueError(f"{todo_id} not found")
        matching_end = next(
            (
                index
                for index in range(matching_start + 1, entries_end)
                if ENTRY_HEADER_RE.match(_line_without_ending(document_lines[index]))
                or TOP_LEVEL_SECTION_RE.match(
                    _line_without_ending(document_lines[index])
                )
            ),
            entries_end,
        )

        entry_lines = document_lines[matching_start:matching_end]
        trailing_blank_lines: list[str] = []
        while entry_lines and not _line_without_ending(entry_lines[-1]).strip():
            trailing_blank_lines.insert(0, entry_lines.pop())

        retained_lines = [
            line
            for line in entry_lines
            if ATTACHMENT_FIELD_LINE_RE.fullmatch(_line_without_ending(line)) is None
        ]
        attachment_lines = []
        if selection.plan:
            attachment_lines.append(f"  - **Plan:** {selection.plan}")
        if selection.spec:
            attachment_lines.append(f"  - **Spec:** {selection.spec}")
        if selection.references:
            attachment_lines.append(
                f"  - **Reference:** {', '.join(selection.references)}"
            )

        newline = _detect_newline(current)
        if attachment_lines and retained_lines and not retained_lines[-1].endswith(
            ("\n", "\r\n")
        ):
            retained_lines[-1] += newline
        needs_terminal_newline = (
            bool(trailing_blank_lines)
            or matching_end < entries_end
            or current.endswith(("\n", "\r\n"))
        )
        rendered_attachments = [
            line
            + (
                newline
                if index < len(attachment_lines) - 1 or needs_terminal_newline
                else ""
            )
            for index, line in enumerate(attachment_lines)
        ]
        replacement = retained_lines + rendered_attachments + trailing_blank_lines
        return "".join(
            document_lines[:matching_start]
            + replacement
            + document_lines[matching_end:]
        )

    atomic_update_todos(todos_path, transform)
    return True


def audit_todo_markdown(repository: Path, todos_path: Path) -> list[AttachmentAuditFinding]:
    """Parse and audit attachments from real TODO Markdown without writing."""
    findings: list[AttachmentAuditFinding] = []
    text = todos_path.read_text(encoding="utf-8")
    for entry in parse_entries(text):
        findings.extend(
            audit_attachment_fields(repository, f"TODO-{entry['id']}", entry["fields"])
        )
    return findings


def scan_ids(text: str) -> set[int]:
    """Return all TODO-N IDs found in markdown text."""
    return {int(m) for m in re.findall(r"TODO-(\d+)", text)}


def compute_next_id(todos_path: Path, archive_path: Path) -> int:
    """Compute next sequential ID from TODOS.md and TODOS-archive.md."""
    all_ids: set[int] = set()
    if todos_path.exists():
        all_ids |= _scan_document_ids(todos_path.read_text(encoding="utf-8"))
    if archive_path.exists():
        all_ids |= _scan_document_ids(archive_path.read_text(encoding="utf-8"))
    if not all_ids:
        return 1
    return max(all_ids) + 1


NEXT_TODO_ID_LINE_RE = re.compile(
    r"^(?:>[ \t]+-[ \t]+)?NEXT_TODO_ID:(?P<value>[^\r\n]*)$"
)

SECTION_HEADINGS = ("## Metadata", "## Entry Schema", "## Entries")


@dataclass(frozen=True)
class TodoDocumentSections:
    metadata: str
    schema: str
    entries: str
    diagnostics: list[str]
    newline: str
    has_canonical_layout: bool


def _preamble_line_indexes(lines: list[str]) -> range:
    """Return the contiguous blockquote preamble immediately after ``# TODOS``."""
    heading = next(
        (index for index, line in enumerate(lines) if line.rstrip("\r\n") == "# TODOS"),
        None,
    )
    if heading is None:
        return range(0)

    start = heading + 1
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start < len(lines) and NEXT_TODO_ID_LINE_RE.fullmatch(lines[start].rstrip("\r\n")):
        start += 1
        while start < len(lines) and not lines[start].strip():
            start += 1
    if start == len(lines) or not lines[start].startswith(">"):
        return range(0)

    end = start
    while end < len(lines):
        line = lines[end]
        if line.startswith(">") or not line.strip():
            end += 1
            continue
        break
    return range(start, end)


def _detect_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _line_without_ending(line: str) -> str:
    return line.rstrip("\r\n")


def _metadata_line_indexes(lines: list[str]) -> list[int]:
    return [
        index
        for index, line in enumerate(lines)
        if NEXT_TODO_ID_LINE_RE.fullmatch(_line_without_ending(line))
    ]


def _section_spans(lines: list[str]) -> list[tuple[str, int, int]]:
    headings: list[tuple[str, int]] = []
    for index, line in enumerate(lines):
        stripped = _line_without_ending(line)
        if stripped in SECTION_HEADINGS:
            headings.append((stripped, index))
    spans: list[tuple[str, int, int]] = []
    for position, (heading, index) in enumerate(headings):
        end = headings[position + 1][1] if position + 1 < len(headings) else len(lines)
        spans.append((heading, index + 1, end))
    return spans


def parse_todos_document_sections(text: str) -> TodoDocumentSections:
    lines = text.splitlines(keepends=True)
    newline = _detect_newline(text)
    spans = _section_spans(lines)
    diagnostics: list[str] = []
    heading_positions = [
        _line_without_ending(line)
        for line in lines
        if _line_without_ending(line) in SECTION_HEADINGS
    ]
    has_canonical_layout = heading_positions == list(SECTION_HEADINGS)
    if not has_canonical_layout:
        diagnostics.append("TODOS.md must contain ## Metadata, ## Entry Schema, and ## Entries in that order")

    def section_text(heading: str) -> str:
        chunks: list[list[str]] = []
        for span_heading, start, end in spans:
            if span_heading != heading:
                continue
            chunk = lines[start:end]
            while chunk and not chunk[0].strip():
                chunk = chunk[1:]
            while chunk and not chunk[-1].strip():
                chunk = chunk[:-1]
            if chunk:
                chunks.append(chunk)
        if not chunks:
            return ""
        return newline.join("".join(chunk) for chunk in chunks)

    metadata_indexes = _metadata_line_indexes(lines)
    misplaced_indexes = []
    metadata_spans = [
        (start, end) for heading, start, end in spans if heading == "## Metadata"
    ]
    if metadata_spans:
        misplaced_indexes = [
            index
            for index in metadata_indexes
            if not any(start <= index < end for start, end in metadata_spans)
        ]
    else:
        misplaced_indexes = metadata_indexes
    if misplaced_indexes:
        diagnostics.append("NEXT_TODO_ID appears outside ## Metadata")
    if len(metadata_indexes) > 1:
        diagnostics.append("NEXT_TODO_ID is duplicated")

    return TodoDocumentSections(
        metadata=section_text("## Metadata"),
        schema=section_text("## Entry Schema"),
        entries=section_text("## Entries"),
        diagnostics=diagnostics,
        newline=newline,
        has_canonical_layout=has_canonical_layout,
    )


def _scan_document_ids(text: str) -> set[int]:
    """Scan sectioned documents only within ## Entries, otherwise scan legacy text."""
    sections = parse_todos_document_sections(text)
    has_section_headings = bool(_section_spans(text.splitlines(keepends=True)))
    return scan_ids(sections.entries if has_section_headings else text)


def _repair_embedded_metadata(lines: list[str]) -> None:
    marker = "> **Format rules (enforced by `todos-manager` skill):**"
    for index in _preamble_line_indexes(lines):
        line = lines[index]
        stripped = line.rstrip("\r\n")
        if stripped.startswith("> **Format rules") and "NEXT_TODO_ID" in stripped:
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            lines[index] = marker + ending


def read_next_todo_id(text: str) -> tuple[int | None, list[str]]:
    """Read and validate the tracked next TODO ID from the metadata section."""
    lines = text.splitlines(keepends=True)
    sections = parse_todos_document_sections(text)
    metadata_indexes = _metadata_line_indexes(lines)
    issues = list(sections.diagnostics)
    if not metadata_indexes:
        issues.append("NEXT_TODO_ID is missing from ## Metadata")
        return None, issues
    if len(metadata_indexes) > 1:
        return None, issues
    metadata_lines = sections.metadata.splitlines()
    metadata_matches = [
        line
        for line in metadata_lines
        if NEXT_TODO_ID_LINE_RE.fullmatch(line.rstrip("\r\n"))
    ]
    if len(metadata_matches) != 1 or issues:
        return None, issues
    match = NEXT_TODO_ID_LINE_RE.fullmatch(metadata_matches[0].rstrip("\r\n"))
    assert match is not None
    raw = match.group("value").strip(" \t")
    if not re.fullmatch(r"[1-9][0-9]*", raw):
        return None, [f"NEXT_TODO_ID must be a positive base-10 integer, got {raw!r}"]
    return int(raw), []


def compute_scan_next_id(todos_path: Path, archive_path: Path) -> int:
    """Return the scan-derived next TODO ID."""
    return compute_next_id(todos_path, archive_path)


def convert_header_based_todos(text: str, archive_text: str = "") -> tuple[str, str]:
    """Convert a Mode B header-based document into canonical TODO sections."""
    section_re = re.compile(r"^##\s+(.+?)\s*$")
    title_re = re.compile(r"^###\s+(.+?)\s*$")
    header_field_re = re.compile(r"^\s*(?:-\s+)?\*\*([^*]+?):\*\*\s*(.*?)\s*$")
    parsed: list[dict] = []
    current_section = ""
    current: dict | None = None
    canonical_blocks = extract_entry_blocks(text)

    def finish_current() -> None:
        nonlocal current
        if current is not None:
            parsed.append(current)
            current = None

    for line in text.splitlines():
        section_match = section_re.match(line)
        if section_match:
            finish_current()
            current_section = section_match.group(1)
            continue
        title_match = title_re.match(line)
        if title_match:
            finish_current()
            current = {
                "title": title_match.group(1),
                "section": current_section,
                "fields": [],
                "raw": [line],
            }
            continue
        if ENTRY_HEADER_RE.match(line):
            finish_current()
            continue
        if current is None:
            continue
        current["raw"].append(line)
        field_match = header_field_re.match(line)
        if field_match:
            current["fields"].append((field_match.group(1), field_match.group(2)))
    finish_current()
    if not parsed:
        return text, ""

    existing_ids = scan_ids(text) | scan_ids(archive_text)
    next_id = max(existing_ids, default=0) + 1
    converted_blocks: list[str] = []
    reference_blocks: list[str] = []
    for entry in parsed:
        fields = dict(entry["fields"])
        if not fields.get("What") or not fields.get("Why"):
            reference_blocks.append("\n".join(entry["raw"]).rstrip())
            continue

        raw_title = entry["title"]
        completed_suffix = raw_title.endswith(" — Completed")
        title = raw_title.removesuffix(" — Completed")
        section_lower = entry["section"].lower()
        if fields.get("Completed") or completed_suffix or section_lower == "completed":
            status = "[x]"
        elif section_lower == "open":
            status = "[ ]"
        elif any(term in section_lower for term in ("wip", "blocked", "in progress")):
            status = "[→]"
        elif any(term in section_lower for term in ("hold", "deferred", "parking")):
            status = "[~]"
        else:
            status = "[ ]"

        what = fields["What"]
        summary = what.split(". ", 1)[0]
        if ". " in what:
            summary += "."
        block = [f"- {status} **TODO-{next_id}: {title}** — {summary}"]
        has_decisions = False
        for field_name, value in entry["fields"]:
            renamed = {
                "Resolution": "Resolved design",
                "Depends on / blocked by": "Depends on",
            }.get(field_name, field_name)
            if renamed == "Decisions":
                has_decisions = True
            block.append(f"  - **{renamed}:** {value}")
        if not has_decisions:
            block.append(
                "  - **Decisions:** <<USER-REVIEW>> Priority, Effort, Phase, "
                "Branch not yet determined"
            )
        converted_blocks.append("\n".join(block))
        next_id += 1

    schema = (
        "> **Format rules (enforced by `todos-manager` skill):**\n"
        "> - Entry header: `- [ ] **TODO-<n>: <Title>** — <Summary>`\n"
        "> - Required fields: **What:**, **Why:**, **Decisions:**"
    )
    converted = (
        "# TODOS\n\n"
        "## Metadata\n\n"
        f"NEXT_TODO_ID: {next_id}\n\n"
        "## Entry Schema\n\n"
        f"{schema}\n\n"
        "## Entries\n"
    )
    all_entry_blocks = converted_blocks + canonical_blocks
    if all_entry_blocks:
        converted += "\n" + "\n\n".join(all_entry_blocks) + "\n"

    reference = ""
    if reference_blocks:
        reference = (
            "# TODOS Reference\n\n"
            "Entries that could not be auto-converted (missing required fields).\n\n"
            + "\n\n".join(reference_blocks)
            + "\n"
        )
    return converted, reference


def _sectioned_schema_from_legacy(lines: list[str]) -> list[str]:
    preamble_indexes = set(_preamble_line_indexes(lines))
    return [
        line
        for index, line in enumerate(lines)
        if index in preamble_indexes
        and not NEXT_TODO_ID_LINE_RE.fullmatch(_line_without_ending(line))
    ]


def _sectioned_entries_from_legacy(lines: list[str]) -> list[str]:
    preamble_indexes = set(_preamble_line_indexes(lines))
    entry_lines: list[str] = []
    in_entries = False
    for index, line in enumerate(lines):
        stripped = _line_without_ending(line)
        if stripped == "# TODOS" or index in preamble_indexes:
            continue
        if NEXT_TODO_ID_LINE_RE.fullmatch(stripped):
            continue
        if ENTRY_HEADER_RE.match(stripped):
            in_entries = True
        if in_entries:
            entry_lines.append(line)
    while entry_lines and not entry_lines[0].strip():
        entry_lines = entry_lines[1:]
    while entry_lines and not entry_lines[-1].strip():
        entry_lines = entry_lines[:-1]
    return entry_lines


def replace_next_todo_id_line(text: str, next_id: int) -> str:
    """Replace tracked metadata and normalize the document to three sections."""
    newline = _detect_newline(text)
    lines = text.splitlines(keepends=True)
    _repair_embedded_metadata(lines)
    sections = parse_todos_document_sections("".join(lines))
    spans = _section_spans(lines)
    if sections.has_canonical_layout or spans:
        schema_lines = sections.schema.splitlines(keepends=True)
        entries_lines = sections.entries.splitlines(keepends=True)
        first_section_index = min(
            index
            for index, line in enumerate(lines)
            if _line_without_ending(line) in SECTION_HEADINGS
        )
        prefix_lines = [
            line
            for line in lines[:first_section_index]
            if _line_without_ending(line) != "# TODOS"
            and not NEXT_TODO_ID_LINE_RE.fullmatch(_line_without_ending(line))
        ]
        metadata_lines = sections.metadata.splitlines(keepends=True)
        schema_lines = prefix_lines + metadata_lines + schema_lines
    else:
        schema_lines = _sectioned_schema_from_legacy(lines)
        entries_lines = _sectioned_entries_from_legacy(lines)

    def clean(lines_to_clean: list[str]) -> list[str]:
        return [
            line
            for line in lines_to_clean
            if not NEXT_TODO_ID_LINE_RE.fullmatch(_line_without_ending(line))
        ]

    schema_lines = clean(schema_lines)
    entries_lines = clean(entries_lines)
    output: list[str] = [
        "# TODOS" + newline,
        newline,
        "## Metadata" + newline,
        newline,
        f"NEXT_TODO_ID: {next_id}" + newline,
        newline,
        "## Entry Schema" + newline,
        newline,
    ]
    output.extend(schema_lines)
    if output[-1].strip():
        output.append(newline)
    output.extend([newline, "## Entries" + newline])
    if entries_lines:
        output.append(newline)
        output.extend(entries_lines)
        if not output[-1].endswith(("\n", "\r\n")):
            output.append(newline)
    return "".join(output)


@contextmanager
def _todo_lock(lock_path: Path) -> Iterator[None]:
    """Hold the sidecar lock used to serialize TODO updates."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _before_todo_lock() -> None:
    """Test synchronization point before an update attempts the sidecar lock."""


def _before_todos_replace() -> None:
    """Test synchronization point before committing an updated TODO file."""


def _after_todo_transaction_replace(_target: Path) -> None:
    """Test synchronization point after replacing one transaction target."""


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _replace_with_text(target: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", text=True)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, target)
        _fsync_dir(target.parent)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _transaction_dir(project_dir: Path) -> Path:
    return project_dir / ".hermes"


def _archive_journals(project_dir: Path) -> list[Path]:
    hermes_dir = _transaction_dir(project_dir)
    if not hermes_dir.exists():
        return []
    return sorted(hermes_dir.glob("todo-archive-*.json"))


def _apply_payload(payload: Path, target: Path) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as tmp:
            with payload.open("rb") as source:
                shutil.copyfileobj(source, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, target)
        _fsync_dir(target.parent)
        _after_todo_transaction_replace(target)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _read_archive_journal(journal_path: Path) -> dict:
    try:
        data = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Malformed TODO archive transaction journal: {journal_path}") from exc
    required = {"todos_target", "archive_target", "todos_payload", "archive_payload"}
    if not isinstance(data, dict) or set(data) < required:
        raise RuntimeError(f"Malformed TODO archive transaction journal: {journal_path}")
    return data


def _resolve_journal_path(journal_path: Path, raw_path: object) -> Path:
    if not isinstance(raw_path, str):
        raise RuntimeError(f"Malformed TODO archive transaction journal: {journal_path}")
    return Path(raw_path).resolve()


def recover_pending_todo_transaction(project_dir: Path) -> None:
    """Complete any pending TODO archive transaction under the caller's lock."""
    project_dir = project_dir.resolve()
    hermes_dir = _transaction_dir(project_dir).resolve()
    expected_todos_target = (project_dir / "TODOS.md").resolve()
    expected_archive_target = (project_dir / "TODOS-archive.md").resolve()
    for journal_path in _archive_journals(project_dir):
        data = _read_archive_journal(journal_path)
        archive_payload = _resolve_journal_path(journal_path, data["archive_payload"])
        todos_payload = _resolve_journal_path(journal_path, data["todos_payload"])
        archive_target = _resolve_journal_path(journal_path, data["archive_target"])
        todos_target = _resolve_journal_path(journal_path, data["todos_target"])
        if archive_target != expected_archive_target or todos_target != expected_todos_target:
            raise RuntimeError(f"Malformed TODO archive transaction journal: {journal_path}")
        for payload in (archive_payload, todos_payload):
            if payload.parent != hermes_dir or not payload.name.startswith("todo-archive-"):
                raise RuntimeError(f"Malformed TODO archive transaction journal: {journal_path}")
        if not archive_payload.exists() or not todos_payload.exists():
            raise RuntimeError(f"Incomplete TODO archive transaction payloads: {journal_path}")
        _apply_payload(archive_payload, archive_target)
        _apply_payload(todos_payload, todos_target)
        for path in (journal_path, archive_payload, todos_payload):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        _fsync_dir(journal_path.parent)


def _write_archive_transaction(
    project_dir: Path, todos_path: Path, archive_path: Path, todos_text: str, archive_text: str
) -> None:
    transaction_id = uuid.uuid4().hex
    hermes_dir = _transaction_dir(project_dir)
    hermes_dir.mkdir(parents=True, exist_ok=True)
    todos_payload = hermes_dir / f"todo-archive-{transaction_id}.todos.payload"
    archive_payload = hermes_dir / f"todo-archive-{transaction_id}.archive.payload"
    journal_path = hermes_dir / f"todo-archive-{transaction_id}.json"
    _replace_with_text(todos_payload, todos_text)
    _replace_with_text(archive_payload, archive_text)
    journal = {
        "todos_target": str(todos_path),
        "archive_target": str(archive_path),
        "todos_payload": str(todos_payload),
        "archive_payload": str(archive_payload),
    }
    _replace_with_text(journal_path, json.dumps(journal, sort_keys=True) + "\n")
    _fsync_dir(hermes_dir)
    _apply_payload(archive_payload, archive_path)
    _apply_payload(todos_payload, todos_path)
    for path in (journal_path, archive_payload, todos_payload):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    _fsync_dir(hermes_dir)


def atomic_update_todos(todos_path: Path, transform: Callable[[str], str]) -> None:
    """Apply a TODO transform under an exclusive lock using atomic replacement."""
    lock_path = todos_path.with_suffix(todos_path.suffix + ".lock")
    _before_todo_lock()
    with _todo_lock(lock_path):
        recover_pending_todo_transaction(todos_path.parent)
        with todos_path.open("r", encoding="utf-8", newline="") as todos_file:
            original = todos_file.read()
        updated = transform(original)
        fd, tmp_name = tempfile.mkstemp(dir=todos_path.parent, prefix=".TODOS.", text=True)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                tmp.write(updated)
                tmp.flush()
                os.fsync(tmp.fileno())
            _before_todos_replace()
            os.replace(tmp_path, todos_path)
        except BaseException:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise


def assign_next_todo_id(
    project_dir: Path, entry_builder: Callable[[int], str]
) -> tuple[int, list[str]]:
    """Assign the next TODO ID and append its entry in one atomic update."""
    todos_path = project_dir / "TODOS.md"
    archive_path = project_dir / "TODOS-archive.md"
    messages: list[str] = []
    assigned = 1

    def transform(text: str) -> str:
        nonlocal assigned, messages
        tracked, issues = read_next_todo_id(text)
        scanned_next = compute_scan_next_id(todos_path, archive_path)
        tracked_is_stale = tracked is not None and tracked != scanned_next
        if tracked is None or issues or tracked_is_stale:
            assigned = scanned_next
            messages.append(f"add: corrected NEXT_TODO_ID from {tracked} to {scanned_next}")
        else:
            assigned = tracked
        messages.extend(issues)
        updated = replace_next_todo_id_line(text, assigned + 1)
        entry = entry_builder(assigned).rstrip()
        sections = parse_todos_document_sections(updated)
        if sections.has_canonical_layout:
            entry = entry.replace("\r\n", "\n").replace("\r", "\n")
            entry = entry.replace("\n", sections.newline)
            existing_entries = sections.entries.rstrip()
            combined_entries = (
                f"{existing_entries}{sections.newline}{sections.newline}{entry}"
                if existing_entries
                else entry
            )
            return _replace_entries_section(updated, combined_entries)
        return updated.rstrip() + "\n\n" + entry + "\n"

    atomic_update_todos(todos_path, transform)
    return assigned, messages


def reconcile_next_todo_id(project_dir: Path, mode: str) -> tuple[int, list[str]]:
    """Repair tracked metadata so it agrees with the scan-derived ID."""
    todos_path = project_dir / "TODOS.md"
    archive_path = project_dir / "TODOS-archive.md"
    messages: list[str] = []
    reconciled_id = 1

    def transform(text: str) -> str:
        nonlocal reconciled_id
        tracked, issues = read_next_todo_id(text)
        reconciled_id = compute_scan_next_id(todos_path, archive_path)
        if tracked == reconciled_id and not issues:
            return text
        messages.extend(issues)
        if tracked is None:
            messages.append(f"{mode}: inserted NEXT_TODO_ID: {reconciled_id}")
        else:
            messages.append(
                f"{mode}: corrected NEXT_TODO_ID from {tracked} to {reconciled_id}"
            )
        return replace_next_todo_id_line(text, reconciled_id)

    atomic_update_todos(todos_path, transform)
    return reconciled_id, messages


COUNTER_FILE = ".hermes/todo_id_counter"


def read_counter_cache(project_dir: Path) -> int | None:
    """Read the counter cache file. Returns None if not found."""
    counter = project_dir / COUNTER_FILE
    if not counter.exists():
        return None
    try:
        return int(counter.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def counter_matches_scan(project_dir: Path) -> bool:
    """Check if counter cache matches max scanned ID across both files."""
    todos = project_dir / "TODOS.md"
    archive = project_dir / "TODOS-archive.md"
    all_ids: set[int] = set()
    if todos.exists():
        all_ids |= _scan_document_ids(todos.read_text(encoding="utf-8"))
    if archive.exists():
        all_ids |= _scan_document_ids(archive.read_text(encoding="utf-8"))
    if not all_ids:
        return read_counter_cache(project_dir) in (None, 0)
    max_id = max(all_ids)
    cached = read_counter_cache(project_dir)
    return cached == max_id


VALID_STATUSES = {"[ ]", "[→]", "[x]", "[~]"}

ENTRY_HEADER_RE = re.compile(
    r"^-\s+(\[[ →x~]\])\s+(?:\*\*)?TODO-(\d+):\s+([^*]+?)(?:\*\*)?(?:\s+—\s+(.+?))?$"
)

FIELD_RE = re.compile(
    r"^\s+-\s+\*\*([^*:]+?)(?::)?\*\*\s*(.+?)(?:\s*)?$"
)


def _entries_text(text: str) -> str:
    sections = parse_todos_document_sections(text)
    return sections.entries if sections.has_canonical_layout else text


def parse_entries(text: str) -> list[dict]:
    """Parse TODO entries under ## Entries from TODOS.md markdown text.

    Returns a list of dicts with keys: id, status, title, summary, fields.
    """
    lines = _entries_text(text).split("\n")
    entries: list[dict] = []
    current: dict | None = None

    for line in lines:
        header_match = ENTRY_HEADER_RE.match(line)
        if header_match:
            if current:
                entries.append(current)
            status, id_str, title, summary = header_match.groups()
            current = {
                "id": int(id_str),
                "status": status,
                "title": title.strip(),
                "summary": (summary.strip() if summary else ""),
                "fields": {},
            }
            continue

        if current is not None:
            field_match = FIELD_RE.match(line)
            if field_match:
                field_name, field_value = field_match.groups()
                current["fields"][field_name] = field_value.strip()

    if current:
        entries.append(current)

    return entries


REQUIRED_FIELDS = {"What", "Why", "Decisions"}


def validate_entry(entry: dict) -> list[str]:
    """Validate a single parsed entry against schema. Returns list of issues."""
    issues: list[str] = []

    if entry.get("status") not in VALID_STATUSES:
        issues.append(f"TODO-{entry['id']}: Invalid status marker '{entry.get('status')}' — expected one of {VALID_STATUSES}")

    for field in REQUIRED_FIELDS:
        if field not in entry.get("fields", {}):
            issues.append(f"TODO-{entry['id']}: Missing required field **{field}:**")

    return issues


def validate_all_entries(text: str) -> list[dict]:
    """Validate all entries in TODOS.md text. Returns list of {id, issues} dicts."""
    entries = parse_entries(text)
    return [{"id": e["id"], "issues": validate_entry(e)} for e in entries]


def validate_dependency_refs(text: str) -> list[str]:
    """Find dependency references pointing to non-existent IDs."""
    entries = parse_entries(text)
    # Get all IDs from actual entry headers, not field content
    all_ids = {e["id"] for e in entries}
    broken: list[str] = []

    for entry in entries:
        deps = entry["fields"].get("Depends on", "")
        if deps:
            ref_ids = scan_ids(deps)
            for ref_id in ref_ids:
                if ref_id not in all_ids:
                    broken.append(f"TODO-{entry['id']}: Dependency TODO-{ref_id} does not exist")

    return broken


def find_completed_entries(text: str) -> list[dict]:
    """Find all [x] (done) entries in TODOS.md text."""
    entries = parse_entries(text)
    return [e for e in entries if e["status"] == "[x]"]


def extract_entry_blocks(text: str) -> list[str]:
    """Extract raw markdown blocks for entries under ## Entries.

    Returns a list of strings, each containing the header line and sub-bullets
    for one entry.
    """
    lines = _entries_text(text).split("\n")
    blocks: list[str] = []
    current_block: list[str] = []

    for line in lines:
        if ENTRY_HEADER_RE.match(line):
            if current_block:
                blocks.append("\n".join(current_block))
            current_block = [line]
        elif current_block and (line.strip().startswith("- **") or (line.strip() and line[0] in (" ", "\t"))):
            current_block.append(line)
        elif current_block and line.strip() == "":
            current_block.append(line)
        elif current_block:
            blocks.append("\n".join(current_block))
            current_block = []

    if current_block:
        blocks.append("\n".join(current_block))

    return blocks


def _replace_entries_section(text: str, entries_text: str) -> str:
    sections = parse_todos_document_sections(text)
    if not sections.has_canonical_layout:
        return text
    newline = sections.newline
    lines = text.splitlines(keepends=True)
    entries_spans = [
        (start, end)
        for heading, start, end in _section_spans(lines)
        if heading == "## Entries"
    ]
    assert len(entries_spans) == 1
    start, end = entries_spans[0]
    replacement = [newline]
    if entries_text.strip():
        replacement.extend(entries_text.rstrip().splitlines(keepends=True))
        if not replacement[-1].endswith(("\n", "\r\n")):
            replacement.append(newline)
    return "".join(lines[:start] + replacement + lines[end:])


def simulate_archive(todos_text: str, archive_text: str) -> tuple[str, str]:
    """Simulate moving completed entries from TODOS.md to TODOS-archive.md.

    Returns (new_todos_text, new_archive_text).
    """
    completed = find_completed_entries(todos_text)
    if not completed:
        return todos_text, archive_text

    completed_ids = {e["id"] for e in completed}

    # Build new TODOS.md by removing completed entries
    blocks = extract_entry_blocks(todos_text)
    remaining_blocks = []
    archived_blocks = []

    for block in blocks:
        block_ids = scan_ids(block)
        if block_ids & completed_ids:
            archived_blocks.append(block)
        else:
            remaining_blocks.append(block)

    if parse_todos_document_sections(todos_text).has_canonical_layout:
        new_todos = _replace_entries_section(todos_text, "\n\n".join(remaining_blocks))
    else:
        first_entry_pos = -1
        for line in todos_text.split("\n"):
            if ENTRY_HEADER_RE.match(line):
                break
            first_entry_pos += len(line) + 1
        if first_entry_pos == -1 or first_entry_pos >= len(todos_text):
            new_todos = todos_text
        else:
            header = todos_text[:first_entry_pos]
            new_todos = header + "\n".join(remaining_blocks)

    # Append to archive
    if not archive_text.strip():
        archive_header = "# TODOS Archive\n\nCompleted TODOs, archived via `todos-manager --archive`.\n\n"
    else:
        archive_header = archive_text

    new_archive = archive_header
    if archived_blocks:
        new_archive += "\n".join(archived_blocks) + "\n"

    return new_todos, new_archive


def archive_completed_todos(project_dir: Path) -> int:
    """Move completed TODOs to the archive with a recoverable two-file write."""
    todos_path = project_dir / "TODOS.md"
    archive_path = project_dir / "TODOS-archive.md"
    lock_path = todos_path.with_suffix(todos_path.suffix + ".lock")
    _before_todo_lock()
    with _todo_lock(lock_path):
        recover_pending_todo_transaction(project_dir)
        todos_text = todos_path.read_text(encoding="utf-8")
        archive_text = (
            archive_path.read_text(encoding="utf-8") if archive_path.exists() else ""
        )
        completed_count = len(find_completed_entries(todos_text))
        if completed_count == 0:
            return 0
        updated_todos, updated_archive = simulate_archive(todos_text, archive_text)
        _write_archive_transaction(
            project_dir,
            todos_path,
            archive_path,
            updated_todos,
            updated_archive,
        )
        return completed_count
