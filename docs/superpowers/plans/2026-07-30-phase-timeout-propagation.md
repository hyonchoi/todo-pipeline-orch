# Phase Timeout Propagation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each phase profile's `timeout` the authoritative deadline for both its Hermes Kanban worker and its delegated Codex or Claude process.

**Architecture:** Preserve `Phase.timeout` in the immutable prepared-task boundary, pass it to Hermes task creation through `--max-runtime`, and render it into the external-client delegation contract. The contract requires tracked background execution because Hermes clamps foreground terminal calls to 600 seconds, and it forbids Hermes from converting a timed-out or non-zero external run into a successful phase.

**Tech Stack:** Python 3.12+, dataclasses, PyYAML phase profiles, Hermes Kanban CLI, pytest, uv

## Global Constraints

- The phase `timeout` is authoritative for both the complete Hermes Kanban worker and the delegated external process.
- Executable external clients must use Hermes tracked background execution; foreground terminal execution is not acceptable for long-running phases.
- Launch failure, non-zero exit, or timeout must block or fail the task and must never fall back to Hermes implementing or committing the phase itself.
- Gate tasks are non-executable and must not receive `--max-runtime`.
- `prompt_client` remains limited to prompt vocabulary and external-command selection.
- Preserve the `Phase.timeout` default of 1,800 seconds for profiles that omit it.
- Use `rtk`-prefixed commands for searches, reads, tests, linting, and compilation.

---

### Task 1: Preserve timeout and render the delegation deadline

**Files:**
- Modify: `tests/test_kanban_tasks.py`
- Modify: `hermes_pipeline/kanban_tasks.py`

**Interfaces:**
- Consumes: `Phase.timeout: int`, populated by `load_phases()`.
- Produces: `PreparedPhaseTask.timeout: int = 1800`.
- Produces: `_external_client_delegation_block(prompt_client: PromptClient, timeout: int) -> str`.
- Produces: executable task bodies containing `External agent timeout: <N> seconds` and the required background-execution/failure metadata contract.

- [ ] **Step 1: Write failing preparation and delegation tests**

Extend `test_prepare_todo_phases_wraps_executable_phases_with_client_delegation` so its YAML contains a non-default timeout and its assertions prove both structured and rendered propagation:

```python
    phases_path.write_text(
        "phases:\n"
        "  - phase_key: phase_1\n"
        "    name: One\n"
        "    prompt: 'Use {skill_prefix}review.'\n"
        "    tools: Read,Bash\n"
        "    turns: 5\n"
        "    timeout: 2400\n"
    )

    # Existing client-vocabulary assertions remain.
    assert prepared[0].timeout == 2400
    assert "External agent timeout: 2400 seconds" in prepared[0].body
    assert "tracked background execution" in prepared[0].body
    assert "monitor the background process" in prepared[0].body
    assert "external_agent_timeout_seconds" in prepared[0].body
    assert "external_agent_exit_code" in prepared[0].body
    assert "must not inspect partial changes" in prepared[0].body
    assert "must not implement or commit the phase yourself" in prepared[0].body
```

Extend `test_prepare_todo_phases_does_not_wrap_gate_phase_with_client_delegation` with:

```python
    assert prepared[0].timeout == 1800
    assert "External agent timeout" not in prepared[0].body
    assert "tracked background execution" not in prepared[0].body
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
rtk uv run pytest \
  tests/test_kanban_tasks.py::test_prepare_todo_phases_wraps_executable_phases_with_client_delegation \
  tests/test_kanban_tasks.py::test_prepare_todo_phases_does_not_wrap_gate_phase_with_client_delegation \
  -q
```

Expected: FAIL because `PreparedPhaseTask` has no `timeout` attribute and the delegation body has no timeout/background contract.

- [ ] **Step 3: Add timeout to the prepared-task boundary**

Change the dataclass without breaking existing positional constructors:

```python
@dataclass(frozen=True)
class PreparedPhaseTask:
    phase_key: str
    name: str
    body: str
    turns: int
    gate: bool
    timeout: int = 1800
```

Change the delegation helper signature and body:

```python
def _external_client_delegation_block(
    prompt_client: PromptClient,
    timeout: int,
) -> str:
    ...
    return (
        "External client delegation:\n"
        "You are the Hermes dispatcher, not the implementation agent.\n"
        ...
        f"Required external command: `{command}`\n"
        f"External agent timeout: {timeout} seconds.\n"
        "Launch the external command with Hermes tracked background execution, "
        "then monitor the background process until it exits or this deadline "
        "expires. Do not use a foreground terminal call because Hermes may "
        "replace this phase timeout with its shorter foreground cap.\n"
        "If the external client is unavailable, exits non-zero, or exceeds "
        "the deadline, block or fail this task with the exact reason. You "
        "must not inspect partial changes, and must not implement or commit "
        "the phase yourself.\n"
        "When completing the task, include result metadata with "
        "`external_agent_command`, `external_agent_timeout_seconds`, "
        "`external_agent_exit_code`, and any external session identifier.\n\n"
    )
```

Pass the exact phase timeout while preparing executable tasks:

```python
        delegation = (
            ""
            if phase.gate
            else _external_client_delegation_block(
                prompt_client,
                timeout=phase.timeout,
            )
        )
        prepared.append(
            PreparedPhaseTask(
                ...
                turns=phase.turns,
                gate=phase.gate,
                timeout=phase.timeout,
            )
        )
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
rtk uv run pytest \
  tests/test_kanban_tasks.py::test_prepare_todo_phases_wraps_executable_phases_with_client_delegation \
  tests/test_kanban_tasks.py::test_prepare_todo_phases_does_not_wrap_gate_phase_with_client_delegation \
  -q
```

Expected: all parametrized Codex/Claude cases and the gate case PASS.

- [ ] **Step 5: Run preparation regressions**

Run:

```bash
rtk uv run pytest tests/test_kanban_tasks.py -k 'prepare_todo_phases' -q
```

Expected: PASS with no prompt-rendering or atomic-preparation regressions.

- [ ] **Step 6: Commit Task 1**

Use the `git-atomic-commits` skill to stage only:

```text
hermes_pipeline/kanban_tasks.py
tests/test_kanban_tasks.py
```

Commit message:

```text
Preserve phase timeout in delegation tasks
```

---

### Task 2: Apply the timeout to executable Hermes tasks

**Files:**
- Modify: `tests/test_kanban_tasks.py`
- Modify: `tests/test_kanban_registration_barrier.py`
- Modify: `hermes_pipeline/kanban_tasks.py`

**Interfaces:**
- Consumes: `PreparedPhaseTask.timeout: int`.
- Produces: executable `hermes kanban create` commands containing `--max-runtime <timeout>`.
- Preserves: registration barrier and gate commands without `--max-runtime`.

- [ ] **Step 1: Write failing executable-command propagation test**

In `test_create_prepared_todo_phases_preserves_command_chain`, give the executable tasks distinct non-default timeouts:

```python
        PreparedPhaseTask(
            phase_key="phase_1",
            name="One",
            body="already rendered $body",
            turns=5,
            gate=False,
            timeout=2400,
        ),
        PreparedPhaseTask(
            phase_key="phase_2",
            name="Two",
            body="second body",
            turns=10,
            gate=False,
            timeout=7200,
        ),
```

After collecting `create_commands`, assert:

```python
    assert "--max-runtime" not in create_commands[0]
    assert create_commands[1][create_commands[1].index("--max-runtime") + 1] == "2400"
    assert create_commands[2][create_commands[2].index("--max-runtime") + 1] == "7200"
```

- [ ] **Step 2: Write failing gate exclusion test**

Update `_prepared_phases()` in `tests/test_kanban_registration_barrier.py`:

```python
    return [
        PreparedPhaseTask("phase_1", "One", "body one", 5, False, 2400),
        PreparedPhaseTask("phase_gate", "Gate", "gate body", 0, True, 9999),
        PreparedPhaseTask("phase_2", "Two", "body two", 10, False, 7200),
    ]
```

In `test_registration_barrier_owns_executable_chain_and_commits_last`, assert:

```python
    assert "--max-runtime" not in create_commands["__registration_barrier__"]
    assert "--max-runtime" not in create_commands["phase_gate"]
    assert (
        create_commands["phase_1"][
            create_commands["phase_1"].index("--max-runtime") + 1
        ]
        == "2400"
    )
    assert (
        create_commands["phase_2"][
            create_commands["phase_2"].index("--max-runtime") + 1
        ]
        == "7200"
    )
```

- [ ] **Step 3: Run focused registration tests and verify RED**

Run:

```bash
rtk uv run pytest \
  tests/test_kanban_tasks.py::test_create_prepared_todo_phases_preserves_command_chain \
  tests/test_kanban_registration_barrier.py::test_registration_barrier_owns_executable_chain_and_commits_last \
  -q
```

Expected: FAIL because executable create commands do not contain `--max-runtime`.

- [ ] **Step 4: Add the executable-task runtime flag**

In `create_prepared_todo_phases()`, extend only non-gate task commands:

```python
        if not is_gate:
            cmd.extend(
                [
                    "--max-runtime",
                    str(phase.timeout),
                    "--goal",
                    "--goal-max-turns",
                    str(phase.turns),
                ]
            )
```

Do not add the flag to the registration barrier or gate path.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```bash
rtk uv run pytest \
  tests/test_kanban_tasks.py::test_create_prepared_todo_phases_preserves_command_chain \
  tests/test_kanban_registration_barrier.py::test_registration_barrier_owns_executable_chain_and_commits_last \
  -q
```

Expected: PASS.

- [ ] **Step 6: Run registration and recovery regressions**

Run:

```bash
rtk uv run pytest \
  tests/test_kanban_tasks.py \
  tests/test_kanban_registration_barrier.py \
  tests/test_kanban_tasks_legacy.py \
  -q
```

Expected: PASS; registration order, cleanup markers, gates, and legacy compatibility remain unchanged.

- [ ] **Step 7: Commit Task 2**

Use the `git-atomic-commits` skill to stage only:

```text
hermes_pipeline/kanban_tasks.py
tests/test_kanban_tasks.py
tests/test_kanban_registration_barrier.py
```

Commit message:

```text
Apply phase timeout to Hermes tasks
```

---

### Task 3: Align operator documentation and verify the repository

**Files:**
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/reference-kanban-as-scheduler.md`

**Interfaces:**
- Consumes: the timeout propagation behavior implemented in Tasks 1 and 2.
- Produces: operator-facing documentation that distinguishes the phase deadline from Hermes's foreground terminal cap.

- [ ] **Step 1: Update architecture documentation**

In the Kanban task-preparation/execution section of `docs/ARCHITECTURE.md`, document this exact flow:

```text
Phase.timeout
  -> PreparedPhaseTask.timeout
  -> hermes kanban create --max-runtime <seconds>
  -> tracked background Codex/Claude process with the same deadline
```

State that only a zero external-agent exit may complete the phase and that Hermes cannot finish or commit partial work after timeout/non-zero exit.

- [ ] **Step 2: Update the Kanban scheduler reference**

In `docs/reference-kanban-as-scheduler.md`, add an operator note containing:

```markdown
The selected profile's `phases.yaml` owns each executable phase deadline.
TPO sends that value to Hermes as `--max-runtime` and includes it in the
external-agent delegation contract. Delegated clients run as tracked
background processes so Hermes's 600-second foreground terminal cap cannot
shorten a phase configured for longer work.
```

Document that `hermes kanban show <task-id> --json` exposes task state and
`hermes kanban log <task-id>` exposes the worker audit trail.

- [ ] **Step 3: Run documentation and static checks**

Run:

```bash
rtk git diff --check
rtk uv run ruff check .
```

Expected: both commands PASS.

- [ ] **Step 4: Run the complete locked test suite**

Run:

```bash
rtk uv run --locked pytest -q
```

Expected: all tests PASS, with only pre-existing documented skips.

- [ ] **Step 5: Verify the packaged phase resources**

Run:

```bash
rtk uv run python - <<'PY'
from hermes_pipeline.phases import load_phases

phases = load_phases()
assert all(phase.timeout > 600 for phase in phases)
assert {phase.phase_key: phase.timeout for phase in phases}["phase_8_finish_branch"] == 7200
print("phase timeout resources: PASS")
PY
```

Expected:

```text
phase timeout resources: PASS
```

- [ ] **Step 6: Commit Task 3**

Use the `git-atomic-commits` skill to stage only:

```text
docs/ARCHITECTURE.md
docs/reference-kanban-as-scheduler.md
```

Commit message:

```text
Document phase-owned execution deadlines
```

- [ ] **Step 7: Perform final evidence review**

Run:

```bash
rtk git status --short
rtk git log --oneline -4
rtk git diff HEAD~3..HEAD --check
```

Expected: clean worktree, three implementation commits in dependency order, and no whitespace errors.
