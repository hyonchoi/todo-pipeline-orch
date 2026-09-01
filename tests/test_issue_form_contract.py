"""Pin the GitHub issue form to the ``github_issues`` body/label contract.

The form (``.github/ISSUE_TEMPLATE/tpo-todo.yml``) is a machine-consumed schema:
GitHub renders each field as ``### <Label>\\n\\n<value>`` (``_No response_`` for
empty optionals), which is exactly what ``parse_issue_body`` consumes.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from hermes_pipeline.github_issues import (
    KNOWN_SECTIONS,
    LABEL_VOCABULARY,
    NO_RESPONSE,
    PHASE_OPTIONS,
    REQUIRED_SECTIONS,
    TODO_LABEL,
    parse_issue_body,
    phase_label,
    render_issue_body,
)

ROOT = Path(__file__).parents[1]
FORM_PATH = ROOT / ".github" / "ISSUE_TEMPLATE" / "tpo-todo.yml"
PHASES_PATH = ROOT / "hermes_pipeline" / "data" / "phase-profiles" / "gstack" / "phases.yaml"
# Profile phase name -> dropdown option; "Design" predates the profile name and is kept for
# continuity with migrated TODOS.md entries (the phase label is a mirror only).
PHASE_OPTION_ALIASES = {"2 (Autoplan)": "2 (Design)"}
REQUIRED_FORM_FIELDS = {
    "Summary", "What", "Why", "Branch",
    "Priority", "Effort", "Phase", "Test Coverage", "Security Review", "UI Review",
}
PHASE_NAME_RE = re.compile(r"^Phase ([0-9.]+): (.+)$")
FORM_SECTIONS = tuple(s for s in KNOWN_SECTIONS if s != "Legacy ID")
LABEL_NAMES = {name for name, _color, _description in LABEL_VOCABULARY}
# Dropdown label -> label-vocabulary prefix whose suffixes must equal its options.
DROPDOWN_LABEL_PREFIX = {
    "Priority": "priority:",
    "Effort": "effort:",
    "Test Coverage": "test-coverage:",
    "Security Review": "security-review:",
    "UI Review": "ui-review:",
}
SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")
PHASE_LABEL_RE = re.compile(r"^phase:[0-9]+(-[0-9]+)*-[a-z0-9]+(-[a-z0-9]+)*$")


@pytest.fixture(scope="module")
def form() -> dict:
    return yaml.safe_load(FORM_PATH.read_text())


@pytest.fixture(scope="module")
def body(form: dict) -> list[dict]:
    return form["body"]


def _is_required(item: dict) -> bool:
    return bool((item.get("validations") or {}).get("required"))


def test_form_has_legal_issue_form_shape(form, body):
    assert form["name"] == "TPO TODO"
    assert isinstance(form["description"], str) and form["description"]
    assert isinstance(body, list) and body
    for item in body:
        assert item["type"] in {"input", "textarea", "dropdown"}, item
        assert isinstance(item["id"], str) and item["id"]
        assert isinstance(item["attributes"]["label"], str)
        if item["type"] == "dropdown":
            assert isinstance(item["attributes"]["options"], list)


def test_static_labels_exist_in_vocabulary_and_mark_new_issues_as_untriaged_todos(form):
    # GitHub silently drops labels that do not exist on the repo, so every static label
    # must be one that the label sync (ensure_labels / LABEL_VOCABULARY) creates.
    assert set(form["labels"]) <= LABEL_NAMES
    assert set(form["labels"]) == {TODO_LABEL, "needs-triage"}


def test_form_has_no_default_title(form):
    assert "title" not in form


def test_field_labels_are_known_sections_in_order_without_legacy_id(body):
    assert [item["attributes"]["label"] for item in body] == list(FORM_SECTIONS)


def test_required_fields_are_exactly_the_intended_set(body):
    required = {item["attributes"]["label"] for item in body if _is_required(item)}
    assert required == REQUIRED_FORM_FIELDS
    assert required >= set(REQUIRED_SECTIONS)


def test_dropdown_options_match_label_vocabulary_suffixes(body):
    dropdowns = {item["attributes"]["label"]: item for item in body if item["type"] == "dropdown"}
    assert set(dropdowns) == set(DROPDOWN_LABEL_PREFIX) | {"Phase"}
    for label, item in dropdowns.items():
        options = item["attributes"]["options"]
        assert options and all(isinstance(o, str) and o.strip() for o in options), label
        assert _is_required(item), f"{label} dropdown must be required"
        if label in DROPDOWN_LABEL_PREFIX:
            prefix = DROPDOWN_LABEL_PREFIX[label]
            expected = [n[len(prefix):] for n in (name for name, *_ in LABEL_VOCABULARY) if n.startswith(prefix)]
            assert options == expected, label


def test_phase_options_map_to_well_formed_phase_labels(body):
    (phase,) = [item for item in body if item["attributes"]["label"] == "Phase"]
    labels = [phase_label(option) for option in phase["attributes"]["options"]]
    assert len(set(labels)) == len(labels)
    for option, label in zip(phase["attributes"]["options"], labels):
        assert PHASE_LABEL_RE.match(label), (option, label)
        assert label in LABEL_NAMES  # mirror labels must exist so `tpo todos audit --fix` can apply them
    assert tuple(phase["attributes"]["options"]) == PHASE_OPTIONS


def test_phase_options_are_the_gstack_profile_phases(body):
    (phase,) = [item for item in body if item["attributes"]["label"] == "Phase"]
    expected = []
    for entry in yaml.safe_load(PHASES_PATH.read_text())["phases"]:
        num, name = PHASE_NAME_RE.match(entry["name"]).groups()
        option = f"{num} ({name})"
        expected.append(PHASE_OPTION_ALIASES.get(option, option))
    assert phase["attributes"]["options"] == expected


def test_github_rendering_of_form_round_trips_through_parser(body):
    fields: dict[str, str] = {}
    for item in body:
        label = item["attributes"]["label"]
        if item["type"] == "dropdown":
            fields[label] = item["attributes"]["options"][0]
        elif _is_required(item):
            fields[label] = f"sample {label.lower()} value"
        # optional free-text fields are left empty -> GitHub renders _No response_
    rendered = render_issue_body(fields, include_empty=True)
    # Mirror GitHub's issue-form rendering: one H3 per field, in form order.
    github_body = "\n".join(
        f"### {item['attributes']['label']}\n\n{fields.get(item['attributes']['label']) or NO_RESPONSE}\n"
        for item in body
    )
    # The renderer additionally emits the migration-only Legacy ID block; GitHub does not.
    assert rendered == github_body + f"\n### Legacy ID\n\n{NO_RESPONSE}\n"
    expected = {k: (v,) for k, v in fields.items()}
    assert parse_issue_body(github_body) == expected
    assert parse_issue_body(rendered) == expected


def test_field_ids_are_unique_snake_case(body):
    ids = [item["id"] for item in body]
    assert len(set(ids)) == len(ids)
    assert all(SNAKE_CASE_RE.match(i) for i in ids), ids


TRIAGE_DOC = ROOT / "docs" / "agents" / "triage-labels.md"
TRIAGE_ROW_RE = re.compile(r"^\| `[^`]+` +\| `([^`]+)` +\| (.+?) +\|$")


def test_triage_label_descriptions_match_triage_labels_doc():
    rows = dict(
        match.groups()
        for line in TRIAGE_DOC.read_text(encoding="utf-8").splitlines()
        if (match := TRIAGE_ROW_RE.match(line))
    )
    assert set(rows) == {"needs-triage", "needs-info", "ready-for-agent", "ready-for-human", "wontfix"}
    descriptions = {name: description for name, _color, description in LABEL_VOCABULARY}
    assert {name: descriptions[name] for name in rows} == rows


def test_tpo_labels_have_distinct_colors():
    colors = [color for name, color, _ in LABEL_VOCABULARY if name.startswith("tpo:")]
    assert len(colors) == 3 and len(set(colors)) == len(colors)
