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


def test_native_sdd_docs_describe_compiled_plan_to_kanban_lifecycle():
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
    assert "controller gate" in combined
    assert "legacy" in combined.lower()
    assert "cron" in combined and "TPO" in combined and "Kanban" in combined
    assert "registration.json" in combined
    assert "review-fix" in combined
    assert "five" in combined.lower() and "needs_input" in combined
    assert "never resets" in combined.lower()


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
