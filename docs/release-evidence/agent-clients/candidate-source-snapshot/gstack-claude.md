# gstack / Claude candidate qualification

- Evidence status: `candidate/source-snapshot`
- Release: `not selected`
- Source VERSION: `0.7.0`
- Source commit: `e34ae19fe89efbbe950fe492328ed320189fe1a0`
- Profile/client: `gstack / claude`
- Timestamp: `2026-07-30T01:28:08Z`
- Environment: `macOS 26.5.2 (25F84), arm64`
- Client: `Claude Code 2.1.220`
- gstack: `1.60.1.0`
- superpowers: `6.2.0`
- gstack skill root: `~/.local/share/gstack`
- superpowers plugin source: `claude-plugins-official/superpowers@6.2.0`
- Verifier: `Codex final-fix session on the maintainer's workstation`
- Result: `PASS`

This qualifies discovery against the recorded source snapshot. It is not
release-final evidence and does not select a release version.

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

## Disposable fixture and representative invocation

Fixture isolation command:

```bash
QUAL_DIR=$(mktemp -d /tmp/tpo-agent-client-qualification-XXXXXXXX)
git -C "$QUAL_DIR" init -q
printf "# Qualification Fixture\n\nRead-only disposable project.\n" > "$QUAL_DIR/README.md"
cd "$QUAL_DIR"
test ! -d .claude && echo ".claude: absent"
test ! -d .agents && echo ".agents: absent"
git status --short
```

Captured isolation output:

```text
.claude: absent
.agents: absent
?? README.md
```

Representative invocation command:

```bash
claude -p "/autoplan Qualification only: identify the autoplan skill you discovered and state that it started. Do not edit files, run project commands, or continue the review pipeline." --max-turns 1 --output-format text
```

Captured transcript excerpt:

```text
The autoplan skill has been discovered and started. It's the `/autoplan` skill located at `/Users/hyonchoi/.claude/skills/autoplan/SKILL.md`.
```

## Invocation forms

The discovered skill IDs match the package metadata forms `/autoplan`,
`/writing-plans`, `/subagent-driven-development`, `/review`, `/cso`, `/qa`,
`/document-release`, `/document-generate`, and `/ship`.
