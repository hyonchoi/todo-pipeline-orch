"""Mock integration test harness — fixture factory, preflight, runner."""

from __future__ import annotations

import json as _json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from .config import PromptClient, _validate_project_slug
from .profile_prerequisites import (
    HERMES_SKILL_REGISTRY_ROOT,
    unverified_prerequisite_ids,
    verify_hermes_skill_registry_prerequisite,
)

log = logging.getLogger(__name__)


_MOCK_PROJECT_GITIGNORE = """\
# Harness runtime artifacts
.hermes/outcomes/
.hermes/tpo-config.yaml
.hermes/fake-gh-state.json*

# Agent scratch space
.superpowers/
.code-review-graph/

# Python runtime artifacts
__pycache__/
*.py[cod]
"""
_HARNESS_ASSIGNEE = "pipeline"
_HARNESS_PLAN_PATH = "docs/harness/TODO-1-plan.md"
# Placeholder GitHub identity for the disposable fixture. The remote is never
# contacted: every ``gh`` call is served offline by the bundled fake_gh stub.
_HARNESS_REPO = "tpo-harness/mock-project"
_HARNESS_ORIGIN_URL = f"https://github.com/{_HARNESS_REPO}.git"
_HARNESS_NO_PUSH_URL = f"no-push://{_HARNESS_REPO}.git"
_FAKE_GH_STATE_ENV = "TPO_FAKE_GH_STATE"
_FAKE_GH_BIN_RELPATH = Path("bin") / "gh"
_FAKE_GH_STATE_RELPATH = Path(".hermes") / "fake-gh-state.json"
_HARNESS_PLAN = """\
# TODO-1 Mock Name Normalization Plan

1. Add focused tests for `normalize_names` covering whitespace trimming, empty
   values, lowercasing, input order, and an empty input list. Run the focused
   tests and confirm they fail because the implementation does not exist.
2. Create `mock_transform.py` and implement
   `normalize_names(names: list[str]) -> list[str]` using only the Python
   standard library.
3. Run `uv run pytest`, inspect the diff, and commit the tested fixture change.

Acceptance requires `normalize_names([" Alice ", "", "BOB"])` to return
`["alice", "bob"]`, `normalize_names([])` to return `[]`, and the generated
fixture worktree to be clean after its implementation phases complete.

```json tpo-plan
{
  "schema_version": 1,
  "todo_id": "TODO-1",
  "tasks": [
    {
      "id": "task-1",
      "title": "Implement normalize_names in mock_transform.py",
      "instructions": "Add focused tests for `normalize_names` (whitespace trimming, empty values, lowercasing, input order, empty input) and confirm they fail. Then create `mock_transform.py` implementing `normalize_names(names: list[str]) -> list[str]` with the standard library only.",
      "acceptance_criteria": [
        "normalize_names([' Alice ', '', 'BOB']) returns ['alice', 'bob']",
        "normalize_names([]) returns []",
        "The fixture worktree is clean after the task commit"
      ],
      "verification": ["uv run pytest"],
      "commit_message": "feat: add normalize_names mock transform"
    }
  ]
}
```
"""


class HarnessProfileError(ValueError):
    """A selected profile cannot be executed safely by the harness."""

    def __init__(self, code: str, profile_name: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.profile_name = profile_name
        self.detail = detail


class HarnessPreflightError(RuntimeError):
    """The live harness cannot start because a preflight requirement failed."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


_HARNESS_REPO_ENV = "TPO_HARNESS_REPO"
_SANDBOX_REPO_RE = re.compile(r"^[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}/[A-Za-z0-9._-]{1,100}\Z")


@dataclass(frozen=True)
class SandboxRepo:
    """A live GitHub sandbox repository selected for a harness run."""

    repo: str
    slug: str
    url: str

    def __post_init__(self) -> None:
        # Defence in depth: every instance is safe to interpolate into gh argv.
        if _SANDBOX_REPO_RE.match(self.repo) is None:
            raise HarnessPreflightError("invalid_repo", self.repo)


def resolve_sandbox_repo(
    cli_value: str | None, env: Mapping[str, str] | None = None
) -> SandboxRepo:
    """Resolve the sandbox ``owner/name`` from ``--repo`` or ``TPO_HARNESS_REPO``.

    ``cli_value`` wins over the environment. Raises :class:`HarnessPreflightError`:

    - ``repo_missing`` when neither source provides a non-blank value;
    - ``invalid_repo`` when the value is not ``owner/name`` shaped (bad owner,
      extra path segments, whitespace, a ``.git`` suffix, or a trailing dot);
    - ``invalid_slug`` when the name part is not a valid tpo project slug
      (see :func:`hermes_pipeline.config._validate_project_slug`).
    """
    environ = os.environ if env is None else env
    value = None
    for candidate in (cli_value, environ.get(_HARNESS_REPO_ENV)):
        if candidate is not None and candidate.strip():
            value = candidate.strip()
            break
    if value is None:
        raise HarnessPreflightError(
            "repo_missing",
            f"pass --repo owner/name or set {_HARNESS_REPO_ENV}",
        )
    if _SANDBOX_REPO_RE.match(value) is None:
        raise HarnessPreflightError("invalid_repo", value)
    name = value.split("/", 1)[1]
    if name.endswith(".git") or name.endswith("."):
        raise HarnessPreflightError("invalid_repo", value)
    if not _validate_project_slug(name):
        raise HarnessPreflightError("invalid_slug", name)
    return SandboxRepo(repo=value, slug=name, url=f"https://github.com/{value}.git")


_PUSH_PERMISSIONS = frozenset({"WRITE", "MAINTAIN", "ADMIN"})
_REPO_VIEW_JQ = '{permission: .viewerPermission, default_branch: (.defaultBranchRef.name // "")}'


@dataclass(frozen=True)
class GitHubPreflight:
    """Facts established about the sandbox before a live harness run starts."""

    viewer: str
    default_branch: str
    permission: str


def other_ready_issues(
    project_dir: Path, sandbox: SandboxRepo, *, exclude_issue: int | None = None
) -> tuple[int, ...]:
    """Numbers of open ``tpo:todo`` issues in ``sandbox`` carrying ``ready-for-agent``.

    ``exclude_issue`` (the harness's own issue) is skipped; the result is sorted.
    """
    from .github_issues import READY_LABEL, list_todo_issues

    # list_todo_issues already returns issues sorted by number.
    return tuple(
        issue.number
        for issue in list_todo_issues(project_dir, repo=sandbox.repo)
        if READY_LABEL in issue.labels and issue.number != exclude_issue
    )


def github_preflight(
    project_dir: Path, sandbox: SandboxRepo, *, exclude_issue: int | None = None
) -> GitHubPreflight:
    """Verify gh auth, the viewer login, push permission, and sandbox quiescence.

    ``GitHubIssuesError`` from ``gh`` propagates unchanged (the CLI already maps
    ``gh_auth`` and friends). Raises :class:`HarnessPreflightError`:

    - ``gh_viewer_unknown`` when ``gh api user`` reports no usable login;
    - ``gh_invalid`` when ``gh repo view`` returns malformed JSON;
    - ``gh_permission`` (detail: the permission, or ``unknown`` when gh reports
      ``null``) without WRITE/MAINTAIN/ADMIN;
    - ``sandbox_not_quiescent`` (detail: ``#12, #15``) when another open
      ``tpo:todo`` issue is still ``ready-for-agent``.
    """
    from .github_issues import GitHubIssuesError, _gh, check_auth, current_login

    check_auth(project_dir)
    try:
        viewer = current_login(project_dir)
    except GitHubIssuesError as exc:
        if exc.code == "gh_invalid":
            raise HarnessPreflightError("gh_viewer_unknown") from exc
        raise
    stdout = _gh(
        project_dir,
        [
            "repo", "view", sandbox.repo,
            "--json", "viewerPermission,defaultBranchRef",
            "--jq", _REPO_VIEW_JQ,
        ],
    )

    def _malformed() -> HarnessPreflightError:
        return HarnessPreflightError("gh_invalid", "gh repo view returned malformed JSON")

    try:
        view = _json.loads(stdout)
    except _json.JSONDecodeError as exc:
        raise _malformed() from exc
    if not isinstance(view, Mapping) or not isinstance(view.get("default_branch"), str):
        raise _malformed()
    default_branch = view["default_branch"]
    permission = view.get("permission")
    if not isinstance(permission, str):
        # gh reports ``viewerPermission: null`` for accounts without push rights.
        raise HarnessPreflightError("gh_permission", "unknown")
    if permission not in _PUSH_PERMISSIONS:
        raise HarnessPreflightError("gh_permission", permission)
    ready = other_ready_issues(project_dir, sandbox, exclude_issue=exclude_issue)
    if ready:
        raise HarnessPreflightError(
            "sandbox_not_quiescent", ", ".join(f"#{number}" for number in ready)
        )
    return GitHubPreflight(viewer=viewer, default_branch=default_branch, permission=permission)


# Seam for tests: ``hermes_pipeline.harness._git`` is monkeypatched to a recorder.
_git = subprocess.run
_SANDBOX_SEED_PATHS = ("pyproject.toml", "tests/__init__.py", "docs/harness/SANDBOX.md")
_HARNESS_GIT_USER_EMAIL = "test@localhost"
_HARNESS_GIT_USER_NAME = "TPO Harness"


_URL_USERINFO_RE = re.compile(r"://[^/@\s]+@")
_GIT_NO_PROMPT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "GCM_INTERACTIVE": "never"}


def _git_verb(args: list[str]) -> str:
    """The git subcommand in *args*, skipping leading ``-c KEY=VAL`` pairs."""
    i = 0
    while i + 1 < len(args) and args[i] == "-c":
        i += 2
    return args[i] if i < len(args) else "git"


def _run_git(args: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    verb = _git_verb(args)
    try:
        result = _git(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, **_GIT_NO_PROMPT_ENV},
        )
    except OSError as exc:
        raise HarnessPreflightError("git_error", f"git {verb} failed: {exc}") from exc
    if check and result.returncode != 0:
        # ``git commit`` reports "nothing to commit" on stdout with an empty stderr.
        output = (result.stderr or "").strip() or (result.stdout or "").strip()[-200:]
        detail = _URL_USERINFO_RE.sub("://***@", output)[:200]
        raise HarnessPreflightError("git_error", f"git {verb} failed: {detail}")
    return result


def clone_sandbox(sandbox: SandboxRepo, project_dir: Path, *, branch: str | None = None) -> None:
    """Clone *sandbox* into *project_dir* and set a local harness git identity.

    With *branch*, ``--branch <branch>`` selects the checked-out branch instead of
    the remote's HEAD.
    """
    if project_dir.exists():
        raise HarnessPreflightError("workspace_exists", str(project_dir))
    project_dir.parent.mkdir(parents=True, exist_ok=True)
    branch_args = ["--branch", branch] if branch is not None else []
    _run_git(["clone", *branch_args, "--", sandbox.url, str(project_dir)], cwd=project_dir.parent)
    _run_git(["config", "user.email", _HARNESS_GIT_USER_EMAIL], cwd=project_dir)
    _run_git(["config", "user.name", _HARNESS_GIT_USER_NAME], cwd=project_dir)


def _tracked_blobs_missing(project_dir: Path, paths: tuple[str, ...] | list[str]) -> list[str]:
    """Return the *paths* that are not tracked files (blobs) at HEAD of *project_dir*."""
    missing = []
    for rel in paths:
        result = _run_git(["cat-file", "-t", f"HEAD:{rel}"], cwd=project_dir, check=False)
        if result.returncode != 0 or result.stdout.strip() != "blob":
            missing.append(rel)
    return missing


def sandbox_seed_check(project_dir: Path, sandbox: SandboxRepo) -> None:
    """Require every sandbox seed path to be a tracked file (blob) at HEAD of the clone."""
    missing = _tracked_blobs_missing(project_dir, _SANDBOX_SEED_PATHS)
    if missing:
        raise HarnessPreflightError(
            "sandbox_not_seeded",
            f"missing: {', '.join(missing)}; run tpo test --repo {sandbox.repo} --init-sandbox",
        )


_SANDBOX_GITIGNORE = """\
# Harness runtime state: pipeline.toml, pipeline_branch.txt, outcomes, fake-gh
# state. Written into the clone by every run; none of it may reach a PR.
.hermes/

# Agent scratch space
.superpowers/
.code-review-graph/

# Python runtime artifacts
__pycache__/
*.py[cod]
.venv/
"""
_SANDBOX_SEED_FILES: dict[str, str] = {
    "README.md": (
        "# TPO harness sandbox\n\n"
        "Disposable repository driven by `tpo test`; see "
        "docs/howto-live-integration-test-harness.md in todo-pipeline-orchestrator.\n"
    ),
    ".gitignore": _SANDBOX_GITIGNORE,
    "pyproject.toml": (
        "[project]\n"
        'name = "tpo-harness-sandbox"\n'
        'version = "0.0.0"\n'
        'requires-python = ">=3.12"\n'
        "\n"
        "[dependency-groups]\n"
        'dev = ["pytest>=8"]\n'
        "\n"
        "[tool.pytest.ini_options]\n"
        'testpaths = ["tests"]\n'
    ),
    "tests/__init__.py": "",
    "docs/harness/SANDBOX.md": (
        "# TPO harness sandbox marker\n\n"
        "seed_version: 1\n\n"
        "Managed by `tpo test --init-sandbox`. Do not edit by hand.\n"
    ),
}
# Tracked paths tolerated in a sandbox besides the seed files (e.g. workflows).
_SANDBOX_ALLOWED_FOREIGN = (".github/",)
_SANDBOX_SEED_COMMIT_MESSAGE = "chore(harness): seed sandbox"
# Lines the sandbox ``.gitignore`` must carry; a tracked one lacking any is replaced.
_SANDBOX_GITIGNORE_REQUIRED = (".hermes/",)
_DEFAULT_BRANCH_JQ = '.defaultBranchRef.name // ""'
_FOREIGN_DETAIL_LIMIT = 5


def _sandbox_default_branch(workspace: Path, sandbox: SandboxRepo) -> str:
    from .github_issues import _gh

    stdout = _gh(
        workspace,
        ["repo", "view", sandbox.repo, "--json", "defaultBranchRef", "--jq", _DEFAULT_BRANCH_JQ],
    )
    return stdout.strip()


def _remote_refs(workspace: Path, sandbox: SandboxRepo) -> list[str]:
    """Ref names (``refs/heads/*``, ``refs/tags/*``) the remote advertises."""
    result = _run_git(["ls-remote", "--heads", "--tags", "--", sandbox.url], cwd=workspace)
    refs = []
    for line in result.stdout.splitlines():
        _sha, sep, ref = line.rstrip("\n").partition("\t")
        if sep and ref:
            refs.append(ref)
    return refs


def _write_seed_files(project_dir: Path, rels: list[str]) -> None:
    try:
        for rel in rels:
            target = project_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_SANDBOX_SEED_FILES[rel])
    except OSError as exc:
        raise HarnessPreflightError("workspace_error", str(exc)) from exc


def _commit_and_push_seed(
    project_dir: Path, sandbox: SandboxRepo, branch: str, written: list[str], *, log: logging.Logger
) -> None:
    # ``add -f`` so a pre-existing tracked ``.gitignore`` cannot hide seed files;
    # ``-c commit.gpgsign=false`` so operator signing config cannot block the commit.
    _run_git(["add", "-f", "--", *written], cwd=project_dir)
    _run_git(["-c", "commit.gpgsign=false", "commit", "-m", _SANDBOX_SEED_COMMIT_MESSAGE], cwd=project_dir)
    incomplete = _tracked_blobs_missing(project_dir, tuple(_SANDBOX_SEED_FILES))
    if incomplete:
        raise HarnessPreflightError("seed_incomplete", ", ".join(incomplete))
    log.info("init-sandbox: pushing seed to %s %s", sandbox.repo, branch)
    _run_git(["push", "origin", f"HEAD:refs/heads/{branch}"], cwd=project_dir)


def _gitignore_needs_replacing(project_dir: Path) -> bool:
    result = _run_git(["show", "HEAD:.gitignore"], cwd=project_dir, check=False)
    if result.returncode != 0:
        # Defensive: callers only reach here when .gitignore is tracked at HEAD.
        return True
    lines = {line.strip() for line in result.stdout.splitlines()}
    return any(required not in lines for required in _SANDBOX_GITIGNORE_REQUIRED)


def _foreign_detail(foreign: list[str]) -> str:
    detail = ", ".join(foreign[:_FOREIGN_DETAIL_LIMIT])
    if len(foreign) > _FOREIGN_DETAIL_LIMIT:
        detail += f", +{len(foreign) - _FOREIGN_DETAIL_LIMIT} more"
    return detail


def _init_empty_sandbox(
    sandbox: SandboxRepo, workspace: Path, project_dir: Path, *, log: logging.Logger
) -> None:
    from .github_issues import _gh

    log.info("init-sandbox: initialising empty repository %s at %s", sandbox.repo, project_dir)
    _run_git(["init", "-b", "main"], cwd=project_dir)
    _run_git(["remote", "add", "origin", sandbox.url], cwd=project_dir)
    _run_git(["config", "user.email", _HARNESS_GIT_USER_EMAIL], cwd=project_dir)
    _run_git(["config", "user.name", _HARNESS_GIT_USER_NAME], cwd=project_dir)
    written = list(_SANDBOX_SEED_FILES)
    log.info("init-sandbox: writing seed files %s", ", ".join(written))
    _write_seed_files(project_dir, written)
    _commit_and_push_seed(project_dir, sandbox, "main", written, log=log)
    log.info("init-sandbox: setting default branch of %s to main", sandbox.repo)
    _gh(workspace, ["api", "-X", "PATCH", f"repos/{sandbox.repo}", "-f", "default_branch=main"])
    observed = _sandbox_default_branch(workspace, sandbox)
    if observed != "main":
        raise HarnessPreflightError("default_branch_unset", observed)


def _seed_existing_sandbox(
    sandbox: SandboxRepo, project_dir: Path, default_branch: str, *, log: logging.Logger
) -> str:
    log.info("init-sandbox: cloning %s (%s) into %s", sandbox.repo, default_branch, project_dir)
    clone_sandbox(sandbox, project_dir, branch=default_branch)
    checked_out = _run_git(["symbolic-ref", "--short", "HEAD"], cwd=project_dir).stdout.strip()
    if checked_out != default_branch:
        raise HarnessPreflightError("default_branch_mismatch", f"{checked_out} != {default_branch}")
    tracked = _run_git(["ls-tree", "-r", "--name-only", "HEAD"], cwd=project_dir).stdout.splitlines()
    foreign = [
        path
        for path in tracked
        if path not in _SANDBOX_SEED_FILES and not path.startswith(_SANDBOX_ALLOWED_FOREIGN)
    ]
    if foreign:
        raise HarnessPreflightError("sandbox_not_empty", _foreign_detail(foreign))
    tracked_set = set(tracked)
    to_write = [rel for rel in _SANDBOX_SEED_FILES if rel not in tracked_set]
    if ".gitignore" not in to_write and _gitignore_needs_replacing(project_dir):
        to_write.append(".gitignore")
    if not to_write:
        log.info("init-sandbox: %s already seeded on %s", sandbox.repo, default_branch)
        return "already_seeded"
    log.info("init-sandbox: %s tracks %s; writing %s", sandbox.repo, ", ".join(tracked) or "(nothing)", ", ".join(to_write))
    _write_seed_files(project_dir, to_write)
    _commit_and_push_seed(project_dir, sandbox, default_branch, to_write, log=log)
    return "seeded"


def init_sandbox(sandbox: SandboxRepo, workspace: Path, *, log: logging.Logger = log) -> str:
    """Seed the live sandbox repository so ``sandbox_seed_check`` passes.

    This is the ONLY code path in the harness allowed to push to the sandbox's
    default branch; runs push feature branches only. Every push is a plain
    fast-forward (never ``--force``). Returns ``"seeded"`` after pushing a seed
    commit or ``"already_seeded"`` when every seed file is already tracked and
    ``.gitignore`` carries the required rules (no writes, no push).

    - Empty repository: taken only when ``gh`` reports no default branch AND
      ``git ls-remote`` advertises no refs (``default_branch_unknown`` when they
      disagree). ``git init -b main`` under ``workspace/<slug>``, commit every
      seed file, push ``main``, set it as the GitHub default branch, and re-read
      it (``default_branch_unset`` if the re-read does not report ``main``).
    - Non-empty repository: clone the reported default branch, then refuse with
      ``sandbox_not_empty`` when any tracked path is neither a seed file nor
      under :data:`_SANDBOX_ALLOWED_FOREIGN`. The guard refuses any repository
      tracking files outside the seed set and ``.github/**``; a README-only or
      minimal-scaffold repository is treated as seedable by design. Otherwise
      write the missing seed files (and replace a ``.gitignore`` lacking the
      required rules), verify every seed file is a blob at HEAD
      (``seed_incomplete``), and push to the default branch, whatever its name.

    ``workspace/<slug>`` is removed again if seeding fails after creating it.
    """
    from .github_issues import check_auth

    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HarnessPreflightError("workspace_error", str(exc)) from exc
    check_auth(workspace)
    refs = _remote_refs(workspace, sandbox)
    default_branch = _sandbox_default_branch(workspace, sandbox)
    project_dir = workspace / sandbox.slug

    if default_branch == "" and refs:
        raise HarnessPreflightError("default_branch_unknown", ", ".join(refs[:_FOREIGN_DETAIL_LIMIT]))
    if default_branch == "" and project_dir.exists():
        raise HarnessPreflightError("workspace_exists", str(project_dir))

    created_project_dir = False
    try:
        if default_branch == "":
            try:
                project_dir.mkdir(parents=True)
            except OSError as exc:
                raise HarnessPreflightError("workspace_error", str(exc)) from exc
            created_project_dir = True
            _init_empty_sandbox(sandbox, workspace, project_dir, log=log)
            return "seeded"
        # clone_sandbox raises workspace_exists before creating anything.
        created_project_dir = not project_dir.exists()
        return _seed_existing_sandbox(sandbox, project_dir, default_branch, log=log)
    except BaseException:
        if created_project_dir:
            shutil.rmtree(project_dir, ignore_errors=True)
        raise


@dataclass(frozen=True)
class RunBaseline:
    """Remote state captured before a live harness run, for verification and cleanup."""

    head_pairs: tuple[tuple[str, str], ...]
    started_at: datetime
    viewer: str
    default_branch: str

    @property
    def heads(self) -> Mapping[str, str]:
        """Read-only ``{branch: sha}`` view of the remote heads."""
        return MappingProxyType(dict(self.head_pairs))


def take_baseline(
    project_dir: Path, *, viewer: str, default_branch: str, now: datetime | None = None
) -> RunBaseline:
    """Snapshot remote branch heads and a second-precision UTC start timestamp."""
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    result = _run_git(["ls-remote", "--heads", "origin"], cwd=project_dir)
    heads: dict[str, str] = {}
    for line in result.stdout.splitlines():
        sha, sep, ref = line.rstrip("\n").partition("\t")
        if sep and sha and ref.startswith("refs/heads/"):
            heads[ref.removeprefix("refs/heads/")] = sha
    # GitHub timestamps are second-precision: floor, then back off one second.
    started = now.astimezone(UTC).replace(microsecond=0) - timedelta(seconds=1)
    return RunBaseline(
        head_pairs=tuple(heads.items()),
        started_at=started,
        viewer=viewer,
        default_branch=default_branch,
    )


def _render_project_contract(profile_name: str) -> str:
    """Render the ``.hermes/pipeline.toml`` text for *profile_name* (raises on unknown profile)."""
    from .contract import required_capabilities
    from .phases import load_phases, resolve_profile_phases_path

    capabilities = sorted(required_capabilities(load_phases(resolve_profile_phases_path(profile_name))))
    capabilities_toml = ", ".join(f'"{item}"' for item in capabilities)
    return (
        "# Pipeline execution contract — read at tick start.\n"
        "# See docs/tutorial-getting-started.md and `tpo doctor --help`.\n"
        "schema_version = 2\n"
        f'assignee = "{_HARNESS_ASSIGNEE}"\n'
        f"capabilities = [{capabilities_toml}]\n"
        f'profile = "{profile_name}"\n'
    )


def _write_project_contract_text(project_dir: Path, pipeline_toml: str) -> None:
    hermes_dir = project_dir / ".hermes"
    hermes_dir.mkdir(exist_ok=True)
    (hermes_dir / "pipeline.toml").write_text(pipeline_toml)


def write_project_contract(project_dir: Path, profile_name: str) -> None:
    """Write the ``.hermes/pipeline.toml`` execution contract for *profile_name*.

    Creates ``.hermes/`` if needed and overwrites any existing ``pipeline.toml``
    (a cloned sandbox may already carry one).
    """
    _write_project_contract_text(project_dir, _render_project_contract(profile_name))


def create_mock_project(
    path: Path, fixture_name: str, profile_name: str = "gstack"
) -> dict[str, Any]:
    """Create a mock project in *path* for integration testing."""
    # Resolve the profile first so an unknown profile fails before touching the filesystem.
    pipeline_toml = _render_project_contract(profile_name)

    path.mkdir(parents=True, exist_ok=True)

    _env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True, env=_env)
    subprocess.run(["git", "config", "user.email", "test@localhost"], cwd=path, check=True, capture_output=True, env=_env)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True, capture_output=True, env=_env)

    gh_state = _fake_gh_state_for_fixture(fixture_name)
    (path / "README.md").write_text(f"# Mock Project — {fixture_name}\n")
    (path / ".gitignore").write_text(_MOCK_PROJECT_GITIGNORE)
    plan_path = path / _HARNESS_PLAN_PATH
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(_HARNESS_PLAN)

    # Create pipeline.toml contract for assignee configuration
    _write_project_contract_text(path, pipeline_toml)

    # Offline GitHub Issues backend: an executable ``gh`` stand-in plus its
    # gitignored state file, seeded with the fixture's single eligible issue.
    gh_bin = path / _FAKE_GH_BIN_RELPATH
    gh_bin.parent.mkdir()
    shutil.copyfile(fake_gh_script_path(), gh_bin)
    gh_bin.chmod(0o755)
    gh_state_path = path / _FAKE_GH_STATE_RELPATH
    gh_state_path.write_text(_json.dumps(gh_state, indent=2, sort_keys=True) + "\n")
    # The fetch URL gives the tick a GitHub identity; the push URL uses an
    # unknown scheme so any stray `git push` fails fast without touching the
    # network, and no credential helper can be consulted.
    for git_args in (
        ["remote", "add", "origin", _HARNESS_ORIGIN_URL],
        ["remote", "set-url", "--push", "origin", _HARNESS_NO_PUSH_URL],
        ["config", "--local", "credential.helper", ""],
    ):
        subprocess.run(["git", *git_args], cwd=path, check=True, capture_output=True, env=_env)

    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial: mock project setup"], cwd=path, check=True, capture_output=True)

    todo_id = _get_todo_id_for_fixture(fixture_name)
    return {
        "project_slug": "mock-project",
        "todo_id": todo_id,
        "branch": f"feat/mock-{fixture_name}",
        "fixture_name": fixture_name,
        "profile": profile_name,
        "repo": _HARNESS_REPO,
        "gh_bin": str(gh_bin),
        "gh_state": str(gh_state_path),
    }


def fake_gh_script_path() -> Path:
    """Filesystem path of the bundled offline ``gh`` stand-in."""
    from .contract import _resolve_bundled_dir

    return _resolve_bundled_dir("harness") / "fake_gh.py"


def _get_issue_body_for_fixture(fixture_name: str) -> str:
    """Issue body for the fixture TODO, rendered by the production issue-form renderer."""
    from .github_issues import render_issue_body

    if fixture_name != "happy-path":
        raise ValueError(f"Unknown fixture: {fixture_name}")
    return render_issue_body(_HARNESS_ISSUE_FIELDS | {"Branch": f"feat/mock-{fixture_name}"}, include_empty=False)


def _harness_decisions() -> dict[str, str]:
    """The fixture's decisions, derived from the same fields that build its issue body."""
    from .github_issues import DECISION_SECTIONS

    return {key: _HARNESS_ISSUE_FIELDS[key] for key in DECISION_SECTIONS if key in _HARNESS_ISSUE_FIELDS}


# Offline fixture issue: every review is opted out so the harness never blocks.
_HARNESS_ISSUE_FIELDS: dict[str, str] = {
    "What": (
        "Create `mock_transform.py` with "
        "`normalize_names(names: list[str]) -> list[str]`. For each input "
        "string, strip surrounding whitespace, discard empty strings after "
        "stripping, lowercase the remaining value, and preserve input order. "
        "Return an empty list for empty input."
    ),
    "Why": (
        "Provide a small, executable feature that exercises the complete "
        "harness pipeline without external services."
    ),
    "Context": (
        "Language `Python 3.12+`, Dependencies `standard library only`. "
        'Acceptance criteria: `normalize_names([" Alice ", "", "BOB"])` '
        'returns `["alice", "bob"]`; `normalize_names([])` returns `[]`; '
        "tests run with `uv run pytest`."
    ),
    "Plan": _HARNESS_PLAN_PATH,
    "Priority": "P1",
    "Effort": "S",
    "Phase": "4 (Development)",
    "Test Coverage": "required",
    "Security Review": "not-required",
    "UI Review": "not-required",
}


def _fake_gh_state_for_fixture(fixture_name: str) -> dict[str, Any]:
    """Initial fake ``gh`` state: one open, unblocked, ready ``tpo:todo`` issue (#1)."""
    from .github_issues import READY_LABEL, TODO_LABEL

    number = _get_todo_id_for_fixture(fixture_name)
    labels = [TODO_LABEL, READY_LABEL]
    return {
        "repo": _HARNESS_REPO,
        "labels": labels,
        "issues": {
            str(number): {
                "id": 1000 + number,
                "number": number,
                "title": "Implement mock name normalization",
                "body": _get_issue_body_for_fixture(fixture_name),
                "state": "open",
                "labels": [{"name": name} for name in labels],
                "assignees": [],
                "html_url": f"https://github.com/{_HARNESS_REPO}/issues/{number}",
                "issue_dependencies_summary": {
                    "blocked_by": 0,
                    "blocking": 0,
                    "total_blocked_by": 0,
                    "total_blocking": 0,
                },
            }
        },
        "comments": {},
        "dependencies": [],
    }


@contextmanager
def fake_gh_env(project_dir: Path):
    """Route every ``gh`` call (this process and children) to the fixture's fake gh.

    Sets ``TPO_GH_BIN`` and ``TPO_FAKE_GH_STATE`` and prepends ``<project>/bin``
    to ``PATH`` so agent shells resolving a bare ``gh`` hit the stub too. This
    mutates process-global ``os.environ`` (restored on exit) and is therefore
    not safe to nest or to use from concurrent threads.
    """
    from .github_issues import GH_BIN_ENV

    gh_bin = project_dir / _FAKE_GH_BIN_RELPATH
    values = {
        GH_BIN_ENV: str(gh_bin),
        _FAKE_GH_STATE_ENV: str(project_dir / _FAKE_GH_STATE_RELPATH),
        "PATH": os.pathsep.join(
            [str(gh_bin.parent), *filter(None, [os.environ.get("PATH", "")])]
        ),
    }
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, previous in saved.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


def _get_todo_id_for_fixture(fixture_name: str) -> int:
    return 1


def preflight_check(
    *,
    prompt_client: PromptClient = "claude",
    profile_name: str | None = None,
    prerequisites=None,
) -> None:
    """Verify required CLI tools are available.

    The ``gh`` check is skipped when ``TPO_GH_BIN`` names an override binary
    (the harness points it at the fixture's offline stub).
    """
    from .github_issues import GH_BIN_ENV
    from .hermes_adapter import AgentClientDependencyError, HermesDependencyError

    if shutil.which("git") is None:
        raise RuntimeError(
            "Missing dependency: git — Git is not installed or not on PATH. "
            "Install: https://git-scm.com"
        )
    if not os.environ.get(GH_BIN_ENV) and shutil.which("gh") is None:
        raise RuntimeError(
            "Missing dependency: gh — GitHub CLI is not installed or not on PATH. "
            "Install: https://cli.github.com"
        )
    if shutil.which("hermes") is None:
        raise HermesDependencyError(
            "Hermes CLI is not installed or not on PATH. Install: https://hermos.dev"
        )
    selected_product = {
        "claude": "Claude Code",
        "codex": "Codex",
    }[prompt_client]
    if shutil.which(prompt_client) is None:
        raise AgentClientDependencyError(
            f"{prompt_client} CLI for the selected prompt client "
            f"({selected_product}) is not installed or not on PATH."
        )
    if profile_name is not None and prerequisites is not None:
        _validate_profile_prerequisites(
            profile_name=profile_name,
            prompt_client=prompt_client,
            prerequisites=prerequisites,
        )


@dataclass
class ConvergenceDetector:
    """Track consecutive same-class phase failures within a single run."""
    threshold: int = 3
    _consecutive: int = field(default=0, repr=False)
    _last_error_class: str | None = field(default=None, repr=False)

    def record(self, phase_key: str, error_class: str | None) -> None:
        if error_class is None:
            self._consecutive = 0
            self._last_error_class = None
        elif error_class == self._last_error_class:
            self._consecutive += 1
        else:
            self._consecutive = 1
            self._last_error_class = error_class

    def should_halt(self) -> bool:
        return self._consecutive >= self.threshold


class HarnessMonitor:
    """Write pipeline events to a JSONL log file."""

    def __init__(self, log_path: Path) -> None:
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._start_time: float | None = None

    def __call__(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        event = {
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event_type": event_type,
        }
        if data:
            event.update(data)
        if event_type == "phase_started" and self._start_time is None:
            self._start_time = time.time()
            event["run_start_time"] = event["timestamp"]
        with open(self.log_path, "a") as f:
            f.write(_json.dumps(event, sort_keys=True) + "\n")


from .phases import Phase  # noqa: E402


class ConvergenceHaltError(Exception):
    """Raised when the convergence detector halts a run mid-phase-loop."""


class KanbanPreflightError(RuntimeError):
    """Raised when --kanban hermes is selected but the tenant is not accessible."""


class HarnessCleanupError(RuntimeError):
    """Raised when timeout cleanup cannot prove the workspace is quiescent."""


class PollCancellationError(RuntimeError):
    """Raised when the polling thread ignores cooperative cancellation."""


# Preflight timeout for hermes kanban list (seconds)
_PREFLIGHT_TIMEOUT = 15

# Maximum poll interval for kanban-as-scheduler phase polling (seconds)
_KANBAN_POLL_MAX_INTERVAL = 30.0

# Registration creates are individually bounded at 60 seconds. Allow the
# worker to observe cancellation after its current create returns so cleanup
# never races a still-mutating producer.
_POLL_CANCELLATION_TIMEOUT = 65.0

# Maximum characters in error messages captured by the harness
_ERROR_MESSAGE_MAX = 500

_OFFLINE_TERMINAL_PROMPT = """\
Run the harness local terminal workflow.

This disposable fixture's `origin` remote is a placeholder that must never be
contacted. Do not invoke the ship skill, and do not push or open a pull request.

1. Run the TODO acceptance tests and any repository tests added by prior phases.
2. Inspect the complete local diff and commit every intended fixture change.
3. Write the current branch name to `.hermes/pipeline_branch.txt` if needed and
   include that state in the final local commit.
4. Verify `git status --porcelain` is empty.

Complete this task only when the local branch is tested, clean, and fully
committed. Exit non-zero if any local verification or commit cannot complete.
"""


def _kanban_preflight(*, tenant: str) -> None:
    """Fail fast if the kanban tenant isn't accessible before constructing the real adapter.

    Runs `hermes kanban list --tenant <tenant>` and raises KanbanPreflightError with an
    actionable message on non-zero exit, rather than letting the failure surface later as a
    silent non-blocking warning deep in HermesKanbanAdapter.
    """
    try:
        result = subprocess.run(
            ["hermes", "kanban", "list", "--tenant", tenant],
            capture_output=True,
            text=True,
            timeout=_PREFLIGHT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise KanbanPreflightError(
            f"Preflight check timed out after {_PREFLIGHT_TIMEOUT}s: `hermes kanban list --tenant {tenant}` "
            f"did not respond. Verify your --kanban tenant is correct and reachable."
        )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise KanbanPreflightError(
            f"--kanban hermes requires `hermes login` and access to tenant '{tenant}'. "
            f"Verify with: hermes kanban list --tenant {tenant}\n"
            f"Preflight error: {detail}"
        )


def _auto_complete_gate_tasks(
    tenant: str,
    tick_id: str,
    *,
    completed_phase_key: str,
    phases: list[Phase] | None = None,
) -> None:
    """Complete blocked gate tasks whose direct predecessor just finished.

    Gate tasks are created as blocked with --parent pointing to their
    predecessor. In kanban-as-scheduler mode, the kanban board should
    unblock them when the parent finishes. However, if the kanban board
    doesn't propagate the unblock signal, we auto-complete the gate to
    let child phases proceed.

    Only completes gates whose predecessor matches completed_phase_key,
    preventing gates from auto-completing at registration time before
    their parent phase has run.

    Best-effort: exceptions are logged, not raised.
    """
    from .kanban_tasks import BLOCKED, complete_todo_kanban_task, get_todo_kanban_tasks

    try:
        tasks = get_todo_kanban_tasks(tenant, tick_id)
    except Exception as e:
        log.warning("failed to query kanban tasks for gate auto-complete: %s", e)
        return

    # Build predecessor map from the same phase list used for registration.
    # When --phase is used, only a subset of phases is registered — using
    # load_phases() (all phases) would create predecessor mappings for
    # phases that don't exist as kanban tasks, causing gates to never match.
    if phases is None:
        from .phases import load_phases
        phases = load_phases()
    gate_predecessor = {}
    for i, phase in enumerate(phases):
        if getattr(phase, "gate", False) and i > 0:
            gate_predecessor[phase.phase_key] = phases[i - 1].phase_key

    for phase_key, info in tasks.items():
        if info.status != BLOCKED:
            continue
        # Only auto-complete gates whose predecessor just finished.
        pred = gate_predecessor.get(phase_key)
        if pred is None or pred != completed_phase_key:
            continue
        if complete_todo_kanban_task(tenant, info.task_id):
            log.info("auto-completed gate task %s (%s) after %s done", info.task_id, phase_key, completed_phase_key)
        else:
            log.warning("gate task %s (%s) remains blocked: auto-complete after %s done failed", info.task_id, phase_key, completed_phase_key)


def _poll_kanban_phases(
    *,
    project_slug: str,
    tick_id: str,
    state_dir: Path,
    todo_id: str,
    project_dir: Path,
    phases_path: Path | None,
    monitor: _ConvergenceMonitor,
    detector: ConvergenceDetector,
    poll_interval: float = 5.0,
    max_poll_interval: float = _KANBAN_POLL_MAX_INTERVAL,
    phases: list[Phase] | None = None,
    prompt_client: PromptClient = "claude",
    cancel_event: Any = None,
    registration_event: Any = None,
    offline_terminal_phase_key: str | None = None,
) -> bool:
    """Poll kanban-as-scheduler phases to completion.

    1. Registers all phases as kanban tasks. With a written harness profile the
       cards are pre-rendered (a tpo-plan manifest fans the development phase
       out into ``plan:``/``validate:`` cards) and created directly; every loop
       decision below is keyed on that registered card set, not the profile.
    2. Auto-completes gate tasks so child phases become ready.
    3. Polls get_todo_kanban_status() until all phases terminal.
    4. Emits JSONL events via monitor.
    5. Calls observe_outcomes() to write decision store.

    Returns True if all phases completed successfully (all done), False otherwise.
    """
    from .contract import load_contract as _load_contract
    from .kanban_tasks import (
        TERMINAL_STATUSES,
        get_todo_kanban_status,
        observe_outcomes,
        register_todo_phases,
    )
    from .phases import Phase as _Phase

    # Resolve assignee from project contract (same path as tpo tick)
    assignee = "default"
    try:
        assignee = _load_contract(state_dir).assignee
    except Exception as e:
        log.warning("failed to load pipeline contract, using assignee='default': %s", e)
    log.info("registering kanban phases for %s tick %s (assignee=%s)", todo_id, tick_id, assignee)
    # Registration can mutate Hermes before it raises. Signal that cleanup may
    # be required before entering the call so a partial create cannot cause the
    # only durable recovery marker to be deleted with the harness workspace.
    if registration_event is not None:
        registration_event.set()
    # Registration renders the cards (a tpo-plan manifest fans the development
    # phase out into plan:/validate: cards). Capture that set so every loop
    # decision keys on the registered cards, and give the last worker card the
    # offline workflow when the manifest skipped the profile's terminal phase.
    registered_cards: list[_Phase] = []

    def _on_prepared(prepared):
        if offline_terminal_phase_key is not None and not any(
            task.phase_key == offline_terminal_phase_key for task in prepared
        ):
            prepared = _with_offline_terminal_card(prepared)
        registered_cards[:] = [
            _Phase(phase_key=task.phase_key, name=task.name, gate=task.gate, kind=task.kind)
            for task in prepared
        ]
        return prepared

    register_todo_phases(
        todo_id=todo_id,
        tick_id=tick_id,
        board_slug=project_slug,
        project_dir=project_dir,
        phases_path=phases_path,
        assignee=assignee,
        prompt_client=prompt_client,
        plan_path=_HARNESS_PLAN_PATH,
        spec_path=None,
        reference_paths=(),
        decisions=_harness_decisions(),
        cancel_event=cancel_event,
        transform_prepared=_on_prepared,
    )
    # Callers that stub registration never render cards: fall back to the profile.
    cards: list[_Phase] | None = registered_cards or phases
    # Intentionally unguarded — fail fast before polling begins, matching
    # register_todo_phases()'s unguarded call above.
    initial_status = get_todo_kanban_status(project_slug, tick_id)
    log.info(
        "initial phase status: %s",
        ", ".join(f"{k}={v}" for k, v in sorted(initial_status.items())) or "(none)",
    )

    # Gate tasks will be auto-completed when their parent phase finishes,
    # not at registration time — this ensures parent output exists before
    # child phases can start.

    previous_status: dict[str, str] = {}
    all_terminal = False
    current_interval = poll_interval
    card_by_key = {card.phase_key: card for card in cards or []}
    # Completion means every *registered* card is terminal — keying this on the
    # profile phases would spin forever under a manifest fan-out.
    expected_phase_keys = frozenset(card_by_key)
    pre_run_statuses = (None, "todo", "ready", "blocked")
    unstarted_statuses = (None, "todo", "ready")

    def _is_terminal_status(phase_key: str, status: str) -> bool:
        if status in TERMINAL_STATUSES:
            return True
        if status != "blocked":
            return False
        if not card_by_key:
            return True  # no card knowledge at all (bare unit callers): legacy rule
        card = card_by_key.get(phase_key)
        # A blocked registered gate waits for auto-completion; a blocked worker
        # is stuck for good. An unregistered key is never terminal by omission.
        return card is not None and not card.gate

    while not all_terminal:
        if cancel_event is not None:
            if cancel_event.wait(current_interval):
                return False
        else:
            time.sleep(current_interval)
        current_interval = min(current_interval * 1.5, max_poll_interval)

        try:
            status_map = get_todo_kanban_status(project_slug, tick_id)
        except Exception as e:
            log.warning("kanban status poll failed: %s", e)
            continue

        if not status_map:
            continue

        try:
            for phase_key, status in status_map.items():
                prev = previous_status.get(phase_key)

                if prev in pre_run_statuses and status == "running":
                    log.info("phase %s: %s -> running", phase_key, prev or "none")
                    monitor.current_phase_key = phase_key
                    monitor("phase_started", {"phase_key": phase_key, "todo_id": todo_id})

                elif prev == "running" and status == "done":
                    log.info("phase %s: running -> done", phase_key)
                    monitor.current_phase_key = None
                    monitor("phase_completed", {"phase_key": phase_key, "todo_id": todo_id, "duration_ms": 0})
                    # Auto-complete any gate task whose predecessor just finished
                    _auto_complete_gate_tasks(
                        project_slug, tick_id, completed_phase_key=phase_key, phases=cards
                    )

                elif prev == "running" and status == "failed":
                    log.info("phase %s: running -> failed", phase_key)
                    monitor.current_phase_key = None
                    # monitor() records the failure with the detector and raises
                    # ConvergenceHaltError itself if the threshold is tripped —
                    # see _ConvergenceMonitor.__call__. No separate detector.record()
                    # call is needed here.
                    monitor("phase_failed", {"phase_key": phase_key, "todo_id": todo_id, "duration_ms": 0})

                elif prev == "running" and status == "blocked":
                    log.info("phase %s: running -> blocked", phase_key)
                    monitor.current_phase_key = None
                    monitor("phase_blocked", {"phase_key": phase_key, "todo_id": todo_id})

                elif prev in unstarted_statuses and status == "blocked":
                    log.info("phase %s: %s -> blocked", phase_key, prev or "none")
                    monitor.current_phase_key = None
                    monitor("phase_blocked", {"phase_key": phase_key, "todo_id": todo_id})

                elif prev in pre_run_statuses and status == "done":
                    # Phase completed between polls without ever being observed
                    # as "running" (fast phase, coarse poll interval). Still
                    # emit the event and run gate auto-complete so downstream
                    # gates aren't left blocked.
                    log.info("phase %s: %s -> done", phase_key, prev or "none")
                    monitor.current_phase_key = None
                    monitor("phase_completed", {"phase_key": phase_key, "todo_id": todo_id, "duration_ms": 0})
                    _auto_complete_gate_tasks(
                        project_slug, tick_id, completed_phase_key=phase_key, phases=cards
                    )

                elif prev in pre_run_statuses and status == "failed":
                    log.info("phase %s: %s -> failed", phase_key, prev or "none")
                    monitor.current_phase_key = None
                    monitor("phase_failed", {"phase_key": phase_key, "todo_id": todo_id, "duration_ms": 0})
        except ConvergenceHaltError:
            log.warning(
                "convergence detector: %d+ consecutive phase_failure, halting",
                detector.threshold,
            )
            all_terminal = True

        if status_map != previous_status:
            current_interval = poll_interval
        previous_status = dict(status_map)

        if not all_terminal:
            all_terminal = (
                expected_phase_keys.issubset(status_map)
                and all(
                    _is_terminal_status(phase_key, status)
                    for phase_key, status in status_map.items()
                )
            )

    try:
        final_status = get_todo_kanban_status(project_slug, tick_id)
        observe_outcomes(state_dir=state_dir, tick_id=tick_id, status_map=final_status)
    except Exception as e:
        log.warning("observe_outcomes failed: %s", e)

    return expected_phase_keys.issubset(previous_status) and all(
        status == "done" for status in previous_status.values()
    )


def _classify_error_class(exc: Exception) -> str:
    """Bucket an exception into a coarse error class for convergence tracking / reports."""
    from .hermes_adapter import (
        AgentClientDependencyError,
        ClaudeCallError,
        ClaudeDependencyError,
        HermesCallError,
        HermesDependencyError,
    )

    if isinstance(
        exc,
        (HermesDependencyError, AgentClientDependencyError, ClaudeDependencyError),
    ):
        return "dependency_error"
    if isinstance(exc, HermesCallError):
        return "hermes_error"
    if isinstance(exc, ClaudeCallError):
        return "claude_error"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "phase_failure"


class _ConvergenceMonitor:
    """Wraps a monitor callback: forwards events, feeds the convergence detector,
    and tracks the currently in-flight phase for partial-report generation on
    overall-timeout. Raises ConvergenceHaltError if the detector trips.
    """

    def __init__(
        self,
        inner: Callable[[str, dict[str, Any] | None], None],
        detector: ConvergenceDetector,
        error_holder: dict[str, Any],
    ) -> None:
        self._inner = inner
        self._detector = detector
        self._holder = error_holder
        self.current_phase_key: str | None = None

    def __call__(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        data = dict(data or {})

        if event_type == "phase_started":
            self.current_phase_key = data.get("phase_key")
            self._inner(event_type, data)
            return

        if event_type == "phase_failed":
            error_class = self._holder.pop("error_class", "phase_failure")
            data["error_class"] = error_class
            self._inner(event_type, data)
            self._detector.record(data.get("phase_key", ""), error_class)
            if self._detector.should_halt():
                raise ConvergenceHaltError(
                    f"convergence detector: {self._detector.threshold}+ consecutive "
                    f"{error_class} failures, halting run"
                )
            return

        if event_type == "phase_completed":
            self._inner(event_type, data)
            self._detector.record(data.get("phase_key", ""), None)
            return

        self._inner(event_type, data)


def filter_phases(phases: list[Phase], phase_key: str) -> list[Phase]:
    """Return a single-element list with only the requested phase."""
    for p in phases:
        if p.phase_key == phase_key:
            return [p]
    available = ", ".join(p.phase_key for p in phases)
    raise ValueError(
        f"Unknown phase key {phase_key!r}. Available: {available}"
    )


def _offline_terminal_phase_key(
    phases: list[Phase], profile_name: str = "<selected>"
) -> str:
    """Return the executable phase that must be made safe for an offline run."""
    terminal_indexes = [index for index, phase in enumerate(phases) if phase.terminal]
    if len(terminal_indexes) != 1:
        raise HarnessProfileError(
            "invalid_terminal_topology",
            profile_name,
            "expected exactly one terminal phase",
        )
    terminal_index = terminal_indexes[0]
    terminal = phases[terminal_index]
    if not terminal.gate:
        return terminal.phase_key
    for phase in reversed(phases[:terminal_index]):
        if not phase.gate:
            return phase.phase_key
    raise HarnessProfileError(
        "invalid_terminal_topology",
        profile_name,
        "terminal gate has no executable predecessor",
    )


_LIVE_SAFE_PROFILES = frozenset({"gstack", "agent-skills"})


def validate_live_profile(phases: list[Phase], profile_name: str) -> None:
    """Reject profiles whose terminal phase is unsafe to run against a live sandbox."""
    terminal_count = sum(1 for phase in phases if phase.terminal)
    if terminal_count != 1:
        raise HarnessProfileError(
            "invalid_terminal_topology",
            profile_name,
            "expected exactly one terminal phase",
        )
    if profile_name not in _LIVE_SAFE_PROFILES:
        raise HarnessProfileError(
            "unsafe_terminal",
            profile_name,
            "profile is not on the live-safe allow-list",
        )


def _with_offline_terminal_card(prepared: list) -> list:
    """Append the offline workflow to the last worker card's external-agent prompt."""
    for index in range(len(prepared) - 1, -1, -1):
        task = prepared[index]
        if task.gate:
            continue
        marker = "END EXTERNAL AGENT PROMPT\n"
        body = (
            task.body.replace(marker, f"\n{_OFFLINE_TERMINAL_PROMPT.rstrip()}\n{marker}", 1)
            if marker in task.body
            else f"{task.body.rstrip()}\n\n{_OFFLINE_TERMINAL_PROMPT}"
        )
        return [*prepared[:index], replace(task, body=body), *prepared[index + 1:]]
    return prepared


def _with_offline_terminal_workflow(
    phases: list[Phase], terminal_phase_key: str
) -> list[Phase]:
    """Replace the network-only final phase for the no-remote harness fixture."""
    return [
        replace(phase, prompt=_OFFLINE_TERMINAL_PROMPT)
        if phase.phase_key == terminal_phase_key
        else phase
        for phase in phases
    ]


def _build_harness_profile_data(
    profile_data: dict[str, Any], phases: list[Phase]
) -> dict[str, Any]:
    """Preserve profile metadata while replacing phases for the harness run."""
    harness_profile_data = dict(profile_data)
    harness_profile_data["phases"] = [asdict(phase) for phase in phases]
    return harness_profile_data


def _validate_profile_prerequisites(
    *, profile_name: str, prompt_client: PromptClient, prerequisites
) -> None:
    unverified = unverified_prerequisite_ids(prerequisites, prompt_client)
    if unverified:
        raise HarnessProfileError(
            "unverified_prerequisites",
            profile_name,
            ", ".join(unverified),
        )
    for prerequisite in prerequisites.skills:
        client = prerequisite.clients[prompt_client]
        if (
            prerequisite.support == "Conditional"
            and client.discovery_root == HERMES_SKILL_REGISTRY_ROOT
        ):
            verified, _detail = verify_hermes_skill_registry_prerequisite(
                assignee=_HARNESS_ASSIGNEE,
                skill_id=prerequisite.skill_id,
                runner=subprocess.run,
            )
            if not verified:
                raise HarnessProfileError(
                    "missing_conditional_prerequisite",
                    profile_name,
                    prerequisite.skill_id,
                )


@contextmanager
def isolate_config(*, state_dir: Path):
    """Context manager that points tpo at an isolated config file.

    HOME is left untouched — the harness invokes real hermes/claude CLI
    subprocesses, which need the real $HOME to read auth credentials.
    """
    saved = {}
    for key in ("TPO_CONFIG_FILE",):
        if key in os.environ:
            saved[key] = os.environ[key]

    state_dir.mkdir(parents=True, exist_ok=True)
    config_path = state_dir / "tpo-config.yaml"
    config_path.write_text(f"state_dir: {state_dir}\n")
    os.environ["TPO_CONFIG_FILE"] = str(config_path)

    try:
        yield
    finally:
        for key in ("TPO_CONFIG_FILE",):
            if key in saved:
                os.environ[key] = saved[key]
            else:
                os.environ.pop(key, None)


@dataclass
class HarnessResult:
    """Result of a harness run."""
    exit_code: int
    report_path: Path | None
    temp_dir: Path | None
    summary: str


def _prune_retained_state(state_dir: Path) -> None:
    """Remove only disposable harness state from a retained fixture."""
    for filename in ("pipeline_branch.txt", "tpo-config.yaml"):
        path = state_dir / filename
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not prune retained harness state %s: %s", path, exc)

    for dirname in ("outcomes", "pipeline_checkpoints", "ready_for_review"):
        path = state_dir / dirname
        try:
            if not path.exists():
                continue
            if next(path.iterdir(), None) is not None:
                continue
            path.rmdir()
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.warning("could not prune retained harness state %s: %s", path, exc)




def _run_with_timeout(
    fn: Callable[[], bool], *, timeout: int, cancel_event: Any = None
) -> tuple[bool, bool, dict[str, Any]]:
    """Run `fn` on a daemon worker thread, joined with `timeout`.

    Returns (success, timed_out, result_box). result_box carries
    "convergence_error" or "exception" keys when fn raised those instead
    of returning normally.
    """
    import threading

    result_box: dict[str, Any] = {}

    def _run_and_capture() -> None:
        try:
            result_box["success"] = fn()
        except ConvergenceHaltError as e:
            result_box["convergence_error"] = e
        except Exception as e:
            result_box["exception"] = e

    worker = threading.Thread(target=_run_and_capture, daemon=True)
    worker.start()
    worker.join(timeout=timeout)

    if worker.is_alive():
        if cancel_event is None:
            raise PollCancellationError(
                "poll worker exceeded the overall timeout without cancellation"
            )
        cancel_event.set()
        worker.join(timeout=_POLL_CANCELLATION_TIMEOUT)
        if worker.is_alive():
            raise PollCancellationError(
                "poll worker did not stop after cooperative cancellation"
            )
        return False, True, result_box
    if "convergence_error" in result_box:
        log.warning(str(result_box["convergence_error"]))
        return False, False, result_box
    if "exception" in result_box:
        raise result_box["exception"]
    return result_box["success"], False, result_box


def _cancel_registered_tasks(
    *, project_slug: str, tick_id: str, project_dir: Path
) -> bool:
    """Attempt sanitized, marker-aware remote cleanup for one harness tick."""
    from .kanban_tasks import cancel_todo_kanban_tasks

    try:
        return cancel_todo_kanban_tasks(
            project_slug,
            tick_id,
            project_dir=project_dir,
        )
    except Exception as exc:
        log.warning(
            "remote harness cleanup failed: error_type=%s",
            type(exc).__name__,
        )
        return False


def run_harness(
    *,
    fixture_name: str,
    loop: bool,
    phase_only: str | None,
    keep_dir: bool,
    timeout: int,
    convergence_threshold: int,
    config: Any = None,
    profile_name: str = "gstack",
) -> HarnessResult:
    """Main orchestration: bootstrap fixture, run pipeline, generate report."""
    from .contract import PROFILE_NAME_RE
    from .kanban_tasks import TERMINAL_STATUSES, get_todo_kanban_status
    from .logging_setup import new_tick_id
    from .phases import (
        load_phases,
        load_profile_prerequisites,
        resolve_profile_phases_path,
    )
    from .test_report import (
        diff_reports,
        generate_report,
        summarize_diff,
        summarize_report,
    )

    prompt_client = getattr(config, "prompt_client", "claude")

    if not isinstance(profile_name, str) or not PROFILE_NAME_RE.fullmatch(profile_name):
        raise HarnessProfileError("invalid_profile_name", "<invalid>")
    profile_path = resolve_profile_phases_path(profile_name)
    profile_data = yaml.safe_load(profile_path.read_text())
    if not isinstance(profile_data, dict):
        raise HarnessProfileError("invalid_profile_data", profile_name)
    all_phases = load_phases(profile_path)
    prerequisites = load_profile_prerequisites(profile_name)
    unverified = unverified_prerequisite_ids(prerequisites, prompt_client)
    if unverified:
        raise HarnessProfileError(
            "unverified_prerequisites",
            profile_name,
            ", ".join(unverified),
        )
    offline_terminal_phase_key = _offline_terminal_phase_key(all_phases, profile_name)
    phases = all_phases
    if phase_only:
        phases = filter_phases(all_phases, phase_only)
        if phases[0].gate:
            raise HarnessProfileError(
                "gate_phase_not_executable", profile_name, phases[0].phase_key
            )
    phases = _with_offline_terminal_workflow(phases, offline_terminal_phase_key)

    # Allocate under ~/.hermes/tmp rather than the OS default temp root: on
    # macOS, tempfile.mkdtemp() resolves under /var/folders/..., which is a
    # symlink to /private/var/folders/... — a prefix the Hermes agent's
    # write-tool sensitive-path guard blocks, causing every worker in
    # --kanban hermes mode to crash-loop on writes inside the mock project.
    harness_tmp_root = Path("~/.hermes/tmp").expanduser()
    harness_tmp_root.mkdir(parents=True, exist_ok=True)
    workspace_dir = Path(tempfile.mkdtemp(prefix="harness-", dir=harness_tmp_root))
    project_dir = workspace_dir / "project"
    artifacts_dir = workspace_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    workspace_quiescent = True
    # Route gh for this process and every child (hermes/claude workers, tpo
    # subcommands run inside the fixture) to the fixture's offline stub.
    try:
        with fake_gh_env(project_dir):
            preflight_check(
                prompt_client=prompt_client,
                profile_name=profile_name,
                prerequisites=prerequisites,
            )
            fixture = create_mock_project(project_dir, fixture_name, profile_name)

            state_dir = project_dir / ".hermes"

            events_log = artifacts_dir / "events.jsonl"
            base_monitor = HarnessMonitor(events_log)
            detector = ConvergenceDetector(threshold=convergence_threshold)
            error_holder: dict[str, Any] = {}
            monitor = _ConvergenceMonitor(base_monitor, detector, error_holder)

            tick_id = new_tick_id()

            _kanban_preflight(tenant=fixture["project_slug"])

            # Always register an explicit harness profile. The full profile keeps
            # every production phase key while substituting the offline terminal
            # workflow for the no-remote fixture.
            _phases_path_override = artifacts_dir / "harness-phases.yaml"
            _phases_path_override.write_text(
                yaml.safe_dump(_build_harness_profile_data(profile_data, phases))
            )

            checkpoint_dir = state_dir / "pipeline_checkpoints"
            ready_dir = state_dir / "ready_for_review"
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            ready_dir.mkdir(parents=True, exist_ok=True)

            timed_out = False
            timed_out_phase: str | None = None
            with isolate_config(state_dir=state_dir):
                # Emit initial event so the events log file exists for report generation
                base_monitor(
                    "run_started",
                    {
                        "tick_id": tick_id,
                        "kanban_mode": "hermes",
                        "profile": profile_name,
                        "fixture_name": fixture_name,
                        "prompt_client": prompt_client,
                    },
                )

                import threading

                cancel_event = threading.Event()
                registration_event = threading.Event()

                def _poll() -> bool:
                    todo_id_str = f"TODO-{fixture['todo_id']}"
                    return _poll_kanban_phases(
                        project_slug=fixture["project_slug"],
                        tick_id=tick_id,
                        state_dir=state_dir,
                        todo_id=todo_id_str,
                        project_dir=project_dir,
                        phases_path=_phases_path_override,
                        monitor=monitor,
                        detector=detector,
                        phases=phases,
                        prompt_client=prompt_client,
                        cancel_event=cancel_event,
                        registration_event=registration_event,
                        offline_terminal_phase_key=offline_terminal_phase_key,
                    )

                try:
                    success, timed_out, result_box = _run_with_timeout(
                        _poll,
                        timeout=timeout,
                        cancel_event=cancel_event,
                    )
                except PollCancellationError as exc:
                    workspace_quiescent = False
                    raise HarnessCleanupError(
                        f"{exc}; remote cleanup was not started while the registration "
                        f"worker remained active; workspace retained at {workspace_dir}"
                    ) from exc
                except Exception as exc:
                    if not registration_event.is_set():
                        raise
                    cleanup_confirmed = _cancel_registered_tasks(
                        project_slug=fixture["project_slug"],
                        tick_id=tick_id,
                        project_dir=project_dir,
                    )
                    if cleanup_confirmed:
                        raise
                    workspace_quiescent = False
                    raise HarnessCleanupError(
                        "Poll failed after Hermes task registration and task or "
                        "worker termination could not be confirmed; workspace "
                        f"retained at {workspace_dir}"
                    ) from exc

                if timed_out:
                    try:
                        in_flight = get_todo_kanban_status(fixture["project_slug"], tick_id)
                        timed_out_phase = next(
                            (k for k, v in in_flight.items()
                             if v not in TERMINAL_STATUSES),
                            None,
                        )
                    except Exception:
                        pass
                    cleanup_confirmed = _cancel_registered_tasks(
                        project_slug=fixture["project_slug"],
                        tick_id=tick_id,
                        project_dir=project_dir,
                    )
                    if not cleanup_confirmed:
                        workspace_quiescent = False
                        raise HarnessCleanupError(
                            "Hermes task or worker termination could not be "
                            f"confirmed; workspace retained at {workspace_dir}"
                        )
                    timed_out_phase = timed_out_phase or monitor.current_phase_key
                    if timed_out_phase:
                        base_monitor(
                            "phase_timed_out",
                            {"phase_key": timed_out_phase},
                        )
                elif "convergence_error" in result_box:
                    # Convergence-halt fired during polling. The poll loop already
                    # exited with all_terminal=True, so phases are already in terminal
                    # state on the kanban board — no additional cleanup needed beyond
                    # surfacing the convergence error in the result.
                    log.warning("convergence-halt: %s", result_box["convergence_error"])

            output_dir = artifacts_dir / "reports"
            generate_report(events_log, output_dir)
            report_json = output_dir / "report.json"
            summary = summarize_report(report_json)
            if timed_out:
                summary = f"[overall timeout after {timeout}s] " + summary

            if loop:
                prev_reports = sorted(output_dir.parent.glob(f"{fixture_name}-report.*.json"))
                if prev_reports:
                    diffs = diff_reports(prev_reports[-1], report_json)
                    diff_summary = summarize_diff(diffs)
                    summary += f" | diff: {diff_summary}"

                if prev_reports:
                    next_n = int(prev_reports[-1].stem.split(".")[-1]) + 1
                else:
                    next_n = 1
                next_report = output_dir.parent / f"{fixture_name}-report.{next_n}.json"
                next_report.write_text(report_json.read_text())

            status_map = get_todo_kanban_status(fixture["project_slug"], tick_id)
            print(
                f"[kanban] tenant={fixture['project_slug']} tick_id={tick_id} "
                f"profile={profile_name} "
                f"phases={status_map} "
                f"report={report_json} keep={'yes' if keep_dir else 'no (temp dir will be removed)'}"
            )

            if keep_dir and not timed_out:
                _prune_retained_state(state_dir)

            exit_code = 0 if (success and not timed_out) else 1

            return HarnessResult(
                exit_code=exit_code,
                report_path=report_json,
                temp_dir=workspace_dir if keep_dir else None,
                summary=summary,
            )

    except Exception:
        raise

    finally:
        if not keep_dir and workspace_quiescent:
            shutil.rmtree(workspace_dir, ignore_errors=True)
