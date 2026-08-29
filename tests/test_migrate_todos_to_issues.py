"""CLI-boundary tests for ``scripts/migrate_todos_to_issues.py``.

Every test drives ``main(argv)`` directly and observes ``gh`` through the
``fake_gh`` recorder, so no subprocess is ever spawned.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from hermes_pipeline import github_issues as gi
from hermes_pipeline.todos_md import parse_todo_entries
from scripts.migrate_todos_to_issues import (
    PHASE_OPTION_ALIASES,
    PHASE_OPTIONS,
    MigrationDataError,
    compute_dependency_edges,
    extract_fields,
    main,
)
from tests.gh_fakes import issue_payload

REPO = "acme/repo"
ACCEPT = ["-H", "Accept: application/vnd.github+json"]
REPO_ROOT = Path(__file__).resolve().parents[1]
FORM_PATH = REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "tpo-todo.yml"
AUTH_OK = ("gh", "auth", "status", "--hostname", "github.com")

TODO_43 = """\
- [ ] **TODO-43: Refactor production orchestration hotspots** — Extract cohesive boundaries from oversized CLI, harness, and Kanban functions
  - **What:** Incrementally extract cohesive, testable boundaries from `cli._tick_project`, `harness.run_harness`/`_poll_kanban_phases`, and `kanban_tasks.create_prepared_todo_phases`; preserve public behavior, cancellation/recovery semantics, durable-state ordering, and existing CLI contracts. Broad redesign and unrelated cleanup are out of scope.
  - **Why:** Reduce maintenance and regression risk in oversized orchestration functions while their behavior remains protected by strong tests.
  - **Pros:** Smaller review surfaces, clearer ownership, easier focused testing, and safer future changes to pipeline orchestration.
  - **Cons:** High regression potential around state transitions and external-process cleanup; temporary adapter layers may be needed while boundaries move.
  - **Context:** `hermes_pipeline/cli.py`, `hermes_pipeline/harness.py`, `hermes_pipeline/kanban_tasks.py`, `docs/ARCHITECTURE.md`
  - **Depends on:** (none)
  - **Assumptions:** Refactoring will be divided into independently reviewable, behavior-preserving slices; existing provider-free tests remain the primary regression gate, with live Hermes validation identified separately where necessary.
  - **Plan:** docs/superpowers/plans/2026-08-20-todo-43-production-orchestration-hotspots.md
  - **Reference:** docs/ARCHITECTURE.md
  - **Decisions:** Priority `P2`, Effort `L`, Phase `2 (Design)`, Branch `feature/todo-43-orchestration-hotspots-impl`, Test Coverage `required`, Security Review `not-required`, UI Review `not-required`
"""

TODO_36 = """\
- [x] **TODO-36: Reorganize and refresh README.md's docs table** — Fix broken link, remove stale CLI-name references, group the 30-entry doc table by subsystem, and rewrite Getting Started for real install paths
  - **What:** Restructure the flat 30-row "Documentation" table in README.md (README.md:37-75) into subsystem-grouped sections (pipeline core, multi-project setup, pipeline contract, todos-manager, skill test harness) instead of one undifferentiated table. Fix the broken link `[Install TODOS Manager](tpo-skills-install)` (README.md:63) to point at a real doc/section. Update `docs/pipeline-modularization-plan.md`, which still references the pre-rename `pipeline-watch`/`hermes-pipeline` CLI names instead of `tpo`. Rewrite the Getting Started / Installation section (README.md:103-138) to reflect the actual install and onboarding paths, currently undocumented or misleading:
    1. Document `uv tool install` as the primary install method — the CLI is invoked directly as `tpo ...`, not `uv run tpo ...` (README's current "Run"/"CLI Commands" section (README.md:113-155) exclusively shows `uv run tpo <cmd>`, which only applies to running from a source checkout, not the packaged/installed CLI).
    2. Add an explicit "starting a project from scratch" path (`tpo init <project>` on a project with no TODOS.md yet, tying into `todos-manager --init`).
    3. Add an explicit "adopting tpo on an existing project" path — call out that `todos-manager --convert` (or `--revise` for individual entries) is required to bring a pre-existing/hand-written TODOS.md into the enforced schema before `tpo tick` can select from it.
  - **Why:** The docs table has grown organically to 30 links in one flat list with no grouping beyond a `Quadrant` column, making it hard to find the right doc. One link is broken (points at a CLI command name, not a path). `pipeline-modularization-plan.md` was missed during the `tpo` CLI rename (commits `5fbc837`..`e6aab3a`), leaving stale command names in a doc new contributors are pointed to first. Separately, the Getting Started flow only demonstrates `uv run tpo ...` from a source checkout and never mentions `uv tool install` (the actual distribution model per TODO-34's context) or the two distinct onboarding paths — new project vs. existing project with a pre-existing TODOS.md needing `--convert`/`--revise` — leaving new users without a clear starting point for either case.
  - **Pros:** Easier onboarding via a scannable, grouped doc index; no dead links; consistent CLI naming across all docs; new users get a correct install command and a clear fork for "new project" vs. "existing project" onboarding.
  - **Cons:** Touches a widely-linked file (README.md); requires re-verifying every link after restructuring to avoid introducing new breaks; Getting Started rewrite needs to stay in sync with `docs/tutorial-getting-started.md`, which may itself need the same `uv tool install` correction.
  - **Context:** README.md Documentation table (lines 37-75) and Getting Started / Run sections (lines 103-155); stale references in docs/pipeline-modularization-plan.md; CLI rename history in TODO-33 (already-committed `tpo` rename) and TODO-34/35 (skills install follow-ons); `todos-manager --convert`/`--revise` subcommands (docs/howto-todos-manager.md) as the existing-project onboarding mechanism.
  - **Spec:** docs/superpowers/plans/2026-07-28-todo-36-readme-refresh.md
  - **Reference:** docs/superpowers/specs/2026-07-28-todo-36-readme-refresh-design.md
  - **Decisions:** Priority `P2`, Effort `M`, Phase `4 (Development)`, Branch `docs/reorganize-readme-docs-table`, Test Coverage `not-required`, Security Review `not-required`, UI Review `not-required`
  - **Completed:** v0.6.4 (2026-07-28)
"""

# Active entry with a multi-line What (nested list + paragraph break), an
# em dash inside the title, and dependencies on TODO-43 (active), TODO-36
# (done in TODOS.md) and TODO-2 (archived).
TODO_50 = """\
- [ ] **TODO-50: Multi-line fixture — with em dash** — Exercises continuation lines
  - **What:** First paragraph of the What field:
    1. first continuation item
       - nested bullet
    2. second continuation item

    Second paragraph after a blank line.
  - **Why:** Because continuation lines must survive migration.
  - **Depends on:** `TODO-43`, `TODO-36`, `TODO-2`
  - **Reference:** docs/a.md, docs/b.md
  - **Decisions:** Priority `P1`, Effort `S`, Phase `4 (Development)`, Branch `feature/multi-line`, Test Coverage `required`, Security Review `required`
"""

TODO_51 = """\
- [→] **TODO-51: In-progress entry** — Must not migrate
  - **What:** x
  - **Why:** y
  - **Decisions:** Priority `P1`, Effort `S`, Phase `4 (Development)`, Branch `feature/x`, Test Coverage `required`, Security Review `required`
"""

TODO_52 = """\
- [~] **TODO-52: On-hold entry** — Must not migrate
  - **What:** x
  - **Why:** y
  - **Decisions:** Priority `P1`, Effort `S`, Phase `4 (Development)`, Branch `feature/x`, Test Coverage `required`, Security Review `required`
"""

PREAMBLE = (
    "# TODOS\n\n## Metadata\n\nNEXT_TODO_ID: 53\n\n## Entry Schema\n\n"
    "> - Optional fields: **Pros:**, **Depends on:**\n\n## Entries\n\n"
)
FIXTURE = PREAMBLE + "\n".join([TODO_43, TODO_36, TODO_50, TODO_51, TODO_52])
ARCHIVE = (
    "# TODOS Archive\n\n## Entries\n\n"
    "- [x] **TODO-2: Archived entry** — done\n  - **What:** x\n  - **Why:** y\n"
    "  - **Decisions:** Priority `P1`, Effort `S`, Phase `4 (Development)`, Branch `feature/a`, Test Coverage `required`, Security Review `required`\n"
)

GOLDEN_43_BODY = (
    "### Summary\n\nExtract cohesive boundaries from oversized CLI, harness, and Kanban functions\n\n"
    "### What\n\nIncrementally extract cohesive, testable boundaries from `cli._tick_project`, "
    "`harness.run_harness`/`_poll_kanban_phases`, and `kanban_tasks.create_prepared_todo_phases`; "
    "preserve public behavior, cancellation/recovery semantics, durable-state ordering, and "
    "existing CLI contracts. Broad redesign and unrelated cleanup are out of scope.\n\n"
    "### Why\n\nReduce maintenance and regression risk in oversized orchestration functions while "
    "their behavior remains protected by strong tests.\n\n"
    "### Pros\n\nSmaller review surfaces, clearer ownership, easier focused testing, and safer "
    "future changes to pipeline orchestration.\n\n"
    "### Cons\n\nHigh regression potential around state transitions and external-process cleanup; "
    "temporary adapter layers may be needed while boundaries move.\n\n"
    "### Context\n\n`hermes_pipeline/cli.py`, `hermes_pipeline/harness.py`, "
    "`hermes_pipeline/kanban_tasks.py`, `docs/ARCHITECTURE.md`\n\n"
    "### Assumptions\n\nRefactoring will be divided into independently reviewable, "
    "behavior-preserving slices; existing provider-free tests remain the primary regression gate, "
    "with live Hermes validation identified separately where necessary.\n\n"
    "### Plan\n\ndocs/superpowers/plans/2026-08-20-todo-43-production-orchestration-hotspots.md\n\n"
    "### Spec\n\n_No response_\n\n"
    "### Reference\n\ndocs/ARCHITECTURE.md\n\n"
    "### Branch\n\nfeature/todo-43-orchestration-hotspots-impl\n\n"
    "### Priority\n\nP2\n\n"
    "### Effort\n\nL\n\n"
    "### Phase\n\n2 (Design)\n\n"
    "### Test Coverage\n\nrequired\n\n"
    "### Security Review\n\nnot-required\n\n"
    "### UI Review\n\nnot-required\n\n"
    "### Legacy ID\n\nTODO-43\n"
)

GOLDEN_43_LABELS = {
    "tpo:todo",
    "ready-for-agent",
    "priority:P2",
    "effort:L",
    "phase:2-design",
    "test-coverage:required",
    "security-review:not-required",
    "ui-review:not-required",
    "legacy-id:TODO-43",
}

TODO_43_ROW = (
    "| TODO-43 | {issue} | Refactor production orchestration hotspots | "
    "docs/superpowers/plans/2026-08-20-todo-43-production-orchestration-hotspots.md | "
    "feature/todo-43-orchestration-hotspots-impl |"
)
TODO_50_ROW = "| TODO-50 | {issue} | Multi-line fixture — with em dash | (none) | feature/multi-line |"
LEGACY_43 = f"repos/{REPO}/issues?state=all&labels=legacy-id%3ATODO-43&per_page=100"
LEGACY_50 = f"repos/{REPO}/issues?state=all&labels=legacy-id%3ATODO-50&per_page=100"


def _label_list_stdout(names) -> str:
    return json.dumps([{"name": name} for name in names])


def _write_fixture(tmp_path: Path, text: str = FIXTURE, archive: str | None = ARCHIVE) -> Path:
    todos = tmp_path / "TODOS.md"
    todos.write_text(text, encoding="utf-8")
    if archive is not None:
        (tmp_path / "TODOS-archive.md").write_text(archive, encoding="utf-8")
    return todos


def _base_argv(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--todos", str(tmp_path / "TODOS.md"),
        "--project-dir", str(tmp_path),
        "--repo", REPO,
        "--out", str(tmp_path / "docs" / "migration" / "todos-to-issues.md"),
        *extra,
    ]


def _doc(tmp_path: Path) -> str:
    return (tmp_path / "docs" / "migration" / "todos-to-issues.md").read_text(encoding="utf-8")


def _entry(todo_id: str, text: str = FIXTURE):
    return next(e for e in parse_todo_entries(text) if e.todo_id == todo_id)


def _wire_live(fake_gh, numbers: dict[str, int], *, origin: str = f"git@github.com:{REPO}.git"):
    """Auth ok, origin matches, labels present, empty legacy lookups, numbered creates."""
    fake_gh.on("git", "remote", "get-url", "origin", stdout=origin + "\n")
    fake_gh.on(*AUTH_OK)
    fake_gh.on("gh", "label", "list", stdout=_label_list_stdout(n for n, _, _ in gi.LABEL_VOCABULARY))
    fake_gh.on("gh", "label", "create")
    fake_gh.on("gh", "api", *ACCEPT, "--paginate", "--slurp", stdout="[[]]")

    def create(argv):
        legacy = next(a for a in argv if a.startswith("legacy-id:"))
        return 0, f"https://github.com/{REPO}/issues/{numbers[legacy.split(':', 1)[1]]}\n", ""

    fake_gh.on("gh", "issue", "create", handler=create)


def _assert_no_shell(fake_gh):
    assert fake_gh.calls, "expected at least one recorded call"
    assert all("shell" not in kwargs for kwargs in fake_gh.kwargs)


# -- pure field extraction --------------------------------------------------


def test_golden_body_for_todo_43_matches_render_issue_body():
    fields = extract_fields(_entry("TODO-43"), FIXTURE)
    assert fields.body == GOLDEN_43_BODY
    assert fields.title == "Refactor production orchestration hotspots"
    assert set(fields.labels) == GOLDEN_43_LABELS


def test_multi_line_what_keeps_relative_indent_and_blank_lines():
    fields = extract_fields(_entry("TODO-50"), FIXTURE)
    sections = gi.parse_issue_body(fields.body)
    assert sections["What"] == (
        "First paragraph of the What field:\n"
        "1. first continuation item\n"
        "   - nested bullet\n"
        "2. second continuation item\n"
        "\n"
        "Second paragraph after a blank line.",
    )
    assert sections["Summary"] == ("Exercises continuation lines",)  # em dash in title ignored
    assert fields.title == "Multi-line fixture — with em dash"
    assert sections["Reference"] == ("docs/a.md, docs/b.md",)
    assert sections["Legacy ID"] == ("TODO-50",)
    assert "security-review:required" in fields.labels
    assert "ui-review:not-required" in fields.labels  # default when Decisions omit UI Review


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (("Effort `L`, ", ""), "TODO-43: Decisions missing Effort"),
        (("Effort `L`", "Effort `XL`"), "TODO-43: Effort 'XL' has no label"),
        (("Phase `2 (Design)`", "Phase `9 (Nope)`"), "TODO-43: Phase '9 (Nope)' is not a form option"),
        (("  - **Why:** Reduce", "  - **Why:** ### Reduce"), "TODO-43: section values must not contain H3"),
        (("  - **Why:** Reduce maintenance and regression risk in oversized orchestration functions while their behavior remains protected by strong tests.", "  - **Why:**"), "TODO-43: Why is missing or empty"),
        (("  - **Pros:** Smaller", "    - **Pros:** Smaller"), "TODO-43: inconsistent indentation for field 'Pros'"),
    ],
)
def test_data_errors_name_the_todo_and_field(mutation, message):
    broken = FIXTURE.replace(*mutation)
    assert broken != FIXTURE
    with pytest.raises(MigrationDataError, match=re.escape(message)):
        extract_fields(_entry("TODO-43", broken), broken)


def test_phase_alias_maps_to_form_option():
    aliased = FIXTURE.replace("Phase `2 (Design)`", "Phase `2 (Autoplan)`")
    fields = extract_fields(_entry("TODO-43", aliased), aliased)
    assert gi.parse_issue_body(fields.body)["Phase"] == ("2 (Design)",)
    assert "phase:2-design" in fields.labels


@pytest.mark.skipif(not FORM_PATH.exists(), reason="issue form not present")
def test_phase_options_mirror_the_issue_form():
    body = yaml.safe_load(FORM_PATH.read_text(encoding="utf-8"))["body"]
    (phase,) = [item for item in body if item.get("attributes", {}).get("label") == "Phase"]
    assert tuple(phase["attributes"]["options"]) == PHASE_OPTIONS
    assert set(PHASE_OPTION_ALIASES.values()) <= set(PHASE_OPTIONS)


def test_dependency_classes_done_not_selected_unknown():
    items = [extract_fields(_entry(i), FIXTURE) for i in ("TODO-43", "TODO-50")]
    edges, outside = compute_dependency_edges(
        items, done_ids=frozenset({"TODO-36", "TODO-2"}), active_ids=frozenset({"TODO-43", "TODO-50"})
    )
    assert edges == [("TODO-50", "TODO-43")]
    assert outside == [("TODO-50", "TODO-36", "done"), ("TODO-50", "TODO-2", "done")]
    only_50 = [items[1]]
    _, outside = compute_dependency_edges(
        only_50, done_ids=frozenset({"TODO-36", "TODO-2"}), active_ids=frozenset({"TODO-43", "TODO-50"})
    )
    assert ("TODO-50", "TODO-43", "not selected in this run") in outside
    with pytest.raises(MigrationDataError, match="TODO-50: Depends on TODO-2, an unknown id"):
        compute_dependency_edges(only_50, done_ids=frozenset({"TODO-36"}), active_ids=frozenset({"TODO-43", "TODO-50"}))


# -- dry run ---------------------------------------------------------------


def test_dry_run_makes_zero_gh_calls_and_writes_nothing(fake_gh, tmp_path, capsys):
    _write_fixture(tmp_path)
    rc = main(_base_argv(tmp_path, "--dry-run"))
    assert rc == 0
    assert fake_gh.calls == []
    assert not (tmp_path / "docs").exists()
    out = capsys.readouterr().out
    assert GOLDEN_43_BODY in out
    assert "WARNING: docs/superpowers/plans/2026-08-20-todo-43-production-orchestration-hotspots.md: manifest todo_id must be rewritten from TODO-43" in out
    doc = out.split("# TODOS.md → GitHub Issues migration\n", 1)[1]
    assert "- Repository: acme/repo" in doc
    assert TODO_43_ROW.format(issue="(dry-run)") in doc
    assert TODO_50_ROW.format(issue="(dry-run)") in doc
    assert "| TODO-36 |" not in doc and "| TODO-51 |" not in doc
    not_imported = doc.split("## Not imported\n")[1].split("## Dependencies")[0]
    assert "- TODO-36: `[x]` done in TODOS.md (not yet archived)" in not_imported
    assert "- TODO-51: `[→]` in progress: re-add after migration" in not_imported
    assert "- TODO-52: `[~]` on hold: re-add with tpo:on-hold" in not_imported
    assert "- TODO-50 blocked by TODO-43" in doc
    assert "- TODO-50 → TODO-36: done" in doc and "- TODO-50 → TODO-2: done" in doc
    assert "## Plan manifests to rewrite" in doc
    assert (
        "- `docs/superpowers/plans/2026-08-20-todo-43-production-orchestration-hotspots.md`: "
        "set manifest `todo_id` to `TODO-<new#>` (currently `TODO-43`) — done in the runtime cutover commit"
    ) in doc
    assert "issues #16 and #21 carry legacy `TODO-N:` title prefixes" in doc


def test_only_filter_marks_excluded_dependency_as_not_selected(fake_gh, tmp_path, capsys):
    _write_fixture(tmp_path)
    assert main(_base_argv(tmp_path, "--dry-run", "--only", "TODO-50")) == 0
    out = capsys.readouterr().out
    assert "| TODO-50 |" in out and "| TODO-43 |" not in out
    assert "- TODO-50 → TODO-43: not selected in this run" in out


def test_only_with_unknown_or_done_id_is_a_usage_error(fake_gh, tmp_path, capsys):
    _write_fixture(tmp_path)
    assert main(_base_argv(tmp_path, "--dry-run", "--only", "TODO-43,TODO-36")) == 2
    assert "TODO-36" in capsys.readouterr().err
    assert main(_base_argv(tmp_path, "--dry-run", "--only", "TODO-999")) == 2
    assert fake_gh.calls == []


def test_unknown_dependency_without_archive_is_a_data_error(fake_gh, tmp_path, capsys):
    _write_fixture(tmp_path, archive=None)
    assert main(_base_argv(tmp_path, "--dry-run")) == 2
    assert "TODO-50: Depends on TODO-2, an unknown id" in capsys.readouterr().err


def test_missing_required_decision_is_a_data_error(fake_gh, tmp_path, capsys):
    _write_fixture(tmp_path, FIXTURE.replace("Effort `L`, ", ""))
    assert main(_base_argv(tmp_path, "--dry-run")) == 2
    err = capsys.readouterr().err
    assert "TODO-43" in err and "Effort" in err
    assert fake_gh.calls == []


def test_relative_paths_resolve_against_project_dir(fake_gh, tmp_path, capsys, monkeypatch):
    _write_fixture(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    rc = main(["--todos", "TODOS.md", "--project-dir", str(tmp_path), "--repo", REPO, "--dry-run", "--out", "docs/m.md"])
    assert rc == 0
    assert f"would go to {tmp_path / 'docs' / 'm.md'}" in capsys.readouterr().out


# -- live-run pre-flight ----------------------------------------------------


def test_non_dry_run_requires_repo_or_origin(fake_gh, tmp_path, capsys):
    _write_fixture(tmp_path)
    fake_gh.on("git", "remote", "get-url", "origin", rc=128, stderr="fatal: no origin")
    argv = _base_argv(tmp_path)
    del argv[argv.index("--repo"): argv.index("--repo") + 2]
    assert main(argv) == 2
    assert "repo" in capsys.readouterr().err.lower()
    assert fake_gh.gh_calls() == []


def test_repo_mismatch_is_refused_unless_allowed(fake_gh, tmp_path, capsys):
    _write_fixture(tmp_path)
    _wire_live(fake_gh, {"TODO-43": 1, "TODO-50": 2}, origin="https://github.com/other/repo.git")
    fake_gh.on("gh", "api", *ACCEPT, f"repos/{REPO}/issues/1", stdout=json.dumps(issue_payload(1, id=9001)))
    fake_gh.on("gh", "api", *ACCEPT, "--method", "POST")
    assert main(_base_argv(tmp_path)) == 2
    err = capsys.readouterr().err
    assert "repo mismatch" in err and "--allow-repo-mismatch" in err
    assert fake_gh.gh_calls() == []
    assert main(_base_argv(tmp_path, "--allow-repo-mismatch")) == 0
    assert "differs from origin other/repo" in capsys.readouterr().err


def test_origin_unresolvable_with_repo_warns_and_proceeds(fake_gh, tmp_path, capsys):
    _write_fixture(tmp_path)
    _wire_live(fake_gh, {"TODO-43": 1, "TODO-50": 2})
    fake_gh.on("git", "remote", "get-url", "origin", rc=128, stderr="fatal: no origin")
    fake_gh.on("gh", "api", *ACCEPT, f"repos/{REPO}/issues/1", stdout=json.dumps(issue_payload(1, id=9001)))
    fake_gh.on("gh", "api", *ACCEPT, "--method", "POST")
    assert main(_base_argv(tmp_path)) == 0
    assert "warning: origin unresolvable" in capsys.readouterr().err


def test_auth_failure_stops_before_any_label_write(fake_gh, tmp_path, capsys):
    _write_fixture(tmp_path)
    _wire_live(fake_gh, {})
    fake_gh.on(*AUTH_OK, rc=1, stderr="You are not logged into any GitHub hosts. Run gh auth login")
    assert main(_base_argv(tmp_path)) == 1
    assert [c[:2] for c in fake_gh.gh_calls()] == [["auth", "status"]]
    assert "gh_auth" in capsys.readouterr().err
    assert TODO_43_ROW.format(issue="(pending)") in _doc(tmp_path)


def test_unwritable_out_is_a_usage_error_before_any_gh_call(fake_gh, tmp_path, capsys):
    _write_fixture(tmp_path)
    _wire_live(fake_gh, {"TODO-43": 1, "TODO-50": 2})
    (tmp_path / "docs").write_text("a regular file", encoding="utf-8")
    assert main(_base_argv(tmp_path)) == 2
    assert "not writable" in capsys.readouterr().err
    assert fake_gh.gh_calls() == []


# -- live path through fake gh --------------------------------------------


def test_live_run_argv_sequence_and_doc(fake_gh, tmp_path, capsys):
    _write_fixture(tmp_path)
    _wire_live(fake_gh, {"TODO-43": 101, "TODO-50": 102})
    fake_gh.on("gh", "api", *ACCEPT, f"repos/{REPO}/issues/101", stdout=json.dumps(issue_payload(101, id=9101)))
    fake_gh.on("gh", "api", *ACCEPT, "--method", "POST")

    rc = main(_base_argv(tmp_path))

    assert rc == 0
    calls = fake_gh.gh_calls()
    assert calls[0][:2] == ["auth", "status"]
    assert calls[1][:2] == ["label", "list"]
    creates = [c for c in calls if c[:2] == ["label", "create"]]
    assert {c[-1] for c in creates} == {"phase:2-design", "phase:4-development", "legacy-id:TODO-43", "legacy-id:TODO-50"}
    rest = calls[len(creates) + 2:]
    assert rest[0] == ["api", *ACCEPT, "--paginate", "--slurp", LEGACY_43]
    assert rest[1][:2] == ["issue", "create"]
    assert rest[1][rest[1].index("--title") + 1] == "Refactor production orchestration hotspots"
    labels = [rest[1][i + 1] for i, a in enumerate(rest[1]) if a == "--label"]
    assert set(labels) == GOLDEN_43_LABELS
    assert rest[2][-1] == LEGACY_50
    assert rest[3][:2] == ["issue", "create"]
    # dependency edge: TODO-50 (#102) blocked by TODO-43 (#101); TODO-36/TODO-2 not migrated -> no edge
    assert rest[4] == ["api", *ACCEPT, f"repos/{REPO}/issues/101"]
    assert rest[5] == [
        "api", *ACCEPT, "--method", "POST",
        f"repos/{REPO}/issues/102/dependencies/blocked_by", "-F", "issue_id=9101",
    ]
    assert len(rest) == 6
    _assert_no_shell(fake_gh)

    doc = _doc(tmp_path)
    assert TODO_43_ROW.format(issue="#101") in doc
    assert TODO_50_ROW.format(issue="#102") in doc
    assert "- #102 (TODO-50) blocked by #101 (TODO-43)" in doc
    assert "set manifest `todo_id` to `TODO-101` (currently `TODO-43`)" in doc
    out = capsys.readouterr().out
    assert "TODO-43 -> #101 created" in out and "TODO-50 -> #102 created" in out


def test_existing_legacy_issue_is_reused_without_create(fake_gh, tmp_path, capsys):
    _write_fixture(tmp_path)
    _wire_live(fake_gh, {"TODO-50": 102})
    fake_gh.on(
        "gh", "api", *ACCEPT, "--paginate", "--slurp", LEGACY_43,
        stdout=json.dumps([[issue_payload(77, labels=("tpo:todo", "legacy-id:TODO-43"))]]),
    )
    fake_gh.on("gh", "api", *ACCEPT, f"repos/{REPO}/issues/77", stdout=json.dumps(issue_payload(77, id=9077)))
    fake_gh.on("gh", "api", *ACCEPT, "--method", "POST")

    assert main(_base_argv(tmp_path)) == 0
    creates = [c for c in fake_gh.gh_calls() if c[:2] == ["issue", "create"]]
    assert len(creates) == 1 and "legacy-id:TODO-50" in creates[0]
    assert "TODO-43 -> #77 exists" in capsys.readouterr().out
    doc = _doc(tmp_path)
    assert "| TODO-43 | #77 |" in doc
    assert "- #102 (TODO-50) blocked by #77 (TODO-43)" in doc


def test_closed_legacy_issue_is_reused_but_not_a_blocker(fake_gh, tmp_path, capsys):
    _write_fixture(tmp_path)
    _wire_live(fake_gh, {"TODO-50": 102})
    fake_gh.on(
        "gh", "api", *ACCEPT, "--paginate", "--slurp", LEGACY_43,
        stdout=json.dumps([[issue_payload(77, state="closed", labels=("tpo:todo", "legacy-id:TODO-43"))]]),
    )
    assert main(_base_argv(tmp_path)) == 0
    assert not any("--method" in c for c in fake_gh.gh_calls())
    captured = capsys.readouterr()
    assert "TODO-43 -> #77 exists (closed)" in captured.out
    assert "warning: #102 (TODO-50) not blocked by #77 (TODO-43) (blocker issue is closed)" in captured.err
    doc = _doc(tmp_path)
    assert "| TODO-43 | #77 (closed) |" in doc
    assert "Edges skipped (blocker issue is closed):\n\n- #102 (TODO-50) not blocked by #77 (TODO-43)" in doc


def test_duplicate_legacy_issues_stop_with_rc_1_before_creating(fake_gh, tmp_path, capsys):
    _write_fixture(tmp_path)
    _wire_live(fake_gh, {"TODO-43": 101, "TODO-50": 102})
    fake_gh.on(
        "gh", "api", *ACCEPT, "--paginate", "--slurp", LEGACY_43,
        stdout=json.dumps([[issue_payload(7), issue_payload(8)]]),
    )
    assert main(_base_argv(tmp_path)) == 1
    assert not any(c[:2] == ["issue", "create"] for c in fake_gh.gh_calls())
    err = capsys.readouterr().err
    assert "TODO-43: 2 issues carry legacy-id:TODO-43 (#7, #8)" in err
    assert TODO_43_ROW.format(issue="(pending)") in _doc(tmp_path)


def test_partial_failure_writes_pending_doc_and_rc_1(fake_gh, tmp_path, capsys):
    _write_fixture(tmp_path)
    _wire_live(fake_gh, {"TODO-43": 101})

    def create(argv):
        if "legacy-id:TODO-50" in argv:
            return 1, "", "HTTP 401: not logged in"
        return 0, f"https://github.com/{REPO}/issues/101\n", ""

    fake_gh.on("gh", "issue", "create", handler=create)
    assert main(_base_argv(tmp_path)) == 1
    captured = capsys.readouterr()
    assert "TODO-43 -> #101 created" in captured.out
    assert "gh_auth" in captured.err and "TODO-50" in captured.err
    assert not any("--method" in c for c in fake_gh.gh_calls())
    _assert_no_shell(fake_gh)
    doc = _doc(tmp_path)
    assert TODO_43_ROW.format(issue="#101") in doc
    assert TODO_50_ROW.format(issue="(pending)") in doc


def test_rerun_merges_existing_doc_rows_and_never_regresses_numbers(fake_gh, tmp_path, capsys):
    _write_fixture(tmp_path)
    _wire_live(fake_gh, {"TODO-43": 101, "TODO-50": 102})
    fake_gh.on("gh", "api", *ACCEPT, f"repos/{REPO}/issues/101", stdout=json.dumps(issue_payload(101, id=9101)))
    fake_gh.on("gh", "api", *ACCEPT, "--method", "POST")
    assert main(_base_argv(tmp_path)) == 0

    # Second run migrates only TODO-50 and fails before assigning it a number.
    fake_gh.on("gh", "api", *ACCEPT, "--paginate", "--slurp", LEGACY_50, rc=1, stderr="HTTP 500")
    assert main(_base_argv(tmp_path, "--only", "TODO-50")) == 1
    doc = _doc(tmp_path)
    assert TODO_43_ROW.format(issue="#101") in doc, "row for an id outside this run is preserved"
    assert TODO_50_ROW.format(issue="#102") in doc, "a real number is never overwritten by (pending)"
    assert "(pending)" not in doc


# -- against the real TODOS.md --------------------------------------------


@pytest.mark.skipif(not (REPO_ROOT / "TODOS.md").exists(), reason="TODOS.md already removed")
def test_real_todos_md_parses_seven_active_entries_with_no_edges():
    text = (REPO_ROOT / "TODOS.md").read_text(encoding="utf-8")
    entries = parse_todo_entries(text)
    active = [e for e in entries if e.status == " "]
    assert [e.todo_id for e in active] == [
        "TODO-4", "TODO-5", "TODO-23", "TODO-28", "TODO-39", "TODO-42", "TODO-43",
    ]
    extracted = [extract_fields(entry, text) for entry in active]
    for fields in extracted:
        assert len(fields.body) <= gi.MAX_ISSUE_BODY_CHARS
        assert gi.parse_issue_body(fields.body)["Legacy ID"] == (fields.todo_id,)
    done = frozenset(e.todo_id for e in entries if e.status == "x")
    archive = REPO_ROOT / "TODOS-archive.md"
    if archive.exists():
        done |= frozenset(e.todo_id for e in parse_todo_entries(archive.read_text(encoding="utf-8")))
    edges, outside = compute_dependency_edges(
        extracted, done_ids=done, active_ids=frozenset(e.todo_id for e in active)
    )
    assert edges == []
    assert {c for c, _, _ in outside} == {"TODO-4", "TODO-5", "TODO-23", "TODO-28", "TODO-42"}
    assert {cls for _, _, cls in outside} == {"done"}


def test_unreadable_existing_doc_is_a_usage_error(fake_gh, tmp_path, capsys):
    _write_fixture(tmp_path)
    _wire_live(fake_gh, {"TODO-43": 1, "TODO-50": 2})
    out = tmp_path / "docs" / "migration" / "todos-to-issues.md"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"\xff\xfe not utf-8")
    assert main(_base_argv(tmp_path)) == 2
    assert "cannot read existing mapping document" in capsys.readouterr().err
    assert fake_gh.gh_calls() == []
    assert out.read_bytes() == b"\xff\xfe not utf-8"


def test_writability_probe_leaves_no_file_when_run_fails_before_writing(fake_gh, tmp_path, capsys):
    _write_fixture(tmp_path)
    fake_gh.on("git", "remote", "get-url", "origin", stdout=f"git@github.com:{REPO}.git\n")
    fake_gh.on(*AUTH_OK, raises=FileNotFoundError("gh"))
    out = tmp_path / "docs" / "migration" / "todos-to-issues.md"
    # gh_missing is a GitHubIssuesError, so the doc is still written on that path;
    # probe hygiene is observable by monkeypatching the writer away instead.
    import scripts.migrate_todos_to_issues as mod

    def boom(*args, **kwargs):
        raise KeyboardInterrupt

    original = mod._migrate
    mod._migrate = boom
    try:
        with pytest.raises(KeyboardInterrupt):
            main(_base_argv(tmp_path))
    finally:
        mod._migrate = original
    assert out.parent.is_dir(), "created parent directories are kept"
    assert not out.exists(), "probe must not leave an empty mapping document"
