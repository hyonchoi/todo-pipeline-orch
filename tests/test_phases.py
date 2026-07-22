from pathlib import Path

import pytest

from hermes_pipeline.contract import ContractSchemaError
from hermes_pipeline.phases import load_phases, resolve_profile_phases_path

FIXTURE = """
phases:
  - phase_key: "phase_2_autoplan"
    name: "Phase 2: Autoplan"
    prompt: "do autoplan"
    tools: "Read,Write,Bash"
    turns: 20
    timeout: 1800
  - phase_key: "phase_8_finish"
    name: "Phase 8: Finish Branch"
    prompt: "finish branch"
    tools: "Read,Write,Bash"
    turns: 15
"""

def test_load_phases_from_yaml(tmp_path):
    p = tmp_path / "phases.yaml"
    p.write_text(FIXTURE)
    phases = load_phases(p)
    assert len(phases) == 2
    assert phases[0].phase_key == "phase_2_autoplan"
    assert phases[0].name == "Phase 2: Autoplan"
    assert phases[0].turns == 20
    assert phases[1].timeout == 1800  # default

def test_gate_phase_needs_no_llm_fields(tmp_path):
    p = tmp_path / "phases.yaml"
    p.write_text(
        """
phases:
  - phase_key: "phase_9_ship"
    name: "Phase 9: Ship Gate"
    gate: true
    terminal: true
"""
    )
    phases = load_phases(p)
    assert len(phases) == 1
    gate = phases[0]
    assert gate.gate is True
    assert gate.terminal is True
    assert gate.prompt == ""
    assert gate.tools == ""
    assert gate.turns == 0

def test_non_gate_phase_defaults_gate_false(tmp_path):
    p = tmp_path / "phases.yaml"
    p.write_text(FIXTURE)
    phases = load_phases(p)
    assert phases[0].gate is False

def test_real_phases_yaml_ends_with_blocked_gate():
    phases = load_phases()  # default: configs/phases.yaml
    keys = [p.phase_key for p in phases]
    assert keys[-1] == "phase_9_ship"
    gate = phases[-1]
    assert gate.gate is True
    # Phase 8 must no longer be terminal — the gate replaces the
    # ready-for-review handoff.
    phase_8 = next(p for p in phases if p.phase_key == "phase_8_finish_branch")
    assert phase_8.terminal is False


def test_real_phases_yaml_has_review_phase_between_dev_and_cso():
    phases = load_phases()  # default: configs/phases.yaml
    keys = [p.phase_key for p in phases]
    assert "phase_5_review" in keys, keys
    dev_i = keys.index("phase_4_development")
    rev_i = keys.index("phase_5_review")
    cso_i = keys.index("phase_6_1_cso")
    assert dev_i < rev_i < cso_i, keys


def test_real_phases_yaml_review_phase_fields():
    phases = {p.phase_key: p for p in load_phases()}
    rev = phases["phase_5_review"]
    assert rev.tools == "Read,Edit,Bash"
    assert rev.turns == 30
    assert rev.timeout == 2400
    assert rev.terminal is False
    assert rev.gate is False
    # Prompt is instruction-only: it must NOT carry rollback control flow.
    assert "reset --hard" not in rev.prompt


def test_real_phases_yaml_order_unchanged_for_existing_phases():
    keys = [p.phase_key for p in load_phases()]
    assert keys == [
        "phase_2_autoplan",
        "phase_3_writing_plan",
        "phase_4_development",
        "phase_5_review",
        "phase_6_1_cso",
        "phase_6_2_qa",
        "phase_7_document_release",
        "phase_8_finish_branch",
        "phase_9_ship",
    ]


def test_resolve_profile_phases_path_gstack():
    path = resolve_profile_phases_path("gstack")
    assert path.name == "phases.yaml"
    assert "gstack" in str(path)
    assert path.is_file()


def test_resolve_profile_phases_path_unknown_raises_with_available_profiles():
    with pytest.raises(ContractSchemaError, match="gstack"):
        resolve_profile_phases_path("bogus-profile")


def test_load_phases_no_args_still_returns_gstack_phases():
    phases = load_phases()
    assert phases[0].phase_key == "phase_2_autoplan"


def test_real_phases_yaml_finish_branch_uses_ship_skill():
    phases = {p.phase_key: p for p in load_phases()}
    finish = phases["phase_8_finish_branch"]
    assert "/ship" in finish.prompt
    assert "finishing-a-development-branch" not in finish.prompt


AGENT_SKILLS_PHASE_ORDER = [
    "phase_1_spec",
    "phase_1b_spec_gate",
    "phase_2_plan",
    "phase_3_implement",
    "phase_4_review",
    "phase_5_security",
    "phase_6_document_release",
    "phase_7_ship",
    "phase_8_ship",
]


def test_agent_skills_phases_yaml_order():
    phases = load_phases(resolve_profile_phases_path("agent-skills"))
    assert [p.phase_key for p in phases] == AGENT_SKILLS_PHASE_ORDER


def test_agent_skills_phases_yaml_gates():
    phases = {p.phase_key: p for p in load_phases(resolve_profile_phases_path("agent-skills"))}
    assert phases["phase_1b_spec_gate"].gate is True
    assert phases["phase_1b_spec_gate"].terminal is False
    assert phases["phase_8_ship"].gate is True
    assert phases["phase_8_ship"].terminal is True


def test_agent_skills_phases_yaml_non_gate_phases_reference_skills():
    phases = {p.phase_key: p for p in load_phases(resolve_profile_phases_path("agent-skills"))}
    assert "agent-skills:spec-driven-development" in phases["phase_1_spec"].prompt
    assert "agent-skills:planning-and-task-breakdown" in phases["phase_2_plan"].prompt
    assert "agent-skills:incremental-implementation" in phases["phase_3_implement"].prompt
    assert "agent-skills:test-driven-development" in phases["phase_3_implement"].prompt
    assert "agent-skills:code-review-and-quality" in phases["phase_4_review"].prompt
    assert "agent-skills:security-and-hardening" in phases["phase_5_security"].prompt
    assert "agent-skills:ship" in phases["phase_7_ship"].prompt



def test_render_phase_prompt_no_spec_reference_unchanged():
    """Regression guard: omitting spec/reference kwargs must produce
    byte-identical output to pre-TODO-25 behavior."""
    from hermes_pipeline import phases as phases_mod
    out = phases_mod._render_phase_prompt(
        "do thing", todo_id="TODO-7", tick_id="01JT", project_slug="demo",
    )
    assert "Spec (authoritative):" not in out
    assert "Reference material:" not in out
    assert out == (
        "Pipeline context:\n"
        "- todo_id: TODO-7\n"
        "- tick_id: 01JT\n"
        "- project_slug: demo\n"
        "Work on TODO-7 ONLY. Do not pick a different TODO.\n\n"
        "do thing"
    )


def test_render_phase_prompt_both_spec_and_reference():
    from hermes_pipeline import phases as phases_mod
    out = phases_mod._render_phase_prompt(
        "do thing", todo_id="TODO-25", tick_id="01JT", project_slug="demo",
        spec_path="docs/pipeline/TODO-25-spec.md",
        reference_paths=["docs/notes/a.md", "docs/notes/b.md"],
    )
    assert "Spec (authoritative): docs/pipeline/TODO-25-spec.md\n" in out
    assert "Reference material: docs/notes/a.md, docs/notes/b.md\n" in out


def test_render_phase_prompt_only_spec():
    from hermes_pipeline import phases as phases_mod
    out = phases_mod._render_phase_prompt(
        "do thing", todo_id="TODO-25", tick_id="01JT", project_slug="demo",
        spec_path="docs/pipeline/TODO-25-spec.md",
    )
    assert "Spec (authoritative): docs/pipeline/TODO-25-spec.md\n" in out
    assert "Reference material:" not in out


def test_render_phase_prompt_only_reference():
    from hermes_pipeline import phases as phases_mod
    out = phases_mod._render_phase_prompt(
        "do thing", todo_id="TODO-25", tick_id="01JT", project_slug="demo",
        reference_paths=["docs/notes/a.md"],
    )
    assert "Spec (authoritative):" not in out
    assert "Reference material: docs/notes/a.md\n" in out


def test_render_phase_prompt_empty_reference_list_omitted():
    from hermes_pipeline import phases as phases_mod
    out = phases_mod._render_phase_prompt(
        "do thing", todo_id="TODO-25", tick_id="01JT", project_slug="demo",
        reference_paths=[],
    )
    assert "Reference material:" not in out
