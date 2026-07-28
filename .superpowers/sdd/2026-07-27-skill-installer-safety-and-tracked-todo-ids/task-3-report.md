# Task 3 Report: Safe Skill Uninstall

## Status

Implemented Task 3. The `tpo skills uninstall` command now supports `--target`, `--scope`, and explicit `-y/--yes` confirmation. Existing destinations are preflighted before any selected destination is removed.

## Commits

- Final commit hash is recorded in the final response after the report is amended.

## Tests

- `uv run pytest tests/test_skills_install.py -k "uninstall" -v`: 4 passed, 19 deselected.
- `uv run pytest tests/test_skills_install.py -v`: 23 passed.
- `uv run ruff check hermes_pipeline/cli.py tests/test_skills_install.py`: all checks passed.
- `python -m compileall -q hermes_pipeline/cli.py`: passed.
- `git diff --check`: passed.
- `uv run pytest`: 814 passed, 5 skipped, 1 failed.

The full-suite failure is `tests/test_config_from_env.py::test_from_env_pipeline_projects_dir_compat_alias`. It is unrelated to this task: the test reads the existing default global config at `/Users/hyonchoi/.config/tpo/config.yaml`, which marks `projects_dir` active and therefore takes precedence over the deprecated environment alias.

## Files Changed

- `hermes_pipeline/cli.py`
  - Added the `skills uninstall` parser and confirmation flag.
  - Added `_cmd_skills_uninstall` with all-target preflight before deletion and structured output.
- `tests/test_skills_install.py`
  - Added parser, confirmation refusal, successful removal, and all-target preflight rollback tests.
- `.superpowers/sdd/2026-07-27-skill-installer-safety-and-tracked-todo-ids/task-3-report.md`
  - Added this implementation report.

## Self-Review Notes

- The command uses `_skills_install_targets` and `_preflight_skill_replacement` as required.
- Missing destinations are reported as successful no-ops.
- Symlinks and non-directory destinations are rejected by the shared preflight helper before deletion.
- A failed preflight for any selected target prevents deletion of every selected target.
- `config` is intentionally unused because `skills` commands do not require runtime configuration.

## Concerns

- The full suite is not completely green because of the unrelated global-config-sensitive compatibility test described above.
- The implementation follows the requested preflight/delete sequence; as with other filesystem operations, a destination can change between preflight and deletion.
