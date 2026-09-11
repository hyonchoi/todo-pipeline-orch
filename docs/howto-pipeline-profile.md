# How to: Set Up the Pipeline Profile

This guide walks through setting up the dedicated pipeline agent profile for unattended kanban execution.

## Quick Start (Two Commands)

```bash
# 1. Initialize the project contract with the pipeline assignee
tpo init <project> --assignee pipeline

# 2. Install the bundled Hermes profile
tpo install-profile
```

## Step-by-Step Walkthrough

### Step 1: Initialize the Project Contract

```bash
tpo init myproject --assignee pipeline
```

Output:
```
Wrote pipeline execution contract: /path/to/myproject/.hermes/pipeline.toml
```

This creates a `pipeline.toml` with `assignee = "pipeline"` and capabilities derived from the current phases.yaml.

### Step 2: Install the Pipeline Profile

```bash
tpo install-profile
```

This clones your currently-active Hermes profile (`hermes profile create pipeline --clone`)
so the new `pipeline` profile inherits a working `config.yaml`, `.env`, and skills —
then overlays the bundled pipeline-specific `SOUL.md` on top.

Output:
```
Creating 'pipeline' profile cloned from the active profile...
Locating profile directory...
Pipeline profile installed successfully.

Next step: set the assignee in your project contract:
  tpo init <project> --assignee pipeline
Then verify with:
  tpo doctor <project>
```

### Step 3: Verify Everything is Wired

```bash
tpo doctor myproject
```

Output (success; prerequisite diagnostics appear before the final line):
```text
prompt client: <claude-or-codex> (global for all projects under projects_dir)
Prerequisites for profile 'native-sdd':
...
OK: schema_version=3 assignee=pipeline profile=native-sdd capabilities=['Bash', 'Edit', 'Read', 'Write']
```

The `DEPRECATED:` line appears only for a contract that selects `gstack` (or one
that declares no `profile` at all, which resolves to `gstack`). It is
informational and never changes the exit code — see
[Migrating from gstack](howto-native-sdd-profile.md#migrating-from-gstack).

If the selected profile has any `Unverified` prerequisite, `doctor` prints an
`UNSUPPORTED` result and exits 2 even when the contract itself is valid.

Output (missing profile):
```
MISSING: Hermes profile 'pipeline' is not installed, but contract assignee is set to 'pipeline'
Cause: The profile was never installed, or it was removed after install.
Fix: Install the bundled profile with `tpo install-profile`, or create a custom profile named 'pipeline' with `hermes profile create pipeline`.
```

### Step 4: Run a Tick

```bash
tpo tick myproject
```

The pipeline tick registers kanban phases with `--assignee pipeline`. Hermes routes the tasks to the installed profile, which runs with SOUL.md in context.

## Reinstalling After SOUL.md Changes

If you've edited the bundled SOUL.md and want to reinstall:

```bash
tpo install-profile --force
```

`--force` deletes the existing `pipeline` profile first, then re-clones from the
active profile and re-overlays SOUL.md. Without `--force`, `install-profile`
refuses to overwrite an existing `pipeline` profile.

## Custom Profiles (Escape Hatch)

The bundled profile is a default. To create a custom profile:

```bash
hermes profile create my-custom-profile --description "My custom pipeline agent"
# Edit SOUL.md at ~/.hermes/profiles/my-custom-profile/SOUL.md
tpo init myproject --assignee my-custom-profile
```

**Important:** SOUL.md is advisory — it shapes agent behavior through instructions, not enforcement. The pipeline execution contract's `capabilities` field gates tool access at tick start; `doctor` also hard-fails (exit 2) if a non-default `assignee`'s Hermes profile isn't installed or Hermes itself isn't on PATH.

## Phase keys and validation roles

A Hermes agent profile selects the worker environment. A pipeline phase profile
also declares the ordered workflow in `phases.yaml`. New schema-v6 run
registrations pin that complete workflow, so editing a profile affects future
runs. An existing v6 run resumes from its pinned definitions even if the current
profile has changed or become invalid. Keep `phase_key` as the phase's identity;
optional `role` metadata selects
special validation without changing that key. For example, these entries can
appear in a phase profile's `phases` list:

```yaml
- phase_key: build_feature
  role: implementation
  name: Implement the approved Plan
  prompt: Implement {todo_id} from {plan_path}.
  tools: Read,Write,Edit,Bash
  turns: 100
  timeout: 7200
- phase_key: inspect_changes
  role: review
  name: Review the changes
  prompt: Review and verify the changes for {todo_id}.
  tools: Read,Write,Edit,Bash
  turns: 30
  timeout: 2400
- phase_key: summarize_checks
  name: Summarize verification
  prompt: Summarize the verified changes for {todo_id}.
  tools: Read,Bash
  turns: 10
  timeout: 600
- phase_key: open_pull_request
  role: delivery
  name: Deliver the branch
  prompt: Open an unmerged pull request for {todo_id}.
  tools: Read,Bash
  turns: 30
  timeout: 1800
```

This illustrates identity and role metadata; write complete task, verification,
and delivery instructions for a production profile. The omitted role on
`summarize_checks` defaults to `worker`, which uses generic validation and has
no single-commit restriction. Existing clean-worktree and result-validation
requirements still apply. Each special role may appear at most once. TPO never
invents review or delivery phases when the profile omits them.

For a dynamically scheduled run, TPO releases workers in the pinned declaration
order only after their predecessors validate. Completion requires every declared
worker, including deferred cards not yet visible on the board. A new
manifest-bearing run requires exactly one reachable worker with
`role: implementation` to retain task checkpoints. Custom manifest profiles
that previously omitted roles must add that metadata before registering new
runs. Gate phases do not create workers; the human terminal boundary remains unchanged. Planless
profiles retain the static declared chain. The same supervisor launcher command
serves every phase: `--execution` selects the registered phase and prompt, the
supervisor runs and collects it, and TPO selects the next phase. Card headers,
execution records, results, and status keep keys such as `inspect_changes`;
retries use attempt generations instead of renaming phases.

A delivery phase may precede other workers. Issue closeout waits for authorized
evidence from every required worker, including those after delivery, and the
final HEAD must equal the delivered PR head. Completing the delivery card alone
cannot close the issue or authorize later changes to that head.

Supported registrations through schema v5 retain their original identities and
read protocol. In particular, legacy `review:0` and `finish` cards are not aliases
for keys in new registrations. Preserve registration and execution journals and
drain active v6 runs before downgrading to code that cannot read them.

## Exit Codes

**`install-profile`:**
| Exit | Meaning |
|------|---------|
| 0 | Cloned, SOUL.md overlaid, and verified |
| 1 | Bundled SOUL.md not found, `hermes profile show` failed, or SOUL.md copy failed |
| 2 | Hermes CLI not found on PATH, or `hermes profile create` failed (e.g. profile already exists — rerun with `--force`) |

**`doctor`:**
| Exit | Meaning |
|------|---------|
| 0 | Contract clean, profile verified (if non-default assignee) |
| 1 | Capability drift — contract missing tools phases.yaml requires |
| 2 | Contract missing/invalid, or assigned profile not installed, or Hermes not on PATH |

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `doctor` reports MISSING profile | Profile not installed | `tpo install-profile` |
| `doctor` reports MISSING (Hermes not on PATH) | Hermes CLI not installed | Install Hermes (https://hermos.dev) and ensure it's on PATH |
| `doctor` reports DRIFT | phases.yaml added a tool | Edit `pipeline.toml` capabilities, or `tpo init <project> --force` |
| Tasks not being picked up | Assignee doesn't match profile name | Ensure `assignee` in `pipeline.toml` matches `hermes profile list` name exactly |
| Profile installed but agent doesn't behave correctly | SOUL.md is advisory; model may not follow all instructions | Edit SOUL.md and reinstall with `--force` |
