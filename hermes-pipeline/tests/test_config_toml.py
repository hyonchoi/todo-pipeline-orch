from __future__ import annotations
from pathlib import Path
from hermes_pipeline.config import Config, load_toml_overlay

def _write(p: Path, body: str) -> Path:
    p.write_text(body)
    return p

def test_loads_selection_section(tmp_path):
    f = _write(tmp_path / "config.toml", """
[selection]
model = "claude-opus-4-7"
max_tokens = 4000
auto_execute = false
prompt_path = ".hermes/prompts/selection.md"
expected_prompt_sha = "abc123"

[circuit_breaker]
no_progress_threshold = 3
backoff_interval_min = 30
alert_dedup_hours = 24
""")
    cfg = load_toml_overlay(Config.default(), f)
    assert cfg.selection.model == "claude-opus-4-7"
    assert cfg.selection.auto_execute is False
    assert cfg.selection.expected_prompt_sha == "abc123"
    assert cfg.circuit_breaker.no_progress_threshold == 3

def test_missing_optional_fields_use_defaults(tmp_path):
    f = _write(tmp_path / "config.toml", '[selection]\nmodel = "claude-opus-4-7"\n')
    cfg = load_toml_overlay(Config.default(), f)
    assert cfg.selection.auto_execute is False           # default
    assert cfg.selection.expected_prompt_sha is None     # optional
    assert cfg.circuit_breaker.no_progress_threshold == 3  # default

def test_malformed_toml_raises_with_path(tmp_path):
    f = _write(tmp_path / "config.toml", "[selection\nmodel = ")
    import pytest
    with pytest.raises(ValueError) as ei:
        load_toml_overlay(Config.default(), f)
    assert str(f) in str(ei.value)
