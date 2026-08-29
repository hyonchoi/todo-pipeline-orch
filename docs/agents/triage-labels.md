# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

All five labels exist on the repository: they are part of the pipeline label
vocabulary created by `tpo todos labels sync <project>`, alongside the `tpo:*`
and mirror labels listed under
[TPO backlog items](issue-tracker.md#tpo-backlog-items). For pipeline TODOs,
`ready-for-agent` is the selection gate and any other triage label blocks
selection. TPO itself may move an issue from `ready-for-agent` to `needs-info`
when run registration fails on a permanent content fault (`plan_invalid`,
`branch_invalid`, `branch_exists`); infrastructure codes such as
`authority_untracked` or `authority_invalid` never demote, and it never applies any
other triage label.

The right-hand column is fixed for pipeline TODOs: `tpo todos labels sync`
creates these exact labels and `tpo doctor` reports any that are missing.
Remap only the left-hand column when another skill uses a different role
vocabulary.
