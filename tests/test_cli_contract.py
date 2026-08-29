"""Tests for the init/doctor subcommands (pipeline execution contract)."""
from __future__ import annotations

import subprocess as _test_sp
from unittest.mock import MagicMock

import pytest

from hermes_pipeline.cli import (
    _cmd_doctor,
    _cmd_init,
    _cmd_install_profile,
    _cmd_plan_validate,
    _doctor_active_registration,
    _verify_hermes_skill_registry_prerequisite,
    build_parser,
)
from hermes_pipeline.config import Config
from hermes_pipeline.contract import (
    CONTRACT_SCHEMA_VERSION,
    PipelineContract,
    _render_contract_toml,
    bundled_profile_dir,
)
from hermes_pipeline.phases import Phase


class TestPlanValidate:
    def test_parser_contract(self):
        args = build_parser().parse_args(
            [
                "plan",
                "validate",
                "demo",
                "--todo",
                "TODO-42",
                "--plan",
                "docs/candidate.md",
                "--require-manifest",
            ]
        )
        assert args.project == "demo"
        assert args.todo == 42
        assert args.require_manifest is True
        assert args.plan == "docs/candidate.md"

    def test_candidate_plan_validates_before_todo_is_persisted(self, tmp_path, capsys):
        project = _create_project(tmp_path, "demo")
        (project / "docs").mkdir()
        (project / "docs" / "candidate.md").write_text("# Legacy plan\n")

        result = _cmd_plan_validate(
            FakeArgs(
                project="demo",
                todo=42,
                plan="docs/candidate.md",
                require_manifest=False,
            ),
            Config(projects_dir=tmp_path),
        )

        assert result == 0
        assert "legacy" in capsys.readouterr().out.lower()
        assert "TODO-42" not in (project / "TODOS.md").read_text()

    def test_candidate_plan_preserves_repository_path_safety(self, tmp_path, capsys):
        _create_project(tmp_path, "demo")
        (tmp_path / "outside.md").write_text("# Outside\n")

        result = _cmd_plan_validate(
            FakeArgs(
                project="demo",
                todo=42,
                plan="../outside.md",
                require_manifest=False,
            ),
            Config(projects_dir=tmp_path),
        )

        assert result == 1
        assert "attachment_outside_repository" in capsys.readouterr().out

    def test_valid_manifest_reports_task_count(self, tmp_path, capsys, fake_gh):
        project = _create_project(tmp_path, "demo")
        _serve_issue_plan(fake_gh, "docs/plan.md")
        (project / "docs").mkdir()
        (project / "docs" / "plan.md").write_text(
            '```json tpo-plan\n{"schema_version":1,"todo_id":"TODO-42","tasks":'
            '[{"id":"task-1","title":"Build","instructions":"Do it",'
            '"acceptance_criteria":["It works"],"verification":["uv run pytest"],'
            '"commit_message":"feat: build"}]}\n```\n'
        )

        result = _cmd_plan_validate(
            FakeArgs(project="demo", todo=42, require_manifest=False),
            Config(projects_dir=tmp_path),
        )

        assert result == 0
        output = capsys.readouterr().out
        assert "valid manifest" in output
        assert "1 task" in output

    def test_legacy_plan_warns_unless_manifest_is_required(self, tmp_path, capsys, fake_gh):
        project = _create_project(tmp_path, "demo")
        _serve_issue_plan(fake_gh, "docs/plan.md")
        (project / "docs").mkdir()
        (project / "docs" / "plan.md").write_text("# Legacy plan\n")
        config = Config(projects_dir=tmp_path)

        assert _cmd_plan_validate(
            FakeArgs(project="demo", todo=42, require_manifest=False), config
        ) == 0
        assert "legacy" in capsys.readouterr().out.lower()
        assert _cmd_plan_validate(
            FakeArgs(project="demo", todo=42, require_manifest=True), config
        ) == 1
        assert "requires" in capsys.readouterr().out.lower()

    def test_invalid_manifest_fails_without_raw_document(self, tmp_path, capsys, fake_gh):
        project = _create_project(tmp_path, "demo")
        _serve_issue_plan(fake_gh, "plan.md")
        (project / "plan.md").write_text("```json tpo-plan\n{secret}\n```\n")

        result = _cmd_plan_validate(
            FakeArgs(project="demo", todo=42, require_manifest=False),
            Config(projects_dir=tmp_path),
        )

        assert result == 1
        output = capsys.readouterr().out
        assert "invalid_json" in output
        assert "secret" not in output


class FakeArgs:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _create_project(projects_dir, name):
    project_dir = projects_dir / name
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "TODOS.md").write_text("# TODOS\n")
    return project_dir


def _serve_issue_plan(fake_gh, plan, *, number=42):
    """Serve issue ``number`` on ``acme/repo`` whose body attaches ``plan``."""
    import json

    from tests.gh_fakes import issue_payload

    fake_gh.on(*ORIGIN, stdout="https://github.com/acme/repo.git\n")
    fake_gh.on(
        *API,
        f"repos/acme/repo/issues/{number}",
        stdout=json.dumps(issue_payload(number, body=f"### Plan\n\n{plan}\n")),
    )


class TestTodoIdFlag:
    @pytest.mark.parametrize("value", ["0", "-5", "TODO-0"])
    def test_rejects_non_positive_ids(self, value):
        with pytest.raises(SystemExit) as excinfo:
            build_parser().parse_args(["plan", "validate", "demo", "--todo", value])
        assert excinfo.value.code == 2


class TestPlanValidateFromIssue:
    """Without ``--plan`` the Plan attachment is read from the GitHub issue."""

    def _serve(self, fake_gh, tmp_path, body):
        import json

        from tests.gh_fakes import issue_payload

        project = _create_project(tmp_path, "demo")
        fake_gh.on(*ORIGIN, stdout="https://github.com/acme/repo.git\n")
        fake_gh.on(*API, "repos/acme/repo/issues/42", stdout=json.dumps(issue_payload(42, body=body)))
        return project

    def _args(self, **overrides):
        values = {"project": "demo", "todo": 42, "plan": None, "require_manifest": False}
        values.update(overrides)
        return FakeArgs(**values)

    def test_legacy_plan_from_issue_is_valid(self, tmp_path, capsys, fake_gh):
        project = self._serve(fake_gh, tmp_path, "### Plan\n\ndocs/plan.md\n")
        (project / "docs").mkdir()
        (project / "docs" / "plan.md").write_text("# Legacy plan\n")

        assert _cmd_plan_validate(self._args(), Config(projects_dir=tmp_path)) == 0
        out = capsys.readouterr().out
        assert "valid legacy Markdown for TODO-42" in out
        assert ["api", "-H", "Accept: application/vnd.github+json", "repos/acme/repo/issues/42"] in fake_gh.gh_calls()

    def test_manifest_todo_id_must_match_issue(self, tmp_path, capsys, fake_gh):
        project = self._serve(fake_gh, tmp_path, "### Plan\n\ndocs/plan.md\n")
        (project / "docs").mkdir()
        (project / "docs" / "plan.md").write_text(
            '```json tpo-plan\n{"schema_version":1,"todo_id":"TODO-7","tasks":'
            '[{"id":"task-1","title":"Build","instructions":"Implement it",'
            '"acceptance_criteria":["Works"],"verification":["uv run pytest"],'
            '"commit_message":"feat: build"}]}\n```\n'
        )

        assert _cmd_plan_validate(self._args(), Config(projects_dir=tmp_path)) == 1
        assert "Plan validation failed for TODO-42:" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("body", "code"),
        [
            ("### What\n\nno plan\n", "plan_invalid:missing"),
            ("### Plan\n\na.md\n\n### Plan\n\nb.md\n", "plan_invalid:duplicate"),
        ],
    )
    def test_missing_or_duplicate_plan_sections_fail(self, tmp_path, capsys, fake_gh, body, code):
        self._serve(fake_gh, tmp_path, body)

        assert _cmd_plan_validate(self._args(), Config(projects_dir=tmp_path)) == 1
        assert f"Plan validation failed for TODO-42: {code}" in capsys.readouterr().out

    def test_control_character_plan_path_is_unreadable_not_a_traceback(self, tmp_path, capsys):
        _create_project(tmp_path, "demo")

        result = _cmd_plan_validate(
            self._args(plan="docs/pl\x00an.md"), Config(projects_dir=tmp_path)
        )

        assert result == 1
        assert "Plan validation failed for TODO-42: attachment_unreadable" in capsys.readouterr().out

    def test_closed_issue_appends_warning(self, tmp_path, capsys, fake_gh):
        import json

        from tests.gh_fakes import issue_payload

        project = _create_project(tmp_path, "demo")
        (project / "docs").mkdir()
        (project / "docs" / "plan.md").write_text("# Legacy plan\n")
        fake_gh.on(*ORIGIN, stdout="https://github.com/acme/repo.git\n")
        fake_gh.on(*API, "repos/acme/repo/issues/42", stdout=json.dumps(issue_payload(
            42, body="### Plan\n\ndocs/plan.md\n", state="closed", state_reason="completed",
        )))

        assert _cmd_plan_validate(self._args(), Config(projects_dir=tmp_path)) == 0
        out = capsys.readouterr().out
        assert "valid legacy Markdown for TODO-42" in out
        assert "; warning: issue is closed (completed)" in out

    def test_github_failure_is_reported_without_stderr(self, tmp_path, capsys, fake_gh):
        _create_project(tmp_path, "demo")
        fake_gh.on(*ORIGIN, stdout="https://github.com/acme/repo.git\n")
        fake_gh.on(*API, rc=1, stderr="HTTP 429 rate limit exceeded secret-token")

        assert _cmd_plan_validate(self._args(), Config(projects_dir=tmp_path)) == 1
        captured = capsys.readouterr()
        assert "Plan validation failed for TODO-42: gh_rate_limited" in captured.out
        assert "secret-token" not in captured.out + captured.err


class TestRecoverCounterRemoved:
    def test_recover_counter_is_not_a_subcommand(self):
        parser = build_parser()
        assert "recover-counter" not in parser.format_help()
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(["recover-counter", "x"])
        assert excinfo.value.code == 2


def _todos_help():
    parser = build_parser()
    for action in parser._subparsers._group_actions:
        if hasattr(action, "choices") and "todos" in action.choices:
            return action.choices["todos"].format_help()
    raise AssertionError("todos subparser missing")


class TestTodosLabelsSync:
    """``tpo todos labels sync <project>`` creates the missing label vocabulary."""

    def _project(self, tmp_path, fake_gh, *, present):
        import json

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        fake_gh.on(*ORIGIN, stdout="https://github.com/acme/repo.git\n")
        fake_gh.on("gh", "auth", "status")
        fake_gh.on("gh", "label", "list", stdout=json.dumps([{"name": n} for n in present]))
        fake_gh.on("gh", "label", "create")
        return Config(projects_dir=projects_dir)

    def test_parser(self):
        args = build_parser().parse_args(["todos", "labels", "sync", "demo"])
        assert (args.todos_command, args.labels_command, args.project) == ("labels", "sync", "demo")
        assert "labels" in _todos_help()

    def test_creates_missing_labels_after_auth_check(self, tmp_path, fake_gh, capsys):
        from hermes_pipeline.cli import _cmd_todos_labels_sync
        from hermes_pipeline.github_issues import LABEL_VOCABULARY

        names = [name for name, _c, _d in LABEL_VOCABULARY]
        config = self._project(tmp_path, fake_gh, present=names[2:])
        args = build_parser().parse_args(["todos", "labels", "sync", "demo"])

        assert _cmd_todos_labels_sync(args, config) == 0
        out = capsys.readouterr().out
        assert f"created: {names[0]}" in out and f"created: {names[1]}" in out
        calls = fake_gh.gh_calls()
        assert calls[0] == ["auth", "status", "--hostname", "github.com"]
        assert calls[1][:2] == ["label", "list"]
        creates = [c for c in calls if c[:2] == ["label", "create"]]
        assert [c[-1] for c in creates] == names[:2]
        assert all("--repo" in c and "acme/repo" in c for c in creates)

    def test_zero_writes_when_all_labels_present(self, tmp_path, fake_gh, capsys):
        from hermes_pipeline.cli import _cmd_todos_labels_sync
        from hermes_pipeline.github_issues import LABEL_VOCABULARY

        config = self._project(tmp_path, fake_gh, present=[n for n, _c, _d in LABEL_VOCABULARY])
        args = build_parser().parse_args(["todos", "labels", "sync", "demo"])

        assert _cmd_todos_labels_sync(args, config) == 0
        assert (
            f"labels up to date ({len(LABEL_VOCABULARY)} names present; "
            "color/description not compared)"
        ) in capsys.readouterr().out
        assert not [c for c in fake_gh.gh_calls() if c[:2] == ["label", "create"]]

    def test_auth_failure_reports_code_and_writes_nothing(self, tmp_path, fake_gh, capsys):
        from hermes_pipeline.cli import _cmd_todos_labels_sync
        from hermes_pipeline.github_issues import LABEL_VOCABULARY

        config = self._project(tmp_path, fake_gh, present=[])
        fake_gh.on("gh", "auth", "status", rc=1, stderr="not logged in; run gh auth login")
        args = build_parser().parse_args(["todos", "labels", "sync", "demo"])

        assert _cmd_todos_labels_sync(args, config) == 1
        assert capsys.readouterr().err.strip() == f"Error: gh_auth (0 of {len(LABEL_VOCABULARY)} labels created)"
        assert not [c for c in fake_gh.gh_calls() if c[:2] == ["label", "create"]]

    def test_partial_failure_reports_created_labels_and_progress(self, tmp_path, fake_gh, capsys):
        from hermes_pipeline.cli import _cmd_todos_labels_sync
        from hermes_pipeline.github_issues import LABEL_VOCABULARY

        names = [name for name, _c, _d in LABEL_VOCABULARY]
        config = self._project(tmp_path, fake_gh, present=names[3:])
        fake_gh.on("gh", "label", "create", handler=lambda argv: (
            (1, "", "HTTP 401") if argv[-1] == names[2] else (0, "", "")
        ))
        args = build_parser().parse_args(["todos", "labels", "sync", "demo"])

        assert _cmd_todos_labels_sync(args, config) == 1
        captured = capsys.readouterr()
        assert captured.out.splitlines() == sorted(f"created: {name}" for name in names[:2])
        assert captured.err.strip() == f"Error: gh_auth (2 of {len(names)} labels created)"

    def test_origin_failure_includes_detail(self, tmp_path, fake_gh, capsys):
        from hermes_pipeline.cli import _cmd_todos_labels_sync
        from hermes_pipeline.github_issues import LABEL_VOCABULARY

        config = self._project(tmp_path, fake_gh, present=[])
        fake_gh.on(*ORIGIN, stdout="git@gitlab.com:acme/repo.git\n")
        args = build_parser().parse_args(["todos", "labels", "sync", "demo"])

        assert _cmd_todos_labels_sync(args, config) == 1
        assert capsys.readouterr().err.strip() == (
            "Error: origin_identity_invalid: origin is not a github.com remote "
            f"(0 of {len(LABEL_VOCABULARY)} labels created)"
        )

    def test_rejected_create_is_not_swallowed(self, tmp_path, fake_gh, capsys):
        from hermes_pipeline.cli import _cmd_todos_labels_sync

        config = self._project(tmp_path, fake_gh, present=[])
        fake_gh.on("gh", "label", "create", rc=1, stderr="Validation Failed (HTTP 422)")
        args = build_parser().parse_args(["todos", "labels", "sync", "demo"])

        assert _cmd_todos_labels_sync(args, config) == 1
        assert capsys.readouterr().err.startswith("Error: gh_rejected (0 of ")

    def test_truncated_label_listing_is_actionable(self, tmp_path, fake_gh, capsys):
        from hermes_pipeline.cli import _cmd_todos_labels_sync

        config = self._project(tmp_path, fake_gh, present=[f"l{i}" for i in range(1000)])
        args = build_parser().parse_args(["todos", "labels", "sync", "demo"])

        assert _cmd_todos_labels_sync(args, config) == 1
        assert capsys.readouterr().err.strip() == (
            "Error: gh_truncated (label list capped at 1000; sync manually)"
        )
        assert not [c for c in fake_gh.gh_calls() if c[:2] == ["label", "create"]]

    def test_unknown_project_returns_2(self, tmp_path, fake_gh):
        from hermes_pipeline.cli import _cmd_todos_labels_sync

        config = self._project(tmp_path, fake_gh, present=[])
        args = build_parser().parse_args(["todos", "labels", "sync", "nope"])
        assert _cmd_todos_labels_sync(args, config) == 2
        assert fake_gh.gh_calls() == []


def _create_valid_doctor_project(projects_dir, profile="gstack"):
    project_dir = _create_project(projects_dir, "demo")
    (project_dir / ".hermes").mkdir(parents=True)
    (project_dir / ".hermes" / "pipeline.toml").write_text(
        "schema_version = 2\n"
        'capabilities = ["Read", "Write", "Edit", "Bash"]\n'
        f'profile = "{profile}"\n'
    )
    return FakeArgs(project="demo")


def _allow_hermes_registry_skill_check(*args, **kwargs):
    cmd = args[0] if args else kwargs.get("args", [])
    if cmd == ["hermes", "--version"]:
        return MagicMock(returncode=0, stderr="", stdout="Hermes Agent v0.19.0\n")
    if cmd == ["hermes", "skills", "list", "--enabled-only"]:
        return MagicMock(returncode=0, stderr="", stdout="ai-coding-agents\n")
    pytest.fail("doctor must not inspect a remote worker for external skills")


def _seed_doctor_github(fake_gh, issues=(), *, labels=None, repo="acme/repo"):
    """Serve a healthy GitHub for doctor: origin, auth, full label vocabulary, issues."""
    import json

    from hermes_pipeline.github_issues import LABEL_VOCABULARY
    from tests.gh_fakes import seed_project_issues

    seed_project_issues(fake_gh, list(issues), repo=repo)
    fake_gh.on("gh", "auth", "status")
    names = [name for name, _color, _desc in LABEL_VOCABULARY] if labels is None else list(labels)
    fake_gh.on("gh", "label", "list", stdout=json.dumps([{"name": name} for name in names]))
    return fake_gh


def test_doctor_rejects_hermes_older_than_minimum(tmp_path, mocker, capsys):
    args = _create_valid_doctor_project(tmp_path)
    def run(cmd, **_kwargs):
        output = (
            "ai-coding-agents\n"
            if cmd == ["hermes", "skills", "list", "--enabled-only"]
            else "Hermes Agent v0.18.9\n"
        )
        return MagicMock(returncode=0, stderr="", stdout=output)

    mocker.patch("hermes_pipeline.cli._cli_sp.run", side_effect=run)

    assert _cmd_doctor(args, Config(projects_dir=tmp_path)) == 2
    output = capsys.readouterr().out
    assert "0.19.0" in output
    assert "0.18.9" in output


def test_doctor_reports_plan_readiness_from_github_issues(tmp_path, mocker, capsys, fake_gh):
    from tests.gh_fakes import issue_payload

    args = _create_valid_doctor_project(tmp_path, profile="native-sdd")
    project = tmp_path / "demo"
    (project / "docs").mkdir()
    (project / "docs" / "manifest.md").write_text(
        '```json tpo-plan\n{"schema_version":1,"todo_id":"TODO-1","tasks":'
        '[{"id":"task-1","title":"Build","instructions":"Implement it",'
        '"acceptance_criteria":["Works"],"verification":["uv run pytest"],'
        '"commit_message":"feat: build"}]}\n```\n'
    )
    _seed_doctor_github(fake_gh, [
        issue_payload(1, body="### Plan\n\ndocs/manifest.md\n\n### Branch\n\nfeat/one\n"),
        issue_payload(2, body="### Branch\n\nfeat/two\n"),
        issue_payload(3, body="### Branch\n\nfeat/three\n", labels=("tpo:todo",)),
    ])
    mocker.patch(
        "hermes_pipeline.cli._cli_sp.run",
        side_effect=_allow_hermes_registry_skill_check,
    )

    assert _cmd_doctor(args, Config(projects_dir=tmp_path)) == 0
    output = capsys.readouterr().out
    assert "GitHub auth: ok" in output
    assert "Repository: acme/repo" in output
    assert "Label vocabulary: ok" in output
    assert "Plan readiness: eligible=1 blocked=2 (not_ready=1 plan_invalid=1)" in output
    assert "Runs: active=0 delivered=0 abandoned=0" in output
    assert "TODOS.md" not in output
    assert "OK:" in output


def test_doctor_github_checks_are_offline_tolerant(tmp_path, mocker, capsys, fake_gh):
    args = _create_valid_doctor_project(tmp_path)
    fake_gh.on(*ORIGIN, stdout="https://github.com/acme/repo.git\n")
    fake_gh.on("gh", "auth", "status", rc=1, stderr="not logged in")
    fake_gh.on("gh", "label", "list", rc=1, stderr="HTTP 429 rate limit exceeded")
    fake_gh.on(*API, rc=1, stderr="HTTP 429 rate limit exceeded")
    mocker.patch(
        "hermes_pipeline.cli._cli_sp.run",
        side_effect=_allow_hermes_registry_skill_check,
    )

    assert _cmd_doctor(args, Config(projects_dir=tmp_path)) == 1
    output = capsys.readouterr().out
    assert "WARNING: GitHub auth unavailable (gh_auth)" in output
    assert "Repository: acme/repo" in output
    assert "WARNING: Label vocabulary unavailable (gh_rate_limited)" in output
    assert "WARNING: Plan readiness unavailable (gh_rate_limited)" in output
    assert "Runs: active=0 delivered=0 abandoned=0" in output
    assert "OK:" not in output


@pytest.mark.parametrize(
    ("rule", "detail"),
    [
        ({"stdout": "git@gitlab.com:acme/repo.git\n"}, "origin is not a github.com remote"),
        ({"raises": FileNotFoundError("git")}, "git not found"),
        ({"rc": 128, "stderr": "fatal: not a git repository"}, "no origin remote or not a git repository"),
        ({"raises": _test_sp.TimeoutExpired("git", 60)}, "git remote get-url origin failed"),
        ({"raises": PermissionError("git")}, "git remote get-url origin failed"),
    ],
)
def test_doctor_reports_repository_identity_detail(tmp_path, mocker, capsys, fake_gh, rule, detail):
    args = _create_valid_doctor_project(tmp_path)
    _seed_doctor_github(fake_gh)
    fake_gh.on(*ORIGIN, **rule)
    mocker.patch(
        "hermes_pipeline.cli._cli_sp.run",
        side_effect=_allow_hermes_registry_skill_check,
    )

    assert _cmd_doctor(args, Config(projects_dir=tmp_path)) == 1
    output = capsys.readouterr().out
    assert f"INVALID: repository identity: {detail}" in output
    assert "fatal:" not in output
    assert "OK:" not in output


def test_doctor_reports_missing_labels_with_sync_fix(tmp_path, mocker, capsys, fake_gh):
    args = _create_valid_doctor_project(tmp_path)
    _seed_doctor_github(fake_gh, labels=("tpo:todo", "needs-triage"))
    mocker.patch(
        "hermes_pipeline.cli._cli_sp.run",
        side_effect=_allow_hermes_registry_skill_check,
    )

    assert _cmd_doctor(args, Config(projects_dir=tmp_path)) == 1
    output = capsys.readouterr().out
    assert "INVALID: missing " in output
    assert "ready-for-agent" in output and "tpo:on-hold" in output
    assert "Fix: tpo todos labels sync demo" in output
    assert fake_gh.gh_calls().count(["auth", "status", "--hostname", "github.com"]) == 1


def _seed_runs(state, runs):
    import json

    for tick, payload, marker in runs:
        run = state / "runs" / tick
        run.mkdir(parents=True)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        (run / "registration.json").write_text(text)
        if marker:
            (run / marker).write_text("")


_THREE_RUNS = (
    ("tick-a", {"schema_version": 2, "issue_number": 5}, None),
    ("tick-b", {"schema_version": 2, "issue_number": 6}, "issue-closed"),
    ("tick-c", {"schema_version": 2, "issue_number": 7}, "abandoned"),
)


def test_doctor_lists_active_runs_without_current_tick(tmp_path, mocker, capsys, fake_gh):
    args = _create_valid_doctor_project(tmp_path)
    _seed_doctor_github(fake_gh)
    _seed_runs(tmp_path / "demo" / ".hermes", _THREE_RUNS)
    mocker.patch(
        "hermes_pipeline.cli._cli_sp.run",
        side_effect=_allow_hermes_registry_skill_check,
    )

    assert _cmd_doctor(args, Config(projects_dir=tmp_path)) == 0
    output = capsys.readouterr().out
    assert "Runs: active=1 delivered=1 abandoned=1" in output
    assert "tick tick-a → #5 (active; no current tick)" in output
    assert "tick tick-b" not in output and "tick tick-c" not in output
    assert "WARNING" not in output and "Fix" not in output
    assert "OK:" in output


def test_doctor_flags_orphaned_active_run_when_current_tick_differs(
    tmp_path, mocker, capsys, fake_gh
):
    args = _create_valid_doctor_project(tmp_path)
    _seed_doctor_github(fake_gh)
    state = tmp_path / "demo" / ".hermes"
    _seed_runs(state, _THREE_RUNS)
    (state / "current_tick_id.txt").write_text("tick-z\n")
    mocker.patch(
        "hermes_pipeline.cli._cli_sp.run",
        side_effect=_allow_hermes_registry_skill_check,
    )

    assert _cmd_doctor(args, Config(projects_dir=tmp_path)) == 1
    output = capsys.readouterr().out
    assert "tick tick-a → #5\n" in output
    assert "WARNING: run tick-a is active but is not the current tick" in output
    assert (
        "Fix (tick tick-a): tpo todos complete demo --todo 5 --pr <pr> if delivered, "
        f"or touch {state / 'runs' / 'tick-a' / 'abandoned'} to give up"
    ) in output
    assert "Current tick tick-z: no registration (no TODO selected)" in output
    assert "Fix: preserve" not in output
    assert "OK:" not in output


def test_doctor_accepts_active_run_that_is_the_current_tick(tmp_path, mocker, capsys, fake_gh):
    args = _create_valid_doctor_project(tmp_path)
    _seed_doctor_github(fake_gh)
    state = tmp_path / "demo" / ".hermes"
    _seed_runs(state, _THREE_RUNS)
    (state / "current_tick_id.txt").write_text("tick-a\n")
    # The minimal registration fixture has no worktree; the authority check is
    # covered by its own tests, so isolate the Runs summary here.
    mocker.patch("hermes_pipeline.cli._doctor_active_registration", return_value=True)
    mocker.patch(
        "hermes_pipeline.cli._cli_sp.run",
        side_effect=_allow_hermes_registry_skill_check,
    )

    assert _cmd_doctor(args, Config(projects_dir=tmp_path)) == 0
    output = capsys.readouterr().out
    assert "tick tick-a → #5\n" in output
    assert "WARNING" not in output and "Fix" not in output
    assert "OK:" in output


def test_doctor_runs_summary_skips_unsupported_and_forged_registrations(
    tmp_path, mocker, capsys, fake_gh
):
    args = _create_valid_doctor_project(tmp_path)
    _seed_doctor_github(fake_gh)
    _seed_runs(tmp_path / "demo" / ".hermes", (
        ("tick-v1", {"schema_version": 1, "todo_id": "TODO-5"}, None),
        ("tick-bad", '{"issue_number": 5', None),
        ("tick-forged", {"schema_version": 2, "issue_number": "5\nOK: forged"}, None),
    ))
    mocker.patch(
        "hermes_pipeline.cli._cli_sp.run",
        side_effect=_allow_hermes_registry_skill_check,
    )

    assert _cmd_doctor(args, Config(projects_dir=tmp_path)) == 0
    output = capsys.readouterr().out
    assert "Runs: active=0 delivered=0 abandoned=0 unsupported=3" in output
    for tick in ("tick-v1", "tick-bad", "tick-forged"):
        assert f"tick {tick}: unsupported or malformed registration" in output
    assert "forged" not in output.replace("tick-forged", "")
    assert [line for line in output.splitlines() if line.startswith("OK:")] == [
        line for line in output.splitlines() if line.startswith("OK: schema_version=")
    ]
    assert len([line for line in output.splitlines() if line.startswith("OK:")]) == 1


def test_doctor_warns_when_runs_is_not_a_directory(tmp_path, mocker, capsys, fake_gh):
    args = _create_valid_doctor_project(tmp_path)
    _seed_doctor_github(fake_gh)
    state = tmp_path / "demo" / ".hermes"
    (state / "runs").write_text("not a directory\n")
    mocker.patch(
        "hermes_pipeline.cli._cli_sp.run",
        side_effect=_allow_hermes_registry_skill_check,
    )

    assert _cmd_doctor(args, Config(projects_dir=tmp_path)) == 1
    output = capsys.readouterr().out
    assert f"WARNING: {state / 'runs'} is not a directory" in output
    assert "OK:" not in output


def test_doctor_reports_installed_skill_drift(tmp_path, mocker, capsys):
    args = _create_valid_doctor_project(tmp_path)
    installed = tmp_path / "demo" / ".agents" / "skills" / "todos-manager"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("stale skill\n")
    mocker.patch(
        "hermes_pipeline.cli._cli_sp.run",
        side_effect=_allow_hermes_registry_skill_check,
    )

    assert _cmd_doctor(
        args, Config(projects_dir=tmp_path, prompt_client="codex")
    ) == 1
    output = capsys.readouterr().out
    assert "SKILL DRIFT" in output
    assert "tpo skills install" in output


ORIGIN = ("git", "remote", "get-url", "origin")
API = ("gh", "api", "-H", "Accept: application/vnd.github+json")


def _pin_live_issue(fake_gh, issue, *, body=None, state="open", labels=("tpo:todo",)):
    from tests.gh_fakes import issue_payload

    fake_gh.on(*ORIGIN, stdout=f"https://github.com/{issue.repo}.git\n")
    fake_gh.on(
        *API,
        stdout=__import__("json").dumps(
            issue_payload(
                issue.number,
                title=issue.title,
                body=issue.body if body is None else body,
                state=state,
                labels=labels,
            )
        ),
    )


def test_doctor_active_registration_reports_unsupported_schema(tmp_path, capsys):
    project = tmp_path / "repo"
    state = project / ".hermes"
    (state / "runs" / "tick-1").mkdir(parents=True)
    (state / "current_tick_id.txt").write_text("tick-1\n")
    (state / "runs" / "tick-1" / "registration.json").write_text(
        __import__("json").dumps({"schema_version": 1, "todo_id": "TODO-1"})
    )

    assert _doctor_active_registration(project, state) is False
    output = capsys.readouterr().out
    assert "REGISTRATION UNSUPPORTED: schema_version 1" in output
    assert "finish or abandon this run before upgrading" in output
    assert "Fix" not in output
    assert "DRIFT" not in output


def test_doctor_active_registration_reports_expected_and_actual_hashes(
    tmp_path, mocker, capsys, fake_gh
):
    from tests.gh_fakes import make_issue

    issue = make_issue(1, repo="acme/repo", title="Example", body="### Plan\n\nplan.md\n")
    _pin_live_issue(fake_gh, issue)
    project = tmp_path / "repo"
    worktree = project / ".worktrees" / "todo-1-example"
    state = project / ".hermes"
    worktree.mkdir(parents=True)
    (worktree / "plan.md").write_text("drifted\n")
    (worktree / "TODOS.md").write_text(
        "- [ ] **TODO-1: Example**\n  - **Plan:** plan.md\n"
    )
    (state / "runs" / "tick-1").mkdir(parents=True)
    (state / "current_tick_id.txt").write_text("tick-1\n")
    (state / "runs" / "tick-1" / "registration.json").write_text(
        __import__("json").dumps(
            {
                "schema_version": 2,
                "todo_id": "TODO-1",
                "repository": str(project),
                "worktree": str(worktree),
                "branch": "feat/example",
                "base_sha": "a" * 40,
                "plan_path": "plan.md",
                "plan_hash": "0" * 64,
                "issue_number": 1,
                "issue_url": issue.url,
                "issue_snapshot": issue.snapshot + "tampered\n",
                "selected_entry_hash": issue.entry_hash,
            }
        )
    )

    def run(cmd, **_kwargs):
        if "--git-common-dir" in cmd:
            output = str(project / ".git") + "\n"
        elif cmd[1:3] == ["branch", "--show-current"]:
            output = "feat/example\n"
        elif cmd[1:3] == ["show", "a" * 40 + ":plan.md"]:
            output = "drifted\n"
        elif cmd[1:3] == ["show", "a" * 40 + ":TODOS.md"]:
            output = "- [ ] **TODO-1: Example**\n  - **Plan:** plan.md\n"
        elif cmd[1:3] == ["status", "--porcelain=v1"]:
            output = ""
        else:
            output = "a" * 40 + "\n"
        return MagicMock(returncode=0, stdout=output, stderr="")

    mocker.patch("hermes_pipeline.cli._cli_sp.run", side_effect=run)

    assert _doctor_active_registration(project, state) is False
    output = capsys.readouterr().out
    assert "plan_hash expected=" in output
    assert "actual=" in output
    assert "selected_entry_hash expected=" in output
    assert "REGISTRATION DRIFT: selected_entry_hash" in output
    assert "Issue authority: pinned" in output


def _closeout_project(tmp_path):
    from hermes_pipeline.run_registration import register_pinned_run
    from tests.gh_fakes import make_issue

    project = tmp_path / "repo"
    project.mkdir()
    for command in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
        ("remote", "add", "origin", "https://github.com/acme/repo.git"),
    ):
        _test_sp.run(["git", *command], cwd=project, check=True)
    todos = (
        "## Entries\n\n- [ ] **TODO-1: Example**\n"
        "  - **Plan:** plan.md\n  - **Branch:** feat/example\n"
    )
    (project / "TODOS.md").write_text(todos)
    (project / "plan.md").write_text("# Legacy plan\n")
    _test_sp.run(["git", "add", "."], cwd=project, check=True)
    _test_sp.run(["git", "commit", "-qm", "base"], cwd=project, check=True)
    state = project / ".hermes"
    registration = register_pinned_run(
        project_dir=project,
        state_dir=state,
        tick_id="tick-1",
        selected_issue=make_issue(
            1,
            repo="acme/repo",
            title="Example",
            body="### Plan\n\nplan.md\n\n### Branch\n\nfeat/example\n",
        ),
        repo="acme/repo",
        plan_path="plan.md",
        profile="native-sdd",
        prompt_client="codex",
        assignee="pipeline",
        review_assignee=None,
        step_keys=("phase_4_development",),
    )
    (state / "current_tick_id.txt").write_text("tick-1\n")
    (registration.worktree / "TODOS.md").write_text(
        todos.replace("- [ ]", "- [x]")
        + "  - **Completed:** PR #12, 2026-08-19\n"
    )
    _test_sp.run(["git", "add", "TODOS.md"], cwd=registration.worktree, check=True)
    _test_sp.run(
        ["git", "commit", "-qm", "close todo"],
        cwd=registration.worktree,
        check=True,
    )
    return project, state, registration


def test_doctor_active_registration_accepts_post_closeout_todo_state(
    tmp_path, capsys, fake_gh
):
    from hermes_pipeline.github_issues import issue_from_api
    from tests.gh_fakes import issue_payload

    project, state, registration = _closeout_project(tmp_path)
    issue = issue_from_api(
        issue_payload(1, title="Example", body="### Plan\n\nplan.md\n\n### Branch\n\nfeat/example\n"),
        repo="acme/repo",
    )
    assert issue.entry_hash == registration.selected_entry_hash
    _pin_live_issue(fake_gh, issue)

    assert _doctor_active_registration(project, state) is True
    output = capsys.readouterr().out
    assert "base_sha expected=" in output
    assert "Current lifecycle: head_sha=" in output
    assert "REGISTRATION DRIFT" not in output
    assert "Issue authority: pinned" in output


@pytest.mark.parametrize(
    ("live", "code"),
    [
        ({"body": "### Plan\n\nplan.md\n\n### Branch\n\nfeat/other\n"}, "issue_drift"),
        ({"state": "closed"}, "issue_closed"),
        ({"labels": ("tpo:todo", "tpo:on-hold")}, "issue_on_hold"),
    ],
)
def test_doctor_active_registration_reports_live_issue_drift(
    tmp_path, capsys, fake_gh, live, code
):
    from hermes_pipeline.github_issues import issue_from_api
    from tests.gh_fakes import issue_payload

    project, state, _registration = _closeout_project(tmp_path)
    issue = issue_from_api(
        issue_payload(1, title="Example", body="### Plan\n\nplan.md\n\n### Branch\n\nfeat/example\n"),
        repo="acme/repo",
    )
    _pin_live_issue(fake_gh, issue, **live)

    assert _doctor_active_registration(project, state) is False
    output = capsys.readouterr().out
    assert f"ISSUE DRIFT: {code}" in output
    assert "REGISTRATION DRIFT" not in output
    assert "Fix (tick tick-1):" in output


def test_doctor_active_registration_warns_when_issue_check_unavailable(
    tmp_path, capsys, fake_gh
):
    project, state, _registration = _closeout_project(tmp_path)
    fake_gh.on(*ORIGIN, stdout="https://github.com/acme/repo.git\n")
    fake_gh.on(*API, rc=1, stderr="HTTP 429 rate limit exceeded")

    assert _doctor_active_registration(project, state) is False
    output = capsys.readouterr().out
    assert "WARNING: issue check unavailable (gh_rate_limited)" in output
    assert "Fix (tick tick-1):" in output


def test_doctor_active_registration_reports_malformed_pin_as_drift(
    tmp_path, capsys, fake_gh
):
    import json

    project, state, _registration = _closeout_project(tmp_path)
    fake_gh.on(*ORIGIN, stdout="https://github.com/acme/repo.git\n")
    path = state / "runs" / "tick-1" / "registration.json"
    payload = json.loads(path.read_text())
    del payload["issue_url"]
    path.write_text(json.dumps(payload))

    assert _doctor_active_registration(project, state) is False
    output = capsys.readouterr().out
    assert "REGISTRATION DRIFT: registration is malformed" in output
    assert "WARNING" not in output
    assert "Fix (tick tick-1):" in output


@pytest.mark.parametrize("mutation", [("repository", None), ("repository", 123)])
def test_doctor_active_registration_reports_uninspectable_registration(
    tmp_path, capsys, fake_gh, mutation
):
    import json

    project, state, _registration = _closeout_project(tmp_path)
    path = state / "runs" / "tick-1" / "registration.json"
    payload = json.loads(path.read_text())
    key, value = mutation
    if value is None:
        del payload[key]
    else:
        payload[key] = value
    path.write_text(json.dumps(payload))

    assert _doctor_active_registration(project, state) is False
    output = capsys.readouterr().out
    assert "REGISTRATION DRIFT: active registration could not be inspected" in output
    assert "Fix (tick tick-1):" in output


def test_doctor_active_registration_accepts_picked_none_tick(tmp_path, capsys):
    state = tmp_path / ".hermes"
    state.mkdir()
    (state / "current_tick_id.txt").write_text("tick-none\n")

    assert _doctor_active_registration(tmp_path, state) is True
    output = capsys.readouterr().out
    assert "Current tick tick-none: no registration (no TODO selected)" in output
    assert "DRIFT" not in output and "Fix" not in output


def test_hermes_registry_prerequisite_requires_exact_enabled_skill_name(mocker):
    """A similarly named skill must not satisfy the required Hermes skill ID."""
    mocker.patch(
        "hermes_pipeline.cli._cli_sp.run",
        return_value=MagicMock(
            returncode=0,
            stderr="",
            stdout=(
                "Installed Skills (enabled only)\n"
                "Name                       Category  Source  Trust  Status\n"
                "ai-coding-agents-helper    local     local   local  enabled\n"
                "0 hub-installed, 0 builtin, 1 local - 1 enabled shown\n"
            ),
        ),
    )

    verified, detail = _verify_hermes_skill_registry_prerequisite(
        assignee="default",
        skill_id="ai-coding-agents",
    )

    assert verified is False
    assert "not enabled" in detail


def test_hermes_registry_prerequisite_timeout_is_actionable(mocker):
    import subprocess

    mocker.patch(
        "hermes_pipeline.cli._cli_sp.run",
        side_effect=subprocess.TimeoutExpired(["hermes"], 10),
    )

    verified, detail = _verify_hermes_skill_registry_prerequisite(
        assignee="pipeline",
        skill_id="ai-coding-agents",
    )

    assert verified is False
    assert detail == "`hermes -p pipeline skills list --enabled-only` timed out."


def test_hermes_registry_prerequisite_failure_hides_raw_stderr(mocker):
    secret = "Authorization: Bearer provider-secret"
    mocker.patch(
        "hermes_pipeline.cli._cli_sp.run",
        return_value=MagicMock(returncode=1, stderr=secret, stdout=""),
    )

    verified, detail = _verify_hermes_skill_registry_prerequisite(
        assignee="default",
        skill_id="ai-coding-agents",
    )

    assert verified is False
    assert detail == "`hermes skills list --enabled-only` failed (rc=1)."
    assert secret not in detail


class TestBuildParserInit:
    def test_init_help(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["init", "--help"])

    def test_init_parses_project_and_force(self):
        parser = build_parser()
        args = parser.parse_args(["init", "demo", "--force"])
        assert args.command == "init"
        assert args.project == "demo"
        assert args.force is True

    def test_init_force_defaults_false(self):
        parser = build_parser()
        args = parser.parse_args(["init", "demo"])
        assert args.force is False


class TestCmdInit:
    def test_init_unknown_project_returns_2(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        config = Config(projects_dir=projects_dir)
        result = _cmd_init(FakeArgs(project="nope", force=False), config)
        assert result == 2

    def test_init_writes_default_contract(self, tmp_path, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        config = Config(projects_dir=projects_dir)

        result = _cmd_init(FakeArgs(project="demo", force=False), config)

        assert result == 0
        contract = projects_dir / "demo" / ".hermes" / "pipeline.toml"
        assert contract.is_file()
        assert "Wrote pipeline execution contract" in capsys.readouterr().out

    def test_init_idempotent_without_force(self, tmp_path, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        config = Config(projects_dir=projects_dir)

        _cmd_init(FakeArgs(project="demo", force=False), config)
        contract = projects_dir / "demo" / ".hermes" / "pipeline.toml"
        contract.write_text('schema_version = 2\nassignee = "custom"\n')

        result = _cmd_init(FakeArgs(project="demo", force=False), config)

        assert result == 0
        assert "already exists" in capsys.readouterr().out
        assert 'assignee = "custom"' in contract.read_text()

    def test_init_force_overwrites(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        config = Config(projects_dir=projects_dir)

        _cmd_init(FakeArgs(project="demo", force=False), config)
        contract = projects_dir / "demo" / ".hermes" / "pipeline.toml"
        contract.write_text('schema_version = 2\nassignee = "custom"\n')

        result = _cmd_init(FakeArgs(project="demo", force=True), config)

        assert result == 0
        assert 'assignee = "custom"' not in contract.read_text()
        assert f"schema_version = {CONTRACT_SCHEMA_VERSION}" in contract.read_text()


class TestInitAssignee:
    def test_init_assignee_parser(self):
        parser = build_parser()
        args = parser.parse_args(["init", "demo", "--assignee", "pipeline"])
        assert args.assignee == "pipeline"

    def test_init_assignee_defaults_to_none(self):
        parser = build_parser()
        args = parser.parse_args(["init", "demo"])
        assert args.assignee is None

    def test_init_writes_assignee_flag_value(self, tmp_path, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        config = Config(projects_dir=projects_dir)

        result = _cmd_init(FakeArgs(project="demo", force=False, assignee="pipeline"), config)

        assert result == 0
        contract = projects_dir / "demo" / ".hermes" / "pipeline.toml"
        assert 'assignee = "pipeline"' in contract.read_text()

    def test_init_without_assignee_uses_default(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        config = Config(projects_dir=projects_dir)

        result = _cmd_init(FakeArgs(project="demo", force=False, assignee=None), config)

        assert result == 0
        contract = projects_dir / "demo" / ".hermes" / "pipeline.toml"
        assert 'assignee = "default"' in contract.read_text()


class TestInitProfile:
    def test_init_profile_parser_defaults_to_gstack(self):
        parser = build_parser()
        args = parser.parse_args(["init", "demo"])
        assert args.profile == "gstack"

    def test_init_profile_parser_accepts_flag(self):
        parser = build_parser()
        args = parser.parse_args(["init", "demo", "--profile", "agent-skills"])
        assert args.profile == "agent-skills"

    def test_init_writes_selected_profile(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        config = Config(projects_dir=projects_dir)

        result = _cmd_init(
            FakeArgs(project="demo", force=False, assignee=None, profile="agent-skills"), config
        )

        assert result == 0
        contract = projects_dir / "demo" / ".hermes" / "pipeline.toml"
        assert 'profile = "agent-skills"' in contract.read_text()

    def test_init_without_profile_flag_defaults_to_gstack(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        config = Config(projects_dir=projects_dir)

        result = _cmd_init(FakeArgs(project="demo", force=False, assignee=None), config)

        assert result == 0
        contract = projects_dir / "demo" / ".hermes" / "pipeline.toml"
        assert 'profile = "gstack"' in contract.read_text()

    def test_init_unknown_profile_returns_error(self, tmp_path, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        config = Config(projects_dir=projects_dir)

        result = _cmd_init(
            FakeArgs(project="demo", force=False, assignee=None, profile="bogus-profile"), config
        )

        assert result == 2
        contract = projects_dir / "demo" / ".hermes" / "pipeline.toml"
        assert not contract.exists()
        assert "bogus-profile" in capsys.readouterr().out

    def test_init_capabilities_computed_from_selected_profile(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        config = Config(projects_dir=projects_dir)

        _cmd_init(
            FakeArgs(project="demo", force=False, assignee=None, profile="agent-skills"), config
        )

        contract = projects_dir / "demo" / ".hermes" / "pipeline.toml"
        text = contract.read_text()
        # agent-skills profile's non-gate phases only use Read/Write/Edit/Bash
        assert '"Read"' in text
        assert '"Bash"' in text


class TestInstallProfileParser:
    def test_install_profile_parses_force(self):
        parser = build_parser()
        args = parser.parse_args(["install-profile", "--force"])
        assert args.command == "install-profile"
        assert args.force is True

    def test_install_profile_force_defaults_false(self):
        parser = build_parser()
        args = parser.parse_args(["install-profile"])
        assert args.force is False


class TestBuildParserDoctor:
    def test_doctor_help(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["doctor", "--help"])

    def test_doctor_parses_project(self):
        parser = build_parser()
        args = parser.parse_args(["doctor", "demo"])
        assert args.command == "doctor"
        assert args.project == "demo"


class TestCmdDoctor:
    def test_doctor_unknown_project_returns_2(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        config = Config(projects_dir=projects_dir)
        result = _cmd_doctor(FakeArgs(project="nope"), config)
        assert result == 2

    def test_doctor_missing_contract_returns_2(self, tmp_path, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 2
        assert "tpo init" in capsys.readouterr().out

    def test_doctor_invalid_contract_returns_2(self, tmp_path, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text("schema_version = 99\n")
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 2
        assert "INVALID" in capsys.readouterr().out

    def test_doctor_clean_returns_0(self, tmp_path, mocker, capsys, fake_gh):
        _seed_doctor_github(fake_gh)
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 2\ncapabilities = ["Read", "Write"]\n'
        )
        mocker.patch(
            "hermes_pipeline.cli.load_phases",
            return_value=[Phase(phase_key="p1", name="P1", tools="Read,Write")],
        )
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run",
            side_effect=_allow_hermes_registry_skill_check,
        )
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 0
        output = capsys.readouterr().out
        assert "OK" in output
        assert "Plan readiness: eligible=0 blocked=0\n" in output

    def test_doctor_drift_returns_1(self, tmp_path, mocker, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 2\ncapabilities = ["Read"]\n'
        )
        mocker.patch(
            "hermes_pipeline.cli.load_phases",
            return_value=[Phase(phase_key="p1", name="P1", tools="Read,Write,Bash")],
        )
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 1
        out = capsys.readouterr().out
        assert "DRIFT" in out
        assert "Write" in out and "Bash" in out


class TestDoctorProfileAware:
    def test_doctor_reports_global_prompt_client_scope(
        self, monkeypatch, tmp_path, capsys, fake_gh
    ):
        _seed_doctor_github(fake_gh)
        args = _create_valid_doctor_project(tmp_path)
        monkeypatch.setattr(
            "hermes_pipeline.cli._cli_sp.run",
            _allow_hermes_registry_skill_check,
        )

        assert (
            _cmd_doctor(
                args,
                Config(projects_dir=tmp_path, prompt_client="codex"),
            )
            == 0
        )

        output = capsys.readouterr().out
        assert (
            "prompt client: codex (global for all projects under projects_dir)"
            in output
        )
        assert "separate project roots" in output
        assert "TODO-42" in output

    @pytest.mark.parametrize(
        ("prompt_client", "discovery_root", "invocation"),
        [
            ("claude", ".claude/skills", "/autoplan"),
            ("codex", ".codex/skills", "$autoplan"),
        ],
    )
    def test_doctor_reports_conditional_prerequisites_without_local_failure(
        self,
        monkeypatch,
        tmp_path,
        capsys,
        prompt_client,
        discovery_root,
        invocation,
        fake_gh,
    ):
        _seed_doctor_github(fake_gh)
        args = _create_valid_doctor_project(tmp_path)
        monkeypatch.setattr(
            "hermes_pipeline.cli._cli_sp.run",
            _allow_hermes_registry_skill_check,
        )

        assert (
            _cmd_doctor(
                args,
                Config(projects_dir=tmp_path, prompt_client=prompt_client),
            )
            == 0
        )

        output = capsys.readouterr().out
        assert "Conditional" in output
        assert discovery_root in output
        assert invocation in output
        assert "worker provisioning is required" in output

    def test_doctor_marks_unverified_profile_unsupported(
        self, monkeypatch, tmp_path, capsys
    ):
        args = _create_valid_doctor_project(tmp_path, profile="agent-skills")
        monkeypatch.setattr(
            "hermes_pipeline.cli._cli_sp.run",
            lambda *args, **kwargs: pytest.fail(
                "doctor must not inspect a remote worker for external skills"
            ),
        )

        assert (
            _cmd_doctor(
                args,
                Config(projects_dir=tmp_path, prompt_client="codex"),
            )
            == 2
        )

        output = capsys.readouterr().out
        assert "Unverified" in output
        assert "not advertised as supported" in output
        assert "UNSUPPORTED" in output
        assert "OK:" not in output

    def test_doctor_loads_phases_from_contract_profile(self, tmp_path, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 2\n'
            'capabilities = ["Read", "Write", "Bash"]\n'
            'profile = "agent-skills"\n'
        )
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        out = capsys.readouterr().out
        assert result == 1
        assert "Edit" in out

    def test_doctor_unknown_profile_returns_2(self, tmp_path, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 2\n'
            'capabilities = ["Read", "Write", "Bash"]\n'
            'profile = "nonexistent-profile"\n'
        )
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 2
        assert "MISSING" in capsys.readouterr().out

    def test_doctor_malformed_profile_yaml_returns_2(self, tmp_path, mocker, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 2\ncapabilities = ["Read", "Write", "Bash"]\n'
        )
        mocker.patch(
            "hermes_pipeline.cli.load_phases",
            side_effect=ValueError("malformed phases.yaml"),
        )
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 2
        assert "INVALID" in capsys.readouterr().out

    def test_doctor_ok_message_includes_profile(self, tmp_path, mocker, capsys, fake_gh):
        _seed_doctor_github(fake_gh)
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 2\ncapabilities = ["Read", "Write"]\nprofile = "gstack"\n'
        )
        mocker.patch(
            "hermes_pipeline.cli.load_phases",
            return_value=[Phase(phase_key="p1", name="P1", tools="Read,Write")],
        )
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run",
            side_effect=_allow_hermes_registry_skill_check,
        )
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 0
        assert "profile=gstack" in capsys.readouterr().out


class TestDoctorMissingProfile:
    def test_doctor_checks_profile_for_non_default_assignee(self, tmp_path, mocker, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\ncapabilities = ["Read", "Write", "Bash"]\n'
        )
        mocker.patch(
            "hermes_pipeline.cli.load_phases",
            return_value=[Phase(phase_key="p1", name="P1", tools="Read,Write,Bash")],
        )
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run",
            return_value=MagicMock(returncode=1, stderr="profile not found", stdout=""),
        )
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 2
        out = capsys.readouterr().out
        assert "pipeline" in out.lower() or "profile" in out.lower()

    def test_doctor_skips_profile_show_for_default_assignee(
        self, tmp_path, mocker, capsys,
        fake_gh,
    ):
        _seed_doctor_github(fake_gh)
        """Default assignee skips profile show but still checks the skill registry."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 2\ncapabilities = ["Read", "Write", "Bash"]\n'
        )
        call_count = {"n": 0}
        original_run = _test_sp.run
        def tracking_run(*a, **kw):
            call_count["n"] += 1
            cmd = a[0] if a else kw.get("args", [])
            if "profile" in cmd:
                return MagicMock(returncode=1, stderr="profile not found", stdout="")
            if cmd == ["hermes", "skills", "list", "--enabled-only"]:
                return MagicMock(returncode=0, stderr="", stdout="ai-coding-agents\n")
            if cmd == ["hermes", "--version"]:
                return MagicMock(returncode=0, stderr="", stdout="Hermes Agent v0.19.0\n")
            return original_run(*a, **kw)
        mocker.patch("hermes_pipeline.cli._cli_sp.run", side_effect=tracking_run)
        mocker.patch(
            "hermes_pipeline.cli.load_phases",
            return_value=[Phase(phase_key="p1", name="P1", tools="Read,Write,Bash")],
        )
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 0
        assert call_count["n"] == 2

    def test_doctor_profile_check_success_returns_0(self, tmp_path, mocker, capsys, fake_gh):
        _seed_doctor_github(fake_gh)
        """Non-default assignee whose profile IS installed should pass clean."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\ncapabilities = ["Read", "Write", "Bash"]\n'
        )
        mocker.patch(
            "hermes_pipeline.cli.load_phases",
            return_value=[Phase(phase_key="p1", name="P1", tools="Read,Write,Bash")],
        )
        def run(cmd, **_kwargs):
            if cmd == ["hermes", "profile", "show", "pipeline"]:
                return MagicMock(returncode=0, stderr="", stdout="")
            if cmd == ["hermes", "-p", "pipeline", "skills", "list", "--enabled-only"]:
                return MagicMock(returncode=0, stderr="", stdout="ai-coding-agents\n")
            if cmd == ["hermes", "--version"]:
                return MagicMock(returncode=0, stderr="", stdout="Hermes Agent v0.19.0\n")
            raise AssertionError(f"unexpected command: {cmd}")

        mocker.patch("hermes_pipeline.cli._cli_sp.run", side_effect=run)
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 0
        assert "OK" in capsys.readouterr().out

    def test_doctor_checks_hermes_skill_registry_prerequisite(
        self, tmp_path, mocker, capsys,
        fake_gh,
    ):
        _seed_doctor_github(fake_gh)
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\n'
            'capabilities = ["Read", "Write", "Edit", "Bash"]\n'
        )

        def run(cmd, **_kwargs):
            if cmd == ["hermes", "profile", "show", "pipeline"]:
                return MagicMock(returncode=0, stderr="", stdout="")
            if cmd == ["hermes", "-p", "pipeline", "skills", "list", "--enabled-only"]:
                return MagicMock(returncode=0, stderr="", stdout="ai-coding-agents\n")
            if cmd == ["hermes", "--version"]:
                return MagicMock(returncode=0, stderr="", stdout="Hermes Agent v0.19.0\n")
            raise AssertionError(f"unexpected command: {cmd}")

        mocker.patch("hermes_pipeline.cli._cli_sp.run", side_effect=run)
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 0
        out = capsys.readouterr().out
        assert "ai-coding-agents" in out
        assert "verified locally" in out

    def test_doctor_missing_hermes_skill_registry_prerequisite_returns_2(
        self, tmp_path, mocker, capsys
    ):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\n'
            'capabilities = ["Read", "Write", "Edit", "Bash"]\n'
        )

        def run(cmd, **_kwargs):
            if cmd == ["hermes", "profile", "show", "pipeline"]:
                return MagicMock(returncode=0, stderr="", stdout="")
            if cmd == ["hermes", "-p", "pipeline", "skills", "list", "--enabled-only"]:
                return MagicMock(returncode=0, stderr="", stdout="")
            raise AssertionError(f"unexpected command: {cmd}")

        mocker.patch("hermes_pipeline.cli._cli_sp.run", side_effect=run)
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 2
        out = capsys.readouterr().out
        assert "MISSING" in out
        assert "ai-coding-agents" in out

    def test_doctor_missing_default_hermes_skill_registry_returns_2(
        self, tmp_path, mocker, capsys
    ):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 2\ncapabilities = ["Read", "Write", "Edit", "Bash"]\n'
        )

        def run(cmd, **_kwargs):
            if cmd == ["hermes", "skills", "list", "--enabled-only"]:
                return MagicMock(returncode=0, stderr="", stdout="")
            raise AssertionError(f"unexpected command: {cmd}")

        mocker.patch("hermes_pipeline.cli._cli_sp.run", side_effect=run)
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 2
        out = capsys.readouterr().out
        assert "MISSING" in out
        assert "ai-coding-agents" in out

    def test_doctor_hermes_not_on_path_returns_2(self, tmp_path, mocker, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\ncapabilities = ["Read", "Write", "Bash"]\n'
        )
        mocker.patch(
            "hermes_pipeline.cli.load_phases",
            return_value=[Phase(phase_key="p1", name="P1", tools="Read,Write,Bash")],
        )
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run",
            side_effect=FileNotFoundError("hermes"),
        )
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 2
        assert "not on PATH" in capsys.readouterr().out


class TestCmdInitPatchErrors:
    def test_init_malformed_existing_toml_returns_1(self, tmp_path, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text("not valid = toml =")
        config = Config(projects_dir=projects_dir)

        result = _cmd_init(FakeArgs(project="demo", force=False, assignee="pipeline"), config)

        assert result == 1

    def test_init_missing_schema_version_returns_1(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text('assignee = "default"\n')
        config = Config(projects_dir=projects_dir)

        result = _cmd_init(FakeArgs(project="demo", force=False, assignee="pipeline"), config)

        assert result == 1


class TestRenderContractToml:
    def test_render_contract_toml_roundtrips(self):
        import tomllib

        contract = PipelineContract(
            schema_version=1, assignee="pipeline", capabilities=("Read", "Write")
        )
        rendered = _render_contract_toml(contract)
        parsed = tomllib.loads(rendered)

        assert parsed["schema_version"] == 1
        assert parsed["assignee"] == "pipeline"
        assert parsed["capabilities"] == ["Read", "Write"]


class TestBundledProfileDir:
    def test_bundled_profile_dir_resolves_soul_md(self):
        profile_dir = bundled_profile_dir()
        assert (profile_dir / "SOUL.md").is_file()


class TestCmdInstallProfile:
    def test_install_profile_happy_path_returns_0(self, mocker, tmp_path, capsys):
        show_out = f"Profile: pipeline\nPath:    {tmp_path}\n"
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run",
            side_effect=[
                MagicMock(returncode=0, stderr="", stdout=""),  # create
                MagicMock(returncode=0, stderr="", stdout=show_out),  # show
            ],
        )

        result = _cmd_install_profile(FakeArgs(force=False), config=None)

        out = capsys.readouterr().out
        assert result == 0
        assert "installed successfully" in out
        assert (tmp_path / "SOUL.md").is_file()

    def test_install_profile_force_deletes_existing_first(self, mocker, tmp_path):
        show_out = f"Profile: pipeline\nPath:    {tmp_path}\n"
        run_mock = mocker.patch(
            "hermes_pipeline.cli._cli_sp.run",
            side_effect=[
                MagicMock(returncode=0, stderr="", stdout=""),  # delete
                MagicMock(returncode=0, stderr="", stdout=""),  # create
                MagicMock(returncode=0, stderr="", stdout=show_out),  # show
            ],
        )

        _cmd_install_profile(FakeArgs(force=True), config=None)

        delete_call = run_mock.call_args_list[0]
        assert delete_call.args[0][:3] == ["hermes", "profile", "delete"]
        create_call = run_mock.call_args_list[1]
        assert create_call.args[0][:3] == ["hermes", "profile", "create"]

    def test_install_profile_force_delete_fails_returns_2(self, mocker, capsys):
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run",
            side_effect=[
                MagicMock(returncode=1, stderr="", stdout=""),  # delete fails, no stderr
            ],
        )

        result = _cmd_install_profile(FakeArgs(force=True), config=None)

        assert result == 2
        out = capsys.readouterr().out
        assert "Problem: `hermes profile delete` failed" in out

    def test_install_profile_soul_missing_returns_1(self, mocker, tmp_path, caplog):
        mocker.patch(
            "hermes_pipeline.contract.bundled_profile_dir", return_value=tmp_path / "nonexistent"
        )

        result = _cmd_install_profile(FakeArgs(force=False), config=None)

        assert result == 1

    def test_install_profile_hermes_not_on_path_returns_2(self, mocker, capsys):
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run",
            side_effect=FileNotFoundError("hermes"),
        )

        result = _cmd_install_profile(FakeArgs(force=False), config=None)

        assert result == 2
        assert "not found" in capsys.readouterr().out

    def test_install_profile_create_command_fails_returns_2(self, mocker, capsys):
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run",
            return_value=MagicMock(returncode=1, stderr="boom", stdout=""),
        )

        result = _cmd_install_profile(FakeArgs(force=False), config=None)

        assert result == 2
        assert "failed" in capsys.readouterr().out

    def test_install_profile_verify_fails_returns_1(self, mocker, capsys):
        create_ok = MagicMock(returncode=0, stderr="", stdout="")
        show_fail = MagicMock(returncode=1, stderr="", stdout="")
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run", side_effect=[create_ok, show_fail]
        )

        result = _cmd_install_profile(FakeArgs(force=False), config=None)

        assert result == 1
        assert "created but" in capsys.readouterr().out

    def test_install_profile_path_missing_from_show_output_returns_1(self, mocker, capsys):
        create_ok = MagicMock(returncode=0, stderr="", stdout="")
        show_no_path = MagicMock(returncode=0, stderr="", stdout="Profile: pipeline\n")
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run", side_effect=[create_ok, show_no_path]
        )

        result = _cmd_install_profile(FakeArgs(force=False), config=None)

        assert result == 1
        assert "Could not determine the profile path" in capsys.readouterr().out

    def test_install_profile_soul_copy_failure_returns_1(self, mocker, tmp_path, capsys):
        show_out = f"Profile: pipeline\nPath:    {tmp_path}\n"
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run",
            side_effect=[
                MagicMock(returncode=0, stderr="", stdout=""),  # create
                MagicMock(returncode=0, stderr="", stdout=show_out),  # show
            ],
        )
        mocker.patch("hermes_pipeline.cli.shutil.copyfile", side_effect=OSError("disk full"))

        result = _cmd_install_profile(FakeArgs(force=False), config=None)

        assert result == 1
        assert "Failed to copy pipeline SOUL.md" in capsys.readouterr().out

    def test_install_profile_force_delete_hermes_not_found_returns_2(self, mocker, capsys):
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run",
            side_effect=FileNotFoundError("hermes"),
        )

        result = _cmd_install_profile(FakeArgs(force=True), config=None)

        assert result == 2
        assert "not found" in capsys.readouterr().out


class TestTodosComplete:
    """``tpo todos complete <project> --todo N --pr N`` drives the issue-close state machine."""

    @pytest.fixture(autouse=True)
    def _merged_pr(self, mocker):
        self.pr_view = mocker.patch(
            "hermes_pipeline.todos_completion._pr_view",
            return_value={"state": "MERGED", "url": "https://github.com/acme/repo/pull/7"},
        )

    def _project(self, tmp_path, fake_gh, *, state="open", labels=("tpo:todo", "tpo:in-progress"),
                 comments=(), state_reason=None):
        import json

        from tests.gh_fakes import issue_payload

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        remote = {"state": state, "labels": list(labels), "comments": list(comments), "writes": []}
        fake_gh.on(*ORIGIN, stdout="https://github.com/acme/repo.git\n")
        fake_gh.on(*API, "repos/acme/repo/issues/5", handler=lambda argv: (
            0, json.dumps(issue_payload(
                5, state=remote["state"], labels=remote["labels"], state_reason=state_reason,
            )), ""
        ))
        fake_gh.on(*API, "--paginate", "--slurp", "repos/acme/repo/issues/5/comments",
                   handler=lambda argv: (0, json.dumps([[{"body": b} for b in remote["comments"]]]), ""))

        def comment(argv):
            with open(argv[argv.index("--body-file") + 1]) as handle:
                remote["comments"].append(handle.read())
            remote["writes"].append("comment")
            return 0, "", ""

        def close(argv):
            remote.update(state="closed")
            remote["writes"].append("close")
            return 0, "", ""

        def edit(argv):
            remote["labels"].remove("tpo:in-progress")
            remote["writes"].append("edit")
            return 0, "", ""

        fake_gh.on("gh", "issue", "comment", handler=comment)
        fake_gh.on("gh", "issue", "close", handler=close)
        fake_gh.on("gh", "issue", "edit", handler=edit)
        return Config(projects_dir=projects_dir), remote

    def test_parser_takes_project_positional_and_todo_id_forms(self):
        args = build_parser().parse_args(["todos", "complete", "demo", "--todo", "TODO-5", "--pr", "7"])
        assert (args.project, args.todo, args.pr, args.date) == ("demo", 5, 7, None)
        args = build_parser().parse_args(["todos", "complete", "demo", "--todo", "5", "--pr", "7",
                                          "--date", "2026-08-29"])
        assert (args.todo, args.date) == (5, "2026-08-29")

    def test_parser_rejects_non_calendar_date(self):
        with pytest.raises(SystemExit) as excinfo:
            build_parser().parse_args(["todos", "complete", "demo", "--todo", "5", "--pr", "7",
                                       "--date", "2026-02-31"])
        assert excinfo.value.code == 2

    def test_parser_rejects_legacy_project_root_flag(self):
        with pytest.raises(SystemExit) as excinfo:
            build_parser().parse_args(["todos", "complete", "--project-root", ".", "--todo", "5", "--pr", "7"])
        assert excinfo.value.code == 2

    def test_completes_issue_with_manual_marker(self, tmp_path, fake_gh, capsys):
        from hermes_pipeline.cli import _cmd_todos_complete

        config, remote = self._project(tmp_path, fake_gh)
        args = build_parser().parse_args(["todos", "complete", "demo", "--todo", "5", "--pr", "7",
                                          "--date", "2026-08-29"])

        assert _cmd_todos_complete(args, config) == 0
        assert capsys.readouterr().out.strip() == "completed"
        self.pr_view.assert_called_once()
        assert self.pr_view.call_args.args[1] == "https://github.com/acme/repo/pull/7"
        assert remote["writes"] == ["comment", "close", "edit"]
        assert remote["comments"] == [
            "Completed: PR #7 https://github.com/acme/repo/pull/7, 2026-08-29\n"
            "<!-- tpo-completed tick=manual pr=7 -->"
        ]
        assert not (tmp_path / "projects" / "demo" / ".hermes" / "runs").exists()

    def test_rerun_is_idempotent(self, tmp_path, fake_gh, capsys):
        from hermes_pipeline.cli import _cmd_todos_complete

        config, remote = self._project(
            tmp_path, fake_gh, state="closed", labels=("tpo:todo",),
            comments=["<!-- tpo-completed tick=manual pr=7 -->"],
        )
        args = build_parser().parse_args(["todos", "complete", "demo", "--todo", "5", "--pr", "7"])
        assert _cmd_todos_complete(args, config) == 0
        assert capsys.readouterr().out.strip() == "completed"
        assert remote["writes"] == []

    def test_propagation_lag_exits_3_pending(self, tmp_path, fake_gh, capsys):
        from hermes_pipeline.cli import _cmd_todos_complete

        config, remote = self._project(tmp_path, fake_gh)
        fake_gh.on("gh", "issue", "close")  # accepted, but the read side still says open
        args = build_parser().parse_args(["todos", "complete", "demo", "--todo", "5", "--pr", "7"])
        assert _cmd_todos_complete(args, config) == 3
        assert capsys.readouterr().out.strip() == "pending"
        assert remote["state"] == "open"

    @pytest.mark.parametrize("state", ["OPEN", "CLOSED"])
    def test_unmerged_pr_is_refused_unless_forced(self, tmp_path, fake_gh, capsys, state):
        from hermes_pipeline.cli import _cmd_todos_complete

        config, remote = self._project(tmp_path, fake_gh)
        self.pr_view.return_value = {"state": state, "url": "https://github.com/acme/repo/pull/7"}
        args = build_parser().parse_args(["todos", "complete", "demo", "--todo", "5", "--pr", "7"])
        assert _cmd_todos_complete(args, config) == 2
        assert "not merged" in capsys.readouterr().err
        assert remote["writes"] == []

        forced = build_parser().parse_args(["todos", "complete", "demo", "--todo", "5", "--pr", "7", "--force"])
        assert _cmd_todos_complete(forced, config) == 0
        assert remote["state"] == "closed"

    def test_pr_lookup_failure_exits_1(self, tmp_path, fake_gh, capsys):
        from hermes_pipeline.cli import _cmd_todos_complete
        from hermes_pipeline.result_contract import ResultContractError

        config, remote = self._project(tmp_path, fake_gh)
        self.pr_view.side_effect = ResultContractError("pr_missing")
        args = build_parser().parse_args(["todos", "complete", "demo", "--todo", "5", "--pr", "7"])
        assert _cmd_todos_complete(args, config) == 1
        assert "pr_missing" in capsys.readouterr().err
        assert remote["writes"] == []

    def test_active_run_is_refused_unless_forced(self, tmp_path, fake_gh, capsys):
        import json

        from hermes_pipeline.cli import _cmd_todos_complete

        config, remote = self._project(tmp_path, fake_gh)
        run_dir = tmp_path / "projects" / "demo" / ".hermes" / "runs" / "01HA6PH2V0ZJ7GK0S39D243TQX"
        run_dir.mkdir(parents=True)
        (run_dir / "registration.json").write_text(json.dumps({"schema_version": 2, "issue_number": 5}))
        args = build_parser().parse_args(["todos", "complete", "demo", "--todo", "5", "--pr", "7"])
        assert _cmd_todos_complete(args, config) == 2
        err = capsys.readouterr().err
        assert "run 01HA6PH2V0ZJ7GK0S39D243TQX is active" in err and "--force" in err
        assert remote["writes"] == []

        forced = build_parser().parse_args(["todos", "complete", "demo", "--todo", "5", "--pr", "7", "--force"])
        assert _cmd_todos_complete(forced, config) == 0
        assert remote["state"] == "closed"
        assert not (run_dir / "issue-closed").exists()

    def test_active_run_message_names_only_matching_active_ticks(self, tmp_path, fake_gh, capsys):
        import json

        from hermes_pipeline.cli import _cmd_todos_complete

        config, _remote = self._project(tmp_path, fake_gh)
        runs = tmp_path / "projects" / "demo" / ".hermes" / "runs"
        for tick, number, closed in (("tick-5", 5, False), ("tick-55", 55, False), ("tick-old", 5, True)):
            run = runs / tick
            run.mkdir(parents=True)
            (run / "registration.json").write_text(json.dumps({"schema_version": 2, "issue_number": number}))
            if closed:
                (run / "issue-closed").write_text("")
        broken = runs / "tick-broken"
        broken.mkdir()
        (broken / "registration.json").write_text('{"issue_number": 5')
        args = build_parser().parse_args(["todos", "complete", "demo", "--todo", "5", "--pr", "7"])

        assert _cmd_todos_complete(args, config) == 2
        err = capsys.readouterr().err
        assert "Error: run tick-5 is active for TODO-5" in err
        assert "tick-55" not in err and "tick-old" not in err and "tick-broken" not in err

    def test_not_planned_issue_exits_1(self, tmp_path, fake_gh, capsys):
        from hermes_pipeline.cli import _cmd_todos_complete

        config, remote = self._project(tmp_path, fake_gh, state="closed", state_reason="not_planned")
        args = build_parser().parse_args(["todos", "complete", "demo", "--todo", "5", "--pr", "7"])
        assert _cmd_todos_complete(args, config) == 1
        assert "issue_not_planned" in capsys.readouterr().err
        assert remote["writes"] == []

    def test_conflicting_completion_exits_1_unless_forced(self, tmp_path, fake_gh, capsys):
        from hermes_pipeline.cli import _cmd_todos_complete

        config, remote = self._project(tmp_path, fake_gh, comments=["<!-- tpo-completed tick=manual pr=6 -->"])
        args = build_parser().parse_args(["todos", "complete", "demo", "--todo", "5", "--pr", "7"])
        assert _cmd_todos_complete(args, config) == 1
        assert "completion_conflict" in capsys.readouterr().err
        assert remote["writes"] == []

        forced = build_parser().parse_args(["todos", "complete", "demo", "--todo", "5", "--pr", "7", "--force"])
        assert _cmd_todos_complete(forced, config) == 0
        assert remote["writes"] == ["comment", "close", "edit"]

    def test_gh_failure_exits_1_with_code(self, tmp_path, fake_gh, capsys):
        from hermes_pipeline.cli import _cmd_todos_complete

        config, _remote = self._project(tmp_path, fake_gh)
        fake_gh.on("gh", "issue", "close", rc=1, stderr="gh auth login required")
        args = build_parser().parse_args(["todos", "complete", "demo", "--todo", "5", "--pr", "7"])
        assert _cmd_todos_complete(args, config) == 1
        assert "gh_auth" in capsys.readouterr().err

    def test_unknown_project_exits_2(self, tmp_path, fake_gh):
        from hermes_pipeline.cli import _cmd_todos_complete

        config, _remote = self._project(tmp_path, fake_gh)
        args = build_parser().parse_args(["todos", "complete", "nope", "--todo", "5", "--pr", "7"])
        assert _cmd_todos_complete(args, config) == 2

    def test_main_routes_todos_through_runtime_config(self, tmp_path, fake_gh, mocker, capsys):
        from hermes_pipeline import cli

        config, remote = self._project(tmp_path, fake_gh)
        mocker.patch.object(cli.Config, "from_env", return_value=config)
        mocker.patch.object(cli, "configure_logging")
        assert cli.main(["todos", "complete", "demo", "--todo", "5", "--pr", "7"]) == 0
        assert capsys.readouterr().out.strip() == "completed"
        assert remote["state"] == "closed"


class TestPriorTickId:
    @pytest.mark.parametrize("value", ["01HA6PH2V0ZJ7GK0S39D243TQX", "20260829120000123456", "01PRIOR"])
    def test_well_formed_prior_tick_id_is_returned(self, tmp_path, value):
        from hermes_pipeline.cli import _read_prior_tick_id

        (tmp_path / "current_tick_id.txt").write_text(value + "\n")
        assert _read_prior_tick_id(tmp_path) == value

    @pytest.mark.parametrize("value", ["../evil", "tick-1", "A" * 27, "", "01hb6"])
    def test_malformed_prior_tick_id_is_treated_as_cold_start(self, tmp_path, caplog, value):
        from hermes_pipeline.cli import _read_prior_tick_id

        (tmp_path / "current_tick_id.txt").write_text(value)
        with caplog.at_level("ERROR", logger="hermes_pipeline.cli"):
            assert _read_prior_tick_id(tmp_path) is None
        assert any(r.levelname == "ERROR" and "tick id" in r.getMessage() for r in caplog.records)
