# Changelog

## 1.0.1

### Patch Changes

- Align the happy-path harness with embedded GitHub issue Plans and recoverable empty run anchors.

## 1.0.0

### Major Changes

- tpo test runs against a live GitHub sandbox repository (--repo/--init-sandbox); the offline fake-gh harness and --phase are removed. kanban_tasks.register_todo_phases (and its transform_prepared hook) is removed; call prepare_todo_phases then create_prepared_todo_phases.

- The default phase profile is now native-sdd: tpo init writes profile: native-sdd unless --profile is given, and gstack is deprecated (deprecated: true in its phases.yaml; deprecation notices from tpo init, tpo doctor, and tpo tick, and a DEPRECATED: doctor line plus a tick warning for contracts with no profile key, which still resolve to gstack). Pass --profile gstack to keep the old flow, or migrate with tpo init <project> --force --profile native-sdd. tpo test gains a bounded multi-tick driver so native-sdd runs reach the PR handoff (human-gate blocked). See ADR-0004.

- Plan-manifest compilation no longer emits a per-task `validate:<task-id>` controller gate. Each Plan task is one worker card chained onto the previous worker, so a manifest run never opens a human-input boundary between tasks; the final human merge gate is unchanged. This is what lets a manifest run execute autonomously end to end.

TPO still parses and verifies every worker's `metadata.tpo_result` and Git facts before the chain advances, and the check is now anchored where the gate never anchored it: every task's reported commit must be an ancestor of the run branch's HEAD (`unreachable_commit`), which rejects a commit that `git reset --hard` discarded even though its topology still verifies. Which check applies is derived from board state rather than local files, so a lost or moved state directory cannot turn a resumed run into a verification failure: a task that a later card is built on is verified by immutable topology, and the chain tip is additionally verified against current HEAD and a clean worktree. One guarantee is deliberately given up: because a worker now starts as soon as its predecessor closes, the worktree's cleanliness between tasks is no longer observed, and only transient dirt removed before review escapes. Review (`verify_read_only_review`) and finish verification remain the backstops.

A validation failure blocks nothing and creates no card. It reports no tick progress and writes a run-scoped `result-validation-blocked` marker beside the run's other evidence, cleared once the results validate; the structural stall paths write the same marker with `chain_wiring_incomplete` or `gate_completion_failed`. The circuit-breaker alert now names the stuck step and code, and `tpo doctor` prints `RESULT VALIDATION BLOCKED` with a pointer to the marker, without changing its verdict. Independent review now opens on the last Plan worker, and `pinned_tick_budget` is one tick per Plan task instead of one per worker/gate pair.

Runs registered before this change keep their `validate:<id>` step keys and their gates are still completed, so they resume cleanly. Downgrading is not supported: a run registered after this change lists no `validate:<id>` keys, which an older TPO rejects as `registration_invalid` on every tick with no path out, so do not downgrade past this release while a manifest run is active.

- TPO no longer manufactures kanban cards to represent gates. It reads the phase states Hermes already maintains.

Hermes has statuses and a dispatcher; it has no notion of a "gate" phase. TPO invented one and then needed three more layers to prop it up: a gate phase became a real card created unassigned (`--assignee "-"`) so no worker would take it, which made it invisible to the dispatcher and meaningless to TPO's own poller, which is why TPO then called `hermes kanban block` to force it into `blocked` -- the one status the poller read as terminal. That last step was broken in production: `block_task` updates only rows whose status is `running` or `ready`, and a freshly created card is `todo`, so the update matched nothing, the CLI exited 1, and the create-once guard (`if tasks.get(KEY) is None`) meant it was never retried. `recompute_ready` then promoted the card `todo -> ready` and nothing could move it again. Runs stalled with `review-acceptance` sitting unassigned in `ready` and no pull request.

Nothing replaces it, because Hermes already does this. A worker that exits non-zero is retried until `failure_limit`, at which point `_record_task_failure` transitions the card `ready -> blocked` and emits `gave_up`; `_has_sticky_block` then keeps `recompute_ready` from ever promoting it back. Every phase prompt already tells the worker to exit non-zero. That is the gate. Validating a phase means observing the state it lands in, not creating a card to stand for the observation.

Removed: `_block_gate_task` and `_mark_gate_needs_input`; gate-card synthesis in `prepare_todo_phases`, `create_prepared_todo_phases` and `review_reconciliation._create_task`; the `review-acceptance` card and `REVIEW_ACCEPTANCE_KEY`; the per-round `review:<n>` barrier and `fix-validation:<n>` cards; the `human-gate` card, `HUMAN_GATE_KEY`, `_human_merge_gate`, `_needs_input` and `_block`; and `harness._auto_complete_gate_tasks`. `hermes kanban block` is no longer invoked anywhere in TPO. A `gate: true` phase now registers no card at all, so `planned_phase_keys` and the expected-phase sentinel omit it.

`classify_pinned_run` judges a run from phase states alone and takes no `run_dir`: any card `failed`, `archived` or `blocked` is a failed run -- `blocked` is now unambiguous, since only Hermes's failure gate produces it -- and a run is delivered when every card is `done`, the `finish` card included. `finish` is the ordinary worker that runs the repository gates, pushes the branch and opens the pull request, and it is the last card a pinned run creates, which is what separates a delivered board from the all-done board that appears between reconciler hops. The pull-request invariant is still proved separately by `verify_pull_request`. `all_phases_complete` now counts `blocked` as terminal for the same reason, so a genuinely failed run releases the tick lock instead of holding it forever.

Where TPO used to write a diagnostic onto a gate card it cannot create, delivery and review reconciliation now log the code and report no progress; the tick already turns that into a circuit-breaker alert. Review acceptance is recorded only by the `accepted-review-head` file that was already the delivery authority, and the `finish-verified` marker is kept for its one remaining job -- latching the live-worktree check that cannot be repeated once the worktree moves on -- and is no longer consulted for the run verdict.

The bundled profiles keep `gate: true` and `kind:` in `phases.yaml` and stay valid; `gate` now means only "registers no card", and `kind` is parsed but read by nothing. Removing both keys from the profiles is a follow-up.

- Deliver the phase profile's prompt to the external client unmodified, and ask
the result contract only for facts the dispatcher can observe.

The `metadata.tpo_result` template used to be composed into the work
instruction, so it landed inside `BEGIN/END EXTERNAL AGENT PROMPT` and was
passed verbatim to the external client along with the profile's prompt. The
client therefore received ~30 lines of JSON schema addressed to the Hermes
dispatcher, which made a failure impossible to attribute to the profile under
test -- and unfollowable: it asked the client for its own session id and told
it not to report a result object on failure, so a read-only review card that
found no defects still blocked. The template now sits in the delegation block
that already referred to it as "below"; the delimited block carries only what
the profile or the Plan task wrote.

BREAKING: `metadata.tpo_result` no longer accepts (or requires) `tdd` and
`external_session_id`. Neither had a consumer outside the validator: TPO never
re-ran the reported commands, and the dispatcher already reports the session id
on the failure path from what it tracked. `git`, `acceptance`, `review` and
`delivery` are unchanged. Drain any in-flight run before upgrading; a worker
card created by the previous release reports the old shape and is rejected as
`malformed_result`. The command validator's error code, which only delivery
checks can now reach, is reported as `invalid_command` rather than
`invalid_tdd`.

- The phase profile is the specification, and TPO now renders its prompts instead of substituting its own. Under a plan manifest the review and delivery cards used to carry prompts TPO authored -- a read-only review with an empty tool set, and a finish instruction forbidding the very commit the profile mandates -- so `phase_5_review` and `phase_8_finish_branch` contributed only their phase-key names. Both cards are now rendered from the profile's own prompt through `_render_phase_prompt`, with per-card facts (the reviewed head, the accepted review head, the branch) carried in the non-templated pipeline-context header, and they take their `tools`, `turns` and `timeout` from the profile phase (the review card was created with no tools and a 1800s timeout against the profile's `Read,Write,Edit,Bash` and 2400s). Deferred creation is unchanged: the review card still opens only once the Plan tasks are done, and the finish card only once the review is accepted.

The review-round machinery that contradicted the profile is gone. `MAX_REVIEW_ROUNDS`, `_ensure_round`, `_ensure_rereview` and the `review-fix:<n>` / `re-review:<n>` / `fix-validation:<n>` cards are removed: the profile's reviewer applies its own findings as one review-fix commit, so review is binary -- a `done` review card is the pass and a `blocked` one is the profile's own nonzero exit.

The result contract follows. `verify_read_only_review` is removed along with the optional `review` section, `ReviewEvidence`, and the findings/verdict validation and template, because nothing reads a verdict once rounds are gone. `render_result_template` no longer takes `pinned_head_sha`; no card pins its SHAs, since a review or finish card may legitimately add one commit. A new `verify_optional_single_commit` proves the 0-or-1 bound for both cards -- reported parent is the recomputed anchor, the reported commit really has that parent, its real diff matches `changed_files`, and the reported head is reachable from HEAD. `accepted-review-head` now records the review card's `resulting_head_sha` rather than the pre-review head, so the anchor includes the review's own fix commit, and `_verify_finish` requires `accepted_head` to be an ancestor of HEAD with at most one commit between them instead of exact equality. `ValidatedRegistration` exposes the run's pinned `profile`.

- Delivery under a `requires_plan` profile can complete again, and three checks that were weaker than their docstrings claimed now hold.

`render_result_template` no longer pre-fills `"changed_files": []` when a card may legitimately add nothing. The template's standing instruction is to replace every `<...>` placeholder and to keep pre-filled values verbatim, so an obedient worker kept the empty list -- while the profile MANDATES a commit from `phase_8_finish_branch` ("commit those as one separate atomic commit"). The real diff was then non-empty, `verify_optional_single_commit` raised `changed_files_mismatch`, and no compliant finish worker could ever be accepted: delivery could not complete at all. The field is now a placeholder describing the condition in both modes, and the `changed_files` guidance line is always published instead of being suppressed exactly when the value is conditional. `parse_worker_result` also tolerates a `review` key again, parsed and ignored, so a review card opened before the review section was removed (2400s timeout) can still land rather than becoming `malformed_result` forever; `SCHEMA_VERSION` stays 1, since bumping it would reject every in-flight card instead of one key's worth, and the bump belongs in the release that drops the key.

Reachability is now membership of HEAD's first-parent mainline, not `merge-base --is-ancestor`, in both `verify_optional_single_commit` and `verify_worker_git_topology`. Ancestry is satisfied by a merge's SECOND parent, so a commit built off the anchor with arbitrary content and merged in as a side parent passed as "reachable" without ever being on the branch the run delivers. `_persist_accepted_head` is write-once and refuses to overwrite a differing head, so the anchor cannot be re-derived from a card report under the relaxed check it itself enables; and `finish-verified` is written only after `delivery_head_mismatch`, `delivery_authority_drift` and `pr_identity_mismatch` have passed, so a tick that failed delivery no longer grants the next tick the weaker verification. `_verify_finish`'s docstring now states the true bound it enforces: one commit of arbitrary content, unreviewed, with the human merge gate as the only remaining control over its content.

Pre-existing fixes. `register_pinned_run` records the branch in `<worktree>/.hermes/pipeline_branch.txt` -- the file `phase_8_finish_branch` opens by telling the worker to verify, which nothing could create under a plan manifest -- alongside a self-ignoring `.hermes/.gitignore` so nothing TPO writes into the checkout reads as an uncommitted change. Note that TPO's own readers (`cli._has_pending_pr_handoff`, `ship.maybe_ship_ready`, `harness.read_recorded_branch`) read the project-level `<project>/.hermes/pipeline_branch.txt`, a different file that is deliberately left alone. Diff paths are read with `-c core.quotePath=false` and `-z`, because `core.quotePath` defaults true and C-quoted output contains a backslash the contract rejects, making every commit that touched a non-ASCII path a permanent `changed_files_mismatch` with no reportable alternative. `_git_bytes` raises `git_verification_failed` rather than `registration_invalid`, so a broken git in the worktree-clean check no longer surfaces as `finish_review_head_mismatch: registration_invalid`, blaming both the worker and the registration; the one call site where the code really is a false registration claim (`git show <base>:<plan>`) translates it locally. `_create_task` requires `project_dir` and has no `or worktree` fallback: the finish card passed none, so its pending-create marker was written under `<worktree>/.hermes/runs/<tick>/`, a directory a fresh worktree never has, and the `FileNotFoundError` stopped the finish card from being created.

- The per-Plan-task card fan-out is deleted. `phase_4_development` is now an ordinary phase: one card, carrying the profile's own prompt, created by the same path every other phase uses, with the profile's `tools`, `turns` and `timeout`.

Under a plan manifest that phase used to be fanned into one `plan:<task-id>` card per Plan task, and each card passed `""` to `_render_phase_prompt` in place of the profile's prompt, then appended TPO-authored prose built from the manifest ("Implement Plan task ...", the instructions, criteria, verification list, required commit message, "Complete only this task using red-green-refactor TDD."). The profile's phase_4 prompt therefore reached no agent at all, and everything it says was lost: re-open the Plan and exit nonzero if it is missing or insufficient; "Start from main and create a task branch using repository conventions. Preserve unrelated tracked and untracked work."; the five-step per-task discipline (red-green-refactor, smallest change, focused checks AND diff inspection, explicit staging with exactly one atomic commit per Plan task, coordinator review between tasks); "Never commit a red, incomplete, or known-failing task"; and "Do not implement inline and do not ask questions in this unattended phase." A live codex run left `uv.lock` untracked and died on `worktree_dirty` for want of two of those sentences. The fan-out also contradicted the profile's own shape: the prompt addresses one agent orchestrating all tasks and its `turns: 100` / `timeout: 7200` budget is stated for that whole phase, while TPO handed that budget to each of N cards. `tests/test_external_prompt_boundary.py` now asserts byte equality between the implementation card's delimited block and the rendered profile prompt, so appending anything fails the suite.

The manifest survives as authority, not as a card generator: it still gates eligibility, pins the Plan hash, supplies the acceptance criteria the card's single report must echo in Plan order, and sets the commit-count bound. `verify_worker_git_topology` takes an `expected_commits` parameter, default 1, and the implementation card passes `len(manifest.tasks)` -- derived from the profile's "Stage explicit files or hunks and create exactly one atomic commit per Plan task", so N tasks owe exactly N commits and no looser rule. The bound is `rev-list --count base..head == N` and `rev-parse <head>~N == base`, which at N=1 is literally the `<head>^ == base` check it replaces; the count is checked first so a card that made one commit for a three-task Plan reads as `commit_count_mismatch` rather than `git_verification_failed`. `_implementation_head` recomputes the head from that one report against `base_sha` instead of walking a per-task chain, and `reconcile_plan_task_results` reconciles one step.

This is a persisted-format break in both directions, and it fails closed. `registration.json` records `step_keys`; `load_validated_registration` now requires `phase_4_development` among them, so a run registered before this release lists only `plan:<task-id>` keys and is rejected with `registration_invalid` instead of being verified against a card shape that no longer exists. The subset idiom is kept, so extra keys (a resumed run's legacy `validate:<id>`) still load, but subset semantics cannot rescue a required key that is new rather than dropped. Downgrading is symmetric: old code looks for `plan:*` and rejects a new registration the same way. Drain any active manifest run before upgrading or downgrading; the branch and its commits are untouched either way.

Consequences. `pinned_tick_budget` is 7 ticks for a plan-pinned run whatever the Plan's task count, because `step_keys` no longer scales with it -- the enumerated tick sequence is unchanged (the implementation cards were always created in one tick and settled by that tick's own poll), so the formula stays `len(step_keys) + 6` and only its `len` term's meaning changes. The legacy `validate:<id>` gate completion and its `gate_completion_failed` marker code are removed, unreachable now that such a registration cannot load. The "legacy Plan without a manifest" warning is gone: a manifest-free Plan takes the same ordinary path as any other phase, publishing no result template and having its result parsed by nothing. `Phase.compile_plan_tasks` is parsed but read by nothing, kept only so the bundled `native-sdd` profile -- which still declares it, because the profile is the specification and this change does not edit it -- stays loadable.

One residual, named rather than papered over: the single report's `acceptance` array now carries every Plan task's criteria, so the whole Plan's criteria must fit `MAX_METADATA_BYTES` (64 KiB) where the cap previously applied one task at a time. An embedded Plan is bounded to 65,536 chars so it can barely reach that; a legacy tracked-path Plan is unbounded and a pathological one is unreportable. No new bound is introduced -- the 64 KiB cap could already be blown by a single task's criteria -- and `tests/test_plan_stress.py` pins the realistic worst case, a 50-task Plan, at an order of magnitude of headroom.

### Minor Changes

- `tpo tick` now reports what it isolates. A project whose tick raises still never aborts the scan -- every remaining project is ticked -- but the per-project log line carries the exception message and a sanitized, line-by-line-redacted traceback instead of a bare `error_type=`, and the scan exits 1 when any project's tick raised. `run_tick` treats that non-zero exit as a tick failure and raises `HarnessTickError("tick_crashed")` with `rc=<n>` plus the tick log's tail as `detail`, so `drive_ticks` reports the crash instead of falling through to `tick_stalled` on the unchanged board it left behind. `workers_unaccounted` is still derived by `_tick_failure` from the persisted tick id rather than asserted by the crash path: the subprocess exited on its own, so a re-asserted pinned tick id means shutdown can cancel that tick's cards, and forfeiting cleanup would strand the sandbox branch and PR for nothing.

Documentation corrections: ADR-0002's "five-round review breaker" clause is superseded by an amendment (drift and the final merge boundaries stand); `howto-review-outcomes.md` and `hermes-state-machine.md` no longer claim a `blocked` card stops automation, because `all_phases_complete` counts `blocked` as complete, so production cron releases the project and selects the next TODO while only the harness poller fails; and `howto-live-integration-test-harness.md` no longer describes delivery as `finish` done with a `blocked` `human-gate` card, which no code creates and which `classify_pinned_run` would classify as failed. `tpo tick`'s exit codes are documented in `reference-cli.md`.

- A tick that reconciles nothing no longer fails the whole harness run, and a legitimately no-progress tick no longer fails it either.

`reconcile_todo_completion` now handles `RetryableReviewRegistration` around the `finish` card's create exactly as `reconcile_reviews` already handles it for `review:0`: the card may or may not have landed, so the reconciler returns "progress" and the next tick re-derives the truth from the snapshot, leaving the pending marker in place. It used to propagate; while `tpo tick`'s catch-all returned 0 that cost one tick, but now that a raising tick exits non-zero and `run_tick` reports it, an ambiguous `hermes kanban create` for the finish card failed the entire run as `tick_crashed`.

`drive_ticks` now counts *consecutive* identical settled boards and fails with `tick_stalled` at three instead of comparing only against the immediately previous map. The old test tolerated zero no-progress ticks, and because it runs before the budget test it was the operative bound on every retry path: `reconcile_plan_task_results` returning False on a `result-validation-blocked` result set, `reconcile_reviews` returning False on a topology or contract failure, a `_blocked` delivery, and an ambiguous create whose card did *not* land all leave the board unchanged and need one more tick, and every one of them ended the run on the spot. The counter resets whenever the board moves, so the tolerance is one tick per stall episode; the `tick_stalled` detail now names the consecutive count so an operator can tell a tolerated transient from a real stall, the tolerated tick is logged at warning level, and the `tick_stalled` monitor event is still emitted only on the tick that actually fails the run.

`pinned_tick_budget` is `len(step_keys) + 6`, up from `+ 5`. The constant is the enumerated non-plan cost of a run and that enumeration gained a term: three ticks for the delivered path, two for the ambiguous-but-landed creates of `review:0` and `finish`, and one for the no-progress tick the stall detector now tolerates -- a consumer that could not reach this budget at all while `tick_stalled` decided every board-invariant stall on the very next tick. At N=1 the enumeration needs 6 of the 7 ticks it gets; keeping `+ 5` would have left a single-task run zero slack and made `tick_budget_exhausted` reachable by a run that had not run out of legitimate work.

Documentation: `howto-live-integration-test-harness.md` describes the three-board threshold, the tolerated-and-logged repeat, and the new formula. `howto-native-sdd-profile.md`'s run-evidence row for `pending-review-create.json` was wrong three ways -- it is written before *every* dynamic card create rather than on an ambiguous outcome, it covers `finish` as well as `review:0`, and it recovers nothing: `_persist_pending_create` writes it and `_clear_pending_create` deletes it, and no reader exists anywhere. Recovery is `_find_task_id_in_snapshot`, re-run before every attempt against an idempotency-keyed create, plus `RetryableReviewRegistration`. The row now says so and points at `pending-task-create.json`, the different marker `reconcile_pending_task_create` really does read. The marker code itself is left alone as pre-existing dead code.

- Credential redaction, commit-evidence attribution, and blocked-card accounting.

One credential pattern for the package. `todos_create` carried a second, divergent regex, so the two redactors had different coverage and the newly load-bearing one -- `sanitize_result_text`, now reached by the per-project tick catch-all's full sanitized traceback, whose tail becomes `tick_crashed`'s detail -- was the weaker. Both now share `result_contract.SECRET_RE`, the union of the two plus five shapes neither caught: `github_pat_` fine-grained tokens (which `gh auth login` issues by default and which `gh[pousr]_` cannot match), a bare `sk-ant-` key, an AWS access-key id, a space-separated `.netrc` `login ... password ...` line, and a PEM private key block including its base64 body. Note the pattern is also a rejection predicate for reported metadata, so each alternative widens what `unsafe_metadata` refuses.

Commit evidence is attributed to whoever is at fault. A worker reporting a valid-shaped but non-existent 40-hex SHA made every topology query exit 128, which every git-failure helper collapses into `git_verification_failed` -- and `_verify_finish` passes that code through on purpose so a broken worktree reads as broken, so a fabricated report was attributed to broken infrastructure. `verify_optional_single_commit` and `verify_worker_git_topology` now prove each reported SHA resolves to a real commit before any topology query and raise `unknown_commit` when it does not. The check is `rev-parse --verify --quiet <sha>^{commit}`, not `cat-file -e`: a tree's own SHA is a valid object, and `cat-file -e <sha>^{commit}` exits 128 for an unknown name and would raise the very code being avoided.

A blocked card is no longer silently abandoned. `all_phases_complete` counts `blocked` as complete on purpose -- a sticky block is terminal and a tick must not spin on it -- but `observe_outcomes` wrote no outcome line for it, so the decision store held neither a success nor a failure for a run that abandoned its branch, worktree and unmerged work. It now writes `failed_at_phase_<key>` with `kanban_status: "blocked"`, the same vocabulary `failed` gets. It deliberately does not get the `all_phases_complete` sentinel, because `blocked` is not in `COMPLETION_STATUSES` and an abandoned run carrying that sentinel would be the false success itself. The completion semantics are unchanged; the missing record was the defect. This does not move the circuit breaker's counter: `observe_from_outcomes` classifies `phase_complete` before it classifies a failure, so a run whose earlier phases completed still reads as progress -- as it already did for `failed`.

A run upgraded in the middle of a review round now names the upgrade. Such a board still carries a `review-fix:<n>`, `re-review:<n>` or `fix-validation:<n>` card; its read-only `review:0` reported the implementation head, the round's own card moved HEAD past it, and `accepted-review-head` was never written -- so every tick demanded a HEAD the run had left behind and reported `head_mismatch` forever, blaming the worker's topology. `reconcile_reviews` now detects a legacy round key before measuring anything and reports `review_round_upgrade_discontinuity`. Such a tick is not recoverable in place: it must be abandoned and the TODO re-selected, which `docs/howto-debugging-and-recovery.md` now spells out.

Two smaller correctness fixes. The per-project tick catch-all renders the exception under a guard: both `sanitize_result_text(e)` and `_sanitized_traceback(e)` call back into the exception, so a `__str__` that itself raises escaped the handler, aborted the whole scan before the remaining projects ticked, discarded the `crashed` list, and let the interpreter write an unsanitized traceback to stderr -- the exact text the handler exists to sanitize. It now falls back to an `error_type`-only line. And `tick_crashed` joins `tick_stalled` in `_OBSERVED_CARDS_TICK_ERRORS`: it is raised only from a subprocess that returned a non-zero rc, having finished its project loop, so its observed cards are the honest completeness set -- unlike `tick_timeout`, a SIGKILL that can die mid-`hermes kanban create`. Forfeiting the check with `None` was not the conservative branch, because `_wait_for_kanban_quiescence` gates its membership test on `expected_phase_keys is not None`. `tick_crashed`'s log line no longer claims the tick "did not register a runnable run", which is true only of `tick_not_persisted`.

Documentation. Twenty-one statements across `README.md`, `docs/ARCHITECTURE.md`, `docs/hermes-state-machine.md`, `docs/howto-native-sdd-profile.md` and `docs/reference-kanban-as-scheduler.md` described a gate phase as a card in the `--parent` chain, unassigned and blocked with a sticky `needs_input`, and the terminal boundary as a `human-gate` card. No card has ever existed for a gate phase in any profile: both card-creating loops in `kanban_tasks` `continue` unconditionally on `phase.gate`, and no code anywhere issues a `hermes kanban block` -- the only `needs_input` string in the package is inside the worker-facing prompt telling the dispatcher to block itself. The terminal boundary is the open, unmerged pull request and its human merge decision, and `tests/test_harness_docs.py` now forbids the false phrasings rather than merely correcting them.

Test-only. `pinned_tick_budget`'s shipped `+ 6` is pinned concretely, because the existing bound asserted only `budget >= 6` and the drive test recomputed the budget from the function under test, so reducing it to `+ 5` -- which leaves a single-task run zero slack -- passed everything. Two `_verify_finish` guards used `match="finish_review_head_mismatch"`, the wrapper code emitted for every inner failure, and so could not tell `commit_count_mismatch` from `parent_mismatch`; they assert the full detail now. New coverage for the first-parent check that is the sole guard against a merge hiding the anchor as its second parent, for the zero-commit branch's `or git.changed_files`, and for a reportable path containing a control character, which is what `-z` alone carries. `test_native_sdd_identical_settled_maps_stall` is deleted as fully subsumed by `test_native_sdd_two_consecutive_no_progress_ticks_stall`.

### Patch Changes

- Worker-facing Kanban cards now publish the exact `metadata.tpo_result` object they must be closed with. A native-sdd run previously stalled with `malformed_result: unexpected or missing fields` after a Plan task was implemented, committed, and tested, because the strict shape lived only in the parser: the card asked for "the required structured result metadata" and never said what that was, so the worker closed with a prose summary, the chain never advanced, and no review card opened. Plan-task, review, re-review, review-fix, and finish cards now carry a JSON template rendered from the contract constants themselves, with every pipeline-known value pre-filled (schema version, tick, TODO and step identities, the success verdict, the Plan's acceptance criteria, and the pinned head, branch and empty changed-files list of a read-only card), leaving the worker only the facts it holds. The parser now rejects a value that is wholly an unfilled `<...>` placeholder, so a card that omits a field fails loudly instead of recording placeholder evidence.

- TPO now accepts the result metadata envelope the Hermes platform actually produces. `parse_worker_result` required a completed run's `metadata` to hold exactly one key, `tpo_result`, but a worker supplies that whole mapping itself through `hermes kanban complete --metadata`, Hermes stamps `worker_session_id` on top of it, and workers routinely add their own keys (`notes`, `findings`, `commit_message`). On a live board, all 67 completed runs carried `worker_session_id` and 66 carried further worker-authored keys, so the check rejected every real result. Because `parse_worker_result` is the single choke point for plan tasks, reviews, review fixes and delivery, the native-sdd profile could not advance any step: a live run stalled with `malformed_result: unexpected or missing fields` after its Plan task had been implemented, committed, tested and closed with a well-formed `tpo_result`. The envelope's sibling keys are now tolerated and simply never read, while the 64 KiB size bound over the whole mapping stays in place, a resource guard rather than a content one. Validation of `tpo_result` itself is unchanged and remains exact-key-checked at every level, now with mutation coverage at each of those levels so the guarantee is tested rather than assumed. A narrower subset check would not have worked either: 66 of those 67 runs would still fail it, and the `worker_session_id` stamp is conditional on Hermes's tool path, absent on its CLI path, and forgeable, so it is never treated as an authenticity signal.

- TPO no longer rejects a completed worker result because of text it never reads. `parse_worker_result` applied its unsafe-string scan -- control characters, GitHub token prefixes, and any `authorization`, `token`, `password` or `secret` followed by a separator and a value, matched anywhere in prose -- to three values outside the evidence it actually consumes, and a match discarded an otherwise perfect result. Because a rejected result is a silent permanent wedge -- the reconciler writes a sticky marker, opens no card, notifies nobody, and the closed run's data is immutable, so every later tick re-parses and re-fails -- each was unrecoverable without human intervention, which the autonomous profile is meant not to need.

The worst case was structurally unfixable by any worker. `parse_worker_result` requires the worker to echo the Plan's acceptance criteria verbatim, but scanned that text before comparing it: a criterion such as `Expired token: request returns 401`, `Login rejects a bad password: no session is created` or `Sends authorization: Bearer <t> on every call` was rejected as `unsafe_metadata`, so any TODO whose criteria mention a token, password, authorization header or secret could not be completed -- the required text is dictated by the Plan, and no worker output could pass. `acceptance` is now exempt from the scan, on the same grounds `issue_snapshot` already is in `load_validated_registration`: it is hash-pinned authority content that TPO renders into the worker-facing card itself, and every criterion is still checked for exact equality against the Plan manifest, which is what actually constrains it.

The run summary is no longer bounded or scanned. Hermes requires a closing summary and stores it verbatim, but TPO reads it nowhere -- its value was already discarded after bounding -- so ANSI-coloured output, a `token: expired` log line, or anything over 8 KiB wedged the step for nothing. Hermes' own redactor made it likelier still, rewriting `Token: refreshed successfully` into `Token: *** successfully`, which also matched. Worker-authored sibling keys in the metadata envelope (`notes`, `findings`, `commit_message`) are likewise no longer scanned; they are never read, and nothing echoes them into a log, notification, report, comment, issue or card. The 64 KiB size bound over the whole envelope stays, a resource guard rather than a content one. Everything TPO does consume or compare -- `git.changed_files`, the `tdd` commands, review findings and delivery fields -- is still scanned, now with mutation coverage pinning each level of the recursion and each half of the rule.

Plan manifests are validated more strictly to compensate. Exempting acceptance criteria makes `parse_plan_manifest` the last filter on that text, and its control-character class did not cover the Unicode bidi overrides and isolates (U+202A-U+202E, U+2066-U+2069) that `result_contract` rejected, so a Trojan-Source-style criterion -- text that renders as something other than what it says -- would have reached card bodies. That class now matches, and a test asserts the containment relationship rather than string equality so the two cannot drift in the unsafe direction. U+200E and U+200F, the likelier accidental paste, are deliberately not in the class and continue to validate.

Migration: a Plan already registered to a run, whose task strings contain one of those bidi characters, now fails `registration_invalid` on every subsequent step, because `load_validated_registration` re-parses the Plan on each one. The Plan is hash-pinned against the registration, so editing the character out changes the digest and fails the same check. Such a run must be cancelled and re-registered; retrying cannot repair it. Machine-generated Plans are not expected to contain these characters, so this is a precaution rather than an anticipated migration.

- `ship.ci_is_green` no longer answers "green" for an empty status-check rollup; it raises `ChecksInconclusive`, and `_bump_and_merge` turns that into an `ApproveRefused` instead of merging.

An empty rollup is not evidence that a repo configures no CI — it is evidence that the rollup is empty, a shape with several causes. The dangerous one is a workflow startup failure: a run is created, concludes `failure`, and produces zero jobs, so `gh` reports no checks at all. That is exactly what an agent which breaks `.github/workflows/*` leaves behind (live example: `WearExerciseManager` at `4c14b532`, where `check-suites` shows one `github-actions` suite with `latest_check_runs_count: 0` and `conclusion: failure`). The old rule read that as an absent gate and let the merge through.

`ci_is_green` is handed a bare list with no repo or sha, so it cannot corroborate the absence itself and no boolean it returns would be honest; returning `False` would silently convert "undetermined" into "red". Raising is the only way it can say "I cannot tell" and force a caller to confront the ambiguity. The corroboration a caller owes is worked out in `todos_completion._rollup_is_honestly_empty`.

Behaviour change to be aware of: a repository that genuinely configures no CI can no longer be approved through `_bump_and_merge`. That is deliberate fail-closed behaviour — the refusal names the ambiguity so an operator can act on it — but the durable fix is for the call site, which does know the repo and the sha, to corroborate the empty rollup against the head commit's check-suites rather than refuse.

- Stop treating a repository's installed GitHub Apps as evidence that CI reported.

`_check_state` corroborates gh's "no checks reported on the '<branch>' branch"
against the head commit before it may mean green. That corroboration required
the `check-suites` endpoint to report `total_count == 0`, but GitHub opens a
check suite for every installed App subscribing to `check_suite` whether or not
that App ever runs anything. Any repository with a common App installed (codecov,
renovate, netlify, sentry, vercel, read-the-docs) and no reporting workflow
therefore failed the corroboration on every tick: the delivery gate blocked with
`checks_unavailable` and demanded a human, forever, for a pull request that had
no gate to pass.

The corroboration now inspects the suites instead of counting them. A rollup is
honestly empty only when the legacy status endpoint still reports zero, every
suite produced zero check runs, every suite carries a null conclusion, and no
suite belongs to `github-actions`. That keeps the shape this check exists for --
a workflow startup failure, which a worker editing `.github/workflows/*` can
create, and which appears as a zero-run suite with a non-null conclusion -- and
keeps the ambiguous zero-run `github-actions` suite fail-closed. Every error
while establishing the absence still raises `checks_unavailable`.

Also makes the human-gate branch of `reconcile_todo_completion` confirm that the
`url` echoed by `gh pr view` is the pull request the delivery named, a check its
sibling branch already performed.

- Let approve merge a repository that genuinely has no CI, without reopening the false green.

`approve` was squash-merging pull requests on an empty `statusCheckRollup` while
logging "no CI checks found; proceeding (nothing to gate on)". An empty rollup has
at least six causes and only one of them is "there is no gate to pass"; the
dangerous one is a workflow startup failure, where a run is created, concludes
`failure` and produces zero jobs, which is exactly what a worker that breaks
`.github/workflows/*` leaves behind. Live shape: `yehiashouman/WearExerciseManager`
at `4c14b532d7da2a99a9e3b337fece90a5336fdc43`.

`ci_is_green([])` now raises `ChecksInconclusive`, and `_bump_and_merge` no longer
inherits that as a verdict in either direction. It corroborates the emptiness
against the head commit -- reusing `todos_completion._rollup_is_honestly_empty`
rather than restating the rule -- and merges only when the absence is proven: no
check suite produced a run, no suite carries a conclusion, no suite belongs to
`github-actions`, the suite page is complete, and the commit has no legacy
statuses. That merge is recorded in the approve audit log.

Every other outcome refuses with `ApproveRefused`: a disproved corroboration, an
unidentifiable repository, an unreadable check-suites page, and a rollup that
describes a commit other than the one `--match-head-commit` would merge. An error
while proving a negative is not proof of the negative, and the merge it would
unlock is irreversible.

- Worker cards now tell the Hermes dispatcher to deliver the external-agent prompt on the client's standard input instead of inlining it into a shell command line. A live native-sdd run blocked `phase_4_development` after 42 seconds with `external Codex command exited non-zero (exit code 2) before phase work`: the delegation block published `Required external command: codex exec --sandbox workspace-write` and left the dispatcher to compose the shell word itself, so it single-quoted the prompt and the apostrophe in the profile's own sentence "Execute the Plan's ordered tasks with a fresh native implementer subagent for each task" closed the string early. Codex never ran, and no implementation, inspection, or commit was performed. The `claude` branch carried the same latent defect with double quotes, where a `"`, `$`, or backtick would have broken it instead.

Now that the phase profile is the specification and its prose is arbitrary, this was a whole failure class rather than one typo, so the fix removes the shell from the transport path. The required command is `codex exec --sandbox workspace-write - < "$PROMPT_FILE"` and `claude -p --permission-mode dontAsk --allowedTools <tools> < "$PROMPT_FILE"`; both clients read a prompt from stdin (Codex when `-` is given, Claude Code when no prompt argument is passed). The dispatcher is told to copy the delimited block byte-for-byte with no interpolation, escaping, re-wrapping, or summarizing, and to write the prompt file to a temporary directory outside the repository -- never inside the worktree, not even a gitignored path -- because the phase verifies that the worktree is clean and a stray untracked file fails the run with `worktree_dirty` before any work begins.

The prompt text itself is untouched: the delimited block is still byte-for-byte the rendered profile prompt, which `tests/test_external_prompt_boundary.py` continues to assert by equality. Only the wrapper around it changed. The `--sandbox workspace-write` and `--permission-mode dontAsk` flags, the validated `--allowedTools` list, the timeout and cleanup-grace wording, the tracked-background-execution requirement, and the result-metadata template are all unchanged.

- Allow network access for Codex dispatcher runs and fix prompt marker extraction and shell redirection.

- Allow Codex agents to commit in linked worktrees by granting access only to their Git common directory.

- Keep dispatcher result collection read-only and require reported gate evidence and a final clean worktree.

## 0.10.0

### Major Changes

- Move the TODO backlog from TODOS.md to GitHub Issues. A TODO is now an open issue labelled tpo:todo on the project's github.com origin; its ID is TODO-<issue-number> (legacy IDs are preserved as legacy-id labels and in docs/migration/todos-to-issues.md). The selected issue is pinned as an identity-bound snapshot in registration.json (schema 2; v1 registrations are rejected — finish or abandon active runs before upgrading), closeout closes the issue instead of committing TODOS.md, and the selection prompt changed (re-pin selection.expected_prompt_sha). TODOS.md, TODOS-archive.md, the todos-manager skill, tpo skills install/uninstall, tpo recover-counter, and the skill test environment are removed; tpo todos audit and tpo todos labels sync are added.

### Minor Changes

- Embed implementation Plans in GitHub TODO issues and add recoverable todo-manager workflows.

## 0.9.0

### Minor Changes

- Compile tracked Plan manifests into visible Kanban tasks with validated review, delivery, and recovery gates.

### Patch Changes

- Prevent nonexistent TODO selection and keep agent/provider payloads out of logs and persisted error records.

- Make todos-manager author validated manifests for actionable Plans.

## 0.8.0

### Minor Changes

- Add a native SDD/TDD pipeline profile without client workflow skills

- Allow the mock integration harness to select and safely test supported bundled phase profiles with profile-attributed reports.

### Patch Changes

- Fix Hermes skill prerequisite detection for table-formatted CLI output.

- Fix Claude phase delegation by placing the prompt before variadic allowed-tool arguments.

## 0.7.4

### Patch Changes

- Fix Version Packages automation to create a new pull request after the previous release pull request was merged.

- Fix release automation to find and update an existing open Version Packages pull request.

- Replace the npm Changesets dependency with Python-native release fragments, version aggregation, changelog generation, and automated Version Packages pull requests.

## 0.7.3

### Patch Changes

- 83a69c7: Adopt Changesets fragments and an automated Version Packages pull request for release versioning and changelog generation.
- 5a62958: Finalize versioned agent-client evidence during Changesets releases and keep release metadata tests version-independent.

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.2] - 2026-08-07

### Added

- Add project-specific agent instructions and track the production orchestration refactor as TODO-43.

### Changed

- Strengthen CI with locked dependency sync, four-artifact release consistency checks, and an 88% coverage floor.

## [0.7.1] - 2026-08-07

### Added

- Add repository-local Plan, Spec, and Reference attachments to `todos-manager --add` and `--revise`, with bounded discovery, explicit confirmation, and attachment auditing.

### Fixed

- Reject attachment symlinks that resolve into excluded repository paths and allow combined Plan/Spec candidates to be explicitly declined.

## [0.7.0] - 2026-08-04

### Added

- Set `prompt_client` to `claude` or `codex` so phase prompts use the correct client name and skill invocation syntax.
- `tpo doctor` now reports agent-client prerequisites, backed by release qualification evidence for supported gstack and Superpowers workflows.
- Hermes phase registration now uses a non-spawnable barrier with durable crash recovery, preventing partial task chains from dispatching.
- `tpo doctor` now verifies Hermes-owned dispatcher prerequisites against the assigned Hermes profile's local skill registry.

### Changed

- Phase prompts are fully rendered before tick persistence or Hermes task creation, so template errors cannot strand an active tick.
- Release qualification evidence distinguishes candidate source snapshots from release-final artifacts, making support claims traceable to the shipped version.

### Fixed

- Uncertain Hermes task creation, cleanup, and barrier completion now fail closed without releasing partial task chains.
- Prior-tick reconciliation now runs before new TODO selection and safely retries interrupted registration commits.
- Manual gate phases now remain in the dependency chain, so downstream executable phases wait for human approval.
- `tpo tick` now refuses profiles whose selected prompt-client prerequisites are still `Unverified` before selection or task registration.
- Pending Hermes task-create recovery now runs before unsupported-profile checks, so cleanup markers remain recoverable after profile metadata changes.
- Default-assignee projects now verify Hermes-owned dispatcher prerequisites during `tpo doctor`, while still skipping the non-default profile-existence check.
- Harness cancellation now waits for registration to quiesce, preserves uncertain recovery markers, rejects unstable cleanup snapshots, and prevents cancelled barriers from committing late work.
- Tick and harness failure logs now retain static failure types without exposing raw prompt or provider exception content.

## [0.6.6] - 2026-07-28

### Added

- `tpo tick` now uses the bundled selection prompt by default, so new projects can run without creating `.hermes/prompts/selection.md`.
- The selection prompt now tells the agent to explain why no TODO was selected, making empty or blocked ticks easier to diagnose.

### Changed

- Missing pipeline contracts now fall back to the dedicated `pipeline` assignee and warn when that Hermes profile is unavailable.
- Per-project TOML overlays now preserve the project-local state directory used during selection.
- Kanban gate phases now stay manually blocked instead of inheriting parent completion.
- The default `gstack` profile now ends at Phase 8 PR handoff: `/ship` opens or updates a PR and does not merge it.

### Fixed

- Selection parsing now handles warning-prefixed JSON, quoted `"null"` picks, and fenced final answers without letting earlier example JSON override the real decision.
- Picked-none ticks now log the selection rationale so operators can see why the circuit breaker observed no progress.
- Eval docs and runner now use the bundled selection prompt by default while still allowing explicit prompt overrides.
- Ticks now skip new TODO selection while the recorded PR handoff branch is open, closed without merge, or cannot be verified as merged.
- Completed PR handoffs now fail closed when branch state is missing, stale, or unreadable, preventing new TODO work from stacking on an unverified PR branch.
- Legacy/custom `phase_9_ship` profiles still create ship sidecars before the in-flight early return, so `tpo approve` can finish old gate-based ticks.

## [0.6.4] - 2026-07-28

### Changed

- New users now get a shorter `tpo` onboarding path from the README and getting-started tutorial.
- Documentation now separates installed CLI commands from agent-only `todos-manager` skill invocations.
- Historical modularization notes now label legacy command names so they do not conflict with current `tpo` usage.

### Fixed

- README and tutorial links now point to current pipeline contract, CLI, and TODO manager references.

## [0.6.3] - 2026-07-27

### Added

- `tpo skills install --reinstall` now gives users an explicit way to replace an installed todos-manager skill after reviewing local changes.
- `tpo skills uninstall` can remove installed todos-manager skills for Claude, Codex, or both targets with an explicit `--yes` confirmation.

### Changed

- todos-manager now stores the next TODO ID in tracked project state and reconciles it before assigning new IDs.
- TODO ID updates now use locked, atomic file replacement so concurrent writers cannot silently reuse or skip IDs.
- `tpo recover-counter` now rebuilds the legacy counter cache from tracked TODO metadata when the tracked state is valid.

### Fixed

- Skill install and uninstall now preflight all selected targets before replacing or removing installed skills.
- Failed skill replacement or uninstall cleanup now preserves recoverable backups and reports failure instead of pretending the operation fully succeeded.
- Stale, duplicated, malformed, CRLF, and archive-only TODO ID metadata now reconcile consistently across the bundled skill docs and the executable test oracle.

## [0.6.2] - 2026-07-27

### Changed

- `tpo config init` now writes active default values for the supported global config keys, so generated config files are immediately editable without uncommenting placeholders.
- Global config now exposes only the runtime settings that are still used: `projects_dir`, `state_dir`, logging, retention, and Slack channel.

### Fixed

- Existing config files that still contain removed historical keys no longer block runtime commands.
- `PIPELINE_PROJECTS_DIR` remains a deprecated fallback when `projects_dir` is not set in a config file.
- `tpo config get` now reports file attribution for active config entries even when their value matches the built-in default.

### Removed

- Unused global config entries and obsolete state-lock helper code that no longer participates in tick, harness, or counter workflows.

## [0.6.1] - 2026-07-26

### Deprecated

- `PIPELINE_PROJECTS_DIR` remains supported as an environment override for compatibility, but users should migrate to `tpo config set projects_dir <path>`.

### Migration

- If you set `PIPELINE_PROJECTS_DIR` in your shell profile, run `tpo config set projects_dir <your-path>` once, then remove the shell export when convenient.

### Added

- `tpo config` subcommand: `init`, `get`, `set`, `path`
- Global config loading from `${XDG_CONFIG_HOME:-~/.config}/tpo/config.yaml`, `~/.tpo/config.yaml`, and the legacy `${HERMES_HOME:-~/.hermes}/tpo.yaml` fallback.
- `_validate_project_slug()` — rejects path traversal (`..`), leading dashes, and CLI flag injection

## [0.6.0] - 2026-07-24

### Changed

- **BREAKING:** CLI renamed from `pipeline-watch`/`hermes-pipeline` to `tpo`. Reinstall with `uv tool install hermes-pipeline` to get the new name. `pipeline-watch` and `hermes-pipeline` still work for one release — each prints a deprecation warning to stderr before dispatching, and will be removed in the next version bump.
- The `todos-manager` skill is now bundled as package data (`hermes_pipeline/data/skills/todos-manager/`) instead of living outside the package at `skills/todos-manager/`. `uv tool install` now works end-to-end without a manual clone step.

### Added

- `tpo skills install [--target {codex|claude|all}] [--scope {user|project}] [--force]` — installs the bundled `todos-manager` skill to `~/.claude/skills/`, `~/.agents/skills/`, or both.

### Removed

- `scripts/install-todos-manager.sh` — superseded by `tpo skills install`.

## [0.5.10] - 2026-07-24

### Added

- Ruff linting job to GitHub Actions CI (sha-pinned actions) and a `.pre-commit-config.yaml` for local ruff auto-fix on commit.

### Changed

- Applied ruff auto-fixes across the entire codebase: import reordering (isort), `Optional[X]` → `X | None` modernization, `datetime.timezone.utc` → `datetime.UTC`, and dead code removal. No behavioral changes — 704 tests pass unchanged.

### Removed

- Unused imports, unused variables, and redundant type annotations cleaned by ruff.

## [0.5.9] - 2026-07-24

### Changed

- Split the bundled `data/profiles/` directory into two distinct namespaces: `data/hermes-identity/pipeline/` for Hermes' persona/identity data (`SOUL.md`), and `data/phase-profiles/` for pipeline phase configs (`gstack/phases.yaml`, `agent-skills/phases.yaml`). Identity data and phase-orchestration config no longer share one directory. (TODO-32)

## [0.5.8] - 2026-07-23

### Added

- `pipeline-watch test` now logs each phase transition (running/done/failed) and an
  initial phase status table to the console via `log.info()`, instead of writing
  silently to `events.jsonl` only. (TODO-30)
- Raised the `--timeout` default for `pipeline-watch test` from 3600s (1h) to 86400s
  (24h) so healthy long test runs are no longer killed by the default. The flag still
  works as an explicit override. (TODO-30)

## [0.5.7] - 2026-07-23

### Added

- `UI Review` decision field for `todos-manager` TODO entries, mirroring the existing `Security Review` field — `required`/`not-required`, auto-derived from title/summary keywords (ui, frontend, design, visual, layout, component, css, style, dashboard, artifact, page, screen, modal, form, navigation, button, icon, animation) during `--add`, surfaced in the synthesis block, validated as a required `Decisions` sub-key, and gap-checked by `--revise`.

## [0.5.6] - 2026-07-22

### Removed

- `hermes_pipeline/approve_plan.py` — dead plan-gate subsystem module (CLI `approve-plan` subcommand removed in v0.5.5; no remaining call sites).
- `hermes_pipeline/runner.py` — dead null-kanban-scheduler subsystem module; consolidation into single-kanban-only design removes its `PipelineRunner` orchestration role.
- `hermes_pipeline/watcher.py` — dead watcher entrypoint (replaced by `__main__.py` event loop in v0.5.1).
- Plan-gate branches in `hermes_pipeline/gates.py` and `hermes_pipeline/gate_state.py` — `PLAN_GATE_PHASE_KEY`, plan-gate phase marker logic.
- Null-kanban-scheduler branches in `hermes_pipeline/harness.py`, `hermes_pipeline/phases.py` — `PipelineRunner` dispatch, `run()` function, `_invoke_hermes()` / `_invoke_review_phase()` null-mode handlers, marker-based state fallback, gate-dispatch harness.
- CLI subcommands: `merge`, `status`, `kill` (null-scheduler dead code; Hermes kanban-only consolidation in v0.5.2 and later supersedes these).
- `ReadyForReview` and its `State` methods (`write_ready_for_review`, `write_ready_for_review_min`, `read_ready_for_review`, `set_merge_status`, `list_ready_for_review_pending`) in `hermes_pipeline/state.py` — orphaned once `phases.py`/`runner.py` (their only writers) were deleted.
- `hermes_pipeline/gates.py` decision-sheet and rejection-sidecar I/O (`write_decision_sheet`, `read_decision_sheet`, `write_rejection_sidecar`, `read_rejection_sidecar`, `_sanitize_override`, `_HIGH_RISK_KEYWORDS`) — dead once the plan-gate branch was removed; only `REJECTION_SUFFIX` remains (still read by `decision/context.py`'s rejection-count reader).
- Test modules and fixtures tied to deleted plan-gate and null-scheduler subsystems.

### Changed

- `hermes_pipeline/decision/context.py` `build_in_flight()` — removed file-marker fallback during kanban service outages. Now returns empty list if kanban lookup fails (strict single-kanban design, no degraded fallback). Added explicit test coverage for the outage path.

## [0.5.5] - 2026-07-21

### Added

- Optional `**Spec:**` and `**Reference:**` fields on TODOS.md entries. `**Spec:**` names a single authoritative deliverable doc; `**Reference:**` is a comma-delimited list of supplementary background paths. When present, the pipeline's first phase (`_invoke_hermes`) validates each path (containment under `project_dir`, existence) and injects the surviving paths into the phase prompt via `_render_phase_prompt`. Both fields are `--revise`-only in the `todos-manager` skill — never AI-pre-filled or auto-suggested — and fail soft: any parse error, missing file, or traversal attempt silently drops that item rather than raising.
- `hermes_pipeline/todos_md.py`: new standalone `find_todo_fields()` parser that extracts `Spec:`/`Reference:` values for a given TODO entry, anchored between that entry's header and the next, so it can't bleed into a neighboring entry.

## [0.5.4] - 2026-07-20

### Changed

- Extracted plan-gate status logic (`GateStatus` enum, `check_gate_status()` → `gate_status()`) out of `hermes_pipeline/gates.py` into a new read-only `hermes_pipeline/gate_state.py` module. `kanban_tasks.py`'s inline rejection-sidecar check now routes through this shared module instead of duplicating the logic.

## [0.5.3] - 2026-07-20

### Added

- Pluggable pipeline phase profiles: `hermes-pipeline init --profile <name>` lets a project choose which skill-set drives its phases (`gstack`, the default, or the new `agent-skills` profile). Each profile ships its own bundled `phases.yaml` and computes its own required capabilities.
- `agent-skills` profile: a 9-phase pipeline that maps to the `agent-skills:*` skill family instead of gstack's own skills.
- Pipeline contracts now record a `profile` field (schema bumped to v2), validated against a lowercase alphanumeric/hyphen naming rule so a malformed or path-unsafe profile name is rejected before any file resolution happens.
- `hermes-pipeline doctor` is profile-aware: it loads phases from the contract's declared profile and reports drift/missing/invalid profile errors by name.
- Docs: `docs/howto-agent-skills-profile.md` walks through setting up the agent-skills profile; `docs/howto-pipeline-contract.md` documents the new `profile` field and schema v2.

### Changed

- Phase execution (`tick`, `doctor`, `init`) now resolves phases from the project's contract-selected profile instead of a single hardcoded `phases.yaml`, falling back to `gstack` only when no contract exists yet.
- Existing (schema v1) contracts without a `profile` field are rejected with a clear version-mismatch error — re-run `init` to upgrade.

## [0.5.2] - 2026-07-19

### Added

- Regression tests confirming that harness kanban-phase registration correctly resolves task assignee from the pipeline contract, and falls back to `"default"` with a warning when the contract can't be loaded.

### Fixed

- Test coverage for the harness kanban-scheduler checklist is now fully wired to production functions — the remaining checklist rows are linked to real tests, closing out TODO-24.

## [0.5.1] - 2026-07-19

### Changed

- Gate-task auto-completion now routes through `kanban_tasks.complete_todo_kanban_task` instead of a harness-local subprocess call, keeping the production completion path in one place.

### Fixed

- A failed gate-task completion no longer gets logged as a success — the harness now only reports "auto-completed" when the completion actually succeeded.
- A gate that fails to auto-complete now logs a warning naming the task and phase, so a stuck gate can be traced back to its failed completion attempt instead of failing silently.

## [0.5.0] - 2026-07-16

### Added

- **`--kanban {null,hermes}` flag** — Opt-in real kanban adapter for the mock integration test harness, wired to a dedicated tenant with tick_id-labeled card bodies (TODO-20). Default (`null`) behavior is unchanged.
- **Preflight validation** — `hermes kanban list --tenant` check with actionable error if the kanban board is unreachable.
- **Kanban-as-scheduler polling** — `run_harness` now drives real pipeline phases end-to-end through `_poll_kanban_phases`, reusing `register_todo_phases`, `get_todo_kanban_status`, and `all_phases_complete` from the production kanban module instead of a harness-only phase loop.
- **Contract-resolved kanban assignee** — Phase registration reads `assignee` from `.hermes/pipeline.toml` via the same `load_contract()` path as `pipeline-watch tick`, falling back to `"default"` if the contract is missing or malformed.
- **Gate task auto-completion** — `_auto_complete_gate_tasks` automatically completes downstream gate tasks once their parent phase finishes, including the ready/`None` → done transition (fast phases that complete between polls without ever being observed as `running`).

### Fixed

- **Invalid `KanbanOutcome` literal** — `"failed"` corrected to `"abandoned"` across all call sites.
- **Silent kanban-cleanup gaps** — Added cleanup on `continue_on_failure=False` phase failure and convergence-halt paths.
- **Kanban phase-completion gap** — Phases that complete between polls without passing through `running` (ready/`None` → done) no longer leave downstream gate tasks blocked.

## [0.4.11] - 2026-07-15

### Added

- **Mock integration test harness** — Repeatable, verifiable end-to-end pipeline testing. Creates mock projects with preset TODOs, runs the full pipeline through isolated temp directories, monitors phase transitions, generates JSONL event logs and structured findings reports. Supports iterative fix cycles with `--loop` to diff reports across runs.
- **`hermes-pipeline` CLI entrypoint** — Registered alias for the Hermes Pipeline CLI, accessible from any terminal.
- **`hermes-pipeline test` subcommand** — Drives the mock harness via `--fixture`, `--loop`, `--phase`, `--keep`, `--timeout`, and `--convergence-threshold` flags.
- **Convergence detector** — Automatic halt when N+ consecutive phase failures share the same error class, preventing infinite retry loops.
- **`continue_on_failure` mode** — PipelineRunner continues through non-critical phase failures and auto-approves gate phases, surfolding structural correctness of the full pipeline.
- **PipelineRunner monitor callbacks** — Real-time hooks for `phase_started`, `phase_completed`, and `phase_failed` transitions.
- **Environment threading for subprocess isolation** — Phase subprocesses inherit only test-scoped environment variables, preventing the harness from reading user-level config or credentials.

### Fixed

- **Harness phase failure reporting** — Timeout and convergence-halt events are recorded in the JSONL event log so reports reflect the actual failure mode instead of silent truncation.
- **Version test resilience** — Version parsing no longer fails when the VERSION file contains unexpected trailing content.

### Changed

- **Test report module** — New `test_report.py` provides `generate_report`, `summarize_report`, `diff_reports`, and `summarize_diff` for structured pipeline analysis.

## [0.4.10] - 2026-07-14

### Added

- **`todos-manager --revise` subcommand** — revise an existing TODO entry by filling missing or weak fields with AI-pre-filled suggestions. Selects an entry by TODO-ID, scans for gaps (What, Why, Decisions, optional fields), auto-researches the codebase scoped to gaps, presents a synthesis block with confidence tags, and writes the updated entry back to TODOS.md. Reuses the auto-research phase from `--add`. Only revises active entries — archived entries are never modified.
- **Entry boundary parsing spec** — shared algorithm for identifying TODO entry start/end positions in TODOS.md. Used by both `--archive` and `--revise` to extract entries without DRY violations.

### Changed

- **`todos-manager --add` subcommand revised** — after you provide a title and summary, auto-researches the codebase to pre-fill TODO fields (What, Why, Decisions) before the interactive prompts. Reduces manual typing for entries that correspond to existing code areas.
- **TODOS Manager skill updated to seven subcommands** — `--revise` is now documented alongside `--init`, `--add`, `--convert`, `--audit`, `--archive`, and `--list`.

## [0.4.8] - 2026-07-13

### Added

- **`todos-manager --list` subcommand** — report-only listing of active TODO entries as a markdown table (ID, status, title, summary). Pass `--all` to also show archived entries from `TODOS-archive.md` in a separate table. Modifies no files.
- **`todos-manager --convert` header-based transformation (Mode B)** — converts header-based TODOS.md entries (freeform text, title-as-header, no schema fields) into the canonical enforced format. Creates dated backup files and a reference document. Idempotent — already-converted files are skipped.

### Fixed

- **Decision agent JSON parser crashes on CLI backend warnings** — `_parse()` no longer requires the response to start with a code fence. CLI backends that prepend stderr-style warning lines before the fenced JSON block now parse correctly. The parser also tolerates one-line fenced JSON, missing closing fences, and trailing prose after fenced blocks.

### Changed

- **TODOS Manager skill updated to six subcommands** — `--list` is now documented alongside `--init`, `--add`, `--convert`, `--audit`, and `--archive`. Updated ARCHITECTURE.md, CLAUDE.md, README.md, and how-to guide to match.

## [0.4.7] - 2026-07-13

### Added

- **Skill test environment (Phase 1)** — `tests/skill-test-environment/` provides a structural unit test suite for the `todos-manager` skill: a demo-project TODOS.md/TODOS-archive.md fixture, golden YAML assertion files for each subcommand (`--add`, `--init`, `--audit`, `--archive`), and pure-Python verification modules (`skill_logic.py`, `verify.py`) covering ID sequencing, entry parsing, format validation, and archive logic. Runs in under 5 seconds with zero token cost: `uv run pytest tests/skill-test-environment/unit/ -v`. Phase 2 (agent-driven, AI-judged semantic validation) is deferred.

## [0.3.2] - 2026-06-19

### Added

- **`--verbose` / `--debug` logging flags** — `--verbose` increases log detail (selection results, lock state, tick_id). `--debug` enables full debug logging (agent call summaries, circuit breaker transitions, kanban registration)
- **`recover-counter` subcommand** — scans `TODOS.md` for the highest `TODO-N` ID and initializes `.hermes/todo_id_counter`; prevents ID collisions when bootstrapping a project with hand-written TODOs

### Fixed

- **`--debug` flag now enables `pipeline.verbose` logger** — verbose log lines are now visible in debug mode (they were previously only shown with `--verbose`)

## [0.3.3] - 2026-06-23

### Added

- **Multi-project scan loop** — `pipeline-watch tick` without a project argument now scans all active projects in `projects_dir`, running one selection per project under a single global lock. `pipeline-watch kill` without a project argument similarly scans all projects
- **Per-project configuration** — `<project>/.hermes/project.toml` for filtering (`enabled = false` to archive) and per-project Slack channel via `[notifications] slack_channel`
- **Project discovery** — new `project_config` module with `_discover_projects()`, `_is_enabled()`, and `_resolve_slack_channel()` for filesystem-based project filtering

### Changed

- **Selection model default** — `SelectionConfig.model` defaults to `"auto"` instead of `"claude-opus-4-7"`. Hermes resolves `"auto"` to the current best model, so the pipeline stays current without reconfiguring.
- **`tick` subcommand** — optional `project` argument; when omitted, scans all active projects instead of requiring a specific project
- **`kill` subcommand** — optional `project` argument; when omitted, scans all projects for in-flight phases
- **State migration** — first-run migration of global state (`~/.hermes/`) to per-project state (`<project>/.hermes/`) via new `state_migration` module

### Fixed

- **Kill across projects** — `kill --todo` now searches all project state directories for the specified TODO, returns exit code 2 if not found anywhere
- **Slug validation** — `_validate_project_slug` rejects single-character slugs and invalid directory names during project discovery, preventing misconfigured projects from entering the scan loop

### Removed

- **Circuit breaker cron backoff** — The circuit breaker no longer adjusts the Hermes cron interval (backoff/resume). `backoff_interval_min` and `backed_off` are removed from config and circuit state. The gateway service owns tick scheduling.

## [0.3.1] - 2026-06-16

### Added

- **`pipeline-watch tick` subcommand** — kanban-as-scheduler pipeline tick: selects a TODO via Hermes agent, registers phases as kanban tasks with `--parent` dependency chain, and observes circuit breaker
- **Kanban task registration** — `register_todo_phases` creates phases as kanban tasks with `--idempotency-key` for dedup and `--parent` for sequential execution
- **Circuit breaker outcome observation** — `observe_outcomes` writes phase completion/failure outcomes to JSONL sidecars; `observe_from_outcomes` reads outcomes to drive circuit breaker state
- **Stale-marker PID verification** — `_phase_started_ids` checks process liveness before sweeping stale markers; wedged-but-alive processes remain visible
- **Kanban-aware in-flight detection** — `build_in_flight` queries kanban for in-flight tasks, falls back to file markers
- **`.hermes/prompts/` tracking** — prompt templates are tracked in git; runtime state (decisions, outcomes, locks) is ignored

### Changed

- **Tutorial updated** — `pipeline-watch tick` is the primary workflow for development and debugging; Hermes cron is optional for production

### Fixed

- **Circuit breaker config loaded once per tick** — eliminated duplicate TOML overlay reads and `CircuitBreaker` instantiations
- **Project slug validation** — rejects path traversal (`..`, `.`) and CLI flag injection (`--help`, `-v`) in project slugs
- **Partial registration detection** — expected phase keys are now persisted after kanban task registration; `all_phases_complete` verifies all expected phases are present before returning true
- **Tick stall detection** — `tick_started` sentinel without terminal outcomes is now treated as a stall (not completion) so the circuit breaker can detect no-progress conditions
- **Hermes kanban CLI adaptation** — adapted to CLI drift: `--board` → `--tenant`, positional title
- **Atomic tick_id persistence** — tick_id is persisted atomically and before kanban registration, preventing split-brain on crash

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
- **Kill subcommand** — `pipeline-watch kill --todo TODO-N` or `--all` for in-flight phases; confirms process exit, releases tick lock when owned
- **Outcome sidecar** — terminal `merge_status` transitions write outcome metadata (failed, killed_by_operator)
- **Eval suite** — 8 selection-prompt fixtures with runner (`tests/eval/`); non-blocking eval workflow
- **Operator how-to guides** — Diataxis-formatted guides for config, eval, kill, and prompt-sha-mismatch troubleshooting
- **Hermes state machine** — docs for phase lifecycle (state machine table)

### Changed

- **Directory structure flattened** — `hermes-pipeline/src/hermes_pipeline/` → `hermes_pipeline/`; `hermes-pipeline/tests/` → `tests/`; `hermes-pipeline/configs/` → `configs/`
- **Raised minimum Python version** from >=3.9 to >=3.12
- **Decision-driven pipeline** — watcher and CLI pruned to delegate selection and scheduling to Hermes

### Fixed

- **Hallucinated picks rejected** — LLM must pick a TODO that exists in TODOS.md; rejects hallucinated IDs
- **Atomic state writes** — `set_merge_status` and `ready_for_review` use tmp+rename to prevent partial writes
- **Best-effort outcome sidecar** — `set_merge_status` doesn't fail if sidecar write fails
- **Config path resolution** — phases resolve config relative to flattened directory structure
- **Kill targets `child_pid`** — kill subcommand targets the phase child process, not the watcher
- **Canonical RFR filename** — decision context normalizes filename and checks PID liveness on sweep

### Removed

- **Deterministic `selection.py`** — replaced by Hermes LLM-driven decision engine
- **`pipeline-watch auto` subcommand** — scheduling moved to Hermes cron (`hermes cron set pipeline-tick */5 * * * *`)
- **System crontab registration** — `install-cron.sh` removed; tick schedule managed via `hermes cron set`
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

### Fixed

- **Process group kill on timeout** — `hermes_agent_call()` kills the entire process group (hermes + children) on timeout instead of only the parent process, preventing orphaned subprocesses.
- **stderr capture on agent timeout** — after SIGKILL, agent timeout path now captures stderr for diagnostics.
- **Transient spawn retry** — `hermes_call()` and `hermes_agent_call()` retry up to 2 times on transient OSError before failing, improving resilience against brief network hiccups.
- **KeyboardInterrupt propagation** — `hermes_agent_call()` propagates KeyboardInterrupt instead of silently swallowing it during timeout handling.
- **Tool enforcement via CLI flags** — tool and turn constraints are enforced via `hermes chat -q` CLI flags (`-t` for tools, `--max-turns` for turns) instead of purely advisory prompt headers.
- **Preflight hermes check** — `check_hermes()` validates the hermes CLI availability before pipeline operations, failing fast with a clear error.
- **Renamed claude functions** — `_anthropic_call()` and `_run_claude_subprocess()` renamed to `_hermes_call()` and `_run_hermes_subprocess()` to reflect the new dependency.

## [0.3.4] - 2026-06-29

### Added

- **Ship gate (Phase 9)** — New `phase_9_ship` blocked kanban task that holds every completed TODO in-flight until a human approves via `pipeline-watch approve`. The blocked gate replaces the `terminal: true` flag on Phase 8, keeping the pipeline loop running until approval.
- **`pipeline-watch approve` subcommand** — Deterministically ships an approved TODO: bumps VERSION/pyproject.toml/CHANGELOG on the work branch, gates on CI-green, and squash-merges to main with `--match-head-commit`. Idempotent — re-running on an already-merged PR just completes the gate.
- **SHA-staleness guard** — Refuses to merge if the PR head SHA has changed since review. `--force --force` (double pass) bypasses this guard and writes an audit log entry.
- **Dirty-tree and CI-green guards** — Refuses approve if the working tree is dirty or CI is not green. Force flag never bypasses these guards.
- **"Ready to ship" Slack alert** — Fired exactly once when all phases complete except the blocked gate. Deduped by the existence of the ship sidecar file.
- **`ShipSidecar` dataclass + atomic sidecar I/O** — Writes `outcomes/<tick_id>-ship.json` with PR details, head SHA, and branch names. Read by `approve` to verify SHA and complete the merge.
- **`approve_lock` via fcntl** — Serializes concurrent approve calls so two operators can't race the same merge.
- **`get_todo_kanban_tasks`** — Queries kanban for all tasks of a tick, returning `KanbanTaskInfo` with task IDs and statuses. Used by `approve` to resolve and complete the gate task.
- **`bump_in_pr`** — Writes VERSION, pyproject.toml, and CHANGELOG on the work branch, commits, and pushes. Restores the original branch after completion (even on failure).

### Changed

- **`Phase` dataclass** — `gate` flag added; `prompt`, `tools`, `turns` now optional with defaults so gate phases need no LLM fields.
- **`configs/phases.yaml`** — `phase_9_ship` added as a `gate: true` phase; `terminal: true` moved from Phase 8 to Phase 9.
- **`register_todo_phases`** — Gate phases are created with `--initial-status blocked` and no `--goal` flags (pure markers, never dispatched to an agent).
- **`_tick_project`** — Calls `maybe_ship_ready` before the `all_phases_complete` early-return, so the "ready to ship" alert fires even though the blocked gate keeps `all_phases_complete` returning False.

### Fixed

- **Branch left on `work_branch` after bump failure** — `bump_in_pr` wraps `git checkout work_branch` in try/finally that restores the original branch, so a CI-red refusal or merge failure doesn't leave the operator on the wrong branch.

## [0.4.0] - 2026-07-07

### Added

- **Code review phase (Phase 5)** — New `phase_5_review` phase runs gstack `/review` skill autonomously via `hermes chat -q` between development and CSO. Pre-review snapshot captures HEAD and diff; post-review runs pytest and either commits fixes (`review_clean`) or restores the worktree (`review_reverted_test_failure`, `review_timeout`, `review_skipped_no_diff`). Machine-verified outcomes enable deterministic pipeline progression.
- **`hermes_pipeline/review_phase.py`** — New module owning the code-owned review lifecycle: `capture_pre_review_state()`, `_run_hermes_subprocess()`, `run_pytest()`, `restore_worktree()`, `finalize_review()`, `write_review_artifacts()`, `commit_all()`.
- **Phase 5 config entry** — Added `phase_5_review` to `configs/phases.yaml` with `tools: "Read,Edit,Bash"`, `turns: 30`, `timeout: 2400`, positioned between `phase_4_development` and `phase_6_1_cso`.
- **Dry-run documentation** — `docs/pipeline/phase_5_review_dry_run_note.md` documents the required manual validation before enabling unattended runs.
- **Comprehensive tests** — New test modules `tests/test_phases.py` (config validation), `tests/test_phases_invoke.py` (routing tests), and `tests/test_review_phase.py` (unit tests with real git repo fixtures for capture/restore/finalize logic).

### Changed

- **`hermes_pipeline/phases.py`** — Added `_invoke_review_phase()` and routing in `_invoke_hermes()` to dispatch `phase_5_review` through the code-owned lifecycle instead of the generic rc-check path.
- **Phase order** — `configs/phases.yaml` now has 9 phases with `phase_5_review` inserted between development and CSO.

### Fixed

- **Path traversal in artifact filenames** — `todo_id` is now validated against a strict pattern before use in file paths, preventing directory escape.
- **Secret leakage in review artifacts** — Hermes stdout embedded in committed findings is now redacted of API keys, tokens, and other sensitive patterns.
- **Race condition in `restore_worktree`** — Documented the isolated-worktree assumption; sequential `reset --hard` + `clean -fd` is safe under that constraint.
- **Missing git author config** — `commit_all()` now sets explicit `user.name`/`user.email` via `-c` flags, preventing failures when no global git config exists.

### Added (docs)

- **Architecture overview** — `docs/ARCHITECTURE.md` documents lane structure, phase execution flow, and data flow across the pipeline.

## [0.4.4] - 2026-07-10

### Added

- **`pipeline-watch install-profile`** — Installs the bundled pipeline Hermes profile for unattended kanban execution. Use `--force` to reinstall after SOUL.md changes. See 0.4.6 below for a follow-up fix to how the profile is created.
- **`--assignee` flag on `init`** — Set the Hermes profile assignee when creating the project contract: `pipeline-watch init <project> --assignee pipeline`.
- **Doctor profile verification** — `doctor` now checks that a non-default assignee profile exists in Hermes. Fails exit code 2 if the profile is missing, with cause/fix guidance.
- **Bundled pipeline profile** — New in-package `data/profiles/pipeline/` with SOUL.md. Ships in the wheel.

### Changed

- **`phases.yaml` moved in-package** — Resolved via `importlib.resources` instead of repo-relative path. Works from installed wheel.
- **Hatchling wheel config** — `pyproject.toml` configured to include `hermes_pipeline` package data in wheel.

## [0.4.6] - 2026-07-11

### Changed

- **`install-profile` clones the active profile instead of installing a bare distribution** — `hermes profile install` only copies files present in the source distribution, so the bundled `distribution.yaml` (SOUL.md only) produced a `pipeline` profile with no `config.yaml`/`.env`/skills, unusable without manual setup. `install-profile` now runs `hermes profile create pipeline --clone` to inherit a working baseline from the currently-active profile, then overlays the bundled pipeline-specific `SOUL.md` on top. `--force` deletes any existing `pipeline` profile first.
- **`install-profile` error handling hardened** — `hermes profile delete`'s exit code is now checked instead of ignored; `hermes profile show` is wrapped in the same "Hermes not on PATH" handling as the other Hermes calls and surfaces its stderr on failure; the parsed profile path is validated as a real directory before `SOUL.md` is copied into it.

### Removed

- **`hermes_pipeline/data/profiles/pipeline/distribution.yaml`** — no longer used now that `install-profile` clones instead of installing a distribution.

## [0.4.3] - 2026-07-10

### Added

- **`pipeline-watch init` subcommand** — Writes the default pipeline execution contract (`.hermes/pipeline.toml`) for a project, declaring assignee and tool capabilities. Idempotent — use `--force` to regenerate after editing `configs/phases.yaml`. Capabilities are computed from phase definitions, not hardcoded.
- **`pipeline-watch doctor` subcommand** — Verifies a project's pipeline execution contract against `configs/phases.yaml`. Exit codes: 0 (clean), 1 (capability drift), 2 (missing/invalid contract).
- **Pipeline execution contract** — Versioned TOML manifest (`.hermes/pipeline.toml`) that declares per-project assignee and tool capabilities. Ticks validate the contract at start: missing contract falls back to computed defaults, stale version or capability mismatch fails the tick with a remediation message.

## [0.4.1] - 2026-07-08

### Added

- **Plan Gate (phase_2b_plan_gate)** — Human review checkpoint between Autoplan and Writing Plan. Autoplan produces a decision sheet (`## Decisions` section) that is parsed into a structured JSON artifact. The gate blocks the pipeline until a human approves or rejects the plan via `pipeline-watch approve-plan`.
- **`pipeline-watch approve-plan` subcommand** — Approve (`--approve`) or reject (`--reject --reason ...`) plan-gate decision sheets. Supports `--override q_id=LABEL` to correct individual recommendations without re-running Autoplan. Override injection protection via sanitization.
- **Risk classifier** — Keyword-based high-risk TODO classification (dependency, architecture, security, data, broad scope). Projects with rejection history are automatically classified as high-risk, triggering the plan gate.

### Changed

- **Phase list** — New `phase_2b_plan_gate` gate phase between `phase_2_autoplan` and `phase_3_writing_plan`. Gate phases are registered as blocked kanban tasks (never dispatched to an agent).
- **Dispatcher** — `maybe_plan_gate_ready` alert fires when plan-gate is blocked but pre-gate phases are complete, notifying via Slack.
- **Runner** — `_invoke_hermes` short-circuits gate phases (approved → skip, blocked/rejected → raise).
- **`all_phases_complete`** — Rejected plan-gate (archived) no longer stalls the tick; rejection sidecar on disk is the authoritative signal.

### Added (internal)

- **Decision sheet schema** — `DecisionSheet` / `DecisionQuestion` / `_Option` frozen dataclasses with full validation (unique question IDs, label matching, answer ∈ options, positive todo_id, schema versioning).
- **Gate status check** — `check_gate_status()` pure read of gate state from kanban + rejection sidecar. Returns `GateStatus` enum (BLOCKED, READY, RUNNING, FAILED, UNKNOWN).

## [0.4.2] - 2026-07-09

### Added

- **TODOS Manager skill v2.1** — Rewrote `skills/todos-manager/SKILL.md` to enforce canonical TODOS.md schema with five subcommands (`--init`, `--add`, `--convert`, `--audit`, `--archive`). Schema requires What/Why/Decisions fields, supports Pros/Cons/Context/Depends on/Assumptions/Completed/Resolved design. Stable TODO-<n> IDs computed by scanning both TODOS.md and TODOS-archive.md. Completed entries archive to `TODOS-archive.md`. Skill source lives at `skills/todos-manager/SKILL.md` (git-tracked); install via `scripts/install-todos-manager.sh` to symlink to `~/.claude/skills/` and `~/.agents/skills/`.

### Changed

- **TODOS.md preamble** — Added format rules blockquote documenting the enforced schema, status markers, required/optional fields, and ID assignment rules.
- **`.claude/` gitignore** — Added `.claude/` to `.gitignore` so agent-client skill installs remain local-only (platform-neutral skill source at `skills/todos-manager/`).

### Removed

- **`.claude/skills/todos-manager/SKILL.md`** — Removed from git tracking; skill now lives at `skills/todos-manager/SKILL.md` (git-tracked) and installs via symlink.

### Planned

- Dashboard UI for pipeline status
- Slack/Discord notifications for merge events
- Migration guides for breaking changes
