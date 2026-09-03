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
    "data", "skills", "todo-manager", "SKILL.md"
)
REQUEST_WRITER = SKILL.parent.joinpath("scripts", "write_request.py")


def skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def normalized_skill_text() -> str:
    return " ".join(skill_text().split())


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


def test_skill_selects_only_the_latest_finalized_plan_and_strips_outer_tags():
    text = normalized_skill_text()
    ordered = [
        "latest complete `<proposed_plan>...</proposed_plan>` block",
        "Ignore drafts, summaries, quoted examples, and incomplete blocks",
        "Strip only that block's opening and closing tags",
        "Preserve all inner Markdown",
    ]
    positions = [text.index(phrase) for phrase in ordered]
    assert positions == sorted(positions)


def test_skill_derives_the_strict_task_schema_without_inventing_plan_content():
    text = normalized_skill_text()
    for key in (
        "`id`",
        "`title`",
        "`instructions`",
        "`acceptance_criteria`",
        "`verification`",
        "`commit_message`",
    ):
        assert key in text
    assert "one task per explicit implementation task" in text
    assert "Do not invent task boundaries, acceptance criteria, verification commands, or commit messages" in text
    assert "ask the user to finalize the Plan" in text


def test_skill_researches_all_remaining_issue_fields_before_preview():
    text = normalized_skill_text()
    assert "Research the repository and current issue context" in text
    assert "every field required by `tpo todos create`" in text
    assert "Do not add `Plan`, `Legacy ID`, labels, an issue number, or a TODO ID" in text
    assert "Resolve uncertainty with the user before preview" in text


def test_skill_requires_full_preview_exact_approval_and_durable_recovery():
    text = normalized_skill_text()
    preview = text.index("Show the complete output")
    approval = text.index("exact reply `create`")
    mutation = text.index(
        "tpo todos create PROJECT --request-file REQUEST --approved-repo OWNER/REPO --yes"
    )
    assert preview < approval < mutation
    assert "Never invoke `--yes` before that approval" in text
    assert "Keep this file until the CLI reports completion" in text
    assert "first rerun the same command without `--issue`" in text
    assert "only when the partial issue number and its matching transaction marker were independently confirmed" in text
    assert "reuse the identical literal `PROJECT`, request path, and canonical repository binding" in text


def test_skill_rejects_drafts_wrappers_and_secrets():
    text = normalized_skill_text()
    assert "Stop if no finalized block exists" in text
    assert "Do not submit `<proposed_plan>` tags" in text
    assert "Never place credentials, tokens, authorization data, provider responses, or secrets" in text
    assert "redact the value and stop for user direction" in text


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
