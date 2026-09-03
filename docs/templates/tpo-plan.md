# Embedded TPO Plan Template

New TODOs store the Plan once, as the final folded block in the issue body.
`tpo todos create` renders this wrapper and substitutes the created issue
number; request files contain only the human Markdown and task objects. The
result has this canonical shape:

````markdown
<details>
<summary>Implementation Plan</summary>
---
# Implementation Plan

Describe the implementation here.

```json tpo-plan
{
  "schema_version": 1,
  "todo_id": "TODO-N",
  "tasks": [
    {
      "id": "task-1",
      "title": "Implement behavior",
      "instructions": "Bounded implementation instructions.",
      "acceptance_criteria": ["Observable criterion"],
      "verification": ["uv run pytest tests/test_example.py"],
      "commit_message": "feat(scope): implement behavior"
    }
  ]
}
```

---
</details>
````

TPO permits at most 50 ordered tasks and rejects unknown keys, duplicate blocks
or IDs, unsafe IDs, empty required fields, mismatched TODO IDs, and oversized
values. The block must be final and unique. Human Plan Markdown cannot contain
structural `details`/matching `summary` tags, a `tpo-plan` fence, or
`proposed_plan` wrappers. Existing repository-path Plans remain supported only
as the legacy format.
