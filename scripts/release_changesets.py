#!/usr/bin/env python3
"""Python-native changeset and release metadata management."""

from __future__ import annotations

import argparse
import re
import secrets
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

PACKAGE_NAME = "hermes-pipeline"
SEMVER_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
ENTRY_RE = re.compile(r'^"hermes-pipeline": (patch|minor|major)$')
BUMP_ORDER = {"patch": 0, "minor": 1, "major": 2}
CONDITIONAL_PAIR_EVIDENCE = {
    ("gstack", "claude"): "gstack-claude.md",
    ("gstack", "codex"): "gstack-codex.md",
    # native-sdd has the same sole external contract: Hermes dispatches the
    # selected client through ai-coding-agents. It does not use the gstack or
    # superpowers portions of these canonical artifacts.
    ("native-sdd", "claude"): "gstack-claude.md",
    ("native-sdd", "codex"): "gstack-codex.md",
}
EVIDENCE_FILES = tuple(dict.fromkeys(CONDITIONAL_PAIR_EVIDENCE.values()))


class ReleaseError(ValueError):
    """Raised when release inputs or artifacts are invalid."""


@dataclass(frozen=True)
class Changeset:
    path: Path
    bump: str | None
    summary: str


def project_version(root: Path) -> str:
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    version = pyproject.get("project", {}).get("version")
    if not isinstance(version, str) or SEMVER_RE.fullmatch(version) is None:
        raise ReleaseError(f"pyproject.toml has invalid project version: {version!r}")
    return version


def _replace_pyproject_version(text: str, version: str) -> str:
    section_match = re.search(
        r"(?ms)^\[project\]\s*\n(?P<body>.*?)(?=^\[|\Z)", text
    )
    if section_match is None:
        raise ReleaseError("pyproject.toml has no [project] section")
    body = section_match.group("body")
    updated_body, replacements = re.subn(
        r'(?m)^(version\s*=\s*)"[^"]*"\s*$',
        rf'\g<1>"{version}"',
        body,
        count=1,
    )
    if replacements != 1:
        raise ReleaseError(
            "pyproject.toml [project] section must contain one string version"
        )
    return (
        text[: section_match.start("body")]
        + updated_body
        + text[section_match.end("body") :]
    )


def parse_changeset(path: Path) -> Changeset:
    lines = path.read_text().splitlines()
    if not lines or lines[0] != "---":
        raise ReleaseError(f"{path} must start with a --- delimiter")
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise ReleaseError(f"{path} has no closing --- delimiter") from exc

    frontmatter = [line for line in lines[1:closing] if line.strip()]
    summary = "\n".join(lines[closing + 1 :]).strip()
    if not frontmatter:
        if summary:
            raise ReleaseError(f"{path} empty changeset must not contain a summary")
        return Changeset(path=path, bump=None, summary="")
    if len(frontmatter) != 1:
        raise ReleaseError(f"{path} must contain exactly one package entry")
    match = ENTRY_RE.fullmatch(frontmatter[0])
    if match is None:
        raise ReleaseError(
            f'{path} must declare "{PACKAGE_NAME}": patch|minor|major'
        )
    if not summary:
        raise ReleaseError(f"{path} release changeset must contain a summary")
    return Changeset(path=path, bump=match.group(1), summary=summary)


def changesets(root: Path) -> list[Changeset]:
    changeset_dir = root / ".changeset"
    if not changeset_dir.exists():
        return []
    paths = sorted(
        path
        for path in changeset_dir.glob("*.md")
        if not path.name.startswith(".")
    )
    return [parse_changeset(path) for path in paths]


def add_changeset(
    root: Path,
    *,
    bump: str | None,
    summary: str | None,
    empty: bool,
    now: datetime | None = None,
) -> Path:
    if empty:
        if bump is not None or summary is not None:
            raise ReleaseError("--empty cannot be combined with a bump or summary")
        text = "---\n---\n"
    else:
        if bump not in BUMP_ORDER:
            raise ReleaseError("a patch, minor, or major bump is required")
        if summary is None or not summary.strip():
            raise ReleaseError("a nonblank summary is required")
        text = f'---\n"{PACKAGE_NAME}": {bump}\n---\n\n{summary.strip()}\n'

    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d%H%M%S")
    changeset_dir = root / ".changeset"
    changeset_dir.mkdir(parents=True, exist_ok=True)
    for _attempt in range(10):
        path = changeset_dir / f"{stamp}-{secrets.token_hex(4)}.md"
        try:
            with path.open("x", encoding="utf-8", errors="strict") as stream:
                stream.write(text)
        except FileExistsError:
            continue
        return path
    raise ReleaseError("could not allocate a unique changeset filename")


def _git(root: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def check_pr_status(root: Path, since: str) -> None:
    for item in changesets(root):
        parse_changeset(item.path)
    base = _git(root, ["merge-base", since, "HEAD"])
    diff = _git(
        root,
        ["diff", "--name-status", f"{base}...HEAD", "--", ".changeset"],
    )
    added = False
    errors: list[str] = []
    for line in diff.splitlines():
        status, _, name = line.partition("\t")
        if not name.endswith(".md"):
            continue
        if status == "A":
            added = True
            parse_changeset(root / name)
        elif status.startswith(("M", "D", "R")):
            errors.append(f"pull requests may not {status} an inherited changeset: {name}")
    if errors:
        raise ReleaseError("; ".join(errors))
    if not added:
        raise ReleaseError(
            "pull request must add a .changeset/*.md file; use add --empty "
            "when no release is intended"
        )


def bump_version(version: str, bump: str) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _release_section(
    version: str,
    items: list[Changeset],
    release_date: date | None,
) -> str:
    heading = f"## [{version}] - {release_date}" if release_date else f"## {version}"
    blocks = [heading]
    for bump in ("major", "minor", "patch"):
        matching = [item.summary for item in items if item.bump == bump]
        if not matching:
            continue
        blocks.append(f"### {bump.title()} Changes")
        blocks.append("\n\n".join(f"- {summary}" for summary in matching))
    return "\n\n".join(blocks) + "\n\n"


def _prepend_release(changelog: str, section: str) -> str:
    first_release = re.search(r"(?m)^## ", changelog)
    if first_release is None:
        return changelog.rstrip() + "\n\n" + section
    return changelog[: first_release.start()] + section + changelog[first_release.start() :]


def _replace_once(text: str, old: str, new: str, *, source: Path) -> str:
    if text.count(old) != 1:
        raise ReleaseError(
            f"{source} must contain exactly one {old.strip()!r} marker"
        )
    return text.replace(old, new, 1)


def finalize_release_evidence(root: Path, version: str) -> None:
    evidence_root = root / "docs/release-evidence/agent-clients"
    candidate_root = evidence_root / "candidate-source-snapshot"
    release_root = evidence_root / version
    rendered: dict[str, str] = {}
    for filename in EVIDENCE_FILES:
        source = candidate_root / filename
        if not source.is_file():
            raise ReleaseError(f"missing candidate evidence: {source}")
        text = source.read_text()
        text = _replace_once(
            text,
            " candidate qualification\n",
            " release qualification\n",
            source=source,
        )
        text = _replace_once(
            text,
            "- Evidence status: `candidate/source-snapshot`\n",
            "- Evidence status: `release-final`\n",
            source=source,
        )
        text = _replace_once(
            text,
            "- Release: `not selected`\n",
            f"- Release: `{version}`\n",
            source=source,
        )
        text, replacements = re.subn(
            r"(?m)^- Source version: `[0-9]+\.[0-9]+\.[0-9]+`$",
            f"- Source version: `{version}`",
            text,
            count=1,
        )
        if replacements != 1:
            raise ReleaseError(f"{source} must contain one semantic Source version field")
        text = _replace_once(
            text,
            "This qualifies discovery against the recorded source snapshot. It is not\n"
            "release-final evidence and does not select a release version.\n",
            "This release-final artifact records the passing qualification at the source\n"
            f"commit above for release `{version}`.\n",
            source=source,
        )
        rendered[filename] = text

    release_root.mkdir(parents=True, exist_ok=True)
    for filename, text in rendered.items():
        (release_root / filename).write_text(text)


def apply_release(root: Path, *, release_date: date | None = None) -> str | None:
    items = changesets(root)
    if not items:
        return None
    releasing = [item for item in items if item.bump is not None]
    if not releasing:
        for item in items:
            item.path.unlink()
        return project_version(root)

    selected = max((item.bump for item in releasing), key=BUMP_ORDER.__getitem__)
    current = project_version(root)
    new_version = bump_version(current, selected)
    pyproject_path = root / "pyproject.toml"
    pyproject_path.write_text(
        _replace_pyproject_version(pyproject_path.read_text(), new_version)
    )
    changelog_path = root / "CHANGELOG.md"
    changelog_path.write_text(
        _prepend_release(
            changelog_path.read_text(),
            _release_section(new_version, releasing, release_date),
        )
    )
    subprocess.run(["uv", "lock"], cwd=root, check=True)
    finalize_release_evidence(root, new_version)
    for item in items:
        item.path.unlink()
    check_consistency(root)
    return new_version


def check_consistency(root: Path) -> str:
    expected = project_version(root)
    errors: list[str] = []
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
        raise ReleaseError("; ".join(errors))
    return expected


def _interactive_add(args: argparse.Namespace) -> tuple[str | None, str | None]:
    if args.empty or args.bump is not None or args.summary or args.summary_file:
        return args.bump, args.summary
    if not sys.stdin.isatty():
        raise ReleaseError("non-interactive add requires --empty or --bump and a summary")
    bump = input("Bump (patch/minor/major): ").strip().lower()
    summary = input("Summary: ").strip()
    return bump, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_parser = subparsers.add_parser("add")
    add_parser.add_argument("--bump", choices=tuple(BUMP_ORDER))
    summary = add_parser.add_mutually_exclusive_group()
    summary.add_argument("--summary")
    summary.add_argument("--summary-file", type=Path)
    add_parser.add_argument("--empty", action="store_true")
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--since", required=True)
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--date", type=date.fromisoformat)
    subparsers.add_parser("check")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        if args.command == "add":
            bump, summary_text = _interactive_add(args)
            if args.summary_file is not None:
                summary_text = args.summary_file.read_text()
            path = add_changeset(
                root,
                bump=bump,
                summary=summary_text,
                empty=args.empty,
            )
            print(path.relative_to(root))
        elif args.command == "status":
            check_pr_status(root, args.since)
            print("pull request release intent is valid")
        elif args.command == "apply":
            version = apply_release(root, release_date=args.date)
            if version is None:
                print("no pending changesets")
            else:
                print(f"release metadata applied at {version}")
        else:
            version = check_consistency(root)
            print(f"release metadata is consistent at {version}")
    except (OSError, ReleaseError, subprocess.CalledProcessError) as exc:
        print(f"release error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
