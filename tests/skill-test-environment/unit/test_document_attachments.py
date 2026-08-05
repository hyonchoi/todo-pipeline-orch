"""Executable behavior matrix for todos-manager document attachments."""

from pathlib import Path

import pytest

from tests.skill_test_environment import skill_logic
from tests.skill_test_environment.skill_logic import (
    AttachmentCandidate,
    AttachmentSelection,
    AttachmentValidationError,
    AttachmentWorkflow,
    apply_attachment_selection_to_todo,
    audit_attachment_fields,
    audit_todo_markdown,
    classify_attachment_document,
    discover_attachment_candidates,
    load_attachment_policy,
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
        validation="valid",
    )


def test_packaged_markdown_policy_drives_the_harness():
    policy = load_attachment_policy()

    assert policy["version"] == 1
    assert policy["candidate_limit"] == 5
    assert policy["confirmation"] == {
        "zero": "none",
        "one": "explicit-selection",
        "multiple": "explicit-selection",
    }
    assert policy["sources"] == ["explicit", "git changed or untracked", "bounded search"]
    assert policy["relevance"] == ["explicit", "todo-id", "close-scope", "concrete-target-overlap"]


@pytest.mark.parametrize(
    ("candidates", "state", "selected_plan", "confirm_error"),
    [
        ((), "none detected", None, None),
        ((_candidate("docs/one.md", "Plan"),), "suggested", None, "Plan requires explicit selection"),
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


@pytest.mark.parametrize("role", ["Plan", "Spec", "Reference"])
@pytest.mark.parametrize(
    ("candidate_count", "policy_key", "policy_action", "should_require_selection"),
    [
        (0, "zero", "explicit-selection", True),
        (1, "one", "none", False),
        (2, "multiple", "none", False),
    ],
    ids=["zero", "one", "multiple"],
)
def test_confirmation_policy_controls_every_role_cardinality(
    tmp_path,
    monkeypatch,
    role,
    candidate_count,
    policy_key,
    policy_action,
    should_require_selection,
):
    confirmation = {
        "zero": "none",
        "one": "explicit-selection",
        "multiple": "explicit-selection",
    }
    confirmation[policy_key] = policy_action
    monkeypatch.setitem(skill_logic.ATTACHMENT_POLICY, "confirmation", confirmation)
    candidates = tuple(
        _candidate(f"docs/{role.lower()}-{number}.md", role)
        for number in range(candidate_count)
    )
    workflow = AttachmentWorkflow(tmp_path, command="add", candidates=candidates)
    for other_role in {"Plan", "Spec", "Reference"} - {role}:
        workflow.choose_none(other_role)

    if should_require_selection:
        with pytest.raises(ValueError, match=role):
            workflow.confirm()
    else:
        assert workflow.confirm() == AttachmentSelection()


def test_confirmation_policy_controls_combined_role_choice(tmp_path, monkeypatch):
    monkeypatch.setitem(
        skill_logic.ATTACHMENT_POLICY,
        "confirmation",
        {"zero": "none", "one": "none", "multiple": "explicit-selection"},
    )
    workflow = AttachmentWorkflow(
        tmp_path,
        command="add",
        candidates=(_candidate("docs/combined.md", "Plan", "Spec"),),
    )
    workflow.choose_none("Reference")

    assert workflow.confirm() == AttachmentSelection()


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


def test_preview_approval_mutates_actual_todo_markdown_only_after_approval(tmp_path):
    _write(tmp_path, "docs/plan.md")
    todos = tmp_path / "TODOS.md"
    todos.write_text(
        "# TODOS\n\n## Metadata\n\nNEXT_TODO_ID: 2\n\n## Entry Schema\n\n"
        "schema\n\n## Entries\n\n- [ ] **TODO-1: Example task** — Example summary\n"
        "  - **What:** Do the work\n  - **Why:** It matters enough\n"
        "  - **Decisions:** Priority `P2`\n",
        encoding="utf-8",
    )
    before = todos.read_bytes()
    selection = AttachmentSelection(plan="docs/plan.md")

    assert apply_attachment_selection_to_todo(todos, "TODO-1", selection, approved=False) is False
    assert todos.read_bytes() == before
    assert apply_attachment_selection_to_todo(todos, "TODO-1", selection, approved=True) is True
    assert "  - **Plan:** docs/plan.md" in todos.read_text(encoding="utf-8")


def test_workflow_finish_owns_real_todo_mutation_and_cancellation(tmp_path):
    _write(tmp_path, "docs/plan.md")
    todos = tmp_path / "TODOS.md"
    todos.write_text("## Entries\n\n- [ ] **TODO-1: One** — Summary here\n  - **What:** x\n", encoding="utf-8")
    before = todos.read_bytes()
    workflow = AttachmentWorkflow(tmp_path, command="revise", todos_path=todos, todo_id="TODO-1")
    workflow.select_manual("Plan", "docs/plan.md")
    workflow.confirm()

    assert workflow.finish(approved=False) is False
    assert todos.read_bytes() == before
    assert workflow.finish(approved=True) is True
    assert "**Plan:** docs/plan.md" in todos.read_text(encoding="utf-8")


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


def test_lone_reference_suggestion_requires_explicit_selection(tmp_path):
    _write(tmp_path, "docs/context.md")
    workflow = AttachmentWorkflow(tmp_path, command="add", candidates=(_candidate("docs/context.md", "Reference"),))
    with pytest.raises(ValueError, match="Reference requires explicit selection"):
        workflow.confirm()
    workflow.select_candidate("Reference", 1)
    assert workflow.confirm().references == ("docs/context.md",)


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
    assert workflow.role_state("Reference") == "selected"


def test_unchanged_existing_references_report_preserved(tmp_path):
    _write(tmp_path, "docs/context.md")
    workflow = AttachmentWorkflow(tmp_path, command="revise", existing=AttachmentSelection(references=("docs/context.md",)))
    assert workflow.role_state("Reference") == "preserved"


def test_choose_none_reference_reports_field_wide_removal_as_selected(tmp_path):
    for path in ("docs/adr/0001.md", "docs/context.md"):
        _write(tmp_path, path)
    workflow = AttachmentWorkflow(
        tmp_path,
        command="revise",
        existing=AttachmentSelection(
            references=("docs/adr/0001.md", "docs/context.md")
        ),
    )

    workflow.choose_none("Reference")

    assert workflow.selection.references == ()
    assert workflow.role_state("Reference") == "selected"
    assert workflow.confirm().references == ()


@pytest.mark.parametrize("operation", ["select", "replace", "combined"])
def test_plan_and_spec_selection_rejects_existing_reference_conflict(tmp_path, operation):
    _write(tmp_path, "docs/shared.md")
    workflow = AttachmentWorkflow(
        tmp_path,
        command="revise",
        existing=AttachmentSelection(references=("docs/shared.md",)),
        candidates=(_candidate("docs/shared.md", "Plan", "Spec"),),
    )

    with pytest.raises(ValueError, match="already present in Reference"):
        if operation == "select":
            workflow.select_candidate("Plan", 1)
        elif operation == "replace":
            workflow.replace("Plan", "docs/shared.md")
        else:
            workflow.attach_combined(1)


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


def test_rejected_combined_selection_preserves_prior_plan(tmp_path):
    for path in ("docs/prior.md", "docs/shared.md"):
        _write(tmp_path, path)
    workflow = AttachmentWorkflow(
        tmp_path,
        command="revise",
        existing=AttachmentSelection(plan="docs/prior.md", references=("docs/shared.md",)),
        candidates=(_candidate("docs/shared.md", "Plan", "Spec"),),
    )
    with pytest.raises(ValueError, match="already present in Reference"):
        workflow.attach_combined(1)
    assert workflow.selection.plan == "docs/prior.md"


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
        target_paths=("src/cache.py",),
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
        search_batches=(tuple(
            f"docs/superpowers/plans/TODO-40-{index}.md" for index in range(4)
        ),),
        todo_id="TODO-40",
        read_limit=9,
        search_limit=3,
        reads_used=7,
        searches_used=2,
    )

    assert result.reads == 9
    assert result.searches == 3
    assert result.exhausted is True
    assert result.skipped_source == "bounded search"


def test_generic_subject_substring_does_not_establish_strong_relevance(tmp_path):
    plan = "1. Change `src/other.py`.\n2. Verify with `uv run pytest`.\n"
    _write(tmp_path, "docs/superpowers/plans/cache.md", plan)

    result = discover_attachment_candidates(
        tmp_path,
        search_batches=(("docs/superpowers/plans/cache.md",),),
        subject_terms=("cache",),
        target_paths=("src/cache.py",),
    )

    assert result.candidates == ()


def test_generic_planning_overlap_does_not_establish_close_scope_relevance(tmp_path):
    _write(
        tmp_path,
        "docs/superpowers/plans/unrelated.md",
        "1. Change `src/other.py`.\n2. Verify tests for the implementation.\n",
    )

    result = discover_attachment_candidates(
        tmp_path,
        search_batches=(("docs/superpowers/plans/unrelated.md",),),
        title_summary="Change implementation and verify tests for unrelated migration",
    )

    assert result.candidates == ()


def test_close_title_summary_scope_is_strong_relevance(tmp_path):
    _write(
        tmp_path,
        "docs/superpowers/plans/cache-eviction.md",
        "1. Change `src/other.py`.\n2. Verify tests.\n",
    )
    result = discover_attachment_candidates(
        tmp_path,
        search_batches=(("docs/superpowers/plans/cache-eviction.md",),),
        title_summary="Cache eviction for bounded storage",
    )
    assert [candidate.path for candidate in result.candidates] == [
        "docs/superpowers/plans/cache-eviction.md"
    ]


@pytest.mark.parametrize(
    "close_scope_policy",
    [
        {"minimum_specific_term_overlap": 3, "generic_terms": []},
        {
            "minimum_specific_term_overlap": 2,
            "generic_terms": ["cache", "eviction"],
        },
    ],
    ids=["minimum-overlap", "generic-vocabulary"],
)
def test_close_scope_relevance_uses_structured_policy(
    tmp_path, monkeypatch, close_scope_policy
):
    _write(
        tmp_path,
        "docs/superpowers/plans/cache-eviction.md",
        "1. Change `src/other.py`.\n2. Verify tests.\n",
    )
    monkeypatch.setitem(
        skill_logic.ATTACHMENT_POLICY,
        "close_scope",
        close_scope_policy,
    )

    result = discover_attachment_candidates(
        tmp_path,
        search_batches=(("docs/superpowers/plans/cache-eviction.md",),),
        title_summary="Cache eviction for bounded storage",
    )

    assert result.candidates == ()


def test_search_accounting_counts_empty_and_repeated_result_invocations(tmp_path):
    _write(tmp_path, "docs/plan.md", "1. Change `src/cache.py`.\n2. Verify tests.\n")
    result = discover_attachment_candidates(
        tmp_path,
        search_batches=((), ("docs/plan.md",), ("docs/plan.md",)),
        target_paths=("src/cache.py",),
    )
    assert result.searches == 3
    assert result.reads == 2


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
        {"Reference": "docs/research, , notes.md, docs/missing.md"},
    )
    assert [(finding.stored_path, finding.defect) for finding in empty_item] == [
        ("", "contains an empty path between separators"),
        ("docs/missing.md", "does not exist"),
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
        ("Plan", "docs/missing.md", "does not exist"),
        ("Reference", "../outside.md", "resolves outside the repository"),
        (
            "Reference",
            "docs/also-missing.md",
            "does not exist",
        ),
    ]
    assert fields == snapshot


def test_audit_parses_real_todo_markdown_and_never_writes(tmp_path):
    _write(tmp_path, "docs/valid.md")
    todos = tmp_path / "TODOS.md"
    todos.write_text(
        "## Entries\n\n- [ ] **TODO-12: Audit** — Audit attachments\n"
        "  - **What:** x\n  - **Plan:** docs/missing.md\n"
        "  - **Spec:** docs/valid.md\n"
        "  - **Reference:** docs/valid.md, , docs/also-missing.md\n",
        encoding="utf-8",
    )
    before = todos.read_bytes()
    findings = audit_todo_markdown(tmp_path, todos)
    assert [(item.role, item.stored_path) for item in findings] == [
        ("Plan", "docs/missing.md"), ("Reference", ""), ("Reference", "docs/also-missing.md")
    ]
    assert todos.read_bytes() == before


def test_markdown_mutation_matches_exact_todo_id(tmp_path):
    todos = tmp_path / "TODOS.md"
    todos.write_text(
        "## Entries\n\n- [ ] **TODO-10: Ten** — Summary ten\n  - **What:** ten\n\n"
        "- [ ] **TODO-1: One** — Summary one\n  - **What:** one\n",
        encoding="utf-8",
    )
    apply_attachment_selection_to_todo(todos, "TODO-1", AttachmentSelection(plan="docs/one.md"), approved=True)
    text = todos.read_text(encoding="utf-8")
    assert text.index("**Plan:** docs/one.md") > text.index("TODO-1: One")


def test_markdown_mutation_uses_fresh_text_read_under_lock(tmp_path, monkeypatch):
    from tests.skill_test_environment import skill_logic

    todos = tmp_path / "TODOS.md"
    todos.write_text("## Entries\n\n- [ ] **TODO-1: One** — Summary\n  - **What:** old\n", encoding="utf-8")
    monkeypatch.setattr(
        skill_logic,
        "_before_todo_lock",
        lambda: todos.write_text(todos.read_text(encoding="utf-8").replace("old", "fresh"), encoding="utf-8"),
    )
    apply_attachment_selection_to_todo(todos, "TODO-1", AttachmentSelection(plan="docs/plan.md"), approved=True)
    assert "**What:** fresh" in todos.read_text(encoding="utf-8")


def test_legacy_entry_without_attachments_remains_valid(tmp_path):
    fields = {
        "What": "Keep the old entry valid",
        "Why": "Attachments are optional",
        "Decisions": "Priority `P2`",
    }

    assert audit_attachment_fields(tmp_path, "TODO-1", fields) == []
