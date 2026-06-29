from pathlib import Path
from hermes_pipeline.phases import Phase, load_phases

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
