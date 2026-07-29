# gstack / Claude release qualification

- Evidence status: `release-final`
- Release: `0.7.0`
- Source VERSION: `0.7.0`
- Source commit: `9d47c5ff42ce1e925a65f2d401da12e3b0020019`
- Profile/client: `gstack / claude`
- Timestamp: `2026-07-29T22:04:55Z`
- Environment: `macOS 26.5.2 (25F84), arm64`
- Client: `Claude Code 2.1.220`
- gstack: `1.60.1.0`
- superpowers: `6.2.0`
- gstack skill root: `~/.local/share/gstack`
- superpowers plugin source: `claude-plugins-official/superpowers@6.2.0`
- Verifier: `Codex final-fix session on the maintainer's workstation`
- Result: `PASS`

This release-final artifact promotes the passing candidate qualification after
`/ship` selected release `0.7.0`. The recorded discovery facts were unchanged.

## Discovery commands and captured output

### gstack skills

Command:

```bash
rtk ls "$HOME/.claude/skills"/{autoplan,review,cso,qa,document-release,document-generate,ship}/SKILL.md
```

Captured output:

```text
/Users/hyonchoi/.claude/skills/autoplan/SKILL.md -> /Users/hyonchoi/.local/share/gstack/autoplan/SKILL.md  53B
/Users/hyonchoi/.claude/skills/cso/SKILL.md -> /Users/hyonchoi/.local/share/gstack/cso/SKILL.md  48B
/Users/hyonchoi/.claude/skills/document-generate/SKILL.md -> /Users/hyonchoi/.local/share/gstack/document-generate/SKILL.md  62B
/Users/hyonchoi/.claude/skills/document-release/SKILL.md -> /Users/hyonchoi/.local/share/gstack/document-release/SKILL.md  61B
/Users/hyonchoi/.claude/skills/qa/SKILL.md -> /Users/hyonchoi/.local/share/gstack/qa/SKILL.md  47B
/Users/hyonchoi/.claude/skills/review/SKILL.md -> /Users/hyonchoi/.local/share/gstack/review/SKILL.md  51B
/Users/hyonchoi/.claude/skills/ship/SKILL.md -> /Users/hyonchoi/.local/share/gstack/ship/SKILL.md  49B
```

### superpowers plugin

Command:

```bash
rtk ls "$HOME/.claude/plugins/cache/claude-plugins-official/superpowers/6.2.0/.claude-plugin/plugin.json" "$HOME/.claude/plugins/cache/claude-plugins-official/superpowers/6.2.0/skills"/{writing-plans,subagent-driven-development}/SKILL.md
```

Captured output:

```text
/Users/hyonchoi/.claude/plugins/cache/claude-plugins-official/superpowers/6.2.0/.claude-plugin/plugin.json  497B
/Users/hyonchoi/.claude/plugins/cache/claude-plugins-official/superpowers/6.2.0/skills/subagent-driven-development/SKILL.md  27.4K
/Users/hyonchoi/.claude/plugins/cache/claude-plugins-official/superpowers/6.2.0/skills/writing-plans/SKILL.md  6.7K
```

## Invocation forms

The discovered skill IDs match the package metadata forms `/autoplan`,
`/writing-plans`, `/subagent-driven-development`, `/review`, `/cso`, `/qa`,
`/document-release`, `/document-generate`, and `/ship`.
