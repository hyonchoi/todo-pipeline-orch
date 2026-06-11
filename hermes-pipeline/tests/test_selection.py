"""
Comprehensive tests for Lane B: selection.py (TB.1-TB.5)
"""
import json
from pathlib import Path
from hermes_pipeline.selection import (
    Todo,
    CycleDetector,
    EligibilityFilter,
    CycleNotifier,
    select_for_project,
)


# ============================================================================
# TB.1: TODOS.md Parser Tests
# ============================================================================

class TestTodoParseHeader:
    """Test parsing of TODO heading lines."""

    def test_parse_valid_header(self):
        """Parse a valid TODO heading."""
        todo_id, title = Todo.parse_header("## TODO-42: Build the feature")
        assert todo_id == 42
        assert title == "Build the feature"

    def test_parse_header_with_extra_spaces(self):
        """Parse heading with extra spaces."""
        todo_id, title = Todo.parse_header("##   TODO-7:   Some Title Here   ")
        assert todo_id == 7
        assert title == "Some Title Here"

    def test_parse_invalid_header_no_number(self):
        """Non-TODO headings return None."""
        assert Todo.parse_header("## Regular Heading") is None
        assert Todo.parse_header("# TODO-1: Wrong level") is None

    def test_parse_header_large_number(self):
        """Parse TODO with large ID."""
        todo_id, title = Todo.parse_header("## TODO-99999: Big ID")
        assert todo_id == 99999


class TestTodoParseFields:
    """Test parsing of TODO metadata fields."""

    def test_parse_depends_on(self):
        """Extract dependencies from a block."""
        block = [
            "Some description",
            "Depends on: 1, 5, 7",
            "More text",
        ]
        fields = Todo.parse_fields(block)
        assert fields["depends_on"] == {1, 5, 7}

    def test_parse_priority(self):
        """Extract priority from a block."""
        block = ["Priority: P2", "Some text"]
        fields = Todo.parse_fields(block)
        assert fields["priority"] == 2

    def test_parse_priority_without_p(self):
        """Extract priority without 'P' prefix."""
        block = ["Priority: 3"]
        fields = Todo.parse_fields(block)
        assert fields["priority"] == 3

    def test_parse_effort_short(self):
        """Extract effort with short codes."""
        for short, expected in [("S", "S"), ("M", "M"), ("L", "L"), ("XL", "XL")]:
            block = [f"Effort: {short}"]
            fields = Todo.parse_fields(block)
            assert fields["effort"] == expected

    def test_parse_effort_long(self):
        """Extract effort with long names."""
        for long_name, expected in [
            ("Small", "S"),
            ("Medium", "M"),
            ("Large", "L"),
            ("Extra", "XL"),
        ]:
            block = [f"Effort: {long_name}"]
            fields = Todo.parse_fields(block)
            assert fields["effort"] == expected

    def test_parse_no_fields(self):
        """Block with no metadata fields."""
        block = ["Just a description", "More description"]
        fields = Todo.parse_fields(block)
        assert fields["depends_on"] == set()
        assert fields["priority"] is None
        assert fields["effort"] is None


class TestTodoParseTodos:
    """Test full TODOS.md parsing."""

    def test_parse_two_todos(self):
        """Parse two complete TODO blocks."""
        content = """## TODO-1: First Task
- Status: [space]
- Priority: P1
- Effort: S

## TODO-2: Second Task
- Status: [ ]
- Priority: P3
- Depends on: 1
"""
        todos = Todo.parse_todos(content)
        assert len(todos) == 2
        assert todos[0].todo_id == 1
        assert todos[0].title == "First Task"
        assert todos[0].priority == 1
        assert todos[0].effort == "S"
        assert todos[1].todo_id == 2
        assert todos[1].title == "Second Task"
        assert 1 in todos[1].depends_on

    def test_parse_with_status_field(self):
        """Parse various status values."""
        content = """## TODO-1: Done Task
Status: x

## TODO-2: In Progress Task
Status: →

## TODO-3: Blocked Task
Status: ~

## TODO-4: Pending Task
Status: [space]
"""
        todos = Todo.parse_todos(content)
        assert todos[0].status == "x"
        assert todos[1].status == "→"
        assert todos[2].status == "~"
        assert todos[3].status == " "

    def test_parse_empty_content(self):
        """Parse empty content returns empty list."""
        todos = Todo.parse_todos("")
        assert todos == []

    def test_parse_no_todos(self):
        """Parse content with no TODO headings."""
        content = "## Regular Section\nSome text here\n## Another Section"
        todos = Todo.parse_todos(content)
        assert todos == []

    def test_parse_multiple_dependencies(self):
        """Parse TODOs with multiple dependencies."""
        content = """## TODO-1: Task with multiple deps
Depends on: 2, 3, 4
Priority: P2
"""
        todos = Todo.parse_todos(content)
        assert todos[0].depends_on == frozenset([2, 3, 4])

    def test_parse_case_insensitive_fields(self):
        """Field parsing is case insensitive."""
        content = """## TODO-99: Flexible Case
PRIORITY: P4
EFFORT: L
DEPENDS ON: 5, 10
"""
        todos = Todo.parse_todos(content)
        assert todos[0].priority == 4
        assert todos[0].effort == "L"
        assert todos[0].depends_on == frozenset([5, 10])


# ============================================================================
# TB.2: Cycle Detection Tests
# ============================================================================

class TestCycleDetectorSelfLoops:
    """Test detection of self-loop cycles."""

    def test_self_loop_single_todo(self):
        """Detect when a TODO depends on itself."""
        todos = [Todo(todo_id=1, title="Self Loop", status=" ", depends_on=frozenset([1]))]
        detector = CycleDetector(todos)
        cycles = detector.detect_cycles()
        assert frozenset([1]) in cycles

    def test_no_self_loop(self):
        """No self-loop when TODO doesn't depend on itself."""
        todos = [Todo(todo_id=1, title="No Loop", status=" ", depends_on=frozenset([2]))]
        detector = CycleDetector(todos)
        cycles = detector.detect_cycles()
        assert not cycles


class TestCycleDetectorMultiNodeCycles:
    """Test detection of multi-node cycles using Tarjan."""

    def test_simple_two_node_cycle(self):
        """Detect cycle: 1 -> 2 -> 1."""
        todos = [
            Todo(todo_id=1, title="A", status=" ", depends_on=frozenset([2])),
            Todo(todo_id=2, title="B", status=" ", depends_on=frozenset([1])),
        ]
        detector = CycleDetector(todos)
        cycles = detector.detect_cycles()
        assert frozenset([1, 2]) in cycles

    def test_three_node_cycle(self):
        """Detect cycle: 1 -> 2 -> 3 -> 1."""
        todos = [
            Todo(todo_id=1, title="A", status=" ", depends_on=frozenset([2])),
            Todo(todo_id=2, title="B", status=" ", depends_on=frozenset([3])),
            Todo(todo_id=3, title="C", status=" ", depends_on=frozenset([1])),
        ]
        detector = CycleDetector(todos)
        cycles = detector.detect_cycles()
        assert frozenset([1, 2, 3]) in cycles

    def test_no_cycle_in_dag(self):
        """DAG with no cycles."""
        todos = [
            Todo(todo_id=1, title="A", status=" ", depends_on=frozenset([2])),
            Todo(todo_id=2, title="B", status=" ", depends_on=frozenset([3])),
            Todo(todo_id=3, title="C", status=" ", depends_on=frozenset()),
        ]
        detector = CycleDetector(todos)
        cycles = detector.detect_cycles()
        assert len(cycles) == 0

    def test_multiple_separate_cycles(self):
        """Multiple independent cycles in one graph."""
        todos = [
            # Cycle 1: 1 <-> 2
            Todo(todo_id=1, title="A", status=" ", depends_on=frozenset([2])),
            Todo(todo_id=2, title="B", status=" ", depends_on=frozenset([1])),
            # Cycle 2: 3 <-> 4
            Todo(todo_id=3, title="C", status=" ", depends_on=frozenset([4])),
            Todo(todo_id=4, title="D", status=" ", depends_on=frozenset([3])),
        ]
        detector = CycleDetector(todos)
        cycles = detector.detect_cycles()
        assert frozenset([1, 2]) in cycles
        assert frozenset([3, 4]) in cycles
        assert len(cycles) == 2

    def test_cycle_with_non_cycle_nodes(self):
        """Graph with both a cycle and a DAG tail."""
        todos = [
            # Cycle: 1 -> 2 -> 1
            Todo(todo_id=1, title="A", status=" ", depends_on=frozenset([2])),
            Todo(todo_id=2, title="B", status=" ", depends_on=frozenset([1])),
            # Non-cycle: 3 -> 1
            Todo(todo_id=3, title="C", status=" ", depends_on=frozenset([1])),
        ]
        detector = CycleDetector(todos)
        cycles = detector.detect_cycles()
        assert frozenset([1, 2]) in cycles


# ============================================================================
# TB.3: Eligibility Filter & Sort Tests
# ============================================================================

class TestEligibilityFilter:
    """Test filtering of eligible TODOs."""

    def test_filter_only_pending(self):
        """Only pending TODOs are eligible."""
        todos = [
            Todo(todo_id=1, title="Pending", status=" ", depends_on=frozenset()),
            Todo(todo_id=2, title="Done", status="x", depends_on=frozenset()),
            Todo(todo_id=3, title="In Progress", status="→", depends_on=frozenset()),
            Todo(todo_id=4, title="Blocked", status="~", depends_on=frozenset()),
        ]
        detector = CycleDetector(todos)
        cycles = detector.detect_cycles()
        filter_obj = EligibilityFilter(todos, cycles)
        eligible = filter_obj.filter_eligible()
        assert len(eligible) == 1
        assert eligible[0].todo_id == 1

    def test_filter_excludes_cyclic(self):
        """TODOs in cycles are not eligible."""
        todos = [
            Todo(todo_id=1, title="In Cycle", status=" ", depends_on=frozenset([2])),
            Todo(todo_id=2, title="In Cycle", status=" ", depends_on=frozenset([1])),
            Todo(todo_id=3, title="Free", status=" ", depends_on=frozenset()),
        ]
        detector = CycleDetector(todos)
        cycles = detector.detect_cycles()
        filter_obj = EligibilityFilter(todos, cycles)
        eligible = filter_obj.filter_eligible()
        assert len(eligible) == 1
        assert eligible[0].todo_id == 3

    def test_filter_requires_deps_satisfied(self):
        """TODOs whose dependencies aren't satisfied are not eligible."""
        todos = [
            Todo(todo_id=1, title="Done", status="x", depends_on=frozenset()),
            Todo(todo_id=2, title="Depends on Done", status=" ", depends_on=frozenset([1])),
            Todo(todo_id=3, title="Depends on Pending", status=" ", depends_on=frozenset([4])),
            Todo(todo_id=4, title="Pending", status=" ", depends_on=frozenset()),
        ]
        detector = CycleDetector(todos)
        cycles = detector.detect_cycles()
        filter_obj = EligibilityFilter(todos, cycles)
        eligible = filter_obj.filter_eligible()
        # 2 is eligible (its dep is done), 4 is eligible (no deps)
        eligible_ids = {t.todo_id for t in eligible}
        assert eligible_ids == {2, 4}

    def test_filter_empty_when_all_done(self):
        """No eligible TODOs when all are done."""
        todos = [
            Todo(todo_id=1, title="Done", status="x", depends_on=frozenset()),
            Todo(todo_id=2, title="Done", status="x", depends_on=frozenset()),
        ]
        detector = CycleDetector(todos)
        cycles = detector.detect_cycles()
        filter_obj = EligibilityFilter(todos, cycles)
        eligible = filter_obj.filter_eligible()
        assert eligible == []


class TestEligibilitySort:
    """Test sorting of eligible TODOs."""

    def test_sort_by_priority(self):
        """TODOs are sorted by priority (P1 < P2 < P3 < P4)."""
        todos = [
            Todo(todo_id=1, title="P3", status=" ", priority=3, depends_on=frozenset()),
            Todo(todo_id=2, title="P1", status=" ", priority=1, depends_on=frozenset()),
            Todo(todo_id=3, title="P4", status=" ", priority=4, depends_on=frozenset()),
            Todo(todo_id=4, title="P2", status=" ", priority=2, depends_on=frozenset()),
        ]
        detector = CycleDetector(todos)
        filter_obj = EligibilityFilter(todos, detector.detect_cycles())
        eligible = filter_obj.filter_eligible()
        sorted_todos = filter_obj.sort_by_priority(eligible)
        assert [t.priority for t in sorted_todos] == [1, 2, 3, 4]

    def test_sort_by_effort_within_priority(self):
        """Within same priority, sort by effort (S < M < L < XL)."""
        todos = [
            Todo(todo_id=1, title="L", status=" ", priority=1, effort="L", depends_on=frozenset()),
            Todo(todo_id=2, title="S", status=" ", priority=1, effort="S", depends_on=frozenset()),
            Todo(todo_id=3, title="XL", status=" ", priority=1, effort="XL", depends_on=frozenset()),
            Todo(todo_id=4, title="M", status=" ", priority=1, effort="M", depends_on=frozenset()),
        ]
        detector = CycleDetector(todos)
        filter_obj = EligibilityFilter(todos, detector.detect_cycles())
        eligible = filter_obj.filter_eligible()
        sorted_todos = filter_obj.sort_by_priority(eligible)
        assert [t.effort for t in sorted_todos] == ["S", "M", "L", "XL"]

    def test_sort_by_unblocks_count(self):
        """Higher unblocks_count comes first within same priority/effort."""
        todos = [
            # Task A: when done, unblocks B and C (count=2)
            Todo(todo_id=1, title="A", status=" ", priority=1, effort="S", depends_on=frozenset()),
            Todo(todo_id=2, title="B", status=" ", priority=1, effort="S", depends_on=frozenset([1])),
            Todo(todo_id=3, title="C", status=" ", priority=1, effort="S", depends_on=frozenset([1])),
            # Task D: when done, unblocks only E (count=1)
            Todo(todo_id=4, title="D", status=" ", priority=1, effort="S", depends_on=frozenset()),
            Todo(todo_id=5, title="E", status=" ", priority=1, effort="S", depends_on=frozenset([4])),
        ]
        detector = CycleDetector(todos)
        filter_obj = EligibilityFilter(todos, detector.detect_cycles())
        eligible = filter_obj.filter_eligible()
        sorted_todos = filter_obj.sort_by_priority(eligible)
        # A and D both eligible with same priority/effort, but A unblocks more
        # so A should come before D
        assert sorted_todos[0].todo_id == 1
        assert sorted_todos[1].todo_id == 4

    def test_sort_by_todo_id_as_tiebreaker(self):
        """Lower TODO ID comes first when all other factors are equal."""
        todos = [
            Todo(todo_id=5, title="Task5", status=" ", priority=2, effort="M", depends_on=frozenset()),
            Todo(todo_id=2, title="Task2", status=" ", priority=2, effort="M", depends_on=frozenset()),
            Todo(todo_id=9, title="Task9", status=" ", priority=2, effort="M", depends_on=frozenset()),
        ]
        detector = CycleDetector(todos)
        filter_obj = EligibilityFilter(todos, detector.detect_cycles())
        eligible = filter_obj.filter_eligible()
        sorted_todos = filter_obj.sort_by_priority(eligible)
        assert [t.todo_id for t in sorted_todos] == [2, 5, 9]

    def test_sort_unspecified_priority_last(self):
        """TODOs with unspecified priority rank lower."""
        todos = [
            Todo(todo_id=1, title="P2", status=" ", priority=2, depends_on=frozenset()),
            Todo(todo_id=2, title="No Priority", status=" ", priority=None, depends_on=frozenset()),
            Todo(todo_id=3, title="P1", status=" ", priority=1, depends_on=frozenset()),
        ]
        detector = CycleDetector(todos)
        filter_obj = EligibilityFilter(todos, detector.detect_cycles())
        eligible = filter_obj.filter_eligible()
        sorted_todos = filter_obj.sort_by_priority(eligible)
        assert sorted_todos[0].priority == 1
        assert sorted_todos[1].priority == 2
        assert sorted_todos[2].priority is None


# ============================================================================
# TB.4: Cycle Notification Dedup Tests
# ============================================================================

class TestCycleNotifier:
    """Test cycle change detection and notification."""

    def test_should_notify_on_first_cycle(self, tmp_path):
        """Notify when cycles first appear."""
        notifier = CycleNotifier(tmp_path)
        cycles = {frozenset([1, 2])}
        assert notifier.should_notify(cycles) is True

    def test_should_not_notify_same_cycles(self, tmp_path):
        """Don't notify if same cycles persist."""
        notifier = CycleNotifier(tmp_path)
        cycles = {frozenset([1, 2])}
        # First time: should notify
        assert notifier.should_notify(cycles) is True
        # Save the composition
        notifier._save_composition(notifier._normalize_cycles(cycles))
        # Second time with same cycles: should not notify
        assert notifier.should_notify(cycles) is False

    def test_should_notify_on_cycle_change(self, tmp_path):
        """Notify when cycle composition changes."""
        notifier = CycleNotifier(tmp_path)
        cycles1 = {frozenset([1, 2])}
        # First time
        assert notifier.should_notify(cycles1) is True
        notifier._save_composition(notifier._normalize_cycles(cycles1))
        # Different cycles
        cycles2 = {frozenset([3, 4])}
        assert notifier.should_notify(cycles2) is True

    def test_should_notify_when_cycles_resolve(self, tmp_path):
        """Notify when cycles are resolved (cleared)."""
        notifier = CycleNotifier(tmp_path)
        cycles1 = {frozenset([1, 2])}
        assert notifier.should_notify(cycles1) is True
        notifier._save_composition(notifier._normalize_cycles(cycles1))
        # Now cycles are resolved
        cycles2 = set()
        assert notifier.should_notify(cycles2) is True

    def test_update_and_notify_stores_cycles(self, tmp_path):
        """update_and_notify stores cycles and returns notification status."""
        notifier = CycleNotifier(tmp_path)
        cycles = {frozenset([1, 2])}
        result = notifier.update_and_notify(cycles)
        assert result is True  # First time, should notify
        assert notifier.cycle_warning_file.exists()
        # Check stored content
        with open(notifier.cycle_warning_file) as f:
            data = json.load(f)
            assert data["cycles"] == [[1, 2]]

    def test_update_and_notify_clears_on_resolve(self, tmp_path):
        """update_and_notify clears the file when cycles resolve."""
        notifier = CycleNotifier(tmp_path)
        # First, store some cycles
        cycles1 = {frozenset([1, 2])}
        notifier.update_and_notify(cycles1)
        assert notifier.cycle_warning_file.exists()
        # Now resolve them
        cycles2 = set()
        notifier.update_and_notify(cycles2)
        assert not notifier.cycle_warning_file.exists()

    def test_normalize_cycles_sorts(self, tmp_path):
        """normalize_cycles produces a sortable, consistent output."""
        notifier = CycleNotifier(tmp_path)
        cycles1 = {frozenset([2, 1]), frozenset([4, 3])}
        cycles2 = {frozenset([3, 4]), frozenset([1, 2])}
        # Same cycles but different order should normalize the same
        norm1 = notifier._normalize_cycles(cycles1)
        norm2 = notifier._normalize_cycles(cycles2)
        assert norm1 == norm2


# ============================================================================
# TB.5: Orchestrator with Parse Isolation Tests
# ============================================================================

class TestSelectForProject:
    """Test the orchestrator function."""

    def test_select_from_simple_todos(self, tmp_path):
        """Select best TODO from a simple list."""
        todos_md = tmp_path / "TODOS.md"
        todos_md.write_text("""## TODO-1: First Task
Priority: P1
Effort: S
Status: [space]

## TODO-2: Second Task
Priority: P2
Effort: M
Status: [space]
""")
        selected, error = select_for_project(todos_md)
        assert error is None
        assert selected is not None
        assert selected.todo_id == 1

    def test_select_skips_done_todos(self, tmp_path):
        """Selection skips done TODOs."""
        todos_md = tmp_path / "TODOS.md"
        todos_md.write_text("""## TODO-1: Done Task
Status: x

## TODO-2: Pending Task
Priority: P1
Status: [space]
""")
        selected, error = select_for_project(todos_md)
        assert error is None
        assert selected is not None
        assert selected.todo_id == 2

    def test_select_respects_dependencies(self, tmp_path):
        """Selection respects dependency constraints."""
        todos_md = tmp_path / "TODOS.md"
        todos_md.write_text("""## TODO-1: Blocker Task
Status: x

## TODO-2: Depends on 1
Depends on: 1
Status: [space]

## TODO-3: Free Task
Status: [space]
""")
        selected, error = select_for_project(todos_md)
        assert error is None
        assert selected is not None
        # Both 2 and 3 are eligible; should pick by priority (none specified, same effort)
        # Default sorting picks 3 (lower ID among eligible with same priority/effort)
        # Actually, let me recalculate: 3 is free (no deps), 2 depends on 1 (satisfied)
        # Both eligible, neither has priority/effort specified, so sort by ID
        assert selected.todo_id == 2

    def test_select_detects_and_reports_cycles(self, tmp_path):
        """Selection detects cycles and stores them."""
        todos_md = tmp_path / "TODOS.md"
        todos_md.write_text("""## TODO-1: Cycle Part A
Depends on: 2
Status: [space]

## TODO-2: Cycle Part B
Depends on: 1
Status: [space]
""")
        selected, error = select_for_project(todos_md, tmp_path)
        # Both TODOs are in a cycle, so neither is eligible
        assert selected is None
        assert error is None
        # Check that cycle was recorded
        cycle_file = tmp_path / "last_cycle_warning.json"
        assert cycle_file.exists()
        with open(cycle_file) as f:
            data = json.load(f)
            # Should contain the cycle [1, 2] or [2, 1]
            assert [[1, 2]] == data["cycles"] or [[2, 1]] in [c for c in data["cycles"]]

    def test_select_returns_error_on_missing_file(self, tmp_path):
        """Selection returns error when TODOS.md is missing."""
        todos_md = tmp_path / "NONEXISTENT.md"
        selected, error = select_for_project(todos_md)
        assert selected is None
        assert error is not None
        assert "not found" in error.lower()

    def test_select_returns_none_when_no_eligible(self, tmp_path):
        """Selection returns None when no eligible TODOs."""
        todos_md = tmp_path / "TODOS.md"
        todos_md.write_text("""## TODO-1: Done
Status: x

## TODO-2: In Progress
Status: →
""")
        selected, error = select_for_project(todos_md)
        assert selected is None
        assert error is None

    def test_select_isolates_parse_errors(self, tmp_path):
        """Parse errors are caught and returned, not raised."""
        todos_md = tmp_path / "TODOS.md"
        # Write invalid UTF-8 (simulate a real parse error)
        todos_md.write_bytes(b"## TODO-1: \xff\xfe Invalid")
        selected, error = select_for_project(todos_md)
        assert selected is None
        assert error is not None

    def test_select_with_empty_todos_md(self, tmp_path):
        """Empty TODOS.md returns None, None."""
        todos_md = tmp_path / "TODOS.md"
        todos_md.write_text("")
        selected, error = select_for_project(todos_md)
        assert selected is None
        assert error is None
