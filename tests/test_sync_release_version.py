from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.sync_release_version import (
    VersionConsistencyError,
    check_consistency,
    package_version,
    synchronize,
)


def _write_release_files(root: Path, *, version: str = "1.2.3") -> None:
    (root / "package.json").write_text(
        json.dumps({"name": "hermes-pipeline", "version": version})
    )
    (root / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "hermes-pipeline",
                "version": version,
                "packages": {
                    "": {"name": "hermes-pipeline", "version": version}
                },
            }
        )
    )
    (root / "VERSION").write_text(f"{version}\n")
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "hermes-pipeline"\nversion = "{version}"\n'
    )
    (root / "uv.lock").write_text(
        f'[[package]]\nname = "hermes-pipeline"\nversion = "{version}"\n'
    )
    (root / "CHANGELOG.md").write_text(f"# Changelog\n\n## {version}\n")


def test_repository_release_metadata_is_consistent():
    root = Path(__file__).resolve().parents[1]
    assert check_consistency(root) == "0.7.2"


def test_synchronize_copies_package_version_and_regenerates_lock(mocker, tmp_path):
    _write_release_files(tmp_path, version="1.2.3")
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "hermes-pipeline", "version": "1.3.0"})
    )
    run = mocker.patch("scripts.sync_release_version.subprocess.run")

    assert synchronize(tmp_path) == "1.3.0"

    assert (tmp_path / "VERSION").read_text() == "1.3.0\n"
    assert 'version = "1.3.0"' in (tmp_path / "pyproject.toml").read_text()
    run.assert_called_once_with(["uv", "lock"], cwd=tmp_path, check=True)


def test_synchronize_propagates_lockfile_regeneration_failure(mocker, tmp_path):
    _write_release_files(tmp_path)
    mocker.patch(
        "scripts.sync_release_version.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, ["uv", "lock"]),
    )

    with pytest.raises(subprocess.CalledProcessError):
        synchronize(tmp_path)


@pytest.mark.parametrize(
    "artifact", ["VERSION", "package-lock.json", "pyproject.toml", "uv.lock"]
)
def test_check_consistency_rejects_version_drift(tmp_path, artifact):
    _write_release_files(tmp_path)
    path = tmp_path / artifact
    path.write_text(path.read_text().replace("1.2.3", "1.2.4"))

    with pytest.raises(VersionConsistencyError, match=artifact):
        check_consistency(tmp_path)


def test_check_consistency_accepts_historical_keep_a_changelog_heading(tmp_path):
    _write_release_files(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [1.2.3] - 2026-08-07\n"
    )

    assert check_consistency(tmp_path) == "1.2.3"


def test_check_consistency_requires_current_changelog_release(tmp_path):
    _write_release_files(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")

    with pytest.raises(VersionConsistencyError, match="CHANGELOG.md"):
        check_consistency(tmp_path)


def test_package_version_rejects_non_semver(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "hermes-pipeline", "version": "1.2.3-beta.1"})
    )

    with pytest.raises(VersionConsistencyError, match="invalid version"):
        package_version(tmp_path)
