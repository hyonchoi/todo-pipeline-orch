"""Executable behavior matrix for todos-manager document attachments."""

import subprocess
from pathlib import Path

import pytest

from tests.skill_test_environment import skill_logic
from tests.skill_test_environment.skill_logic import (
    AttachmentCandidate,
    AttachmentSelection,
    AttachmentValidationError,
    AttachmentWorkflow,
    EvidenceLocator,
    ManifestTaskDraft,
    PlanAuthoringWorkflow,
    PlanReadiness,
    apply_attachment_selection_to_todo,
    audit_attachment_fields,
    audit_todo_markdown,
    classify_attachment_document,
    discover_attachment_candidates,
    load_attachment_policy,
    parse_stored_references,
    validate_attachment_path,
)


def _digest(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


def _authoring_task(plan_text: str) -> tuple[ManifestTaskDraft, dict[str, tuple[EvidenceLocator, ...]]]:
    evidence = EvidenceLocator(
        kind="plan_lines",
        source="docs/plan.md",
        start_line=3,
        end_line=7,
        digest=_digest("## Task\nImplement feature.\n- Criterion: works\n- Verify: uv run pytest\n- Commit: feat: implement feature\n"),
    )
    task = ManifestTaskDraft(
        title="Task",
        instructions="Implement feature.",
        acceptance_criteria=("works",),
        verification=("uv run pytest",),
        commit_message="feat: implement feature",
    )
    return task, {field: (evidence,) for field in (
        "title", "instructions", "acceptance_criteria", "verification", "commit_message"
    )}


def test_manifest_authoring_existing_manifest_is_byte_preserving_noop(tmp_path):
    document = (
        "# Plan\r\n```json tpo-plan\r\n"
        '{"schema_version":1,"todo_id":"TODO-42","tasks":[{"id":"task-01",'
        '"title":"T","instructions":"I","acceptance_criteria":["A"],'
        '"verification":["V"],"commit_message":"C"}]}\r\n```\r\n'
    )
    plan = _write(tmp_path, "docs/plan.md", document)
    before = plan.read_bytes()
    workflow = PlanAuthoringWorkflow(tmp_path, project="demo", todo_id="TODO-42", plan_path="docs/plan.md")
    assert workflow.prepare() == "manifest"
    assert workflow.diff == ""
    assert plan.read_bytes() == before


def test_legacy_manifest_proposal_has_exact_diff_and_out_of_band_provenance(tmp_path):
    document = "# Plan\n\n## Task\nImplement feature.\n- Criterion: works\n- Verify: uv run pytest\n- Commit: feat: implement feature\n"
    _write(tmp_path, "docs/plan.md", document)
    task, provenance = _authoring_task(document)
    workflow = PlanAuthoringWorkflow(tmp_path, project="demo", todo_id="TODO-42", plan_path="docs/plan.md")
    workflow.approve_source()
    assert workflow.prepare((task,), {"task-01": provenance}) == "candidate"
    expected_manifest = (
        '{"schema_version":1,"todo_id":"TODO-42","tasks":'
        '[{"id":"task-01","title":"Task","instructions":"Implement feature.",'
        '"acceptance_criteria":["works"],"verification":["uv run pytest"],'
        '"commit_message":"feat: implement feature"}]}\n```\n'
    )
    assert workflow.diff == (
        "--- a/docs/plan.md\n+++ b/docs/plan.md\n@@ -5,3 +5,7 @@\n - Criterion: works\n"
        " - Verify: uv run pytest\n - Commit: feat: implement feature\n+\n+```json tpo-plan\n+"
        + expected_manifest.replace("\n```\n", "\n+```\n")
    )
    assert workflow.provenance == {"task-01": provenance}
    assert "provenance" not in workflow.candidate_text


def test_new_plan_proposal_and_approval_order_validate_exact_command(tmp_path):
    plan_text = "# Plan\n\n## Task\nImplement feature.\n- Criterion: works\n- Verify: uv run pytest\n- Commit: feat: implement feature\n"
    task, provenance = _authoring_task(plan_text)
    todos = _write(tmp_path, "TODOS.md", "## Entries\n\n- [ ] **TODO-42: Work** — x\n")
    calls = []
    workflow = PlanAuthoringWorkflow(
        tmp_path, project="demo", todo_id="TODO-42", plan_path="docs/plan.md",
        source_text=plan_text, todos_path=todos,
        command_runner=lambda args: calls.append(args) or 0,
    )
    workflow.approve_source()
    workflow.prepare((task,), {"task-01": provenance})
    assert workflow.diff == (
        "--- /dev/null\n+++ b/docs/plan.md\n@@ -0,0 +1,11 @@\n"
        "+# Plan\n+\n+## Task\n+Implement feature.\n+- Criterion: works\n"
        "+- Verify: uv run pytest\n+- Commit: feat: implement feature\n+\n"
        "+```json tpo-plan\n+{\"schema_version\":1,\"todo_id\":\"TODO-42\","
        "\"tasks\":[{\"id\":\"task-01\",\"title\":\"Task\","
        "\"instructions\":\"Implement feature.\",\"acceptance_criteria\":[\"works\"],"
        "\"verification\":[\"uv run pytest\"],\"commit_message\":"
        "\"feat: implement feature\"}]}\n+```\n"
    )
    candidate = workflow.stage_and_validate()
    assert calls == [["tpo", "plan", "validate", "demo", "--todo", "TODO-42", "--plan", str(candidate.relative_to(tmp_path)), "--require-manifest"]]
    assert candidate.parent == tmp_path / "docs"
    assert candidate.stat().st_mode & 0o777 == 0o600
    workflow.confirm_diff()
    workflow.approve_final_todo(workflow.render_final_todo_preview())
    workflow.install()
    assert (tmp_path / "docs/plan.md").stat().st_mode & 0o777 == 0o644
    assert not candidate.exists()


def test_validated_candidate_drift_fails_closed_and_cleans_up(tmp_path):
    document = "# Plan\n\n## Task\nImplement feature.\n- Criterion: works\n- Verify: uv run pytest\n- Commit: feat: implement feature\n"
    plan = _write(tmp_path, "docs/plan.md", document)
    todos = _write(tmp_path, "TODOS.md", "## Entries\n\n- [ ] **TODO-42: Work** — x\n")
    task, provenance = _authoring_task(document)
    workflow = PlanAuthoringWorkflow(tmp_path, project="demo", todo_id="TODO-42", plan_path="docs/plan.md", todos_path=todos, command_runner=lambda _args: 0)
    workflow.approve_source(); workflow.prepare((task,), {"task-01": provenance})
    candidate = workflow.stage_and_validate()
    workflow.confirm_diff(); workflow.approve_final_todo(workflow.render_final_todo_preview())
    candidate.write_text(candidate.read_text() + "tampered")
    with pytest.raises(RuntimeError, match="candidate drift"):
        workflow.install()
    assert plan.read_text() == document
    assert not candidate.exists()


@pytest.mark.parametrize("field", ["title", "instructions", "acceptance_criteria", "verification", "commit_message"])
def test_each_manifest_field_requires_content_specific_support(tmp_path, field):
    document = "# Plan\n\n## Task\nImplement feature.\n- Criterion: works\n- Verify: uv run pytest\n- Commit: feat: implement feature\n"
    _write(tmp_path, "docs/plan.md", document)
    task, provenance = _authoring_task(document)
    replacements = {
        "title": "Invented title", "instructions": "Invented boundary",
        "acceptance_criteria": ("invented criterion",),
        "verification": ("invented command",), "commit_message": "feat: invented",
    }
    values = task.__dict__ | {field: replacements[field]}
    workflow = PlanAuthoringWorkflow(tmp_path, project="demo", todo_id="TODO-42", plan_path="docs/plan.md")
    workflow.approve_source()
    assert workflow.prepare((ManifestTaskDraft(**values),), {"task-01": provenance}) == "insufficient_evidence"


def test_plan_line_source_must_match_selected_plan(tmp_path):
    document = "# Plan\n\n## Task\nImplement feature.\n- Criterion: works\n- Verify: uv run pytest\n- Commit: feat: implement feature\n"
    _write(tmp_path, "docs/plan.md", document)
    task, provenance = _authoring_task(document)
    provenance["title"] = (EvidenceLocator("plan_lines", "docs/other.md", 3, 7, provenance["title"][0].digest),)
    workflow = PlanAuthoringWorkflow(tmp_path, project="demo", todo_id="TODO-42", plan_path="docs/plan.md")
    workflow.approve_source()
    assert workflow.prepare((task,), {"task-01": provenance}) == "insufficient_evidence"


def test_repository_lines_and_exact_git_commit_evidence(tmp_path):
    document = "# Plan\n\nTask\n"
    _write(tmp_path, "docs/plan.md", document)
    support = _write(tmp_path, "docs/support.txt", "Task\nImplement feature.\nworks\nuv run pytest\nfeat: implement feature\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-qm", "support"], cwd=tmp_path, check=True)
    oid = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()
    task = ManifestTaskDraft("Task", "Implement feature.", ("works",), ("uv run pytest",), "feat: implement feature")
    repo_locator = EvidenceLocator("repository_lines", "docs/support.txt", 1, 5, _digest(support.read_text()))
    commit_locator = EvidenceLocator("git_commit", "docs/support.txt", digest=_digest(support.read_text()), commit=oid)
    for locator in (repo_locator, commit_locator):
        provenance = {field: (locator,) for field in ("title", "instructions", "acceptance_criteria", "verification", "commit_message")}
        workflow = PlanAuthoringWorkflow(tmp_path, project="demo", todo_id="TODO-42", plan_path="docs/plan.md")
        workflow.approve_source()
        assert workflow.prepare((task,), {"task-01": provenance}) == "candidate"
    symbolic = EvidenceLocator("git_commit", "docs/support.txt", digest=_digest(support.read_text()), commit="HEAD")
    blob_oid = subprocess.run(["git", "rev-parse", "HEAD:docs/support.txt"], cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()
    blob = EvidenceLocator("git_commit", "docs/support.txt", digest=_digest(support.read_text()), commit=blob_oid)
    wrong_format_length = EvidenceLocator(
        "git_commit", "docs/support.txt", digest=_digest(support.read_text()), commit="a" * 64
    )
    abbreviated = EvidenceLocator(
        "git_commit", "docs/support.txt", digest=_digest(support.read_text()), commit=oid[:12]
    )
    for rejected in (symbolic, blob, abbreviated, wrong_format_length):
        bad = {field: (rejected,) for field in ("title", "instructions", "acceptance_criteria", "verification", "commit_message")}
        workflow = PlanAuthoringWorkflow(tmp_path, project="demo", todo_id="TODO-42", plan_path="docs/plan.md")
        workflow.approve_source()
        assert workflow.prepare((task,), {"task-01": bad}) == "insufficient_evidence"


def test_final_preview_is_exact_and_success_changes_only_selected_todo_and_plan(tmp_path):
    document = "# Plan\n\n## Task\nImplement feature.\n- Criterion: works\n- Verify: uv run pytest\n- Commit: feat: implement feature\n"
    plan = _write(tmp_path, "docs/plan.md", document)
    other_plan = _write(tmp_path, "docs/other.md", "unchanged")
    todos = _write(tmp_path, "TODOS.md", "## Entries\n\n- [ ] **TODO-41: Other** — x\n  - **Plan:** docs/other.md\n\n- [ ] **TODO-42: Work** — x\n")
    task, provenance = _authoring_task(document)
    workflow = PlanAuthoringWorkflow(tmp_path, project="demo", todo_id="TODO-42", plan_path="docs/plan.md", todos_path=todos, command_runner=lambda _args: 0)
    workflow.approve_source(); workflow.prepare((task,), {"task-01": provenance}); workflow.stage_and_validate(); workflow.confirm_diff()
    preview = (
        "## Entries\n\n"
        "- [ ] **TODO-41: Other** — x\n"
        "  - **Plan:** docs/other.md\n\n"
        "- [ ] **TODO-42: Work** — x\n"
        "  - **Plan:** docs/plan.md\n"
    )
    assert workflow.render_final_todo_preview() == preview
    with pytest.raises(RuntimeError, match="preview"):
        workflow.approve_final_todo(preview + "drift")
    workflow.approve_final_todo(preview); workflow.install()
    assert other_plan.read_text() == "unchanged"
    assert todos.read_text().count("docs/plan.md") == 1
    assert "TODO-41: Other" in todos.read_text() and "docs/other.md" in todos.read_text()
    assert "```json tpo-plan" in plan.read_text()


def test_todo_drift_after_plan_install_fails_without_hidden_plan_rollback(
    tmp_path, monkeypatch
):
    document = "# Plan\n\n## Task\nImplement feature.\n- Criterion: works\n- Verify: uv run pytest\n- Commit: feat: implement feature\n"
    plan = _write(tmp_path, "docs/plan.md", document)
    todos = _write(
        tmp_path,
        "TODOS.md",
        "## Entries\n\n- [ ] **TODO-42: Work** — x\n",
    )
    task, provenance = _authoring_task(document)
    workflow = PlanAuthoringWorkflow(
        tmp_path,
        project="demo",
        todo_id="TODO-42",
        plan_path="docs/plan.md",
        todos_path=todos,
        command_runner=lambda _args: 0,
    )
    workflow.approve_source()
    workflow.prepare((task,), {"task-01": provenance})
    workflow.stage_and_validate()
    workflow.confirm_diff()
    workflow.approve_final_todo(workflow.render_final_todo_preview())
    concurrent = todos.read_text() + "\n## Concurrent\n\nPreserve me.\n"
    monkeypatch.setattr(
        skill_logic,
        "_before_todo_lock",
        lambda: todos.write_text(concurrent),
    )

    with pytest.raises(RuntimeError, match="TODO drift"):
        workflow.install()

    assert "```json tpo-plan" in plan.read_text()
    assert todos.read_text() == concurrent


def test_new_plan_publish_does_not_clobber_concurrent_winner(tmp_path, monkeypatch):
    source = "# Plan\n\n## Task\nImplement feature.\n- Criterion: works\n- Verify: uv run pytest\n- Commit: feat: implement feature\n"
    todos = _write(tmp_path, "TODOS.md", "## Entries\n\n- [ ] **TODO-42: Work** — x\n")
    task, provenance = _authoring_task(source)
    workflow = PlanAuthoringWorkflow(tmp_path, project="demo", todo_id="TODO-42", plan_path="docs/plan.md", todos_path=todos, source_text=source, command_runner=lambda _args: 0)
    workflow.approve_source(); workflow.prepare((task,), {"task-01": provenance}); candidate = workflow.stage_and_validate(); workflow.confirm_diff(); workflow.approve_final_todo(workflow.render_final_todo_preview())
    monkeypatch.setattr(skill_logic, "_before_new_plan_publish", lambda: _write(tmp_path, "docs/plan.md", "concurrent winner\n"))
    with pytest.raises(RuntimeError, match="concurrent target"):
        workflow.install()
    assert (tmp_path / "docs/plan.md").read_text() == "concurrent winner\n"
    assert not candidate.exists()


def test_new_plan_rejects_lexical_parent_symlink(tmp_path):
    outside = tmp_path / "outside"; outside.mkdir()
    (tmp_path / "docs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        PlanAuthoringWorkflow(tmp_path, project="demo", todo_id="TODO-42", plan_path="docs/plan.md", source_text="# Plan\n")
    assert list(outside.iterdir()) == []


def test_new_plan_revalidates_parent_after_injected_swap(tmp_path, monkeypatch):
    source = "# Plan\n\n## Task\nImplement feature.\n- Criterion: works\n- Verify: uv run pytest\n- Commit: feat: implement feature\n"
    outside = tmp_path / "outside"; outside.mkdir()
    task, provenance = _authoring_task(source)
    workflow = PlanAuthoringWorkflow(tmp_path, project="demo", todo_id="TODO-42", plan_path="docs/plan.md", source_text=source, command_runner=lambda _args: 0)
    workflow.approve_source(); workflow.prepare((task,), {"task-01": provenance})
    def swap_parent():
        (tmp_path / "docs").rmdir()
        (tmp_path / "docs").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(skill_logic, "_after_plan_parent_validation", swap_parent)
    with pytest.raises(ValueError, match="parent.*symlink"):
        workflow.stage_and_validate()
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("path", ["docs/../plan.md", "./docs/plan.md"])
def test_new_plan_rejects_noncanonical_lexical_components(tmp_path, path):
    with pytest.raises(ValueError, match="lexical"):
        PlanAuthoringWorkflow(tmp_path, project="demo", todo_id="TODO-42", plan_path=path, source_text="# Plan\n")


@pytest.mark.parametrize("stop", ["source", "diff", "todo", "validator"])
def test_authoring_cancellation_or_validator_failure_preserves_plan(tmp_path, stop):
    document = "# Plan\n\n## Task\nImplement feature.\n- Criterion: works\n- Verify: uv run pytest\n- Commit: feat: implement feature\n"
    plan = _write(tmp_path, "docs/plan.md", document)
    task, provenance = _authoring_task(document)
    runner = (lambda _args: 1) if stop == "validator" else (lambda _args: 0)
    workflow = PlanAuthoringWorkflow(tmp_path, project="demo", todo_id="TODO-42", plan_path="docs/plan.md", command_runner=runner)
    if stop != "source":
        workflow.approve_source()
        workflow.prepare((task,), {"task-01": provenance})
        if stop in {"diff", "todo", "validator"}:
            try:
                workflow.stage_and_validate()
            except RuntimeError:
                pass
        if stop == "todo":
            workflow.confirm_diff()
    workflow.cancel()
    assert plan.read_text() == document
    assert list(plan.parent.glob(".plan.md.tpo-plan-*")) == []


def test_unsupported_evidence_is_insufficient_and_creates_nothing(tmp_path):
    document = "# Plan\n\n## Task\nImplement feature.\n- Criterion: works\n- Verify: uv run pytest\n- Commit: feat: implement feature\n"
    plan = _write(tmp_path, "docs/plan.md", document)
    task, provenance = _authoring_task(document)
    provenance["verification"] = ()
    workflow = PlanAuthoringWorkflow(tmp_path, project="demo", todo_id="TODO-42", plan_path="docs/plan.md")
    workflow.approve_source()
    assert workflow.prepare((task,), {"task-01": provenance}) == "insufficient_evidence"
    assert plan.read_text() == document


def test_install_rejects_plan_or_todo_drift_and_concurrent_new_target(tmp_path):
    plan_text = "# Plan\n\n## Task\nImplement feature.\n- Criterion: works\n- Verify: uv run pytest\n- Commit: feat: implement feature\n"
    task, provenance = _authoring_task(plan_text)
    for drift in ("plan", "todo", "target"):
        repo = tmp_path / drift
        repo.mkdir()
        plan_path = None if drift == "target" else _write(repo, "docs/plan.md", plan_text)
        todos = _write(repo, "TODOS.md", "## Entries\n\n- [ ] **TODO-42: Work** — x\n")
        workflow = PlanAuthoringWorkflow(repo, project="demo", todo_id="TODO-42", plan_path="docs/plan.md", todos_path=todos, source_text=plan_text if drift == "target" else None, command_runner=lambda _args: 0)
        workflow.approve_source()
        workflow.prepare((task,), {"task-01": provenance})
        workflow.stage_and_validate()
        workflow.confirm_diff()
        workflow.approve_final_todo(workflow.render_final_todo_preview())
        if drift == "plan":
            plan_path.write_text(plan_text + "drift")
        elif drift == "todo":
            todos.write_text(todos.read_text() + "drift")
        else:
            _write(repo, "docs/plan.md", "concurrent")
        with pytest.raises(RuntimeError, match="drift|concurrent"):
            workflow.install()
        workflow.cancel()
        assert (repo / "docs/plan.md").read_text() == (plan_text + "drift" if drift == "plan" else "concurrent" if drift == "target" else plan_text)


def test_manifest_task_ids_are_ordered_and_bounded(tmp_path):
    document = "# Plan\n\nT I A V C\n" + "\n".join(f"Task {i}" for i in range(49)) + "\n"
    _write(tmp_path, "docs/plan.md", document)
    locator = EvidenceLocator("plan_lines", "docs/plan.md", 1, 52, _digest(document))
    tasks = tuple(ManifestTaskDraft("T", "I", ("A",), ("V",), "C") for _ in range(50))
    provenance = {f"task-{i:02d}": {field: (locator,) for field in ("title", "instructions", "acceptance_criteria", "verification", "commit_message")} for i in range(1, 51)}
    workflow = PlanAuthoringWorkflow(tmp_path, project="demo", todo_id="TODO-42", plan_path="docs/plan.md")
    workflow.approve_source()
    assert workflow.prepare(tasks, provenance) == "candidate"
    assert [item["id"] for item in __import__("json").loads(workflow.candidate_text.split("```json tpo-plan\n", 1)[1].split("\n```", 1)[0])["tasks"]] == [f"task-{i:02d}" for i in range(1, 51)]


def _write(repo: Path, relative: str, text: str = "supporting context") -> Path:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _candidate(path: str, *roles: str) -> AttachmentCandidate:
    return AttachmentCandidate(
        path=path,
        roles=roles,
        relevance_reason="explicit task context",
        source="explicit",
        validation="valid",
    )


def test_packaged_markdown_policy_drives_the_harness():
    policy = load_attachment_policy()

    assert policy["version"] == 1
    assert policy["candidate_limit"] == 5
    assert policy["confirmation"] == {
        "zero": "none",
        "one": "explicit-selection",
        "multiple": "explicit-selection",
    }
    assert policy["sources"] == ["explicit", "git changed or untracked", "bounded search"]
    assert policy["relevance"] == ["explicit", "todo-id", "close-scope", "concrete-target-overlap"]


@pytest.mark.parametrize(
    ("document", "state"),
    [
        (
            "# Plan\n```json tpo-plan\n"
            '{"schema_version":1,"todo_id":"TODO-42","tasks":['
            '{"id":"task-1","title":"Work","instructions":"Do work",'
            '"acceptance_criteria":["It works"],'
            '"verification":["uv run pytest"],'
            '"commit_message":"feat: work"}]}\n```\n',
            "manifest",
        ),
        ("# Human-authored execution plan\n\n1. Do the work.\n", "legacy"),
        (
            "```json tpo-plan\n"
            '{"schema_version":1,"todo_id":"TODO-99","tasks":[]}\n```\n',
            "invalid",
        ),
    ],
)
def test_selected_plan_readiness_uses_production_validator(
    tmp_path, document, state
):
    _write(tmp_path, "docs/plan.md", document)
    workflow = AttachmentWorkflow(tmp_path, command="add", todo_id="TODO-42")
    workflow.select_manual("Plan", "docs/plan.md")

    readiness = workflow.evaluate_plan_readiness(research_completed=True)

    assert isinstance(readiness, PlanReadiness)
    assert readiness.state == state
    assert f"Plan readiness: {state}" in workflow.render_synthesis(readiness)
    assert f"Plan readiness: {state}" in workflow.render_preview(readiness)


def test_plan_readiness_cannot_replace_or_precede_ai_research(tmp_path):
    _write(tmp_path, "docs/plan.md", "# Legacy plan\n")
    workflow = AttachmentWorkflow(tmp_path, command="add", todo_id="TODO-42")
    workflow.select_manual("Plan", "docs/plan.md")

    with pytest.raises(RuntimeError, match="AI research"):
        workflow.evaluate_plan_readiness(research_completed=False)

    assert workflow.selection.plan == "docs/plan.md"
    readiness = workflow.evaluate_plan_readiness(research_completed=True)
    synthesis = "Why: researched rationale\nWhat: researched scope"
    assert workflow.render_synthesis(
        readiness, research_synthesis=synthesis
    ).startswith(synthesis)


@pytest.mark.parametrize(
    ("candidates", "state", "selected_plan", "confirm_error"),
    [
        ((), "none detected", None, None),
        ((_candidate("docs/one.md", "Plan"),), "suggested", None, "Plan requires explicit selection"),
        (
            (
                _candidate("docs/one.md", "Plan"),
                _candidate("docs/two.md", "Plan"),
            ),
            "unresolved",
            None,
            "Plan is unresolved",
        ),
    ],
    ids=["zero", "one", "multiple"],
)
def test_add_candidate_cardinality_controls_confirmation(
    tmp_path, candidates, state, selected_plan, confirm_error
):
    for candidate in candidates:
        _write(tmp_path, candidate.path)
    workflow = AttachmentWorkflow(tmp_path, command="add", candidates=candidates)

    assert workflow.role_state("Plan") == state
    if confirm_error:
        with pytest.raises(ValueError, match=confirm_error):
            workflow.confirm()
        assert workflow.selection.plan is None
    else:
        assert workflow.confirm().plan == selected_plan


@pytest.mark.parametrize("role", ["Plan", "Spec", "Reference"])
@pytest.mark.parametrize(
    ("candidate_count", "policy_key", "policy_action", "should_require_selection"),
    [
        (0, "zero", "explicit-selection", True),
        (1, "one", "none", False),
        (2, "multiple", "none", False),
    ],
    ids=["zero", "one", "multiple"],
)
def test_confirmation_policy_controls_every_role_cardinality(
    tmp_path,
    monkeypatch,
    role,
    candidate_count,
    policy_key,
    policy_action,
    should_require_selection,
):
    confirmation = {
        "zero": "none",
        "one": "explicit-selection",
        "multiple": "explicit-selection",
    }
    confirmation[policy_key] = policy_action
    monkeypatch.setitem(skill_logic.ATTACHMENT_POLICY, "confirmation", confirmation)
    candidates = tuple(
        _candidate(f"docs/{role.lower()}-{number}.md", role)
        for number in range(candidate_count)
    )
    workflow = AttachmentWorkflow(tmp_path, command="add", candidates=candidates)
    for other_role in {"Plan", "Spec", "Reference"} - {role}:
        workflow.choose_none(other_role)

    if should_require_selection:
        with pytest.raises(ValueError, match=role):
            workflow.confirm()
    else:
        assert workflow.confirm() == AttachmentSelection()


def test_confirmation_policy_controls_combined_role_choice(tmp_path, monkeypatch):
    monkeypatch.setitem(
        skill_logic.ATTACHMENT_POLICY,
        "confirmation",
        {"zero": "none", "one": "none", "multiple": "explicit-selection"},
    )
    workflow = AttachmentWorkflow(
        tmp_path,
        command="add",
        candidates=(_candidate("docs/combined.md", "Plan", "Spec"),),
    )
    workflow.choose_none("Reference")

    assert workflow.confirm() == AttachmentSelection()


def test_combined_candidate_can_be_explicitly_declined(tmp_path):
    workflow = AttachmentWorkflow(
        tmp_path,
        command="add",
        candidates=(_candidate("docs/combined.md", "Plan", "Spec"),),
    )
    workflow.choose_none("Plan")
    workflow.choose_none("Spec")
    workflow.choose_none("Reference")

    assert workflow.confirm() == AttachmentSelection()


def test_add_supports_manual_and_omitted_attachments_without_early_write(tmp_path):
    _write(tmp_path, "docs/manual plan.md")
    writes = []
    manual = AttachmentWorkflow(tmp_path, command="add")
    manual.select_manual("Plan", "docs/manual plan.md")

    manual.confirm()
    assert writes == []
    assert manual.finish(approved=False, writer=writes.append) is False
    assert writes == []
    assert manual.finish(approved=True, writer=writes.append) is True
    assert writes == [AttachmentSelection(plan="docs/manual plan.md")]

    omitted = AttachmentWorkflow(tmp_path, command="add")
    omitted.choose_none("Plan")
    assert omitted.confirm() == AttachmentSelection()


def test_preview_approval_mutates_actual_todo_markdown_only_after_approval(tmp_path):
    _write(tmp_path, "docs/plan.md")
    todos = tmp_path / "TODOS.md"
    todos.write_text(
        "# TODOS\n\n## Metadata\n\nNEXT_TODO_ID: 2\n\n## Entry Schema\n\n"
        "schema\n\n## Entries\n\n- [ ] **TODO-1: Example task** — Example summary\n"
        "  - **What:** Do the work\n  - **Why:** It matters enough\n"
        "  - **Decisions:** Priority `P2`\n",
        encoding="utf-8",
    )
    before = todos.read_bytes()
    selection = AttachmentSelection(plan="docs/plan.md")

    assert apply_attachment_selection_to_todo(todos, "TODO-1", selection, approved=False) is False
    assert todos.read_bytes() == before
    assert apply_attachment_selection_to_todo(todos, "TODO-1", selection, approved=True) is True
    assert "  - **Plan:** docs/plan.md" in todos.read_text(encoding="utf-8")


def test_workflow_finish_owns_real_todo_mutation_and_cancellation(tmp_path):
    _write(tmp_path, "docs/plan.md")
    todos = tmp_path / "TODOS.md"
    todos.write_text("## Entries\n\n- [ ] **TODO-1: One** — Summary here\n  - **What:** x\n", encoding="utf-8")
    before = todos.read_bytes()
    workflow = AttachmentWorkflow(tmp_path, command="revise", todos_path=todos, todo_id="TODO-1")
    workflow.select_manual("Plan", "docs/plan.md")
    workflow.confirm()

    assert workflow.finish(approved=False) is False
    assert todos.read_bytes() == before
    assert workflow.finish(approved=True) is True
    assert "**Plan:** docs/plan.md" in todos.read_text(encoding="utf-8")


def test_ambiguity_blocks_preview_until_one_candidate_is_selected(tmp_path):
    _write(tmp_path, "docs/one.md")
    _write(tmp_path, "docs/two.md")
    workflow = AttachmentWorkflow(
        tmp_path,
        command="add",
        candidates=(
            _candidate("docs/one.md", "Plan"),
            _candidate("docs/two.md", "Plan"),
        ),
    )

    with pytest.raises(ValueError, match="Plan is unresolved"):
        workflow.confirm()
    with pytest.raises(RuntimeError, match="confirmation"):
        workflow.finish(approved=True, writer=lambda _: None)

    workflow.select_candidate("Plan", 2)
    assert workflow.confirm().plan == "docs/two.md"


def test_lone_reference_suggestion_requires_explicit_selection(tmp_path):
    _write(tmp_path, "docs/context.md")
    workflow = AttachmentWorkflow(tmp_path, command="add", candidates=(_candidate("docs/context.md", "Reference"),))
    with pytest.raises(ValueError, match="Reference requires explicit selection"):
        workflow.confirm()
    workflow.select_candidate("Reference", 1)
    assert workflow.confirm().references == ("docs/context.md",)


def test_revise_preserves_replaces_and_removes_singletons(tmp_path):
    for path in ("docs/old-plan.md", "docs/old-spec.md", "docs/new-plan.md"):
        _write(tmp_path, path)
    workflow = AttachmentWorkflow(
        tmp_path,
        command="revise",
        existing=AttachmentSelection(
            plan="docs/old-plan.md",
            spec="docs/old-spec.md",
        ),
        candidates=(_candidate("docs/new-plan.md", "Plan"),),
    )

    assert workflow.role_state("Plan") == "preserved"
    assert workflow.confirm() == AttachmentSelection(
        plan="docs/old-plan.md",
        spec="docs/old-spec.md",
    )

    workflow.replace("Plan", "docs/new-plan.md")
    workflow.remove("Spec")
    assert workflow.confirm() == AttachmentSelection(plan="docs/new-plan.md")


def test_revise_warns_about_invalid_existing_paths_without_blocking_other_edits(
    tmp_path,
):
    _write(tmp_path, "docs/old-plan.md")
    _write(tmp_path, "docs/new-plan.md")
    workflow = AttachmentWorkflow(
        tmp_path,
        command="revise",
        existing=AttachmentSelection(
            plan="docs/old-plan.md",
            spec="docs/missing-spec.md",
        ),
    )

    assert [(warning.role, warning.stored_path) for warning in workflow.warnings] == [
        ("Spec", "docs/missing-spec.md")
    ]
    workflow.replace("Plan", "docs/new-plan.md")

    assert workflow.confirm() == AttachmentSelection(
        plan="docs/new-plan.md",
        spec="docs/missing-spec.md",
    )


def test_revise_references_append_deduplicate_remove_and_exclude_roles(tmp_path):
    for path in (
        "docs/plan.md",
        "docs/spec.md",
        "docs/adr/0001.md",
        "docs/context.md",
        "docs/adr/0002.md",
    ):
        _write(tmp_path, path)
    workflow = AttachmentWorkflow(
        tmp_path,
        command="revise",
        existing=AttachmentSelection(
            plan="docs/plan.md",
            spec="docs/spec.md",
            references=("docs/adr/0001.md", "docs/context.md"),
        ),
    )

    workflow.append_reference("docs/context.md")
    workflow.append_reference("docs/adr/0002.md")
    with pytest.raises(ValueError, match="matches Plan or Spec"):
        workflow.append_reference("docs/plan.md")
    workflow.remove_reference("docs/context.md")

    assert workflow.confirm().references == (
        "docs/adr/0001.md",
        "docs/adr/0002.md",
    )
    assert workflow.role_state("Reference") == "selected"


def test_unchanged_existing_references_report_preserved(tmp_path):
    _write(tmp_path, "docs/context.md")
    workflow = AttachmentWorkflow(tmp_path, command="revise", existing=AttachmentSelection(references=("docs/context.md",)))
    assert workflow.role_state("Reference") == "preserved"


def test_choose_none_reference_reports_field_wide_removal_as_selected(tmp_path):
    for path in ("docs/adr/0001.md", "docs/context.md"):
        _write(tmp_path, path)
    workflow = AttachmentWorkflow(
        tmp_path,
        command="revise",
        existing=AttachmentSelection(
            references=("docs/adr/0001.md", "docs/context.md")
        ),
    )

    workflow.choose_none("Reference")

    assert workflow.selection.references == ()
    assert workflow.role_state("Reference") == "selected"
    assert workflow.confirm().references == ()


@pytest.mark.parametrize("operation", ["select", "replace", "combined"])
def test_plan_and_spec_selection_rejects_existing_reference_conflict(tmp_path, operation):
    _write(tmp_path, "docs/shared.md")
    workflow = AttachmentWorkflow(
        tmp_path,
        command="revise",
        existing=AttachmentSelection(references=("docs/shared.md",)),
        candidates=(_candidate("docs/shared.md", "Plan", "Spec"),),
    )

    with pytest.raises(ValueError, match="already present in Reference"):
        if operation == "select":
            workflow.select_candidate("Plan", 1)
        elif operation == "replace":
            workflow.replace("Plan", "docs/shared.md")
        else:
            workflow.attach_combined(1)


def test_combined_plan_and_spec_requires_explicit_combined_choice(tmp_path):
    _write(tmp_path, "docs/combined.md")
    workflow = AttachmentWorkflow(
        tmp_path,
        command="revise",
        candidates=(_candidate("docs/combined.md", "Plan", "Spec"),),
    )

    with pytest.raises(ValueError, match="combined Plan and Spec choice"):
        workflow.confirm()
    workflow.attach_combined(1)

    assert workflow.confirm() == AttachmentSelection(
        plan="docs/combined.md",
        spec="docs/combined.md",
    )


def test_rejected_combined_selection_preserves_prior_plan(tmp_path):
    for path in ("docs/prior.md", "docs/shared.md"):
        _write(tmp_path, path)
    workflow = AttachmentWorkflow(
        tmp_path,
        command="revise",
        existing=AttachmentSelection(plan="docs/prior.md", references=("docs/shared.md",)),
        candidates=(_candidate("docs/shared.md", "Plan", "Spec"),),
    )
    with pytest.raises(ValueError, match="already present in Reference"):
        workflow.attach_combined(1)
    assert workflow.selection.plan == "docs/prior.md"


def test_invalid_manual_value_recovers_without_rediscovery(tmp_path):
    _write(tmp_path, "docs/valid.md")
    workflow = AttachmentWorkflow(tmp_path, command="add")

    with pytest.raises(AttachmentValidationError, match="repository-relative"):
        workflow.select_manual("Plan", "/tmp/outside.md")
    assert workflow.discovery_runs == 1
    assert workflow.selection == AttachmentSelection()

    workflow.select_manual("Plan", "docs/valid.md")
    assert workflow.discovery_runs == 1
    assert workflow.confirm().plan == "docs/valid.md"


def test_discovery_obeys_precedence_candidate_limit_and_exclusions(tmp_path):
    plan = "1. Change `src/cache.py`.\n2. Verify with `uv run pytest`.\n"
    paths = ["docs/explicit.md", "docs/git.md"] + [
        f"docs/superpowers/plans/search-{index}.md" for index in range(6)
    ]
    for path in paths:
        _write(tmp_path, path, plan)
    _write(tmp_path, "docs/archive/ignored.md", plan)

    result = discover_attachment_candidates(
        tmp_path,
        explicit_paths=("docs/explicit.md",),
        git_paths=("docs/git.md", "docs/archive/ignored.md"),
        search_paths=tuple(paths[2:]),
        subject_terms=("cache",),
        target_paths=("src/cache.py",),
    )

    assert [candidate.source for candidate in result.candidates] == [
        "explicit",
        "git changed or untracked",
        "bounded search",
        "bounded search",
        "bounded search",
    ]
    assert len(result.candidates) == 5
    assert result.skipped_source == "bounded search"
    assert all("archive" not in candidate.path for candidate in result.candidates)


def test_discovery_honors_shared_read_and_search_budgets(tmp_path):
    plan = "1. Change `src/cache.py`.\n2. Verify with `uv run pytest`.\n"
    for index in range(4):
        _write(tmp_path, f"docs/superpowers/plans/TODO-40-{index}.md", plan)

    result = discover_attachment_candidates(
        tmp_path,
        search_batches=(tuple(
            f"docs/superpowers/plans/TODO-40-{index}.md" for index in range(4)
        ),),
        todo_id="TODO-40",
        read_limit=9,
        search_limit=3,
        reads_used=7,
        searches_used=2,
    )

    assert result.reads == 9
    assert result.searches == 3
    assert result.exhausted is True
    assert result.skipped_source == "bounded search"


def test_todo_id_relevance_uses_canonical_numeric_boundaries(tmp_path):
    plan = "1. Change `src/cache.py`.\n2. Verify with `uv run pytest`.\n"
    paths = tuple(
        f"docs/superpowers/plans/TODO-{number}-plan.md"
        for number in (4, 40, 400)
    )
    for path in paths:
        _write(tmp_path, path, plan)

    result = discover_attachment_candidates(
        tmp_path,
        search_batches=(paths,),
        todo_id="TODO-4",
    )

    assert [candidate.path for candidate in result.candidates] == [paths[0]]


def test_generic_subject_substring_does_not_establish_strong_relevance(tmp_path):
    plan = "1. Change `src/other.py`.\n2. Verify with `uv run pytest`.\n"
    _write(tmp_path, "docs/superpowers/plans/cache.md", plan)

    result = discover_attachment_candidates(
        tmp_path,
        search_batches=(("docs/superpowers/plans/cache.md",),),
        subject_terms=("cache",),
        target_paths=("src/cache.py",),
    )

    assert result.candidates == ()


def test_generic_planning_overlap_does_not_establish_close_scope_relevance(tmp_path):
    _write(
        tmp_path,
        "docs/superpowers/plans/unrelated.md",
        "1. Change `src/other.py`.\n2. Verify tests for the implementation.\n",
    )

    result = discover_attachment_candidates(
        tmp_path,
        search_batches=(("docs/superpowers/plans/unrelated.md",),),
        title_summary="Change implementation and verify tests for unrelated migration",
    )

    assert result.candidates == ()


def test_close_title_summary_scope_is_strong_relevance(tmp_path):
    _write(
        tmp_path,
        "docs/superpowers/plans/cache-eviction.md",
        "1. Change `src/other.py`.\n2. Verify tests.\n",
    )
    result = discover_attachment_candidates(
        tmp_path,
        search_batches=(("docs/superpowers/plans/cache-eviction.md",),),
        title_summary="Cache eviction for bounded storage",
    )
    assert [candidate.path for candidate in result.candidates] == [
        "docs/superpowers/plans/cache-eviction.md"
    ]


@pytest.mark.parametrize(
    "close_scope_policy",
    [
        {"minimum_specific_term_overlap": 3, "generic_terms": []},
        {
            "minimum_specific_term_overlap": 2,
            "generic_terms": ["cache", "eviction"],
        },
    ],
    ids=["minimum-overlap", "generic-vocabulary"],
)
def test_close_scope_relevance_uses_structured_policy(
    tmp_path, monkeypatch, close_scope_policy
):
    _write(
        tmp_path,
        "docs/superpowers/plans/cache-eviction.md",
        "1. Change `src/other.py`.\n2. Verify tests.\n",
    )
    monkeypatch.setitem(
        skill_logic.ATTACHMENT_POLICY,
        "close_scope",
        close_scope_policy,
    )

    result = discover_attachment_candidates(
        tmp_path,
        search_batches=(("docs/superpowers/plans/cache-eviction.md",),),
        title_summary="Cache eviction for bounded storage",
    )

    assert result.candidates == ()


def test_search_accounting_counts_empty_and_repeated_result_invocations(tmp_path):
    _write(tmp_path, "docs/plan.md", "1. Change `src/cache.py`.\n2. Verify tests.\n")
    result = discover_attachment_candidates(
        tmp_path,
        search_batches=((), ("docs/plan.md",), ("docs/plan.md",)),
        target_paths=("src/cache.py",),
    )
    assert result.searches == 3
    assert result.reads == 2


def test_discovery_rejects_reference_comma_immediately_after_classification(
    tmp_path,
):
    relative = "docs/context,notes.md"
    _write(tmp_path, relative, "Background material for the task.\n")

    result = discover_attachment_candidates(
        tmp_path,
        explicit_paths=(relative,),
    )

    assert result.candidates == ()
    assert result.errors == (f"{relative}: Reference path contains a comma.",)


@pytest.mark.parametrize("excluded_target", [".git/config", "archive/plan.md"])
def test_discovery_rechecks_exclusions_after_symlink_resolution(
    tmp_path, excluded_target
):
    target = _write(tmp_path, excluded_target, "TODO-40\nBackground material.\n")
    alias = tmp_path / "docs" / "alias.md"
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.symlink_to(target)

    result = discover_attachment_candidates(
        tmp_path,
        explicit_paths=("docs/alias.md",),
        todo_id="TODO-40",
    )

    assert result.candidates == ()
    assert result.reads == 0


@pytest.mark.parametrize(
    ("relative", "text", "roles"),
    [
        (
            "docs/implementation,notes.md",
            "TODO-40\n1. Change `src/cache.py`.\n2. Verify with `uv run pytest`.\n",
            ("Plan",),
        ),
        (
            "docs/requirements,notes.md",
            "TODO-40\n## Outcome\nBound cache size.\n"
            "## Acceptance criteria\n- enforced\n",
            ("Spec",),
        ),
        (
            "docs/combined,notes.md",
            "TODO-40\n## Outcome\nBound cache size.\n"
            "## Acceptance criteria\n- enforced\n"
            "1. Change `src/cache.py`.\n2. Verify with `uv run pytest`.\n",
            ("Plan", "Spec"),
        ),
    ],
    ids=["plan", "spec", "combined"],
)
def test_discovery_allows_commas_for_non_reference_roles(
    tmp_path,
    relative,
    text,
    roles,
):
    _write(tmp_path, relative, text)

    result = discover_attachment_candidates(
        tmp_path,
        search_batches=((relative,),),
        todo_id="TODO-40",
    )

    assert [(candidate.path, candidate.roles) for candidate in result.candidates] == [
        (relative, roles)
    ]
    assert result.errors == ()


@pytest.mark.parametrize(
    ("relative", "text", "roles"),
    [
        (
            "docs/gstack/cache-plan.md",
            "Status: APPROVED\nImplementation steps for cache work.\n",
            ("Plan",),
        ),
        (
            "docs/superpowers/plans/cache.md",
            "# Cache implementation plan\n",
            ("Plan",),
        ),
        (
            "docs/other/cache.md",
            "1. Change `src/cache.py`.\n2. Verify with `uv run pytest`.\n",
            ("Plan",),
        ),
        (
            "docs/other/cache-spec.md",
            "## Outcome\nBound cache size.\n## Acceptance criteria\n- limit is enforced\n",
            ("Spec",),
        ),
        (
            "docs/other/combined.md",
            "## Outcome\nBound cache size.\n## Acceptance criteria\n- enforced\n"
            "1. Change `src/cache.py`.\n2. Verify with `uv run pytest`.\n",
            ("Plan", "Spec"),
        ),
    ],
    ids=["gstack", "superpowers", "fallback-plan", "spec", "combined"],
)
def test_recognized_and_fallback_document_formats(relative, text, roles):
    assert classify_attachment_document(relative, text) == roles


@pytest.mark.parametrize(
    ("text", "roles"),
    [
        (
            "- [ ] Add `src/cache.py`.\n- [ ] Verify with `uv run pytest`.\n",
            ("Plan",),
        ),
        ("1. Run `uv run pytest`.\n", ("Reference",)),
        ("1. Test `src/cache.py` with `uv run pytest`.\n", ("Reference",)),
        ("1. Verify `src/cache.py` with `uv run pytest`.\n", ("Reference",)),
    ],
    ids=["implementation-checklist", "run-only", "test-only", "verify-only"],
)
def test_semantic_plan_requires_an_implementation_mutation(text, roles):
    assert classify_attachment_document("docs/other/notes.md", text) == roles


@pytest.mark.parametrize(
    ("text", "roles"),
    [
        (
            "### Task 1: Update cache storage\n"
            "Change `src/cache.py` to bound cache size.\n"
            "Verify with `uv run pytest tests/test_cache.py`.\n",
            ("Plan",),
        ),
        (
            "### Task 1: Verify cache behavior\n"
            "Run `uv run pytest tests/test_cache.py`.\n",
            ("Reference",),
        ),
        (
            "    ### Task 1: Update cache storage\n"
            "Change `src/cache.py` to bound cache size.\n"
            "Verify with `uv run pytest tests/test_cache.py`.\n",
            ("Reference",),
        ),
        (
            "```markdown\n"
            "### Task 1: Update cache storage\n"
            "Change `src/cache.py` to bound cache size.\n"
            "Verify with `uv run pytest tests/test_cache.py`.\n"
            "```\n",
            ("Reference",),
        ),
        (
            "### Task 1: Update cache behavior\n"
            "Verify with `uv run pytest tests/test_cache.py`.\n",
            ("Reference",),
        ),
        (
            "### Task 1: Build cache checks\n"
            "Build: `uv run tpo test --phase build`.\n",
            ("Reference",),
        ),
        (
            "### Task 1: Update cache checks\n"
            "Update: `npm test -- tests/cache.spec.ts`.\n",
            ("Reference",),
        ),
        (
            "    ```markdown\n"
            "### Task 1: Update cache storage\n"
            "Change `src/cache.py` to bound cache size.\n"
            "Verify with `uv run pytest tests/test_cache.py`.\n",
            ("Plan",),
        ),
        (
            "### Task 1: Update cache storage\n"
            "Target: src/cache.py\n"
            "Bound cache size during writes.\n"
            "Verify with `uv run pytest tests/test_cache.py`.\n",
            ("Plan",),
        ),
        (
            "### Task 1: Update cache suite\n"
            "Target: tests/cache.spec.ts\n",
            ("Reference",),
        ),
        (
            "### Task 1: Cache storage\n"
            "1. Update `src/cache.py` to bound cache size.\n"
            "2. Verify with `uv run pytest tests/test_cache.py`.\n",
            ("Plan",),
        ),
        (
            "### Task 1: Cache storage\n"
            "Target: src/cache.py\n"
            "Verify with `uv run pytest tests/test_cache.py`.\n"
            "    1. Update `src/cache.py` to bound cache size.\n",
            ("Reference",),
        ),
        (
            "```markdown```\n"
            "### Task 1: Update cache storage\n"
            "Change `src/cache.py` to bound cache size.\n"
            "Verify with `uv run pytest tests/test_cache.py`.\n",
            ("Plan",),
        ),
    ],
    ids=[
        "implementation-task",
        "verification-only-task",
        "indented-task-example",
        "fenced-task-example",
        "verification-command-without-change-target",
        "tpo-test-command-without-change-target",
        "npm-test-command-without-change-target",
        "indented-fence-before-real-task",
        "separate-target-declaration",
        "target-declaration-without-verification",
        "ordered-mutation-in-task-body",
        "indented-ordered-mutation",
        "inline-backticks-before-real-task",
    ],
)
def test_task_heading_plan_requires_mutation_target_and_verification(text, roles):
    assert classify_attachment_document("docs/other/cache.md", text) == roles


@pytest.mark.parametrize(
    "text",
    [
        (
            "### Task 1: Update health behavior\n"
            "Target: src/health.py\n"
            "Add health checks for readiness.\n"
        ),
        (
            "### Task 1: Add style policy\n"
            "Target: pyproject.toml\n"
            "Add lint rules for imports.\n"
        ),
        (
            "### Task 1: Update cache storage\n"
            "Target: src/cache.py\n"
            "No tests are required.\n"
        ),
        (
            "### Task 1: Update cache storage\n"
            "Target: src/cache.py\n"
            "Test coverage is documented elsewhere.\n"
        ),
        (
            "### Task 1: Update cache storage\n"
            "Target: src/cache.py\n"
            "Tests will not run: `uv run pytest tests/test_cache.py`.\n"
        ),
        (
            "### Task 1: Update style policy\n"
            "Target: pyproject.toml\n"
            "Lint rules are documented elsewhere.\n"
        ),
        (
            "### Task 1: Update health behavior\n"
            "Target: src/health.py\n"
            "- Health checks are documented elsewhere.\n"
        ),
    ],
    ids=[
        "health-check-noun",
        "lint-noun",
        "negated-tests",
        "descriptive-test-coverage",
        "future-negated-test-command",
        "descriptive-lint-state",
        "bulleted-verification-noun",
    ],
)
def test_task_verification_requires_an_affirmative_action(text):
    assert classify_attachment_document("docs/other/cache.md", text) == (
        "Reference",
    )


@pytest.mark.parametrize(
    ("verification", "roles"),
    [
        (
            "Test that cache is bounded after 100 writes.\n",
            ("Plan",),
        ),
        (
            "Check cache remains bounded after 100 writes.\n",
            ("Plan",),
        ),
        (
            "Verify that cache will not exceed 100 entries.\n",
            ("Plan",),
        ),
        ("Test does not run.\n", ("Reference",)),
        ("Test doesn't run.\n", ("Reference",)),
        ("Test won't run.\n", ("Reference",)),
        ("Test cannot run.\n", ("Reference",)),
        ("Test can't execute.\n", ("Reference",)),
        ("Test did not execute.\n", ("Reference",)),
        ("Test shouldn't run.\n", ("Reference",)),
    ],
    ids=[
        "test-that-assertion",
        "check-imperative",
        "verify-negated-outcome",
        "does-not-run",
        "doesnt-run",
        "wont-run",
        "cannot-run",
        "cant-execute",
        "did-not-execute",
        "shouldnt-run",
    ],
)
def test_task_verification_distinguishes_actions_from_non_execution(
    verification,
    roles,
):
    text = (
        "### Task 1: Update cache storage\n"
        "Target: src/cache.py\n"
        f"{verification}"
    )

    assert classify_attachment_document("docs/other/cache.md", text) == roles


@pytest.mark.parametrize(
    ("acceptance", "roles"),
    [
        (
            "- Cache remains bounded after 100 writes.\n",
            ("Plan",),
        ),
        (
            "- [ ] Cache writes never exceed the configured bound.\n",
            ("Plan",),
        ),
        (
            "1. Cache will not exceed 100 entries after writes.\n",
            ("Plan",),
        ),
        ("- Verification steps\n", ("Reference",)),
        ("- Tests are not required.\n", ("Reference",)),
        ("- Tests aren't required.\n", ("Reference",)),
    ],
    ids=[
        "declarative-outcome",
        "checkbox-outcome",
        "negated-bound-outcome",
        "topic-fragment",
        "non-required-tests",
        "contracted-non-required-tests",
    ],
)
def test_task_acceptance_criteria_require_a_concrete_outcome(
    acceptance,
    roles,
):
    text = (
        "### Task 1: Update cache storage\n"
        "Target: src/cache.py\n"
        "#### Acceptance Criteria\n"
        f"{acceptance}"
    )

    assert classify_attachment_document("docs/other/cache.md", text) == roles


@pytest.mark.parametrize(
    "acceptance",
    [
        "",
        "Acceptance criteria are documented elsewhere.\n",
    ],
    ids=["empty-section", "descriptive-noun-mention"],
)
def test_task_acceptance_criteria_require_a_concrete_item(acceptance):
    text = (
        "### Task 1: Update cache storage\n"
        "Target: src/cache.py\n"
        "#### Acceptance Criteria\n"
        f"{acceptance}"
    )

    assert classify_attachment_document("docs/other/cache.md", text) == (
        "Reference",
    )


@pytest.mark.parametrize(
    ("task_body", "roles"),
    [
        (
            "#### Modify `src/cache.py`\n"
            "Verify with `uv run pytest tests/test_cache.py`.\n",
            ("Plan",),
        ),
        (
            "#### Modify `src/cache.py`\n"
            "#### Acceptance Criteria\n"
            "- Cache remains bounded after 100 writes.\n",
            ("Plan",),
        ),
        (
            "```markdown\n"
            "#### Modify `src/cache.py`\n"
            "```\n"
            "Verify with `uv run pytest tests/test_cache.py`.\n",
            ("Reference",),
        ),
        (
            "#### Acceptance Criteria\n"
            "- Cache remains bounded after 100 writes.\n",
            ("Reference",),
        ),
    ],
    ids=[
        "h4-mutation-target",
        "h4-target-before-acceptance-section",
        "fenced-h4-target",
        "acceptance-heading-is-not-target",
    ],
)
def test_task_h4_headings_preserve_action_and_section_state(task_body, roles):
    text = "### Task 1: Cache storage\n" + task_body

    assert classify_attachment_document("docs/other/cache.md", text) == roles


@pytest.mark.parametrize("target", ["CacheTest", "HealthCheck", "PolicyLint"])
def test_target_declaration_accepts_interface_names_with_verification_suffix(target):
    text = (
        "### Task 1: Update cache interface\n"
        f"Target: {target}\n"
        "Verify with `uv run pytest tests/test_cache.py`.\n"
    )

    assert classify_attachment_document("docs/other/cache.md", text) == ("Plan",)


def test_existing_todo25_implementation_document_classifies_as_plan():
    relative = "docs/pipeline/TODO-25-spec-impl.md"
    text = Path(relative).read_text(encoding="utf-8")

    assert classify_attachment_document(relative, text) == ("Plan",)


@pytest.mark.parametrize(
    ("relative", "roles"),
    [
        (
            "docs/superpowers/plans/2026-08-04-todo-40-document-attachments.md",
            ("Plan",),
        ),
        (
            "docs/superpowers/specs/2026-08-04-todo-40-document-attachments-design.md",
            ("Spec",),
        ),
    ],
    ids=["todo-40-plan", "todo-40-design-spec"],
)
def test_todo40_governing_documents_classify_by_actual_role(relative, roles):
    text = Path(relative).read_text(encoding="utf-8")

    assert classify_attachment_document(relative, text) == roles


def test_path_validation_normalizes_inside_paths_and_rejects_escape(tmp_path):
    _write(tmp_path, "docs/plan.md")
    (tmp_path / "docs" / "plans").mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "docs" / "outside-link.md").symlink_to(outside)

    assert validate_attachment_path(tmp_path, "docs/../docs/plan.md") == "docs/plan.md"
    with pytest.raises(AttachmentValidationError, match="resolves outside"):
        validate_attachment_path(tmp_path, "../outside.md")
    with pytest.raises(AttachmentValidationError, match="regular file"):
        validate_attachment_path(tmp_path, "docs/plans")
    with pytest.raises(AttachmentValidationError, match="symlink"):
        validate_attachment_path(tmp_path, "docs/outside-link.md")


def test_reference_representation_has_no_literal_comma_escape(tmp_path):
    _write(tmp_path, "docs/research")
    _write(tmp_path, "notes.md")

    with pytest.raises(AttachmentValidationError, match="contains a comma"):
        validate_attachment_path(
            tmp_path,
            "docs/research,notes.md",
            reference_input=True,
        )

    assert parse_stored_references("docs/research,notes.md") == (
        "docs/research",
        "notes.md",
    )
    assert audit_attachment_fields(
        tmp_path,
        "TODO-12",
        {"Reference": "docs/research,notes.md"},
    ) == []
    empty_item = audit_attachment_fields(
        tmp_path,
        "TODO-12",
        {"Reference": "docs/research, , notes.md, docs/missing.md"},
    )
    assert [(finding.stored_path, finding.defect) for finding in empty_item] == [
        ("", "contains an empty path between separators"),
        ("docs/missing.md", "does not exist"),
    ]


def test_reference_rejects_comma_in_normalized_symlink_target(tmp_path):
    _write(tmp_path, "docs/context,notes.md")
    (tmp_path / "docs" / "context-alias.md").symlink_to("context,notes.md")

    with pytest.raises(AttachmentValidationError, match="contains a comma"):
        validate_attachment_path(
            tmp_path,
            "docs/context-alias.md",
            reference_input=True,
        )

    assert validate_attachment_path(
        tmp_path,
        "docs/context-alias.md",
    ) == "docs/context,notes.md"


def test_audit_validates_each_stored_reference_without_mutation(tmp_path):
    _write(tmp_path, "docs/valid.md")
    fields = {
        "Plan": "docs/missing.md",
        "Reference": "docs/valid.md, ../outside.md, docs/also-missing.md",
    }
    snapshot = fields.copy()

    findings = audit_attachment_fields(tmp_path, "TODO-12", fields)

    assert [(finding.role, finding.stored_path, finding.defect) for finding in findings] == [
        ("Plan", "docs/missing.md", "does not exist"),
        ("Reference", "../outside.md", "resolves outside the repository"),
        (
            "Reference",
            "docs/also-missing.md",
            "does not exist",
        ),
    ]
    assert fields == snapshot


def test_audit_parses_real_todo_markdown_and_never_writes(tmp_path):
    _write(tmp_path, "docs/valid.md")
    todos = tmp_path / "TODOS.md"
    todos.write_text(
        "## Entries\n\n- [ ] **TODO-12: Audit** — Audit attachments\n"
        "  - **What:** x\n  - **Plan:** docs/missing.md\n"
        "  - **Spec:** docs/valid.md\n"
        "  - **Reference:** docs/valid.md, , docs/also-missing.md\n",
        encoding="utf-8",
    )
    before = todos.read_bytes()
    findings = audit_todo_markdown(tmp_path, todos)
    assert [(item.role, item.stored_path) for item in findings] == [
        ("Plan", "docs/missing.md"), ("Reference", ""), ("Reference", "docs/also-missing.md")
    ]
    assert todos.read_bytes() == before


def test_markdown_mutation_matches_exact_todo_id(tmp_path):
    todos = tmp_path / "TODOS.md"
    todos.write_text(
        "## Entries\n\n- [ ] **TODO-10: Ten** — Summary ten\n  - **What:** ten\n\n"
        "- [ ] **TODO-1: One** — Summary one\n  - **What:** one\n",
        encoding="utf-8",
    )
    apply_attachment_selection_to_todo(todos, "TODO-1", AttachmentSelection(plan="docs/one.md"), approved=True)
    text = todos.read_text(encoding="utf-8")
    assert text.index("**Plan:** docs/one.md") > text.index("TODO-1: One")


def test_markdown_mutation_preserves_attachment_label_mentions_in_other_fields(
    tmp_path,
):
    todos = tmp_path / "TODOS.md"
    todos.write_text(
        "## Entries\n\n- [ ] **TODO-1: One** — Summary\n"
        "  - **What:** Explain the **Plan:** role without changing this sentence.\n"
        "  - **Why:** Preserve **Spec:** and **Reference:** mentions too.\n"
        "  - **Plan:** docs/old-plan.md\n",
        encoding="utf-8",
    )

    apply_attachment_selection_to_todo(
        todos,
        "TODO-1",
        AttachmentSelection(plan="docs/new-plan.md"),
        approved=True,
    )

    text = todos.read_text(encoding="utf-8")
    assert "  - **What:** Explain the **Plan:** role without changing this sentence." in text
    assert "  - **Why:** Preserve **Spec:** and **Reference:** mentions too." in text
    assert "  - **Plan:** docs/old-plan.md" not in text
    assert "  - **Plan:** docs/new-plan.md" in text


def test_markdown_mutation_preserves_nested_attachment_examples(tmp_path):
    todos = tmp_path / "TODOS.md"
    nested_example = "    - **Plan:** docs/example-only.md"
    todos.write_text(
        "## Entries\n\n- [ ] **TODO-1: One** — Summary\n"
        "  - **Context:** Example syntax follows.\n"
        f"{nested_example}\n"
        "  - **Plan:** docs/old-plan.md\n",
        encoding="utf-8",
    )

    apply_attachment_selection_to_todo(
        todos,
        "TODO-1",
        AttachmentSelection(plan="docs/new-plan.md"),
        approved=True,
    )

    text = todos.read_text(encoding="utf-8")
    assert nested_example in text
    assert "  - **Plan:** docs/old-plan.md" not in text
    assert "  - **Plan:** docs/new-plan.md" in text


def test_markdown_mutation_replaces_only_selected_entry_span_under_entries(
    tmp_path,
):
    todos = tmp_path / "TODOS.md"
    repeated_entry = (
        "- [ ] **TODO-1: One** — Summary\n"
        "  - **What:** Keep the repeated example intact.\n"
        "  - **Plan:** docs/old-plan.md\n"
    )
    todos.write_text(
        "# TODOS\n\n## Metadata\n\nNEXT_TODO_ID: 2\n\n"
        "## Entry Schema\n\n```markdown\n"
        f"{repeated_entry}```\n\n## Entries\n\n{repeated_entry}",
        encoding="utf-8",
    )
    before = todos.read_text(encoding="utf-8")
    schema_before = before.split("## Entry Schema", 1)[1].split("## Entries", 1)[0]

    apply_attachment_selection_to_todo(
        todos,
        "TODO-1",
        AttachmentSelection(plan="docs/new-plan.md"),
        approved=True,
    )

    after = todos.read_text(encoding="utf-8")
    schema_after = after.split("## Entry Schema", 1)[1].split("## Entries", 1)[0]
    entries_after = after.split("## Entries", 1)[1]
    assert schema_after == schema_before
    assert "  - **Plan:** docs/old-plan.md" not in entries_after
    assert "  - **Plan:** docs/new-plan.md" in entries_after


@pytest.mark.parametrize(
    "document",
    [
        "# TODOS\n\n## Metadata\n\nNEXT_TODO_ID: 2\n",
        (
            "## Entries\n\n- [ ] **TODO-1: One** — First\n"
            "  - **What:** First entry.\n\n"
            "## Entries\n\n- [ ] **TODO-2: Two** — Second\n"
            "  - **What:** Second entry.\n"
        ),
        "## Entries\n\n- [ ] **TODO-2: Two** — Second\n  - **What:** Second.\n",
    ],
    ids=["missing-entries", "duplicate-entries", "missing-todo"],
)
def test_markdown_mutation_rejects_invalid_target_without_writing(tmp_path, document):
    todos = tmp_path / "TODOS.md"
    todos.write_text(document, encoding="utf-8")

    with pytest.raises(ValueError):
        apply_attachment_selection_to_todo(
            todos,
            "TODO-1",
            AttachmentSelection(plan="docs/new-plan.md"),
            approved=True,
        )

    assert todos.read_text(encoding="utf-8") == document


def test_markdown_mutation_preserves_sibling_entries_and_surrounding_sections(
    tmp_path,
):
    todos = tmp_path / "TODOS.md"
    prefix = "# TODOS\n\n## Metadata\n\nNEXT_TODO_ID: 4\n\n## Entries\n\n"
    first = "- [ ] **TODO-1: One** — First\n  - **What:** First entry.\n\n"
    target = (
        "- [ ] **TODO-2: Two** — Target\n"
        "  - **What:** Target entry.\n"
        "  - **Plan:** docs/old-plan.md\n\n"
    )
    third = "- [ ] **TODO-3: Three** — Third\n  - **What:** Third entry.\n\n"
    suffix = "## Notes\n\nKeep this suffix byte-for-byte.\n"
    todos.write_text(prefix + first + target + third + suffix, encoding="utf-8")

    apply_attachment_selection_to_todo(
        todos,
        "TODO-2",
        AttachmentSelection(plan="docs/new-plan.md"),
        approved=True,
    )

    expected_target = target.replace("docs/old-plan.md", "docs/new-plan.md")
    assert todos.read_text(encoding="utf-8") == prefix + first + expected_target + third + suffix


def test_markdown_mutation_bounds_last_entry_at_next_top_level_section(tmp_path):
    todos = tmp_path / "TODOS.md"
    prefix = "## Entries\n\n"
    target = "- [ ] **TODO-1: One** — Summary\n  - **What:** Work.\n\n"
    notes = "## Notes\n\nKeep this suffix byte-for-byte.\n"
    todos.write_text(prefix + target + notes, encoding="utf-8")

    apply_attachment_selection_to_todo(
        todos,
        "TODO-1",
        AttachmentSelection(plan="docs/plan.md"),
        approved=True,
    )

    expected_target = target.replace(
        "\n\n",
        "\n  - **Plan:** docs/plan.md\n\n",
        1,
    )
    assert todos.read_text(encoding="utf-8") == prefix + expected_target + notes


def test_markdown_mutation_uses_fresh_text_read_under_lock(tmp_path, monkeypatch):
    from tests.skill_test_environment import skill_logic

    todos = tmp_path / "TODOS.md"
    todos.write_text("## Entries\n\n- [ ] **TODO-1: One** — Summary\n  - **What:** old\n", encoding="utf-8")
    monkeypatch.setattr(
        skill_logic,
        "_before_todo_lock",
        lambda: todos.write_text(todos.read_text(encoding="utf-8").replace("old", "fresh"), encoding="utf-8"),
    )
    apply_attachment_selection_to_todo(todos, "TODO-1", AttachmentSelection(plan="docs/plan.md"), approved=True)
    assert "**What:** fresh" in todos.read_text(encoding="utf-8")


def test_legacy_entry_without_attachments_remains_valid(tmp_path):
    fields = {
        "What": "Keep the old entry valid",
        "Why": "Attachments are optional",
        "Decisions": "Priority `P2`",
    }

    assert audit_attachment_fields(tmp_path, "TODO-1", fields) == []
