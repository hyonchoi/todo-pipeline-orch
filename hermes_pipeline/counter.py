"""Counter recovery — scan TODOS.md for max TODO-N and initialize .hermes/todo_id_counter."""

from __future__ import annotations
import re
from pathlib import Path

TODO_ID_RE = re.compile(r"\bTODO-(\d+)\b")


def recover_counter(project_dir: Path) -> int:
    """Scan TODOS.md for TODO-N entries and initialize/update the counter file.

    Reads project_dir / "TODOS.md", finds the maximum N in TODO-N patterns,
    and writes max(existing_counter, scanned_max) to
    project_dir / ".hermes" / "todo_id_counter".

    If the counter file exists and has a higher value than the scanned max
    (e.g., completed TODOs were removed), the existing counter is preserved.
    This prevents ID resurrection.

    Args:
        project_dir: Path to the project root (containing TODOS.md).

    Returns:
        The counter value after recovery.

    Raises:
        FileNotFoundError: If TODOS.md doesn't exist in the project directory.
    """
    todos_path = project_dir / "TODOS.md"
    if not todos_path.exists():
        raise FileNotFoundError(f"TODOS.md not found in {project_dir}")

    # Scan TODOS.md for TODO-N patterns
    todos_content = todos_path.read_text()
    scanned_ids = [int(m) for m in TODO_ID_RE.findall(todos_content)]
    scanned_max = max(scanned_ids) if scanned_ids else 0

    # Read existing counter (if any)
    counter_path = project_dir / ".hermes" / "todo_id_counter"
    existing_value = 0
    if counter_path.exists():
        try:
            existing_value = int(counter_path.read_text().strip())
        except (ValueError, OSError):
            # Corrupt or unreadable counter — treat as 0
            existing_value = 0

    # Use the maximum of existing and scanned (never decrease)
    result = max(existing_value, scanned_max)

    # Write the counter file (create .hermes/ if needed)
    counter_path.parent.mkdir(parents=True, exist_ok=True)
    counter_path.write_text(str(result))

    return result
