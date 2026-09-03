from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_pipeline.todos_create import (
    TodoCreateError,
    create_lock,
    execute_create,
    load_create_request,
    render_create_body,
)
from tests.gh_fakes import API_ARGV, FakeGh, issue_payload

FIELDS = {
    "Summary": "Ship it",
    "What": "Build it",
    "Why": "Users need it",
    "Pros": "Faster",
    "Cons": "Risk",
    "Context": "None",
    "Assumptions": "None",
    "Spec": "docs/spec.md",
    "Reference": "README.md",
    "Branch": "feat/embed",
    "Priority": "P1",
    "Effort": "M",
    "Phase": "4 (Development)",
    "Test Coverage": "required",
    "Security Review": "required",
    "UI Review": "not-required",
}


def request() -> dict:
    return {
        "schema_version": 1,
        "transaction_id": "12345678-1234-4234-9234-123456789abc",
        "title": "Embed implementation plan",
        "fields": FIELDS,
        "plan_markdown": "# Implementation Plan\n\nDo the work.\n",
        "tasks": [{
            "id": "task-1", "title": "Implement", "instructions": "Implement safely",
            "acceptance_criteria": ["Works"], "verification": ["uv run pytest"],
            "commit_message": "feat: implement",
        }],
    }


def write_request(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def test_request_renders_marker_fields_and_canonical_embedded_manifest(tmp_path):
    path = tmp_path / "request.json"
    write_request(path, request())
    loaded = load_create_request(path)
    body = render_create_body(loaded, issue_number=42)
    assert body.startswith("<!-- tpo-create:12345678-1234-4234-9234-123456789abc -->\n")
    assert "### Plan" not in body
    assert body.endswith("---\n</details>\n")
    assert '  "todo_id": "TODO-42"' in body
    assert body.count("```json tpo-plan") == 1


@pytest.mark.parametrize("mutation, code", [
    (lambda p: p.update(extra=True), "unknown_keys"),
    (lambda p: p.update(transaction_id="12345678-1234-1234-9234-123456789abc"), "transaction_id"),
    (lambda p: p["fields"].update(Plan="x"), "invalid_fields"),
    (lambda p: p["fields"].update(Branch="-unsafe"), "unsafe_branch"),
    (lambda p: p.update(plan_markdown="<proposed_plan>x</proposed_plan>"), "forbidden_plan_structure"),
    (lambda p: p.update(plan_markdown="<!-- tpo-create:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa -->"), "forbidden_plan_structure"),
    (lambda p: p.update(plan_markdown="<DeTaIlS>hidden</DETAILS>"), "forbidden_plan_structure"),
    (lambda p: p.update(plan_markdown="<SUMMARY>Implementation Plan</SUMMARY>"), "forbidden_plan_structure"),
])
def test_request_rejects_invalid_contract(tmp_path, mutation, code):
    payload = request()
    payload["fields"] = dict(payload["fields"])
    mutation(payload)
    path = tmp_path / "request.json"
    write_request(path, payload)
    with pytest.raises(TodoCreateError, match=code):
        load_create_request(path)


@pytest.mark.parametrize("field", ["id", "title", "instructions", "acceptance_criteria", "verification", "commit_message"])
def test_request_rejects_controls_in_every_task_string(tmp_path, field):
    payload = request()
    task = payload["tasks"][0]
    if isinstance(task[field], list):
        task[field][0] += "\x7f"
    else:
        task[field] += "\x7f"

    with pytest.raises(TodoCreateError, match="invalid_task"):
        load_create_request(_write(tmp_path / "request.json", payload))


def test_duplicate_json_key_is_rejected(tmp_path):
    path = tmp_path / "request.json"
    path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(TodoCreateError, match="duplicate_json_key"):
        load_create_request(path)


def test_request_load_rejects_symlinks_and_public_modes(tmp_path):
    path = tmp_path / "request.json"
    write_request(path, request())
    path.chmod(0o644)
    with pytest.raises(TodoCreateError, match="request_mode"):
        load_create_request(path)
    path.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(TodoCreateError, match="invalid_request_file"):
        load_create_request(link)


@pytest.mark.parametrize(
    "plan_markdown, code",
    [
        ("<proposed_plan># Draft</proposed_plan>", "forbidden_plan_structure"),
        ("Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz123456", "secret_content"),
    ],
)
def test_request_rejects_draft_and_secret_fixtures(tmp_path, plan_markdown, code):
    payload = request()
    payload["plan_markdown"] = plan_markdown
    with pytest.raises(TodoCreateError, match=code):
        load_create_request(_write(tmp_path / "request.json", payload))


def test_persisted_request_is_private(tmp_path):
    from hermes_pipeline.todos_create import persist_create_request

    loaded = load_create_request(_write(tmp_path / "request.json", request()))
    persisted = persist_create_request(tmp_path / "state", loaded)
    assert stat.S_IMODE(persisted.stat().st_mode) == 0o600


def test_execute_create_orders_ready_before_triage_removal_and_cleans_request(tmp_path, mocker):
    from hermes_pipeline import github_issues
    from hermes_pipeline.todos_create import execute_create

    loaded = load_create_request(_write(tmp_path / "request.json", request()))
    body = render_create_body(loaded, issue_number=42)
    initial = SimpleNamespace(number=42, title=loaded.title, body=body, state="open", labels=("tpo:todo", "needs-triage"))
    complete = SimpleNamespace(number=42, title=loaded.title, body=body, state="open", labels=("tpo:todo", "ready-for-agent", "priority:P1"))
    mocker.patch.object(github_issues, "repository_identity", return_value="acme/repo")
    mocker.patch.object(github_issues, "list_all_issues", return_value=(initial,))
    mocker.patch.object(
        github_issues, "fetch_issue", side_effect=[initial, initial, initial, initial, complete]
    )
    mocker.patch("subprocess.run", return_value=SimpleNamespace(returncode=0))
    add = mocker.patch.object(github_issues, "add_label")
    remove = mocker.patch.object(github_issues, "remove_label")
    mocker.patch("hermes_pipeline.cli._audit_phase_options", return_value=())
    mocker.patch("hermes_pipeline.cli._audit_default_branch", return_value="main")
    mocker.patch(
        "hermes_pipeline.cli._audit_issue",
        side_effect=[(["label:missing:priority:P1"], ["priority:P1"], []), ([], [], [])],
    )
    number = execute_create(
        tmp_path, tmp_path / "state", loaded, approved_repo="acme/repo"
    )
    assert number == 42
    assert [call.args[2] for call in add.call_args_list] == ["priority:P1", "ready-for-agent"]
    assert remove.call_args.args[2] == "needs-triage"
    assert not (tmp_path / "state" / "todo-create" / f"{loaded.transaction_id}.json").exists()


def test_execute_create_retains_request_after_label_failure(tmp_path, mocker):
    from hermes_pipeline import github_issues
    from hermes_pipeline.todos_create import execute_create

    loaded = load_create_request(_write(tmp_path / "request.json", request()))
    body = render_create_body(loaded, issue_number=42)
    issue = SimpleNamespace(number=42, title=loaded.title, body=body, state="open", labels=("tpo:todo", "needs-triage"))
    mocker.patch.object(github_issues, "repository_identity", return_value="acme/repo")
    mocker.patch.object(github_issues, "list_all_issues", return_value=(issue,))
    mocker.patch.object(github_issues, "fetch_issue", return_value=issue)
    mocker.patch("subprocess.run", return_value=SimpleNamespace(returncode=0))
    mocker.patch.object(github_issues, "add_label", side_effect=RuntimeError("failed"))
    mocker.patch("hermes_pipeline.cli._audit_phase_options", return_value=())
    mocker.patch("hermes_pipeline.cli._audit_default_branch", return_value="main")
    mocker.patch("hermes_pipeline.cli._audit_issue", return_value=(["label:missing:priority:P1"], ["priority:P1"], []))
    with pytest.raises(RuntimeError, match="failed"):
        execute_create(
            tmp_path, tmp_path / "state", loaded, approved_repo="acme/repo"
        )
    assert (tmp_path / "state" / "todo-create" / f"{loaded.transaction_id}.json").exists()


def _write(path: Path, payload: dict) -> Path:
    write_request(path, payload)
    return path


def _issue(req, body, labels=("tpo:todo", "needs-triage"), *, title=None):
    return SimpleNamespace(
        number=42, title=title or req.title, body=body, state="open", labels=tuple(labels)
    )


def _patch_machine(mocker, req, issues, *, audit=None):
    from hermes_pipeline import github_issues

    mocker.patch.object(github_issues, "repository_identity", return_value="acme/repo")
    mocker.patch.object(github_issues, "list_all_issues", side_effect=lambda *a, **k: tuple(issues))
    mocker.patch.object(github_issues, "fetch_issue", side_effect=lambda *a, **k: issues[0])
    mocker.patch("subprocess.run", return_value=SimpleNamespace(returncode=0))
    mocker.patch("hermes_pipeline.cli._audit_phase_options", return_value=())
    mocker.patch("hermes_pipeline.cli._audit_default_branch", return_value="main")
    mocker.patch(
        "hermes_pipeline.cli._audit_issue",
        side_effect=audit or (lambda *a, **k: ([], [], [])),
    )


def test_marker_discovery_is_unfiltered_and_fully_paginated(tmp_path, mocker):
    from hermes_pipeline import github_issues
    from hermes_pipeline.todos_create import _matching_issues, creation_marker

    req = load_create_request(_write(tmp_path / "request.json", request()))
    marker = creation_marker(req.transaction_id)
    fake = FakeGh().on(
        *API_ARGV,
        "--paginate",
        "--slurp",
        "repos/acme/repo/issues?state=all&per_page=100",
        stdout=json.dumps([[issue_payload(1)], [issue_payload(42, body=marker, labels=())]]),
    )
    mocker.patch.object(github_issues, "_run", fake)
    found = _matching_issues(tmp_path, "acme/repo", marker)
    assert [item.number for item in found] == [42]
    assert "labels=" not in " ".join(fake.gh_calls()[0])


def test_duplicate_transaction_markers_across_issues_fail_closed(tmp_path, mocker):
    req = load_create_request(_write(tmp_path / "request.json", request()))
    partial = _issue(req, f"<!-- tpo-create:{req.transaction_id} -->\n")
    second = SimpleNamespace(**{**partial.__dict__, "number": 43})
    _patch_machine(mocker, req, [partial, second])
    with pytest.raises(TodoCreateError, match="duplicate_marker"):
        execute_create(tmp_path, tmp_path / "state", req, approved_repo="acme/repo")


def test_unknown_create_outcome_recovers_marker_without_second_create(tmp_path, mocker):
    from hermes_pipeline import github_issues
    from hermes_pipeline.todos_create import _render_fields

    req = load_create_request(_write(tmp_path / "request.json", request()))
    partial = _issue(req, _render_fields(req))
    complete = _issue(req, render_create_body(req, issue_number=42), ("tpo:todo", "ready-for-agent"))
    issues = []
    _patch_machine(mocker, req, issues)
    create = mocker.patch.object(
        github_issues, "create_issue", side_effect=github_issues.GitHubIssuesError("gh_unavailable", "issue create")
    )
    mocker.patch.object(github_issues, "update_issue_body", side_effect=lambda *a, **k: issues.__setitem__(0, complete))
    issues.append(partial)
    # First discovery must look empty, then recovery observes the partial issue.
    github_issues.list_all_issues.side_effect = [(), (partial,)]
    mocker.patch.object(github_issues, "fetch_issue", return_value=complete)
    assert execute_create(
        tmp_path, tmp_path / "state", req, approved_repo="acme/repo"
    ) == 42
    assert create.call_count == 1
    github_issues.repository_identity.assert_called_once_with(tmp_path)
    assert create.call_args.kwargs["repo"] == "acme/repo"


@pytest.mark.parametrize(
    "labels",
    [
        ("tpo:todo", "needs-triage"),
        ("tpo:todo", "needs-triage", "priority:P1"),
        ("tpo:todo", "needs-triage", "priority:P1", "ready-for-agent"),
        ("tpo:todo", "priority:P1", "ready-for-agent"),
    ],
)
def test_partial_label_states_converge_and_complete_retry_is_idempotent(tmp_path, mocker, labels):
    from hermes_pipeline import github_issues

    req = load_create_request(_write(tmp_path / "request.json", request()))
    body = render_create_body(req, issue_number=42)
    issues = [_issue(req, body, labels)]

    def audit(_project, issue, **_kwargs):
        missing = [] if "priority:P1" in issue.labels else ["priority:P1"]
        return ([f"label:missing:{x}" for x in missing], missing, [])

    _patch_machine(mocker, req, issues, audit=audit)
    def add(_p, _n, label, **_k):
        issues[0] = _issue(req, body, (*issues[0].labels, label))
    def remove(_p, _n, label, **_k):
        issues[0] = _issue(req, body, tuple(x for x in issues[0].labels if x != label))
    add_mock = mocker.patch.object(github_issues, "add_label", side_effect=add)
    remove_mock = mocker.patch.object(github_issues, "remove_label", side_effect=remove)
    assert execute_create(
        tmp_path, tmp_path / "state", req, approved_repo="acme/repo"
    ) == 42
    assert "needs-triage" not in issues[0].labels
    if labels == ("tpo:todo", "priority:P1", "ready-for-agent"):
        assert not add_mock.called and not remove_mock.called


def test_lock_contention_fails_without_persisting_request(tmp_path, mocker):
    from hermes_pipeline import github_issues

    req = load_create_request(_write(tmp_path / "request.json", request()))
    state = tmp_path / "state"
    mocker.patch.object(github_issues, "repository_identity", return_value="acme/repo")
    with create_lock(state), pytest.raises(TodoCreateError, match="create_locked"):
        execute_create(tmp_path, state, req, approved_repo="acme/repo")
    assert not (state / "todo-create" / f"{req.transaction_id}.json").exists()


@pytest.mark.parametrize("failure", ["late_drift", "label_timeout"])
def test_late_drift_and_label_timeout_retain_recovery_request(tmp_path, mocker, failure):
    from hermes_pipeline import github_issues

    req = load_create_request(_write(tmp_path / "request.json", request()))
    body = render_create_body(req, issue_number=42)
    issues = [_issue(req, body)]
    _patch_machine(
        mocker, req, issues,
        audit=lambda *a, **k: (["label:missing:priority:P1"], ["priority:P1"], []),
    )
    if failure == "late_drift":
        github_issues.fetch_issue.side_effect = lambda *a, **k: _issue(req, body, title="changed")
        expected = "late_drift"
    else:
        mocker.patch.object(
            github_issues, "add_label",
            side_effect=github_issues.GitHubIssuesError("gh_unavailable", "issue edit"),
        )
        expected = "gh_unavailable"
    with pytest.raises(Exception, match=expected):
        execute_create(tmp_path, tmp_path / "state", req, approved_repo="acme/repo")
    assert (tmp_path / "state" / "todo-create" / f"{req.transaction_id}.json").exists()
