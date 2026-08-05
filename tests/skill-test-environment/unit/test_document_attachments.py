"""Executable behavior matrix for todos-manager document attachments."""

from pathlib import Path

import pytest

from tests.skill_test_environment.skill_logic import (
    AttachmentCandidate,
    AttachmentSelection,
    AttachmentValidationError,
    AttachmentWorkflow,
    audit_attachment_fields,
    classify_attachment_document,
    discover_attachment_candidates,
    parse_stored_references,
    validate_attachment_path,
)


def _write(repo: Path, relative: str, text: str = "supporting context") -> Path:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _candidate(path: str, *roles: str) -> AttachmentCandidate:
    return AttachmentCandidate(
        path=path,
        roles=roles,
        relevance_reason="explicit task context",
        source="explicit",
    )


@pytest.mark.parametrize(
    ("candidates", "state", "selected_plan", "confirm_error"),
    [
        ((), "none detected", None, None),
        ((_candidate("docs/one.md", "Plan"),), "suggested", "docs/one.md", None),
        (
            (
                _candidate("docs/one.md", "Plan"),
                _candidate("docs/two.md", "Plan"),
            ),
            "unresolved",
            None,
            "Plan is unresolved",
        ),
    ],
    ids=["zero", "one", "multiple"],
)
def test_add_candidate_cardinality_controls_confirmation(
    tmp_path, candidates, state, selected_plan, confirm_error
):
    for candidate in candidates:
        _write(tmp_path, candidate.path)
    workflow = AttachmentWorkflow(tmp_path, command="add", candidates=candidates)

    assert workflow.role_state("Plan") == state
    if confirm_error:
        with pytest.raises(ValueError, match=confirm_error):
            workflow.confirm()
        assert workflow.selection.plan is None
    else:
        assert workflow.confirm().plan == selected_plan


def test_add_supports_manual_and_omitted_attachments_without_early_write(tmp_path):
    _write(tmp_path, "docs/manual plan.md")
    writes = []
    manual = AttachmentWorkflow(tmp_path, command="add")
    manual.select_manual("Plan", "docs/manual plan.md")

    manual.confirm()
    assert writes == []
    assert manual.finish(approved=False, writer=writes.append) is False
    assert writes == []
    assert manual.finish(approved=True, writer=writes.append) is True
    assert writes == [AttachmentSelection(plan="docs/manual plan.md")]

    omitted = AttachmentWorkflow(tmp_path, command="add")
    omitted.choose_none("Plan")
    assert omitted.confirm() == AttachmentSelection()


def test_ambiguity_blocks_preview_until_one_candidate_is_selected(tmp_path):
    _write(tmp_path, "docs/one.md")
    _write(tmp_path, "docs/two.md")
    workflow = AttachmentWorkflow(
        tmp_path,
        command="add",
        candidates=(
            _candidate("docs/one.md", "Plan"),
            _candidate("docs/two.md", "Plan"),
        ),
    )

    with pytest.raises(ValueError, match="Plan is unresolved"):
        workflow.confirm()
    with pytest.raises(RuntimeError, match="confirmation"):
        workflow.finish(approved=True, writer=lambda _: None)

    workflow.select_candidate("Plan", 2)
    assert workflow.confirm().plan == "docs/two.md"


def test_revise_preserves_replaces_and_removes_singletons(tmp_path):
    for path in ("docs/old-plan.md", "docs/old-spec.md", "docs/new-plan.md"):
        _write(tmp_path, path)
    workflow = AttachmentWorkflow(
        tmp_path,
        command="revise",
        existing=AttachmentSelection(
            plan="docs/old-plan.md",
            spec="docs/old-spec.md",
        ),
        candidates=(_candidate("docs/new-plan.md", "Plan"),),
    )

    assert workflow.role_state("Plan") == "preserved"
    assert workflow.confirm() == AttachmentSelection(
        plan="docs/old-plan.md",
        spec="docs/old-spec.md",
    )

    workflow.replace("Plan", "docs/new-plan.md")
    workflow.remove("Spec")
    assert workflow.confirm() == AttachmentSelection(plan="docs/new-plan.md")


def test_revise_warns_about_invalid_existing_paths_without_blocking_other_edits(
    tmp_path,
):
    _write(tmp_path, "docs/old-plan.md")
    _write(tmp_path, "docs/new-plan.md")
    workflow = AttachmentWorkflow(
        tmp_path,
        command="revise",
        existing=AttachmentSelection(
            plan="docs/old-plan.md",
            spec="docs/missing-spec.md",
        ),
    )

    assert [(warning.role, warning.stored_path) for warning in workflow.warnings] == [
        ("Spec", "docs/missing-spec.md")
    ]
    workflow.replace("Plan", "docs/new-plan.md")

    assert workflow.confirm() == AttachmentSelection(
        plan="docs/new-plan.md",
        spec="docs/missing-spec.md",
    )


def test_revise_references_append_deduplicate_remove_and_exclude_roles(tmp_path):
    for path in (
        "docs/plan.md",
        "docs/spec.md",
        "docs/adr/0001.md",
        "docs/context.md",
        "docs/adr/0002.md",
    ):
        _write(tmp_path, path)
    workflow = AttachmentWorkflow(
        tmp_path,
        command="revise",
        existing=AttachmentSelection(
            plan="docs/plan.md",
            spec="docs/spec.md",
            references=("docs/adr/0001.md", "docs/context.md"),
        ),
    )

    workflow.append_reference("docs/context.md")
    workflow.append_reference("docs/adr/0002.md")
    with pytest.raises(ValueError, match="matches Plan or Spec"):
        workflow.append_reference("docs/plan.md")
    workflow.remove_reference("docs/context.md")

    assert workflow.confirm().references == (
        "docs/adr/0001.md",
        "docs/adr/0002.md",
    )


def test_combined_plan_and_spec_requires_explicit_combined_choice(tmp_path):
    _write(tmp_path, "docs/combined.md")
    workflow = AttachmentWorkflow(
        tmp_path,
        command="revise",
        candidates=(_candidate("docs/combined.md", "Plan", "Spec"),),
    )

    with pytest.raises(ValueError, match="combined Plan and Spec choice"):
        workflow.confirm()
    workflow.attach_combined(1)

    assert workflow.confirm() == AttachmentSelection(
        plan="docs/combined.md",
        spec="docs/combined.md",
    )


def test_invalid_manual_value_recovers_without_rediscovery(tmp_path):
    _write(tmp_path, "docs/valid.md")
    workflow = AttachmentWorkflow(tmp_path, command="add")

    with pytest.raises(AttachmentValidationError, match="repository-relative"):
        workflow.select_manual("Plan", "/tmp/outside.md")
    assert workflow.discovery_runs == 1
    assert workflow.selection == AttachmentSelection()

    workflow.select_manual("Plan", "docs/valid.md")
    assert workflow.discovery_runs == 1
    assert workflow.confirm().plan == "docs/valid.md"


def test_discovery_obeys_precedence_candidate_limit_and_exclusions(tmp_path):
    plan = "1. Change `src/cache.py`.\n2. Verify with `uv run pytest`.\n"
    paths = ["docs/explicit.md", "docs/git.md"] + [
        f"docs/superpowers/plans/search-{index}.md" for index in range(6)
    ]
    for path in paths:
        _write(tmp_path, path, plan)
    _write(tmp_path, "docs/archive/ignored.md", plan)

    result = discover_attachment_candidates(
        tmp_path,
        explicit_paths=("docs/explicit.md",),
        git_paths=("docs/git.md", "docs/archive/ignored.md"),
        search_paths=tuple(paths[2:]),
        subject_terms=("cache",),
    )

    assert [candidate.source for candidate in result.candidates] == [
        "explicit",
        "git changed or untracked",
        "bounded search",
        "bounded search",
        "bounded search",
    ]
    assert len(result.candidates) == 5
    assert result.skipped_source == "bounded search"
    assert all("archive" not in candidate.path for candidate in result.candidates)


def test_discovery_honors_shared_read_and_search_budgets(tmp_path):
    plan = "1. Change `src/cache.py`.\n2. Verify with `uv run pytest`.\n"
    for index in range(4):
        _write(tmp_path, f"docs/superpowers/plans/TODO-40-{index}.md", plan)

    result = discover_attachment_candidates(
        tmp_path,
        search_paths=tuple(
            f"docs/superpowers/plans/TODO-40-{index}.md" for index in range(4)
        ),
        todo_id="TODO-40",
        read_limit=2,
        search_limit=1,
    )

    assert result.reads == 1
    assert result.searches == 1
    assert result.exhausted is True
    assert result.skipped_source == "bounded search"


@pytest.mark.parametrize(
    ("relative", "text", "roles"),
    [
        (
            "docs/gstack/cache-plan.md",
            "Status: APPROVED\nImplementation steps for cache work.\n",
            ("Plan",),
        ),
        (
            "docs/superpowers/plans/cache.md",
            "# Cache implementation plan\n",
            ("Plan",),
        ),
        (
            "docs/other/cache.md",
            "1. Change `src/cache.py`.\n2. Verify with `uv run pytest`.\n",
            ("Plan",),
        ),
        (
            "docs/other/cache-spec.md",
            "## Outcome\nBound cache size.\n## Acceptance criteria\n- limit is enforced\n",
            ("Spec",),
        ),
        (
            "docs/other/combined.md",
            "## Outcome\nBound cache size.\n## Acceptance criteria\n- enforced\n"
            "1. Change `src/cache.py`.\n2. Verify with `uv run pytest`.\n",
            ("Plan", "Spec"),
        ),
    ],
    ids=["gstack", "superpowers", "fallback-plan", "spec", "combined"],
)
def test_recognized_and_fallback_document_formats(relative, text, roles):
    assert classify_attachment_document(relative, text) == roles


def test_path_validation_normalizes_inside_paths_and_rejects_escape(tmp_path):
    _write(tmp_path, "docs/plan.md")
    (tmp_path / "docs" / "plans").mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "docs" / "outside-link.md").symlink_to(outside)

    assert validate_attachment_path(tmp_path, "docs/../docs/plan.md") == "docs/plan.md"
    with pytest.raises(AttachmentValidationError, match="resolves outside"):
        validate_attachment_path(tmp_path, "../outside.md")
    with pytest.raises(AttachmentValidationError, match="regular file"):
        validate_attachment_path(tmp_path, "docs/plans")
    with pytest.raises(AttachmentValidationError, match="symlink"):
        validate_attachment_path(tmp_path, "docs/outside-link.md")


def test_reference_representation_has_no_literal_comma_escape(tmp_path):
    _write(tmp_path, "docs/research")
    _write(tmp_path, "notes.md")

    with pytest.raises(AttachmentValidationError, match="contains a comma"):
        validate_attachment_path(
            tmp_path,
            "docs/research,notes.md",
            reference_input=True,
        )

    assert parse_stored_references("docs/research,notes.md") == (
        "docs/research",
        "notes.md",
    )
    assert audit_attachment_fields(
        tmp_path,
        "TODO-12",
        {"Reference": "docs/research,notes.md"},
    ) == []
    empty_item = audit_attachment_fields(
        tmp_path,
        "TODO-12",
        {"Reference": "docs/research, , notes.md"},
    )
    assert [(finding.stored_path, finding.defect) for finding in empty_item] == [
        ("", "contains an empty path between separators")
    ]


def test_audit_validates_each_stored_reference_without_mutation(tmp_path):
    _write(tmp_path, "docs/valid.md")
    fields = {
        "Plan": "docs/missing.md",
        "Reference": "docs/valid.md, ../outside.md, docs/also-missing.md",
    }
    snapshot = fields.copy()

    findings = audit_attachment_fields(tmp_path, "TODO-12", fields)

    assert [(finding.role, finding.stored_path, finding.defect) for finding in findings] == [
        ("Plan", "docs/missing.md", "does not exist or is not a regular file"),
        ("Reference", "../outside.md", "resolves outside the repository"),
        (
            "Reference",
            "docs/also-missing.md",
            "does not exist or is not a regular file",
        ),
    ]
    assert fields == snapshot


def test_legacy_entry_without_attachments_remains_valid(tmp_path):
    fields = {
        "What": "Keep the old entry valid",
        "Why": "Attachments are optional",
        "Decisions": "Priority `P2`",
    }

    assert audit_attachment_fields(tmp_path, "TODO-1", fields) == []
