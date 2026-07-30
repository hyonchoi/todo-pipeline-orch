# Agent Client Release Qualification

This protocol qualifies profile/client pairs whose package metadata says
`Conditional`. It tests external skill installation and client discovery that
the hermetic package suite cannot prove.

Normal CI does not run these checks. Third-party credentials and installations
are forbidden in hermetic CI. Run qualification manually in a disposable,
isolated environment and commit only the captured evidence.

## Conditional pairs

### `gstack` / `claude`

- Environment prerequisites: record the exact Hermes, Claude Code, gstack, and
  superpowers versions.
- Hermes dispatcher check: confirm the `ai-coding-agents` skill is available
  in the Hermes skill registry and can invoke `claude -p`.
- Discovery checks: follow symlinks under `~/.claude/skills` and confirm every
  required gstack `SKILL.md`; then confirm the official
  `claude-plugins-official/superpowers` plugin manifest and required
  `writing-plans` and `subagent-driven-development` skills.
- Invocation forms: confirm the discovered skill IDs map to `/autoplan`,
  `/writing-plans`, and the other slash-prefixed forms in package metadata.
- Representative invocation: from a disposable Git fixture with no
  project-local skill directory, invoke `/autoplan` in qualification-only mode
  and capture output proving the client discovered and started the skill
  without an unknown-skill error.
- Evidence artifact:
  `docs/release-evidence/agent-clients/<release>/gstack-claude.md`.
- Required fields: evidence status, release, qualified source VERSION and
  commit, profile/client pair, UTC timestamp, OS, client version, distribution
  versions, exact skill/plugin sources, discovery commands and their captured
  output, invocation forms, result, and verifier.
- Blocking rule: a release advertising this Conditional pair is blocked when
  the current release has no passing artifact or the artifact records a
  failure.

### `gstack` / `codex`

- Environment prerequisites: record the exact Hermes, Codex, gstack, and
  superpowers versions.
- Hermes dispatcher check: confirm the `ai-coding-agents` skill is available
  in the Hermes skill registry and can invoke `codex exec`.
- Discovery checks: follow symlinks under `~/.codex/skills` and confirm every
  required gstack `SKILL.md`; then confirm the curated
  `openai-curated-remote/superpowers` plugin manifest and required
  `writing-plans` and `subagent-driven-development` skills.
- Invocation forms: confirm the discovered skill IDs map to `$autoplan`,
  `$superpowers:writing-plans`, and the other package-qualified or dollar-prefixed
  forms in package metadata.
- Representative invocation: from a disposable Git fixture with no
  project-local skill directory, invoke `$autoplan` in qualification-only mode
  and capture output proving the client discovered and started the skill
  without an unknown-skill error.
- Evidence artifact:
  `docs/release-evidence/agent-clients/<release>/gstack-codex.md`.
- Required fields: evidence status, release, qualified source VERSION and
  commit, profile/client pair, UTC timestamp, OS, client version, distribution
  versions, exact skill/plugin sources, discovery commands and their captured
  output, invocation forms, result, and verifier.
- Blocking rule: a release advertising this Conditional pair is blocked when
  the current release has no passing artifact or the artifact records a
  failure.

## Evidence handling

Use the [agent client evidence schema](release-evidence/agent-clients/README.md#required-artifact-fields)
and its release-directory naming convention. A passing artifact must contain
the real commands and output captured from the stated environment. Do not copy
an earlier release's result or create a placeholder passing artifact.

Before `/ship` selects a version, store an honest qualification snapshot under
`docs/release-evidence/agent-clients/candidate-source-snapshot/`. It must say
`Evidence status: candidate/source-snapshot` and `Release: not selected`, and
must record the source `VERSION` and commit that were actually qualified. A
candidate `PASS` is useful review evidence, but it does not satisfy the
release-specific blocking rule.

During the release commit, `/ship` owns the version decision and evidence
finalization:

1. Select and synchronize the release version.
2. Re-run qualification if the recorded environment, discovery output, or
   qualified source has changed.
3. Copy each current passing candidate artifact to
   `docs/release-evidence/agent-clients/<release>/`.
4. Set `Evidence status: release-final`, set `Release` to the selected version,
   and ensure `Source VERSION` matches it. Preserve the exact qualified source
commit, discovery commands/output, representative invocation command/transcript,
and fixture-isolation evidence.
5. Include those versioned artifacts in the same release commit and run the
   evidence validation tests.

`Unverified` pairs are unsupported and non-blocking until authoritative
evidence promotes their package metadata. Changing only documentation or
`prompt_client` does not promote them.
