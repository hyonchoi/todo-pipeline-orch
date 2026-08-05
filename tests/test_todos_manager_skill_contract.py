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
