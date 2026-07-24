"""Tests for the tpo skills install subcommand and bundled skill data."""
from __future__ import annotations

from importlib.resources import files


def test_todos_manager_skill_is_packaged_data():
    """SKILL.md and sections/ are importable via importlib.resources."""
    data_root = files("hermes_pipeline.data")
    skill_md = data_root.joinpath("skills", "todos-manager", "SKILL.md")
    assert skill_md.is_file()
    sections_dir = data_root.joinpath("skills", "todos-manager", "sections")
    section_names = {p.name for p in sections_dir.iterdir()}
    assert "schema.md" in section_names
    assert "id-assignment.md" in section_names
