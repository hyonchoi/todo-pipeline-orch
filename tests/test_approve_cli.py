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
