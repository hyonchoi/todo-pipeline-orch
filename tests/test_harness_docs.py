from pathlib import Path


def test_harness_docs_describe_profile_selection_and_fail_closed_rules():
    root = Path(__file__).resolve().parents[1]
    cli_reference = (root / "docs" / "reference-cli.md").read_text()
    harness_guide = (root / "docs" / "howto-mock-integration-test-harness.md").read_text()

    assert "`--profile`" in cli_reference
    assert "Unverified" in cli_reference
    assert "Gate phases are rejected" in cli_reference
    assert "tpo test --fixture happy-path --profile" in harness_guide
    assert "terminal gate" in harness_guide
    assert "profile attribution" in harness_guide.lower()
