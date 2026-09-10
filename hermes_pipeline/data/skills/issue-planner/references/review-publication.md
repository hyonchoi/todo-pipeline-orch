# Review and publication

Read before review or remote mutation. This is an instruction-level workflow,
not an automated batch publisher or AI-review service.

## Independent review

Supply only the complete packet and relevant repository evidence to fresh
read-only Codex and Claude reviewer contexts, without authoring history. The
author cannot review their own packet. Each engine assesses completeness of
each issue and whole-group consistency: coverage gaps, task/test validity,
overlapping ownership, dependencies, sizing, confirmed PR strategy, and whether
final validation proves the original goal. A batch session is allowed only
with a separate verdict for every parent/child key, including final validation.

Bind verdicts to those keys and the complete packet digest defined in the
batch-record reference. Review exact canonical previews, source provenance,
delivery contracts, graph, exceptions, and request identities. PASS means no
unresolved actionable findings. Findings block even if output omits a verdict;
UNVERIFIED is never PASS. Sanitize evidence; retain its hash, not raw provider
responses. Record separate per-key verdict evidence even if the batch record
groups successful keys in a single engine entry.

Give each reviewer route a ten-minute deadline; cancel safely on timeout.
Allow at most three review/fix rounds. Substantive edits invalidate earlier
verdicts and require fresh review of the changed complete packet. After the
third failed round stop and report blockers. Before declaring an engine
unavailable, try a safe alternate read-only route under the same deadline
rule. Never install tools, alter authentication, or weaken sandbox controls
to obtain a review. Disclose reduced review when one engine is unavailable;
publication then requires an independent PASS covering every issue from the
remaining engine and recorded alternate-route evidence. Neither engine
available means stop. Failed findings are not engine unavailability.

## Canonical previews and approval

Run for every request, with literal project slug and EOF at confirmation:

```sh
tpo todos create PROJECT --request-file REQUEST </dev/null
```

Show the full canonical output: Project, Repository, title, body, and hold.
For groups also show the entire parent title/body, native hierarchy and
dependency graph. Show reviewed strategy, exceptions, sanitized per-key review
results, and whether incremental release or permanent manual-only holds will
follow. Confirm target identities match research. Obtain one exact `create`
reply for this entire packet. Never infer it from earlier plan approval,
similar words, or a record's existence. Any edit or target substitution
requires a new complete preview and approval; substantive edits require new
reviews. Do not call `--yes` before approval.

After approval and before the first remote creation, exclusively write the
immutable private batch record using `scripts/write_request.py --batch
PROJECT_ROOT UUID` and the approved JSON on stdin. Follow the batch-record
schema. Retain the exact parent preview, requests, sanitized evidence, and
approval binding for recovery. Hashes alone cannot reconstruct lost content.

## Ordered publication

1. On an initial split-group publication, enumerate **all pages** of issues
   in the literal repository, including closed issues and excluding PRs.
   Compare exact parent transaction-marker lines, never substring matches or
   search-index results. More than one match is ambiguous: stop for manual
   reconciliation. One match must pass full identity/body/non-executable-state
   readback before reuse. Zero matches permits parent creation only on a
   known first attempt, using the approved ordinary title/body and marker.
   Use GitHub for the parent; never `tpo todos create` or executable labels.
   If creation is uncertain, or this is a resumed batch with no verifiable
   parent, stop for manual reconciliation rather than creating another.
2. Create each executable child through the immutable request transaction:

   ```sh
   tpo todos create PROJECT --request-file REQUEST --approved-repo OWNER/REPO --yes
   ```

   Every batch child, including a single issue, has `hold: true` in its initial
   creation call. The CLI verifies the hold before eligibility transitions.
   Preserve existing per-issue receipts/journals. On approved recovery, first
   retry the identical command without `--issue`; transaction discovery is the
   normal route. Supply `--issue N` only after independently confirming the
   partial issue and its exact transaction marker. Never mint a replacement
   transaction, hand-edit, delete, close, or recreate a partial child.
3. Bind actual child numbers to their approved keys using verified transaction
   markers. Establish native sub-issue relationships and separate dependency
   edges through GitHub; read back every edge, including all terminal-validator
   prerequisites. Wrong parents, duplicate markers, absent edges, unexpected
   edges, or an incomplete graph block all release. Preserve partial resources
   for manual reconciliation; do not replace them. Child bodies remain the
   approved stable-key versions; resolve parent numbers through native links
   later during PR preparation.
4. Before any incremental release, re-read the parent identity, exact approved
   body/title, and non-executable state; every child's identity, exact rendered
   body/title, completed creation transaction, and hold; and the complete
   native hierarchy/dependency graph. Child previews contain a placeholder
   TODO number: compare remote bodies against deterministic CLI rendering of
   the approved request with its assigned number, not the pre-number hash.
   Unexpected body edits or missing holds are drift, not permission to continue.
   Stop for reconciliation. For a single issue apply the same checks without
   a parent or hierarchy.
5. Integration strategy releases **none**, even after acceptance of manual
   execution. For incremental strategy only, remove each verified child's
   `tpo:on-hold` label after the whole packet passes readback. Re-read each
   result. This release is not atomic. An interruption, uncertain mutation,
   or resumed release requires manual reconciliation, not blind continuation
   or an assertion that all children are still held. Report exactly which
   releases are verified, which remain held, and which are unknown. Do not
   automatically re-hold children that may already have been selected.

Keep requests and batch records after success as well as partial failure;
they bind provenance and make later reconciliation possible. Records are
evidence, never reusable authorization for rescoping or new remote writes.
Handoff must state tested/publication evidence limits. Live publication checks
need separate authorization; deterministic local checks do not prove live
GitHub or provider behavior.
