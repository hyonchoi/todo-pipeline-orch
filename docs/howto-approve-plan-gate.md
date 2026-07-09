# How to approve or reject a plan gate

The plan gate (`phase_2b_plan_gate`) is a human review checkpoint between
Autoplan and Writing Plan. When Autoplan finishes, it produces a decision
sheet — a structured JSON artifact extracted from the `## Decisions` section
of the plan markdown file. The gate blocks the pipeline until you approve or
reject the plan.

By the end of this guide, you'll know how to respond to a plan-gate Slack
alert, review the decisions, and unblock the pipeline.

## Prerequisites

- The pipeline is installed: `uv sync` (see [Getting Started](tutorial-getting-started.md#installation)).
- Hermes is installed and authenticated: `hermes login`.
- A plan gate is blocked on a TODO — you'll get a Slack alert like:
  ```:mag: my-project TODO-5 plan needs review — 3 decision(s) to approve. Run: pipeline-watch approve-plan my-project --todo TODO-5```

## When the plan gate fires

The plan gate is inserted automatically between `phase_2_autoplan` and
`phase_3_writing_plan`. It fires when:

1. Autoplan completes and produces a `## Decisions` section in the plan file.
2. The risk classifier determines the TODO is high-risk, or the project has
   prior rejection history.

High-risk signals: the TODO mentions dependencies, architecture changes,
security, data migrations, or broad-scope keywords (all, every, entire, global).

## Steps

### 1. Receive the alert

When pre-gate phases complete, the dispatcher fires a Slack notification:

```
:mag: my-project TODO-5 plan needs review — 3 decision(s) to approve.
Run: pipeline-watch approve-plan my-project --todo TODO-5
```

You can also check the gate status by looking at the kanban board for the
project — the plan-gate task will show as `blocked`.

### 2. Review the decision sheet

The decision sheet lives in `.hermes/decisions/` as `<tick_id>-plan.json`.
Find the tick_id for a TODO:

```bash
# Check the current tick
cat .hermes/current_tick_id.txt

# Or list decision sheets
ls .hermes/decisions/*-plan.json
```

Open the JSON file. Each decision has:
- `question_id` — unique identifier (e.g., `q1`)
- `classification` — one of `taste`, `premise`, `user-challenge`, `mechanical`
- `prompt` — the question being asked
- `options` — available choices with labels (`A`, `B`, ...)
- `recommendation` — Autoplan's recommended option
- `answer` — empty until you approve

### 3. Approve the plan

To accept all recommendations as-is:

```bash
uv run pipeline-watch approve-plan <project> --todo TODO-N --approve
```

This fills each `answer` field with the recommended option, persists the
sheet, and completes the gate task on kanban. The pipeline advances to
`phase_3_writing_plan`.

### 4. Approve with overrides

To accept the plan but change specific recommendations:

```bash
uv run pipeline-watch approve-plan <project> --todo TODO-N --approve \
    --override q1=B --override q3=A
```

Each `--override` takes `question_id=label` (e.g., `q1=B`). The override is
validated: the question ID must exist, and the label must be one of the
question's option labels.

### 5. Reject the plan

To reject the plan and halt the pipeline:

```bash
uv run pipeline-watch approve-plan <project> --todo TODO-N \
    --reject --reason "The approach does not address the performance constraint discussed in the design review."
```

Rejection writes a sidecar file (`<tick_id>-rejected.json`) and archives the
gate task on kanban. The pipeline does not advance to writing plan.

## Verification

After approving:
```bash
# Check kanban — the gate task should be done
hermes kanban list --tenant <project> --json
```

After rejecting:
```bash
# Check rejection sidecar exists
ls .hermes/decisions/*-rejected.json
```

## Troubleshooting

**"no plan-gate decision sheet or kanban task found for TODO-N"**
- Autoplan may not have finished. Run `pipeline-watch tick` for the project
  and wait for Autoplan to complete.
- The TODO may not be high-risk. The risk classifier gates only certain
  TODOs. Check the TODO description for high-risk keywords.

**"plan-gate for TODO-N is 'done', not 'blocked'"**
- The gate was already approved or rejected. Check the kanban board or
  rejection sidecar to see what happened.

**"override references unknown question"**
- The question ID (e.g., `q1`) must match one in the decision sheet. Open
  the sheet to see valid IDs: `cat .hermes/decisions/<tick_id>-plan.json`.

**"label must be one of [...]"**
- The override label (e.g., `B`) must match an option label for that
  question. Check the options in the decision sheet.

## Rejection history and automatic gating

When a project has any rejection history (a `-rejected.json` sidecar exists
with `rejection_count > 0`), the risk classifier treats all subsequent
TODOs as high-risk. This means future plans will hit the gate even if the
TODO doesn't mention high-risk keywords. The system learned from the
rejection and wants human eyes on the next plan too.

## See Also

- [Architecture overview](ARCHITECTURE.md) — Phase flow diagram showing the plan gate position
- [Pipeline state machine](hermes-state-machine.md) — Gate state transitions
- [Decision sheet schema](hermes_pipeline/decision/schema.py) — `DecisionSheet` / `DecisionQuestion` frozen dataclasses (source reference)
