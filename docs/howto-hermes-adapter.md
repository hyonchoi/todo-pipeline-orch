# How to use the Hermes adapter

The `hermes_pipeline.hermes_adapter` module replaces all direct Anthropic SDK
calls in the orchestrator. Instead of importing `anthropic` and calling
`messages.create()`, code shells out to `hermes chat -q` via subprocess. This
centralizes model policy (auth, model selection, fallback) under Hermes rather
than Python.

Use the appropriate adapter for the job:

- **`hermes_call()`** — simple one-shot query, returns a string. Use this when
  you need a text response (e.g., the decision agent asking "which TODO to pick?").
- **`hermes_agent_call()`** — agent-style call with PID tracking and structured
  result. Use this when you need to spawn a long-running phase and track its
  process (e.g., phase execution).
- **`claude_call()`** — one-shot selection query through Claude Code CLI,
  used as the eval backend fallback when Hermes is unavailable.

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
    print(f"hermes failed: rc={e.returncode}")
```

The function runs `hermes chat -q <prompt> -Q -t none --source tool` and passes the prompt
as the `-q` argument. It returns stripped stdout on success, or raises `HermesCallError`
on non-zero exit.

**Key behavior:**
- `model="auto"` (default) lets Hermes resolve from its config.
- `model="claude-sonnet-4-6"` adds `-m claude-sonnet-4-6` to the command.
- Prompt and response bodies are not retained in the raised error or debug logs.
- Timeout defaults to 120 seconds.

### Selection isolation and the Claude fallback

Hermes queries pass `-t none` to disable tools instead of inheriting configured
defaults. Claude queries pass `--tools "" --strict-mcp-config --mcp-config
'{"mcpServers":{}}'` to disable builtins and configured MCP servers.

All three adapters remove every `HERMES_KANBAN_*` environment variable from
the child process, including empty or newly added worker variables. They
preserve authentication, profile configuration, `PATH`, and the parent process
environment. A selection subprocess therefore does not inherit its parent's
Kanban worker lifecycle context.

These controls do not sandbox arbitrary user-configured hooks, plugins, or
executable wrappers. Use controlled client configuration for live evaluation;
see [the eval guide](howto-eval-suite.md).

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

The function runs `hermes chat -q <prompt> -Q -t <tools> --max-turns <turns>
--source tool` as a long-lived subprocess
and returns a `HermesAgentResult` with `returncode`, `stdout`, `stderr`, and
`timed_out`.

**Key behavior:**
- The `on_pid` callback fires right after the process starts — use it to
  record the PID in a phase_started marker.
- Explicit toolsets are passed unchanged with `-t`; an empty toolset becomes
  `-t none`, disabling configured default tools. Turn limits use `--max-turns`.
  An `AGENT_MODE` prompt header also describes these constraints.
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
```

`HermesCallError` carries only the exit code. Its message identifies the client
and return code without including stdout or stderr. Run Hermes directly in a
controlled terminal when provider-level diagnostics are required.

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

**`HermesCallError: hermes call failed with return code 1`.**
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
Check that the installed Hermes CLI supports `-t` and `--max-turns`. The
adapter supplies those flags as well as a prompt header; it does not rely on
the header alone. Explicit phase toolsets still grant the requested tools,
while query and empty-tool calls disable them.

## Caveats

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
