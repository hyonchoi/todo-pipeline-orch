# Plan is the execution authority

`Plan:` is the execution authority and the sole field that makes a TODO pipeline-actionable. `Spec:` is the authoritative outcome contract, and `Reference:` supplies non-authoritative context; when both a Plan and Spec exist, workers execute the Plan and validate the result against the Spec. This replaces the prior implication that `Spec:` drives execution and keeps execution instructions distinct from acceptance requirements.

A Plan may contain exactly one strict `json tpo-plan` block. TPO compiles its
ordered tasks into visible worker/controller-gate pairs. Manifest-free Markdown
remains a legacy compatibility contract and compiles to one development card.
Both the selected TODO and Plan bytes must be tracked at the pinned base commit;
local or hash-drifted authority is not executable.

Superseded in part by ADR-0003: the selected TODO is pinned as an issue snapshot in the run registration rather than as tracked TODOS.md bytes; the Plan clause stands.
