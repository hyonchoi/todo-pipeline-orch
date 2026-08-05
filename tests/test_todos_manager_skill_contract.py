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
