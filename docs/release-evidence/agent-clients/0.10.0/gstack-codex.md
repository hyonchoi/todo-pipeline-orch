# gstack / Codex release qualification

- Evidence status: `release-final`
- Release: `0.10.0`
- Source version: `0.10.0`
- Source commit: `32c108f754abe4fd8d1b54575d0a155455fc9be2`
- Profile/client: `gstack / codex`
- Timestamp: `2026-07-30T23:07:18Z`
- Environment: `macOS 26.5.2 (25F84), arm64`
- Hermes: `Hermes Agent v0.18.2 (2026.7.7.2)`
- Client: `Codex CLI 0.146.0`
- gstack: `1.60.1.0`
- superpowers: `6.2.0`
- gstack skill root: `~/.local/share/gstack`
- superpowers plugin source: `openai-curated-remote/superpowers@6.2.0`
- Verifier: `Codex final-fix session on the maintainer's workstation`
- Result: `PASS`

This release-final artifact records the passing qualification at the source
commit above for release `0.10.0`.

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
rtk hermes chat -q 'Qualification only. Load the ai-coding-agents skill, then use the terminal tool to invoke Codex exactly once in non-interactive exec mode. Run: codex exec -s read-only "Respond with exactly CODEX_DISPATCH_OK". Do not edit files. Return a concise transcript containing the exact command, the external process exit code, and the external stdout marker. If the command cannot be verified, say FAIL with the exact reason.' -Q --source tool
```

Captured output:

```text
Warning: Unknown toolsets: messaging
session_id: 20260730_190611_c689f5
Command: codex exec -s read-only "Respond with exactly CODEX_DISPATCH_OK"
Exit code: 0
Stdout marker: CODEX_DISPATCH_OK
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
codex exec "\$autoplan Qualification only: identify the autoplan skill you discovered and state that it started. Do not edit files, run project commands, or continue the review pipeline." -s read-only -c 'model_reasoning_effort="low"'
```

Captured transcript excerpt:

```text
Discovered `/autoplan` at `/Users/hyonchoi/.local/share/gstack/autoplan/SKILL.md`.
Qualification started.
```

## Invocation forms

The discovered skill IDs match the package metadata forms `$autoplan`,
`$superpowers:writing-plans`, `$superpowers:subagent-driven-development`,
`$review`, `$cso`, `$qa`, `$document-release`, `$document-generate`, and `$ship`.
