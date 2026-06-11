"""
Lane B: TODOS.md parsing, cycle detection, eligibility filtering, and task selection.

Implements:
  TB.1: TODOS.md parser (headings, dependencies, priority, effort, status)
  TB.2: Cycle detection using Tarjan's SCC algorithm
  TB.3: Eligibility filter + sort by priority/effort/unblocks/order
  TB.4: Cycle notification dedup via last_cycle_warning.json
  TB.5: select_for_project orchestrator with parse isolation
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Set, FrozenSet


# ============================================================================
# TB.1: TODOS.md Parser
# ============================================================================

@dataclass(frozen=True)
class Todo:
    """Immutable TODO record parsed from TODOS.md."""
    todo_id: int
    title: str
    status: str  # " " (pending), "→" (in progress), "x" (done), "~" (blocked)
    priority: Optional[int] = None  # 1-4 (P1-P4), None if not specified
    effort: Optional[str] = None  # "S", "M", "L", "XL", None if not specified
    depends_on: frozenset[int] = frozenset()

    @staticmethod
    def parse_header(line: str) -> Optional[tuple[int, str]]:
        """
        Parse a TODO heading like '## TODO-123: Title here'.
        Returns (todo_id, title) or None if not a valid TODO heading.
        """
        match = re.match(r'^##\s+TODO-(\d+):\s*(.*)$', line.strip())
        if match:
            return int(match.group(1)), match.group(2)
        return None

    @staticmethod
    def parse_fields(block: list[str]) -> dict:
        """
        Extract metadata fields from a TODO block.
        Looks for patterns: "Depends on: 1, 5, 7", "Priority: P1", "Effort: L"
        Also handles markdown list syntax: "- Status: x", "- Priority: P1", etc.
        Returns dict with keys: depends_on (set[int]), priority (int|None), effort (str|None)
        """
        result = {
            "depends_on": set(),
            "priority": None,
            "effort": None,
        }
        for line in block:
            line = line.strip()
            # Remove leading "- " or "* " if present (markdown list)
            if line.startswith("- "):
                line = line[2:]
            elif line.startswith("* "):
                line = line[2:]
            line = line.strip()

            # Match "Depends on: 1, 5, 7"
            dep_match = re.match(r'^Depends\s+on:\s*(.+)$', line, re.IGNORECASE)
            if dep_match:
                deps_str = dep_match.group(1)
                deps = [int(d.strip()) for d in deps_str.split(',') if d.strip().isdigit()]
                result["depends_on"].update(deps)
            # Match "Priority: P1" or "Priority: 1"
            prio_match = re.match(r'^Priority:\s*P?(\d)$', line, re.IGNORECASE)
            if prio_match:
                result["priority"] = int(prio_match.group(1))
            # Match "Effort: M" or "Effort: Medium"
            # Pattern: either single-letter code (S|M|L|XL) or full word (Small|Medium|Large|Extra)
            effort_match = re.match(r'^Effort:\s*(?:(S|M|L|XL)$|(Small|Medium|Large|Extra)$)', line, re.IGNORECASE)
            if effort_match:
                code = effort_match.group(1)
                word = effort_match.group(2)
                if code:
                    result["effort"] = code.upper()
                elif word:
                    word_upper = word.upper()
                    if word_upper.startswith("SMALL"):
                        result["effort"] = "S"
                    elif word_upper.startswith("MEDIUM"):
                        result["effort"] = "M"
                    elif word_upper.startswith("LARGE"):
                        result["effort"] = "L"
                    elif word_upper.startswith("EXTRA"):
                        result["effort"] = "XL"
        return result

    @staticmethod
    def parse_todos(content: str) -> list[Todo]:
        """
        Parse TODOS.md content into a list of Todo records.

        Expected format:
          ## TODO-1: Title
          - Status: [space|→|x|~]
          - Depends on: 2, 3
          - Priority: P2
          - Effort: M

          ## TODO-2: Another Title
          ...
        """
        todos = []
        lines = content.split('\n')
        i = 0
        while i < len(lines):
            line = lines[i]
            parsed_header = Todo.parse_header(line)
            if parsed_header:
                todo_id, title = parsed_header
                # Collect lines until next heading or EOF
                block = []
                i += 1
                while i < len(lines) and not lines[i].strip().startswith('##'):
                    block.append(lines[i])
                    i += 1
                # Parse fields
                fields = Todo.parse_fields(block)
                # Extract status from block (first char of a line starting with "Status:" or dash)
                status = " "  # default to pending
                for line_in_block in block:
                    status_match = re.match(r'^-?\s*Status:\s*([→x~ ])', line_in_block)
                    if status_match:
                        status = status_match.group(1)
                        break
                    # Also check for inline status like "- [x] Status message"
                    inline_match = re.match(r'^-\s*\[(.)\]', line_in_block)
                    if inline_match:
                        char = inline_match.group(1)
                        if char == 'x':
                            status = 'x'
                        elif char == ' ':
                            status = " "
                        break

                todo = Todo(
                    todo_id=todo_id,
                    title=title,
                    status=status,
                    priority=fields["priority"],
                    effort=fields["effort"],
                    depends_on=frozenset(fields["depends_on"]),
                )
                todos.append(todo)
            else:
                i += 1
        return todos


# ============================================================================
# TB.2: Cycle Detection (Tarjan's SCC Algorithm)
# ============================================================================

class CycleDetector:
    """Detect cycles in a TODO dependency graph using Tarjan's SCC algorithm."""

    def __init__(self, todos: list[Todo]):
        self.todos = todos
        self.todo_by_id = {t.todo_id: t for t in todos}
        self.graph = self._build_graph()

    def _build_graph(self) -> dict[int, set[int]]:
        """Build adjacency list: todo_id -> set of todo_ids it depends on."""
        graph = {}
        for todo in self.todos:
            graph[todo.todo_id] = todo.depends_on.copy()
        return graph

    def detect_cycles(self) -> Set[FrozenSet[int]]:
        """
        Detect all cycles (both self-loops and multi-node cycles) using Tarjan's SCC.
        Returns a set of frozensets, each representing a cycle of TODO IDs.
        """
        # First, detect self-loops
        cycles = set()
        for todo_id, deps in self.graph.items():
            if todo_id in deps:
                cycles.add(frozenset([todo_id]))

        # Run Tarjan's algorithm to find all SCCs of size > 1
        self.index_counter = 0
        self.stack = []
        self.indices = {}
        self.lowlinks = {}
        self.on_stack = {}

        for todo_id in self.graph:
            if todo_id not in self.indices:
                self._strongconnect(todo_id, cycles)

        return cycles

    def _strongconnect(self, v: int, cycles: Set[FrozenSet[int]]) -> None:
        """Tarjan's algorithm: recursively visit nodes and build SCCs."""
        self.indices[v] = self.index_counter
        self.lowlinks[v] = self.index_counter
        self.index_counter += 1
        self.stack.append(v)
        self.on_stack[v] = True

        for w in self.graph.get(v, []):
            if w not in self.indices:
                self._strongconnect(w, cycles)
                self.lowlinks[v] = min(self.lowlinks[v], self.lowlinks[w])
            elif self.on_stack.get(w, False):
                self.lowlinks[v] = min(self.lowlinks[v], self.indices[w])

        # If v is a root node, pop the stack and emit SCC
        if self.lowlinks[v] == self.indices[v]:
            component = []
            while True:
                w = self.stack.pop()
                self.on_stack[w] = False
                component.append(w)
                if w == v:
                    break
            # Only add cycles of size > 1
            if len(component) > 1:
                cycles.add(frozenset(component))


# ============================================================================
# TB.3: Eligibility Filter & Sort
# ============================================================================

class EligibilityFilter:
    """Filter and sort TODOs by eligibility and priority."""

    def __init__(self, todos: list[Todo], cycles: Set[FrozenSet[int]]):
        self.todos = todos
        self.cycles = cycles
        self.todo_by_id = {t.todo_id: t for t in todos}
        self.cyclic_ids = self._extract_cyclic_ids()

    def _extract_cyclic_ids(self) -> Set[int]:
        """Flatten all cycle sets into a single set of cyclic TODO IDs."""
        result = set()
        for cycle in self.cycles:
            result.update(cycle)
        return result

    def filter_eligible(self) -> list[Todo]:
        """
        Filter TODOs to only those that are eligible for selection.

        Criteria:
          1. status == " " (pending, not in progress, done, or blocked)
          2. not in any cycle
          3. all dependencies are satisfied (status == "x")
        """
        eligible = []
        for todo in self.todos:
            # Criterion 1: pending status
            if todo.status != " ":
                continue
            # Criterion 2: not cyclic
            if todo.todo_id in self.cyclic_ids:
                continue
            # Criterion 3: all deps satisfied
            all_deps_satisfied = all(
                self.todo_by_id[dep_id].status == "x"
                for dep_id in todo.depends_on
                if dep_id in self.todo_by_id
            )
            if not all_deps_satisfied:
                continue
            eligible.append(todo)
        return eligible

    def _unblocks_count(self, todo: Todo) -> int:
        """
        Count how many OTHER pending (eligible) TODOs would have ALL their deps satisfied
        if this TODO were marked as done.

        A TODO is "unblocked" by this one if:
          - It has pending status (eligible)
          - This TODO is in its dependencies
          - All OTHER dependencies of that TODO are already satisfied

        Returns the count.
        """
        count = 0
        for other in self.todos:
            if other.todo_id == todo.todo_id:
                continue
            # Other must be pending
            if other.status != " ":
                continue
            # This TODO must be in other's deps
            if todo.todo_id not in other.depends_on:
                continue
            # All OTHER deps must be satisfied
            all_other_deps_satisfied = all(
                self.todo_by_id[dep_id].status == "x"
                for dep_id in other.depends_on
                if dep_id != todo.todo_id and dep_id in self.todo_by_id
            )
            if all_other_deps_satisfied:
                count += 1
        return count

    def sort_by_priority(self, todos: list[Todo]) -> list[Todo]:
        """
        Sort eligible TODOs by:
          1. Priority (P1=1 < P2=2 < P3=3 < P4=4, unspecified=999)
          2. Effort (S < M < L < XL, unspecified=999)
          3. Unblocks count (descending: higher counts first)
          4. TODO ID (ascending: lower IDs first)
        """
        effort_rank = {"S": 0, "M": 1, "L": 2, "XL": 3}

        def sort_key(todo: Todo):
            prio = todo.priority if todo.priority is not None else 999
            eff = effort_rank.get(todo.effort, 999) if todo.effort else 999
            unblocks = -self._unblocks_count(todo)  # Negative for descending
            return (prio, eff, unblocks, todo.todo_id)

        return sorted(todos, key=sort_key)


# ============================================================================
# TB.4: Cycle Notification Dedup
# ============================================================================

class CycleNotifier:
    """Track cycle changes and notify only when composition changes."""

    def __init__(self, state_dir: Optional[Path] = None):
        if state_dir is None:
            state_dir = Path.home() / ".hermes"
        self.state_dir = state_dir
        self.cycle_warning_file = state_dir / "last_cycle_warning.json"

    def should_notify(self, cycles: Set[FrozenSet[int]]) -> bool:
        """
        Check if we should notify about cycles.
        Returns True if cycle composition changed since last tick.
        """
        current_composition = self._normalize_cycles(cycles)
        last_composition = self._load_last_composition()

        return current_composition != last_composition

    def update_and_notify(self, cycles: Set[FrozenSet[int]]) -> bool:
        """
        Update stored cycle composition and return whether to notify.
        If cycles are resolved (empty), clears the stored entry.
        """
        should_notify = self.should_notify(cycles)

        if cycles:
            # Store the current cycle composition
            composition = self._normalize_cycles(cycles)
            self._save_composition(composition)
        else:
            # Clear the entry when cycles resolve
            if self.cycle_warning_file.exists():
                self.cycle_warning_file.unlink()

        return should_notify

    def _normalize_cycles(self, cycles: Set[FrozenSet[int]]) -> list[list[int]]:
        """
        Normalize cycles to a sortable list of sorted lists.
        Allows comparison across ticks.
        """
        normalized = [sorted(cycle) for cycle in cycles]
        normalized.sort()
        return normalized

    def _load_last_composition(self) -> list[list[int]]:
        """Load the last stored cycle composition from disk."""
        if not self.cycle_warning_file.exists():
            return []
        try:
            with open(self.cycle_warning_file) as f:
                data = json.load(f)
                return data.get("cycles", [])
        except Exception:
            return []

    def _save_composition(self, composition: list[list[int]]) -> None:
        """Save cycle composition to disk."""
        self.cycle_warning_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.cycle_warning_file, "w") as f:
            json.dump({"cycles": composition}, f)


# ============================================================================
# TB.5: Orchestrator with Parse Isolation
# ============================================================================

def select_for_project(todos_md_path: Path, state_dir: Optional[Path] = None) -> tuple[Optional[Todo], Optional[str]]:
    """
    Orchestrate the selection pipeline for a single project.

    Returns:
      (selected_todo or None, parse_error or None)

    If parse_error is not None, selected_todo will be None (parse failed).
    If both are None, no eligible TODOs are available (but no error).
    """
    try:
        # Read TODOS.md
        if not todos_md_path.exists():
            return None, f"TODOS.md not found: {todos_md_path}"

        content = todos_md_path.read_text(encoding='utf-8')

        # TB.1: Parse TODOs
        todos = Todo.parse_todos(content)
        if not todos:
            return None, None

        # TB.2: Detect cycles
        detector = CycleDetector(todos)
        cycles = detector.detect_cycles()

        # TB.4: Notify about cycles (if any)
        notifier = CycleNotifier(state_dir)
        notifier.update_and_notify(cycles)

        # TB.3: Filter and sort eligible TODOs
        filter_obj = EligibilityFilter(todos, cycles)
        eligible = filter_obj.filter_eligible()
        if not eligible:
            return None, None

        sorted_todos = filter_obj.sort_by_priority(eligible)
        selected = sorted_todos[0]

        return selected, None

    except Exception as e:
        # Catch all parsing errors and return them without raising
        return None, str(e)
