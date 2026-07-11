import sys
from pathlib import Path

# Map hyphenated directory to valid Python package name for imports.
_skill_test_dir = Path(__file__).parent / "skill-test-environment"
if _skill_test_dir.exists():
    _alias = Path(__file__).parent / "skill_test_environment"
    if not _alias.exists():
        # Create a symlink so "import tests.skill_test_environment" works
        try:
            _alias.symlink_to("skill-test-environment")
        except FileExistsError:
            pass  # symlink already exists

import pytest
from pathlib import Path

@pytest.fixture
def tmp_project(tmp_path):
    """A scratch project dir with TODOS.md + .hermes/."""
    proj = tmp_path / "demo"
    (proj / ".hermes").mkdir(parents=True)
    (proj / "TODOS.md").write_text("# TODOS\n\n")
    (proj / ".hermes" / "todo_id_counter").write_text("0")
    return proj

@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """A scratch ~/.hermes/ replacement."""
    sd = tmp_path / "state"
    (sd / "pipeline_locks").mkdir(parents=True)
    return sd
