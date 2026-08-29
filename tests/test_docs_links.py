from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).parents[1]
DOCS = (
    ROOT / "README.md",
    ROOT / "docs" / "reference-cli.md",
    ROOT / "docs" / "howto-github-issues-todos.md",
    ROOT / "docs" / "tutorial-getting-started.md",
    ROOT / "docs" / "agents" / "issue-tracker.md",
    ROOT / "docs" / "agents" / "triage-labels.md",
    ROOT / "docs" / "howto-agent-skills-profile.md",
    ROOT / "docs" / "release-qualification-agent-clients.md",
    ROOT / "docs" / "release-evidence" / "agent-clients" / "README.md",
)
LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)


def _slug(heading: str) -> str:
    value = re.sub(r"<[^>]+>", "", heading).strip().lower()
    value = re.sub(r"[^\w -]", "", value)
    return re.sub(r"[\s-]+", "-", value)


@pytest.mark.parametrize("source", DOCS, ids=lambda path: str(path.relative_to(ROOT)))
def test_local_markdown_links_and_fragments_resolve(source: Path):
    failures: list[str] = []
    for raw_target in LINK_RE.findall(source.read_text()):
        target = raw_target.strip("<>")
        parsed = urlsplit(target)
        if parsed.scheme or target.startswith(("mailto:", "#")):
            continue
        destination = (source.parent / unquote(parsed.path)).resolve()
        if not destination.exists():
            failures.append(f"{target}: missing {destination.relative_to(ROOT)}")
            continue
        if parsed.fragment and destination.suffix.lower() == ".md":
            anchors = {
                _slug(value)
                for value in HEADING_RE.findall(destination.read_text())
            }
            if unquote(parsed.fragment) not in anchors:
                failures.append(f"{target}: missing fragment")
    assert not failures, "\n".join(failures)
