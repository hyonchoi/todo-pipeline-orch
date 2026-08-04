# gstack / Claude release qualification

- Evidence status: `release-final`
- Release: `0.7.0`
- Source VERSION: `0.7.0`
- Source commit: `32c108f754abe4fd8d1b54575d0a155455fc9be2`
- Profile/client: `gstack / claude`
- Timestamp: `2026-07-30T23:07:18Z`
- Environment: `macOS 26.5.2 (25F84), arm64`
- Hermes: `Hermes Agent v0.18.2 (2026.7.7.2)`
- Client: `Claude Code 2.1.220`
- gstack: `1.60.1.0`
- superpowers: `6.2.0`
- gstack skill root: `~/.local/share/gstack`
- superpowers plugin source: `claude-plugins-official/superpowers@6.2.0`
- Verifier: `Codex final-fix session on the maintainer's workstation`
- Result: `PASS`

This release-final artifact records the passing qualification refreshed at the
source commit above after `/ship` selected release `0.7.0`.

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

### Hermes enabled-skill discovery

Command:

```bash
rtk hermes skills list --enabled-only
```

Captured output:

```text
Installed Skills (enabled only)
Name              Category  Source  Trust  Status
ai-coding-agents            local   local  enabled
0 hub-installed, 0 builtin, 1 local — 1 enabled shown
```

### Hermes dispatcher invocation

Command:

```bash
rtk hermes chat -q 'Qualification only. Load the ai-coding-agents skill, then use the terminal tool to invoke Claude Code exactly once in non-interactive print mode. Run: claude -p "Respond with exactly CLAUDE_DISPATCH_OK" --max-turns 1 --output-format text. Do not edit files. Return a concise transcript containing the exact command, the external process exit code, and the external stdout marker. If the command cannot be verified, say FAIL with the exact reason.' -Q --source tool
```

Captured output:

```text
Warning: Unknown toolsets: messaging
session_id: 20260730_190540_fd0254
Command: claude -p "Respond with exactly CLAUDE_DISPATCH_OK" --max-turns 1 --output-format text
Exit code: 0
Stdout marker: CLAUDE_DISPATCH_OK
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
## /autoplan Skill Identified and Started

**Skill:** `autoplan` — Auto-Review Pipeline from `~/.claude/skills/autoplan/SKILL.md`

**Status:** Qualification only — per your arguments, I'm identifying the skill and stopping here. No files edited, no project commands run, no review pipeline phases executed.
```

## Invocation forms

The discovered skill IDs match the package metadata forms `/autoplan`,
`/writing-plans`, `/subagent-driven-development`, `/review`, `/cso`, `/qa`,
`/document-release`, `/document-generate`, and `/ship`.
