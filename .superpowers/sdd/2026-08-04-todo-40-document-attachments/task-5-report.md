# Task 5 Report: Validate audit behavior and packaged installation parity

## Status

Implemented, verified within the provider-free boundary, committed, and
self-reviewed. The full suite reproduces the inherited eight live Hermes
provider failures caused by HTTP 429 quota exhaustion.

Commit: `36f55ae Verify TODO attachment audit and installation`

## Implementation

- Extended the `--audit` contract to validate every present Plan, Spec, and
  Reference value through the shared `sections/document-attachments.md` path
  policy.
- Required one TODO-, role-, and path-specific audit finding per attachment
  defect, including missing files, directory targets, repository containment
  escape, outside-target symlinks, and literal commas in Reference paths.
- Kept attachment audit behavior non-mutating: audit never requires, removes,
  replaces, or repairs attachments. Existing tracked-ID reconciliation remains
  unchanged.
- Added exact audit finding and remediation examples for every required path
  defect.
- Added a coverage table mapping all authoritative TODO-40 specification groups
  to acceptance scenarios or repository contract tests, with runtime
  selection, prompt consumption, worktree creation, and execution explicitly
  assigned to TODO-39 and outside this suite.
- Required `document-attachments.md` in packaged resources.
- Added a project-scoped Codex install test that compares installed `SKILL.md`
  and the complete installed `sections/*.md` filename set and bytes against
  `importlib.resources`.

## Files

- `hermes_pipeline/data/skills/todos-manager/SKILL.md`
- `hermes_pipeline/data/skills/todos-manager/sections/error-messages.md`
- `hermes_pipeline/data/skills/todos-manager/sections/acceptance-scenarios.md`
- `tests/test_skills_install.py`
- `tests/test_todos_manager_skill_contract.py`

The installed `.agents/skills/todos-manager` copy was treated only as a
generated verification artifact and removed after parity verification. No
installed mirror was edited directly or committed.

## Initial structural TDD evidence

The evidence in this section verifies the Markdown contract and installation
mechanics implemented by the original Task 5 pass. It did not execute the
attachment interaction behavior; Fix Round 1 below adds that behavioral
coverage.

### RED

Command:

```text
rtk uv run pytest tests/test_todos_manager_skill_contract.py tests/test_skills_install.py -q
```

- Initial audit/package RED: `2 failed, 48 passed`; failures showed absent
  non-mutating audit wording and absent path-defect examples.
- Final RED after adding the acceptance-coverage contract:
  `3 failed, 48 passed`; the third failure showed the authoritative-spec
  coverage table was absent.

All failures were the intended missing Task 5 contracts, not setup or syntax
errors.

### GREEN

The same command after implementation: `51 passed in 0.14s`.

Complete relevant suite:

```text
rtk uv run pytest tests/test_todos_manager_skill_contract.py tests/test_skills_install.py tests/skill-test-environment/unit tests/test_todos_md.py -q
```

Result: `166 passed in 0.32s`.

## Fresh installed-copy evidence

Command:

```text
rtk uv run tpo skills install --target codex --scope project --reinstall
```

Result: installed successfully to
`.agents/skills/todos-manager` in the Task 5 worktree.

Direct recursive comparison:

```text
rtk diff -qr hermes_pipeline/data/skills/todos-manager .agents/skills/todos-manager
```

Result: exit 0 with no differences.

Dedicated byte-parity test:

```text
rtk uv run pytest tests/test_skills_install.py::TestCmdSkillsInstall::test_project_install_matches_packaged_skill_byte_for_byte -q
```

Result: `1 passed in 0.06s`.

## Repository verification

Full suite:

```text
rtk uv run pytest -q
```

Result: `8 failed, 1198 passed, 3 skipped in 149.58s`.

All eight failures were live cases in `tests/eval/runner.py`:

- `clean_strict`
- `clean_strict_schema`
- `empty_todos`
- `heavy_drift_no_metadata`
- `injection_attempt`
- `mid_drift_freeform_notes`
- `outcome_aware_avoids_failed`
- `respects_in_flight`

Each failed because `hermes chat` exhausted three retries and returned
`HTTP 429: The usage limit has been reached`. This matches the inherited live
provider failure boundary and is not caused by the Task 5 diff.

Provider-free suite:

```text
rtk uv run pytest -q --ignore=tests/eval
```

Result: `1194 passed, 3 skipped in 59.70s`.

Lint:

```text
rtk uv run ruff check .
```

Result: `All checks passed!`.

Whitespace verification:

```text
rtk git diff --check
rtk git diff --cached --check
rtk git diff 36f55ae^ 36f55ae --check
```

Result: all exited 0 with no whitespace findings.

All `uv` commands also emitted the pre-existing warning that the parent
repository `VIRTUAL_ENV` differs from the worktree `.venv`; uv correctly
ignored it and used the worktree environment.

## Self-review

- Confirmed commit `36f55ae` contains exactly the five files prescribed by the
  Task 5 brief: 137 insertions and 3 deletions.
- Confirmed the worktree is clean after the commit and generated-mirror
  cleanup.
- Confirmed audit attachment validation delegates to the already canonical
  shared policy instead of duplicating path rules.
- Confirmed the wording does not accidentally make optional attachments
  required and does not disable the existing tracked metadata repair contract.
- Confirmed install parity checks both the complete Markdown section filename
  set and each file's bytes, so a missing, extra, or altered installed policy
  fails the test.
- Confirmed the coverage table names the TODO-39 boundary rather than implying
  TODO-40 covers runtime selection or worktree behavior.
- No Task 5 implementation defects found during self-review.

## Concerns

- Full live verification remains externally blocked by Hermes provider quota:
  eight HTTP 429 failures. Provider-free verification is green.
- No other concerns.

## Fix Round 1

> Superseded by Fix Round 2. Round 1's pure-Python model was disconnected from
> the packaged Markdown skill, so its claims of complete executable coverage
> were too broad. The Round 2 section records the corrected boundary.

### Status and behavior changes

Added a deterministic pure-Python attachment-policy harness and executable
matrix coverage for the authoritative design contract. The tests now drive:

- zero, one, and multiple candidate states; manual and omitted add choices;
- add/revise confirmation, ambiguity, combined roles, and write gating;
- revise preservation, replacement, removal, invalid-existing warnings,
  Reference append/removal/order/deduplication, and Plan/Spec exclusion;
- explicit/Git/search discovery precedence, candidate/read/search limits,
  exclusions, recognized gstack/Superpowers documents, and semantic fallback;
- normalization, missing/directory/traversal/outside-symlink validation,
  field-local recovery without rediscovery, non-mutating audit, and legacy
  entries without attachments; and
- real project installation with byte-for-byte packaged/installed parity.

Resolved the literal-comma ambiguity without adding escaping. A known single
Reference candidate containing a comma is rejected before storage. In stored
`Reference:` text, every comma is unconditionally a separator; audit trims and
validates each token, reports empty tokens, and never guesses that two stored
tokens were one comma-containing filename.

The coverage table now has an executable-test column and an explicit mapping
column. Its contract test parses the table, requires the complete row set, and
checks the named executable tests for attachment cardinality, validation
recovery, and the completion matrix.

### Files

- `hermes_pipeline/data/skills/todos-manager/SKILL.md`
- `hermes_pipeline/data/skills/todos-manager/sections/acceptance-scenarios.md`
- `hermes_pipeline/data/skills/todos-manager/sections/document-attachments.md`
- `hermes_pipeline/data/skills/todos-manager/sections/error-messages.md`
- `hermes_pipeline/data/skills/todos-manager/sections/schema.md`
- `tests/skill-test-environment/skill_logic.py`
- `tests/skill-test-environment/unit/test_document_attachments.py`
- `tests/test_todos_manager_skill_contract.py`

### Executable TDD evidence

RED after adding the behavior matrix and strengthened coverage-table test:

```text
rtk uv run pytest tests/skill-test-environment/unit/test_document_attachments.py tests/test_todos_manager_skill_contract.py -q
21 failed, 13 passed in 0.48s
```

The failures were the intended unimplemented attachment behaviors and absent
three-column coverage mapping. A second focused RED for invalid-existing
warning behavior was:

```text
rtk uv run pytest tests/skill-test-environment/unit/test_document_attachments.py::test_revise_warns_about_invalid_existing_paths_without_blocking_other_edits -q
1 failed in 0.04s
```

GREEN after the behavior implementation and contract correction:

```text
rtk uv run pytest tests/skill-test-environment/unit/test_document_attachments.py tests/test_todos_manager_skill_contract.py -q
35 passed in 0.07s
```

Relevant unit, contract, install, and TODO suites:

```text
rtk uv run pytest tests/test_todos_manager_skill_contract.py tests/test_skills_install.py tests/skill-test-environment/unit tests/test_todos_md.py -q
187 passed in 0.32s
```

Fresh installed-copy verification:

```text
rtk uv run tpo skills install --target codex --scope project --reinstall
OK (codex): installed todos-manager to <worktree>/.agents/skills/todos-manager

rtk diff -qr hermes_pipeline/data/skills/todos-manager .agents/skills/todos-manager
exit 0, no differences

rtk uv run pytest tests/test_skills_install.py::TestCmdSkillsInstall::test_project_install_matches_packaged_skill_byte_for_byte -q
1 passed in 0.04s
```

The generated `.agents` install mirror was removed after verification.

Additional verification:

```text
rtk uv run pytest -q --ignore=tests/eval
exit 0; RTK suppressed the all-passing output

rtk uv run ruff check .
All checks passed!

rtk git diff --check
exit 0, no output
```

All `uv` commands emitted the pre-existing warning that the parent
`VIRTUAL_ENV` differs from the worktree `.venv`; uv ignored it and used the
worktree environment.

### Self-review

- Every completion item in the authoritative design has an executable test or
  the existing real install-parity test, and the table parser prevents required
  rows or named mappings from disappearing silently.
- Behavioral assertions use filesystem effects, returned state, diagnostics,
  counters, and write callbacks rather than checking for Markdown phrases.
- The Reference representation is deterministic and adds no unsupported
  escaping syntax.
- Task 1-4 role semantics and workflow interfaces remain unchanged; only the
  impossible stored literal-comma audit diagnostic was corrected.
- The generated install mirror is absent and unrelated worktree content was
  not modified.

### Concerns

- Live `tests/eval` provider cases were not rerun in this fix round; the
  preceding Task 5 run documents their external HTTP 429 quota failure.
- No implementation concerns remain in the provider-free boundary.

## Fix Round 2

> Superseded in part by Fix Round 3. Round 2 did not yet connect workflow
> completion to locked TODO mutation, audit real TODO text, or fully drive
> confirmation, discovery accounting, and relevance from structured policy.

### Behavior and contract changes

- Embedded a versioned JSON policy block in the authoritative packaged
  `sections/document-attachments.md`. The harness parses this block and uses
  it for limits, sources, exclusions, field names, Reference syntax, and exact
  defect classes. Loading also fails unless `SKILL.md`, `auto-research.md`, and
  `revise.md` remain routed to the authoritative section.
- The real install-parity test now parses packaged and freshly installed policy
  and compares the resulting structures in addition to byte parity.
- Added real canonical `TODOS.md` mutation: cancellation preserves bytes and
  approval atomically adds/removes Plan, Spec, and Reference fields in the
  selected entry.
- Lone suggestions now require explicit selection; plain `confirm` cannot
  silently select them.
- Stored Reference parsing reports every empty token and continues validating
  all non-empty tokens.
- Plan/Spec selection, replacement, and combined selection reject paths already
  present in Reference; Reference also reports `preserved` for existing values.
- Searches are counted by invocation rather than returned path. Generic subject
  substrings no longer establish relevance; explicit context, TODO ID, or
  concrete-target overlap is required.
- Missing files, directories, and other non-regular targets now produce
  distinct defects. Candidate records include `validation`.

### Files

- `hermes_pipeline/data/skills/todos-manager/SKILL.md`
- `hermes_pipeline/data/skills/todos-manager/sections/acceptance-scenarios.md`
- `hermes_pipeline/data/skills/todos-manager/sections/document-attachments.md`
- `hermes_pipeline/data/skills/todos-manager/sections/revise.md`
- `tests/skill-test-environment/skill_logic.py`
- `tests/skill-test-environment/unit/test_document_attachments.py`
- `tests/test_skills_install.py`
- `tests/test_todos_manager_skill_contract.py`

### RED/GREEN evidence

Initial RED after adding packaged-policy and real-Markdown behavior imports:

```text
rtk uv run pytest tests/skill-test-environment/unit/test_document_attachments.py -q
1 error during collection: cannot import name 'apply_attachment_selection_to_todo'
```

After the executable boundary existed, the focused suite exposed two remaining
behavior defects (real Markdown rendering and strong-relevance fixtures):

```text
rtk uv run pytest tests/skill-test-environment/unit/test_document_attachments.py -q
2 failed, 25 passed in 0.07s
```

Final focused GREEN:

```text
rtk uv run pytest tests/skill-test-environment/unit/test_document_attachments.py tests/test_todos_manager_skill_contract.py tests/test_skills_install.py::TestCmdSkillsInstall::test_project_install_matches_packaged_skill_byte_for_byte -q
42 passed in 0.15s
```

Relevant suites and checks:

```text
rtk uv run pytest tests/test_todos_manager_skill_contract.py tests/test_skills_install.py tests/skill-test-environment/unit tests/test_todos_md.py -q
193 passed in 0.32s

rtk uv run pytest -q --ignore=tests/eval
exit 0; RTK suppressed all-passing output

rtk uv run ruff check .
All checks passed!

rtk git diff --check
exit 0, no output
```

Fresh install verification:

```text
rtk uv run tpo skills install --target codex --scope project --reinstall
OK (codex): installed todos-manager to <worktree>/.agents/skills/todos-manager

rtk diff -qr hermes_pipeline/data/skills/todos-manager .agents/skills/todos-manager
exit 0, no differences

rtk uv run pytest tests/test_skills_install.py::TestCmdSkillsInstall::test_project_install_matches_packaged_skill_byte_for_byte -q
1 passed in 0.08s
```

The generated `.agents` mirror was removed after verification.

### Honest verification boundary

Provider-free tests now execute the deterministic contract parsed from the
packaged/installed Markdown and mutate real TODO Markdown. They do not execute
an LLM's interpretation of unconstrained prose. Live agent conformance remains
an integration boundary requiring an agent runner; this suite fails closed if
the executable policy block or its Markdown routing disappears, but cannot
prove that every future agent follows surrounding narrative instructions.

### Concerns

- Live `tests/eval` provider cases were not rerun because the existing report
  records external HTTP 429 quota exhaustion.
- No provider-free failures remain.

## Fix Round 3

### Changes

- Structured policy now drives discovery source order, confirmation behavior,
  accepted relevance signals, limits, exclusions, fields, errors, and Reference
  syntax. The loader still verifies SKILL/auto-research/revise routing.
- Discovery accepts already-consumed read/search counters, counts each search
  invocation including empty/repeated-result invocations, and supports close
  title/summary scope without treating one generic keyword as sufficient.
- `AttachmentWorkflow.finish` now performs approved real TODO mutation itself;
  cancellation performs no write.
- Actual canonical TODO Markdown is parsed for attachment audit and remains
  byte-identical after audit.
- Lone Reference suggestions require explicit selection just like Plan/Spec.
- Combined-role conflicts validate before mutation, preserving prior state.
- TODO mutation matches exact parsed header IDs and derives its replacement
  from the fresh text read under the TODO lock.
- Existing References report `preserved` only until append/removal changes them.

### Files

- `hermes_pipeline/data/skills/todos-manager/sections/document-attachments.md`
- `tests/skill-test-environment/skill_logic.py`
- `tests/skill-test-environment/unit/test_document_attachments.py`

### RED/GREEN and verification

Initial RED:

```text
rtk uv run pytest tests/skill-test-environment/unit/test_document_attachments.py -q
1 error during collection: cannot import name 'audit_todo_markdown'
```

Behavioral RED after the new boundary existed:

```text
rtk uv run pytest tests/skill-test-environment/unit/test_document_attachments.py -q
3 failed, 33 passed in 0.13s
```

Focused GREEN:

```text
rtk uv run pytest tests/skill-test-environment/unit/test_document_attachments.py tests/test_todos_manager_skill_contract.py -q
50 passed in 0.10s
```

Final relevant suite and checks:

```text
rtk uv run pytest tests/test_todos_manager_skill_contract.py tests/test_skills_install.py tests/skill-test-environment/unit tests/test_todos_md.py -q
202 passed in 0.36s

rtk uv run pytest -q --ignore=tests/eval
exit 0; RTK suppressed all-passing output

rtk uv run ruff check .
All checks passed!

rtk git diff --check
exit 0, no output
```

Fresh install parity:

```text
rtk uv run tpo skills install --target codex --scope project --reinstall
OK (codex): installed todos-manager to <worktree>/.agents/skills/todos-manager

rtk diff -qr hermes_pipeline/data/skills/todos-manager .agents/skills/todos-manager
exit 0, no differences

rtk uv run pytest tests/test_skills_install.py::TestCmdSkillsInstall::test_project_install_matches_packaged_skill_byte_for_byte -q
1 passed in 0.06s
```

The generated mirror was removed. The honest boundary remains the same: these
tests execute deterministic packaged policy and real TODO Markdown, not a live
LLM's interpretation of narrative prose. Live provider evals were not rerun
because the existing report records HTTP 429 quota exhaustion.

## Fix Round 4

### Changes

- Routed zero, one, and multiple candidate confirmation through the structured
  `confirmation` policy for Plan, Spec, Reference, and combined Plan/Spec
  choices. Focused tests change each policy action at runtime, so hard-coded
  cardinality behavior or a lost Markdown-policy route is detected.
- Added structured close-scope policy for the minimum distinct specific-term
  overlap and the generic vocabulary that cannot contribute. Close title and
  summary relevance now requires at least two non-generic shared terms;
  generic planning language such as change, implementation, verify, and tests
  cannot qualify an unrelated document.
- Marked field-wide Reference removal as changed when the resulting list
  differs from the original selection. `choose_none("Reference")` now reports
  `selected` after clearing existing References rather than falsely reporting
  `preserved`.

### Files

- `hermes_pipeline/data/skills/todos-manager/sections/document-attachments.md`
- `tests/skill-test-environment/skill_logic.py`
- `tests/skill-test-environment/unit/test_document_attachments.py`
- `.superpowers/sdd/2026-08-04-todo-40-document-attachments/task-5-report.md`

### RED/GREEN evidence

Policy-driven confirmation RED:

```text
rtk uv run pytest tests/skill-test-environment/unit/test_document_attachments.py -q -k 'confirmation_policy'
9 failed, 1 passed, 36 deselected in 0.16s
```

The failures covered zero candidates for all three roles, one candidate for
Plan and Spec, multiple candidates for all three roles, and the combined-role
choice. The existing lone-Reference policy branch was the one passing case.

Policy-driven confirmation GREEN:

```text
rtk uv run pytest tests/skill-test-environment/unit/test_document_attachments.py -q -k 'confirmation_policy or add_candidate_cardinality'
13 passed, 33 deselected in 0.08s
```

Close-scope relevance RED:

```text
rtk uv run pytest tests/skill-test-environment/unit/test_document_attachments.py -q -k 'generic_planning_overlap or close_title_summary_scope or close_scope_relevance_uses'
3 failed, 1 passed, 45 deselected in 0.11s
```

The generic-overlap regression and both structured-policy override cases
incorrectly admitted candidates; the specific cache-eviction close-scope case
already passed.

Close-scope relevance GREEN:

```text
rtk uv run pytest tests/skill-test-environment/unit/test_document_attachments.py -q -k 'generic_planning_overlap or close_title_summary_scope or close_scope_relevance_uses or generic_subject_substring'
5 passed, 44 deselected in 0.04s
```

Field-wide Reference removal RED:

```text
rtk uv run pytest tests/skill-test-environment/unit/test_document_attachments.py::test_choose_none_reference_reports_field_wide_removal_as_selected -q
1 failed in 0.05s
```

The observed state was `preserved` instead of `selected`.

Complete attachment behavior GREEN:

```text
rtk uv run pytest tests/skill-test-environment/unit/test_document_attachments.py -q
50 passed in 0.07s
```

Relevant suites:

```text
rtk uv run pytest tests/test_todos_manager_skill_contract.py tests/test_skills_install.py tests/skill-test-environment/unit tests/test_todos_md.py -q
216 passed in 0.33s
```

Provider-free repository suite:

```text
rtk uv run pytest -q --ignore=tests/eval
1294 passed, 3 skipped in 60.58s
```

Lint and whitespace verification:

```text
rtk uv run ruff check .
All checks passed!

rtk git diff --check
exit 0, no output
```

Fresh install parity:

```text
rtk uv run tpo skills install --target codex --scope project --reinstall
OK (codex): installed todos-manager to <worktree>/.agents/skills/todos-manager

rtk diff -qr hermes_pipeline/data/skills/todos-manager .agents/skills/todos-manager
exit 0, no differences

rtk uv run pytest tests/test_skills_install.py::TestCmdSkillsInstall::test_project_install_matches_packaged_skill_byte_for_byte -q
1 passed in 0.05s
```

The generated `.agents` mirror was removed after verification.

### Honest verification boundary

The provider-free suite executes the deterministic policy parsed from the
packaged Markdown, policy overrides that prove behavioral routing, and real
TODO Markdown mutation. It does not execute a live LLM's interpretation of the
narrative skill text. Live `tests/eval` cases were not rerun because the prior
Task 5 evidence records external HTTP 429 quota exhaustion; this round did not
claim live provider conformance.
