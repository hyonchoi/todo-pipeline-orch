# Agent Client Release Evidence

Before `/ship` selects a release version, store manual agent-client
qualification results as candidate/source-snapshot evidence:

```text
docs/release-evidence/agent-clients/candidate-source-snapshot/<profile>-<client>.md
```

Candidate evidence must explicitly use `Release: not selected`; the current
source `VERSION` is evidence metadata, not a release decision.

After `/ship` selects the exact release, finalize the passing artifacts under a
directory named for that release:

```text
docs/release-evidence/agent-clients/<release>/<profile>-<client>.md
```

For example, the release-final `gstack` / `codex` artifact for release `0.7.0`
would be
`docs/release-evidence/agent-clients/0.7.0/gstack-codex.md`.

Do not create a passing artifact without running and capturing the live
qualification commands. See the
[qualification protocol](../../release-qualification-agent-clients.md#conditional-pairs)
for the required pair-specific procedure.

## Required artifact fields

Each artifact must include:

- Evidence status: `candidate/source-snapshot` or `release-final`
- Release (`not selected` for candidate evidence)
- Qualified source VERSION and full source commit
- Profile/client pair
- UTC timestamp
- Operating system and version
- Exact client version
- Exact gstack and superpowers distribution versions
- Exact gstack skill root and superpowers plugin source
- Discovery command and complete discovery output
- Verified invocation forms
- Result: `PASS` or `FAIL`
- Verifier name or stable identity

A candidate `PASS` is valid only for the recorded source snapshot and
environment. It is review evidence, not release-final evidence. A release-final
`PASS` is valid only for the recorded release, source, and environment. A
`FAIL` blocks a release that advertises the corresponding `Conditional` pair.
Missing release-final evidence also blocks that advertised pair.

## Release commit finalization

`/ship` selects the release version. In the release commit, copy the current
candidate/source-snapshot artifacts into `<release>/`, change their evidence
status to `release-final`, set `Release` and `Source VERSION` to the selected
version, and retain the exact source commit, commands, and captured output. If
any recorded fact has changed, re-run qualification first. Commit the versioned
artifacts with the other synchronized release files and run:

```bash
rtk env -u VIRTUAL_ENV uv run --locked pytest -q tests/test_release_qualification_evidence.py
```
