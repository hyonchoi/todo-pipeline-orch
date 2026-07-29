# Agent Client Release Qualification

This protocol qualifies profile/client pairs whose package metadata says
`Conditional`. It tests external skill installation and client discovery that
the hermetic package suite cannot prove.

Normal CI does not run these checks. Third-party credentials and installations
are forbidden in hermetic CI. Run qualification manually in a disposable,
isolated environment and commit only the captured evidence.

## Conditional pairs

### `gstack` / `claude`

- Environment prerequisites: record the exact Claude Code version, gstack
  version, superpowers version, a clean discovery root, and confirmation that
  no project-local `.claude/skills` directory was inherited.
- Discovery command: from the clean discovery root, run the read-only command
  `find .claude/skills -type f -name SKILL.md -print | sort`.
- Representative invocation: explicitly invoke `/autoplan` in a disposable
  fixture. The client must identify and start the skill without an
  unknown-skill error.
- Evidence artifact:
  `docs/release-evidence/agent-clients/<release>/gstack-claude.md`.
- Required fields: UTC timestamp, OS, client version, distribution versions,
  install commands, discovery output, invocation transcript excerpt, result,
  and verifier.
- Blocking rule: a release advertising this Conditional pair is blocked when
  the current release has no passing artifact or the artifact records a
  failure.

### `gstack` / `codex`

- Environment prerequisites: record the exact Codex version, gstack version,
  superpowers version, a clean discovery root, and confirmation that no
  project-local `.agents/skills` directory was inherited.
- Discovery command: from the clean discovery root, run the read-only command
  `find .agents/skills -type f -name SKILL.md -print | sort`.
- Representative invocation: explicitly invoke `$autoplan` in a disposable
  fixture. The client must identify and start the skill without an
  unknown-skill error.
- Evidence artifact:
  `docs/release-evidence/agent-clients/<release>/gstack-codex.md`.
- Required fields: UTC timestamp, OS, client version, distribution versions,
  install commands, discovery output, invocation transcript excerpt, result,
  and verifier.
- Blocking rule: a release advertising this Conditional pair is blocked when
  the current release has no passing artifact or the artifact records a
  failure.

## Evidence handling

Use the [agent client evidence schema](release-evidence/agent-clients/README.md#required-artifact-fields)
and its release-directory naming convention. A passing artifact must contain
the real commands and output captured from the stated environment. Do not copy
an earlier release's result or create a placeholder passing artifact.

`Unverified` pairs are unsupported and non-blocking until authoritative
evidence promotes their package metadata. Changing only documentation or
`prompt_client` does not promote them.
