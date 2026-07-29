# gstack / Claude qualification

- Release: `0.6.6`
- Profile/client: `gstack` / `claude`
- Timestamp: `2026-07-29T15:55:44Z`
- Environment: macOS 26.5.2 (25F84), arm64
- Client: Claude Code 2.1.220
- gstack: 1.60.1.0
- superpowers: 6.2.0
- Verifier: Codex `/review` session on the maintainer's workstation
- Result: `PASS`

## Discovery evidence

gstack's required skills resolve through symlinks below
`~/.claude/skills`:

```text
autoplan
review
cso
qa
document-release
document-generate
ship
```

Superpowers is the official Claude plugin
`claude-plugins-official/superpowers` at version 6.2.0. Its manifest and the
required skills were verified at:

```text
~/.claude/plugins/cache/claude-plugins-official/superpowers/6.2.0/.claude-plugin/plugin.json
~/.claude/plugins/cache/claude-plugins-official/superpowers/6.2.0/skills/writing-plans/SKILL.md
~/.claude/plugins/cache/claude-plugins-official/superpowers/6.2.0/skills/subagent-driven-development/SKILL.md
```

## Invocation forms

The discovered skill IDs match the package metadata forms `/autoplan`,
`/writing-plans`, `/subagent-driven-development`, `/review`, `/cso`, `/qa`,
`/document-release`, `/document-generate`, and `/ship`.
