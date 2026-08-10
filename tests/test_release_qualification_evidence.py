from __future__ import annotations

import re
import subprocess
import tomllib
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


def _source_version(document: str) -> str:
    for field in ("Source version", "Source VERSION"):
        match = re.search(rf"^- {field}: (.+)$", document, re.MULTILINE)
        if match is not None:
            return match.group(1).strip("`")
    raise AssertionError("missing source version field")


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
    assert re.fullmatch(r"\d+\.\d+\.\d+", _field(document, "Source version"))
    assert re.fullmatch(r"[0-9a-f]{40}", _field(document, "Source commit"))
    assert _field(document, "Profile/client") == f"gstack / {client}"
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
        _field(document, "Timestamp"),
    )
    assert _field(document, "Environment")
    assert re.fullmatch(
        r"Hermes Agent v\d+\.\d+\.\d+ \(\d+\.\d+\.\d+\.\d+\)",
        _field(document, "Hermes"),
    )
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

    fixture = document.split(
        "## Disposable fixture and representative invocation", 1
    )[1].split("## Invocation forms", 1)[0]
    fixture_command = _fenced(fixture, "Fixture isolation command", "bash")
    fixture_output = _fenced(fixture, "Captured isolation output", "text")
    invocation_command = _fenced(
        fixture, "Representative invocation command", "bash"
    )
    invocation_output = _fenced(
        fixture, "Captured transcript excerpt", "text"
    )
    assert "mktemp -d" in fixture_command
    assert "git -C" in fixture_command
    assert ".claude: absent" in fixture_output
    assert ".agents: absent" in fixture_output
    expected_invocation = "/autoplan" if client == "claude" else r"\$autoplan"
    assert expected_invocation in invocation_command
    assert "autoplan" in invocation_output.lower()
    assert "started" in invocation_output.lower()
    assert "unknown-skill" not in invocation_output.lower()

    hermes_discovery = _section(document, "Hermes enabled-skill discovery")
    discovery_command = _fenced(hermes_discovery, "Command", "bash")
    discovery_output = _fenced(hermes_discovery, "Captured output", "text")
    assert discovery_command == "rtk hermes skills list --enabled-only"
    assert "ai-coding-agents" in discovery_output
    assert "enabled" in discovery_output

    dispatcher = _section(document, "Hermes dispatcher invocation")
    dispatcher_command = _fenced(dispatcher, "Command", "bash")
    dispatcher_output = _fenced(dispatcher, "Captured output", "text")
    expected_command = (
        'claude -p "Respond with exactly CLAUDE_DISPATCH_OK"'
        if client == "claude"
        else 'codex exec -s read-only "Respond with exactly CODEX_DISPATCH_OK"'
    )
    expected_marker = (
        "CLAUDE_DISPATCH_OK" if client == "claude" else "CODEX_DISPATCH_OK"
    )
    assert dispatcher_command.startswith("rtk hermes chat -q ")
    assert "ai-coding-agents" in dispatcher_command
    assert expected_command in dispatcher_output
    assert "Exit code: 0" in dispatcher_output
    assert f"Stdout marker: {expected_marker}" in dispatcher_output


def test_candidate_evidence_does_not_claim_a_final_release():
    assert not (EVIDENCE_ROOT / "0.6.6").exists()
    protocol = Path("docs/release-qualification-agent-clients.md").read_text()
    schema = (EVIDENCE_ROOT / "README.md").read_text()
    for document in (protocol, schema):
        assert "Python" in document
        assert "candidate/source-snapshot" in document
        assert "release commit" in document


@pytest.mark.parametrize("client", ("claude", "codex"))
def test_release_final_artifact_matches_selected_version_and_candidate(client: str):
    version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)

    candidate = (CANDIDATE_ROOT / f"gstack-{client}.md").read_text()
    release = (EVIDENCE_ROOT / version / f"gstack-{client}.md").read_text()

    assert _field(release, "Evidence status") == "release-final"
    assert _field(release, "Release") == version
    assert _source_version(release) == version
    assert _field(release, "Source commit") == _field(candidate, "Source commit")
    assert _field(release, "Profile/client") == f"gstack / {client}"
    assert _field(release, "Result") == "PASS"
    subprocess.run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            _field(release, "Source commit"),
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    for heading in (
        "gstack skills",
        "superpowers plugin",
        "Hermes enabled-skill discovery",
        "Hermes dispatcher invocation",
    ):
        assert _section(release, heading) == _section(candidate, heading)
    assert release.split("## Invocation forms", 1)[1] == candidate.split(
        "## Invocation forms", 1
    )[1]
