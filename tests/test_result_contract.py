from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.gh_fakes import make_issue

REPO = "acme/repo"
ISSUE_BODY = "### What\n\nResult contract.\n\n### Plan\n\nplan.md\n\n### Branch\n\ntodo-42\n"

from hermes_pipeline.github_issues import (
    MAX_ISSUE_SNAPSHOT_CHARS,
    canonical_issue_snapshot,
    snapshot_hash,
)
from hermes_pipeline.result_contract import (
    _FINDING_PRIORITIES,
    ResultContractError,
    load_validated_registration,
    parse_worker_result,
    sanitize_result_text,
    verify_read_only_review,
    verify_worker_git_result,
    verify_worker_git_topology,
)

PLAN = '''# Plan

```json tpo-plan
{"schema_version":1,"todo_id":"TODO-42","tasks":[{"id":"task-1","title":"Do it","instructions":"Implement it.","acceptance_criteria":["Observable criterion"],"verification":["uv run pytest"],"commit_message":"feat: do it"}]}
```
'''

PLAN_TWO_TASKS = '''# Plan

```json tpo-plan
{"schema_version":1,"todo_id":"TODO-42","tasks":[{"id":"task-1","title":"Do it","instructions":"Implement it.","acceptance_criteria":["Observable criterion"],"verification":["uv run pytest"],"commit_message":"feat: do it"},{"id":"task-2","title":"Do it again","instructions":"Implement it again.","acceptance_criteria":["Observable criterion"],"verification":["uv run pytest"],"commit_message":"feat: do it again"}]}
```
'''


def _result(**updates):
    value = {
        "schema_version": 1,
        "tick_id": "01TICK",
        "todo_id": "TODO-42",
        "step_key": "plan:task-1",
        "verdict": "success",
        "external_session_id": "session-1",
        "git": {
            "expected_parent_sha": "a" * 40,
            "resulting_head_sha": "b" * 40,
            "task_commit_sha": "b" * 40,
            "changed_files": ["src/example.py"],
        },
        "tdd": {
            "red": {"command": "uv run pytest tests/test_example.py", "exit_code": 1},
            "green": {"command": "uv run pytest tests/test_example.py", "exit_code": 0},
            "refactor": {"command": "uv run pytest tests/test_example.py", "exit_code": 0},
        },
        "acceptance": [{"criterion": "Observable criterion", "status": "passed"}],
    }
    value.update(updates)
    return value


def test_parse_valid_final_successful_run():
    payload = {
        "runs": [
            {"status": "failed", "metadata": {"tpo_result": _result()}},
            {"status": "succeeded", "summary": "done", "metadata": {"tpo_result": _result()}},
        ]
    }
    parsed = parse_worker_result(
        payload,
        tick_id="01TICK",
        todo_id="TODO-42",
        step_key="plan:task-1",
        acceptance_criteria=("Observable criterion",),
    )
    assert parsed.external_session_id == "session-1"
    assert parsed.git.changed_files == ("src/example.py",)


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda p: p.update(tick_id="wrong"), "identity_mismatch"),
        (lambda p: p.update(external_session_id=""), "invalid_session"),
        (lambda p: p["git"].update(task_commit_sha="c" * 40), "invalid_git"),
        (lambda p: p["tdd"]["red"].update(exit_code=0), "invalid_tdd"),
        (lambda p: p["tdd"]["green"].update(exit_code=1), "invalid_tdd"),
        (lambda p: p["acceptance"][0].update(status="pending"), "invalid_acceptance"),
        (lambda p: p["tdd"]["red"].update(command="x" * 501), "size_limit"),
    ],
)
def test_parse_rejects_invalid_contract(mutation, code):
    result = _result()
    mutation(result)
    with pytest.raises(ResultContractError, match=code):
        parse_worker_result(
            {"runs": [{"status": "succeeded", "metadata": {"tpo_result": result}}]},
            tick_id="01TICK",
            todo_id="TODO-42",
            step_key="plan:task-1",
            acceptance_criteria=("Observable criterion",),
        )


def test_parse_rejects_malformed_missing_and_oversized_metadata():
    for payload in ({}, {"runs": [{}]}, {"runs": [{"status": "succeeded", "metadata": {}}]}):
        with pytest.raises(ResultContractError):
            parse_worker_result(
                payload,
                tick_id="01TICK",
                todo_id="TODO-42",
                step_key="plan:task-1",
                acceptance_criteria=("Observable criterion",),
            )
    oversized = _result(extra="x" * 65536)
    with pytest.raises(ResultContractError, match="size_limit"):
        parse_worker_result(
            {"runs": [{"status": "succeeded", "metadata": {"tpo_result": oversized}}]},
            tick_id="01TICK",
            todo_id="TODO-42",
            step_key="plan:task-1",
            acceptance_criteria=("Observable criterion",),
        )


@pytest.mark.parametrize(
    "sibling",
    [
        {"provider_body": "password=super-secret"},
        {"unsafe\x00key": "value"},
        {"padding": "x" * 65536},
    ],
)
def test_parse_validates_entire_enclosing_run_metadata(sibling):
    metadata = {"tpo_result": _result(), **sibling}
    with pytest.raises(ResultContractError, match="unsafe_metadata|size_limit|malformed_result"):
        parse_worker_result(
            {"runs": [{"status": "succeeded", "metadata": metadata}]},
            tick_id="01TICK",
            todo_id="TODO-42",
            step_key="plan:task-1",
            acceptance_criteria=("Observable criterion",),
        )


def test_summary_and_diagnostics_are_sanitized():
    secret = "gh" + "p_abcdefghijklmnopqrstuvwxyz1234567890"
    sanitized = sanitize_result_text(f"bad\x00 token {secret}", maximum=8192)
    assert "\x00" not in sanitized
    assert secret not in sanitized
    assert "[REDACTED]" in sanitized
    assert sanitize_result_text("a\u2028b\u2029c\u202ed\u2066e\r\n\tf", maximum=100) == "a b cde f"


@pytest.mark.parametrize(
    "path",
    [
        ("git", "changed_files", 0),
        ("acceptance", 0, "criterion"),
        ("delivery", "checks", 0, "command"),
    ],
)
def test_parse_rejects_secrets_and_controls_at_any_metadata_depth(path):
    result = _result()
    result["delivery"] = {
        "pr_url": "https://github.com/example/repo/pull/1",
        "branch": "todo-42",
        "head_sha": "b" * 40,
        "checks": [{"command": "uv run pytest", "exit_code": 0}],
    }
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = "password=super-secret\x00"
    with pytest.raises(ResultContractError, match="unsafe_metadata"):
        parse_worker_result(
            {"runs": [{"status": "succeeded", "metadata": {"tpo_result": result}}]},
            tick_id="01TICK",
            todo_id="TODO-42",
            step_key="plan:task-1",
            acceptance_criteria=("Observable criterion",),
        )


def _parse_review(review):
    result = _result(
        step_key="review:initial",
        git={
            "expected_parent_sha": "a" * 40,
            "resulting_head_sha": "a" * 40,
            "task_commit_sha": "a" * 40,
            "changed_files": [],
        },
        acceptance=[],
        review=review,
    )
    return parse_worker_result(
        {"runs": [{"status": "succeeded", "metadata": {"tpo_result": result}}]},
        tick_id="01TICK",
        todo_id="TODO-42",
        step_key="review:initial",
        acceptance_criteria=(),
        allow_no_changes=True,
    )


def test_review_evidence_preserves_bounded_structured_findings():
    finding = {
        "priority": "P2",
        "location": "src/example.py:12",
        "failure_scenario": "The invalid input reaches the unsafe branch.",
        "recommendation": "Validate the input before dispatch.",
    }
    parsed = _parse_review({"verdict": "findings", "findings": [finding]})
    assert parsed.review.verdict == "findings"
    assert parsed.review.findings == (finding,)


@pytest.mark.parametrize(
    "review",
    [
        pytest.param([], id="not-object"),
        pytest.param({"verdict": "clean", "findings": [], "extra": True}, id="unknown-key"),
        pytest.param({"verdict": "maybe", "findings": []}, id="bad-verdict"),
        pytest.param({"verdict": "clean", "findings": [{}]}, id="clean-with-finding"),
        pytest.param({"verdict": "findings", "findings": []}, id="findings-empty"),
        pytest.param(
            {
                "verdict": "findings",
                "findings": [
                    {
                        "priority": "P4",
                        "location": "file.py:1",
                        "failure_scenario": "failure",
                        "recommendation": "fix",
                    }
                ],
            },
            id="bad-priority",
        ),
    ],
)
def test_review_evidence_rejects_ambiguous_or_unbounded_shapes(review):
    with pytest.raises(ResultContractError, match="invalid_review"):
        _parse_review(review)


def test_read_only_review_requires_evidence_pinned_head_and_clean_worktree(
    tmp_path, mocker
):
    clean = _parse_review({"verdict": "clean", "findings": []})
    mocker.patch(
        "hermes_pipeline.result_contract.subprocess.run",
        return_value=SimpleNamespace(returncode=0, stdout=""),
    )
    verify_read_only_review(tmp_path, clean, head_sha="a" * 40)

    dirty = mocker.patch("hermes_pipeline.result_contract.subprocess.run")
    dirty.return_value = SimpleNamespace(returncode=0, stdout="?? untracked.txt\n")
    with pytest.raises(ResultContractError, match="review_dirty_worktree"):
        verify_read_only_review(tmp_path, clean, head_sha="a" * 40)

    changed = SimpleNamespace(
        review=clean.review,
        git=SimpleNamespace(
            expected_parent_sha="a" * 40,
            resulting_head_sha="a" * 40,
            task_commit_sha="a" * 40,
            changed_files=("src/example.py",),
        ),
    )
    with pytest.raises(ResultContractError, match="review_changed_head"):
        verify_read_only_review(tmp_path, changed, head_sha="a" * 40)


def test_delivery_evidence_accepts_only_successful_checks_and_exact_pr_identity():
    delivery = {
        "pr_url": "https://github.com/acme/repo/pull/7",
        "branch": "feat/native",
        "head_sha": "b" * 40,
        "checks": [{"command": "uv run pytest", "exit_code": 0}],
    }
    result = _result(delivery=delivery)
    parsed = parse_worker_result(
        {"runs": [{"status": "succeeded", "metadata": {"tpo_result": result}}]},
        tick_id="01TICK",
        todo_id="TODO-42",
        step_key="plan:task-1",
        acceptance_criteria=("Observable criterion",),
    )
    assert parsed.delivery.pr_url.endswith("/pull/7")
    assert parsed.delivery.checks[0].exit_code == 0

    for mutation in (
        lambda value: value.update(pr_url="https://github.com/acme/repo/issues/7"),
        lambda value: value.update(pr_url="https://evil.example/acme/repo/pull/7"),
        lambda value: value.update(pr_url="https://github.com/acme/repo/pull/7?x=1"),
        lambda value: value.update(pr_url="https://github.com/acme/repo/extra/pull/7"),
        lambda value: value.update(head_sha="short"),
        lambda value: value.update(checks=[]),
        lambda value: value.update(
            checks=[{"command": "uv run pytest", "exit_code": 1}]
        ),
    ):
        invalid = dict(delivery)
        mutation(invalid)
        with pytest.raises(ResultContractError, match="invalid_delivery"):
            parse_worker_result(
                {
                    "runs": [
                        {
                            "status": "succeeded",
                            "metadata": {"tpo_result": _result(delivery=invalid)},
                        }
                    ]
                },
                tick_id="01TICK",
                todo_id="TODO-42",
                step_key="plan:task-1",
                acceptance_criteria=("Observable criterion",),
            )


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def test_verify_git_requires_exactly_one_commit_and_matching_changed_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    parent = _git(repo, "rev-parse", "HEAD")
    (repo / "change.txt").write_text("change")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "change")
    head = _git(repo, "rev-parse", "HEAD")
    result = _result()
    result["git"] = {
        "expected_parent_sha": parent,
        "resulting_head_sha": head,
        "task_commit_sha": head,
        "changed_files": ["change.txt"],
    }
    parsed = parse_worker_result(
        {"runs": [{"status": "succeeded", "metadata": {"tpo_result": result}}]},
        tick_id="01TICK",
        todo_id="TODO-42",
        step_key="plan:task-1",
        acceptance_criteria=("Observable criterion",),
    )
    verify_worker_git_result(repo, parsed.git, expected_parent_sha=parent)
    with pytest.raises(ResultContractError, match="changed_files_mismatch"):
        verify_worker_git_result(
            repo,
            parsed.git.__class__(parent, head, head, ("wrong.txt",)),
            expected_parent_sha=parent,
        )
    (repo / "untracked.txt").write_text("keep")
    with pytest.raises(ResultContractError, match="worktree_dirty"):
        verify_worker_git_result(repo, parsed.git, expected_parent_sha=parent)


def test_historical_git_topology_remains_valid_after_later_fix_commit(tmp_path):
    repo = tmp_path / "repo-history"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    parent = _git(repo, "rev-parse", "HEAD")
    (repo / "first.txt").write_text("first")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "first")
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "later.txt").write_text("later")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "later")

    from hermes_pipeline.result_contract import GitResult

    verify_worker_git_topology(
        repo,
        GitResult(parent, first, first, ("first.txt",)),
        expected_parent_sha=parent,
    )
    with pytest.raises(ResultContractError, match="head_mismatch"):
        verify_worker_git_result(
            repo,
            GitResult(parent, first, first, ("first.txt",)),
            expected_parent_sha=parent,
        )


@pytest.mark.skipif(os.name == "nt", reason="byte filenames require POSIX")
def test_verify_git_reports_invalid_byte_untracked_filename_as_dirty(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    parent = _git(repo, "rev-parse", "HEAD")
    (repo / "change.txt").write_text("change")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "change")
    head = _git(repo, "rev-parse", "HEAD")
    try:
        fd = os.open(
            os.fsencode(repo) + b"/invalid-\xff", os.O_WRONLY | os.O_CREAT, 0o600
        )
    except OSError as exc:
        pytest.skip(f"filesystem rejects invalid-byte filenames: errno={exc.errno}")
    os.close(fd)
    git_result = _result()["git"]
    git_result.update(
        expected_parent_sha=parent,
        resulting_head_sha=head,
        task_commit_sha=head,
        changed_files=["change.txt"],
    )
    parsed = parse_worker_result(
        {"runs": [{"status": "succeeded", "metadata": {"tpo_result": {**_result(), "git": git_result}}}]},
        tick_id="01TICK",
        todo_id="TODO-42",
        step_key="plan:task-1",
        acceptance_criteria=("Observable criterion",),
    )
    with pytest.raises(ResultContractError, match="worktree_dirty"):
        verify_worker_git_result(repo, parsed.git, expected_parent_sha=parent)


def test_registration_rejects_unknown_keys_and_mutable_plan_drift(tmp_path):
    repo, worktree, state, _parent = _registered_repo(tmp_path)
    registration = state / "runs" / "01TICK" / "registration.json"
    payload = json.loads(registration.read_text())
    payload["unknown"] = True
    registration.write_text(json.dumps(payload))
    with pytest.raises(ResultContractError, match="registration_invalid"):
        load_validated_registration(repo, state, "01TICK")

    del payload["unknown"]
    registration.write_text(json.dumps(payload))
    (worktree / "plan.md").write_text("mutable drift")
    authority = load_validated_registration(repo, state, "01TICK")
    assert authority.manifest.tasks[0].id == "task-1"


def _registered_repo(
    tmp_path, *, issue_body: str = ISSUE_BODY, plan_path: str | None = "plan.md",
    embedded: bool = False, plan: str = PLAN,
    step_keys: tuple[str, ...] = ("plan:task-1",),
):
    from hermes_pipeline.plan_manifest import render_embedded_plan
    from hermes_pipeline.run_registration import register_pinned_run

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "remote", "add", "origin", f"git@github.com:{REPO}.git")
    (repo / "plan.md").write_text(plan)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    parent = _git(repo, "rev-parse", "HEAD")
    state = repo / ".hermes"
    if embedded:
        issue_body = ISSUE_BODY.replace("### Plan\n\nplan.md\n\n", "")
        issue_body += render_embedded_plan(plan, expected_todo_id="TODO-42")
        plan_path = None
    registration = register_pinned_run(
        project_dir=repo,
        state_dir=state,
        tick_id="01TICK",
        selected_issue=make_issue(42, repo=REPO, title="Do it", body=issue_body),
        plan_path=plan_path,
        profile="native-sdd",
        prompt_client="claude",
        assignee="pipeline",
        review_assignee=None,
        step_keys=step_keys,
    )
    return repo, registration.worktree, state, parent


def _rewrite_registration(state, mutate):
    path = state / "runs" / "01TICK" / "registration.json"
    payload = json.loads(path.read_text())
    mutate(payload)
    path.write_text(json.dumps(payload))
    return payload


def test_registration_authority_is_the_issue_snapshot(tmp_path):
    repo, worktree, state, parent = _registered_repo(tmp_path)

    authority = load_validated_registration(repo, state, "01TICK")

    assert authority.issue_number == 42
    assert authority.issue_url == "https://github.com/acme/repo/issues/42"
    assert authority.branch == "todo-42"
    assert authority.plan_path == "plan.md"
    assert authority.worktree == (repo / ".worktrees" / "todo-42-do-it").resolve()
    assert authority.manifest.tasks[0].id == "task-1"
    assert authority.plan_hash == json.loads(
        (state / "runs" / "01TICK" / "registration.json").read_text()
    )["plan_hash"]
    assert not (repo / "TODOS.md").exists()


def test_registration_accepts_step_keys_beyond_the_plan_tasks(tmp_path):
    """``required_steps <= steps`` is deliberate, in both directions.

    A profile that registers non-compiled phases alongside the plan workers, and
    a run registered before the per-task controller gate was dropped, both carry
    keys the manifest does not name. Neither may be rejected -- an equality
    check here would refuse to load an in-flight run's own authority.
    """
    extra = ("plan:task-1", "review", "finish", "human", "validate:task-1")
    repo, _worktree, state, _parent = _registered_repo(tmp_path, step_keys=extra)

    authority = load_validated_registration(repo, state, "01TICK")

    assert authority.step_keys == extra

    _rewrite_registration(state, lambda payload: payload.__setitem__("step_keys", ["review"]))
    with pytest.raises(ResultContractError, match="registration_invalid"):
        load_validated_registration(repo, state, "01TICK")


def test_embedded_registration_exposes_verified_artifact_reference(tmp_path):
    from hermes_pipeline.plan_manifest import render_embedded_plan
    from hermes_pipeline.run_registration import register_pinned_run

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "remote", "add", "origin", f"git@github.com:{REPO}.git")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    body = ISSUE_BODY.replace("### Plan\n\nplan.md\n\n", "")
    body += render_embedded_plan(PLAN, expected_todo_id="TODO-42")
    state = repo / ".hermes"
    registration = register_pinned_run(
        project_dir=repo, state_dir=state, tick_id="01TICK",
        selected_issue=make_issue(42, repo=REPO, title="Do it", body=body),
        plan_path=None, profile="native-sdd", prompt_client="claude",
        assignee="pipeline", review_assignee=None,
        step_keys=("plan:task-1",),
    )

    authority = load_validated_registration(repo, state, "01TICK")

    artifact = (state / "runs" / "01TICK" / "plan.md").resolve()
    assert authority.plan_source_kind == "embedded"
    assert authority.plan_path is None
    assert authority.plan_reference is not None
    assert authority.plan_reference.value == str(artifact)
    assert authority.plan_source is not None
    assert authority.plan_source.kind == "embedded"
    assert Path(authority.plan_reference.value).read_text() == PLAN
    assert registration.plan_path is None


def test_embedded_plan_result_validation_scenario(tmp_path):
    repo, _worktree, state, _parent = _registered_repo(tmp_path, embedded=True)

    authority = load_validated_registration(repo, state, "01TICK", repo=REPO)

    assert authority.plan_source is not None
    assert authority.plan_source.kind == "embedded"
    assert authority.manifest is not None
    assert authority.manifest.tasks[0].id == "task-1"


def test_embedded_plan_reconciliation_scenario(tmp_path, mocker):
    from hermes_pipeline.kanban_tasks import reconcile_plan_task_results

    repo, _worktree, state, _parent = _registered_repo(tmp_path, embedded=True)
    mocker.patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
        return_value={
            "plan:task-1": SimpleNamespace(task_id="worker", status="todo"),
        },
    )

    assert reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK", repo=REPO
    )


def test_embedded_plan_closeout_scenario(tmp_path, mocker):
    from hermes_pipeline.todos_completion import reconcile_todo_completion

    repo, _worktree, state, _parent = _registered_repo(tmp_path, embedded=True)
    mocker.patch("hermes_pipeline.todos_completion.get_todo_kanban_tasks", return_value={})

    assert reconcile_todo_completion(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK", repo=REPO
    )


def test_doctor_accepts_valid_embedded_artifact(tmp_path, mocker, capsys):
    from hermes_pipeline.cli import _doctor_active_registration

    repo, _worktree, state, _parent = _registered_repo(tmp_path, embedded=True)
    (state / "current_tick_id.txt").write_text("01TICK\n")
    mocker.patch("hermes_pipeline.github_issues.check_issue_drift", return_value=None)

    assert _doctor_active_registration(repo, state)
    assert "Issue authority: pinned" in capsys.readouterr().out


def test_doctor_surfaces_a_blocked_result_validation(tmp_path, mocker, capsys):
    """``tpo doctor`` is where an operator looks; the stall must be visible there.

    It is a stalled run, not corrupt authority, so the verdict is unchanged.
    """
    from hermes_pipeline.cli import _doctor_active_registration
    from hermes_pipeline.kanban_tasks import RESULT_VALIDATION_BLOCKED_MARKER

    repo, _worktree, state, _parent = _registered_repo(tmp_path, embedded=True)
    (state / "current_tick_id.txt").write_text("01TICK\n")
    (state / "runs" / "01TICK" / RESULT_VALIDATION_BLOCKED_MARKER).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tick_id": "01TICK",
                "step_key": "plan:task-1",
                "code": "worktree_dirty",
                "reason": "worktree_dirty",
            }
        )
        + "\n"
    )
    mocker.patch("hermes_pipeline.github_issues.check_issue_drift", return_value=None)

    assert _doctor_active_registration(repo, state)
    out = capsys.readouterr().out
    assert "RESULT VALIDATION BLOCKED: plan:task-1 worktree_dirty" in out
    assert RESULT_VALIDATION_BLOCKED_MARKER in out


def test_doctor_rejects_embedded_artifact_digest_drift(tmp_path, mocker, capsys):
    from hermes_pipeline.cli import _doctor_active_registration

    repo, _worktree, state, _parent = _registered_repo(tmp_path, embedded=True)
    (state / "current_tick_id.txt").write_text("01TICK\n")
    (state / "runs" / "01TICK" / "plan.md").write_text("drift\n")
    mocker.patch("hermes_pipeline.github_issues.check_issue_drift", return_value=None)

    assert not _doctor_active_registration(repo, state)
    assert "REGISTRATION DRIFT: plan_hash" in capsys.readouterr().out


def test_registration_rejects_schema_v1_payload(tmp_path):
    repo, _worktree, state, _parent = _registered_repo(tmp_path)

    def downgrade(payload):
        payload["schema_version"] = 1
        for key in ("issue_number", "issue_url", "issue_snapshot"):
            del payload[key]

    _rewrite_registration(state, downgrade)
    with pytest.raises(ResultContractError, match="registration_invalid.*schema_version"):
        load_validated_registration(repo, state, "01TICK")


def _retitle(payload):
    payload["issue_snapshot"] = payload["issue_snapshot"].replace("title: Do it", "title: Other")


def _renumber(payload):
    payload["issue_snapshot"] = payload["issue_snapshot"].replace("number: 42", "number: 43")


def _rebody(payload):
    payload["issue_snapshot"] = payload["issue_snapshot"].replace("todo-42", "todo-43")


def _replan(payload):
    payload["issue_snapshot"] = payload["issue_snapshot"].replace("\nplan.md\n", "\nother.md\n")


def _rehash(mutate):
    def apply(payload):
        mutate(payload)
        payload["selected_entry_hash"] = snapshot_hash(payload["issue_snapshot"])

    return apply


def _consistent_renumber(payload):
    _renumber(payload)
    payload["issue_number"] = 43
    payload["issue_url"] = "https://github.com/acme/repo/issues/43"
    payload["selected_entry_hash"] = snapshot_hash(payload["issue_snapshot"])


def _foreign_repo(payload):
    payload["issue_snapshot"] = canonical_issue_snapshot("other/repo", 42, "Do it", ISSUE_BODY)
    payload["issue_url"] = "https://github.com/other/repo/issues/42"
    payload["selected_entry_hash"] = snapshot_hash(payload["issue_snapshot"])


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_renumber, id="tampered-number-line"),
        pytest.param(_rebody, id="tampered-body"),
        pytest.param(_retitle, id="tampered-title"),
        pytest.param(_rehash(_renumber), id="rehashed-number-mismatch"),
        pytest.param(_rehash(_rebody), id="rehashed-branch-from-snapshot"),
        pytest.param(_rehash(_replan), id="rehashed-plan-from-snapshot"),
        pytest.param(_rehash(_retitle), id="rehashed-worktree-slug-from-title"),
        pytest.param(_consistent_renumber, id="todo-id-mismatch"),
        pytest.param(_foreign_repo, id="foreign-repo"),
        pytest.param(
            lambda payload: payload.update(issue_url="https://github.com/acme/repo/issues/7"),
            id="url-mismatch",
        ),
        pytest.param(
            lambda payload: payload.update(issue_snapshot="not a snapshot\n"),
            id="malformed-snapshot",
        ),
    ],
)
def test_registration_rejects_snapshot_tampering(tmp_path, mutate):
    repo, _worktree, state, _parent = _registered_repo(tmp_path)
    _rewrite_registration(state, mutate)

    with pytest.raises(ResultContractError, match="registration_invalid"):
        load_validated_registration(repo, state, "01TICK")


def test_registration_rejects_stale_hash_after_body_edit(tmp_path):
    repo, _worktree, state, _parent = _registered_repo(tmp_path)
    _rewrite_registration(
        state,
        lambda payload: payload.update(
            issue_snapshot=payload["issue_snapshot"].replace(
                "Result contract.", "Result contract!"
            )
        ),
    )

    with pytest.raises(ResultContractError, match="issue snapshot hash"):
        load_validated_registration(repo, state, "01TICK")


def test_registration_bounds_snapshot_size_instead_of_scanning_it(tmp_path):
    repo, _worktree, state, _parent = _registered_repo(
        tmp_path, issue_body=ISSUE_BODY + "\n### Why\n\ntoken: ghp_abcdefghijklmnopqrstuvwxyz\x0c\n"
    )
    assert load_validated_registration(repo, state, "01TICK").issue_number == 42

    def oversize(payload):
        payload["issue_snapshot"] = payload["issue_snapshot"] + "x" * MAX_ISSUE_SNAPSHOT_CHARS
        payload["selected_entry_hash"] = snapshot_hash(payload["issue_snapshot"])

    _rewrite_registration(state, oversize)
    with pytest.raises(ResultContractError, match="registration_invalid"):
        load_validated_registration(repo, state, "01TICK")


def test_registration_repo_identity_is_case_insensitive(tmp_path):
    repo, _worktree, state, _parent = _registered_repo(tmp_path)
    _git(repo, "remote", "set-url", "origin", "git@github.com:ACME/REPO.git")

    assert load_validated_registration(repo, state, "01TICK").issue_number == 42


def test_registration_repo_must_match_live_identity(tmp_path):
    repo, _worktree, state, _parent = _registered_repo(tmp_path)

    load_validated_registration(repo, state, "01TICK", repo=REPO)
    with pytest.raises(ResultContractError, match="registration_invalid"):
        load_validated_registration(repo, state, "01TICK", repo="other/repo")
    _git(repo, "remote", "remove", "origin")
    with pytest.raises(ResultContractError, match="git_verification_failed"):
        load_validated_registration(repo, state, "01TICK")


def _commit(worktree, name: str) -> str:
    (worktree / name).write_text(name)
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-qm", name)
    return _git(worktree, "rev-parse", "HEAD")


def _worker_payload(*, step_key: str, parent: str, head: str, changed: list[str]):
    result = _result(step_key=step_key)
    result["git"] = {
        "expected_parent_sha": parent,
        "resulting_head_sha": head,
        "task_commit_sha": head,
        "changed_files": changed,
    }
    return {"runs": [{"status": "succeeded", "metadata": {"tpo_result": result}}]}


def test_reconcile_completed_worker_validates_without_a_controller_gate(
    tmp_path, mocker
):
    """A validated worker needs no gate card: nothing is completed or blocked."""
    from hermes_pipeline.kanban_tasks import KanbanTaskInfo, reconcile_plan_task_results

    repo, worktree, state, parent = _registered_repo(tmp_path)
    head = _commit(worktree, "change.txt")
    mocker.patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
        return_value={
            "plan:task-1": KanbanTaskInfo("worker", "plan:task-1", "done", "TODO-42"),
        },
    )
    mocker.patch(
        "hermes_pipeline.kanban_tasks._show_task_payload",
        return_value=_worker_payload(
            step_key="plan:task-1", parent=parent, head=head, changed=["change.txt"]
        ),
    )
    complete = mocker.patch(
        "hermes_pipeline.kanban_tasks.complete_todo_kanban_task", return_value=True
    )
    blocked = mocker.patch("hermes_pipeline.kanban_tasks._mark_gate_needs_input")

    for _ in range(2):  # Reconciliation is idempotent across ticks.
        assert reconcile_plan_task_results(
            project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
        )
    complete.assert_not_called()
    blocked.assert_not_called()


def test_reconcile_verifies_earlier_tasks_by_topology_and_the_tip_against_head(
    tmp_path, mocker
):
    """Only the chain tip is checked against current HEAD and a clean worktree.

    A task a later task already built on cannot be HEAD any more, so re-running
    the full check against it would fail after every resume.
    """
    from hermes_pipeline.kanban_tasks import KanbanTaskInfo, reconcile_plan_task_results

    repo, worktree, state, base = _registered_repo(
        tmp_path, plan=PLAN_TWO_TASKS, step_keys=("plan:task-1", "plan:task-2")
    )
    first = _commit(worktree, "one.txt")
    second = _commit(worktree, "two.txt")
    payloads = {
        "worker-1": _worker_payload(
            step_key="plan:task-1", parent=base, head=first, changed=["one.txt"]
        ),
        "worker-2": _worker_payload(
            step_key="plan:task-2", parent=first, head=second, changed=["two.txt"]
        ),
    }
    mocker.patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
        return_value={
            "plan:task-1": KanbanTaskInfo("worker-1", "plan:task-1", "done", "TODO-42"),
            "plan:task-2": KanbanTaskInfo("worker-2", "plan:task-2", "done", "TODO-42"),
        },
    )
    mocker.patch(
        "hermes_pipeline.kanban_tasks._show_task_payload",
        side_effect=lambda task_id: payloads[task_id],
    )

    assert reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
    )

    (worktree / "stray.txt").write_text("uncommitted")
    assert not reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
    )


def test_reconcile_falls_back_to_topology_once_review_builds_on_the_chain(
    tmp_path, mocker
):
    """Review-fix commits advance HEAD; the chain must stay reconcilable."""
    from hermes_pipeline.kanban_tasks import KanbanTaskInfo, reconcile_plan_task_results

    repo, worktree, state, base = _registered_repo(tmp_path)
    head = _commit(worktree, "change.txt")
    mocker.patch(
        "hermes_pipeline.kanban_tasks._show_task_payload",
        return_value=_worker_payload(
            step_key="plan:task-1", parent=base, head=head, changed=["change.txt"]
        ),
    )
    board = {
        "plan:task-1": KanbanTaskInfo("worker", "plan:task-1", "done", "TODO-42"),
    }
    tasks = mocker.patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
        side_effect=lambda *_args: board,
    )
    _commit(worktree, "review-fix.txt")

    assert not reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
    )

    board["review:0"] = KanbanTaskInfo("review", "review:0", "done", "TODO-42")
    assert reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    assert tasks.called


def test_reconcile_completes_legacy_validate_gates_still_named_by_registration(
    tmp_path, mocker
):
    """A run registered before the gate was dropped must still settle its gates.

    ``poll_pinned_run`` waits for every registered step key, so a resumed run
    whose ``step_keys`` still name ``validate:<id>`` would hang forever.
    """
    from hermes_pipeline.kanban_tasks import KanbanTaskInfo, reconcile_plan_task_results

    repo, worktree, state, parent = _registered_repo(
        tmp_path, step_keys=("plan:task-1", "validate:task-1")
    )
    head = _commit(worktree, "change.txt")
    gate = KanbanTaskInfo("gate", "validate:task-1", "blocked", "TODO-42")
    board = {
        "plan:task-1": KanbanTaskInfo("worker", "plan:task-1", "done", "TODO-42"),
        "validate:task-1": gate,
    }
    mocker.patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
        side_effect=lambda *_args: board,
    )
    mocker.patch(
        "hermes_pipeline.kanban_tasks._show_task_payload",
        return_value=_worker_payload(
            step_key="plan:task-1", parent=parent, head=head, changed=["change.txt"]
        ),
    )
    complete = mocker.patch(
        "hermes_pipeline.kanban_tasks.complete_todo_kanban_task", return_value=True
    )

    assert reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    complete.assert_called_once_with("demo", "gate")

    board["validate:task-1"] = KanbanTaskInfo(
        "gate", "validate:task-1", "done", "TODO-42"
    )
    complete.reset_mock()
    assert reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    complete.assert_not_called()


def test_topology_rejects_a_commit_no_longer_reachable_from_head(tmp_path):
    """Parentage, count and changed files all still hold for a discarded commit.

    ``git reset --hard`` leaves the object in the repository, so every immutable
    topology fact keeps passing; only reachability proves the work is on the
    branch the run is delivering.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    parent = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "change.txt")
    _git(repo, "reset", "--hard", "-q", parent)
    git = parse_worker_result(
        _worker_payload(
            step_key="plan:task-1", parent=parent, head=head, changed=["change.txt"]
        ),
        tick_id="01TICK", todo_id="TODO-42", step_key="plan:task-1",
        acceptance_criteria=("Observable criterion",),
    ).git

    with pytest.raises(ResultContractError, match="unreachable_commit"):
        verify_worker_git_topology(repo, git, expected_parent_sha=parent)


def test_reconcile_rejects_a_discarded_commit_even_when_a_decoy_review_card_exists(
    tmp_path, mocker, caplog
):
    """A board card can be forged by a worker that knows its own tick id.

    ``review:0`` only decides which *extra* checks apply; the ancestry anchor is
    unconditional, so a decoy cannot strip verification off a task.
    """
    from hermes_pipeline.kanban_tasks import KanbanTaskInfo, reconcile_plan_task_results

    repo, worktree, state, parent = _registered_repo(tmp_path)
    head = _commit(worktree, "change.txt")
    _git(worktree, "reset", "--hard", "-q", parent)
    mocker.patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
        return_value={
            "plan:task-1": KanbanTaskInfo("worker", "plan:task-1", "done", "TODO-42"),
            "review:0": KanbanTaskInfo("decoy", "review:0", "done", "TODO-42"),
        },
    )
    mocker.patch(
        "hermes_pipeline.kanban_tasks._show_task_payload",
        return_value=_worker_payload(
            step_key="plan:task-1", parent=parent, head=head, changed=["change.txt"]
        ),
    )

    with caplog.at_level(logging.ERROR, logger="hermes_pipeline.kanban_tasks"):
        assert not reconcile_plan_task_results(
            project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
        )

    assert "unreachable_commit" in caplog.text


def test_reconcile_records_a_durable_blocked_marker_and_clears_it_on_success(
    tmp_path, mocker
):
    """Plain cron ``tpo tick`` has no budget: the stall must leave evidence."""
    from hermes_pipeline.kanban_tasks import (
        RESULT_VALIDATION_BLOCKED_MARKER,
        KanbanTaskInfo,
        reconcile_plan_task_results,
    )

    repo, worktree, state, parent = _registered_repo(tmp_path)
    head = _commit(worktree, "change.txt")
    board = {"plan:task-1": KanbanTaskInfo("worker", "plan:task-1", "done", "TODO-42")}
    mocker.patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
        side_effect=lambda *_args: board,
    )
    payload = mocker.patch(
        "hermes_pipeline.kanban_tasks._show_task_payload", return_value={"runs": []}
    )
    marker = state / "runs" / "01TICK" / RESULT_VALIDATION_BLOCKED_MARKER

    for _ in range(2):  # Repeated no-progress ticks converge on one marker.
        assert not reconcile_plan_task_results(
            project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
        )
    recorded = json.loads(marker.read_text())
    assert recorded["step_key"] == "plan:task-1"
    assert recorded["code"] == "missing_successful_run"
    assert recorded["tick_id"] == "01TICK"
    assert isinstance(recorded["reason"], str) and recorded["reason"]

    payload.return_value = _worker_payload(
        step_key="plan:task-1", parent=parent, head=head, changed=["change.txt"]
    )
    assert reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    assert not marker.exists()


def test_git_predicate_reports_a_real_git_failure_rather_than_false(tmp_path):
    """Exit 1 is "no"; anything above it is a broken git, not an answer.

    Collapsing the two would let an unusable repository read as a clean
    non-ancestor verdict, which is the wrong direction to fail in.
    """
    from hermes_pipeline.result_contract import _git_predicate

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    head = _git(repo, "rev-parse", "HEAD")

    assert _git_predicate(repo, "merge-base", "--is-ancestor", head, "HEAD") is True

    with pytest.raises(ResultContractError, match="git_verification_failed"):
        _git_predicate(repo, "merge-base", "--is-ancestor", "0" * 40, "HEAD")


def test_reconcile_records_a_structural_marker_when_the_chain_is_not_wired(
    tmp_path, mocker
):
    """A missing card stalls the run exactly like a rejected result.

    The code has to say which, or an operator cannot tell a wiring problem from
    work TPO refused.
    """
    from hermes_pipeline.kanban_tasks import (
        CHAIN_WIRING_INCOMPLETE_CODE,
        KanbanTaskInfo,
        reconcile_plan_task_results,
    )

    repo, _worktree, state, _parent = _registered_repo(
        tmp_path, step_keys=("plan:task-1", "validate:task-1")
    )
    marker = state / "runs" / "01TICK" / "result-validation-blocked"
    board: dict[str, object] = {}
    mocker.patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
        side_effect=lambda *_args: board,
    )

    # No worker card on the board at all.
    assert not reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    recorded = json.loads(marker.read_text())
    assert recorded["code"] == CHAIN_WIRING_INCOMPLETE_CODE
    assert recorded["step_key"] == "plan:task-1"

    # Worker done, but the legacy gate this registration still names is missing.
    board["plan:task-1"] = KanbanTaskInfo("worker", "plan:task-1", "done", "TODO-42")
    marker.unlink()
    assert not reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    recorded = json.loads(marker.read_text())
    assert recorded["code"] == CHAIN_WIRING_INCOMPLETE_CODE
    assert recorded["step_key"] == "validate:task-1"


def test_reconcile_records_a_marker_when_a_registered_step_key_is_absent(
    tmp_path, mocker
):
    from hermes_pipeline.kanban_tasks import (
        CHAIN_WIRING_INCOMPLETE_CODE,
        reconcile_plan_task_results,
    )

    repo, worktree, state, parent = _registered_repo(tmp_path)
    mocker.patch(
        "hermes_pipeline.result_contract.load_validated_registration",
        return_value=SimpleNamespace(
            manifest=SimpleNamespace(tasks=(SimpleNamespace(id="task-1"),)),
            step_keys=("phase_4_development",),
            base_sha=parent,
            todo_id="TODO-42",
            worktree=worktree,
        ),
    )
    tasks = mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_tasks")

    assert not reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    recorded = json.loads(
        (state / "runs" / "01TICK" / "result-validation-blocked").read_text()
    )
    assert recorded["code"] == CHAIN_WIRING_INCOMPLETE_CODE
    assert recorded["step_key"] == "plan:task-1"
    assert tasks.called


def test_reconcile_records_a_marker_when_a_legacy_gate_cannot_be_completed(
    tmp_path, mocker
):
    from hermes_pipeline.kanban_tasks import (
        GATE_COMPLETION_FAILED_CODE,
        KanbanTaskInfo,
        reconcile_plan_task_results,
    )

    repo, worktree, state, parent = _registered_repo(
        tmp_path, step_keys=("plan:task-1", "validate:task-1")
    )
    head = _commit(worktree, "change.txt")
    mocker.patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
        return_value={
            "plan:task-1": KanbanTaskInfo("worker", "plan:task-1", "done", "TODO-42"),
            "validate:task-1": KanbanTaskInfo(
                "gate", "validate:task-1", "blocked", "TODO-42"
            ),
        },
    )
    mocker.patch(
        "hermes_pipeline.kanban_tasks._show_task_payload",
        return_value=_worker_payload(
            step_key="plan:task-1", parent=parent, head=head, changed=["change.txt"]
        ),
    )
    mocker.patch(
        "hermes_pipeline.kanban_tasks.complete_todo_kanban_task", return_value=False
    )

    assert not reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    recorded = json.loads(
        (state / "runs" / "01TICK" / "result-validation-blocked").read_text()
    )
    assert recorded["code"] == GATE_COMPLETION_FAILED_CODE
    assert recorded["step_key"] == "validate:task-1"


def test_reconcile_invalid_result_reports_no_progress_without_a_blocking_card(
    tmp_path, mocker, caplog
):
    from hermes_pipeline.kanban_tasks import KanbanTaskInfo, reconcile_plan_task_results

    repo, _worktree, state, _parent = _registered_repo(tmp_path)
    mocker.patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
        return_value={
            "plan:task-1": KanbanTaskInfo("worker", "plan:task-1", "done", "TODO-42"),
        },
    )
    mocker.patch("hermes_pipeline.kanban_tasks._show_task_payload", return_value={"runs": []})
    blocked = mocker.patch("hermes_pipeline.kanban_tasks._mark_gate_needs_input")
    complete = mocker.patch("hermes_pipeline.kanban_tasks.complete_todo_kanban_task")

    with caplog.at_level(logging.ERROR, logger="hermes_pipeline.kanban_tasks"):
        assert not reconcile_plan_task_results(
            project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
        )

    assert "TPO result validation failed" in caplog.text
    assert "plan:task-1" in caplog.text
    blocked.assert_not_called()
    complete.assert_not_called()


def test_legacy_registration_bypasses_manifest_only_reconciliation(tmp_path, mocker):
    from hermes_pipeline.kanban_tasks import reconcile_plan_task_results
    from hermes_pipeline.review_reconciliation import reconcile_reviews
    from hermes_pipeline.run_registration import register_pinned_run
    from hermes_pipeline.todos_completion import reconcile_todo_completion

    repo = tmp_path / "legacy"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "remote", "add", "origin", f"https://github.com/{REPO}")
    (repo / "plan.md").write_text("# Legacy Plan\n\nImplement it.\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    state = repo / ".hermes"
    registration = register_pinned_run(
        project_dir=repo,
        state_dir=state,
        tick_id="LEGACY-TICK",
        selected_issue=make_issue(
            7,
            repo=REPO,
            title="Legacy work",
            body="### Plan\n\nplan.md\n\n### Branch\n\ntodo-7\n",
        ),
        plan_path="plan.md",
        profile="native-sdd",
        prompt_client="claude",
        assignee="pipeline",
        review_assignee=None,
        step_keys=(
            "phase_4_development",
            "phase_5_review",
            "phase_8_finish_branch",
            "phase_9_human_review",
        ),
    )

    authority = load_validated_registration(repo, state, "LEGACY-TICK")
    assert authority.manifest is None
    kanban = mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_tasks")
    review = mocker.patch("hermes_pipeline.review_reconciliation.get_todo_kanban_tasks")
    delivery = mocker.patch("hermes_pipeline.todos_completion.get_todo_kanban_tasks")

    assert reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="legacy", tick_id="LEGACY-TICK"
    )
    assert reconcile_reviews(
        project_dir=repo, state_dir=state, tenant="legacy", tick_id="LEGACY-TICK"
    )
    assert reconcile_todo_completion(
        project_dir=repo, state_dir=state, tenant="legacy", tick_id="LEGACY-TICK", repo=REPO
    )
    kanban.assert_not_called()
    review.assert_not_called()
    delivery.assert_not_called()
    assert registration.worktree.is_dir()

    registration_path = state / "runs" / "LEGACY-TICK" / "registration.json"
    drifted = json.loads(registration_path.read_text())
    drifted["plan_hash"] = "0" * 64
    registration_path.write_text(json.dumps(drifted))
    with pytest.raises(ResultContractError, match="registration_invalid"):
        load_validated_registration(repo, state, "LEGACY-TICK")


# --- Published result-metadata template -------------------------------------
#
# The template is the only thing a worker is told about the contract, so these
# tests round-trip every rendered template through the real parser and assert
# the published keys are derived from the contract constants themselves.

_JSON_BLOCK_RE = re.compile(r"```json\n(.*?)\n```", re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"^<.*>$")

_TEMPLATE_FILL = {
    ("external_session_id",): "session-1",
    ("review", "findings", 0, "priority"): "P1",
    ("git", "expected_parent_sha"): "a" * 40,
    ("git", "resulting_head_sha"): "b" * 40,
    ("git", "task_commit_sha"): "b" * 40,
    ("git", "changed_files", 0): "src/example.py",
    ("tdd", "red", "command"): "uv run pytest tests/test_example.py",
    ("tdd", "green", "command"): "uv run pytest tests/test_example.py",
    ("tdd", "refactor", "command"): "uv run pytest tests/test_example.py",
    ("review", "findings", 0, "location"): "src/example.py:12",
    ("review", "findings", 0, "failure_scenario"): "A resumed tick closes the card twice.",
    ("review", "findings", 0, "recommendation"): "Guard the close with the run marker.",
    ("delivery", "pr_url"): "https://github.com/acme/repo/pull/7",
    ("delivery", "checks", 0, "command"): "uv run pytest",
}


def _template_blocks(text: str) -> list[dict]:
    blocks = [json.loads(block) for block in _JSON_BLOCK_RE.findall(text)]
    assert blocks, f"template published no JSON block: {text}"
    return blocks


def _fill_template(value, path=()):
    """Substitute worker-supplied values for every ``<...>`` placeholder."""
    if isinstance(value, dict):
        return {key: _fill_template(item, (*path, key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill_template(item, (*path, index)) for index, item in enumerate(value)]
    if isinstance(value, str) and _PLACEHOLDER_RE.match(value):
        assert path in _TEMPLATE_FILL, f"unfillable placeholder at {path}: {value}"
        return _TEMPLATE_FILL[path]
    return value


def _template_envelope(result: dict) -> dict:
    return {"runs": [{"status": "succeeded", "metadata": {"tpo_result": result}}]}


def test_plan_template_round_trips_through_the_parser():
    from hermes_pipeline.result_contract import render_result_template

    text = render_result_template(
        tick_id="01TICK",
        todo_id="TODO-42",
        step_key="plan:task-1",
        acceptance_criteria=("Observable criterion",),
    )
    template, = _template_blocks(text)

    parsed = parse_worker_result(
        _template_envelope(_fill_template(template)),
        tick_id="01TICK",
        todo_id="TODO-42",
        step_key="plan:task-1",
        acceptance_criteria=("Observable criterion",),
    )

    assert parsed.git.changed_files == ("src/example.py",)
    assert parsed.red.exit_code != 0
    assert (parsed.green.exit_code, parsed.refactor.exit_code) == (0, 0)
    # The criteria are pipeline-known facts, so the template pre-fills them.
    assert "<" not in json.dumps(template["acceptance"])
    assert "do not report a result object" in text


def test_review_template_round_trips_clean_and_findings_verdicts():
    from hermes_pipeline.result_contract import (
        render_result_template,
        verify_read_only_review,
    )

    head = "c" * 40
    text = render_result_template(
        tick_id="01TICK",
        todo_id="TODO-42",
        step_key="review:0",
        section="review",
        pinned_head_sha=head,
        allow_no_changes=True,
    )
    template, findings_variant = _template_blocks(text)

    clean = parse_worker_result(
        _template_envelope(_fill_template(template)),
        tick_id="01TICK", todo_id="TODO-42", step_key="review:0",
        acceptance_criteria=(), allow_no_changes=True,
    )
    assert clean.review is not None and clean.review.verdict == "clean"
    verify_read_only_review(
        Path("."), clean, head_sha=head, require_current=False
    )

    spliced = dict(template)
    spliced["review"] = findings_variant
    reported = parse_worker_result(
        _template_envelope(_fill_template(spliced)),
        tick_id="01TICK", todo_id="TODO-42", step_key="review:0",
        acceptance_criteria=(), allow_no_changes=True,
    )
    assert reported.review is not None
    assert reported.review.verdict == "findings"
    assert reported.review.findings[0]["priority"] in _FINDING_PRIORITIES


def test_delivery_template_round_trips_through_the_parser():
    from hermes_pipeline.result_contract import render_result_template

    head = "b" * 40
    text = render_result_template(
        tick_id="01TICK",
        todo_id="TODO-42",
        step_key="finish",
        section="delivery",
        pinned_head_sha=head,
        branch="todo-42",
        allow_no_changes=True,
    )
    template, = _template_blocks(text)

    parsed = parse_worker_result(
        _template_envelope(_fill_template(template)),
        tick_id="01TICK", todo_id="TODO-42", step_key="finish",
        acceptance_criteria=(), allow_no_changes=True,
    )

    assert parsed.delivery is not None
    # Delivery reconciliation demands the branch and the reviewed head verbatim.
    assert parsed.delivery.branch == "todo-42"
    assert parsed.delivery.head_sha == parsed.git.resulting_head_sha == head
    assert parsed.git.changed_files == ()


def test_template_publishes_every_key_the_contract_constants_require():
    from hermes_pipeline import result_contract as contract

    plan, = _template_blocks(
        contract.render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="plan:task-1",
            acceptance_criteria=("Observable criterion",),
        )
    )
    assert set(plan) == contract._TOP_KEYS
    assert set(plan["git"]) == contract._GIT_KEYS
    assert set(plan["tdd"]) == contract._TDD_KEYS
    for phase in contract._TDD_KEYS:
        assert set(plan["tdd"][phase]) == contract._COMMAND_KEYS
    assert set(plan["acceptance"][0]) == contract._ACCEPTANCE_ENTRY_KEYS

    review, findings_variant = _template_blocks(
        contract.render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="review:0",
            section="review", pinned_head_sha="c" * 40, allow_no_changes=True,
        )
    )
    assert set(review) == contract._TOP_KEYS | {"review"}
    assert set(review["review"]) == contract._REVIEW_KEYS
    assert set(findings_variant) == contract._REVIEW_KEYS
    assert set(findings_variant["findings"][0]) == contract._FINDING_KEYS

    delivery, = _template_blocks(
        contract.render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="finish",
            section="delivery", pinned_head_sha="b" * 40, branch="todo-42",
            allow_no_changes=True,
        )
    )
    assert set(delivery) == contract._TOP_KEYS | {"delivery"}
    assert set(delivery["delivery"]) == contract._DELIVERY_KEYS
    assert set(delivery["delivery"]["checks"][0]) == contract._COMMAND_KEYS


def test_template_rejects_an_unknown_optional_section():
    from hermes_pipeline.result_contract import render_result_template

    with pytest.raises(ResultContractError, match="unknown_result_section"):
        render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="finish", section="nope",
        )


def test_review_body_never_reads_as_constraining_the_review_verdict():
    """The stall this template exists to prevent must not be re-encoded in prose."""
    from hermes_pipeline.result_contract import render_result_template

    head = "c" * 40
    text = render_result_template(
        tick_id="01TICK", todo_id="TODO-42", step_key="review:0", section="review",
        pinned_head_sha=head, allow_no_changes=True,
    )
    template, findings_variant = _template_blocks(text)

    # A reviewer that reads an unqualified verdict sentence as covering the
    # review sub-object stalls the run exactly as the live incident did.
    misread = _fill_template(template)
    misread["review"] = dict(findings_variant, verdict="success")
    with pytest.raises(ResultContractError, match="invalid_review"):
        parse_worker_result(
            _template_envelope(_fill_template(misread)),
            tick_id="01TICK", todo_id="TODO-42", step_key="review:0",
            acceptance_criteria=(), allow_no_changes=True,
        )

    for sentence in text.split(". "):
        if '"verdict" accepts only' in sentence:
            assert "top-level" in sentence, sentence


def test_parser_rejects_unfilled_template_placeholders():
    from hermes_pipeline.result_contract import render_result_template

    template, = _template_blocks(
        render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="plan:task-1",
            acceptance_criteria=("Observable criterion",),
        )
    )
    for path, code in (
        (("external_session_id",), "invalid_session"),
        (("tdd", "red", "command"), "invalid_tdd"),
        (("git", "changed_files"), "invalid_git"),
    ):
        result = _fill_template(template)
        target = result
        for key in path[:-1]:
            target = target[key]
        # Restore the published placeholder for exactly one field.
        published = template
        for key in path:
            published = published[key]
        target[path[-1]] = published
        with pytest.raises(ResultContractError, match=code):
            parse_worker_result(
                _template_envelope(result),
                tick_id="01TICK", todo_id="TODO-42", step_key="plan:task-1",
                acceptance_criteria=("Observable criterion",),
            )


def test_bounded_string_allows_a_real_value_containing_angle_brackets():
    from hermes_pipeline.result_contract import _bounded_string

    assert _bounded_string(
        "uv run pytest -k 'a<b'", maximum=100, code="invalid_tdd"
    ) == "uv run pytest -k 'a<b'"


def test_read_only_cards_need_no_invented_tdd_evidence():
    """A pinned review or finish card is closable by filling the session id alone."""
    from hermes_pipeline.result_contract import render_result_template

    for kwargs, step_key in (
        ({"section": "review", "pinned_head_sha": "c" * 40}, "review:0"),
        (
            {"section": "delivery", "pinned_head_sha": "b" * 40, "branch": "todo-42"},
            "finish",
        ),
    ):
        text = render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key=step_key,
            allow_no_changes=True, **kwargs,
        )
        template = _template_blocks(text)[0]
        assert "<" not in json.dumps(template["tdd"])
        assert "red.exit_code" not in text
        filled = _fill_template(template)
        remaining = [
            value for value in json.dumps(filled).split('"')
            if value.startswith("<") and value.endswith(">")
        ]
        assert remaining == []
        parsed = parse_worker_result(
            _template_envelope(filled),
            tick_id="01TICK", todo_id="TODO-42", step_key=step_key,
            acceptance_criteria=(), allow_no_changes=True,
        )
        assert parsed.external_session_id == _TEMPLATE_FILL[("external_session_id",)]


def test_delivery_template_requires_the_pipeline_known_branch_and_head():
    from hermes_pipeline.result_contract import render_result_template

    with pytest.raises(ResultContractError, match="incomplete_result_section"):
        render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="finish", section="delivery",
            allow_no_changes=True,
        )


def test_review_body_states_the_findings_substitution_before_other_instructions():
    """A dispatcher obeying "exactly, never paraphrase" must not publish clean."""
    from hermes_pipeline.result_contract import render_result_template

    text = render_result_template(
        tick_id="01TICK", todo_id="TODO-42", step_key="review:0", section="review",
        pinned_head_sha="c" * 40, allow_no_changes=True,
    )
    main_fence_end = text.index("```", text.index("```json") + len("```json"))
    substitution = text.index('replace the whole "review" value')

    assert main_fence_end < substitution
    for later in (
        "must not change the worktree",
        "runs no TDD cycle",
        'top-level "verdict"',
    ):
        assert substitution < text.index(later), later
    # Both fences must name which review verdict they carry.
    assert 'publish that second object as "review"' in text
    assert "defect-free" in text


def test_summary_is_not_a_template_field_and_accepts_bracketed_text():
    result = _result()
    parsed = parse_worker_result(
        {"runs": [{"status": "succeeded", "summary": "<none>",
                   "metadata": {"tpo_result": result}}]},
        tick_id="01TICK", todo_id="TODO-42", step_key="plan:task-1",
        acceptance_criteria=("Observable criterion",),
    )
    assert parsed.external_session_id == "session-1"


def test_placeholder_rejection_never_swallows_a_real_bracketed_finding():
    from hermes_pipeline.result_contract import render_result_template

    text = render_result_template(
        tick_id="01TICK", todo_id="TODO-42", step_key="review:0", section="review",
        pinned_head_sha="c" * 40, allow_no_changes=True,
    )
    template, findings_variant = _template_blocks(text)
    reported = dict(template)
    reported["review"] = findings_variant
    filled = _fill_template(reported)
    filled["review"]["findings"][0]["failure_scenario"] = (
        "<script> tags render unescaped, e.g. <img onerror=x>"
    )

    parsed = parse_worker_result(
        _template_envelope(filled),
        tick_id="01TICK", todo_id="TODO-42", step_key="review:0",
        acceptance_criteria=(), allow_no_changes=True,
    )

    assert parsed.review is not None
    assert parsed.review.findings[0]["failure_scenario"].startswith("<script>")


def test_every_rendered_placeholder_is_caught_by_the_placeholder_rule():
    from hermes_pipeline.result_contract import _PLACEHOLDER_RE, render_result_template

    texts = (
        render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="plan:task-1",
            acceptance_criteria=("Observable criterion",),
        ),
        render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="review:0", section="review",
            pinned_head_sha="c" * 40, allow_no_changes=True,
        ),
        render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="finish", section="delivery",
            pinned_head_sha="b" * 40, branch="todo-42", allow_no_changes=True,
        ),
    )
    seen = 0
    for text in texts:
        for block in _template_blocks(text):
            for value in re.findall(r'"(<[^"]*>)"', json.dumps(block)):
                assert _PLACEHOLDER_RE.fullmatch(value), value
                seen += 1
    assert seen >= 10


def test_read_only_tdd_exit_code_reads_as_nothing_ran():
    from hermes_pipeline.result_contract import render_result_template

    template, _variant = _template_blocks(
        render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="review:0", section="review",
            pinned_head_sha="c" * 40, allow_no_changes=True,
        )
    )

    assert template["tdd"]["red"]["exit_code"] == 127


def _all_templates():
    from hermes_pipeline.result_contract import render_result_template

    return (
        render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="plan:task-1",
            acceptance_criteria=("Observable criterion",),
        ),
        render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="review:0", section="review",
            pinned_head_sha="c" * 40, allow_no_changes=True,
        ),
        render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="finish", section="delivery",
            pinned_head_sha="b" * 40, branch="todo-42", allow_no_changes=True,
        ),
    )


def test_template_asks_only_for_what_the_external_client_can_do():
    """The template is passed verbatim to a client with no Kanban tools."""
    kanban_only = ("close the card", "closing the card", "kanban_close",
                   "kanban_block", "kanban_comment")
    for text in _all_templates():
        lowered = text.lower()
        for phrase in kanban_only:
            assert phrase not in lowered, phrase
        # It must still say who closes the card with the reported object.
        assert "dispatcher" in lowered


def test_findings_priority_is_chosen_by_the_reviewer_not_pre_filled():
    from hermes_pipeline.result_contract import _FINDING_PRIORITIES

    _template, findings_variant = _template_blocks(_all_templates()[1])
    priority = findings_variant["findings"][0]["priority"]

    assert priority.startswith("<") and priority.endswith(">")
    for allowed in _FINDING_PRIORITIES:
        assert allowed in priority


def test_delivery_body_demands_every_gate_it_ran():
    text = _all_templates()[2]

    assert "every required gate" in text


def test_review_substitution_is_scoped_to_defects_still_present():
    text = _all_templates()[1]

    assert "still present" in text
    assert "any defect at all" not in text
