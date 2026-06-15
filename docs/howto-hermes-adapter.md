# How to use the Hermes adapter

The `hermes_pipeline.hermes_adapter` module replaces all direct Anthropic SDK
calls in the orchestrator. Instead of importing `anthropic` and calling
`messages.create()`, code shells out to `hermes chat -q` via subprocess. This
centralizes model policy (auth, model selection, fallback) under Hermes rather
than Python.

There are two functions — use the right one for the job:

- **`hermes_call()`** — simple one-shot query, returns a string. Use this when
  you need a text response (e.g., the decision agent asking "which TODO to pick?").
- **`hermes_agent_call()`** — agent-style call with PID tracking and structured
  result. Use this when you need to spawn a long-running phase and track its
  process (e.g., phase execution).

## Prerequisites

- Hermes CLI installed and authenticated (`hermes login`).
- Python 3.12+ with `uv sync` run at the repo root.

## Steps

### Use `hermes_call()` for simple queries

```python
from hermes_pipeline.hermes_adapter import hermes_call, HermesCallError

try:
    response = hermes_call(
        prompt="Which TODO should I work on next?",
        model="claude-sonnet-4-6",
        timeout=60,
    )
    print(response)
except HermesCallError as e:
    # e.returncode, e.stderr available for debugging
    print(f"hermes failed: rc={e.returncode}, stderr={e.stderr}")
```

The function runs `hermes chat -q -Q --source tool` and passes the prompt
via stdin. It returns stripped stdout on success, or raises `HermesCallError`
on non-zero exit.

**Key behavior:**
- `model="auto"` (default) lets Hermes resolve from its config.
- `model="claude-sonnet-4-6"` adds `-m claude-sonnet-4-6` to the command.
- Prompt is sent via stdin — it never appears in process arguments.
- Timeout defaults to 120 seconds.

### Use `hermes_agent_call()` for agent-style phases

```python
from hermes_pipeline.hermes_adapter import hermes_agent_call, HermesAgentResult

def on_pid(pid: int) -> None:
    print(f"Phase started as PID {pid}")

result: HermesAgentResult = hermes_agent_call(
    prompt="Implement the feature described in TODO-7.",
    tools="Read,Write,Bash",
    turns=25,
    timeout=1800,
    cwd="/path/to/project",
    on_pid=on_pid,
)

if result.returncode != 0:
    print(f"Phase failed: {result.stderr[:200]}")
elif result.timed_out:
    print("Phase was killed on timeout")
else:
    print(f"Phase succeeded: {result.stdout[:200]}")
```

The function runs `hermes chat -q -Q --source tool` as a long-lived subprocess
and returns a `HermesAgentResult` with `returncode`, `stdout`, `stderr`, and
`timed_out`.

**Key behavior:**
- The `on_pid` callback fires right after the process starts — use it to
  record the PID in a phase_started marker.
- Tool and turn constraints are encoded as an `AGENT_MODE` header in the
  prompt (not enforced by Hermes at the CLI level — see [Caveats](#caveats)).
- If the process exceeds `timeout`, it is killed and `timed_out=True`.
- `KeyboardInterrupt` during timeout cleanup is not masked — pressing Ctrl+C
  aborts cleanly.

### Handle errors from `hermes_call()`

```python
from hermes_pipeline.hermes_adapter import HermesCallError

try:
    hermes_call(prompt="...")
except HermesCallError as e:
    print(f"Exit code: {e.returncode}")
    print(f"Stderr: {e.stderr}")
    # The error message also includes stdout for debugging partial results.
```

`HermesCallError` carries the exit code, stderr, and a message that includes
both stdout and stderr (truncated to 300 chars each) so you can debug partial
results.

### Handle timeouts from `hermes_agent_call()`

```python
result = hermes_agent_call(prompt="...", timeout=300)

if result.timed_out:
    # Process was killed. stdout may contain partial output.
    print("Timeout — process killed")
    print(f"Partial output: {result.stdout[:200]}")
```

On timeout, the process is killed, `returncode=-1`, and `timed_out=True`.
`stdout` and `stderr` contain whatever the process produced before being killed.

## Verification

Test that Hermes is working:

```bash
hermes chat -q "echo hello" -Q --source tool
```

You should see output from Hermes. If you get a non-zero exit, check
authentication: `hermes login`.

## Troubleshooting

**`HermesCallError: hermes chat failed: rc=1 stderr=E100: gateway unreachable`.**
Hermes cannot reach the LLM provider. Run `hermes chat -q "hello"` manually to
verify connectivity.

**`FileNotFoundError: [Errno 2] No such file or directory: 'hermes'`.**
Hermes CLI is not installed or not in PATH. Install Hermes and ensure it is on
your PATH, then run `hermes login`.

**Prompt seems ignored or wrong model used.**
Check the command args by running `hermes --help` and verifying `chat -q`
supports your Hermes version. If `model="auto"`, Hermes uses its default —
check with `hermes model`.

**Tool constraints not enforced.**
See [Caveats](#caveats) — `hermes chat -q` does not have `--tools` or `--turns`
flags. Constraints are encoded as prompt text only.

## Caveats

### Tool and turn constraints are advisory only

`hermes chat -q` has no `--tools` or `--turns` CLI flags. The adapter encodes
constraints as an `AGENT_MODE` header in the prompt:

```
AGENT_MODE: tools=Read,Write,Bash, max_turns=25. Available tools: Read,Write,Bash. Do not use tools not listed. Complete the task within 25 turns.
```

This is a **prompt-level hint** — the actual tool access depends on Hermes
config, not the Phase's tools string. A phase configured with `tools: "Read"`
will get Hermes' full tool access unless the Hermes config restricts it.

This is a known TODO-6 follow-up: migrate to Hermes per-call tool scoping once
the CLI supports it.

### No proxy, no streaming

The adapter uses `hermes chat -q` (one-shot subprocess) rather than `hermes
proxy start` (long-running OpenAI-compatible proxy). The proxy approach would
keep the Python package in orchestration mode with SDK-style calls. The
one-shot approach means model policy, auth, and fallback are managed by Hermes,
not Python — and the `anthropic` package was removed entirely.

## Related

- [Selection seat contract](../hermes_pipeline/decision/README.md) — how
  `hermes_call()` is used by the decision agent
- [Configure `.hermes/config.toml`](howto-config-toml.md) — tuning selection
  model and circuit-breaker thresholds
- [Pipeline state machine](hermes-state-machine.md) — state transitions
  triggered by phase execution
