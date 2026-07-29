# gstack / Codex release qualification

- Evidence status: `release-final`
- Release: `0.7.0`
- Source VERSION: `0.7.0`
- Source commit: `9d47c5ff42ce1e925a65f2d401da12e3b0020019`
- Profile/client: `gstack / codex`
- Timestamp: `2026-07-29T22:04:55Z`
- Environment: `macOS 26.5.2 (25F84), arm64`
- Client: `Codex CLI 0.146.0`
- gstack: `1.60.1.0`
- superpowers: `6.2.0`
- gstack skill root: `~/.local/share/gstack`
- superpowers plugin source: `openai-curated-remote/superpowers@6.2.0`
- Verifier: `Codex final-fix session on the maintainer's workstation`
- Result: `PASS`

This release-final artifact promotes the passing candidate qualification after
`/ship` selected release `0.7.0`. The recorded discovery facts were unchanged.

## Discovery commands and captured output

### gstack skills

Command:

```bash
rtk ls "$HOME/.codex/skills"/{autoplan,review,cso,qa,document-release,document-generate,ship}/SKILL.md
```

Captured output:

```text
/Users/hyonchoi/.codex/skills/autoplan/SKILL.md  98.2K
/Users/hyonchoi/.codex/skills/cso/SKILL.md  72.3K
/Users/hyonchoi/.codex/skills/document-generate/SKILL.md  61.6K
/Users/hyonchoi/.codex/skills/document-release/SKILL.md  53.4K
/Users/hyonchoi/.codex/skills/qa/SKILL.md  81.2K
/Users/hyonchoi/.codex/skills/review/SKILL.md  103.3K
/Users/hyonchoi/.codex/skills/ship/SKILL.md  79.2K
```

### superpowers plugin

Command:

```bash
rtk ls "$HOME/.config/codex/plugins/cache/openai-curated-remote/superpowers/6.2.0/.codex-plugin/plugin.json" "$HOME/.config/codex/plugins/cache/openai-curated-remote/superpowers/6.2.0/skills"/{writing-plans,subagent-driven-development}/SKILL.md
```

Captured output:

```text
/Users/hyonchoi/.config/codex/plugins/cache/openai-curated-remote/superpowers/6.2.0/.codex-plugin/plugin.json  1.7K
/Users/hyonchoi/.config/codex/plugins/cache/openai-curated-remote/superpowers/6.2.0/skills/subagent-driven-development/SKILL.md  27.4K
/Users/hyonchoi/.config/codex/plugins/cache/openai-curated-remote/superpowers/6.2.0/skills/writing-plans/SKILL.md  6.7K
```

## Invocation forms

The discovered skill IDs match the package metadata forms `$autoplan`,
`$superpowers:writing-plans`, `$superpowers:subagent-driven-development`,
`$review`, `$cso`, `$qa`, `$document-release`, `$document-generate`, and `$ship`.
