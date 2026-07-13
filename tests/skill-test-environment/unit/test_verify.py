"""Tests for the golden file verification module."""

import pytest
from tests.skill_test_environment.verify import load_golden, run_structural, assert_golden
from tests.skill_test_environment.skill_logic import (
    simulate_archive,
)


class TestLoadGolden:
    def test_loads_add_golden(self, skill_golden_dir):
        golden = load_golden(skill_golden_dir / "add_happy_path.yaml")
        assert golden["subcommand"] == "add"
        assert "assertions" in golden

    def test_loads_all_golden_files(self, skill_golden_dir):
        for yaml_file in skill_golden_dir.glob("*.yaml"):
            golden = load_golden(yaml_file)
            assert "subcommand" in golden
            assert "assertions" in golden


class TestRunStructural:
    def test_audit_golden_passes(self, skill_golden_dir, skill_demo_todos, skill_demo_archive):
        """Audit golden should pass against the demo fixture (all entries valid)."""
        golden = load_golden(skill_golden_dir / "audit_report.yaml")
        result = run_structural(golden, skill_demo_todos, skill_demo_archive)
        assert result["passed"] == len(golden["assertions"])
        assert result["failed"] == 0

    def test_archive_golden_passes(self, skill_golden_dir, skill_demo_todos, skill_demo_archive):
        """Archive golden should pass — simulate archive, then verify."""
        new_todos, new_archive = simulate_archive(skill_demo_todos, skill_demo_archive)
        golden = load_golden(skill_golden_dir / "archive_result.yaml")
        result = run_structural(golden, new_todos, new_archive)
        assert result["failed"] == 0, f"Failed assertions: {result['results']}"

    def test_add_golden_fails_without_new_entry(self, skill_golden_dir, skill_demo_todos, skill_demo_archive):
        """Add golden expects 7 entries in TODOS.md — fixture only has 6, so it should fail."""
        golden = load_golden(skill_golden_dir / "add_happy_path.yaml")
        result = run_structural(golden, skill_demo_todos, skill_demo_archive)
        assert result["failed"] > 0

    def test_init_golden_fails_with_existing_files(self, skill_golden_dir, skill_demo_todos, skill_demo_archive):
        """Init golden expects 0 entries — fixture has 6, so regex_count should fail."""
        golden = load_golden(skill_golden_dir / "init_output.yaml")
        result = run_structural(golden, skill_demo_todos, skill_demo_archive)
        assert result["failed"] > 0


class TestAssertGolden:
    def test_raises_on_failure(self, skill_golden_dir, skill_demo_todos, skill_demo_archive):
        """assert_golden should raise when assertions fail."""
        golden_path = skill_golden_dir / "add_happy_path.yaml"
        with pytest.raises(AssertionError):
            assert_golden(golden_path, skill_demo_todos, skill_demo_archive)

    def test_silently_passes(self, skill_golden_dir, skill_demo_todos, skill_demo_archive):
        """assert_golden should not raise when audit golden passes."""
        golden_path = skill_golden_dir / "audit_report.yaml"
        assert_golden(golden_path, skill_demo_todos, skill_demo_archive)
