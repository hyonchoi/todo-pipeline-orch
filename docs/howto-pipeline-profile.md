# How to: Set Up the Pipeline Profile

This guide walks through setting up the dedicated pipeline agent profile for unattended kanban execution.

## Quick Start (Two Commands)

```bash
# 1. Initialize the project contract with the pipeline assignee
pipeline-watch init <project> --assignee pipeline

# 2. Install the bundled Hermes profile
pipeline-watch install-profile
```

## Step-by-Step Walkthrough

### Step 1: Initialize the Project Contract

```bash
pipeline-watch init myproject --assignee pipeline
```

Output:
```
Wrote pipeline execution contract: /path/to/myproject/.hermes/pipeline.toml
```

This creates a `pipeline.toml` with `assignee = "pipeline"` and capabilities derived from the current phases.yaml.

### Step 2: Install the Pipeline Profile

```bash
pipeline-watch install-profile
```

Output:
```
Installing pipeline profile from /path/to/data/profiles/pipeline/...
Verifying profile installation...
Pipeline profile installed successfully.

Next step: set the assignee in your project contract:
  pipeline-watch init <project> --assignee pipeline
Then verify with:
  pipeline-watch doctor <project>
```

### Step 3: Verify Everything is Wired

```bash
pipeline-watch doctor myproject
```

Output (success):
```
OK: schema_version=1 assignee=pipeline capabilities=['Bash', 'Edit', 'Read', 'Write']
```

Output (missing profile):
```
MISSING: Hermes profile 'pipeline' is not installed, but contract assignee is set to 'pipeline'
Cause: The profile was never installed, or it was removed after install.
Fix: Install the bundled profile with `pipeline-watch install-profile`, or create a custom profile named 'pipeline' with `hermes profile create pipeline`.
```

### Step 4: Run a Tick

```bash
pipeline-watch tick myproject
```

The pipeline tick registers kanban phases with `--assignee pipeline`. Hermes routes the tasks to the installed profile, which runs with SOUL.md in context.

## Reinstalling After SOUL.md Changes

If you've edited the bundled SOUL.md and want to reinstall:

```bash
pipeline-watch install-profile --force
```

## Custom Profiles (Escape Hatch)

The bundled profile is a default. To create a custom profile:

```bash
hermes profile create my-custom-profile --description "My custom pipeline agent"
# Edit SOUL.md at ~/.hermes/profiles/my-custom-profile/SOUL.md
pipeline-watch init myproject --assignee my-custom-profile
```

**Important:** SOUL.md is advisory — it shapes agent behavior through instructions, not enforcement. The pipeline execution contract's `capabilities` field gates tool access at tick start; `doctor` also hard-fails (exit 2) if a non-default `assignee`'s Hermes profile isn't installed or Hermes itself isn't on PATH.

## Exit Codes

**`install-profile`:**
| Exit | Meaning |
|------|---------|
| 0 | Installed and verified |
| 1 | Bundled distribution not found, or `hermes profile install`/`show` failed |
| 2 | Hermes CLI not found on PATH |

**`doctor`:**
| Exit | Meaning |
|------|---------|
| 0 | Contract clean, profile verified (if non-default assignee) |
| 1 | Capability drift — contract missing tools phases.yaml requires |
| 2 | Contract missing/invalid, or assigned profile not installed, or Hermes not on PATH |

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `doctor` reports MISSING profile | Profile not installed | `pipeline-watch install-profile` |
| `doctor` reports MISSING (Hermes not on PATH) | Hermes CLI not installed | Install Hermes (https://hermos.dev) and ensure it's on PATH |
| `doctor` reports DRIFT | phases.yaml added a tool | Edit `pipeline.toml` capabilities, or `pipeline-watch init <project> --force` |
| Tasks not being picked up | Assignee doesn't match profile name | Ensure `assignee` in `pipeline.toml` matches `hermes profile list` name exactly |
| Profile installed but agent doesn't behave correctly | SOUL.md is advisory; model may not follow all instructions | Edit SOUL.md and reinstall with `--force` |
