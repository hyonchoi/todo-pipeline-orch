# Global Agent Client Phase Prompts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a global `prompt_client` setting that renders every bundled phase prompt with verified Claude Code or Codex vocabulary while preserving Hermes scheduling and failure-safe registration.

**Architecture:** Extend the existing typed global configuration with a closed `PromptClient` literal, then pass that resolved value explicitly into a strict repository-template renderer. Split kanban registration into a pure preparation pass and an external-mutation pass so production persists tick state only after every phase body renders successfully; use structured package metadata as the source of truth for prerequisite documentation and render-matrix tests.

**Tech Stack:** Python 3.12+, dataclasses and `typing.Literal`, `string.Formatter`, PyYAML, `importlib.resources`, pytest/pytest-mock, Ruff, uv, Markdown.

## Global Constraints

- Preserve Hermes as the sole execution and scheduling path.
- Do not change Hermes models, profiles, assignees, task commands, authentication, timeouts, phase ordering, or gate behavior.
- Preserve existing behavior when `prompt_client` is absent: the effective value is exactly `claude`.
- Keep global config as a whole-file configuration selected through existing discovery rules; do not add an individual environment-variable override.
- Support only the exact, case-sensitive values `claude` and `codex`; YAML null is invalid.
- Keep one canonical set of bundled phase profiles; do not add client overlays or duplicate prompt bodies.
- Claude Code vocabulary is `agent_product="Claude Code"` and `skill_prefix="/"`; Codex vocabulary is `agent_product="Codex"` and `skill_prefix="$"`.
- Strict templates allow only the exact bare fields `todo_id`, `tick_id`, `project_slug`, `agent_product`, and `skill_prefix`, plus escaped `{{` and `}}`.
- Unknown, positional, malformed, unresolved, converted, format-specified, traversed, indexed, or nested fields raise `PhasePromptRenderError` before any Hermes task is created.
- Treat external skill installation/discovery as a documented prerequisite; normal CI must not require third-party credentials or installations.
- `Unverified` profile/client pairs remain unsupported and non-blocking; do not guess namespaced invocation syntax.
- One global `prompt_client` applies to every project under `projects_dir`; mixed-client fleets require separate project roots or TODO-42.
- All test, lint, and verification commands in this plan are explicitly RTK-prefixed.
- A release version bump is out of scope for implementation; when the normal release workflow later bumps `VERSION`, update `VERSION`, `pyproject.toml`, regenerated `uv.lock`, and `CHANGELOG.md` together.

## File Structure

- `hermes_pipeline/config.py` — owns the `PromptClient` type and effective default.
- `hermes_pipeline/config_loader.py` — emits the setting in the generated YAML skeleton and reuses shared `Literal` validation.
- `hermes_pipeline/phases.py` — owns fixed client vocabulary, strict phase-template validation/rendering, and package-data loading for prerequisite metadata.
- `hermes_pipeline/data/phase-profiles/gstack/phases.yaml` — keeps the canonical gstack workflow while replacing client-specific prose with explicit vocabulary fields.
- `hermes_pipeline/data/phase-profiles/gstack/prerequisites.yaml` — declares gstack skill ownership, per-client discovery/invocation forms, and `Conditional` support.
- `hermes_pipeline/data/phase-profiles/agent-skills/phases.yaml` — keeps unverified namespaced references prefix-neutral while making verified product prose client-aware.
- `hermes_pipeline/data/phase-profiles/agent-skills/prerequisites.yaml` — declares namespaced skills as `Unverified` without guessed invocation or discovery contracts.
- `hermes_pipeline/kanban_tasks.py` — defines immutable prepared-task values, performs pure all-phase preparation, and creates already-prepared Hermes tasks.
- `hermes_pipeline/cli.py` — resolves the contract-selected profile and configured prompt client, records preparation failures, and places persistence immediately before external mutation.
- `hermes_pipeline/harness.py` — resolves the Claude-compatible default once and propagates the configured prompt client through filtered/unfiltered registration.
- `tests/test_config_loader.py` and `tests/test_config_cli.py` — cover defaults, YAML/CLI coercion, skeleton output, persistence, and source attribution.
- `tests/test_phases.py` — covers strict grammar, client vocabulary, every bundled profile/client/phase render, and prerequisite metadata consistency.
- `tests/test_kanban_tasks.py` — covers pure preparation, prepared task creation, and zero external calls on late render failure.
- `tests/test_tick_contract.py` — covers production profile/client propagation and prepare/persist/mutate ordering.
- `tests/test_harness.py` — covers configured and `None` harness client propagation, including filtered phases.
- `tests/test_skills_install.py` — retains package-owned Claude/Codex discovery-root assertions without claiming ownership of external distributions.
- `tests/test_cli_contract.py` — covers doctor’s global-client invariant and prerequisite-status diagnostics.
- `README.md`, `docs/reference-cli.md`, and `docs/howto-agent-skills-profile.md` — explain configuration, vocabulary/profile/assignee/model boundaries, and prerequisites.
- `docs/release-qualification-agent-clients.md` — defines the versioned live-evidence protocol and blocking policy.
- `docs/release-evidence/agent-clients/README.md` — provides the checked-in evidence artifact schema without fabricating live results.

---

### Task 1: Typed Global Prompt Client Configuration

**Files:**
- Modify: `hermes_pipeline/config.py`
- Modify: `hermes_pipeline/config_loader.py`
- Modify: `tests/test_config_loader.py`
- Modify: `tests/test_config_cli.py`

**Interfaces:**
- Consumes: existing dataclass-driven `_config_field_hints`, `_coerce_value(value, target_type, key, source)`, and `config init/get/set` handlers.
- Produces: `PromptClient = Literal["claude", "codex"]` and `Config.prompt_client: PromptClient = "claude"`.

- [ ] **Step 1: Add failing loader tests for the closed configuration contract**

Add these imports and tests to `tests/test_config_loader.py`:

```python
from typing import get_type_hints

from hermes_pipeline.config import Config, PromptClient


def test_prompt_client_type_and_default():
    assert PromptClient == get_type_hints(Config)["prompt_client"]
    assert Config.default().prompt_client == "claude"


@pytest.mark.parametrize("value", ["claude", "codex"])
def test_load_global_config_accepts_prompt_client(monkeypatch, tmp_path, value):
    path = tmp_path / "config.yaml"
    path.write_text(f"prompt_client: {value}\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(path))
    assert load_global_config().prompt_client == value


@pytest.mark.parametrize("yaml_value", ["null", "Claude", "CODEX", "cursor"])
def test_load_global_config_rejects_invalid_prompt_client(
    monkeypatch, tmp_path, yaml_value
):
    path = tmp_path / "config.yaml"
    path.write_text(f"prompt_client: {yaml_value}\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(path))
    with pytest.raises(
        ValueError,
        match=r"invalid value for 'prompt_client'.*must be one of.*claude.*codex",
    ):
        load_global_config()


def test_skeleton_contains_prompt_client_default():
    assert "prompt_client: claude\n" in SKELETON
```

- [ ] **Step 2: Run the loader tests and confirm the field is missing**

Run:

```bash
rtk uv run pytest tests/test_config_loader.py -q
```

Expected: FAIL because `PromptClient` and `Config.prompt_client` do not exist and the skeleton omits the key.

- [ ] **Step 3: Implement the typed field and skeleton entry through shared coercion**

In `hermes_pipeline/config.py`, import `Literal` and add:

```python
from typing import Literal

PromptClient = Literal["claude", "codex"]


@dataclass(frozen=True)
class Config:
    projects_dir: Path = field(default_factory=lambda: Path.home() / "projects")
    state_dir: Path = field(default_factory=lambda: Path.home() / ".hermes")
    log_file_subpath: str = "pipeline.log"
    log_retention_days: int = 7
    slack_channel: str = ""
    prompt_client: PromptClient = "claude"
```

In `hermes_pipeline/config_loader.py`, add the exact active skeleton key:

```yaml
prompt_client: claude
```

Keep `_coerce_value()` generic. In its `Literal` branch, reject `None` before `str(value)` so unquoted YAML null cannot become the string `"None"`:

```python
valid = _get_literal_values(target_type, key)
if valid is not None:
    if value is None:
        raise ValueError(
            f"YAML `null` is not valid; must be one of {sorted(valid)}"
        )
    str_val = str(value)
    if str_val not in valid:
        raise ValueError(
            f"must be one of {sorted(valid)}, got {str_val!r}"
        )
    return str_val
```

- [ ] **Step 4: Add failing CLI round-trip tests**

Append to `tests/test_config_cli.py`:

```python
def test_config_init_emits_prompt_client(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("TPO_CONFIG_FILE", str(path))
    assert main(["config", "init"]) == 0
    assert "prompt_client: claude\n" in path.read_text()


def test_config_get_prompt_client_reports_default_source(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("TPO_CONFIG_FILE", str(tmp_path / "missing.yaml"))
    assert main(["config", "get", "prompt_client"]) == 0
    output = capsys.readouterr().out
    assert "claude" in output
    assert "default" in output


def test_config_set_prompt_client_round_trips(monkeypatch, tmp_path, capsys):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("TPO_CONFIG_FILE", str(path))
    assert main(["config", "set", "prompt_client", "codex"]) == 0
    assert load_global_config().prompt_client == "codex"
    assert main(["config", "get", "prompt_client"]) == 0
    output = capsys.readouterr().out
    assert "codex" in output
    assert str(path) in output


@pytest.mark.parametrize("value", ["Claude", "CODEX", "cursor", "null"])
def test_config_set_rejects_invalid_prompt_client(monkeypatch, tmp_path, value):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("TPO_CONFIG_FILE", str(path))
    with pytest.raises(ValueError, match=r"must be one of.*claude.*codex"):
        main(["config", "set", "prompt_client", value])
    assert not path.exists()
```

Use the existing `main` and `load_global_config` imports/style in that test module rather than adding a parallel CLI harness.

- [ ] **Step 5: Run focused configuration tests**

Run:

```bash
rtk uv run pytest tests/test_config_loader.py tests/test_config_cli.py tests/test_config.py tests/test_config_from_env.py -q
```

Expected: PASS; default, file-loaded, and CLI-set clients are all covered without an environment override.

- [ ] **Step 6: Commit the configuration contract**

```bash
git add hermes_pipeline/config.py hermes_pipeline/config_loader.py \
  tests/test_config_loader.py tests/test_config_cli.py
git commit -m "feat(TODO-41): add global prompt client config"
```

### Task 2: Strict Client-Aware Phase Rendering

**Files:**
- Modify: `hermes_pipeline/phases.py`
- Modify: `hermes_pipeline/data/phase-profiles/gstack/phases.yaml`
- Modify: `hermes_pipeline/data/phase-profiles/agent-skills/phases.yaml`
- Modify: `tests/test_phases.py`

**Interfaces:**
- Consumes: `PromptClient`, `Phase`, `load_phases()`, and the five allowed template fields.
- Produces: `PhasePromptRenderError(ValueError)`, `CLIENT_VOCABULARY`, and `_render_phase_prompt(..., prompt_client: PromptClient = "claude", template_source: str | None = None) -> str`.

- [ ] **Step 1: Add the failing strict-grammar renderer matrix**

Extend `tests/test_phases.py` with:

```python
from hermes_pipeline.phases import PhasePromptRenderError, _render_phase_prompt


@pytest.mark.parametrize(
    ("client", "product", "prefix"),
    [("claude", "Claude Code", "/"), ("codex", "Codex", "$")],
)
def test_render_phase_prompt_all_allowed_fields(client, product, prefix):
    out = _render_phase_prompt(
        "{todo_id}|{tick_id}|{project_slug}|{agent_product}|"
        "{skill_prefix}review|{{literal}}",
        todo_id="TODO-41",
        tick_id="01CLIENT",
        project_slug="demo",
        prompt_client=client,
        template_source="test-profile:phase_x",
    )
    assert out.endswith(
        f"TODO-41|01CLIENT|demo|{product}|{prefix}review|{{literal}}"
    )


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("{unknown}", "unknown"),
        ("{}", "positional"),
        ("{0}", "positional"),
        ("{todo_id", "malformed"),
        ("{agent_product!r}", "conversion"),
        ("{todo_id:>10}", "format specification"),
        ("{todo_id.value}", "traversal"),
        ("{todo_id[0]}", "traversal"),
        ("{todo_id:{tick_id}}", "nested"),
    ],
)
def test_render_phase_prompt_rejects_advanced_formatting(template, message):
    with pytest.raises(
        PhasePromptRenderError,
        match=rf"test-profile:phase_x.*{message}",
    ):
        _render_phase_prompt(
            template,
            todo_id="TODO-41",
            tick_id="01CLIENT",
            project_slug="demo",
            template_source="test-profile:phase_x",
        )


def test_render_phase_prompt_rejects_unknown_client():
    with pytest.raises(
        PhasePromptRenderError,
        match=r"prompt_client.*claude.*codex",
    ):
        _render_phase_prompt(
            "{agent_product}",
            todo_id="TODO-41",
            tick_id="01CLIENT",
            project_slug="demo",
            prompt_client="Claude",
        )


def test_render_phase_prompt_does_not_rewrite_unrelated_text():
    template = "Read docs/a/b.md and https://example.test/a; literal $HOME."
    out = _render_phase_prompt(
        template,
        todo_id="TODO-41",
        tick_id="01CLIENT",
        project_slug="demo",
        prompt_client="codex",
    )
    assert out.endswith(template)
```

- [ ] **Step 2: Run the strict renderer tests and confirm current fallback behavior fails them**

Run:

```bash
rtk uv run pytest tests/test_phases.py -q
```

Expected: FAIL because the renderer lacks client vocabulary, accepts unsupported formatting via broad fallback, and has no `PhasePromptRenderError`.

- [ ] **Step 3: Implement exact field parsing and fixed vocabulary**

In `hermes_pipeline/phases.py`, add:

```python
import string
from typing import Final

from .config import PromptClient

CLIENT_VOCABULARY: Final[dict[PromptClient, dict[str, str]]] = {
    "claude": {"agent_product": "Claude Code", "skill_prefix": "/"},
    "codex": {"agent_product": "Codex", "skill_prefix": "$"},
}
_ALLOWED_PROMPT_FIELDS = frozenset(
    {"todo_id", "tick_id", "project_slug", "agent_product", "skill_prefix"}
)


class PhasePromptRenderError(ValueError):
    """A repository-owned phase prompt violates the strict template grammar."""


def _validate_prompt_template(template: str, source: str) -> None:
    formatter = string.Formatter()
    try:
        parsed = list(formatter.parse(template))
    except ValueError as exc:
        raise PhasePromptRenderError(
            f"{source}: malformed braces: {exc}"
        ) from exc
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if field_name == "" or field_name.isdecimal():
            raise PhasePromptRenderError(
                f"{source}: positional field {field_name!r} is not allowed"
            )
        if "." in field_name or "[" in field_name or "]" in field_name:
            raise PhasePromptRenderError(
                f"{source}: attribute/index traversal {field_name!r} is not allowed"
            )
        if field_name not in _ALLOWED_PROMPT_FIELDS:
            raise PhasePromptRenderError(
                f"{source}: unknown field {field_name!r}"
            )
        if conversion is not None:
            raise PhasePromptRenderError(
                f"{source}: conversion on {field_name!r} is not allowed"
            )
        if format_spec:
            kind = "nested replacement field" if "{" in format_spec else "format specification"
            raise PhasePromptRenderError(
                f"{source}: {kind} on {field_name!r} is not allowed"
            )
```

Update `_render_phase_prompt()` to accept the two compatibility-safe keyword defaults, validate `prompt_client` before vocabulary lookup, validate before calling `.format()`, and remove the broad `KeyError`/`IndexError` fallback:

```python
def _render_phase_prompt(
    template: str,
    *,
    todo_id: str,
    tick_id: str,
    project_slug: str,
    spec_path: str | None = None,
    reference_paths: list[str] | None = None,
    prompt_client: PromptClient = "claude",
    template_source: str | None = None,
) -> str:
    source = template_source or "<phase prompt>"
    try:
        vocabulary = CLIENT_VOCABULARY[prompt_client]
    except KeyError as exc:
        raise PhasePromptRenderError(
            f"{source}: prompt_client must be one of "
            f"{sorted(CLIENT_VOCABULARY)}, got {prompt_client!r}"
        ) from exc
    _validate_prompt_template(template, source)
    body = template.format(
        todo_id=todo_id,
        tick_id=tick_id,
        project_slug=project_slug,
        **vocabulary,
    )
    return header + body
```

Retain the existing header and Spec/Reference block byte-for-byte.

- [ ] **Step 4: Convert only verified client-dependent profile prose**

In `hermes_pipeline/data/phase-profiles/gstack/phases.yaml`, replace explicit client-dependent skill uses such as:

```yaml
Use the /autoplan skill in Claude Code.
Follow /review's fix loop.
```

with:

```yaml
Use the {skill_prefix}autoplan skill in {agent_product}.
Follow {skill_prefix}review's fix loop.
```

Apply this exact field treatment to all gstack references listed in the approved matrix: `autoplan`, `writing-plans`, `subagent-driven-development`, `review`, `cso`, `qa`, `document-release`, `document-generate`, and `ship`. Preserve paths, URLs, phase names, tools, turns, timeouts, gates, and all non-client prose.

In `hermes_pipeline/data/phase-profiles/agent-skills/phases.yaml`, replace only explicit product wording with `{agent_product}`. Keep every `agent-skills:*` reference prefix-neutral because its client-specific invocation syntax is `Unverified`.

- [ ] **Step 5: Add a complete bundled rendering snapshot matrix**

Append parameterized coverage to `tests/test_phases.py`:

```python
@pytest.mark.parametrize("profile", ["gstack", "agent-skills"])
@pytest.mark.parametrize(
    ("client", "product", "forbidden_product"),
    [
        ("claude", "Claude Code", "Codex"),
        ("codex", "Codex", "Claude Code"),
    ],
)
def test_every_bundled_phase_renders_for_client(
    profile, client, product, forbidden_product
):
    phases = load_phases(resolve_profile_phases_path(profile))
    for phase in phases:
        rendered = _render_phase_prompt(
            phase.prompt,
            todo_id="TODO-41",
            tick_id="01CLIENT",
            project_slug="demo",
            prompt_client=client,
            template_source=f"{profile}:{phase.phase_key}",
        )
        assert "{agent_product}" not in rendered
        assert "{skill_prefix}" not in rendered
        if "agent_product" in phase.prompt:
            assert product in rendered
            assert forbidden_product not in rendered


@pytest.mark.parametrize(
    ("client", "ordinary", "possessive"),
    [
        ("claude", "Use the /autoplan skill in Claude Code.", "Follow /review's fix loop."),
        ("codex", "Use the $autoplan skill in Codex.", "Follow $review's fix loop."),
    ],
)
def test_gstack_exact_client_grammar(client, ordinary, possessive):
    assert ordinary in _render_phase_prompt(
        "Use the {skill_prefix}autoplan skill in {agent_product}.",
        todo_id="TODO-41", tick_id="01CLIENT", project_slug="demo",
        prompt_client=client,
    )
    assert possessive in _render_phase_prompt(
        "Follow {skill_prefix}review's fix loop.",
        todo_id="TODO-41", tick_id="01CLIENT", project_slug="demo",
        prompt_client=client,
    )
```

Update existing assertions that hard-code `/ship` so they assert the shared template field and exact per-client rendered forms.

- [ ] **Step 6: Run focused renderer/profile tests**

Run:

```bash
rtk uv run pytest tests/test_phases.py tests/test_phases_package_resolution.py \
  tests/test_profile_layout_split.py -q
```

Expected: PASS for every phase/profile/client combination; malformed templates fail with source-aware errors.

- [ ] **Step 7: Commit strict rendering and shared templates**

```bash
git add hermes_pipeline/phases.py \
  hermes_pipeline/data/phase-profiles/gstack/phases.yaml \
  hermes_pipeline/data/phase-profiles/agent-skills/phases.yaml \
  tests/test_phases.py
git commit -m "feat(TODO-41): render strict client-aware phase prompts"
```

### Task 3: Pure Preparation and External Task Creation

**Files:**
- Modify: `hermes_pipeline/kanban_tasks.py`
- Modify: `tests/test_kanban_tasks.py`

**Interfaces:**
- Consumes: `_render_phase_prompt(..., prompt_client, template_source)`, `load_phases(phases_path)`, and existing kanban CLI behavior.
- Produces: `PreparedPhaseTask`, `prepare_todo_phases(...) -> list[PreparedPhaseTask]`, `create_prepared_todo_phases(...) -> list[str]`, and backward-compatible `register_todo_phases(..., prompt_client="claude") -> list[str]`.

- [ ] **Step 1: Add failing tests for all-before-any preparation**

Add to `tests/test_kanban_tasks.py`:

```python
def test_prepare_todo_phases_renders_all_without_external_calls(tmp_path, mocker):
    from hermes_pipeline.kanban_tasks import prepare_todo_phases

    phases_path = tmp_path / "phases.yaml"
    phases_path.write_text(
        "phases:\n"
        "  - phase_key: phase_1\n"
        "    name: One\n"
        "    prompt: 'Use {skill_prefix}review in {agent_product}.'\n"
        "    tools: Read\n"
        "    turns: 5\n"
    )
    run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
    prepared = prepare_todo_phases(
        todo_id="TODO-41",
        tick_id="01CLIENT",
        board_slug="demo",
        project_dir=tmp_path,
        phases_path=phases_path,
        prompt_client="codex",
    )
    run.assert_not_called()
    assert len(prepared) == 1
    assert "Use $review in Codex." in prepared[0].body


def test_late_render_failure_creates_zero_tasks(tmp_path, mocker):
    from hermes_pipeline.kanban_tasks import prepare_todo_phases
    from hermes_pipeline.phases import PhasePromptRenderError

    phases_path = tmp_path / "phases.yaml"
    phases_path.write_text(
        "phases:\n"
        "  - phase_key: phase_1\n"
        "    name: One\n"
        "    prompt: valid\n"
        "    tools: Read\n"
        "    turns: 5\n"
        "  - phase_key: phase_2\n"
        "    name: Two\n"
        "    prompt: '{unknown}'\n"
        "    tools: Read\n"
        "    turns: 5\n"
    )
    run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
    with pytest.raises(PhasePromptRenderError, match=r"phase_2.*unknown"):
        prepare_todo_phases(
            todo_id="TODO-41",
            tick_id="01CLIENT",
            board_slug="demo",
            project_dir=tmp_path,
            phases_path=phases_path,
        )
    run.assert_not_called()
```

- [ ] **Step 2: Run the preparation tests and confirm the API is absent**

Run:

```bash
rtk uv run pytest tests/test_kanban_tasks.py -q
```

Expected: FAIL because `prepare_todo_phases` and `PreparedPhaseTask` do not exist.

- [ ] **Step 3: Extract immutable prepared tasks without changing command semantics**

In `hermes_pipeline/kanban_tasks.py`, add:

```python
from .config import PromptClient


@dataclass(frozen=True)
class PreparedPhaseTask:
    phase_key: str
    name: str
    body: str
    tools: str
    turns: int
    gate: bool


def prepare_todo_phases(
    *,
    todo_id: str,
    tick_id: str,
    board_slug: str,
    project_dir: str | Path,
    phases_path: str | Path | None = None,
    prompt_client: PromptClient = "claude",
) -> list[PreparedPhaseTask]:
    if not re.fullmatch(r"TODO-\d+", todo_id):
        raise ValueError(f"invalid todo_id format: {todo_id!r} (expected TODO-N)")
    phases = load_phases(phases_path)
    return [
        PreparedPhaseTask(
            phase_key=phase.phase_key,
            name=phase.name,
            body=(
                _build_json_header(
                    tick_id=tick_id,
                    phase_key=phase.phase_key,
                    todo_id=todo_id,
                    project_slug=board_slug,
                )
                + "\n"
                + _render_phase_prompt(
                    phase.prompt,
                    todo_id=todo_id,
                    tick_id=tick_id,
                    project_slug=board_slug,
                    prompt_client=prompt_client,
                    template_source=(
                        f"{phases_path or 'gstack'}:{phase.phase_key}"
                    ),
                )
            ),
            tools=phase.tools,
            turns=phase.turns,
            gate=phase.gate,
        )
        for phase in phases
    ]
```

Move the existing subprocess loop, parent/gate flags, cleanup, JSON parsing, and expected-phases sentinel into:

```python
def create_prepared_todo_phases(
    *,
    prepared: list[PreparedPhaseTask],
    tick_id: str,
    board_slug: str,
    project_dir: str | Path,
    assignee: str = "default",
) -> list[str]:
    ...
```

The loop must consume `prepared` only; it must not load or render templates. Preserve existing `--parent`, gate blocking, `--goal`, idempotency key, archive-on-create-failure, and sentinel behavior exactly.

Keep the public compatibility wrapper:

```python
def register_todo_phases(
    *,
    todo_id: str,
    tick_id: str,
    board_slug: str,
    project_dir: str | Path,
    phases_path: str | Path | None = None,
    assignee: str = "default",
    prompt_client: PromptClient = "claude",
) -> list[str]:
    prepared = prepare_todo_phases(
        todo_id=todo_id,
        tick_id=tick_id,
        board_slug=board_slug,
        project_dir=project_dir,
        phases_path=phases_path,
        prompt_client=prompt_client,
    )
    return create_prepared_todo_phases(
        prepared=prepared,
        tick_id=tick_id,
        board_slug=board_slug,
        project_dir=project_dir,
        assignee=assignee,
    )
```

- [ ] **Step 4: Add create-phase compatibility assertions**

Add tests that construct two `PreparedPhaseTask` values directly, call `create_prepared_todo_phases()`, and assert:

```python
assert mock_run.call_count == 2
assert "--parent" not in mock_run.call_args_list[0].args[0]
assert "--parent" in mock_run.call_args_list[1].args[0]
assert "--body" in mock_run.call_args_list[0].args[0]
assert "already rendered $body" in mock_run.call_args_list[0].args[0]
```

Retain and run every existing `register_todo_phases()` test so gate, cleanup, parsing, and sentinel behavior remain covered through the wrapper.

- [ ] **Step 5: Run the complete kanban task suite**

Run:

```bash
rtk uv run pytest tests/test_kanban_tasks.py tests/test_kanban_tasks_legacy.py \
  tests/test_kanban_json_parse.py -q
```

Expected: PASS; late template failure performs no subprocess call, while task-creation failures still archive earlier external tasks.

- [ ] **Step 6: Commit the preparation boundary**

```bash
git add hermes_pipeline/kanban_tasks.py tests/test_kanban_tasks.py
git commit -m "refactor(TODO-41): prepare phase tasks before registration"
```

### Task 4: Production and Harness Propagation

**Files:**
- Modify: `hermes_pipeline/cli.py`
- Modify: `hermes_pipeline/harness.py`
- Modify: `tests/test_tick_contract.py`
- Modify: `tests/test_harness.py`

**Interfaces:**
- Consumes: `Config.prompt_client`, `resolve_profile_phases_path(contract.profile)`, `prepare_todo_phases()`, and `create_prepared_todo_phases()`.
- Produces: production ordering `prepare -> persist tick -> create`, plus `_poll_kanban_phases(..., prompt_client="claude")` and `run_harness(config=None)` compatibility.

- [ ] **Step 1: Add a failing production test for selected profile/client and mutation order**

In `tests/test_tick_contract.py`, add an integration-style test using the module’s existing `_tick_project` fixture helpers. Patch `prepare_todo_phases`, `_persist_tick_id`, and `create_prepared_todo_phases` to append to one `events` list:

```python
events: list[tuple[str, object]] = []

prepare = mocker.patch(
    "hermes_pipeline.kanban_tasks.prepare_todo_phases",
    side_effect=lambda **kwargs: events.append(("prepare", kwargs)) or ["prepared"],
)
persist = mocker.patch(
    "hermes_pipeline.cli._persist_tick_id",
    side_effect=lambda *args, **kwargs: events.append(("persist", kwargs)),
)
create = mocker.patch(
    "hermes_pipeline.kanban_tasks.create_prepared_todo_phases",
    side_effect=lambda **kwargs: events.append(("create", kwargs)) or ["task-1"],
)
```

Create `.hermes/pipeline.toml` with `profile = "agent-skills"`, call the tick with `Config(prompt_client="codex")`, then assert:

```python
assert [name for name, _ in events] == ["prepare", "persist", "create"]
prepare_kwargs = prepare.call_args.kwargs
assert prepare_kwargs["prompt_client"] == "codex"
assert "agent-skills" in str(prepare_kwargs["phases_path"])
```

Use the existing selection and capability mocks so the test reaches registration without invoking Hermes.

- [ ] **Step 2: Add a failing production preparation-error regression**

Add a sibling test where `prepare_todo_phases` raises `PhasePromptRenderError("agent-skills:phase_7_ship: unknown field")`. Assert:

```python
persist.assert_not_called()
create.assert_not_called()
assert not (project_state / "current_tick_id.txt").exists()
outcomes = read_outcomes(project_state)
assert outcomes[-1].outcome == "failed_to_spawn"
assert outcomes[-1].tick_id == tick_id
```

Invoke `_tick_project()` again with successful preparation and assert it reaches creation, proving the failed attempt did not leave duplicate-registration state.

- [ ] **Step 3: Reorder production registration around the pure preparation pass**

In `hermes_pipeline/cli.py`, keep the resolved path from contract loading:

```python
phases_path = resolve_profile_phases_path(contract.profile)
phases = load_phases(phases_path)
```

For the missing-contract fallback, resolve the default gstack path once and assign it to the same `phases_path` variable.

At the current registration site, replace direct registration with:

```python
from .kanban_tasks import (
    create_prepared_todo_phases,
    prepare_todo_phases,
)
from .phases import PhasePromptRenderError

try:
    prepared = prepare_todo_phases(
        todo_id=picked,
        tick_id=tick_id,
        board_slug=project_slug,
        project_dir=project_dir,
        phases_path=phases_path,
        prompt_client=config.prompt_client,
    )
except PhasePromptRenderError as exc:
    append_outcome(
        project_state,
        tick_id,
        outcome="failed_to_spawn",
        detail={"todo_id": picked, "error": str(exc)[:500]},
    )
    log.error("project %s: phase prompt preparation failed: %s", project_slug, exc)
    return

_persist_tick_id(project_state, tick_id)
task_ids = create_prepared_todo_phases(
    prepared=prepared,
    tick_id=tick_id,
    board_slug=project_slug,
    project_dir=project_dir,
    assignee=contract.assignee,
)
```

Preserve `_persist_tick_id()` error handling. Its default
`write_sentinel=True` writes both `current_tick_id.txt` and the existing
`tick_started` outcome before returning. The invariant is that preparation
occurs first, `_persist_tick_id()` runs immediately before the first Hermes
create, and create failures retain the existing `failed_to_spawn` handling.

- [ ] **Step 4: Add failing harness propagation tests**

In `tests/test_harness.py`, extend the existing register-and-poll tests:

```python
def test_poll_kanban_phases_passes_prompt_client(tmp_path, mocker):
    register = mocker.patch(
        "hermes_pipeline.kanban_tasks.register_todo_phases",
        return_value=["task-1"],
    )
    # Reuse the module's terminal-status mocks and monitor fixture.
    _poll_kanban_phases(
        project_slug="demo",
        tick_id="01CLIENT",
        state_dir=tmp_path / ".hermes",
        todo_id="TODO-41",
        project_dir=tmp_path,
        phases_path=None,
        monitor=monitor,
        detector=detector,
        prompt_client="codex",
        poll_interval=0,
    )
    assert register.call_args.kwargs["prompt_client"] == "codex"
```

Add two `run_harness` assertions using the module’s existing bootstrap mocks:

```python
@pytest.mark.parametrize(
    ("config", "expected"),
    [(None, "claude"), (Config(prompt_client="codex"), "codex")],
)
def test_run_harness_resolves_prompt_client_once(config, expected, mocker):
    poll = mocker.patch(
        "hermes_pipeline.harness._poll_kanban_phases",
        return_value=True,
    )
    # Invoke run_harness with the existing fixture setup.
    assert poll.call_args.kwargs["prompt_client"] == expected
```

Extend the filtered `phase_only` test to assert the selected client is retained alongside the temporary filtered `phases_path`.

- [ ] **Step 5: Propagate the resolved harness value explicitly**

In `hermes_pipeline/harness.py`, add the compatibility default to `_poll_kanban_phases`:

```python
prompt_client: PromptClient = "claude",
```

Pass it to `register_todo_phases(prompt_client=prompt_client)`.

At the start of `run_harness()` resolve:

```python
prompt_client = getattr(config, "prompt_client", "claude")
```

Pass that exact value to `_poll_kanban_phases()` for both full-profile and filtered-profile runs. Do not reread global config inside the poller.

- [ ] **Step 6: Run production and harness focused tests**

Run:

```bash
rtk uv run pytest tests/test_tick_contract.py tests/test_harness.py \
  tests/test_harness_e2e.py tests/test_kanban_tasks.py -q
```

Expected: PASS; production uses `agent-skills` when the contract selects it, preparation failure leaves no current tick or Hermes task, and harness defaults to Claude only when the supplied config lacks the field or is `None`.

- [ ] **Step 7: Commit explicit propagation and failure-safe ordering**

```bash
git add hermes_pipeline/cli.py hermes_pipeline/harness.py \
  tests/test_tick_contract.py tests/test_harness.py
git commit -m "feat(TODO-41): propagate prompt client through registration"
```

### Task 5: Structured Prerequisite Metadata and Doctor Diagnostics

**Files:**
- Create: `hermes_pipeline/data/phase-profiles/gstack/prerequisites.yaml`
- Create: `hermes_pipeline/data/phase-profiles/agent-skills/prerequisites.yaml`
- Modify: `hermes_pipeline/phases.py`
- Modify: `hermes_pipeline/cli.py`
- Modify: `tests/test_phases.py`
- Modify: `tests/test_skills_install.py`
- Modify: `tests/test_cli_contract.py`

**Interfaces:**
- Consumes: the approved profile/client prerequisite matrix and bundled phase prompts.
- Produces: `SkillPrerequisite`, `ProfilePrerequisites`, `load_profile_prerequisites(profile)`, and doctor warnings that distinguish `Conditional` from `Unverified`.

- [ ] **Step 1: Add failing metadata schema and coverage tests**

In `tests/test_phases.py`, add:

```python
def test_prerequisite_metadata_covers_every_bundled_skill_reference():
    for profile in ("gstack", "agent-skills"):
        metadata = load_profile_prerequisites(profile)
        declared = {item.skill_id for item in metadata.skills}
        phases = load_phases(resolve_profile_phases_path(profile))
        prompt_text = "\n".join(phase.prompt for phase in phases)
        for skill_id in declared:
            assert skill_id in prompt_text
        assert extract_bundled_skill_references(profile, phases) == declared


def test_gstack_prerequisites_are_conditional_and_verified():
    metadata = load_profile_prerequisites("gstack")
    assert {item.skill_id for item in metadata.skills} == {
        "autoplan",
        "writing-plans",
        "subagent-driven-development",
        "review",
        "cso",
        "qa",
        "document-release",
        "document-generate",
        "ship",
    }
    for item in metadata.skills:
        assert item.support == "Conditional"
        assert item.clients["claude"].discovery_root == ".claude/skills"
        assert item.clients["claude"].invocation == f"/{item.skill_id}"
        assert item.clients["codex"].discovery_root == ".agents/skills"
        assert item.clients["codex"].invocation == f"${item.skill_id}"


def test_agent_skills_prerequisites_do_not_guess_external_contracts():
    metadata = load_profile_prerequisites("agent-skills")
    for item in metadata.skills:
        assert item.support == "Unverified"
        assert item.clients["claude"].discovery_root is None
        assert item.clients["claude"].invocation is None
        assert item.clients["codex"].discovery_root is None
        assert item.clients["codex"].invocation is None
```

Implement `extract_bundled_skill_references()` as a test helper with explicit profile-owned patterns, not as a runtime regex rewriter.

- [ ] **Step 2: Run metadata tests and confirm package data is missing**

Run:

```bash
rtk uv run pytest tests/test_phases.py -q
```

Expected: FAIL because the metadata files and loader types do not exist.

- [ ] **Step 3: Add exact package-data records**

Create `gstack/prerequisites.yaml` with this shape for all nine approved skills:

```yaml
schema_version: 1
profile: gstack
skills:
  - skill_id: autoplan
    distribution_owner: gstack
    support: Conditional
    clients:
      claude:
        discovery_root: .claude/skills
        invocation: /autoplan
      codex:
        discovery_root: .agents/skills
        invocation: $autoplan
```

Use `distribution_owner: superpowers` for `writing-plans` and `subagent-driven-development`; use `gstack` for the other seven.

Create `agent-skills/prerequisites.yaml` with all nine approved namespaced IDs and:

```yaml
schema_version: 1
profile: agent-skills
skills:
  - skill_id: agent-skills:spec-driven-development
    distribution_owner: agent-skills plugin
    support: Unverified
    clients:
      claude:
        discovery_root:
        invocation:
      codex:
        discovery_root:
        invocation:
```

Repeat the complete design matrix; do not populate null fields with guesses.

- [ ] **Step 4: Implement validated metadata loading**

In `hermes_pipeline/phases.py`, add frozen dataclasses:

```python
@dataclass(frozen=True)
class ClientPrerequisite:
    discovery_root: str | None
    invocation: str | None


@dataclass(frozen=True)
class SkillPrerequisite:
    skill_id: str
    distribution_owner: str
    support: Literal["Conditional", "Unverified"]
    clients: dict[PromptClient, ClientPrerequisite]


@dataclass(frozen=True)
class ProfilePrerequisites:
    schema_version: int
    profile: str
    skills: tuple[SkillPrerequisite, ...]
```

Implement:

```python
def load_profile_prerequisites(profile: str) -> ProfilePrerequisites:
    phases_path = resolve_profile_phases_path(profile)
    path = phases_path.with_name("prerequisites.yaml")
    raw = yaml.safe_load(path.read_text())
    ...
```

Validate schema version `1`, exact profile match, unique non-empty skill IDs, exact clients `claude` and `codex`, support in the two-value set, non-null discovery/invocation for `Conditional`, and null discovery/invocation for `Unverified`. Raise `ValueError` naming the metadata path and offending field.

- [ ] **Step 5: Preserve package-owned installer scope**

In `tests/test_skills_install.py`, keep existing assertions that TPO’s own `todos-manager` skill installs into Claude and Codex discovery roots. Add one regression assertion that `load_profile_prerequisites("gstack")` does not change the installer’s source set and that no external `autoplan`, `review`, or `ship` directories are copied by `tpo skills install`.

- [ ] **Step 6: Add failing doctor diagnostics tests**

In `tests/test_cli_contract.py`, extend the existing `TestDoctor` fixtures and assert:

```python
def test_doctor_reports_global_prompt_client_scope(monkeypatch, tmp_path, capsys):
    # Create a valid gstack project contract and run doctor with codex config.
    assert _cmd_doctor(args, Config(projects_dir=tmp_path, prompt_client="codex")) == 0
    output = capsys.readouterr().out
    assert "prompt client: codex (global for all projects under projects_dir)" in output
    assert "separate project roots" in output
    assert "TODO-42" in output


def test_doctor_reports_conditional_prerequisites_without_local_failure(
    monkeypatch, tmp_path, capsys
):
    assert _cmd_doctor(args, Config(projects_dir=tmp_path, prompt_client="claude")) == 0
    output = capsys.readouterr().out
    assert "Conditional" in output
    assert ".claude/skills" in output


def test_doctor_marks_unverified_profile_unsupported(tmp_path, capsys):
    # Create a valid agent-skills contract.
    assert _cmd_doctor(args, Config(projects_dir=tmp_path, prompt_client="codex")) == 0
    output = capsys.readouterr().out
    assert "Unverified" in output
    assert "not advertised as supported" in output
```

The doctor command must not inspect or mutate a remote worker and must not fail a valid project solely because local external skills are absent.

- [ ] **Step 7: Render prerequisite diagnostics from structured metadata**

In `_cmd_doctor()` load `load_profile_prerequisites(contract.profile)` and print the selected client, global-scope invariant, and each prerequisite’s status/discovery information. For `Conditional`, state that worker provisioning is required; for `Unverified`, state that compatibility is unsupported pending evidence. Keep the command’s existing contract/capability exit semantics.

- [ ] **Step 8: Run metadata, installer, and doctor tests**

Run:

```bash
rtk uv run pytest tests/test_phases.py tests/test_skills_install.py \
  tests/test_cli_contract.py tests/test_tick_contract.py -q
```

Expected: PASS; every prompt reference and matrix row agree, package-owned installs remain scoped, and doctor communicates rather than enforcing remote discovery.

- [ ] **Step 9: Commit metadata and diagnostics**

```bash
git add hermes_pipeline/data/phase-profiles/gstack/prerequisites.yaml \
  hermes_pipeline/data/phase-profiles/agent-skills/prerequisites.yaml \
  hermes_pipeline/phases.py hermes_pipeline/cli.py \
  tests/test_phases.py tests/test_skills_install.py tests/test_cli_contract.py
git commit -m "feat(TODO-41): publish client skill prerequisites"
```

### Task 6: User Documentation and Release Qualification

**Files:**
- Modify: `README.md`
- Modify: `docs/reference-cli.md`
- Modify: `docs/howto-agent-skills-profile.md`
- Create: `docs/release-qualification-agent-clients.md`
- Create: `docs/release-evidence/agent-clients/README.md`
- Modify: `tests/test_phases.py`
- Create: `tests/test_docs_links.py`

**Interfaces:**
- Consumes: `Config.prompt_client`, `load_profile_prerequisites(profile)`, and the approved support/blocking semantics.
- Produces: executable configuration guidance, a metadata-validated prerequisite table, an auditable evidence schema for every `Conditional` pair, and a fragment-aware local Markdown link check.

- [ ] **Step 1: Add failing documentation/metadata agreement tests**

Add to `tests/test_phases.py`:

```python
def test_documented_prerequisite_rows_match_package_metadata():
    readme = Path("README.md").read_text()
    reference = Path("docs/reference-cli.md").read_text()
    for profile in ("gstack", "agent-skills"):
        metadata = load_profile_prerequisites(profile)
        for item in metadata.skills:
            row_key = f"`{profile}` | `{item.skill_id}`"
            assert row_key in readme
            assert row_key in reference


def test_release_qualification_covers_conditional_pairs():
    guide = Path("docs/release-qualification-agent-clients.md").read_text()
    for profile in ("gstack", "agent-skills"):
        for item in load_profile_prerequisites(profile).skills:
            if item.support != "Conditional":
                continue
            for client in ("claude", "codex"):
                assert f"`{profile}` / `{client}`" in guide
    assert "Normal CI does not run these checks" in guide
```

- [ ] **Step 2: Run the documentation tests and confirm the guidance is absent**

Run:

```bash
rtk uv run pytest tests/test_phases.py -q
```

Expected: FAIL because the matrix rows and release-qualification document do not yet exist.

- [ ] **Step 3: Document the installed-user configuration workflow**

Add this executable sequence to `README.md` and link to the CLI reference:

```bash
tpo config init
tpo config get prompt_client
tpo config set prompt_client codex
tpo config get prompt_client
tpo doctor <project>
```

State plainly:

- `prompt_client` changes prompt vocabulary only.
- `profile` chooses the bundled phase/skill workflow.
- the contract `assignee` selects the Hermes profile/agent identity.
- Hermes configuration chooses models and authentication.
- selecting a client does not install external skills.
- one global client covers every project under `projects_dir`; split roots for mixed fleets.

Render the exact profile/skill rows from the approved matrix using the same row key asserted by tests. Mark gstack rows `Conditional` and agent-skills rows `Unverified`; do not say both profiles are unconditionally supported.

- [ ] **Step 4: Extend CLI and profile reference documentation**

In `docs/reference-cli.md`, document:

```text
tpo config get prompt_client
tpo config set prompt_client claude
tpo config set prompt_client codex
```

Include exact accepted values, default/source behavior, case sensitivity, lack of an individual environment override, and the distinction table from Step 3.

In `docs/howto-agent-skills-profile.md`, state that namespaced invocation/discovery remains `Unverified` for both clients and that changing `prompt_client` does not promote support. Include remediation: use a verified profile/client pair, provide versioned qualification evidence, or keep the row unsupported.

- [ ] **Step 5: Define the release evidence protocol without fabricating results**

Create `docs/release-qualification-agent-clients.md` with one section for each `gstack / claude` and `gstack / codex` pair. Each section must require:

```markdown
### `gstack` / `claude`

- Environment prerequisites: exact client version, gstack version, superpowers version, clean discovery root, and no inherited project-local skill directory.
- Discovery command: a read-only command that lists the installed `SKILL.md` files beneath `.claude/skills`.
- Representative invocation: explicitly invoke `/autoplan` in a disposable fixture and require the client to identify and start the skill without an unknown-skill error.
- Evidence artifact: `docs/release-evidence/agent-clients/<release>/gstack-claude.md`.
- Required fields: UTC timestamp, OS, client version, distribution versions, install commands, discovery output, invocation transcript excerpt, result, and verifier.
- Blocking rule: a release advertising this Conditional pair is blocked when the current release has no passing artifact or the artifact records a failure.
```

Use the corresponding `.agents/skills` root and `$autoplan` invocation for Codex. State exactly: `Normal CI does not run these checks`; third-party credentials and installations are forbidden in hermetic CI. State that `Unverified` pairs are unsupported and non-blocking until authoritative evidence promotes their metadata.

Create `docs/release-evidence/agent-clients/README.md` with the required artifact fields and naming convention. Do not create a dated “passing” artifact unless the live commands were actually run and captured.

- [ ] **Step 6: Add a fragment-aware local Markdown link test**

Create `tests/test_docs_links.py`:

```python
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).parents[1]
DOCS = (
    ROOT / "README.md",
    ROOT / "docs" / "reference-cli.md",
    ROOT / "docs" / "howto-agent-skills-profile.md",
    ROOT / "docs" / "release-qualification-agent-clients.md",
    ROOT / "docs" / "release-evidence" / "agent-clients" / "README.md",
)
LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)


def _slug(heading: str) -> str:
    value = re.sub(r"<[^>]+>", "", heading).strip().lower()
    value = re.sub(r"[^\w -]", "", value)
    return re.sub(r"[\s-]+", "-", value)


@pytest.mark.parametrize("source", DOCS, ids=lambda path: str(path.relative_to(ROOT)))
def test_local_markdown_links_and_fragments_resolve(source: Path):
    failures: list[str] = []
    for raw_target in LINK_RE.findall(source.read_text()):
        target = raw_target.strip("<>")
        parsed = urlsplit(target)
        if parsed.scheme or target.startswith(("mailto:", "#")):
            continue
        destination = (source.parent / unquote(parsed.path)).resolve()
        if not destination.exists():
            failures.append(f"{target}: missing {destination.relative_to(ROOT)}")
            continue
        if parsed.fragment and destination.suffix.lower() == ".md":
            anchors = {_slug(value) for value in HEADING_RE.findall(destination.read_text())}
            if unquote(parsed.fragment) not in anchors:
                failures.append(f"{target}: missing fragment")
    assert not failures, "\n".join(failures)
```

Keep links to same-document headings covered by existing Markdown tooling; this focused regression validates every local cross-file path and fragment introduced or edited by TODO-41.

- [ ] **Step 7: Validate links, docs, and configuration walkthrough**

Run:

```bash
rtk uv run pytest tests/test_phases.py tests/test_config_cli.py \
  tests/test_cli_contract.py tests/test_docs_links.py -q
```

Expected: PASS; all local Markdown paths/fragments resolve and the CLI examples match tested behavior.

- [ ] **Step 8: Commit user and release documentation**

```bash
git add README.md docs/reference-cli.md docs/howto-agent-skills-profile.md \
  docs/release-qualification-agent-clients.md \
  docs/release-evidence/agent-clients/README.md tests/test_phases.py \
  tests/test_docs_links.py
git commit -m "docs(TODO-41): explain client prerequisites and qualification"
```

### Task 7: Full Regression and Release Readiness

**Files:**
- Modify only if verification exposes a TODO-41 regression in files already listed above.

**Interfaces:**
- Consumes: all APIs and documentation introduced by Tasks 1–6.
- Produces: a clean, reviewable implementation whose hermetic checks do not require external clients or credentials.

- [ ] **Step 1: Run all focused TODO-41 suites together**

```bash
rtk uv run pytest tests/test_config_loader.py tests/test_config_cli.py \
  tests/test_phases.py tests/test_phases_package_resolution.py \
  tests/test_kanban_tasks.py tests/test_kanban_tasks_legacy.py \
  tests/test_tick_contract.py tests/test_harness.py tests/test_harness_e2e.py \
  tests/test_skills_install.py tests/test_cli_contract.py tests/test_docs_links.py -q
```

Expected: PASS with every config/render/registration/propagation/prerequisite branch covered.

- [ ] **Step 2: Run the full provider-free test suite**

```bash
rtk uv run pytest --ignore=tests/eval
```

Expected: PASS; no test attempts to install or invoke gstack, superpowers, agent-skills, Claude Code, or Codex.

- [ ] **Step 3: Run Ruff**

```bash
rtk uv run ruff check .
```

Expected: PASS with no lint errors.

- [ ] **Step 4: Verify package contents include both metadata files**

```bash
rtk uv build
rtk uv run python -c "from importlib.resources import files; root = files('hermes_pipeline').joinpath('data', 'phase-profiles'); assert root.joinpath('gstack', 'prerequisites.yaml').is_file(); assert root.joinpath('agent-skills', 'prerequisites.yaml').is_file()"
```

Expected: the wheel/sdist build succeeds and both prerequisite sources resolve from installed package data.

- [ ] **Step 5: Verify diff hygiene and version scope**

```bash
rtk git diff --check
rtk git status --short
rtk git diff -- VERSION pyproject.toml uv.lock CHANGELOG.md
```

Expected: no whitespace errors; the four release-version files are unchanged because TODO-41 implementation does not itself perform a release bump.

- [ ] **Step 6: Route verification-driven fixes back to their owning task**

If Steps 1–5 expose a regression, return to the task that owns the affected
file, add the concrete regression test there, make the minimal fix, rerun that
task’s focused command, and use that task’s explicit commit file list and
message. Then repeat Tasks 7 Steps 1–5. If verification requires no changes,
do not create an empty commit.
