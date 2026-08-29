#!/usr/bin/env python3
"""One-shot migration of active TODOS.md entries into GitHub Issues.

Reads ``TODOS.md``, renders each ``[ ]`` entry through
``github_issues.render_issue_body`` (the same renderer the issue form feeds),
creates the issue with the label vocabulary plus ``legacy-id:TODO-<n>``, wires
``Depends on:`` edges between migrated entries, and writes a mapping document.
Idempotent: an issue already carrying the legacy-id label is reused, and an
existing mapping document is merged rather than rewritten.

All GitHub access goes through :mod:`hermes_pipeline.github_issues`; this
script never spawns a subprocess itself and never prints token material.
``--dry-run`` performs no ``gh``/``git`` call and writes no file.

Exit codes: 0 ok, 1 gh failure or conflict (mapping so far is printed and
written), 2 usage/data error.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_pipeline import github_issues as gi
from hermes_pipeline.todos_md import TodoEntry, parse_todo_entries

_FIELD_RE = re.compile(r"^(?P<indent>\s*)- \*\*(?P<k>[^:*]+):\*\*(?: ?(?P<v>.*))?$")
_HEADER_RE = re.compile(r"^- \[(?P<status>[ x→~])\] \*\*(?P<todo_id>TODO-\d+):")
_SUMMARY_SEP = " — "
_DECISION_RE = re.compile(
    r"(Priority|Effort|Phase|Branch|Test Coverage|Security Review|UI Review) `([^`]+)`"
)
_TODO_ID_RE = re.compile(r"`(TODO-\d+)`")
_MAPPING_ROW_RE = re.compile(r"^\| (TODO-\d+) \| ([^|]*) \| (.*) \|$")
_ISSUE_CELL_RE = re.compile(r"^#(\d+)")
REQUIRED_DECISIONS = ("Priority", "Effort", "Phase", "Branch", "Test Coverage", "Security Review")
LABEL_DECISIONS = {
    "Priority": "priority:",
    "Effort": "effort:",
    "Test Coverage": "test-coverage:",
    "Security Review": "security-review:",
    "UI Review": "ui-review:",
}
# Mirrors the Phase dropdown in .github/ISSUE_TEMPLATE/tpo-todo.yml (parity-tested).
PHASE_OPTIONS = (
    "2 (Design)",
    "3 (Writing Plan)",
    "4 (Development)",
    "5 (Code Review)",
    "6.1 (CSO Security Review)",
    "6.2 (QA)",
    "7 (Document Release)",
    "8 (Finish Branch)",
)
PHASE_OPTION_ALIASES = {"2 (Autoplan)": "2 (Design)"}
DEFAULT_UI_REVIEW = "not-required"
PHASE_LABEL_COLOR = "c5def5"
LEGACY_LABEL_COLOR = "ededed"
DRY_RUN_NUMBER = "(dry-run)"
PENDING_NUMBER = "(pending)"
NOT_IMPORTED_REASONS = {
    "x": "`[x]` done in TODOS.md (not yet archived)",
    "→": "`[→]` in progress: re-add after migration",
    "~": "`[~]` on hold: re-add with tpo:on-hold",
}
DOC_TITLE = "# TODOS.md → GitHub Issues migration"


class MigrationDataError(ValueError):
    """TODOS.md content or arguments cannot be migrated as-is (exit code 2)."""


class MigrationConflictError(RuntimeError):
    """The repository state contradicts the migration (exit code 1)."""


@dataclass(frozen=True)
class IssueFields:
    todo_id: str
    title: str
    body: str
    labels: tuple[str, ...]
    plan: str
    branch: str
    phase: str
    dependencies: tuple[str, ...]


@dataclass
class MappingState:
    """Issue numbers assigned so far plus which reused issues are closed."""

    numbers: dict[str, int] = field(default_factory=dict)
    closed: set[str] = field(default_factory=set)
    placeholder: str = PENDING_NUMBER

    def cell(self, todo_id: str) -> str:
        number = self.numbers.get(todo_id)
        if number is None:
            return self.placeholder
        return f"#{number} (closed)" if todo_id in self.closed else f"#{number}"

    def ref(self, todo_id: str) -> str:
        number = self.numbers.get(todo_id)
        return f"#{number} ({todo_id})" if number is not None else todo_id


# -- TODOS.md parsing -------------------------------------------------------


def entry_block(entry: TodoEntry, text: str) -> str:
    """Return the full entry block from ``text`` (header through the next header).

    ``TodoEntry.raw`` stops at the first continuation line indented deeper than
    ``  - ``, so multi-line field values are re-read from the source text.
    """
    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if _HEADER_RE.match(line) and f"**{entry.todo_id}:" in line),
        None,
    )
    if start is None:
        raise MigrationDataError(f"{entry.todo_id}: header not found in TODOS.md")
    end = start + 1
    while end < len(lines) and not _HEADER_RE.match(lines[end]) and not lines[end].startswith("## "):
        end += 1
    return "\n".join(lines[start:end]).rstrip() + "\n"


def parse_fields(block: str) -> dict[str, str]:
    """``**Field:** value`` sub-bullets with continuation lines.

    A field starts on any line matching ``- **Field:**`` regardless of indent;
    an indent other than two spaces is an error. Continuation lines drop the
    common four-space prefix, keep deeper relative indentation, and blank lines
    inside a value are preserved.
    """
    fields: dict[str, list[str]] = {}
    current: str | None = None
    for line in block.splitlines()[1:]:
        match = _FIELD_RE.match(line)
        if match:
            if match.group("indent") != "  ":
                raise MigrationDataError(
                    f"inconsistent indentation for field {match.group('k').strip()!r}"
                )
            current = match.group("k").strip()
            if current in fields:
                raise MigrationDataError(f"duplicate field {current!r}")
            fields[current] = [(match.group("v") or "").strip()]
            continue
        if current is None:
            if line.strip():
                raise MigrationDataError(f"unexpected line before first field: {line.strip()!r}")
            continue
        if not line.strip():
            fields[current].append("")
        elif line.startswith("    "):
            fields[current].append(line[4:].rstrip())
        else:
            raise MigrationDataError(f"inconsistent indentation in field {current!r}")
    return {key: "\n".join(lines).strip("\n") for key, lines in fields.items()}


def parse_summary(header: str) -> str:
    """Text after `` — `` following the closing ``**`` of the header title."""
    parts = header.split("**", 2)
    if len(parts) < 3:
        return ""
    rest = parts[2]
    return rest.split(_SUMMARY_SEP, 1)[1].strip() if _SUMMARY_SEP in rest else ""


def parse_decisions(todo_id: str, value: str) -> dict[str, str]:
    decisions = dict(_DECISION_RE.findall(value))
    missing = [key for key in REQUIRED_DECISIONS if key not in decisions]
    if missing:
        raise MigrationDataError(f"{todo_id}: Decisions missing {', '.join(missing)}")
    decisions.setdefault("UI Review", DEFAULT_UI_REVIEW)
    label_names = {name for name, _, _ in gi.LABEL_VOCABULARY}
    for key, prefix in LABEL_DECISIONS.items():
        if f"{prefix}{decisions[key]}" not in label_names:
            raise MigrationDataError(
                f"{todo_id}: {key} {decisions[key]!r} has no label in LABEL_VOCABULARY"
            )
    phase = PHASE_OPTION_ALIASES.get(decisions["Phase"], decisions["Phase"])
    if phase not in PHASE_OPTIONS:
        raise MigrationDataError(
            f"{todo_id}: Phase {decisions['Phase']!r} is not a form option "
            f"({', '.join(PHASE_OPTIONS)})"
        )
    decisions["Phase"] = phase
    if not decisions["Branch"].strip():
        raise MigrationDataError(f"{todo_id}: Branch is empty")
    return decisions


def extract_fields(entry: TodoEntry, text: str) -> IssueFields:
    block = entry_block(entry, text)
    summary = parse_summary(block.splitlines()[0])
    try:
        fields = parse_fields(block)
    except MigrationDataError as exc:
        raise MigrationDataError(f"{entry.todo_id}: {exc}") from None
    if "Decisions" not in fields:
        raise MigrationDataError(f"{entry.todo_id}: Decisions missing")
    decisions = parse_decisions(entry.todo_id, fields["Decisions"])
    for required in ("What", "Why"):
        if not fields.get(required, "").strip():
            raise MigrationDataError(f"{entry.todo_id}: {required} is missing or empty")
    depends_raw = fields.get("Depends on", "")
    if entry.dependencies is None and depends_raw and depends_raw != "(none)":
        raise MigrationDataError(f"{entry.todo_id}: malformed Depends on: {depends_raw!r}")
    dependencies = tuple(_TODO_ID_RE.findall(depends_raw))
    try:
        body = gi.render_issue_body(
            {
                "Summary": summary,
                "What": fields.get("What", ""),
                "Why": fields.get("Why", ""),
                "Pros": fields.get("Pros", ""),
                "Cons": fields.get("Cons", ""),
                "Context": fields.get("Context", ""),
                "Assumptions": fields.get("Assumptions", ""),
                "Plan": fields.get("Plan", ""),
                "Spec": fields.get("Spec", ""),
                "Reference": ", ".join(
                    item.strip() for item in fields.get("Reference", "").split(",") if item.strip()
                ),
                "Branch": decisions["Branch"],
                "Priority": decisions["Priority"],
                "Effort": decisions["Effort"],
                "Phase": decisions["Phase"],
                "Test Coverage": decisions["Test Coverage"],
                "Security Review": decisions["Security Review"],
                "UI Review": decisions["UI Review"],
                "Legacy ID": entry.todo_id,
            },
            include_empty=True,
        )
    except ValueError as exc:
        raise MigrationDataError(f"{entry.todo_id}: {exc}") from None
    if len(body) > gi.MAX_ISSUE_BODY_CHARS:
        raise MigrationDataError(
            f"{entry.todo_id}: body is {len(body)} chars, limit {gi.MAX_ISSUE_BODY_CHARS}"
        )
    labels = (
        gi.TODO_LABEL,
        gi.READY_LABEL,
        f"priority:{decisions['Priority']}",
        f"effort:{decisions['Effort']}",
        gi.phase_label(decisions["Phase"]),
        f"test-coverage:{decisions['Test Coverage']}",
        f"security-review:{decisions['Security Review']}",
        f"ui-review:{decisions['UI Review']}",
        gi.legacy_id_label(entry.todo_id),
    )
    return IssueFields(
        todo_id=entry.todo_id,
        title=entry.title,
        body=body,
        labels=labels,
        plan=fields.get("Plan", "").strip(),
        branch=decisions["Branch"],
        phase=decisions["Phase"],
        dependencies=dependencies,
    )


def compute_dependency_edges(
    items: list[IssueFields],
    *,
    done_ids: frozenset[str] = frozenset(),
    active_ids: frozenset[str] = frozenset(),
) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]]]:
    """Split ``Depends on`` into (child, blocker) edges among migrated ids and
    (child, dependency, class) triples for dependencies outside the run.

    Classes: ``done`` (``[x]`` in TODOS.md or present in TODOS-archive.md) and
    ``not selected in this run`` (active but excluded via ``--only``). Any other
    id is unknown and raises :class:`MigrationDataError`.
    """
    migrated = {item.todo_id for item in items}
    edges: list[tuple[str, str]] = []
    outside: list[tuple[str, str, str]] = []
    for item in items:
        for dep in item.dependencies:
            if dep in migrated:
                edges.append((item.todo_id, dep))
            elif dep in done_ids:
                outside.append((item.todo_id, dep, "done"))
            elif dep in active_ids:
                outside.append((item.todo_id, dep, "not selected in this run"))
            else:
                raise MigrationDataError(
                    f"{item.todo_id}: Depends on {dep}, an unknown id (not active, done, or archived)"
                )
    return edges, outside


def extra_labels(items: list[IssueFields]) -> list[tuple[str, str, str]]:
    extra: dict[str, tuple[str, str, str]] = {}
    for item in items:
        phase = gi.phase_label(item.phase)
        extra.setdefault(phase, (phase, PHASE_LABEL_COLOR, f"Phase {item.phase}"))
        legacy = gi.legacy_id_label(item.todo_id)
        extra.setdefault(legacy, (legacy, LEGACY_LABEL_COLOR, f"Migrated from TODOS.md {item.todo_id}"))
    return list(extra.values())


# -- mapping document -------------------------------------------------------


def _todo_number(todo_id: str) -> int:
    return int(todo_id.split("-", 1)[1])


def parse_mapping_rows(doc: str) -> dict[str, tuple[str, str]]:
    """``legacy id -> (issue cell, rest of row)`` from an existing mapping doc."""
    rows: dict[str, tuple[str, str]] = {}
    for line in doc.splitlines():
        match = _MAPPING_ROW_RE.match(line)
        if match:
            rows[match.group(1)] = (match.group(2).strip(), match.group(3))
    return rows


def render_table(
    items: list[IssueFields], state: MappingState, previous: dict[str, tuple[str, str]] | None = None
) -> str:
    """Mapping table; rows from ``previous`` are merged and a real ``#N`` never regresses."""
    previous = previous or {}
    rows: dict[str, tuple[str, str]] = dict(previous)
    for item in items:
        cell = state.cell(item.todo_id)
        old = previous.get(item.todo_id)
        if item.todo_id not in state.numbers and old is not None and _ISSUE_CELL_RE.match(old[0]):
            cell = old[0]
        rows[item.todo_id] = (cell, f"{item.title} | {item.plan or '(none)'} | {item.branch}")
    lines = ["| Legacy ID | Issue | Title | Plan | Branch |", "|---|---|---|---|---|"]
    for todo_id in sorted(rows, key=_todo_number):
        cell, rest = rows[todo_id]
        lines.append(f"| {todo_id} | {cell} | {rest} |")
    return "\n".join(lines) + "\n"


def render_mapping_doc(
    *,
    repo: str,
    items: list[IssueFields],
    state: MappingState,
    not_imported: list[tuple[str, str]],
    edges: list[tuple[str, str]],
    outside: list[tuple[str, str, str]],
    skipped_blockers: list[tuple[str, str]],
    now: datetime,
    previous: dict[str, tuple[str, str]] | None = None,
) -> str:
    lines = [
        DOC_TITLE,
        "",
        f"- Date: {now.strftime('%Y-%m-%d %H:%M UTC')}",
        f"- Repository: {repo}",
        "- Labels: missing vocabulary/phase/legacy-id labels were created; existing labels "
        "are left untouched, including color/description.",
        "",
        "## Mapping",
        "",
        render_table(items, state, previous).rstrip(),
        "",
        "## Not imported",
        "",
    ]
    lines.extend(
        [f"- {todo_id}: {NOT_IMPORTED_REASONS[status]}" for todo_id, status in not_imported]
        or ["- (none)"]
    )
    lines.extend(["", "## Dependencies", "", "Edges created (`blocked_by`):", ""])
    created = [(c, b) for c, b in edges if (c, b) not in skipped_blockers]
    lines.extend(
        [f"- {state.ref(c)} blocked by {state.ref(b)}" for c, b in created] or ["- (none)"]
    )
    if skipped_blockers:
        lines.extend(["", "Edges skipped (blocker issue is closed):", ""])
        lines.extend(f"- {state.ref(c)} not blocked by {state.ref(b)}" for c, b in skipped_blockers)
    lines.extend(["", "Already satisfied (dependency outside this run):", ""])
    lines.extend([f"- {c} → {d}: {cls}" for c, d, cls in outside] or ["- (none)"])
    lines.extend(["", "## Plan manifests to rewrite", ""])
    lines.extend(
        [
            f"- `{item.plan}`: set manifest `todo_id` to `TODO-{state.numbers.get(item.todo_id, '<new#>')}` "
            f"(currently `{item.todo_id}`) — done in the runtime cutover commit"
            for item in items
            if item.plan
        ]
        or ["- (none)"]
    )
    lines.extend(
        [
            "",
            "## Legacy references",
            "",
            "Legacy `TODO-<n>` identifiers remain in plan filenames "
            "(`docs/superpowers/plans/*-todo-<n>-*.md`), branch names "
            "(`feature/todo-<n>-*`), `docs/pipeline/TODO-25-*`, and issues #16 and #21 carry "
            "legacy `TODO-N:` title prefixes. Each migrated issue carries `legacy-id:TODO-<n>` "
            "and a `### Legacy ID` section so those references stay resolvable.",
            "",
        ]
    )
    return "\n".join(lines)


# -- CLI --------------------------------------------------------------------


def _select_entries(
    text: str, only: list[str] | None
) -> tuple[list[TodoEntry], list[tuple[str, str]], frozenset[str], frozenset[str]]:
    entries = parse_todo_entries(text)
    active = [entry for entry in entries if entry.status == " "]
    not_imported = [(entry.todo_id, entry.status) for entry in entries if entry.status != " "]
    done_ids = frozenset(entry.todo_id for entry in entries if entry.status == "x")
    active_ids = frozenset(entry.todo_id for entry in active)
    if only:
        by_id = {entry.todo_id: entry for entry in active}
        missing = [todo_id for todo_id in only if todo_id not in by_id]
        if missing:
            raise MigrationDataError(
                f"--only ids not found among active entries: {', '.join(missing)}"
            )
        active = [by_id[todo_id] for todo_id in only]
    return active, not_imported, done_ids, active_ids


def _archived_ids(todos_path: Path, project_dir: Path) -> frozenset[str]:
    """Ids in ``TODOS-archive.md``, looked up next to TODOS.md, then under docs/history/."""
    candidates = (
        todos_path.with_name("TODOS-archive.md"),
        project_dir / "docs" / "history" / "TODOS-archive.md",
    )
    archive = next((path for path in candidates if path.exists()), None)
    if archive is None:
        return frozenset()
    print(f"WARNING: using archive {archive} to classify done dependencies", file=sys.stderr)
    return frozenset(entry.todo_id for entry in parse_todo_entries(archive.read_text(encoding="utf-8")))


def _probe_writable(path: Path) -> None:
    """Prove ``path`` is writable. Created parent directories are kept; a file
    created only by the probe is removed again so no empty document is left."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    with path.open("a", encoding="utf-8"):
        pass
    if not existed:
        path.unlink()


def _read_previous_doc(path: Path) -> dict[str, tuple[str, str]]:
    if not path.exists():
        return {}
    try:
        return parse_mapping_rows(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise MigrationDataError(f"cannot read existing mapping document {path}: {exc}") from None


def _resolve(path: Path, project_dir: Path) -> Path:
    return path if path.is_absolute() else project_dir / path


def _resolve_repo(args: argparse.Namespace, project_dir: Path) -> str:
    if args.dry_run:
        return args.repo or "(origin)"
    try:
        origin = gi.repository_identity(project_dir)
    except gi.GitHubIssuesError as exc:
        if args.repo is None:
            raise MigrationDataError(f"--repo not given and origin unresolvable ({exc})") from None
        print(f"warning: origin unresolvable ({exc}); trusting --repo {args.repo}", file=sys.stderr)
        return args.repo
    if args.repo is None or args.repo.lower() == origin.lower():
        return args.repo or origin
    if args.allow_repo_mismatch:
        print(f"warning: --repo {args.repo} differs from origin {origin}", file=sys.stderr)
        return args.repo
    raise MigrationDataError(
        f"repo mismatch: --repo {args.repo} but origin is {origin} (use --allow-repo-mismatch)"
    )


def _migrate(
    project_dir: Path, repo: str, items: list[IssueFields], edges: list[tuple[str, str]],
    state: MappingState, skipped_blockers: list[tuple[str, str]],
) -> None:
    gi.check_auth(project_dir)
    gi.ensure_labels(project_dir, repo=repo, extra=extra_labels(items))
    for item in items:
        existing = gi.find_issues_by_label(
            project_dir, gi.legacy_id_label(item.todo_id), state="all", repo=repo
        )
        if len(existing) > 1:
            numbers = ", ".join(f"#{issue.number}" for issue in existing)
            raise MigrationConflictError(
                f"{item.todo_id}: {len(existing)} issues carry {gi.legacy_id_label(item.todo_id)} ({numbers})"
            )
        if existing:
            (issue,) = existing
            state.numbers[item.todo_id] = issue.number
            if issue.state != "open":
                state.closed.add(item.todo_id)
                print(f"{item.todo_id} -> #{issue.number} exists (closed)")
            else:
                print(f"{item.todo_id} -> #{issue.number} exists")
            continue
        number = gi.create_issue(
            project_dir, title=item.title, body=item.body, labels=item.labels, repo=repo
        )
        state.numbers[item.todo_id] = number
        print(f"{item.todo_id} -> #{number} created")
    for child, blocker in edges:
        if blocker in state.closed:
            skipped_blockers.append((child, blocker))
            print(
                f"warning: {state.ref(child)} not blocked by {state.ref(blocker)} "
                "(blocker issue is closed)", file=sys.stderr,
            )
            continue
        gi.add_blocked_by(project_dir, state.numbers[child], state.numbers[blocker], repo=repo)
        print(f"{state.ref(child)} blocked by {state.ref(blocker)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--todos", type=Path, default=Path("TODOS.md"))
    parser.add_argument("--project-dir", type=Path, default=Path("."))
    parser.add_argument("--repo", help="owner/repo; defaults to the origin remote")
    parser.add_argument("--allow-repo-mismatch", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only", help="comma-separated TODO ids to migrate")
    parser.add_argument("--out", type=Path, default=Path("docs/migration/todos-to-issues.md"))
    args = parser.parse_args(argv)

    project_dir = args.project_dir
    todos_path = _resolve(args.todos, project_dir)
    out_path = _resolve(args.out, project_dir)
    try:
        text = todos_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read {todos_path}: {exc}", file=sys.stderr)
        return 2
    only = [part.strip() for part in args.only.split(",") if part.strip()] if args.only else None

    try:
        active, not_imported, done_ids, active_ids = _select_entries(text, only)
        items = [extract_fields(entry, text) for entry in active]
        edges, outside = compute_dependency_edges(
            items, done_ids=done_ids | _archived_ids(todos_path, project_dir), active_ids=active_ids
        )
        repo = _resolve_repo(args, project_dir)
        if not args.dry_run:
            try:
                _probe_writable(out_path)
            except OSError as exc:
                raise MigrationDataError(f"--out {out_path} is not writable: {exc}") from None
            previous = _read_previous_doc(out_path)
    except MigrationDataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    state = MappingState(placeholder=DRY_RUN_NUMBER if args.dry_run else PENDING_NUMBER)
    skipped_blockers: list[tuple[str, str]] = []
    now = datetime.now(UTC)
    for item in items:
        if item.plan:
            print(f"WARNING: {item.plan}: manifest todo_id must be rewritten from {item.todo_id}")

    def doc(previous: dict[str, tuple[str, str]] | None = None) -> str:
        return render_mapping_doc(
            repo=repo, items=items, state=state, not_imported=not_imported, edges=edges,
            outside=outside, skipped_blockers=skipped_blockers, now=now, previous=previous,
        )

    if args.dry_run:
        for item in items:
            print(f"--- {item.todo_id}: {item.title}")
            print(f"labels: {', '.join(item.labels)}")
            print(item.body)
        print(doc())
        print(f"dry-run: mapping document not written (would go to {out_path})")
        return 0

    rc = 0
    try:
        _migrate(project_dir, repo, items, edges, state, skipped_blockers)
    except (gi.GitHubIssuesError, MigrationConflictError) as exc:
        pending = [item.todo_id for item in items if item.todo_id not in state.numbers]
        print(f"error: {exc}; stopped before {', '.join(pending) or 'dependency wiring'}", file=sys.stderr)
        print("mapping so far:")
        rc = 1
    out_path.write_text(doc(previous), encoding="utf-8")
    print(render_table(items, state, previous))
    print(f"mapping written to {out_path}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
