---
status: APPROVED
---
# TODO-15: Design and Register Dedicated Hermes Profile for Pipeline Orchestrator

**Reframed (autoplan D1):** Define Pipeline Execution Contract — versioned TOML manifest for unattended kanban phase execution. Hermes profile registration deferred to TODO-16.

## Overview

Design a Hermes profile specifically matched to the pipeline orchestrator's needs, and provide a way to register it via `init` or a dedicated subcommand.

## Problem Statement

The "default" Hermes profile is a general-purpose chat profile — not optimized for unattended, goal-driven kanban task execution. Running the pipeline with a profile the operator customized for interactive use risks unexpected behavior.

## Proposed Solution

### Profile Shape

- **Profile name:** `"pipeline"` — short, distinct from operator profiles
- **Model:** Auto-pinned to the selection model (respects TODO-5's fallback ladder via `hermes fallback`)
- **Tools:** `Read`, `Write`, `Bash` — the core set needed for phase execution
- **Skills:** Attach gstack skills used by phases (autoplan, writing-plans, finishing-a-development-branch)
- **Safe-mode:** Enabled to prevent interactive prompts (no `input()`, no user-facing UI)

### Registration

Provide a `pipeline-watch init` or `pipeline-watch setup-profile` subcommand that calls `hermes profile create <name> --model ... --tools ... --skills ...` or `hermes profile install <dist-url>`. The subcommand should detect whether the profile already exists and skip if so. Run once during onboarding.

### Configuration

Profile definition should be versioned with the project. Store profile config in the repository for reproducibility.

## Implementation Steps

1. **Research Hermes profile API** — Verify `hermes profile create`, `hermes profile install`, and available flags
2. **Define profile schema** — Create a JSON/TOML profile definition file that can be versioned
3. **Implement `pipeline-watch init`** — New CLI subcommand that registers the profile
4. **Wire `register_todo_phases`** — Use the profile as default `--assignee` (connects to TODO-14)
5. **Add profile verification** — Validate profile exists and is functional before pipeline execution
6. **Update documentation** — Getting started guide, README updates

## Dependencies

- No hard dependencies (can be designed now)
- TODO-14 (kanban assignee config) will default to this profile once it ships
- TODO-5 (model fallback) — profile should respect fallback ladder

## Risks

- Profile schema changes in Hermes — need to keep pace
- Memory overhead of a second profile per task
- Operators who customize profiles may have different expectations

## Not In Scope

- Profile management UI
- Multi-profile routing per phase
- Profile health monitoring beyond verification

---

## GSTACK AUTOPLAN REVIEW

### Step 0A: Premise Challenge

**Premise 1: "Default profile causes unexpected behavior"**
- **Evaluation:** Partially valid. The default profile *could* have operator customizations (different model, interactive mode, personal tools) that don't match unattended execution needs. However, this is asserted without evidence. No data on actual failure modes from operators using the default profile.
- **Verdict:** Accept with refinement. The premise is reasonable for a P2 feature but needs concrete failure scenarios documented before implementation starts.

**Premise 2: "A dedicated Hermes profile solves the unattended execution problem"**
- **Evaluation:** Codex correctly flags this as potentially solving the wrong problem. A profile is an implementation detail. The durable boundary is a "pipeline execution contract" — a versioned manifest of required tools, behaviors, timeouts, and verification checks. The profile implements the contract, but the contract should be the product-level abstraction.
- **Verdict:** Reframe needed. TODO-15 should be about defining and verifying the execution contract, with the Hermes profile as one implementation.

**Premise 3: "Profile should be versioned with the project"**
- **Evaluation:** Correct. Versioning is essential for reproducibility and drift detection. This is a strong premise.

### Step 0B: Existing Code Leverage

| Sub-problem | Existing Code |
|-------------|---------------|
| Assignee configuration | `kanban_tasks.py:73` — `register_todo_phases(assignee="default")` |
| Config loading | `config.py` — `Config` dataclass with env var overrides |
| Project-level config | `project_config.py` — per-project `.hermes/project.toml` |
| Hermes CLI adapter | `hermes_adapter.py` — subprocess wrappers for Hermes commands |
| Circuit breaker | `circuit.py` — fallback behavior for consecutive failures |
| Phase config | `phases.yaml` — phase definitions with tool requirements |

Key insight: The project already has infrastructure for config overlays (`project.toml`), assignee wiring (`kanban_tasks.py:128`), and Hermes subprocess calls. TODO-15 can layer on existing patterns.

### Step 0C: Dream State Mapping

```
CURRENT STATE                     THIS PLAN                        12-MONTH IDEAL
Profile = whatever the operator   Project-versioned execution       Any agent runtime can
has configured. No verification.  contract + Hermes profile that    execute phases if they
Risk of drift, interactivity,     implements it. `pipeline-watch    meet the contract.
model mismatch.                   doctor` checks drift at tick.     Hermes is one adapter.
                                  Assignee config wired.                Contract is tested
                                                                        deterministically.
```

### Step 0C-bis: Implementation Alternatives

**APPROACH A: Profile-First (Plan as-is)**
- Summary: Create a dedicated Hermes profile via `hermes profile create`, register via `pipeline-watch init`. Minimal diff, uses existing Hermes profile API.
- Effort: S
- Risk: Low
- Pros: Ships fastest. Direct implementation of TODO-15 spec. Leverages Hermes features.
- Cons: Couples to Hermes profile API. Doesn't address the execution contract gap. Fragile if Hermes changes profile schema.
- Reuses: `hermes_adapter.py`, `project_config.py`, `kanban_tasks.py`

**APPROACH B: Contract-First (Codex Recommendation)**
- Summary: Define a versioned execution manifest (TOML) specifying required tools, behaviors, timeouts. Implement a drift checker (`pipeline-watch doctor`) that validates the active environment against the contract. Register a Hermes profile as one implementation of the contract.
- Effort: M
- Risk: Medium
- Pros: Decouples from Hermes API. Deterministically testable. Survives Hermes schema changes. Validates execution, not just configuration.
- Cons: More design work upfront. Requires contract schema design. Hermes profile registration is a follow-up step, not the primary deliverable.
- Reuses: `project_config.py` patterns, `config.py` TOML loading, test infrastructure

**APPROACH C: Profile + Contract Hybrid**
- Summary: Ship both: profile registration AND a minimal contract schema. Profile is the immediate value; contract is the durable boundary. Profile implements the contract.
- Effort: M
- Risk: Low
- Pros: Delivers immediate value (profile) while building durable abstraction (contract). Contract validates profile is correct.
- Cons: Slightly more scope than A. Requires defining contract schema alongside profile registration.
- Reuses: All of A + contract validation patterns

**RECOMMENDATION:** Choose **C** because it delivers immediate value while addressing Codex's valid concern about the execution contract. The contract is lightweight — a TOML file declaring minimum requirements — and the profile implements it.

### Step 0D: Mode-Specific Analysis (SELECTIVE EXPANSION)

**Complexity check:** Plan touches ~5 files: `cli.py` (new subcommand), `project_config.py` (config), `kanban_tasks.py` (assignee), `hermes_adapter.py` (profile verification), docs. Within bounds.

**Minimum set:** Steps 1-5 achieve the goal. Step 6 (docs) is polish, not core functionality.

**Expansion scan:**
1. Drift detection: `pipeline-watch doctor` — read-only check of profile vs contract. Effort: S. Risk: Low.
2. Contract schema: Versioned TOML manifest. Effort: M. Risk: Medium.
3. Profile verification: Validate profile works before tick. Effort: S. Risk: Low. (Already in plan as Step 5)

**Cherry-pick auto-decisions:**
- Drift detection: **Include** (P2 — in blast radius, cheap insurance)
- Contract schema: **Include** (P1 — addresses Codex's fundamental concern)
- Profile verification: Already in plan.

### Step 0E: Temporal Interrogation

```
HOUR 1 (foundations):     What Hermes profile API actually supports?
                          Does `hermes profile create` exist with the right flags?
HOUR 2-3 (core logic):    Contract schema design — what fields are essential?
                          Profile-to-contract mapping — how to implement?
HOUR 4-5 (integration):   Assignee wiring — does `kanban_tasks.py` accept profile names?
                          Profile verification — how to test without running a phase?
HOUR 6+ (polish/tests):   Drift detection — what constitutes drift?
                          Test fixtures — need mock Hermes for profile operations.
```

Critical decisions to resolve now:
1. Does Hermes support `profile create` with tool restrictions? Verify before starting.
2. Contract schema — keep it minimal (tools, model, safe-mode) or comprehensive?
3. How to verify profile without running a phase? Smoke test? Capability check?

### Step 0F: Mode Selection

Auto-decided: **SELECTIVE EXPANSION** — Feature enhancement on existing system. Hold scope with cherry-pick expansions.

---

## CEO DUAL VOICES — CONSENSUS TABLE

| Dimension | Claude Subagent | Codex | Consensus |
|-----------|----------------|-------|-----------|
| 1. Premises valid? | Partial — needs evidence | Under-proven | DISAGREE — Codex wants contract-first, Claude accepts with refinement |
| 2. Right problem to solve? | Yes, but narrow | Reframe to execution contract | DISAGREE |
| 3. Scope calibration correct? | Slightly narrow | Missing verification | CONFIRMED — both say expand scope |
| 4. Alternatives sufficiently explored? | No | Contract-first alternative dismissed | CONFIRMED |
| 5. Competitive/market risks covered? | N/A (internal tool) | Trust/traceability missing | CONFIRMED — needs traceability |
| 6. 6-month trajectory sound? | Fragile if Hermes changes | Durable boundary needed | CONFIRMED — contract approach |

### CODEX SAYS (CEO — Strategy Challenge)

1. Core premise under-proven — no evidence of actual failure modes.
2. Reframe to execution contract, not profile registration.
3. Tool set dangerously broad — needs sandbox boundaries.
4. gstack skill coupling ages badly.
5. Safe-mode is vague — needs measurable criteria.
6. Registration strategy backwards — verify before install.
7. "Skip if exists" is weak — needs version/hash comparison.
8. Dependency claim inaccurate — depends on TODO-14/5 semantics.
9. Missing trust/traceability — operators need inspectable traces.
10. Dismissed alternative: project-owned executor spec behind Hermes.

### CLAUDE SUBAGENT (CEO — Strategic Independence)

1. **[Critical] Core problem misidentified** — Plan assumes `hermes profile create` with tool/skill constraints exists. Neither command is in the codebase. Step 1 is "Research Hermes profile API" — should have been done before writing the plan. If the API doesn't exist, the entire approach is wrong.
2. **[High] Value capture poor relative to effort** — Phases already specify `tools` in `phases.yaml`. Each phase is a non-interactive `hermes chat -q` call with `--goal --goal-max-turns`. The "unexpected behavior" risk is already contained. A profile adds indirection with marginal value.
3. **[High] 6-month regret** — Profile schema drift, deprecation, or multi-tenant needs will make a single "pipeline" profile insufficient. Build on a substrate you control (versioned config file), not one you don't (Hermes profile).
4. **[Medium] Alternatives dismissed** — Inline execution policy (flags per `hermes chat`), environment isolation (dedicated `.hermes` subdir), or deferral with TODO-14-only were not analyzed.
5. **[Medium] Scope wrong** — Over-scoped on registration subcommand, under-scoped on integration path. Doesn't describe how profile settings flow to `hermes kanban create` dispatch.
6. **[Medium] "No hard dependencies" is incorrect** — Hard dependency on Hermes profile API capability.

**Consensus with Codex:** Both voices agree the problem is real but the solution (Hermes profile) is premature. Both recommend a versioned config-file approach as the durable abstraction, with profile registration as an optional implementation. This is a **User Challenge** — both models recommend reframing TODO-15 from "register a profile" to "define pipeline execution config."

---

## Sections 1-10 Review (Auto-Decided)

### Section 1: Architecture Review

**Dependency Graph (Before → After):**

```
BEFORE:                                    AFTER:
┌─────────────┐    hermes CLI    ┌────────┐  ┌─────────────┐   TOML    ┌─────────────┐
│  pipeline-  │ ───────────────▶ │Hermes  │  │  pipeline-  │─────────▶│Execution   │
│  watch CLI  │                  │Profile │  │  watch CLI  │          │Contract    │
└─────────────┘                  └────────┘  └──────┬──────┘   verify  └─────────────┘
                                                    │                    │
                                                    │ hermes CLI         │
                                                    ▼                    ▼
                                           ┌────────────────┐   ┌─────────────┐
                                           │ Hermes Profile  │   │ Profile Reg │
                                           │  ("pipeline")   │◀──│ & Verify    │
                                           └────────────────┘   └─────────────┘
```

**Findings:**
1. **Coupling to Hermes profile API** — Plan directly depends on `hermes profile create/install`. If Hermes changes schema, the orchestrator breaks. Auto-decided: Add contract layer (Approach C) to decouple. Principle P5 (explicit over clever).
2. **No rollback procedure** — If profile registration fails mid-tick, what happens? Auto-decided: Registration is idempotent (skip if exists + hash match). Profile verification at tick start catches drift. Principle P1 (completeness).

**Auto-decision:** Approach C (hybrid) — contract + profile. Classification: Taste. Principle: P5 (explicit) + P1 (completeness).

### Section 2: Error & Rescue Map

```
METHOD/CODEPATH           | WHAT CAN GO WRONG       | EXCEPTION CLASS
--------------------------|-------------------------|-----------------
pipeline-watch init       | Hermes CLI not found    | FileNotFoundError
                          | Profile create fails    | subprocess.CalledProcessError
                          | Profile exists but stale| VersionMismatch (new)
                          | Contract schema invalid | SchemaValidationError
pipeline-watch doctor     | Profile not found       | ProfileNotFound
                          | Contract drift detected | DriftDetected
                          | Hermes unreachable      | ConnectionError
register_todo_phases      | Assignee profile missing| ProfileNotFound
                          | Profile capabilities    | CapabilityMismatch
                          | insufficient            |

EXCEPTION                  | RESCUED? | RESCUE ACTION        | USER SEES
---------------------------|----------|----------------------|------------------
FileNotFoundError          | Y        | Error message        | "Hermes CLI not"
CalledProcessError         | Y        | Log stderr, exit     | "Profile creation"
VersionMismatch            | N ← GAP  | —                    | Nothing ← BAD
SchemaValidationError      | Y        | Print validation     | "Contract schema"
ProfileNotFound            | Y        | Skip, log warning    | "Profile not"
DriftDetected              | Y        | Log warning          | "Profile drift"
CapabilityMismatch         | N ← GAP  | —                    | Nothing ← BAD
```

**GAPS:**
1. `VersionMismatch` — When profile exists but hash differs: need to prompt for update or auto-migrate. Auto-decided: Log warning, continue execution. Operator decides when to update. Principle P3 (pragmatic).
2. `CapabilityMismatch` — When profile doesn't have required tools: block execution with clear error. Auto-decided: Fail loudly at tick start. Principle P1 (zero silent failures).

### Section 3: Security & Threat Model

| Threat | Likelihood | Impact | Mitigation |
|--------|-----------|--------|------------|
| Profile grants excessive tool access | High | High | Contract limits tool set to [Read, Write, Bash] |
| Bash tool executes destructive commands | Medium | High | Phase-level timeouts; workspace scoping |
| Profile credentials leak | Low | High | Hermes-managed auth; no credentials in profile |
| Contract schema injection | Low | Medium | TOML parsing validation; schema version |

**Finding:** Tool restrictions need concrete boundaries. "Read, Write, Bash" is too broad for unattended execution. Plan should specify workspace roots and command allowlists. Auto-decided: Defer to Hermes tool policy (Profile scope). The orchestrator declares required capabilities; Hermes enforces boundaries. Principle P3 (pragmatic — don't reinvent Hermes sandboxing).

### Section 4: Data Flow & Interaction Edge Cases

**Data flow: Profile registration**
```
pipeline-watch init ──▶ Read contract schema ──▶ Verify Hermes CLI ──▶
    │                         │                       │
    ▼                         ▼                       ▼
  [not found]            [schema invalid]        [not found]
  [wrong perms]          [version mismatch]      [auth failed]

hermes profile create ──▶ Write profile ──▶ Verify profile ──▶ Complete
    │                      │                  │
    ▼                      ▼                  ▼
  [API error]            [write failed]     [capability miss]
```

**Finding:** Plan doesn't specify what happens when Hermes profile API doesn't support a required capability (e.g., tool restrictions). Auto-decided: Feature detection at registration time — skip unsupported capabilities, log warnings, document limitations. Principle P3 (pragmatic).

### Section 5: Test Plan

**Test diagram:**
```
NEW CODEPATHS:
- pipeline-watch init (CLI)          → Unit: mock Hermes, assert command
- Contract schema validation         → Unit: valid/invalid TOML schemas
- Profile verification               → Unit: mock profile, check capabilities
- register_todo_phases assignee      → Unit: mock kanban, assert --assignee flag
- pipeline-watch doctor              → Integration: real profile, check drift

EXISTING CODEPATHS AFFECTED:
- kanban_tasks.py assignee param     → Unit: verify "pipeline" default works
- config.py project config           → Unit: profile config in project.toml
```

**Test plan artifact:** Will be written by Eng review phase.

### Section 6: Performance

No performance concerns — profile registration is a one-time operation. Doctor check is read-only, <1s. No N+1 queries, no memory concerns.

### Section 7: Deployment Risk

Low risk — new subcommand, backward compatible. Existing ticks continue to work without profile. Profile verification is opt-in at tick start. Rollback: remove subcommand, revert config.

### Section 8: Observability

**Finding:** Plan needs logging for profile operations. Auto-decided: Add structured logging to `pipeline-watch init` and `doctor` — log profile name, version, capabilities, drift status. Principle P1 (completeness).

### Section 9: DRY & Code Quality

**Finding:** Profile config loading duplicates `project_config.py` patterns. Auto-decided: Extend `project_config.py` with profile section rather than creating a new module. Principle P4 (DRY).

### Section 10: Documentation

Plan mentions "Update documentation" in Step 6. Auto-decided: Include getting-started update with profile setup example. Principle P1 (completeness).

---

## NOT In Scope

- Profile management UI
- Multi-profile routing per phase
- Full execution contract enforcement engine (defer to v2)
- Hermes proxy for profile operations

## What Already Exists

| Capability | Location |
|-----------|----------|
| Assignee param | `kanban_tasks.py:73` — `register_todo_phases(assignee="default")` |
| Project config | `project_config.py` — TOML loading for `.hermes/project.toml` |
| Hermes adapter | `hermes_adapter.py` — subprocess wrappers |
| Phase config | `configs/phases.yaml` — phase definitions |
| Circuit breaker | `circuit.py` — fallback behavior |

## Deferred to TODOS.md

- TODO-16 (new): Execution contract enforcement engine — validate profile capabilities at runtime, fail phases that exceed authorized tool set. P3, Effort M.

---

<!-- AUTONOMOUS DECISION LOG -->
## Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|-------|----------|-----------|-----------|----------|---------|
| 1 | CEO 0C-bis | Approach C (hybrid) | Taste | P5+P1 | Delivers immediate value + durable abstraction | A (too narrow), B (too slow) |
| 2 | CEO 0D | Include drift detection | Mechanical | P2 | In blast radius, <1d CC effort | Defer |
| 3 | CEO 0D | Include contract schema | Mechanical | P1 | Addresses Codex's fundamental concern | Defer |
| 4 | CEO 0F | SELECTIVE EXPANSION | Mechanical | — | Feature enhancement on existing system | HOLD SCOPE |
| 5 | Sec 1 | Add contract layer | Taste | P5 | Decouples from Hermes API | Direct profile-only |
| 6 | Sec 2 | VersionMismatch → warn | Mechanical | P3 | Operator decides update timing | Block execution |
| 7 | Sec 2 | CapabilityMismatch → fail | Mechanical | P1 | Zero silent failures | Log and continue |
| 8 | Sec 3 | Defer to Hermes sandboxing | Mechanical | P3 | Don't reinvent tool boundaries | Build custom allowlist |
| 9 | Sec 4 | Feature detection at registration | Mechanical | P3 | Graceful degradation | Fail if unsupported |
| 10 | Sec 8 | Structured logging | Mechanical | P1 | Completeness — observability | No logging |
| 11 | Sec 9 | Extend project_config.py | Mechanical | P4 | DRY — reuse TOML loading patterns | New module |

**PHASE 1 COMPLETE.** Codex: 10 concerns. Claude subagent: 6 issues. Consensus: 4/6 confirmed, 2 disagreements (premise framing) → surfaced at gate. Passing to Phase 3.

---

## Phase 3: Eng Review

### Step 0 (Scope Challenge) — Actual Code Analysis

**Files examined:**
- `hermes_adapter.py` — Hermes subprocess wrappers (`hermes chat -q`, `hermes kanban create`)
- `config.py` — Config dataclass, `load_toml_overlay()` for `.hermes/config.toml`
- `project_config.py` — `_read_project_toml()`, multi-project scan patterns
- `kanban_tasks.py` — `register_todo_phases()` with `--assignee` flag at line 128
- `cli.py` — Tick flow, subcommand registry
- `configs/phases.yaml` — Phase definitions with per-phase `tools` fields

**API Verification (CRITICAL):**
```
$ hermes profile create --help
  profile_name          Profile name (lowercase, alphanumeric)
  --clone               Copy config.yaml, .env, SOUL.md, skills
  --clone-all           Full copy of active profile
  --no-skills           Empty profile with no bundled skills
  --description         One- or two-sentence description

$ hermes profile install --help
  source                Distribution source (git URL or local dir)
  --name NAME           Override profile name
  --force               Overwrite existing profile
```

**CRITICAL FINDING:** `hermes profile create` does NOT support `--model`, `--tools`, `--skills`, or `--safe-mode` flags. The plan's profile shape (lines 15-19) cannot be implemented via `hermes profile create`. The `--description` flag exists (used by kanban decomposer for routing). `hermes profile install` supports distribution installs from git repos.

This means the "profile registration" path (Approach A and the profile part of Approach C) is architecturally unfeasible as designed.

**Auto-decision:** Reframe to contract-only (Approach B). The contract is a versioned TOML file. Profile registration is deferred. The contract validates the environment, not a profile. Principle P5 (explicit over clever) + P1 (completeness).

### Eng Dual Voices

**Codex Eng Voice** — Key findings:
1. **[Critical]** Assumed Hermes profile API doesn't match installed CLI — `profile create` lacks model/tools/skills flags
2. **[High]** Contract not wired to real execution surfaces — `register_todo_phases()` only sends `--assignee`, `--goal`, `--goal-max-turns`. Contract must map to these flags
3. **[High]** Default tool set `[Read, Write, Bash]` would break existing phases — `phases.yaml` shows phases needing `Edit` for development, review, docs
4. **[High]** Safe-mode underspecified — Hermes `--safe-mode` disables user config, plugins, MCP — may break phase skills
5. **[High]** Silent degradation — "skip unsupported capabilities with warnings" is dangerous for unattended execution; should fail closed
6. **[Medium]** Drift handling backwards — continuing with stale profile recreates the original risk
7. **[Medium]** Per-phase routing dismissed too early — phases have different capability profiles
8. **[Medium]** Partial registration edge case — tick state persisted before verification can leave system requiring recovery

**Claude Eng Subagent** — Key findings:
1. **[Critical]** Hermes profile API unverified — should have been verified before writing plan
2. **[Medium]** Approach C scope inflation — "both" trap when contract is a second feature
3. **[Medium]** Redundant with per-phase tool enforcement — phases already specify tools
4. **[Medium]** Assignee wiring incomplete — no caller passes custom assignee; wiring path undefined
5. **[High]** "Skip if exists" silent drift — no hash defined, no version field
6. **[High]** Capability mismatch blocks entire tick — should be per-phase, not per-tick
7. **[Medium]** Verification "functional" undefined — what constitutes a working profile?
8. **[Medium]** Test plan deferred — no test cases in plan
9. **[Medium]** Missing chaos tests for profile operations
10. **[Medium]** No multi-project verification test
11. **[Medium]** Profile config in repo — credential exposure risk
12. **[High]** Contract-to-profile mapping is the hard part — each contract field needs Hermes API knowledge

### ENG DUAL VOICES — CONSENSUS TABLE

| Dimension | Claude Subagent | Codex | Consensus |
|-----------|----------------|-------|-----------|
| 1. Architecture sound? | Unfeasible as designed | API doesn't match | CONFIRMED — ref needed |
| 2. Test coverage sufficient? | Deferred, gaps | Contract mapping untested | CONFIRMED — insufficient |
| 3. Performance risks addressed? | Low concern | Low concern | CONFIRMED — OK |
| 4. Security threats covered? | Credential risk | Too broad tools | DISAGREE — severity diff |
| 5. Error paths handled? | Gaps in drift/verify | Silent degradation | CONFIRMED — gaps |
| 6. Deployment risk manageable? | Low | Low | CONFIRMED — OK |

### Section 1: Architecture — Revised

**Revised dependency graph (contract-only):**

```
BEFORE:                                    AFTER:
┌──────────────┐                           ┌──────────────┐
│ pipeline-    │                           │ pipeline-    │
│ watch tick   │──▶ register_todo_phases() │ watch tick   │──▶ load contract
└──────────────┘     assignee="default"    └──────┬───────┘  TOML validate
                                                  │
                                                  ▼
                                         ┌──────────────────┐
                                         │ Execution Contract│──▶ assignee
                                         │ .hermes/         │──▶ model_policy
                                         │ pipeline.toml    │──▶ capabilities
                                         └──────────────────┘──▶ safe_mode
```

**Auto-decision:** Contract-only approach. Profile registration deferred to TODO-16. Contract is the primary abstraction, testable without Hermes. Classification: Taste. Principle: P5 (explicit) + P1 (completeness).

**Revised decision #1 (approach):** Reject Approach C, adopt Approach B (contract-only). The profile part is impossible given the Hermes API.

### Section 2: Code Quality

**Findings:**
1. Contract schema loading should reuse `project_config.py` TOML patterns. Existing `_read_project_toml()` pattern works.
2. `register_todo_phases(assignee=...)` needs the assignee value read from contract, not hardcoded.
3. Contract validation should be testable without subprocess calls.

**Auto-decisions:**
- Contract file location: `.hermes/pipeline.toml` in orchestrator repo root. Classification: Mechanical. Principle: P4 (DRY).
- Contract loading: New module `hermes_pipeline/contract.py`, follows `config.py` dataclass patterns. Classification: Mechanical. Principle: P4 (DRY).

### Section 3: Test Plan

**Test diagram — codepaths to coverage:**

```
NEW CODEPATHS:
┌─────────────────────────────────────────────────────────────────┐
│ contract.py: load_pipeline_contract()                          │
│   → Unit: valid TOML schema                                    │
│   → Unit: missing required fields                              │
│   → Unit: schema version mismatch                              │
│   → Unit: empty/nil contract file                              │
├─────────────────────────────────────────────────────────────────┤
│ contract.py: validate_contract()                               │
│   → Unit: all fields present, valid values                     │
│   → Unit: field validation errors (bad tool names, etc)        │
│   → Unit: contract vs phases.yaml cross-validation             │
├─────────────────────────────────────────────────────────────────┤
│ cli.py: _cmd_doctor()                                          │
│   → Unit: clean contract → exit 0, summary output              │
│   → Unit: drift detected → exit 1, actionable message          │
│   → Unit: contract missing → exit 2, remediation command       │
├─────────────────────────────────────────────────────────────────┤
│ cli.py: _cmd_init()                                            │
│   → Unit: write default contract → file exists, valid TOML     │
│   → Unit: idempotent (run twice → no-op, exit 0)               │
├─────────────────────────────────────────────────────────────────┤
│ kanban_tasks.py: register_todo_phases()                        │
│   → Unit: --assignee flag from contract appears in command     │
│   → Unit: contract missing → graceful fallback to "default"    │
└─────────────────────────────────────────────────────────────────┘

EXISTING CODEPATHS AFFECTED:
┌─────────────────────────────────────────────────────────────────┤
│ cli.py: _cmd_tick()                                            │
│   → Unit: tick loads contract before registration              │
│   → Unit: contract validation failure blocks tick              │
│   → Unit: multi-project tick, per-project contract             │
└─────────────────────────────────────────────────────────────────┘
```

**Test plan artifact:** Written inline above.

### Section 4: Performance

No performance concerns — contract is a local TOML file read at tick start. <1ms.

### Failure Modes Registry

| Failure Mode | Trigger | Detection | Recovery | Tested? |
|-------------|---------|-----------|----------|---------|
| Contract missing | Fresh clone, never ran init | `FileNotFoundError` at tick | Run `pipeline-watch init` | Unit |
| Contract schema invalid | Manual edit error | `SchemaValidationError` at tick | Fix TOML, re-run | Unit |
| Contract version drift | Upgrade, old contract | Version field check at doctor | `pipeline-watch init --force` | Unit |
| Phase needs tool not in contract | New phase added | Cross-validation at doctor | Update contract | Unit |

### Completion Summary (Eng)

- Architecture: Revised to contract-only (Approach B). Profile API doesn't support required capabilities.
- Tests: 12+ test cases mapped across 5 codepaths.
- Security: Contract in repo, no credentials. Tool constraints declarative.
- Deployment: Backward compatible. Existing ticks work without contract.

**PHASE 3 COMPLETE.** Codex: 8 concerns. Claude subagent: 12 issues. Consensus: 5/6 confirmed, 1 disagreement (security severity). Passing to Phase 3.5 (DX Review).

---

## Phase 3.5: DX Review

### Step 0 (DX Scope Assessment)

**Product type:** Developer CLI tool (pipeline orchestrator)
**Developer persona:** Solo developer running automated TODO pipeline
**Initial DX completeness:** 6/10 — existing commands (`tick`, `status`, `kill`) are functional; no onboarding flow for new features

**TTHW assessment:**
- Current: `pip install` → `pipeline-watch tick` → wait for selection → phases run
- With contract: `pip install` → `pipeline-watch init` → `pipeline-watch tick` → phases run
- Target: Add 1 command to onboarding. Should be <30 seconds.

### Step 0.5 (DX Dual Voices)

**Codex DX Voice** — Key findings:
1. Rename from "Hermes profile" to "pipeline execution config" — user-facing names matter
2. `pipeline-watch doctor` should be the centerpiece, not init
3. Define 4-command hello-world path with expected output
4. Replace "skip if exists" with version/hash + `--repair`
5. Treat unsupported Hermes capabilities as adapter limitations, not degraded success
6. Add exact error-message templates and remediation commands
7. Add migration commands before implementation starts

**Claude DX Subagent** — Key findings:
1. **[Critical]** Foundational risk not gated — profile API unknown, whole plan speculative
2. **[Critical]** Subcommand name inconsistent — `init` vs `setup-profile` undecided
3. **[Critical]** `VersionMismatch` and `CapabilityMismatch` have no error messages
4. **[Critical]** No documentation content specified — zero copy-paste examples
5. **[High]** No clear onboarding path defined
6. **[High]** `doctor` subcommand has no spec (args, output, exit codes)
7. **[High]** `VersionMismatch` auto-decision defeats contract purpose
8. **[High]** No escape hatches for configuration
9. **[Medium]** `check_hermes()` not reused in init
10. **[Medium]** No mechanism to skip contract entirely
11. **[Medium]** Contract schema validation rules not defined
12. **[Low]** Contract versioning strategy undefined

### DX DUAL VOICES — CONSENSUS TABLE

| Dimension | Claude Subagent | Codex | Consensus |
|-----------|----------------|-------|-----------|
| 1. Getting started < 5 min? | No onboarding defined | Needs 4-command path | CONFIRMED — gap |
| 2. API/CLI naming guessable? | Undecided, inconsistent | Rename away from "profile" | CONFIRMED — fix |
| 3. Error messages actionable? | No messages defined | Needs templates | CONFIRMED — gap |
| 4. Docs findable & complete? | Zero content | Needs copy-paste examples | CONFIRMED — gap |
| 5. Upgrade path safe? | No versioning | Needs migration | DISAGREE — priority |
| 6. Dev environment friction-free? | init/doctor adds steps | Adds value vs friction | CONFIRMED — net positive |

### DX Scorecard (8 Dimensions)

| Dimension | Score | Notes |
|-----------|-------|-------|
| 1. Getting Started | 4/10 | No onboarding flow, no copy-paste examples |
| 2. CLI Ergonomics | 5/10 | `init`/`doctor` names OK; undecided naming is a gap |
| 3. Error Handling | 2/10 | Critical GAPS — no messages defined for VersionMismatch/CapabilityMismatch |
| 4. Documentation | 1/10 | Zero content specified |
| 5. Upgrade Path | 3/10 | Version field mentioned but no migration strategy |
| 6. Escape Hatches | 4/10 | No mechanism to skip contract |
| 7. Observability | 6/10 | Structured logging auto-decided but not specified |
| 8. Consistency | 7/10 | Follows existing CLI patterns (verbs, exit codes) |
| **Overall** | **4.1/10** | Strong architecture, weak DX polish |

### DX Implementation Checklist

1. **Resolve subcommand naming:** `pipeline-watch init` (write default contract), `pipeline-watch doctor` (verify). Done.
2. **Define exit code contract:** `init`: 0 success, 1 error. `doctor`: 0 clean, 1 drift, 2 missing. Done.
3. **Write error message strings** following problem + cause + fix pattern
4. **Define `--help` text** for both new subcommands
5. **Document contract TOML schema** with field descriptions
6. **Add copy-paste examples** to getting-started guide
7. **Version field** in contract schema with migration guidance
8. **Reuse `check_hermes()`** as first step in `init`

### Developer Journey Map (9-Stage)

| Stage | Current | With Contract | Gap? |
|-------|---------|---------------|------|
| 1. Install | `pip install` | `pip install` | No |
| 2. Setup | None | `pipeline-watch init` | New |
| 3. Verify | None | `pipeline-watch doctor` | New |
| 4. First tick | `pipeline-watch tick` | `pipeline-watch tick` | No |
| 5. Selection | Wait for LLM | Wait for LLM | No |
| 6. Phase run | Hermes runs | Hermes runs | No |
| 7. Review | Kanban board | Kanban board | No |
| 8. Approval | Kanban transition | Kanban transition | No |
| 9. Ship | gstack /ship | gstack /ship | No |

### TTHW Assessment

- Current: ~2 min (install → tick)
- With contract: ~3 min (install → init → tick)
- Target: <5 min — achievable

**PHASE 3.5 COMPLETE.** DX overall: 4.1/10. TTHW: 2 min → 3 min. Codex: 7 concerns. Claude subagent: 12 issues. Consensus: 5/6 confirmed, 1 disagreement (upgrade priority). Passing to Phase 4 (Final Gate).

---

## Cross-Phase Themes

**Theme 1: Hermes profile API limitation** — flagged independently in CEO (Codex finding 2, Claude finding 1), Eng (Codex finding 1, Claude finding 1A), and DX (Codex finding 1, Claude finding 1.1). High-confidence signal: the profile-as-solution is unworkable. Contract-only is the path.

**Theme 2: Contract schema as durable abstraction** — flagged in CEO (Codex finding 10), Eng (Codex finding 2, Claude finding 1B), DX (Codex finding 7). All voices agree the contract is the right abstraction.

**Theme 3: Silent degradation is dangerous** — flagged in CEO (Codex finding 7), Eng (Codex finding 5, Claude finding 2A), DX (Claude finding 3.2). Consensus: fail closed, not degrade gracefully.

---

## Deferred to TODOS.md

- **TODO-16** (new): Hermes profile integration — once Hermes profile API supports model/tools/skills flags, wire profile registration as an implementation of the execution contract. P3, Effort M.
- **TODO-17** (new): Contract migration tooling — version migration, `--repair` flag, deprecation warnings. P3, Effort S.

---

## GSTACK REVIEW REPORT

### Runs

| Run | Skill | Status | Findings |
|-----|-------|--------|----------|
| 1 | plan-ceo-review | issues_open | 10 codex + 6 subagent |
| 2 | plan-eng-review | issues_open | 8 codex + 12 subagent |
| 3 | plan-devex-review | issues_open | 7 codex + 12 subagent |

### Auto-Decided Decisions

| # | Phase | Decision | Classification | Principle |
|---|-------|----------|-----------|-----------|
| 12 | Eng | Contract-only (reject profile) | Taste | P5 | Hermes API doesn't support needed flags |
| 13 | Eng | Contract file at .hermes/pipeline.toml | Mechanical | P4 | DRY with existing patterns |
| 14 | Eng | New contract.py module | Mechanical | P4 | DRY with config.py patterns |
| 15 | DX | Subcommand: init + doctor | Mechanical | P5 | Consistent with existing verbs |
| 16 | DX | Exit codes: 0/1/2 | Mechanical | P1 | Completeness |
| 17 | DX | Fail closed on drift | Taste | P1 | Zero silent failures |

### User Challenge

**Challenge 1: Reframe from "Hermes profile" to "pipeline execution config"**
- You said: "Design a Hermes profile specifically matched to the pipeline orchestrator's needs"
- Both models recommend: Reframe to "Define a versioned pipeline execution contract (TOML)" with profile registration as a deferred TODO
- Why: `hermes profile create` does NOT support `--model`, `--tools`, `--skills`, or `--safe-mode` flags. The profile shape you specified cannot be implemented. The durable abstraction is a contract file — versioned, testable, decoupled from Hermes API.
- What we might be missing: Hermes could add these flags in a future release. The contract approach still works — it just means profile registration is deferred.
- If we're wrong, the cost is: We defer a Hermes profile integration (TODO-16). The contract approach delivers immediate value (assignee config, drift detection, verification) without the profile.

### Unresolved Issues

- **VersionMismatch handling:** Eng said "warn and continue" (P3 pragmatic), DX said "fail closed" (P1 completeness). Auto-decided: fail closed on drift. Classification: Taste. Principle: P1 (completeness) dominates for unattended execution.

NO UNRESOLVED DECISIONS
