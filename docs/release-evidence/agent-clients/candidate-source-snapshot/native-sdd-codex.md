# native-sdd / codex candidate qualification

- Evidence status: `candidate/source-snapshot`
- Release: `not selected`
- Source version: `1.0.1`
- Source commit: `unqualified; record the full tested commit after live execution`
- Profile/client: `native-sdd / codex`
- Timestamp: `not run`
- Environment: `not qualified`
- Hermes: `not qualified`
- Client: `not qualified`
- Verifier: `none; live qualification pending`
- Live qualification: `unrun`
- Result: `FAIL`

This qualifies discovery against the recorded source snapshot. It is not
release-final evidence and does not select a release version.

## Qualification limitation

The preceding standard finalization notice is a template, not a claim of
completed discovery: this candidate is unqualified. No live commands or
transcripts have been captured for this client. The source version identifies
the candidate baseline only. Provider-free tests prove prompt transport and
release preflight; they do not prove live user-policy semantics.

Run the [native-SDD live policy recipes](../../../release-qualification-agent-clients.md#native-sdd-live-policy-recipes)
for both `inherit` and `delegated`, retaining harmless user instructions.
Capture Hermes discovery and dispatch, the real client launch/stdin, process
exit, pinned mode through later workers, and implementation/review/finish
observations before replacing this record with genuine passing evidence.
Until then release finalization must remain blocked for this pair.
