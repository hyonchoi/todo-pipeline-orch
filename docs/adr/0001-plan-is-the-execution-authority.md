# Plan is the execution authority

The folded Implementation Plan at the end of a GitHub issue body is the
execution authority for newly created TODOs. `Spec:` is the authoritative
outcome contract, and `Reference:` supplies non-authoritative context; when
both a Plan and Spec exist, workers execute the Plan and validate the result
against the Spec.

A new embedded Plan contains exactly one strict schema-v1 `json tpo-plan`
block. TPO extracts the final folded block before parsing issue-form fields,
pins its bytes in the issue snapshot, and compiles its ordered tasks into
visible worker/controller-gate pairs. Existing `### Plan` repository paths
remain readable as `legacy_path` sources; their bytes stay pinned to the base
commit and manifest-free Markdown still compiles to one development card. An
issue cannot contain both sources.

Registration schema v3 records the tagged source, hash, and (for embedded
Plans) the verified private `plan.md` run artifact. Readers retain schema-v2
compatibility for active legacy runs. There is no v3-to-v2 downgrade: finish
or abandon v3 runs before installing a version that cannot read them.

Superseded in part by ADR-0003: the selected TODO and embedded Plan are pinned
as an issue snapshot rather than as tracked `TODOS.md` bytes.
