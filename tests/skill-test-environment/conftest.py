"""Shared pytest fixtures for skill test environment.

All fixtures prefixed with 'skill_' to avoid collisions with parent tests/conftest.py.
"""
import pytest
from pathlib import Path


@pytest.fixture
def skill_demo_dir() -> Path:
    """Path to the demo-project fixture directory."""
    return Path(__file__).parent / "demo-project"


@pytest.fixture
def skill_golden_dir() -> Path:
    """Path to the golden YAML assertions directory."""
    return Path(__file__).parent / "golden"


@pytest.fixture
def skill_demo_todos(skill_demo_dir) -> str:
    """Content of demo-project TODOS.md."""
    return (skill_demo_dir / "TODOS.md").read_text()


@pytest.fixture
def skill_demo_archive(skill_demo_dir) -> str:
    """Content of demo-project TODOS-archive.md."""
    return (skill_demo_dir / "TODOS-archive.md").read_text()
