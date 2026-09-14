"""Shared project setup helpers for test fixtures."""
from pathlib import Path

# Standard pipeline.toml content for .hermes/pipeline.toml
PIPELINE_TOML = (
    'schema_version = 2\nassignee = "default"\n'
    'capabilities = ["Read", "Write", "Edit", "Bash"]\n'
)


def make_project(
    root: Path,
    name: str,
    *,
    contract: bool = True,
    todos: bool = False,
) -> Path:
    """Create a project directory with optional contract and TODOS.md.

    Args:
        root: Parent directory for project
        name: Project name
        contract: If True, create .hermes/pipeline.toml with PIPELINE_TOML
        todos: If True, create TODOS.md with standard header

    Returns:
        Path to project directory
    """
    project_dir = root / name
    project_dir.mkdir(parents=True, exist_ok=True)

    if contract:
        (project_dir / ".hermes").mkdir(parents=True, exist_ok=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(PIPELINE_TOML)

    if todos:
        (project_dir / "TODOS.md").write_text("# TODOS\n")

    return project_dir


def run_dir(tmp_path: Path, tick_id: str = "01TICK") -> Path:
    """Create and return the run directory for a tick.

    The run directory is where registration.json and other tick state lives.
    """
    path = tmp_path / ".hermes" / "runs" / tick_id
    path.mkdir(parents=True, exist_ok=True)
    return path
