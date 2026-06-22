import json
import shutil
from pathlib import Path

from hermes_pipeline.state_migration import _get_project_state_dir, _migrate_global_state
from hermes_pipeline.config import Config


def test_get_project_state_dir(tmp_path: Path):
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    result = _get_project_state_dir(project_dir)
    assert result == project_dir / ".hermes"


def test_migrate_current_tick_id(tmp_path: Path):
    """Migration moves current_tick_id.txt from global to per-project dir."""
    global_state = tmp_path / "global"
    global_state.mkdir()
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    config = Config(
        projects_dir=tmp_path,
        state_dir=global_state,
    )

    (global_state / "current_tick_id.txt").write_text("abc123\n")

    _migrate_global_state(project_dir, config)

    per_project_state = project_dir / ".hermes"
    assert (per_project_state / "current_tick_id.txt").exists()
    assert (per_project_state / "current_tick_id.txt").read_text().strip() == "abc123"
    assert not (global_state / "current_tick_id.txt").exists()


def test_migrate_circuit_json(tmp_path: Path):
    """Migration moves circuit.json from global to per-project dir."""
    global_state = tmp_path / "global"
    global_state.mkdir()
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    config = Config(
        projects_dir=tmp_path,
        state_dir=global_state,
    )

    circuit_data = {"consecutive_no_progress": 3}
    (global_state / "circuit.json").write_text(json.dumps(circuit_data))

    _migrate_global_state(project_dir, config)

    per_project_state = project_dir / ".hermes"
    assert (per_project_state / "circuit.json").exists()
    data = json.loads((per_project_state / "circuit.json").read_text())
    assert data["consecutive_no_progress"] == 3
    assert not (global_state / "circuit.json").exists()


def test_migrate_outcomes_dir(tmp_path: Path):
    """Migration moves outcomes/ directory from global to per-project dir."""
    global_state = tmp_path / "global"
    global_state.mkdir()
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    config = Config(
        projects_dir=tmp_path,
        state_dir=global_state,
    )

    outcomes_dir = global_state / "outcomes"
    outcomes_dir.mkdir()
    (outcomes_dir / "abc123-phases.json").write_text('{"outcome": "phase_complete"}\n')

    _migrate_global_state(project_dir, config)

    per_project_state = project_dir / ".hermes"
    assert (per_project_state / "outcomes").is_dir()
    assert (per_project_state / "outcomes" / "abc123-phases.json").exists()
    assert not (global_state / "outcomes").exists()


def test_migrate_skips_if_already_migrated(tmp_path: Path):
    """Migration does not overwrite per-project files that already exist."""
    global_state = tmp_path / "global"
    global_state.mkdir()
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    per_project_state = project_dir / ".hermes"
    per_project_state.mkdir()
    config = Config(
        projects_dir=tmp_path,
        state_dir=global_state,
    )

    (per_project_state / "current_tick_id.txt").write_text("existing\n")
    (global_state / "current_tick_id.txt").write_text("global\n")

    _migrate_global_state(project_dir, config)

    assert (per_project_state / "current_tick_id.txt").read_text().strip() == "existing"


def test_migrate_no_op_when_no_global_state(tmp_path: Path):
    """Migration does nothing when global state files don't exist."""
    global_state = tmp_path / "global"
    global_state.mkdir()
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    config = Config(
        projects_dir=tmp_path,
        state_dir=global_state,
    )

    _migrate_global_state(project_dir, config)

    per_project_state = project_dir / ".hermes"
    assert not per_project_state.exists()
