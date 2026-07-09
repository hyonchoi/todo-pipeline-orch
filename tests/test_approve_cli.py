import pytest

import hermes_pipeline.cli as cli
from hermes_pipeline.ship import ApproveRefused, ShipError


def _config(tmp_path):
    return cli.Config(
        lock_dir=tmp_path / "locks",
        projects_dir=tmp_path / "projects",
        state_dir=tmp_path / "state",
        kanban_adapter="null",
    )


def test_approve_parser_requires_todo_and_counts_force():
    parser = cli.build_parser()
    args = parser.parse_args(["approve", "demo", "--todo", "TODO-5", "--force", "--force"])
    assert args.project == "demo"
    assert args.todo == 5
    assert args.force == 2
    assert args.func is cli._cmd_approve


def test_cmd_approve_success_returns_zero(mocker, tmp_path):
    cfg = _config(tmp_path)
    pdir = tmp_path / "projects" / "demo"
    pdir.mkdir(parents=True)
    mocker.patch.object(cli, "_resolve_project_dir", return_value=pdir)
    mocker.patch("hermes_pipeline.state_migration._get_project_state_dir",
                 return_value=tmp_path / "state")
    approve = mocker.patch("hermes_pipeline.ship.approve_ship", return_value="Shipped TODO-5")
    args = cli.build_parser().parse_args(["approve", "demo", "--todo", "TODO-5"])
    assert cli._cmd_approve(args, cfg) == 0
    _, kwargs = approve.call_args
    assert kwargs["todo_id"] == 5
    assert kwargs["force_count"] == 0


def test_cmd_approve_refused_returns_three(mocker, tmp_path):
    cfg = _config(tmp_path)
    pdir = tmp_path / "projects" / "demo"
    pdir.mkdir(parents=True)
    mocker.patch.object(cli, "_resolve_project_dir", return_value=pdir)
    mocker.patch("hermes_pipeline.state_migration._get_project_state_dir",
                 return_value=tmp_path / "state")
    mocker.patch("hermes_pipeline.ship.approve_ship",
                 side_effect=ApproveRefused("CI not green"))
    args = cli.build_parser().parse_args(["approve", "demo", "--todo", "TODO-5"])
    assert cli._cmd_approve(args, cfg) == 3


def test_cmd_approve_unknown_project_returns_two(mocker, tmp_path):
    cfg = _config(tmp_path)
    mocker.patch.object(cli, "_resolve_project_dir", return_value=None)
    args = cli.build_parser().parse_args(["approve", "nope", "--todo", "TODO-5"])
    assert cli._cmd_approve(args, cfg) == 2


def test_module_docstring_mentions_approve():
    assert "approve" in (cli.__doc__ or "")


def test_parse_todo_id_flag_lowercase():
    """_parse_todo_id_flag accepts lowercase 'todo-5'."""
    assert cli._parse_todo_id_flag("todo-5") == 5


def test_parse_todo_id_flag_plain_number():
    """_parse_todo_id_flag accepts plain number '5'."""
    assert cli._parse_todo_id_flag("5") == 5


def test_parse_todo_id_flag_invalid_raises():
    """_parse_todo_id_flag raises ArgumentTypeError on non-numeric input."""
    import argparse
    with pytest.raises(argparse.ArgumentTypeError, match="--todo"):
        cli._parse_todo_id_flag("abc")


# -----------------------------------------------------------------------
# approve-plan CLI handler tests (CP14, CP15)
# -----------------------------------------------------------------------


def _plan_config(tmp_path):
    return cli.Config(
        lock_dir=tmp_path / "locks",
        projects_dir=tmp_path / "projects",
        state_dir=tmp_path / "state",
        kanban_adapter="null",
    )


def _plan_args(argv):
    return cli.build_parser().parse_args(["approve-plan"] + argv)


def test_plan_parser_approve():
    args = _plan_args(["demo", "--todo", "TODO-5", "--approve", "--override", "q1=B"])
    assert args.project == "demo"
    assert args.todo == 5
    assert args.approve is True
    assert args.reject is False
    assert args.override == ["q1=B"]
    assert args.reason is None
    assert args.func is cli._cmd_approve_plan


def test_plan_parser_reject():
    args = _plan_args(["demo", "--todo", "TODO-5", "--reject", "--reason", "bad plan"])
    assert args.approve is False
    assert args.reject is True
    assert args.reason == "bad plan"
    assert args.func is cli._cmd_approve_plan


def test_plan_parser_both_flags_refused():
    """CP14: both --approve and --reject → argparse mutual exclusion fires."""
    import pytest
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["approve-plan", "demo", "--todo", "TODO-5", "--approve", "--reject"])


def test_plan_reject_requires_reason():
    """CP15: --reject without --reason → exit 3 with clear message."""
    cfg = _plan_config(tmp_path := __import__("pathlib").Path("/tmp/plan-test-cli"))
    args = _plan_args(["demo", "--todo", "TODO-5", "--reject"])
    assert cli._cmd_approve_plan(args, cfg) == 3


def test_plan_approve_with_reason_refused():
    """CP15: --approve with --reason → exit 3."""
    cfg = _plan_config(__import__("pathlib").Path("/tmp/plan-test-cli"))
    args = _plan_args(["demo", "--todo", "TODO-5", "--approve", "--reason", "extra"])
    assert cli._cmd_approve_plan(args, cfg) == 3


def test_plan_reject_with_override_refused():
    """CP15: --reject with --override → exit 3."""
    cfg = _plan_config(__import__("pathlib").Path("/tmp/plan-test-cli"))
    args = _plan_args(["demo", "--todo", "TODO-5", "--reject", "--reason", "bad", "--override", "q1=A"])
    assert cli._cmd_approve_plan(args, cfg) == 3


def test_cmd_plan_approve_success(mocker, tmp_path):
    pdir = tmp_path / "projects" / "demo"
    pdir.mkdir(parents=True)
    sd = tmp_path / "state"
    sd.mkdir()
    mocker.patch.object(cli, "_resolve_project_dir", return_value=pdir)
    mocker.patch("hermes_pipeline.state_migration._get_project_state_dir", return_value=sd)
    mocker.patch("hermes_pipeline.approve_plan.approve_plan", return_value="Approved")
    args = _plan_args(["demo", "--todo", "TODO-5", "--approve"])
    assert cli._cmd_approve_plan(args, _plan_config(tmp_path)) == 0


def test_cmd_plan_approve_refused_returns_three(mocker, tmp_path):
    pdir = tmp_path / "projects" / "demo"
    pdir.mkdir(parents=True)
    mocker.patch.object(cli, "_resolve_project_dir", return_value=pdir)
    mocker.patch("hermes_pipeline.state_migration._get_project_state_dir",
                 return_value=tmp_path / "state")
    from hermes_pipeline.ship import ApproveRefused
    mocker.patch("hermes_pipeline.approve_plan.approve_plan",
                 side_effect=ApproveRefused("no decision sheet"))
    args = _plan_args(["demo", "--todo", "TODO-5", "--approve"])
    assert cli._cmd_approve_plan(args, _plan_config(tmp_path)) == 3


def test_cmd_plan_approve_unknown_project_returns_two(mocker, tmp_path):
    mocker.patch.object(cli, "_resolve_project_dir", return_value=None)
    args = _plan_args(["nope", "--todo", "TODO-5", "--approve"])
    assert cli._cmd_approve_plan(args, _plan_config(tmp_path)) == 2
