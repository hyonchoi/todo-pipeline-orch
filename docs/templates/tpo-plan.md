# TPO Plan Template

Describe the implementation in ordinary Markdown, then include exactly one
machine-readable block. Replace `TODO-N` and keep task IDs unique and safe.

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

TPO permits at most 50 ordered tasks and rejects unknown keys, duplicate blocks
or IDs, unsafe IDs, empty required fields, mismatched TODO IDs, and oversized
values. Commit the Plan and selected TODO before execution.
