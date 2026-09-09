# Agent Client Release Evidence

Before the Python release workflow selects a release version, store manual agent-client
qualification results as candidate/source-snapshot evidence:

```text
docs/release-evidence/agent-clients/candidate-source-snapshot/<profile>-<client>.md
```

Candidate evidence must explicitly use `Release: not selected`; the current
source version is evidence metadata, not a release decision.

After the workflow selects the exact release, the repository release-version
command finalizes the artifacts under a directory named for that release:

```text
docs/release-evidence/agent-clients/<release>/<profile>-<client>.md
```

For example, the release-final `gstack` / `codex` artifact for release `0.7.0`
would be
`docs/release-evidence/agent-clients/0.7.0/gstack-codex.md`.

`scripts/release_changesets.py` owns the explicit mapping from every advertised
`Conditional` profile/client pair to its evidence artifact. Native-SDD uses
separate `native-sdd-claude.md` and `native-sdd-codex.md` files because dispatcher
transport alone does not qualify delegated user-policy semantics. gstack and
superpowers discovery are required only for gstack pairs.

Do not create a passing artifact without running and capturing the live
qualification commands. See the
[qualification protocol](../../release-qualification-agent-clients.md#conditional-pairs)
for the required pair-specific procedure.

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

A candidate `PASS` is valid only for the recorded source snapshot and
environment. It is review evidence, not release-final evidence. A release-final
`PASS` is valid only for the recorded release, source, and environment. A
`FAIL` blocks a release that advertises the corresponding `Conditional` pair.
Missing release-final evidence also blocks that advertised pair.
An artifact that cannot prove either Hermes skill enablement or dispatcher
execution must record `Result: FAIL`; direct client invocation alone is not a
substitute.

Native-SDD evidence additionally records both policy modes, retained harmless
user instructions, pinned registration behavior, and phase-specific worker
observations from the [live recipes](../../release-qualification-agent-clients.md#native-sdd-live-policy-recipes).
Unrun candidates must say `FAIL` and identify unavailable evidence, without
invented versions, commands, outputs or verifier claims. The initial metadata
list after the title (including blank lines and indented continuations) must contain exactly one
`- Result: ` field with backtick-delimited `PASS` for finalization; duplicate,
missing or non-PASS fields fail. Transcript mentions are not metadata.

## Release commit finalization

The Python changeset workflow selects the release version. In the release commit,
`scripts/release_changesets.py apply` copies the current candidate/source-snapshot
artifacts into `<release>/`, changes their evidence status to `release-final`,
sets `Release` and `Source version` to the selected version, and retains the
exact source commit, commands, result, and captured output. If any recorded fact
has changed, update the candidate by re-running qualification before merging
the Version Packages pull request. Commit the versioned artifacts with the
other synchronized release files and run:

```bash
rtk env -u VIRTUAL_ENV uv run --locked pytest -q tests/test_release_qualification_evidence.py
```

The complete pre-render preflight validates all four candidates before writing
any release-final artifact. Failed candidates still permit the independent
version/lock/changelog consistency check to pass.
