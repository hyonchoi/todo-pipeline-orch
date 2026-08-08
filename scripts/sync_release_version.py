#!/usr/bin/env python3
"""Synchronize the Changesets package version into Python release metadata."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tomllib
from pathlib import Path

PACKAGE_NAME = "hermes-pipeline"
SEMVER_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


class VersionConsistencyError(ValueError):
    """Raised when release artifacts do not contain one consistent version."""


def package_version(root: Path) -> str:
    package = json.loads((root / "package.json").read_text())
    version = package.get("version")
    if not isinstance(version, str) or SEMVER_RE.fullmatch(version) is None:
        raise VersionConsistencyError(
            f"package.json has invalid version: {version!r}"
        )
    return version


def _replace_pyproject_version(text: str, version: str) -> str:
    section_match = re.search(
        r"(?ms)^\[project\]\s*\n(?P<body>.*?)(?=^\[|\Z)", text
    )
    if section_match is None:
        raise VersionConsistencyError("pyproject.toml has no [project] section")

    body = section_match.group("body")
    updated_body, replacements = re.subn(
        r'(?m)^(version\s*=\s*)"[^"]*"\s*$',
        rf'\g<1>"{version}"',
        body,
        count=1,
    )
    if replacements != 1:
        raise VersionConsistencyError(
            "pyproject.toml [project] section must contain one string version"
        )
    return text[: section_match.start("body")] + updated_body + text[section_match.end("body") :]


def synchronize(root: Path) -> str:
    version = package_version(root)
    (root / "VERSION").write_text(f"{version}\n")

    pyproject_path = root / "pyproject.toml"
    pyproject_path.write_text(
        _replace_pyproject_version(pyproject_path.read_text(), version)
    )
    subprocess.run(["uv", "lock"], cwd=root, check=True)
    return version


def check_consistency(root: Path) -> str:
    expected = package_version(root)
    errors: list[str] = []

    version_file = (root / "VERSION").read_text().strip()
    if version_file != expected:
        errors.append(f"VERSION is {version_file!r}, expected {expected!r}")

    package_lock = json.loads((root / "package-lock.json").read_text())
    locked_root_versions = [
        package_lock.get("version"),
        package_lock.get("packages", {}).get("", {}).get("version"),
    ]
    if locked_root_versions != [expected, expected]:
        errors.append(
            f"package-lock.json root versions are {locked_root_versions!r}, "
            f"expected [{expected!r}, {expected!r}]"
        )

    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    pyproject_version = pyproject.get("project", {}).get("version")
    if pyproject_version != expected:
        errors.append(
            f"pyproject.toml is {pyproject_version!r}, expected {expected!r}"
        )

    lock = tomllib.loads((root / "uv.lock").read_text())
    locked_versions = [
        item.get("version")
        for item in lock.get("package", [])
        if item.get("name") == PACKAGE_NAME
    ]
    if locked_versions != [expected]:
        errors.append(
            f"uv.lock {PACKAGE_NAME} versions are {locked_versions!r}, "
            f"expected [{expected!r}]"
        )

    changelog = (root / "CHANGELOG.md").read_text()
    heading = re.compile(
        rf"^## (?:{re.escape(expected)}|\[{re.escape(expected)}\](?: - [0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})?)[ \t]*$",
        re.MULTILINE,
    )
    if heading.search(changelog) is None:
        errors.append(f"CHANGELOG.md has no release heading for {expected}")

    if errors:
        raise VersionConsistencyError("; ".join(errors))
    return expected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify release artifacts without modifying them",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]

    if not args.check:
        synchronize(root)
    version = check_consistency(root)
    print(f"release metadata is consistent at {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
