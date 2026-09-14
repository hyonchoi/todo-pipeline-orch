"""Shared kanban task helpers for test fixtures."""
import json


def kanban_task(tick_id: str, phase_key: str, status: str, *, todo_id: str) -> dict[str, object]:
    """Create a kanban task dict in the shape expected by execution tests.

    Args:
        tick_id: Tick identifier
        phase_key: Phase key (e.g., "task-1", "gate-1")
        status: Task status
        todo_id: TODO identifier (e.g., "TODO-7")

    Returns:
        Dict with id, status, and body containing JSON header
    """
    header = {"tick_id": tick_id, "phase_key": phase_key, "todo_id": todo_id}
    return {
        "id": f"task-{phase_key}",
        "status": status,
        "body": json.dumps(header) + "\nbody",
    }
