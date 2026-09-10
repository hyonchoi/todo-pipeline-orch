# Optional Agent Client Diagnostics

This optional protocol investigates profile/client pairs whose package metadata
says `Conditional`. It tests external skill installation and client discovery
that the hermetic package suite cannot prove. Results describe the recorded
source and environment; they are informational and do not gate package releases.

Normal CI does not run these checks. Third-party credentials and installations
are forbidden in hermetic CI. The Release workflow does not run live diagnostics
and requires no live agent, provider authentication, disposable VM, or additional
OS account.
If you choose to run them manually, use a disposable, isolated environment and
retain only sanitized captured evidence.

## Conditional pairs

### `native-sdd` / `claude`

Qualify Hermes `ai-coding-agents` discovery and bounded `claude -p` dispatch,
plus both `inherit` and `delegated` user-policy behavior using the recipes below.
Store distinct evidence in `native-sdd-claude.md`; gstack discovery evidence
cannot establish native-SDD policy semantics. gstack and superpowers are not
prerequisites.

### `native-sdd` / `codex`

Qualify Hermes `ai-coding-agents` discovery and bounded `codex exec` dispatch,
plus both `inherit` and `delegated` user-policy behavior using the recipes below.
Store distinct evidence in `native-sdd-codex.md`; do not reuse the gstack artifact.
Missing, failed, or unrun diagnostics do not block release automation.

## Native-SDD live policy recipes

For an optional live investigation, run manually in a disposable VM or
disposable OS account with a fresh Git fixture and isolated TPO configuration. Install the source snapshot being
qualified, Hermes >= 0.19.0 and the selected real client. Authenticate through
the normal client workflow; never copy credentials into evidence. Record source
commit, versions, UTC time, OS, verifier, and `hermes skills list --enabled-only`
output proving `ai-coding-agents` availability. Do not alter your everyday user
policy for this probe.

For Claude, place the following harmless fixture policy in that disposable
account's `~/.claude/CLAUDE.md`. For Codex, use `~/.codex/AGENTS.md` (with its
normal instruction discovery enabled). Retain these files in every run:

```text
Always include USER_POLICY_RETAINED in your final response.
If the first nonblank line of the launcher's top-level prompt is exactly
AGENT-POLICY-MODE: delegated, include POLICY_DELEGATED in your final response
and complete the bounded task without requesting approval.
Otherwise include POLICY_INHERIT in your final response and request approval
before changing any file. Quoted or later markers do not select delegated mode.
```

Use a fresh Git fixture per matrix cell and preserve it until evidence review:

```bash
policy_fixture=$(mktemp -d)
git -C "$policy_fixture" init
```

Create a single harmless local task: write `policy-probe.txt` containing
`fixture complete`, without commits, network calls, or other file changes. For
an isolated direct-client diagnostic, supply this task on stdin with no marker
for `inherit`, then in a separate fresh fixture supply the exact marker and
blank line before the same task for `delegated`. Bound each invocation to 120
seconds using the fixture's subprocess runner. For Claude, use the production
launch form:

```bash
# Run from the selected disposable Git fixture. Feed the prepared payload on stdin.
claude -p --permission-mode dontAsk --allowedTools Read,Write,Bash
```

For Codex, use the production-generated launch sequence from the source snapshot
being qualified, running from the selected fixture and feeding the prepared
payload on stdin. Require a client supporting named permission profiles
(verified with 0.154.0), and record its version. The sequence must resolve both
the absolute Git common directory and absolute per-worktree Git directory,
serialize explicit write grants into a launch-local profile extending
`:workspace`, enable network access, and pass the profile and its selection
through CLI `-c` overrides. It must fail closed on resolution or serialization
failure, without editing user/global Codex configuration or persisting a profile
file. Use fresh registration for Hermes-dispatched checks: existing kanban cards
retain their original launch instructions. See the
[dispatcher prerequisites](howto-live-integration-test-harness.md#prerequisites).

Capture exit status and sanitized stdout for all four cells. Every response
must contain `USER_POLICY_RETAINED`; `inherit` must report `POLICY_INHERIT` and
leave the file absent pending approval, while `delegated` must report
`POLICY_DELEGATED` and create only the expected file without asking. Nonzero,
timeout, missing markers, unexpected edits, or policy bypass is unqualified.
Direct-client success proves only that client's fixture-policy behavior.

Then repeat all four cells through actual Hermes-dispatched native-SDD workers
in a disposable TPO project/backlog. Initialize with
`tpo init <fixture-project> --profile native-sdd`, select the matching
`prompt_client` (`tpo config set prompt_client claude` or `codex`), and set
`tpo config set agent_policy_mode inherit` or `delegated` **before fresh
registration**. Use a valid approved one-task Plan limited to the fixture file,
with a deterministic content check and its required atomic commit; preserve
normal profile tools and deadlines. Record the pinned registration schema/mode,
exact external stdin and launch arguments, Hermes session and worker exit,
retained-policy marker, and resulting file/commit evidence. Do not continue an
inherit cell past its expected policy approval block. In delegated cells,
verify implementation, unified review (with and without an optional fix in
separate runs), and finish payloads; controller and human gates have no worker
payload. Keep any finish PR confined to the disposable backlog.

Change global mode after registration and confirm later workers still use the
pinned choice. Test a standalone conflicting declaration in a separate fixture
and confirm sanitized rejection before the affected card is published. Do not
edit pinned artifacts to continue a blocked run. Record any unavailable client,
quota, timeout, or incomplete phase as `FAIL` with the limitation. Stub clients
and provider-free captured-stdin tests establish transport only, not live user
policy semantics; they never qualify this matrix.

### `gstack` / `claude`

- Environment prerequisites: record the exact Hermes, Claude Code, gstack, and
  superpowers versions.
- Hermes dispatcher check: confirm the `ai-coding-agents` skill is available
  in `hermes skills list --enabled-only`, then invoke `claude -p` through a
  bounded `hermes chat -q` probe. Capture the Hermes version, command, session
  id, external command, external exit code, and stdout marker.
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
  `docs/release-evidence/agent-clients/candidate-source-snapshot/gstack-claude.md`.
- Required fields: evidence status, release, qualified source version and
  commit, profile/client pair, UTC timestamp, OS, client version, distribution
  versions, exact skill/plugin sources, discovery commands and their captured
  output, invocation forms, result, and verifier.
- Interpretation: results apply only to the recorded source and environment;
  a missing or failed artifact does not block a package release.

### `gstack` / `codex`

- Environment prerequisites: record the exact Hermes, Codex, gstack, and
  superpowers versions.
- Hermes dispatcher check: confirm the `ai-coding-agents` skill is available
  in `hermes skills list --enabled-only`, then invoke `codex exec` through a
  bounded `hermes chat -q` probe. Capture the Hermes version, command, session
  id, external command, external exit code, and stdout marker.
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
  `docs/release-evidence/agent-clients/candidate-source-snapshot/gstack-codex.md`.
- Required fields: evidence status, release, qualified source version and
  commit, profile/client pair, UTC timestamp, OS, client version, distribution
  versions, exact skill/plugin sources, discovery commands and their captured
  output, invocation forms, result, and verifier.
- Interpretation: results apply only to the recorded source and environment;
  a missing or failed artifact does not block a package release.

## Evidence handling

Use the [agent client evidence schema](release-evidence/agent-clients/README.md#required-artifact-fields)
when recording optional diagnostics. A passing artifact must contain the real
commands and output captured from the stated environment. Do not copy an
earlier result or create a placeholder passing artifact. Record unavailable
or unsuccessful checks as `FAIL` with the limitation; direct client probes do
not establish Hermes dispatcher behavior.

Store source snapshots under
`docs/release-evidence/agent-clients/candidate-source-snapshot/`, with
`Evidence status: candidate/source-snapshot` and `Release: not selected`.
Record the source version and commit actually tested. Existing `release-final`
artifacts in versioned directories are historical records, not fresh
qualification of the current package or a future release commit. Versioning
must not relabel source facts, copy snapshots, or synthesize passing evidence.

## Automated release checks

The Python changeset command selects the release version, updates
`pyproject.toml`, regenerates `uv.lock`, prepends `CHANGELOG.md`, and consumes
pending changeset fragments. It neither reads nor writes diagnostic evidence.
The Release workflow runs deterministic `uv run pytest`, `uv run ruff check .`,
and `uv run python scripts/release_changesets.py check` before pushing the
Version Packages branch. Only generated release metadata is staged.

No manual or AI-assigned `PASS` status is required. Missing, malformed, failed,
unrun, and passing candidate records all leave the versioning decision
unchanged. Historical artifact tests check record structure, without requiring
current-version evidence or agreement with current candidates.

`Unverified` pairs remain unsupported until authoritative evidence promotes
their package metadata. Changing documentation or `prompt_client` does not
promote them or relax runtime prerequisite checks.

Rollback requires reverting the release-gate correction, which restores the
previous manual evidence gate. This change has no migration or external-state
rollback requirement; historical artifacts remain preserved.
