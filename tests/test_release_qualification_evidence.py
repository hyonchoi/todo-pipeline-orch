from __future__ import annotations

import re
from pathlib import Path

import pytest

EVIDENCE_ROOT = Path("docs/release-evidence/agent-clients")
CANDIDATE_ROOT = EVIDENCE_ROOT / "candidate-source-snapshot"
SKILL_IDS = (
    "autoplan",
    "cso",
    "document-generate",
    "document-release",
    "qa",
    "review",
    "ship",
)
SUPERPOWERS_SKILLS = ("subagent-driven-development", "writing-plans")
INVOCATIONS = {
    "claude": (
        "/autoplan",
        "/writing-plans",
        "/subagent-driven-development",
        "/review",
        "/cso",
        "/qa",
        "/document-release",
        "/document-generate",
        "/ship",
    ),
    "codex": (
        "$autoplan",
        "$superpowers:writing-plans",
        "$superpowers:subagent-driven-development",
        "$review",
        "$cso",
        "$qa",
        "$document-release",
        "$document-generate",
        "$ship",
    ),
}


def _field(document: str, name: str) -> str:
    match = re.search(rf"^- {re.escape(name)}: (.+)$", document, re.MULTILINE)
    assert match is not None, f"missing field: {name}"
    return match.group(1).strip("`")


def _section(document: str, heading: str) -> str:
    match = re.search(
        rf"^### {re.escape(heading)}\n(?P<body>.*?)(?=^### |\Z)",
        document,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"missing section: {heading}"
    return match.group("body")


def _fenced(section: str, label: str, language: str) -> str:
    match = re.search(
        rf"^{re.escape(label)}:\n\n```{language}\n(?P<body>.*?)\n```",
        section,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"missing {label.lower()} {language} block"
    return match.group("body")


@pytest.mark.parametrize("client", ("claude", "codex"))
def test_candidate_artifact_has_every_required_field(client: str):
    path = CANDIDATE_ROOT / f"gstack-{client}.md"
    document = path.read_text()

    assert _field(document, "Evidence status") == "candidate/source-snapshot"
    assert _field(document, "Release") == "not selected"
    assert re.fullmatch(r"\d+\.\d+\.\d+", _field(document, "Source VERSION"))
    assert re.fullmatch(r"[0-9a-f]{40}", _field(document, "Source commit"))
    assert _field(document, "Profile/client") == f"gstack / {client}"
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
        _field(document, "Timestamp"),
    )
    assert _field(document, "Environment")
    assert _field(document, "Client")
    assert re.fullmatch(r"\d+\.\d+\.\d+\.\d+", _field(document, "gstack"))
    assert re.fullmatch(r"\d+\.\d+\.\d+", _field(document, "superpowers"))
    assert _field(document, "gstack skill root") == "~/.local/share/gstack"
    plugin_owner = (
        "claude-plugins-official" if client == "claude" else "openai-curated-remote"
    )
    assert _field(
        document, "superpowers plugin source"
    ) == f"{plugin_owner}/superpowers@6.2.0"
    assert _field(document, "Verifier")
    assert _field(document, "Result") == "PASS"


@pytest.mark.parametrize("client", ("claude", "codex"))
def test_candidate_artifact_captures_complete_discovery_commands_and_output(
    client: str,
):
    document = (CANDIDATE_ROOT / f"gstack-{client}.md").read_text()

    gstack = _section(document, "gstack skills")
    gstack_command = _fenced(gstack, "Command", "bash")
    gstack_output = _fenced(gstack, "Captured output", "text")
    assert gstack_command.startswith("rtk ls ")
    assert f'$HOME/.{client}/skills' in gstack_command
    assert {
        skill_id
        for skill_id in SKILL_IDS
        if f"/{skill_id}/SKILL.md" in gstack_output
    } == set(SKILL_IDS)
    assert len(gstack_output.splitlines()) == len(SKILL_IDS)

    superpowers = _section(document, "superpowers plugin")
    superpowers_command = _fenced(superpowers, "Command", "bash")
    superpowers_output = _fenced(superpowers, "Captured output", "text")
    assert superpowers_command.startswith("rtk ls ")
    if client == "claude":
        assert "/claude-plugins-official/superpowers" in superpowers_command
    else:
        assert "openai-curated-remote/superpowers" in superpowers_command
    assert all(
        f"/skills/{skill_id}/SKILL.md" in superpowers_output
        for skill_id in SUPERPOWERS_SKILLS
    )
    manifest = ".claude-plugin" if client == "claude" else ".codex-plugin"
    assert f"/{manifest}/plugin.json" in superpowers_output
    assert len(superpowers_output.splitlines()) == 3

    invocation_section = document.split("## Invocation forms", 1)[1]
    assert all(
        f"`{invocation}`" in invocation_section
        for invocation in INVOCATIONS[client]
    )


def test_candidate_evidence_does_not_claim_a_final_release():
    assert not (EVIDENCE_ROOT / "0.6.6").exists()
    protocol = Path("docs/release-qualification-agent-clients.md").read_text()
    schema = (EVIDENCE_ROOT / "README.md").read_text()
    for document in (protocol, schema):
        assert "`/ship`" in document
        assert "candidate/source-snapshot" in document
        assert "release commit" in document
