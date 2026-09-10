import importlib.util
import json
import stat
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

import pytest

from hermes_pipeline.cli import build_parser
from hermes_pipeline.config import Config
from tests.test_todos_create import request

SKILL = files("hermes_pipeline").joinpath(
    "data", "skills", "issue-planner", "SKILL.md"
)
REQUEST_WRITER = SKILL.parent.joinpath("scripts", "write_request.py")


def request_path(project: Path) -> Path:
    payload = request()
    path = (
        project
        / ".hermes"
        / "todo-create-input"
        / f"{payload['transaction_id']}.json"
    )
    path.parent.mkdir(parents=True)
    path.parent.chmod(0o700)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


def run_create(projects: Path, argv: list[str]) -> int:
    args = build_parser().parse_args(["todos", "create", *argv])
    return args.func(args, Config(projects_dir=projects))


def run_writer(project: Path, payload: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REQUEST_WRITER), str(project), payload["transaction_id"]],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )


def test_request_writer_creates_private_input_exclusively_in_fixed_namespace(tmp_path):
    payload = request()
    result = run_writer(tmp_path, payload)
    expected = (
        tmp_path
        / ".hermes"
        / "todo-create-input"
        / f"{payload['transaction_id']}.json"
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == expected
    assert stat.S_IMODE(expected.stat().st_mode) == 0o600
    assert json.loads(expected.read_text()) == payload
    repeated = run_writer(tmp_path, payload)
    assert repeated.returncode != 0
    assert json.loads(expected.read_text()) == payload


@pytest.mark.parametrize("checkpoint", ["state-opened", "input-opened"])
def test_request_writer_detects_parent_swap_before_creation(
    tmp_path, monkeypatch, checkpoint
):
    spec = importlib.util.spec_from_file_location("todo_request_writer", REQUEST_WRITER)
    assert spec is not None and spec.loader is not None
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)
    payload = request()
    state = tmp_path / ".hermes"
    directory = state / "todo-create-input"

    def swap(point: str) -> None:
        if point != checkpoint:
            return
        target = state if checkpoint == "state-opened" else directory
        target.rename(target.with_name(f"{target.name}-moved"))
        target.mkdir()

    monkeypatch.setattr(writer, "_checkpoint", swap)
    with pytest.raises(SystemExit, match="directory identity changed"):
        writer.write_request(
            tmp_path, payload["transaction_id"], json.dumps(payload).encode()
        )

    assert not (directory / f"{payload['transaction_id']}.json").exists()
    assert not tuple(tmp_path.rglob(f"{payload['transaction_id']}.json"))

def test_cli_preview_names_resolved_project_and_repository_without_mutation(
    tmp_path, mocker, capsys
):
    project = tmp_path / "demo"
    project.mkdir()
    path = request_path(project)
    mocker.patch(
        "hermes_pipeline.github_issues.repository_identity", return_value="acme/demo"
    )
    execute = mocker.patch("hermes_pipeline.todos_create.execute_create")
    mocker.patch("builtins.input", return_value="cancel")

    assert run_create(tmp_path, ["demo", "--request-file", str(path)]) == 1

    output = capsys.readouterr().out
    assert "Project: demo\nRepository: acme/demo\n" in output
    assert "Title:\nEmbed implementation plan" in output
    execute.assert_not_called()


def test_cli_yes_reuses_the_previewed_target_and_private_input(tmp_path, mocker, capsys):
    project = tmp_path / "demo"
    project.mkdir()
    path = request_path(project)
    mocker.patch(
        "hermes_pipeline.github_issues.repository_identity", return_value="acme/demo"
    )
    execute = mocker.patch(
        "hermes_pipeline.todos_create.execute_create", return_value=42
    )

    assert run_create(
        tmp_path,
        [
            "demo", "--request-file", str(path), "--approved-repo", "acme/demo", "--yes",
        ],
    ) == 0

    assert "Project: demo\nRepository: acme/demo\n" in capsys.readouterr().out
    assert execute.call_args.args[:3] == (project, project / ".hermes", mocker.ANY)
    assert execute.call_args.kwargs == {
        "approved_repo": "acme/demo",
        "issue_number": None,
    }


def test_cli_yes_requires_the_previewed_repository_binding(tmp_path, mocker, capsys):
    project = tmp_path / "demo"
    project.mkdir()
    path = request_path(project)
    mocker.patch(
        "hermes_pipeline.github_issues.repository_identity", return_value="acme/demo"
    )
    execute = mocker.patch("hermes_pipeline.todos_create.execute_create")

    assert run_create(
        tmp_path, ["demo", "--request-file", str(path), "--yes"]
    ) == 2

    assert "approved_repo_required" in capsys.readouterr().err
    execute.assert_not_called()


def test_cli_fails_closed_when_origin_drifts_after_preview(tmp_path, mocker, capsys):
    project = tmp_path / "demo"
    project.mkdir()
    path = request_path(project)
    identity = mocker.patch(
        "hermes_pipeline.github_issues.repository_identity",
        side_effect=["acme/demo", "other/demo"],
    )
    mutate = mocker.patch("hermes_pipeline.github_issues.list_all_issues")

    assert run_create(
        tmp_path,
        [
            "demo", "--request-file", str(path), "--approved-repo", "acme/demo", "--yes",
        ],
    ) == 1

    assert "repository_drift" in capsys.readouterr().err
    assert identity.call_count == 2
    mutate.assert_not_called()


def test_cli_refuses_request_path_outside_the_resolved_project_namespace(
    tmp_path, mocker, capsys
):
    project = tmp_path / "demo"
    project.mkdir()
    wrong_project = tmp_path / "other"
    wrong_project.mkdir()
    path = request_path(wrong_project)
    repository = mocker.patch("hermes_pipeline.github_issues.repository_identity")

    assert run_create(
        tmp_path,
        ["demo", "--request-file", str(path), "--approved-repo", "acme/demo", "--yes"],
    ) == 2

    assert "invalid_request_path" in capsys.readouterr().err
    repository.assert_not_called()


def test_cli_refuses_symlinked_project_input_namespace(tmp_path, mocker, capsys):
    project = tmp_path / "demo"
    project.mkdir()
    external = tmp_path / "external"
    external_path = request_path(external)
    (project / ".hermes").symlink_to(external / ".hermes", target_is_directory=True)
    path = project / ".hermes" / "todo-create-input" / external_path.name
    repository = mocker.patch("hermes_pipeline.github_issues.repository_identity")

    assert run_create(
        tmp_path, ["demo", "--request-file", str(path), "--yes"]
    ) == 2

    assert "invalid_request_path" in capsys.readouterr().err
    repository.assert_not_called()


@pytest.mark.parametrize("issue_args", [[], ["--issue", "42"]])
def test_cli_partial_recovery_keeps_same_request_and_only_uses_confirmed_issue(
    tmp_path, mocker, issue_args
):
    project = tmp_path / "demo"
    project.mkdir()
    path = request_path(project)
    mocker.patch(
        "hermes_pipeline.github_issues.repository_identity", return_value="acme/demo"
    )
    execute = mocker.patch(
        "hermes_pipeline.todos_create.execute_create", side_effect=OSError("partial")
    )

    assert run_create(
        tmp_path,
        [
            "demo", "--request-file", str(path), "--approved-repo", "acme/demo",
            "--yes", *issue_args,
        ],
    ) == 1

    assert execute.call_args.kwargs["issue_number"] == (42 if issue_args else None)
