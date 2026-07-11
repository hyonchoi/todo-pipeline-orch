"""Shared pytest fixtures for skill test environment."""
import pytest
from pathlib import Path

@pytest.fixture
def skill_demo_dir() -> Path:
    return Path(__file__).parent / "demo-project"
