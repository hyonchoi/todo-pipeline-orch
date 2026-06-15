# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-06-11

### Added
- Initial release: `pipeline-watch` CLI with auto-tick, merge, and status commands
- Auto-tick discovery: scans projects for TODOS.md changes and selects eligible TODOs
- Phase 9 merge orchestration: confirm, version bump, and git merge to main
- Cron registration: `install-cron.sh` helper for 5-minute automated ticks
- CLI subcommands: `auto`, `merge`, `status`
- Pending records table showing ready-for-review records with status and age
- Configuration via environment variables: `PIPELINE_LOCK_DIR`, `PIPELINE_PROJECTS_DIR`, etc.

### Fixed
- Improved error messages for invalid arguments (e.g., non-numeric `todo_id`)

### Changed
- Updated Python version requirement from >=3.14 to >=3.9 for broader compatibility

## [0.2.0] - 2026-06-14

### Added
- **Hermes decision engine** — LLM-driven selection replaces deterministic selection (`decision/` module with context builder, agent, schema, store)
- **SHA-pinned prompts** — prompts stored with SHA-256 checksums; mismatch alerts at selection time
- **Injection fences** — fence-tag injection neutralized in untrusted regions of the decision pipeline
- **Immutable decisions + sidecar outcomes** — write-once via `os.link`, per-writer UUID temp files, rotation of stale records
- **Circuit breaker** — no-progress counter, cron backoff, Slack alert deduplication
- **Atomic tick lock** — atomic-mkdir `tick.lock` with stale sweep; prevents duplicate ticks
- **Phase markers** — `phase_started` marker write/delete around invocation; exclusive markers prevent double-run
- **Kill subcommand** — `hermes-pipeline kill <phase>` for in-flight phases; confirms process exit, releases tick lock when owned
- **Outcome sidecar** — terminal `merge_status` transitions write outcome metadata (failed, killed_by_operator)
- **Eval suite** — 8 selection-prompt fixtures with runner (`tests/eval/`); non-blocking eval workflow
- **Operator how-to guides** — Diataxis-formatted guides for config, eval, kill, and prompt-sha-mismatch troubleshooting
- **Hermes state machine** — docs for phase lifecycle (state machine table)

### Changed
- **Directory structure flattened** — `hermes-pipeline/src/hermes_pipeline/` → `hermes_pipeline/`; `hermes-pipeline/tests/` → `tests/`; `hermes-pipeline/configs/` → `configs/`
- **Raised minimum Python version** from >=3.9 to >=3.12
- **Decision-driven pipeline** — watcher and CLI pruned to delegate selection to the Hermes decision engine

### Fixed
- **Hallucinated picks rejected** — LLM must pick a TODO that exists in TODOS.md; rejects hallucinated IDs
- **Atomic state writes** — `set_merge_status` and `ready_for_review` use tmp+rename to prevent partial writes
- **Best-effort outcome sidecar** — `set_merge_status` doesn't fail if sidecar write fails
- **Config path resolution** — phases resolve config relative to flattened directory structure
- **Kill targets `child_pid`** — kill subcommand targets the phase child process, not the watcher
- **Canonical RFR filename** — decision context normalizes filename and checks PID liveness on sweep

### Removed
- **Deterministic `selection.py`** — replaced by Hermes LLM-driven decision engine
- **Redundant `hermes-pipeline/README.md`** — documentation consolidated in root docs

## [0.3.0] - 2026-06-15

### Added
- **Hermes adapter** — `hermes_pipeline/hermes_adapter.py` with `hermes_call()` (simple one-shot queries) and `hermes_agent_call()` (agent-style subprocess with PID tracking). All LLM traffic now routes through `hermes chat -q` instead of direct Anthropic SDK calls.
- **HermesCallError and HermesAgentResult** — structured error and result types for Hermes CLI failures and agent outcomes.
- **.env file support** — `.env` files are now git-ignored (`.env.example` is allowed).
- **CI action pinning** — GitHub Actions pinned to SHA hashes for supply-chain security.

### Changed
- **Decision agent** — `_anthropic_call()` replaced with `_hermes_call()`; no longer imports the `anthropic` package. Timeout is computed from `max_tokens` (1s per 100 tokens, min 30s, max 300s).
- **Phase execution** — `_run_claude_subprocess()` replaced with `hermes_agent_call()`. Tool and turn constraints are now encoded as prompt headers since `hermes chat -q` lacks `--tools`/`--turns` flags.
- **Requirements** — Anthropic API key is no longer needed for runtime selection (eval suite still checks for it as a skip gate). Hermes CLI must be installed and authenticated (`hermes login`) instead.

### Removed
- **Anthropic SDK dependency** — `anthropic>=0.40` removed from `pyproject.toml`. The orchestrator no longer calls the Anthropic API directly.

## [Unreleased]

### Planned
- Dashboard UI for pipeline status
- Slack/Discord notifications for merge events
- Migration guides for breaking changes
