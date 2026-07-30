# gstack / Codex candidate qualification

- Evidence status: `candidate/source-snapshot`
- Release: `not selected`
- Source VERSION: `0.7.0`
- Source commit: `d150862b2430b083122145de85b8264fac43f7bd`
- Profile/client: `gstack / codex`
- Timestamp: `2026-07-30T07:24:46Z`
- Environment: `macOS 26.5.2 (25F84), arm64`
- Client: `Codex CLI 0.146.0`
- gstack: `1.60.1.0`
- superpowers: `6.2.0`
- gstack skill root: `~/.local/share/gstack`
- superpowers plugin source: `openai-curated-remote/superpowers@6.2.0`
- Verifier: `Codex final-fix session on the maintainer's workstation`
- Result: `PASS`

This qualifies discovery against the recorded source snapshot. It is not
release-final evidence and does not select a release version.

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
Discovered skill: `autoplan` at `/Users/hyonchoi/.local/share/gstack/autoplan/SKILL.md`.

`/autoplan` started. I am stopping here per qualification instructions: no file edits, no project commands, no review pipeline.
```

## Invocation forms

The discovered skill IDs match the package metadata forms `$autoplan`,
`$superpowers:writing-plans`, `$superpowers:subagent-driven-development`,
`$review`, `$cso`, `$qa`, `$document-release`, `$document-generate`, and `$ship`.
