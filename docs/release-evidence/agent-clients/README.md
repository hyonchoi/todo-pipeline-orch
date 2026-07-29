# Agent Client Release Evidence

Store manual agent-client qualification results under a directory named for
the exact release:

```text
docs/release-evidence/agent-clients/<release>/<profile>-<client>.md
```

For example, the `gstack` / `codex` artifact for release `0.7.0` would be
`docs/release-evidence/agent-clients/0.7.0/gstack-codex.md`.

Do not create a passing artifact without running and capturing the live
qualification commands. See the
[qualification protocol](../../release-qualification-agent-clients.md#conditional-pairs)
for the required pair-specific procedure.

## Required artifact fields

Each artifact must include:

- Release and profile/client pair
- UTC timestamp
- Operating system and version
- Exact client version
- Exact gstack and superpowers distribution versions
- Exact gstack skill root and superpowers plugin source
- Discovery command and complete discovery output
- Verified invocation forms
- Result: `PASS` or `FAIL`
- Verifier name or stable identity

A `PASS` is valid only for the recorded release and environment. A `FAIL`
blocks a release that advertises the corresponding `Conditional` pair. Missing
evidence also blocks that advertised pair.
