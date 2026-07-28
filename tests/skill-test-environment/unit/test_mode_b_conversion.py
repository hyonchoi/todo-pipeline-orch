"""Mode B header-based conversion oracle tests."""

from tests.skill_test_environment.skill_logic import convert_header_based_todos


def test_mode_b_conversion_sections_entries_and_reference_output():
    legacy = (
        "# TODOS\n\n"
        "## Open\n\n"
        "### Build the importer\n"
        "**What:** Build the importer. Keep later details.\n"
        "**Why:** Projects need migration.\n"
        "**Resolution:** Use a deterministic parser.\n\n"
        "### Needs research\n"
        "**What:** Investigate the old format.\n\n"
        "## Completed\n\n"
        "### Ship the parser — Completed\n"
        "**What:** Ship the parser.\n"
        "**Why:** Migration is ready.\n"
        "**Completed:** 2026-07-27\n"
        "\n"
        "- [ ] **TODO-3: Preserve me** — Existing canonical entry\n"
        "  - **What:** Keep this entry.\n"
        "  - **Why:** Hybrid conversion must not drop it.\n"
        "  - **Decisions:** Priority `P1`\n"
    )
    archive = "# TODOS Archive\n\n- [x] **TODO-4: Earlier** — Done\n"

    converted, reference = convert_header_based_todos(legacy, archive)

    assert converted.startswith(
        "# TODOS\n\n## Metadata\n\nNEXT_TODO_ID: 7\n\n## Entry Schema\n"
    )
    assert "## Entries\n\n- [ ] **TODO-5: Build the importer** — Build the importer." in converted
    assert "- [x] **TODO-6: Ship the parser** — Ship the parser." in converted
    assert "- [ ] **TODO-3: Preserve me** — Existing canonical entry" in converted
    assert "- **Resolved design:** Use a deterministic parser." in converted
    assert converted.count("<<USER-REVIEW>>") == 2
    assert "### Needs research" not in converted
    assert "# TODOS Reference" in reference
    assert "### Needs research" in reference
    assert "**What:** Investigate the old format." in reference

    second_conversion, second_reference = convert_header_based_todos(converted, archive)
    assert second_conversion == converted
    assert second_reference == ""
