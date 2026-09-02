"""Tests for cli.py — test subcommand."""


import logging
from types import SimpleNamespace

import pytest

from hermes_pipeline.cli import _cmd_test, build_parser, main


class TestBuildParser:
    """Test argument parser construction."""

    def test_build_parser_help(self):
        """Parser shows help for main command."""
        parser = build_parser()
        # Parser should have subcommands
        assert parser.prog == "tpo"


def test_main_no_command(tmp_path):
    """main() with no command shows help."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    # Set config file for projects_dir.
    import os
    config_file = tmp_path / "config.yaml"
    config_file.write_text(f"projects_dir: {projects_dir!s}\n")
    os.environ["TPO_CONFIG_FILE"] = str(config_file)

    try:
        import io
        import sys
        old_stdout = sys.stdout
        old_argv = sys.argv
        sys.stdout = io.StringIO()
        sys.argv = ['tpo']
        try:
            result = main(None)
        finally:
            sys.stdout = old_stdout
            sys.argv = old_argv
        # Should return 0 (help is not an error)
        assert result == 0
    finally:
        for key in ["TPO_CONFIG_FILE"]:
            os.environ.pop(key, None)


# --- Test subcommand parsing ---

def _test_args(**overrides):
    base = dict(
        fixture="happy-path",
        repo=None,
        init_sandbox=False,
        profile="gstack",
        loop=False,
        keep=False,
        timeout=60,
        convergence_threshold=3,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _result(**overrides):
    base = dict(
        exit_code=0,
        report_path=None,
        temp_dir=None,
        summary="run summary",
        issue_number=None,
        pr_numbers=(),
        cleanup_leftovers=(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_test_subcommand_parsing():
    """Verify 'test' subcommand parses --fixture flag."""
    parser = build_parser()
    args = parser.parse_args(["test", "--fixture", "happy-path"])
    assert args.command == "test"
    assert args.fixture == "happy-path"
    assert args.profile == "gstack"


def test_test_subcommand_profile_flag():
    parser = build_parser()

    args = parser.parse_args(
        ["test", "--fixture", "happy-path", "--profile", "agent-skills"]
    )

    assert args.profile == "agent-skills"


def test_test_parser_accepts_repo_and_init_sandbox(capsys):
    parser = build_parser()

    args = parser.parse_args(["test", "--repo", "owner/sandbox", "--init-sandbox"])
    assert args.repo == "owner/sandbox"
    assert args.init_sandbox is True

    defaults = parser.parse_args(["test"])
    assert defaults.repo is None
    assert defaults.init_sandbox is False

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["test", "--help"])
    help_output = " ".join(capsys.readouterr().out.split())
    assert exc_info.value.code == 0
    assert "TPO_HARNESS_REPO" in help_output
    assert "sandbox GitHub repository" in help_output
    assert "sandbox issue/PR/branch" in help_output


def test_test_parser_rejects_phase(capsys):
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["test", "--fixture", "happy-path", "--phase", "phase_2_autoplan"])

    assert exc_info.value.code == 2
    assert "--phase" in capsys.readouterr().err


def test_fixture_defaults_to_happy_path():
    parser = build_parser()

    args = parser.parse_args(["test"])

    assert args.fixture == "happy-path"


def test_cmd_test_forwards_repo_and_no_phase_only(mocker):
    run = mocker.patch("hermes_pipeline.harness.run_harness")
    run.return_value = _result()
    args = _test_args(repo="owner/sandbox", profile="agent-skills", loop=True, keep=True)

    assert _cmd_test(args, config="cfg") == 0

    assert run.call_args.args == ()
    assert run.call_args.kwargs == {
        "fixture_name": "happy-path",
        "repo": "owner/sandbox",
        "loop": True,
        "keep_dir": True,
        "timeout": 60,
        "convergence_threshold": 3,
        "config": "cfg",
        "profile_name": "agent-skills",
    }


def test_cmd_test_prints_summary_and_leftovers(mocker, capsys):
    run = mocker.patch("hermes_pipeline.harness.run_harness")
    run.return_value = _result(
        exit_code=1,
        summary="PASS: 3 FAIL: 1",
        cleanup_leftovers=("issue #7 still open", "branch tpo/run-1"),
    )

    assert _cmd_test(_test_args(), config=None) == 1

    out = capsys.readouterr().out.splitlines()
    assert out == [
        "PASS: 3 FAIL: 1",
        "leftover: issue #7 still open",
        "leftover: branch tpo/run-1",
    ]


def test_cmd_test_reports_profile_errors_with_detail(mocker, caplog):
    from hermes_pipeline.harness import HarnessProfileError

    run = mocker.patch("hermes_pipeline.harness.run_harness")
    run.side_effect = HarnessProfileError(
        "missing_conditional_prerequisite", "native-sdd", "profile detail"
    )

    with caplog.at_level(logging.ERROR):
        assert _cmd_test(_test_args(profile="native-sdd"), config=None) == 2

    assert "code=missing_conditional_prerequisite" in caplog.text
    assert "profile=native-sdd" in caplog.text
    assert "profile detail" in caplog.text


def test_cmd_test_preflight_error_exits_2_with_detail(mocker, caplog):
    from hermes_pipeline.harness import HarnessPreflightError

    run = mocker.patch("hermes_pipeline.harness.run_harness")
    run.side_effect = HarnessPreflightError("gh_auth", "gh auth status failed for host")

    with caplog.at_level(logging.ERROR):
        assert _cmd_test(_test_args(), config=None) == 2

    assert "preflight failed" in caplog.text
    assert "code=gh_auth" in caplog.text
    assert "gh auth status failed for host" in caplog.text


def test_cmd_test_remote_cleanup_error_exits_2_with_detail(mocker, caplog):
    from hermes_pipeline.harness import HarnessRemoteCleanupError

    run = mocker.patch("hermes_pipeline.harness.run_harness")
    run.side_effect = HarnessRemoteCleanupError("issue_close_failed", "issue #9 still open")

    with caplog.at_level(logging.ERROR):
        assert _cmd_test(_test_args(), config=None) == 2

    assert "cleanup incomplete" in caplog.text
    assert "code=issue_close_failed" in caplog.text
    assert "issue #9 still open" in caplog.text


def test_cmd_test_cleanup_error_logs_notes(mocker, caplog):
    from hermes_pipeline.harness import HarnessCleanupError

    err = HarnessCleanupError("workspace retained at /tmp/harness-x; leftovers: 2")
    err.add_note("leftover: pid 123 alive")
    err.add_note("leftover: card 42 running")
    run = mocker.patch("hermes_pipeline.harness.run_harness")
    run.side_effect = err

    with caplog.at_level(logging.ERROR):
        assert _cmd_test(_test_args(), config=None) == 2

    assert "cleanup failed" in caplog.text
    assert "workspace retained at /tmp/harness-x" in caplog.text
    assert "leftover: pid 123 alive" in caplog.text
    assert "leftover: card 42 running" in caplog.text


def test_cmd_test_generic_error_logs_message(mocker, caplog):
    run = mocker.patch("hermes_pipeline.harness.run_harness")
    run.side_effect = RuntimeError("boom: clone exploded")

    with caplog.at_level(logging.ERROR):
        assert _cmd_test(_test_args(), config=None) == 2

    assert "error_type=RuntimeError" in caplog.text
    assert "boom: clone exploded" in caplog.text


def test_init_sandbox_dispatch(mocker, tmp_path, capsys):
    sandbox = SimpleNamespace(repo="owner/sandbox")
    resolve = mocker.patch("hermes_pipeline.harness.resolve_sandbox_repo", return_value=sandbox)
    seen = {}

    def fake_init(sb, workspace):
        seen["sandbox"] = sb
        seen["workspace"] = workspace
        assert workspace.is_dir()
        assert workspace.parent == tmp_path
        assert workspace.name.startswith("harness-init-")
        return "seeded"

    init = mocker.patch("hermes_pipeline.harness.init_sandbox", side_effect=fake_init)
    mocker.patch("hermes_pipeline.harness._harness_tmp_root", return_value=tmp_path)
    run = mocker.patch("hermes_pipeline.harness.run_harness")

    assert _cmd_test(_test_args(repo="owner/sandbox", init_sandbox=True), config=None) == 0

    resolve.assert_called_once_with("owner/sandbox")
    assert init.call_count == 1
    assert seen["sandbox"] is sandbox
    assert not seen["workspace"].exists()
    run.assert_not_called()
    assert capsys.readouterr().out.strip() == "sandbox owner/sandbox: seeded"


def test_init_sandbox_preflight_error_exits_2(mocker, caplog):
    from hermes_pipeline.harness import HarnessPreflightError

    mocker.patch(
        "hermes_pipeline.harness.resolve_sandbox_repo",
        side_effect=HarnessPreflightError(
            "repo_missing", "pass --repo owner/name or set TPO_HARNESS_REPO"
        ),
    )
    run = mocker.patch("hermes_pipeline.harness.run_harness")

    with caplog.at_level(logging.ERROR):
        assert _cmd_test(_test_args(init_sandbox=True), config=None) == 2

    run.assert_not_called()
    assert "code=repo_missing" in caplog.text
    assert "pass --repo owner/name or set TPO_HARNESS_REPO" in caplog.text


def test_init_sandbox_rejects_gh_override_exits_2(mocker, tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("TPO_GH_BIN", "/opt/fake/gh")
    mocker.patch("hermes_pipeline.harness._harness_tmp_root", return_value=tmp_path / "tmp")
    git = mocker.patch("hermes_pipeline.harness._git", side_effect=AssertionError("git must not run"))
    gh = mocker.patch("hermes_pipeline.github_issues._run", side_effect=AssertionError("gh must not run"))

    with caplog.at_level(logging.ERROR):
        assert _cmd_test(_test_args(repo="owner/sandbox", init_sandbox=True), config=None) == 2

    git.assert_not_called()
    gh.assert_not_called()
    assert "code=gh_override_forbidden" in caplog.text
    assert list((tmp_path / "tmp").iterdir()) == []  # the init workspace is removed again


def test_init_sandbox_creates_missing_tmp_root(mocker, tmp_path, capsys):
    root = tmp_path / "nested" / "hermes-tmp"
    sandbox = SimpleNamespace(repo="owner/sandbox")
    mocker.patch("hermes_pipeline.harness.resolve_sandbox_repo", return_value=sandbox)
    mocker.patch("hermes_pipeline.harness._harness_tmp_root", return_value=root)
    init = mocker.patch("hermes_pipeline.harness.init_sandbox", return_value="already_seeded")

    assert _cmd_test(_test_args(init_sandbox=True), config=None) == 0

    assert root.is_dir()
    assert init.call_args.args[1].parent == root
    assert not init.call_args.args[1].exists()
    assert capsys.readouterr().out.strip() == "sandbox owner/sandbox: already_seeded"


def test_init_sandbox_github_error_exits_2(mocker, tmp_path, caplog):
    from hermes_pipeline.github_issues import GitHubIssuesError

    sandbox = SimpleNamespace(repo="owner/sandbox")
    mocker.patch("hermes_pipeline.harness.resolve_sandbox_repo", return_value=sandbox)
    mocker.patch("hermes_pipeline.harness._harness_tmp_root", return_value=tmp_path)
    mocker.patch(
        "hermes_pipeline.harness.init_sandbox",
        side_effect=GitHubIssuesError("repo_not_found", "repo view owner/sandbox"),
    )
    run = mocker.patch("hermes_pipeline.harness.run_harness")

    with caplog.at_level(logging.ERROR):
        assert _cmd_test(_test_args(init_sandbox=True), config=None) == 2

    run.assert_not_called()
    assert "sandbox init failed" in caplog.text
    assert "repo_not_found: gh repo view owner/sandbox" in caplog.text


def test_init_sandbox_generic_error_exits_2(mocker, tmp_path, caplog):
    sandbox = SimpleNamespace(repo="owner/sandbox")
    mocker.patch("hermes_pipeline.harness.resolve_sandbox_repo", return_value=sandbox)
    mocker.patch("hermes_pipeline.harness._harness_tmp_root", return_value=tmp_path)
    mocker.patch(
        "hermes_pipeline.harness.init_sandbox",
        side_effect=RuntimeError("git push exploded"),
    )
    run = mocker.patch("hermes_pipeline.harness.run_harness")

    with caplog.at_level(logging.ERROR):
        assert _cmd_test(_test_args(init_sandbox=True), config=None) == 2

    run.assert_not_called()
    assert "sandbox init failed" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "git push exploded" in caplog.text


def test_test_subcommand_loop_flag():
    """Verify --loop flag is parsed."""
    parser = build_parser()
    args = parser.parse_args(["test", "--fixture", "happy-path", "--loop"])
    assert args.loop is True


def test_test_subcommand_loop_help_describes_workspace_snapshot(capsys):
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["test", "--help"])

    help_output = " ".join(capsys.readouterr().out.split())
    assert exc_info.value.code == 0
    assert "numbered report snapshot in the current workspace" in help_output
    assert "previous run" not in help_output


def test_test_subcommand_timeout_default_is_86400():
    """--timeout must default large enough that it stops being the de-facto
    kill switch for healthy long test runs (raised from 3600s / 1h)."""
    parser = build_parser()
    args = parser.parse_args(["test", "--fixture", "happy-path"])
    assert args.timeout == 86400


class TestVerboseDebugFlags:
    """--verbose/--debug are stripped before argparse and configure root logging."""

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["--verbose", "doctor"], (True, False, ["doctor"])),
            (["--debug", "doctor"], (False, True, ["doctor"])),
            (["--verbose", "--debug", "doctor"], (True, True, ["doctor"])),
            (["tick", "myproject", "--verbose"], (True, False, ["tick", "myproject"])),
            (["doctor"], (False, False, ["doctor"])),
        ],
    )
    def test_strip_global_flags(self, argv, expected):
        from hermes_pipeline.cli import _strip_global_flags

        assert _strip_global_flags(argv) == expected

    @staticmethod
    def _project(tmp_path, monkeypatch):
        projects_dir = tmp_path / "projects"
        (projects_dir / "test-proj").mkdir(parents=True)
        config_file = tmp_path / "config.yaml"
        config_file.write_text(f"projects_dir: {projects_dir!s}\n")
        monkeypatch.setenv("TPO_CONFIG_FILE", str(config_file))

    @pytest.mark.parametrize(
        ("argv", "level"),
        [
            (["--verbose", "doctor", "test-proj"], logging.INFO),
            (["doctor", "test-proj", "--verbose"], logging.INFO),
            (["--debug", "doctor", "test-proj"], logging.DEBUG),
            (["doctor", "test-proj", "--debug"], logging.DEBUG),
        ],
    )
    def test_main_configures_logging_level_wherever_the_flag_sits(
        self, tmp_path, monkeypatch, argv, level
    ):
        self._project(tmp_path, monkeypatch)

        # The project has no pipeline contract, so doctor reports MISSING (2):
        # the real subcommand ran after the flag was stripped.
        assert main(argv) == 2
        assert logging.getLogger().level == level
