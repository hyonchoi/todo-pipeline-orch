from pathlib import Path


def test_harness_docs_describe_profile_selection_and_fail_closed_rules():
    root = Path(__file__).resolve().parents[1]
    cli_reference = (root / "docs" / "reference-cli.md").read_text()
    harness_guide = (root / "docs" / "howto-live-integration-test-harness.md").read_text()

    assert "`--profile`" in cli_reference
    assert "`--repo`" in cli_reference
    assert "`--init-sandbox`" in cli_reference
    assert "Unverified" in cli_reference
    assert "unsafe_terminal" in cli_reference
    assert "`--phase`" not in cli_reference
    assert "tpo test --repo OWNER/NAME --profile" in harness_guide
    assert "tpo test --repo OWNER/NAME --init-sandbox" in harness_guide
    assert "sandbox_not_quiescent" in harness_guide
    assert "force-with-lease" in harness_guide
    assert "gh_override_forbidden" in harness_guide
    assert "TPO_FAKE_GH_STATE" not in harness_guide
    assert "repo_missing" in harness_guide and "repo_missing" in cli_reference
    for name in ("README.md", "docs/ARCHITECTURE.md"):
        assert "mock integration" not in (root / name).read_text().lower(), name
    assert (root / "docs" / "howto-mock-integration-test-harness.md").exists() is False


def test_native_sdd_docs_describe_the_single_implementation_card_lifecycle():
    root = Path(__file__).resolve().parents[1]
    documents = [
        (root / "README.md").read_text(),
        (root / "docs" / "ARCHITECTURE.md").read_text(),
        (root / "docs" / "hermes-state-machine.md").read_text(),
        (root / "docs" / "howto-native-sdd-profile.md").read_text(),
        (root / "docs" / "reference-kanban-as-scheduler.md").read_text(),
    ]
    combined = "\n".join(documents)
    assert "Hermes >= 0.19.0" in combined
    assert "```json tpo-plan" in combined
    assert "legacy" in combined.lower()
    # The Plan compiles to NO cards. The profile's implementation phase gets one
    # card whatever the task count, its own prompt is what orders the Plan's
    # tasks, and the manifest survives as the commit-count bound TPO verifies.
    # Named explicitly so the lifecycle cannot be re-described without the key.
    assert "phase_4_development" in combined
    assert "atomic commit per" in combined
    assert "len(tasks)" in combined
    # The step-key change is a persisted-format break in both directions, and an
    # operator upgrading or downgrading with a run in flight must find it here.
    assert "registration_invalid" in combined
    # Every phrasing the deleted per-Plan-task fan-out left behind, forbidden
    # rather than merely corrected: each of these was true of the old lifecycle,
    # so a well-meaning edit could reintroduce one and the docs would then
    # describe a board TPO no longer builds. ``controller gate`` used to be a
    # REQUIRED substring here, and the only thing still satisfying it was a
    # README sentence describing gates that had been deleted two releases
    # earlier -- an assertion that a stale claim can satisfy pins nothing.
    for phrasing in (
        "controller gate",
        "one worker card per",
        "card per task",
        "card and controller gate",
        "single development card",
        "plan worker",
        "per ordered task",
    ):
        assert phrasing not in combined, phrasing
    assert "cron" in combined and "TPO" in combined and "Kanban" in combined
    assert "registration.json" in combined
    assert "review-fix" in combined
    # Review is binary and the profile owns it: the reviewer commits its own
    # fixes, so the docs must describe the card's own status as the outcome and
    # must not resurrect the bounded remediation rounds that contradicted the
    # profile.
    assert "phase_5_review" in combined
    assert "blocked" in combined and "accepted-review-head" in combined
    assert "re-review" not in combined
    assert "fix-validation" not in combined
    assert "never resets" in combined.lower()
    # No card has ever existed for a gate phase, in any profile: both card-
    # creating loops in ``kanban_tasks`` (``_register_todo_phases`` and
    # ``planned_phase_keys``) ``continue`` unconditionally on ``phase.gate``,
    # ``phase_9_human_review`` is ``gate: true``, and no code anywhere issues a
    # ``hermes kanban block`` -- the only ``needs_input`` string in the package
    # is inside the worker-facing prompt telling the DISPATCHER to block itself.
    # These docs described the terminal boundary as a ``human-gate`` card
    # sitting in ``needs_input``, which sent operators looking for a card that
    # cannot exist, so the false phrasings are forbidden rather than merely
    # corrected.
    assert "human-gate" not in combined
    assert "sticky `needs_input`" not in combined
    gate_block_lines = [
        line
        for line in combined.splitlines()
        if "needs_input" in line.lower() and "gate" in line.lower()
    ]
    assert gate_block_lines == [], gate_block_lines
    # The replacement wording, so a correction cannot be undone by deletion.
    assert "human merge decision" in combined


def test_pending_review_create_row_names_the_real_recovery_path():
    """The marker recovers nothing, and the run-evidence row must not imply it does.

    ``_persist_pending_create`` writes ``pending-review-create.json`` and
    ``_clear_pending_create`` deletes it; no reader exists anywhere. The pending
    marker ``reconcile_pending_task_create`` really reads is
    ``pending-task-create.json``, a different file in a different module. What
    actually recovers an ambiguous dynamic-card create is
    ``_find_task_id_in_snapshot`` -- re-run before every create attempt, against
    an idempotency-keyed create -- plus ``RetryableReviewRegistration``, which
    both reconcilers turn into a retry on the next tick. A row that presents the
    marker as the recovery input sends an operator to a file that cannot answer
    the question.
    """
    root = Path(__file__).resolve().parents[1]
    guide = (root / "docs" / "howto-native-sdd-profile.md").read_text()
    (row,) = [
        line
        for line in guide.splitlines()
        if line.startswith("| `pending-review-create.json`")
    ]
    assert "nothing reads it back" in row, row
    assert "_find_task_id_in_snapshot" in row, row
    assert "RetryableReviewRegistration" in row, row
    assert "pending-task-create.json" in row, row

    # The row's claim has to stay true of the code: one writer module, no reader.
    naming = sorted(
        path.name
        for path in (root / "hermes_pipeline").rglob("*.py")
        if "pending-review-create.json" in path.read_text()
    )
    assert naming == ["review_reconciliation.py"], naming


def test_current_runtime_docs_do_not_describe_deleted_review_phase_module():
    root = Path(__file__).resolve().parents[1]
    current_docs = (
        root / "README.md",
        root / "docs" / "ARCHITECTURE.md",
        root / "docs" / "hermes-state-machine.md",
        root / "docs" / "howto-native-sdd-profile.md",
        root / "docs" / "howto-review-outcomes.md",
        root / "docs" / "reference-kanban-as-scheduler.md",
    )
    for path in current_docs:
        assert "review_phase.py" not in path.read_text(), path


def test_plan_template_contains_machine_manifest_contract():
    root = Path(__file__).resolve().parents[1]
    template = (root / "docs" / "templates" / "tpo-plan.md").read_text()
    for phrase in (
        "```json tpo-plan",
        '"schema_version": 1',
        '"todo_id": "TODO-N"',
        '"acceptance_criteria"',
        '"verification"',
        '"commit_message"',
    ):
        assert phrase in template
