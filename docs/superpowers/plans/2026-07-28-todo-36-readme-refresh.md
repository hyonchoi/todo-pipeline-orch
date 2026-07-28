# TODO-36 README Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refresh README and the getting-started tutorial so installed-user onboarding is simple, direct, and consistent with current `tpo` and `todos-manager` behavior.

**Architecture:** This is a documentation-only change. `README.md` becomes the concise front door, `docs/tutorial-getting-started.md` becomes the detailed first-run companion, and `docs/pipeline-modularization-plan.md` is adjusted only where historical stale CLI names are presented as current usage.

**Tech Stack:** Markdown docs, shell verification with `rg`, `test -f`, and `uv run` where useful.

## Global Constraints

- Do not change CLI behavior.
- Do not change `tpo init` semantics; it writes `.hermes/pipeline.toml` only.
- Use installed-user examples as direct `tpo ...` commands.
- Use `uv run tpo ...` only when the text explicitly says source checkout or contributor usage.
- Do not add `--target agents`; valid skill install targets are `codex`, `claude`, and `all`.
- Do not require `hermes login` or provider authentication in README prerequisites.
- Mention Hermes agent/profile availability only as an operational pipeline prerequisite.
- Keep advanced topics in linked docs instead of expanding README inline.

---

### Task 1: Rewrite README as the concise front door

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: `docs/superpowers/specs/2026-07-28-todo-36-readme-refresh-design.md`
- Produces: A README structure that later tasks and verification can rely on:
  `## Overview`, `## Install`, `## Prerequisite setup`, `## Core workflows`,
  `## Subcommands`, `## Documentation`, `## Contributing`, `## License`.

- [ ] **Step 1: Replace the README body with the approved top-level structure**

Use this outline and keep the prose compact:

````markdown
# todo-pipeline-orchestrator

Pipeline watcher and TODOS manager orchestration toolkit, packaged as a uv-managed Python project. The installed CLI is `tpo`.

## Overview

`tpo` helps maintain schema-enforced `TODOS.md` files, install the bundled `todos-manager` skill, prepare pipeline contracts, run pipeline ticks, and approve ready TODOs for shipping. Pipeline phases run through Hermes agent profiles and use kanban tasks for phase scheduling.

Use this README as the quick map. Detailed setup, reference, and recovery guides live in the documentation index below.

## Install

Install `uv` if it is not already available:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Install the CLI as a uv tool:

```bash
uv tool install .
tpo --version
```

From a source checkout, use the project environment instead:

```bash
uv sync
uv run tpo --version
```
````

Then add `## Prerequisite setup`, `## Core workflows`, `## Subcommands`,
`## Documentation`, `## Contributing`, and `## License` sections as described
in later steps.

- [ ] **Step 2: Add the prerequisite setup section**

Use direct installed CLI commands:

````markdown
## Prerequisite setup

For pipeline phase execution, make sure a Hermes agent runtime/profile is available. Provider authentication is model-specific and is not a baseline `tpo` prerequisite.

Install the bundled pipeline profile when you want unattended pipeline phase execution:

```bash
tpo install-profile
```

Install `todos-manager` for the agent that will edit TODOs:

```bash
tpo skills install --target all
```

For project-local Codex/agents setup:

```bash
tpo skills install --scope project --target codex
```

Skill targets:

| Target | Installs to |
|---|---|
| `codex` | `.agents/skills` convention |
| `claude` | `.claude/skills` convention |
| `all` | both conventions |
````

- [ ] **Step 3: Add concise core workflows**

Use this content, preserving the distinction between `todos-manager --init` and
`tpo init`:

````markdown
## Core workflows

Configure the project scan directory:

```bash
tpo config init
tpo config set projects_dir ~/my-projects
```

Start a new project with no `TODOS.md`:

```bash
cd ~/my-projects/my-project
todos-manager --init
todos-manager --add
```

Adopt an existing hand-written `TODOS.md`:

```bash
cd ~/my-projects/my-project
todos-manager --convert
todos-manager --audit
```

Use `todos-manager --revise` for individual entries that need stronger fields.

Write or verify the pipeline contract for an existing project:

```bash
tpo init my-project
tpo doctor my-project
```

Run and ship:

```bash
tpo tick
tpo tick my-project
tpo approve my-project --todo TODO-5
```
````

- [ ] **Step 4: Add the subcommand summary table**

Use exactly these rows unless implementation discovers a current subcommand is missing:

```markdown
## Subcommands

| Subcommand | Purpose |
|---|---|
| `tick` | Discover configured projects, select eligible TODOs, and register pipeline phases. |
| `approve` | Ship a reviewed TODO through the approval/merge workflow. |
| `init` | Write `.hermes/pipeline.toml` for an existing project. |
| `doctor` | Verify a project's pipeline contract against the selected profile. |
| `recover-counter` | Rebuild legacy TODO counter compatibility state from tracked TODO IDs. |
| `install-profile` | Install or refresh the bundled pipeline Hermes profile. |
| `skills` | Install or remove the bundled `todos-manager` skill. |
| `config` | Read and write global `tpo` configuration. |
| `test` | Run the mock integration test harness. |

See [CLI reference](docs/reference-cli.md) for arguments, exit codes, and detailed behavior.
```

- [ ] **Step 5: Add grouped documentation links**

Move every useful current README doc link into one group. Use this grouped shape:

```markdown
## Documentation

### Start here

| Doc | Type | When to read |
|---|---|---|
| [Getting-started tutorial](docs/tutorial-getting-started.md) | Tutorial | First time using `tpo` end-to-end |
| [CLI reference](docs/reference-cli.md) | Reference | All subcommands, arguments, exit codes, and environment variables |

### TODO management

| Doc | Type | When to read |
|---|---|---|
| [TODOS Manager skill](hermes_pipeline/data/skills/todos-manager/SKILL.md) | Reference | TODOS.md schema, ID assignment, and subcommands |
| [Getting started with todos-manager](docs/tutorial-todos-manager.md) | Tutorial | Step-by-step TODO lifecycle walkthrough |
| [Manage TODOS.md with todos-manager](docs/howto-todos-manager.md) | How-to | Using `--init`, `--add`, `--convert`, `--audit`, `--archive`, `--list`, `--revise` |

### Pipeline setup and operation

| Doc | Type | When to read |
|---|---|---|
| [Run a manual tick](docs/howto-pipeline-tick.md) | How-to | Running `tpo tick` for iterative development |
| [Approve and ship a TODO](docs/howto-approve-and-ship.md) | How-to | Running `tpo approve` |
| [Set up the pipeline profile](docs/howto-pipeline-profile.md) | How-to | Installing the dedicated pipeline Hermes profile |
| [Debug ticks and recover counters](docs/howto-debugging-and-recovery.md) | How-to | Using `--verbose`, `--debug`, and `recover-counter` |
| [Handle phase 5 review outcomes](docs/howto-review-outcomes.md) | How-to | Inspecting review artifacts and reverted/timed-out reviews |

### Multi-project configuration

| Doc | Type | When to read |
|---|---|---|
| [Configure `.hermes/config.toml`](docs/howto-config-toml.md) | How-to | Tuning selection model or circuit-breaker thresholds |
| [Set up multiple projects](docs/howto-multi-project-setup.md) | How-to | Configuring per-project settings and the scan loop |
| [Multi-project scan tutorial](docs/tutorial-multi-project-scan.md) | Tutorial | Setting up two projects and running the scan loop |
| [How the scan loop works](docs/explanation-multi-project-scan.md) | Explanation | Global lock, migration decisions, and trade-offs |
| [Troubleshoot state migration](docs/howto-troubleshoot-state-migration.md) | How-to | Migration failed or skipped with multiple projects |

### Contracts, profiles, and adapters

| Doc | Type | When to read |
|---|---|---|
| [Configure the pipeline contract](docs/howto-pipeline-contract.md) | How-to | Editing assignee, fixing capability drift, schema migration |
| [Why the pipeline contract](docs/explanation-pipeline-contract.md) | Explanation | Design rationale for versioned contracts and capability gates |
| [Use the agent-skills profile](docs/howto-agent-skills-profile.md) | How-to | Selecting `gstack` or `agent-skills` pipeline phases |
| [Use the Hermes adapter](docs/howto-hermes-adapter.md) | How-to | How `hermes chat -q` routes LLM calls |
| [Selection seat contract](hermes_pipeline/decision/README.md) | Reference | Integrating with the Hermes config repo |

### Testing and harnesses

| Doc | Type | When to read |
|---|---|---|
| [Run the eval suite](docs/howto-eval-suite.md) | How-to | Before changing the prompt, model, or `decision/agent.py` |
| [Skill test environment](tests/skill-test-environment/README.md) | How-to | Running structural unit tests for `todos-manager` |
| [Skill test environment quickstart](docs/howto-skill-test-environment.md) | How-to | Adding and maintaining skill harness tests |
| [Skill test harness API](docs/reference-skill-test-harness.md) | Reference | Complete API for the skill test environment |
| [Why the skill test harness is pure-Python](docs/explanation-skill-test-harness-design.md) | Explanation | Golden-file harness design rationale |
| [Mock integration test harness](docs/howto-mock-integration-test-harness.md) | How-to | Running `tpo test` against mock project data |
| [Harness production-code coverage checklist](docs/checklist-harness-production-coverage.md) | Reference | Acceptance criteria for production-code path reuse |

### Architecture and reference

| Doc | Type | When to read |
|---|---|---|
| [Architecture overview](docs/ARCHITECTURE.md) | Explanation | Understanding lane structure, data flow, and phase execution |
| [Pipeline state machine](docs/hermes-state-machine.md) | Explanation | Understanding `.hermes/` file layout and transitions |
| [Modularization plan](docs/pipeline-modularization-plan.md) | Explanation | Historical architecture and design plan |
| [Kanban-as-Scheduler](docs/reference-kanban-as-scheduler.md) | Reference/Explanation | How `tpo tick` uses kanban for phase state and ordering |
| [Counter recovery](docs/reference-counter.md) | Reference/Explanation | How `recover_counter()` works |
| [Circuit breaker](docs/explanation-circuit-breaker.md) | Explanation | How no-progress tracking works and why it alerts |
| [Decision module API](docs/reference-decision-api.md) | Reference | Selection schemas, outcome sidecars, and plan-gate types |
```

- [ ] **Step 6: Preserve short Contributing and License sections**

Use:

```markdown
## Contributing

Found a bug or feature request? [Open an issue on GitHub](https://github.com/hyonchoi/todo-pipeline-orchestrator/issues).

## License

See LICENSE for details.
```

- [ ] **Step 7: Verify README checks**

Run:

```bash
rg -n "uv run tpo|hermes login|--target agents|tpo init|#configuration|#troubleshooting|#architecture" README.md
```

Expected:

- `uv run tpo` appears only in the source-checkout note.
- `hermes login` does not appear.
- `--target agents` does not appear.
- `tpo init` appears only in pipeline contract wording.
- No README links point at removed `#configuration`, `#troubleshooting`, or `#architecture` anchors.

- [ ] **Step 8: Commit README rewrite**

Run:

```bash
git add README.md
git commit -m "Rewrite README as tpo front door"
```

---

### Task 2: Revise the getting-started tutorial to match README

**Files:**
- Modify: `docs/tutorial-getting-started.md`

**Interfaces:**
- Consumes: README sections from Task 1.
- Produces: A first-run tutorial that uses canonical TODO initialization,
  direct installed `tpo ...` commands, and project contract wording for `tpo init`.

- [ ] **Step 1: Replace the prerequisites section**

Use this content near the top:

````markdown
## What you'll need

- Python 3.12+
- `uv`
- `tpo` installed as a uv tool, or a source checkout with `uv sync`
- A Hermes agent runtime/profile available when you run pipeline phases
- Write permissions on the git repositories you want `tpo` to scan

Provider authentication depends on the model/runtime configured for your Hermes profile. It is not required for every `tpo` installation path.
````

- [ ] **Step 2: Replace installation verification**

Use:

````markdown
## Step 1: Install and verify `tpo`

If `uv` is missing:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Install the CLI:

```bash
uv tool install .
tpo --version
```

From a source checkout:

```bash
uv sync
uv run tpo --version
```

Use direct `tpo ...` commands for the rest of this tutorial.
````

If the final package install source is not local `.` for release users, replace
`uv tool install .` with the current supported install source while preserving
the same structure.

- [ ] **Step 3: Add setup for profile and skill installation**

Use:

````markdown
## Step 2: Install the pipeline profile and TODO skill

Install the pipeline profile when you want `tpo` to register unattended phase tasks:

```bash
tpo install-profile
```

Install `todos-manager` for the agent that will edit TODOs:

```bash
tpo skills install --target all
```

For project-local Codex/agents setup, run this from the project root instead:

```bash
tpo skills install --scope project --target codex
```

`codex` installs to the `.agents/skills` convention, `claude` installs to the `.claude/skills` convention, and `all` installs both.
````

- [ ] **Step 4: Replace hand-written TODO creation with canonical onboarding**

Use this for the project setup section:

````markdown
## Step 3: Create or adopt a project

Create a demo project:

```bash
mkdir -p ~/my-projects/demo-app
cd ~/my-projects/demo-app
git init
```

For a new project with no `TODOS.md`, initialize the canonical TODO files:

```bash
todos-manager --init
todos-manager --add
```

Commit the generated TODO files and first entry:

```bash
git add TODOS.md TODOS-archive.md
git commit -m "init: create canonical TODOs"
```

For an existing project with a hand-written `TODOS.md`, use this path instead:

```bash
todos-manager --convert
todos-manager --audit
```

Use `todos-manager --revise` when a specific entry needs stronger required or optional fields.
````

- [ ] **Step 5: Rewrite config and contract steps**

Use direct `tpo` commands:

````markdown
## Step 4: Configure project discovery

Tell `tpo` where to find projects:

```bash
tpo config init
tpo config set projects_dir ~/my-projects
tpo config get projects_dir
```

## Step 5: Write and verify the pipeline contract

`tpo init <project>` writes `.hermes/pipeline.toml`. It does not create `TODOS.md`.

```bash
tpo init demo-app
tpo doctor demo-app
```

Expected doctor output starts with:

```text
OK: schema_version=1
```
````

- [ ] **Step 6: Rewrite tick, inspect, approve, and optional automation steps**

Use direct commands and keep details linked:

````markdown
## Step 6: Run a manual tick

```bash
tpo tick demo-app
```

The tick checks project state, selects eligible TODOs, and registers phase tasks. If nothing is ready, it reports that no TODO was picked.

To scan every active project under `projects_dir`:

```bash
tpo tick
```

See [Run a manual tick](howto-pipeline-tick.md) for detailed tick behavior and recovery.

## Step 7: Inspect phase progress

```bash
hermes kanban list --tenant demo-app
```

See [Kanban-as-Scheduler](reference-kanban-as-scheduler.md) for how phase tasks are chained.

## Step 8: Approve and ship

When all phases are complete and the TODO is ready to ship:

```bash
tpo approve demo-app --todo TODO-1
```

See [Approve and ship a TODO](howto-approve-and-ship.md) for guards, exit codes, and recovery.

## Step 9: Automate ticks later

Manual ticks are enough for first setup. For scheduled operation, see [Run a manual tick](howto-pipeline-tick.md) and the Hermes cron guidance in the operations docs.
````

- [ ] **Step 7: Replace next-step links that point at removed README anchors**

Use links to actual docs instead of `../README.md#configuration`,
`../README.md#troubleshooting`, or `../README.md#architecture`:

```markdown
## What you built

You now have:

- `tpo` installed and verified
- `todos-manager` installed for your agent target
- a canonical `TODOS.md`
- a configured `projects_dir`
- a pipeline contract in `.hermes/pipeline.toml`
- a manual tick path

## Next steps

- [CLI reference](reference-cli.md)
- [How to manage TODOS.md with todos-manager](howto-todos-manager.md)
- [How to run a manual tick](howto-pipeline-tick.md)
- [Pipeline contract setup](howto-pipeline-contract.md)
- [Architecture overview](ARCHITECTURE.md)
```

- [ ] **Step 8: Verify tutorial checks**

Run:

```bash
rg -n "uv run tpo|hermes login|--target agents|tpo init|cat > TODOS.md|\\.hermes/todo_id_counter|README.md#configuration|README.md#troubleshooting|README.md#architecture" docs/tutorial-getting-started.md
```

Expected:

- `uv run tpo` appears only in the source-checkout paragraph.
- `hermes login` does not appear unless clearly framed as provider-specific optional setup; prefer no occurrence.
- `--target agents` does not appear.
- `tpo init` is described as `.hermes/pipeline.toml` setup only.
- `cat > TODOS.md` and `.hermes/todo_id_counter` do not appear.
- Removed README anchors are not linked.

- [ ] **Step 9: Commit tutorial revision**

Run:

```bash
git add docs/tutorial-getting-started.md
git commit -m "Update getting-started tutorial for tpo onboarding"
```

---

### Task 3: Label stale modularization-plan CLI names as historical

**Files:**
- Modify: `docs/pipeline-modularization-plan.md`

**Interfaces:**
- Consumes: Current CLI entrypoints from `pyproject.toml` and README from Task 1.
- Produces: Historical plan wording that does not point readers at obsolete
  `pipeline-watch` usage as current instructions.

- [ ] **Step 1: Find stale CLI-name references**

Run:

```bash
rg -n "pipeline-watch|hermes-pipeline|pip install -e|bin/" docs/pipeline-modularization-plan.md
```

Expected current stale clusters include the package structure, `bin/pipeline-watch`,
cron example, execution checklist, and Lane F completion note.

- [ ] **Step 2: Add a historical-status note near the top**

Add this below the title:

```markdown
> Historical note: this plan records the original modularization design. The current installed CLI is `tpo`; legacy `pipeline-watch` and `hermes-pipeline` entrypoints are compatibility shims, not the preferred commands for new docs.
```

- [ ] **Step 3: Change current-sounding stale labels**

Make these narrow wording updates:

- Change `# Plan: Pipeline Modularization + TODOS Manager 스킬` only if desired; keeping the title is acceptable.
- Change `pipeline-watch           # CLI 엔트리포인트 (bash 스크립트)` to `pipeline-watch           # historical CLI entrypoint sketch; current CLI is tpo`.
- Change section heading `**\`bin/pipeline-watch\`:**` to `**Historical \`bin/pipeline-watch\` sketch:**`.
- Change `script="pipeline-watch --auto"` to a historical-note sentence after the code block: `Current scheduled usage should follow the tpo/Hermes cron docs linked from README.`
- Change `pip install -e ./hermes-pipeline` checklist wording to indicate historical local editable install, not current README guidance.
- In Lane F, change `Argparse subcommands (\`auto\`, \`merge\`, \`status\`)` to note these are historical names superseded by current `tpo` subcommands.

Do not rewrite the full Korean design plan.

- [ ] **Step 4: Verify stale references are labeled**

Run:

```bash
rg -n "pipeline-watch|hermes-pipeline|pip install -e|auto`, `merge`, `status`" docs/pipeline-modularization-plan.md
```

Expected: remaining matches are either package names, historical examples, compatibility-shim notes, or explicitly historical text.

- [ ] **Step 5: Commit stale-name labeling**

Run:

```bash
git add docs/pipeline-modularization-plan.md
git commit -m "Label historical CLI names in modularization plan"
```

---

### Task 4: Run documentation verification

**Files:**
- Modify: none expected
- Test: `README.md`, `docs/tutorial-getting-started.md`, `docs/pipeline-modularization-plan.md`

**Interfaces:**
- Consumes: Docs changed in Tasks 1-3.
- Produces: Verification evidence for final review.

- [ ] **Step 1: Run forbidden-pattern checks**

Run:

```bash
rg -n "hermes login|--target agents|cat > TODOS.md|\\.hermes/todo_id_counter|README.md#configuration|README.md#troubleshooting|README.md#architecture" README.md docs/tutorial-getting-started.md
```

Expected: no matches.

- [ ] **Step 2: Check `uv run tpo` context**

Run:

```bash
rg -n -C 2 "uv run tpo" README.md docs/tutorial-getting-started.md docs/reference-cli.md
```

Expected:

- README and tutorial matches, if any, are explicitly source-checkout usage.
- `docs/reference-cli.md` may still contain detailed source-checkout command examples because that file is out of scope for rewrite.

- [ ] **Step 3: Check `tpo init` context**

Run:

```bash
rg -n -C 2 "tpo init" README.md docs/tutorial-getting-started.md
```

Expected: every match describes `.hermes/pipeline.toml` or pipeline contract setup.

- [ ] **Step 4: Run a focused markdown link audit**

Run:

```bash
python - <<'PY'
from pathlib import Path
import re

files = [Path("README.md"), Path("docs/tutorial-getting-started.md")]
link_re = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
missing = []
for path in files:
    text = path.read_text()
    for raw in link_re.findall(text):
        if raw.startswith(("http://", "https://", "mailto:")):
            continue
        target = raw.split("#", 1)[0]
        if not target:
            continue
        resolved = (path.parent / target).resolve()
        if not resolved.exists():
            missing.append(f"{path}: missing {raw}")
if missing:
    print("\n".join(missing))
    raise SystemExit(1)
print("README/tutorial links resolve")
PY
```

Expected: `README/tutorial links resolve`.

- [ ] **Step 5: Run docs-sensitive unit checks**

Run:

```bash
uv run pytest tests/test_deprecation_shims.py tests/test_cli_contract.py tests/test_skills_install.py -q
```

Expected: all selected tests pass. These are not required because docs changed,
but they cheaply confirm referenced CLI compatibility shims, `init`, and skill
install flags still match the docs.

- [ ] **Step 6: Commit verification follow-up only if fixes were needed**

If verification required edits, commit them:

```bash
git add README.md docs/tutorial-getting-started.md docs/pipeline-modularization-plan.md
git commit -m "Fix TODO-36 documentation verification gaps"
```

If no edits were needed, do not create an empty commit.

---

### Task 5: Final review and TODO handoff

**Files:**
- Modify: none expected
- Test: git history and status

**Interfaces:**
- Consumes: Commits from Tasks 1-4.
- Produces: Final implementation summary and TODO-36 readiness evidence.

- [ ] **Step 1: Inspect final diff from the design base**

Run:

```bash
git --no-pager log --oneline -8
git --no-pager status --short
```

Expected: recent commits include the README rewrite, tutorial update, modularization-plan label, and any verification fix. Working tree should be clean unless there are intentional user changes.

- [ ] **Step 2: Summarize verification evidence**

Record the exact verification commands and pass/fail outcomes in the final response:

```text
rg forbidden-pattern check: pass
rg uv-run context check: pass
rg tpo-init context check: pass
README/tutorial link audit: pass
pytest CLI/doc-sensitive subset: pass
```

If any command failed and was not fixed, explain the residual risk plainly.

- [ ] **Step 3: Decide whether TODO-36 can be marked done**

Do not mark TODO-36 complete unless the user explicitly asks. Report that the
docs implementation is ready for review and list the commits.
