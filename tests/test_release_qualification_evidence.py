from __future__ import annotations

import re
from pathlib import Path

import pytest

EVIDENCE_ROOT = Path("docs/release-evidence/agent-clients")
HISTORICAL_ARTIFACTS = sorted(
    path
    for path in EVIDENCE_ROOT.glob("*/*.md")
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", path.parent.name)
)


def _field(document: str, name: str) -> str:
    values = re.findall(rf"^- {re.escape(name)}: (.+)$", document, re.MULTILINE)
    assert len(values) == 1, f"expected one field: {name}"
    return values[0].strip("`")


@pytest.mark.parametrize("path", HISTORICAL_ARTIFACTS, ids=str)
def test_historical_artifact_identifies_its_recorded_release(path: Path):
    """Check archived record structure without qualifying the current package."""
    document = path.read_text()

    assert _field(document, "Evidence status") == "release-final"
    assert _field(document, "Release") == path.parent.name
    assert re.fullmatch(r"[0-9a-f]{40}", _field(document, "Source commit"))
    source_fields = re.findall(
        r"^- Source (?:version|VERSION): `([0-9]+\.[0-9]+\.[0-9]+)`$",
        document,
        re.MULTILINE,
    )
    assert len(source_fields) == 1
    assert _field(document, "Profile/client").replace(" / ", "-") == path.stem
