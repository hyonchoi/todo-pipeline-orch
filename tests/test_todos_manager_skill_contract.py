from importlib.resources import files
from pathlib import Path

import hermes_pipeline.harness as harness

DATA = files("hermes_pipeline").joinpath("data", "skills", "todos-manager")


def skill_text(relative: str) -> str:
    return DATA.joinpath(*relative.split("/")).read_text(encoding="utf-8")


def test_schema_defines_document_attachment_roles():
    schema = skill_text("sections/schema.md")
    assert "| **Plan:** |" in schema
    assert "execution authority" in schema
    assert "outcome contract" in schema
    assert "supplementary" in schema


def test_schema_copies_do_not_claim_spec_reference_are_revise_only():
    copies = [
        skill_text("sections/schema.md"),
        skill_text("sections/id-assignment.md"),
        Path("tests/skill-test-environment/demo-project/TODOS.md").read_text(),
        Path(harness.__file__).read_text(),
    ]
    for text in copies:
        assert "Spec:**/**Reference:** are `--revise`-only" not in text
        assert "**Plan:**" in text


def test_document_attachment_policy_is_shared_and_bounded():
    policy = skill_text("sections/document-attachments.md")
    assert "explicit" in policy
    assert "changed or untracked" in policy
    assert "five qualified candidates" in policy
    assert "20 file reads" in policy
    assert "10 searches" in policy
    assert "symlink" in policy
    assert "repository-relative POSIX" in policy


def test_both_workflows_route_to_shared_attachment_policy():
    skill = skill_text("SKILL.md")
    route = next(
        line
        for line in skill.splitlines()
        if "sections/document-attachments.md" in line
    )
    assert "--add" in route and "--revise" in route


def test_attachment_discovery_precedes_general_research():
    skill = skill_text("SKILL.md")
    assert "complete attachment discovery before general research" in skill
    assert "derive field drafts only after attachment discovery" in skill


def test_attachment_search_roots_are_validated_before_traversal():
    policy = skill_text("sections/document-attachments.md")
    assert "Before reading, listing, or searching any discovery root" in policy
    assert "Do not traverse a rejected root" in policy
    assert "existing directory inside the resolved repository root" in policy


def test_attachment_roles_flow_through_synthesis_and_preview():
    auto_research = skill_text("sections/auto-research.md")
    policy = skill_text("sections/document-attachments.md")
    assert (
        "Plan:            <none detected, suggested path and reason, or numbered unresolved choices>"
        in auto_research
    )
    assert "Spec:            <path and state>" in auto_research
    assert "Reference:       <paths and state>" in auto_research
    assert "existing synthesis confirmation" in policy
    assert "subsequent full-entry preview" in policy


def test_add_contract_covers_zero_one_and_multiple_plan_candidates():
    skill = skill_text("SKILL.md")
    scenarios = skill_text("sections/acceptance-scenarios.md")
    for phrase in ("Plan: none detected", "one candidate", "multiple candidates"):
        assert phrase in skill or phrase in scenarios
    assert "confirm" in scenarios
    assert "unresolved" in scenarios
    assert "none" in scenarios


def test_revise_always_runs_post_creation_attachment_discovery():
    revise = skill_text("sections/revise.md")
    assert "Always run document attachment discovery" in revise
    assert "ordinary fields have no gaps" in revise
    assert "continue to attachment discovery" in revise


def test_revise_contract_preserves_and_explicitly_mutates_attachments():
    revise = skill_text("sections/revise.md")
    required = [
        "Plan", "Spec", "Reference", "preserve", "replace", "remove",
        "append", "deduplicate", "combined Plan", "invalid existing",
    ]
    for phrase in required:
        assert phrase in revise


def test_revise_rejects_reference_append_matching_plan_or_spec():
    revise = skill_text("sections/revise.md")
    assert "Reject a `Reference: append <path>`" in revise
    assert (
        "normalized path matches the selected or existing Plan or Spec" in revise
    )


def test_audit_validates_attachments_without_mutating_them():
    skill = skill_text("SKILL.md")
    required = [
        "Validate every present attachment value",
        "one path-specific finding per defect",
        "never require, remove, replace, or repair attachments",
    ]
    for phrase in required:
        assert phrase in skill


def test_audit_errors_cover_each_representable_attachment_path_defect():
    errors = skill_text("sections/error-messages.md")
    normalized_errors = " ".join(errors.split())
    expected_examples = [
        "docs/missing-plan.md` does not exist",
        "docs/specs` is a directory, not a regular file",
        "../outside-plan.md` resolves outside the repository",
        "docs/external-spec.md` is a symlink that resolves outside the repository",
        "docs/one.md, , docs/two.md` contains an empty Reference item",
    ]
    for example in expected_examples:
        assert example in errors
    assert errors.count("Remediation: Choose an existing document file.") >= 2
    assert errors.count("Remediation: Choose a file inside the repository root.") >= 2
    assert "Every stored comma is a Reference separator" in normalized_errors
    assert "never infer a literal-comma path from stored text" in normalized_errors
    assert "docs/research,notes.md` contains a literal comma" not in errors


def test_acceptance_coverage_maps_the_authoritative_spec_boundary():
    scenarios = skill_text("sections/acceptance-scenarios.md")
    assert "## TODO-40 specification coverage" in scenarios
    table = scenarios.split("## TODO-40 specification coverage", 1)[1]
    rows = {}
    for line in table.splitlines():
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells[0] == "Requirement group":
            assert cells == ["Requirement group", "Executable coverage", "Mapping"]
            continue
        assert len(cells) == 3
        rows[cells[0]] = (cells[1], cells[2])

    expected = {
        "Role semantics and ownership": (
            "test_packaged_markdown_policy_drives_the_harness",
            "test_combined_plan_and_spec_requires_explicit_combined_choice",
        ),
        "Discovery order and exclusions": (
            "test_discovery_obeys_precedence_candidate_limit_and_exclusions",
        ),
        "Discovery budgets and exhaustion": (
            "test_discovery_honors_shared_read_and_search_budgets",
            "test_generic_subject_substring_does_not_establish_strong_relevance",
        ),
        "Qualification, relevance, and classification": (
            "test_recognized_and_fallback_document_formats",
        ),
        "Path normalization and validation": (
            "test_path_validation_normalizes_inside_paths_and_rejects_escape",
        ),
        "Attachment cardinality and Reference syntax": (
            "test_add_candidate_cardinality_controls_confirmation",
            "test_reference_representation_has_no_literal_comma_escape",
            "test_revise_references_append_deduplicate_remove_and_exclude_roles",
        ),
        "Validation recovery": (
            "test_invalid_manual_value_recovers_without_rediscovery",
        ),
        "Interaction and confirmation": (
            "test_ambiguity_blocks_preview_until_one_candidate_is_selected",
            "test_add_supports_manual_and_omitted_attachments_without_early_write",
            "test_preview_approval_mutates_actual_todo_markdown_only_after_approval",
        ),
        "`--add` candidate handling": (
            "test_add_candidate_cardinality_controls_confirmation",
            "test_add_supports_manual_and_omitted_attachments_without_early_write",
        ),
        "`--revise` attachment mutation": (
            "test_revise_preserves_replaces_and_removes_singletons",
            "test_revise_warns_about_invalid_existing_paths_without_blocking_other_edits",
            "test_revise_references_append_deduplicate_remove_and_exclude_roles",
        ),
        "Compatibility and non-mutating attachment audit": (
            "test_legacy_entry_without_attachments_remains_valid",
            "test_audit_validates_each_stored_reference_without_mutation",
        ),
        "Completion scenario matrix": (
            "test_add_candidate_cardinality_controls_confirmation",
            "test_discovery_honors_shared_read_and_search_budgets",
            "test_recognized_and_fallback_document_formats",
            "test_path_validation_normalizes_inside_paths_and_rejects_escape",
            "test_legacy_entry_without_attachments_remains_valid",
        ),
        "Packaged and installed parity": (
            "test_project_install_matches_packaged_skill_byte_for_byte",
        ),
    }
    assert set(rows) == set(expected)
    for requirement, test_names in expected.items():
        executable, mapping = rows[requirement]
        for test_name in test_names:
            assert test_name in executable
        assert mapping
    assert "TODO-39" in rows["Role semantics and ownership"][1]
    assert "outside this suite" in rows["Role semantics and ownership"][1]
