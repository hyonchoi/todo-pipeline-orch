# Agent Client Release Evidence

Optional manual agent-client diagnostics are stored as source snapshots:

```text
docs/release-evidence/agent-clients/candidate-source-snapshot/<profile>-<client>.md
```

Candidate evidence uses `Release: not selected`; the source version and source
commit identify what was actually tested, not a release decision.
Existing `release-final` artifacts under versioned directories are historical
records. They do not qualify the current package or require a new artifact for
every release. Python release automation never copies or rewrites evidence.

Native-SDD uses separate `native-sdd-claude.md` and `native-sdd-codex.md` files
because dispatcher transport alone does not establish user-policy semantics.
gstack and superpowers discovery apply only to gstack pairs.

Do not create a passing artifact without running and capturing the live
commands. See the [optional diagnostic protocol](../../release-qualification-agent-clients.md#conditional-pairs)
for pair-specific procedures. These procedures and their results are
informational; package release automation requires no manual or AI-assigned
passing result, live client authentication, VM, or additional account.

## Required artifact fields

Each artifact must include:

- Evidence status: `candidate/source-snapshot` or `release-final`
- Release (`not selected` for candidate evidence)
- Qualified source version and full source commit
- Profile/client pair
- UTC timestamp
- Operating system and version
- Exact Hermes version
- Exact client version
- Exact gstack and superpowers distribution versions (gstack pairs only)
- Exact gstack skill root and superpowers plugin source (gstack pairs only)
- Discovery command and complete discovery output
- Hermes `skills list --enabled-only` command and output proving that
  `ai-coding-agents` is enabled
- A Hermes dispatcher command and transcript proving that `ai-coding-agents`
  invoked the selected external client, the external process exited zero, and
  the expected stdout marker was returned
- Verified invocation forms
- Disposable fixture command and isolation output
- Representative invocation command and transcript excerpt proving discovery
  and start without an unknown-skill error
- Result: `PASS` or `FAIL`
- Verifier name or stable identity

A `PASS` describes only the recorded source and environment. Archived
`release-final` labels are historical, not evidence of a fresh run. Missing,
failed, or unrun diagnostics do not block releases.
An artifact that cannot prove either Hermes skill enablement or dispatcher
execution must record `Result: FAIL`; direct client invocation alone is not a
substitute.

Native-SDD evidence additionally records both policy modes, retained harmless
user instructions, pinned registration behavior, and phase-specific worker
observations from the [live recipes](../../release-qualification-agent-clients.md#native-sdd-live-policy-recipes).
Unrun candidates must say `FAIL` and identify unavailable evidence, without
invented versions, commands, outputs or verifier claims. These documentation
conventions do not make any result field an automated release prerequisite.

## Release automation boundary

`scripts/release_changesets.py apply` changes only release metadata:
`pyproject.toml`, `uv.lock`, `CHANGELOG.md`, and consumed `.changeset` fragments.
It does not read candidates, require current-version evidence, change recorded
source versions, or create versioned evidence directories.

The Release workflow runs deterministic pytest, Ruff, and release metadata
consistency checks before pushing its Version Packages branch. Historical
artifact tests validate recorded identity and structure only; they do not
compare archives with current candidates or judge captured AI output.
