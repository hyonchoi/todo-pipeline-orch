# gstack / Codex qualification

- Release: `0.6.6`
- Profile/client: `gstack` / `codex`
- Timestamp: `2026-07-29T15:55:44Z`
- Environment: macOS 26.5.2 (25F84), arm64
- Client: Codex CLI 0.145.0
- gstack: 1.60.1.0
- superpowers: 6.2.0
- Verifier: Codex `/review` session on the maintainer's workstation
- Result: `PASS`

## Discovery evidence

gstack's required skills resolve through symlinks below
`~/.codex/skills`:

```text
autoplan
review
cso
qa
document-release
document-generate
ship
```

Superpowers is the curated Codex plugin
`openai-curated-remote/superpowers` at version 6.2.0. Its manifest and the
required skills were verified at:

```text
~/.config/codex/plugins/cache/openai-curated-remote/superpowers/6.2.0/.codex-plugin/plugin.json
~/.config/codex/plugins/cache/openai-curated-remote/superpowers/6.2.0/skills/writing-plans/SKILL.md
~/.config/codex/plugins/cache/openai-curated-remote/superpowers/6.2.0/skills/subagent-driven-development/SKILL.md
```

## Invocation forms

The discovered skill IDs match the package metadata forms `$autoplan`,
`$superpowers:writing-plans`, `$superpowers:subagent-driven-development`, `$review`, `$cso`, `$qa`,
`$document-release`, `$document-generate`, and `$ship`.
