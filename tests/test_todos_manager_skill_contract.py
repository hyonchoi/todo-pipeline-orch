import json
import re
from importlib.resources import files
from pathlib import Path

DATA = files("hermes_pipeline").joinpath("data", "skills", "todos-manager")
CANONICAL_ATTACHMENT_CONFIRMATION = (
    "Attachments may be proposed by `--add` or `--revise`, but require explicit "
    "user confirmation"
)


def skill_text(relative: str) -> str:
    return DATA.joinpath(*relative.split("/")).read_text(encoding="utf-8")


def attachment_policy() -> dict:
    text = skill_text("sections/document-attachments.md")
    match = re.search(
        r"```json todos-manager-attachment-policy\n(.*?)\n```", text, re.DOTALL
    )
    assert match is not None
    return json.loads(match.group(1))


def format_rules_block(text: str) -> str:
    marker = "> **Format rules (enforced by `todos-manager` skill):**"
    lines = text.splitlines()
    start = lines.index(marker)
    block = []
    for line in lines[start:]:
        if not line.startswith(">"):
            break
        block.append(line)
    return "\n".join(block)


def test_schema_defines_document_attachment_roles():
    schema = skill_text("sections/schema.md")
    assert "| **Plan:** |" in schema
    assert "execution authority" in schema
    assert "outcome contract" in schema
    assert "supplementary" in schema


def test_every_canonical_preamble_has_current_attachment_contract():
    canonical_preamble = format_rules_block(skill_text("sections/schema.md"))
    copies = {
        "ID assignment template": skill_text("sections/id-assignment.md"),
        "demo project": Path(
            "tests/skill-test-environment/demo-project/TODOS.md"
        ).read_text(encoding="utf-8"),
        "repository TODOs": Path("TODOS.md").read_text(encoding="utf-8"),
    }

    for name, text in copies.items():
        preamble = format_rules_block(text)
        assert preamble == canonical_preamble, name
        optional_fields = next(
            line for line in preamble.splitlines() if "Optional fields:" in line
        )
        assert optional_fields.index("**Plan:**") < optional_fields.index("**Spec:**"), name
        assert optional_fields.index("**Spec:**") < optional_fields.index(
            "**Reference:**"
        ), name
        assert "Spec:**/**Reference:** are `--revise`-only" not in preamble, name
        assert f"> - {CANONICAL_ATTACHMENT_CONFIRMATION}" in preamble, name


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


def test_plan_readiness_runs_after_ai_research_and_uses_packaged_cli_contract():
    skill = skill_text("SKILL.md")
    policy = skill_text("sections/document-attachments.md")
    auto_research = skill_text("sections/auto-research.md")

    assert "AI research remains authoritative for TODO field synthesis" in skill
    assert "after AI research and Plan selection" in policy
    assert (
        "`tpo plan validate <project> --todo TODO-N --plan <normalized-path>`"
        in policy
    )
    assert "validates before the candidate is persisted" in policy
    assert "Do not replace AI research" in auto_research


def test_plan_readiness_states_flow_through_synthesis_and_preview():
    auto_research = skill_text("sections/auto-research.md")
    policy = skill_text("sections/document-attachments.md")

    for state in ("manifest", "legacy", "invalid"):
        assert f"`{state}`" in policy
    assert "Plan readiness:" in auto_research
    assert "full-entry preview" in policy


def test_plan_readiness_distinguishes_intake_from_runtime_qualification():
    policy = skill_text("sections/document-attachments.md")
    assert "Hermes >= 0.19.0" in policy
    assert "installed project-skill parity" in policy
    assert "tracked at the pinned base commit" in policy
    assert "must not claim" in policy


def test_plan_authoring_policy_is_bounded_and_matches_the_oracle_contract():
    authoring = attachment_policy()["plan_authoring"]

    assert authoring == {
        "task_limit": 50,
        "task_id_format": "task-%02d",
        "evidence_locator_kinds": [
            "plan_lines",
            "repository_lines",
            "git_commit",
        ],
        "evidence_digest": "sha256",
        "candidate_mode": "0600",
        "existing_target_mode": "preserve",
        "new_target_mode": "0644",
        "validator": [
            "tpo",
            "plan",
            "validate",
            "<project>",
            "--todo",
            "TODO-N",
            "--plan",
            "<candidate>",
            "--require-manifest",
        ],
    }


def test_plan_authoring_requires_digest_checked_provenance_for_every_task_field():
    policy = skill_text("sections/document-attachments.md")
    policy = " ".join(policy.split())
    for phrase in (
        "out-of-band provenance map",
        "approved Plan lines",
        "repository-file lines",
        "exact Git commits",
        "SHA-256",
        "title, instructions, every acceptance criterion, every verification command, and commit message",
        "insufficient_evidence",
        "task-01",
        "task-50",
    ):
        assert phrase in policy


def test_plan_authoring_state_machine_orders_validation_and_both_approvals():
    policy = skill_text("sections/document-attachments.md")
    policy = policy.split("### Ordered authoring state machine", 1)[1]
    ordered = [
        "explicit approval of the human Plan snapshot",
        "Draft only evidence-supported manifest fields",
        "Stage the candidate",
        "--require-manifest",
        "Show the exact unified Plan diff",
        "Show the final TODO preview",
        "Recheck the Plan and TODO preimages",
        "Obtain final TODO approval",
        "atomically replace or create the Plan",
        "write the TODO",
    ]
    positions = [policy.index(phrase) for phrase in ordered]
    assert positions == sorted(positions)


def test_plan_authoring_preserves_bytes_paths_modes_and_single_target_scope():
    policy = skill_text("sections/document-attachments.md")
    policy = " ".join(policy.split())
    for phrase in (
        "one selected TODO and one selected Plan",
        "byte-for-byte no-op",
        "same-directory staged candidate",
        "mode `0600`",
        "preserve the existing Plan mode",
        "mode `0644`",
        "concurrently created target",
        "leave both paths byte-for-byte unchanged",
        "delete the staged candidate",
        "never bulk-mutate Plans or TODOs",
    ):
        assert phrase in policy


def test_new_plan_target_validation_is_distinct_from_existing_attachments():
    policy = " ".join(skill_text("sections/document-attachments.md").split())
    for phrase in (
        "Existing attachment candidates remain existing regular files only",
        "new Plan target reservation",
        "repository-relative and contained inside the resolved repository root",
        "existing, contained, non-symlink directory",
        "target is absent",
        "recheck that absence immediately before atomic creation",
        "concurrently created target",
    ):
        assert phrase in policy


def test_all_subcommands_define_manifest_compatibility_outcomes():
    skill = skill_text("SKILL.md")
    revise = " ".join(skill_text("sections/revise.md").split()).lower()
    listing = " ".join(skill_text("sections/list.md").split())

    assert "`--init` performs no Plan discovery, validation, authoring, or mutation" in skill
    assert "`none` remains non-actionable" in skill
    assert "attach it byte-for-byte unchanged" in skill
    assert "another manifest-ready Plan, `none`, or cancellation" in skill
    assert "reports each Plan as `manifest`, `legacy`, `invalid`, or `none`" in skill
    assert "does not edit any Plan" in skill
    assert "preserve every attached Plan byte-for-byte" in skill
    assert "preserves the attached Plan bytes" in skill

    for phrase in (
        "explicitly create, replace, remove, or convert one plan",
        "removing a plan attachment never deletes or edits its file",
        "preserve unchanged plan attachments byte-for-byte",
    ):
        assert phrase in revise
    assert "manifest`, `legacy`, `invalid`, or `none`" in listing
    assert "active and archived" in listing
    assert "| ID | Status | Title | Summary | Plan readiness |" in listing
    assert "| TODO-1 | Pending | Example title | One-line summary | manifest |" in listing
    assert "Apply the same columns" in listing
    assert "Report only — no TODO or Plan files modified" in listing


def test_public_howto_documents_plan_outcome_for_all_eight_subcommands():
    howto = Path("docs/howto-todos-manager.md").read_text(encoding="utf-8")
    outcomes = {
        "--init": "performs no Plan operation",
        "--add": "authors a validated manifest",
        "--convert": "reports Plan readiness and migration needs",
        "--audit": "reports `manifest`, `legacy`, `invalid`, or `none`",
        "--archive": "preserves attached Plan bytes",
        "--complete": "preserves attached Plan bytes",
        "--list": "reports Plan readiness for active and archived entries",
        "--revise": "create, replace, remove, or convert one Plan",
    }

    for subcommand, outcome in outcomes.items():
        assert f"**`{subcommand}`** — {outcome}" in howto


def test_public_docs_explain_manifest_authoring_and_legacy_migration_boundary():
    documents = {
        "how-to": Path("docs/howto-todos-manager.md").read_text(encoding="utf-8"),
        "architecture": Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8"),
    }

    for name, text in documents.items():
        assert "`json tpo-plan`" in text, name
        assert "--require-manifest" in text, name
        assert "legacy" in text, name
        assert "migration" in text, name


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


def test_auto_research_delegates_confirmation_authority_to_shared_policy():
    policy = skill_text("sections/document-attachments.md")
    auto_research = skill_text("sections/auto-research.md")
    assert '"one": "explicit-selection"' in policy
    assert "authoritative candidate-cardinality state machine" in auto_research

    contradictory_confirmation = re.compile(
        r"(?:plain\s+)?`confirm`\s+accepts?[^.\n]{0,80}(?:one|lone)\s+candidate",
        re.IGNORECASE,
    )
    for relative in (
        "SKILL.md",
        "sections/auto-research.md",
        "sections/document-attachments.md",
        "sections/revise.md",
        "sections/acceptance-scenarios.md",
    ):
        assert not contradictory_confirmation.search(skill_text(relative)), relative


def test_add_contract_covers_zero_one_and_multiple_plan_candidates():
    skill = skill_text("SKILL.md")
    scenarios = skill_text("sections/acceptance-scenarios.md")
    for phrase in ("Plan: none detected", "one candidate", "multiple candidates"):
        assert phrase in skill or phrase in scenarios
    assert "confirm" in scenarios
    assert "unresolved" in scenarios
    assert "none" in scenarios


def test_budget_exhaustion_scenario_resolves_plan_before_preview():
    scenarios = skill_text("sections/acceptance-scenarios.md")
    scenario = scenarios.split(
        "#### Scenario A1h: Budget exhaustion and cancellation", 1
    )[1].split("---", 1)[0]
    resolution = "explicitly resolves the Plan row"
    assert resolution in scenario
    assert scenario.index(resolution) < scenario.index("final preview")


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
