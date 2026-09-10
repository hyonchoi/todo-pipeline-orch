# Private approved batch record

The writer validates structure and identity; it does not obtain approval,
perform reviews, publish issues, or manage release. Files are evidence, never
permission inferred from existence. Do not overwrite records on retry.

First write each approved held request with
`scripts/write_request.py PROJECT_ROOT TRANSACTION_UUID`, passing its JSON on
stdin. This exclusively creates
`.hermes/todo-create-input/TRANSACTION_UUID.json`. Then, **after exact packet
approval and before any remote creation**, pass batch JSON on stdin to:

```sh
python scripts/write_request.py --batch PROJECT_ROOT BATCH_UUID
```

This exclusively creates `.hermes/issue-planner-batches/BATCH_UUID.json`.
Files are contained, non-symlink regular files with mode `0600`; managed
directories are `0700`. Retain exact request bytes and this record through
partial failures, successful publication, and rollback. Never use an ordinary
redirect to replace a record.

## Schema v1

Use exactly these root fields:

| Field | Value |
| --- | --- |
| `schema_version` | Integer `1` |
| `batch_id` | New canonical lowercase UUIDv4 |
| `repository` | Literal canonical `OWNER/REPO` |
| `source_plan_sha256` | SHA256 of exact selected source content |
| `parent` | Parent object below, or null for one unsplit issue |
| `issues` | Nonempty array of executable issue objects below |
| `dependencies` | Array of `{"issue_key": CHILD, "requires": PREREQUISITE}` |
| `terminal_validator` | Final-validation issue key for groups; null for unsplit |
| `strategy` | `incremental` or `integration` |
| `manual_handoff` | Boolean; true for integration, false for incremental |
| `reviews` | Sanitized successful review entries below |
| `exceptions` | Approved atomic-Large/reduced-review exceptions below |
| `approval_digest` | Complete approved-record digest below |

Parent object has exactly `key`, `title`, `transaction_marker`, `body`, and
`body_sha256`. The marker is exactly
`<!-- issue-planner-batch: BATCH_UUID -->` and occurs once as its own line in
the approved body. Store full exact body including marker, and its UTF-8
SHA256. This preserves the recoverable preview; never store only a body hash.
The parent has no labels field, executable branch, or Plan manifest. Put
literal delivery contracts, coverage mapping, and source provenance in its
body; they are included in review and approval digests.

Every issue object has exactly `key`, `title`, `parent_key`,
`transaction_id`, `request_sha256`, `body`, `body_sha256`, `role`,
`size`, and `hold`. Keys and transaction UUIDs are unique. Use parent's key
for `parent_key`, null for unsplit. Roles are `implementation`,
`scoped-validation`, or `final-validation`; sizes are `Small`, `Medium`,
or specifically approved atomic `Large`. `hold` must be boolean true.

`request_sha256` hashes raw bytes of the retained request file; the writer
checks its hash, identity, title, and true hold. Retained request input is
limited to 4 MiB during batch verification. Store the exact canonical
pre-number child body in `body` and hash its UTF-8 bytes in `body_sha256`.
After creation the CLI's assigned TODO number changes rendered bytes:
verify the remote body using the approved request rendered with the assigned
number, rather than comparing it directly to the preview digest.

Dependencies contain only executable keys, never the parent, and form a
duplicate-free acyclic graph. Groups have exactly one terminal validator,
which directly requires every other child and remains Small/Medium.
Every child has the same parent. An unsplit record has one implementation
issue, no parent, no dependencies, and no terminal key.

## Review and approval bindings

Canonical digest encoding is UTF-8 JSON with sorted keys, compact separators
`(",", ":")`, `ensure_ascii=False`, and `allow_nan=False`.
`packet_digest` is SHA256 of the record excluding `reviews` and
`approval_digest`. `approval_digest` is SHA256 of the record excluding only
`approval_digest`. Thus approval also binds review evidence. Changes to any
approved packet content require refreshed digests and approvals; substantive
changes require new independent reviews.

Each review has exactly `engine` (`codex` or `claude`), `verdict` (`PASS`),
`issue_keys`, `packet_digest`, `route` (`read-only`), and
`evidence_sha256`. Hash retained sanitized reviewer evidence that states a
separate PASS for each listed issue key. Every available engine must cover
every key including the parent. Do not translate missing verdicts or findings
into PASS. The record cannot attest that a reviewer was independent: perform
and disclose actual reviews before writing it.

Atomic-Large exception objects have exactly `kind: "atomic-large"`,
`issue_key`, `rationale_sha256`, and `permission_sha256`. Retain the
sanitized rationale and explicit particular permission bound by these hashes.

Reduced-review exceptions have exactly `kind: "reduced-review"`,
`unavailable_engine`, `disclosure_sha256`, and `alternate_route_sha256`.
Retain the sanitized unavailable-route evidence and user disclosure. Require
the remaining independent engine's PASS on every key. Two unavailable
engines cannot produce a publishable record. Record validation does not
replace the ten-minute deadlines, three-round limit, or alternate-route work.
