from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.release_changesets import (
    ReleaseError,
    add_changeset,
    apply_release,
    bump_version,
    check_consistency,
    check_pr_status,
    finalize_release_evidence,
    parse_changeset,
    project_version,
)


def _write_release_files(root: Path, *, version: str = "1.2.3") -> None:
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "hermes-pipeline"\nversion = "{version}"\n'
    )
    (root / "uv.lock").write_text(
        f'[[package]]\nname = "hermes-pipeline"\nversion = "{version}"\n'
    )
    (root / "CHANGELOG.md").write_text(f"# Changelog\n\n## {version}\n")


def _write_changeset(root: Path, name: str, bump: str | None, summary: str = "") -> Path:
    directory = root / ".changeset"
    directory.mkdir(exist_ok=True)
    path = directory / name
    if bump is None:
        path.write_text("---\n---\n")
    else:
        path.write_text(
            f'---\n"hermes-pipeline": {bump}\n---\n\n{summary}\n'
        )
    return path


def _write_candidate_evidence(root: Path, filename: str) -> Path:
    candidate = (
        root
        / "docs/release-evidence/agent-clients/candidate-source-snapshot"
        / filename
    )
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(
        "# gstack candidate qualification\n\n"
        "- Evidence status: `candidate/source-snapshot`\n"
        "- Release: `not selected`\n"
        "- Source version: `1.2.3`\n"
        "- Source commit: `abc123`\n"
        "- Result: `PASS`\n\n"
        "This qualifies discovery against the recorded source snapshot. It is not\n"
        "release-final evidence and does not select a release version.\n\n"
        "## Captured evidence\n\nunchanged transcript\n"
    )
    return candidate


def test_repository_release_metadata_is_consistent():
    root = Path(__file__).resolve().parents[1]
    assert check_consistency(root) == project_version(root)
    for obsolete in ("VERSION", "package.json", "package-lock.json"):
        assert not (root / obsolete).exists()


@pytest.mark.parametrize(
    ("current", "bump", "expected"),
    [
        ("1.2.3", "patch", "1.2.4"),
        ("1.2.3", "minor", "1.3.0"),
        ("1.2.3", "major", "2.0.0"),
    ],
)
def test_bump_version(current, bump, expected):
    assert bump_version(current, bump) == expected


def test_parse_changeset_accepts_release_and_empty(tmp_path):
    release = _write_changeset(tmp_path, "release.md", "minor", "Add a feature.")
    empty = _write_changeset(tmp_path, "empty.md", None)
    assert parse_changeset(release).bump == "minor"
    assert parse_changeset(release).summary == "Add a feature."
    assert parse_changeset(empty).bump is None


@pytest.mark.parametrize(
    "text",
    [
        "not frontmatter\n",
        '---\n"other": patch\n---\n\nSummary\n',
        '---\n"hermes-pipeline": tiny\n---\n\nSummary\n',
        '---\n"hermes-pipeline": patch\n---\n',
        "---\n---\n\nUnexpected summary\n",
    ],
)
def test_parse_changeset_rejects_invalid_forms(tmp_path, text):
    path = tmp_path / "bad.md"
    path.write_text(text)
    with pytest.raises(ReleaseError):
        parse_changeset(path)


def test_add_changeset_writes_compatible_unique_file(tmp_path, mocker):
    mocker.patch("scripts.release_changesets.secrets.token_hex", return_value="1234abcd")
    path = add_changeset(
        tmp_path,
        bump="patch",
        summary="Fix the release flow.",
        empty=False,
        now=datetime(2026, 8, 10, 12, 30, 0, tzinfo=UTC),
    )
    assert path.name == "20260810123000-1234abcd.md"
    assert parse_changeset(path).summary == "Fix the release flow."


def test_status_accepts_new_fragment(mocker, tmp_path):
    _write_changeset(tmp_path, "new.md", "patch", "Fix one.")
    mocker.patch(
        "scripts.release_changesets._git",
        side_effect=["base-sha", "A\t.changeset/new.md"],
    )
    check_pr_status(tmp_path, "origin/main")


def test_status_requires_new_fragment(mocker, tmp_path):
    mocker.patch(
        "scripts.release_changesets._git",
        side_effect=["base-sha", ""],
    )
    with pytest.raises(ReleaseError, match="must add"):
        check_pr_status(tmp_path, "origin/main")


def test_status_rejects_modifying_inherited_fragment(mocker, tmp_path):
    _write_changeset(tmp_path, "old.md", "patch", "Changed summary.")
    mocker.patch(
        "scripts.release_changesets._git",
        side_effect=["base-sha", "M\t.changeset/old.md"],
    )
    with pytest.raises(ReleaseError, match="inherited"):
        check_pr_status(tmp_path, "origin/main")


def test_apply_release_uses_highest_bump_and_consumes_all(mocker, tmp_path):
    _write_release_files(tmp_path)
    _write_changeset(tmp_path, "a.md", "patch", "Fix one.")
    _write_changeset(tmp_path, "b.md", "minor", "Add two.")
    _write_changeset(tmp_path, "c.md", None)
    for filename in ("gstack-claude.md", "gstack-codex.md"):
        _write_candidate_evidence(tmp_path, filename)
    def regenerate_lock(*_args, **_kwargs):
        lock = tmp_path / "uv.lock"
        lock.write_text(lock.read_text().replace("1.2.3", "1.3.0"))

    run = mocker.patch(
        "scripts.release_changesets.subprocess.run", side_effect=regenerate_lock
    )

    assert apply_release(tmp_path) == "1.3.0"

    assert project_version(tmp_path) == "1.3.0"
    changelog = (tmp_path / "CHANGELOG.md").read_text()
    assert "## 1.3.0" in changelog
    assert "### Minor Changes" in changelog
    assert "### Patch Changes" in changelog
    assert not list((tmp_path / ".changeset").glob("*.md"))
    run.assert_called_once_with(["uv", "lock"], cwd=tmp_path, check=True)


def test_apply_release_can_render_dated_keep_a_changelog_heading(mocker, tmp_path):
    from datetime import date

    _write_release_files(tmp_path)
    _write_changeset(tmp_path, "release.md", "patch", "Fix one.")
    for filename in ("gstack-claude.md", "gstack-codex.md"):
        _write_candidate_evidence(tmp_path, filename)

    def regenerate_lock(*_args, **_kwargs):
        lock = tmp_path / "uv.lock"
        lock.write_text(lock.read_text().replace("1.2.3", "1.2.4"))

    mocker.patch(
        "scripts.release_changesets.subprocess.run", side_effect=regenerate_lock
    )
    apply_release(tmp_path, release_date=date(2026, 8, 10))
    assert "## [1.2.4] - 2026-08-10" in (tmp_path / "CHANGELOG.md").read_text()


def test_apply_empty_only_consumes_without_bump(mocker, tmp_path):
    _write_release_files(tmp_path)
    _write_changeset(tmp_path, "empty.md", None)
    run = mocker.patch("scripts.release_changesets.subprocess.run")

    assert apply_release(tmp_path) == "1.2.3"
    assert project_version(tmp_path) == "1.2.3"
    assert not list((tmp_path / ".changeset").glob("*.md"))
    run.assert_not_called()


def test_apply_propagates_lockfile_regeneration_failure(mocker, tmp_path):
    _write_release_files(tmp_path)
    _write_changeset(tmp_path, "release.md", "patch", "Fix one.")
    mocker.patch(
        "scripts.release_changesets.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, ["uv", "lock"]),
    )
    with pytest.raises(subprocess.CalledProcessError):
        apply_release(tmp_path)


def test_check_consistency_rejects_lock_drift(tmp_path):
    _write_release_files(tmp_path)
    (tmp_path / "uv.lock").write_text(
        (tmp_path / "uv.lock").read_text().replace("1.2.3", "1.2.4")
    )
    with pytest.raises(ReleaseError, match="uv.lock"):
        check_consistency(tmp_path)


def test_check_consistency_accepts_keep_a_changelog_heading(tmp_path):
    _write_release_files(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [1.2.3] - 2026-08-07\n"
    )
    assert check_consistency(tmp_path) == "1.2.3"


def test_project_version_rejects_prerelease(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "hermes-pipeline"\nversion = "1.2.3-beta.1"\n'
    )
    with pytest.raises(ReleaseError, match="invalid"):
        project_version(tmp_path)


def test_finalize_release_evidence_preserves_snapshot_and_sets_release(tmp_path):
    for filename in ("gstack-claude.md", "gstack-codex.md"):
        _write_candidate_evidence(tmp_path, filename)
    finalize_release_evidence(tmp_path, "1.3.0")
    for filename in ("gstack-claude.md", "gstack-codex.md"):
        release = (
            tmp_path / "docs/release-evidence/agent-clients/1.3.0" / filename
        ).read_text()
        assert "# gstack release qualification" in release
        assert "- Evidence status: `release-final`" in release
        assert "- Release: `1.3.0`" in release
        assert "- Source version: `1.3.0`" in release
        assert "- Source commit: `abc123`" in release
        assert "unchanged transcript" in release


def test_finalize_release_evidence_fails_before_partial_release(tmp_path):
    _write_candidate_evidence(tmp_path, "gstack-claude.md")
    with pytest.raises(ReleaseError, match="gstack-codex.md"):
        finalize_release_evidence(tmp_path, "1.3.0")
    assert not (tmp_path / "docs/release-evidence/agent-clients/1.3.0").exists()
