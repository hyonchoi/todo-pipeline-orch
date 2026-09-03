"""Live integration test harness — sandbox preflight, tick runner, shutdown."""

from __future__ import annotations

import hashlib
import json as _json
import logging
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
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
from .result_contract import ResultContractError, load_validated_registration

log = logging.getLogger(__name__)


_HARNESS_ASSIGNEE = "pipeline"
_HARNESS_PLAN_PATH = "docs/harness/TODO-1-plan.md"
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


class HarnessTickError(RuntimeError):
    """The production ``tpo tick`` subprocess did not register a runnable tick.

    ``tick_id`` is the tick id known when the error was raised; kanban cards may
    exist for it. Set for ``tick_not_started``, ``failed_to_spawn``,
    ``expected_phases_missing`` (and by callers for ``unexpected_registration``);
    ``None`` for ``tick_not_persisted`` and ``picked_none``, where no card can exist.
    ``tick_timeout`` (raised by :func:`run_tick`) carries ``None``: the subprocess
    may have persisted a tick id and spawned workers before the deadline, so the
    caller must re-read ``current_tick_id.txt`` and, when no new id is readable,
    treat the sandbox as not provably idle.

    Plan-pinned (``requires_plan``) runs add, all carrying the tick id:
    ``registration_invalid`` (``load_validated_registration`` rejected the
    persisted ``registration.json``; ``detail`` is the contract error message),
    ``registration_base_mismatch`` (the run was pinned to a ``base_sha`` other
    than the harness's Plan commit), ``registration_plan_mismatch`` (the
    registered ``plan_hash`` is not the hash of the Plan text the harness
    committed, as a ``git replace`` forgery in the clone would produce),
    ``unexpected_selection`` (raised by
    :func:`assert_tick_id_unchanged` when a later tick persisted a different, or
    no, tick id), and, reserved for the multi-tick driver, ``tick_stalled`` (no
    card progressed within the stall window) and ``tick_budget_exhausted`` (the
    tick budget ran out before the run completed).
    """

    def __init__(self, code: str, detail: str = "", *, tick_id: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.tick_id = tick_id


_HARNESS_REPO_ENV = "TPO_HARNESS_REPO"
_SANDBOX_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}/[A-Za-z0-9._-]{1,100}\Z")


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


def ready_issue_numbers(project_dir: Path, sandbox: SandboxRepo) -> tuple[int, ...]:
    """Numbers of open ``tpo:todo`` issues in ``sandbox`` carrying ``ready-for-agent``.

    This is the same listing the production tick compiles its candidates from, so
    it is the authority for "what the tick will see".
    """
    from .github_issues import READY_LABEL, list_todo_issues

    # list_todo_issues already returns issues sorted by number.
    return tuple(
        issue.number
        for issue in list_todo_issues(project_dir, repo=sandbox.repo)
        if READY_LABEL in issue.labels
    )


def other_ready_issues(
    project_dir: Path, sandbox: SandboxRepo, *, exclude_issue: int | None = None
) -> tuple[int, ...]:
    """Ready issue numbers other than ``exclude_issue`` (the harness's own issue)."""
    return tuple(n for n in ready_issue_numbers(project_dir, sandbox) if n != exclude_issue)


_ISSUE_VISIBILITY_TIMEOUT = 60.0


def wait_for_issue_visible(
    project_dir: Path,
    sandbox: SandboxRepo,
    *,
    issue_number: int,
    timeout: float = _ISSUE_VISIBILITY_TIMEOUT,
    poll_interval: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> None:
    """Block until the run's issue is the only ready ``tpo:todo`` issue the tick will see.

    GitHub's label-filtered issue listing lags a fresh ``gh issue create`` by seconds
    (observed live: the production tick listed nothing two seconds after the create
    and picked no candidate). The barrier polls the exact listing the tick uses and
    returns once ``issue_number`` is present. Raises :class:`HarnessPreflightError`:

    - ``sandbox_not_quiescent`` (detail ``#12, #15``) as soon as another ready
      issue is visible alongside ours;
    - ``issue_not_visible`` when ``timeout`` elapses without our issue appearing.
    """
    if not (math.isfinite(poll_interval) and poll_interval > 0):
        raise ValueError("poll_interval must be a finite positive number")
    if not (math.isfinite(timeout) and timeout >= 0):
        raise ValueError("timeout must be a finite non-negative number")
    deadline = now() + timeout
    while True:
        ready = ready_issue_numbers(project_dir, sandbox)
        others = [n for n in ready if n != issue_number]
        if others:
            raise HarnessPreflightError("sandbox_not_quiescent", ", ".join(f"#{n}" for n in others))
        if issue_number in ready:
            return
        remaining = deadline - now()
        if remaining <= 0:
            raise HarnessPreflightError(
                "issue_not_visible",
                f"issue #{issue_number} in {sandbox.repo} not listed as ready after {timeout:g}s; "
                f"check: gh issue view {issue_number} --repo {sandbox.repo}",
            )
        sleep(min(poll_interval, remaining))


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
# Inherited repo-location variables could redirect a harness git call (notably the
# provenance checks) into the agent-owned clone; every ``_run_git`` drops them.
_GIT_SCRUBBED_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_INDEX_FILE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_REPLACE_REF_BASE",
        "GIT_GRAFT_FILE",
        "GIT_NAMESPACE",
        "GIT_COMMON_DIR",
        # An inherited SSH wrapper could observe or redirect a network call.
        "GIT_SSH_COMMAND",
        "GIT_SSH",
    }
)


# Verbs that talk to a remote and therefore need credentials.
_GIT_NETWORK_VERBS = frozenset({"clone", "fetch", "ls-remote", "push"})


def _git_env() -> dict[str, str]:
    """Environment for every harness git call: no inherited repo location, no ambient config.

    Every ``GIT_CONFIG*`` variable and ``GIT_TEMPLATE_DIR`` is dropped and the system
    and global config files are disabled (``GIT_CONFIG_NOSYSTEM=1``,
    ``GIT_CONFIG_GLOBAL=os.devnull``): an ambient ``url.<decoy>.insteadOf`` would
    otherwise redirect a lease-protected delete to another repository with rc 0, and
    a template could plant hooks in the staging repo. ``LC_ALL=C`` keeps git's
    messages stable for classification. Accepted cost: operator git config is not
    read at all, so ``http.*`` (proxy, custom CA), ``safe.directory`` (re-supplied
    per call by :func:`_run_git`), ``credential.*`` (replaced by
    ``gh auth git-credential``, injected per call) and ``url.*.insteadOf`` (for
    example an SSH rewrite) do not apply; the ``https_proxy`` and ``GIT_SSL_CAINFO``
    environment variables still work. ``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_NOSYSTEM``
    need git >= 2.32; on older git the global file is still read.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _GIT_SCRUBBED_ENV and not key.startswith("GIT_CONFIG") and key != "GIT_TEMPLATE_DIR"
    }
    env.update(_GIT_NO_PROMPT_ENV)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["LC_ALL"] = "C"
    return env


def _git_verb(args: list[str]) -> str:
    """The git subcommand in *args*, skipping leading ``-c KEY=VAL`` pairs."""
    i = 0
    while i + 1 < len(args) and args[i] == "-c":
        i += 2
    return args[i] if i < len(args) else "git"


def _run_git(
    args: list[str], *, cwd: Path, check: bool = True, timeout: float = 300.0
) -> subprocess.CompletedProcess[str]:
    """Run ``git *args`` with the scrubbed environment (see :func:`_git_env`).

    Every call gets ``-c safe.directory=<cwd>`` because the protected-scope config
    that would normally mark the directory safe is disabled. Network verbs also get
    ``-c credential.helper=`` (reset any configured helpers) followed by
    ``-c credential.helper=!<gh> auth git-credential`` so the gh token is used even
    though the global config is ignored. The order matters: the empty value clears the
    list, the second entry is the only helper left.
    """
    from .github_issues import gh_bin

    verb = _git_verb(args)
    prefix = ["-c", f"safe.directory={Path(cwd).resolve()}"]
    if verb in _GIT_NETWORK_VERBS:
        prefix += ["-c", "credential.helper=", "-c", f"credential.helper=!{shlex.quote(gh_bin())} auth git-credential"]
    try:
        result = _git(
            ["git", *prefix, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=_git_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise HarnessPreflightError("git_error", f"git {verb} timed out after {timeout:g}s") from exc
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
    try:
        project_dir.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HarnessPreflightError("git_error", f"cannot create {project_dir.parent}: {exc}") from exc
    branch_args = ["--branch", branch] if branch is not None else []
    # Empty template: no ambient GIT_TEMPLATE_DIR hooks land in the clone (ruling R-10.1).
    _run_git(
        ["clone", *branch_args, "--template=", "--", sandbox.url, str(project_dir)], cwd=project_dir.parent
    )
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
    """Require the seed paths and a ``.gitignore`` carrying every runtime-state rule at HEAD.

    Every :data:`_SANDBOX_SEED_PATHS` entry must be a tracked file (blob), and
    ``HEAD:.gitignore`` must exist and contain each :data:`_SANDBOX_GITIGNORE_REQUIRED`
    line, so harness state written into the clone (``.hermes/``) can never reach a PR.
    """
    hint = f"run tpo test --repo {sandbox.repo} --init-sandbox"
    missing = _tracked_blobs_missing(project_dir, _SANDBOX_SEED_PATHS)
    if missing:
        raise HarnessPreflightError("sandbox_not_seeded", f"missing: {', '.join(missing)}; {hint}")
    shown = _run_git(["show", "HEAD:.gitignore"], cwd=project_dir, check=False)
    if shown.returncode != 0:
        raise HarnessPreflightError(
            "sandbox_not_seeded",
            f"missing: .gitignore (must ignore {', '.join(_SANDBOX_GITIGNORE_REQUIRED)}); {hint}",
        )
    lines = {line.strip() for line in shown.stdout.splitlines()}
    lacking = [rule for rule in _SANDBOX_GITIGNORE_REQUIRED if rule not in lines]
    if lacking:
        raise HarnessPreflightError(
            "sandbox_not_seeded", f".gitignore lacks: {', '.join(lacking)}; {hint}"
        )


_SANDBOX_GITIGNORE = """\
# Harness runtime state: pipeline.toml, pipeline_branch.txt, outcomes.
# Written into the clone by every run; none of it may reach a PR.
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
    _run_git(["init", "-q", "-b", "main", "--template="], cwd=project_dir)
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


def _reject_gh_override() -> None:
    """Fail closed when ``TPO_GH_BIN`` is set (ruling R-12.2).

    The live harness talks to the real sandbox: an overridden gh could hide or fake
    every remote step (seeding, issue, PR discovery, cleanup).
    """
    from .github_issues import GH_BIN_ENV

    if os.environ.get(GH_BIN_ENV):
        raise HarnessPreflightError(
            "gh_override_forbidden", f"unset {GH_BIN_ENV} to run the live harness with the real gh"
        )


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

    _reject_gh_override()
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


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _ls_remote_heads(project_dir: Path, url: str, *, cwd: Path | None = None) -> dict[str, str]:
    """``{branch: sha}`` for every ``refs/heads/*`` at *url* (never the clone's mutable ``origin``).

    Runs in *cwd* (default *project_dir*). Once the agent has run, callers pass a
    harness-owned directory instead: a clone-local ``url.<decoy>.insteadOf`` would
    otherwise redirect the listing. Any line that is not ``<sha>\t<ref>`` raises
    ``git_error``: a partial listing must never be mistaken for the remote's full
    head set.
    """
    result = _run_git(["ls-remote", "--heads", "--", url], cwd=project_dir if cwd is None else cwd)
    heads: dict[str, str] = {}
    for line in result.stdout.splitlines():
        sha, sep, ref = line.rstrip("\n").partition("\t")
        if not sep or not sha or not ref:
            raise HarnessPreflightError("git_error", f"git ls-remote: unparseable line {line[:120]!r}")
        if sha.startswith("ref: "):
            continue  # symref advertisement (``ref: refs/heads/main\tHEAD``)
        if _SHA_RE.match(sha) is None:
            raise HarnessPreflightError(
                "git_error", f"git ls-remote: non-hex object id in line {line[:120]!r}"
            )
        if ref.startswith("refs/heads/"):
            heads[ref.removeprefix("refs/heads/")] = sha
    return heads


def take_baseline(
    project_dir: Path,
    sandbox: SandboxRepo,
    *,
    viewer: str,
    default_branch: str,
    now: datetime | None = None,
) -> RunBaseline:
    """Snapshot the sandbox's branch heads and a second-precision UTC start timestamp."""
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    heads = _ls_remote_heads(project_dir, sandbox.url)
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


# Harness issue fields: every review is opted out so the run never blocks on a human.
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


_HARNESS_ISSUE_TITLE = "Implement mock name normalization"
_TODO_ID_RE = re.compile(r"\ATODO-[0-9]+\Z")
_RUN_TOKEN_RE = re.compile(r"\A[0-9a-z]{8}\Z")
_HARNESS_PLAN_PATH_RE = re.compile(r"\Adocs/harness/[0-9a-z]{8}-plan\.md\Z")
_RECONCILE_ATTEMPTS = 5
_RECONCILE_BACKOFF_SECONDS = 2.0
# Create failures that provably left no issue behind: re-raise instead of listing.
_CREATE_CODES_WITHOUT_SIDE_EFFECT = frozenset(
    {"gh_auth", "gh_missing", "gh_version", "gh_not_found", "gh_rejected"}
)


def _run_token() -> str:
    """Eight-character lowercase token that makes one live run's remote artifacts distinct."""
    from .logging_setup import new_tick_id

    return new_tick_id()[-8:].lower()


def _issue_prefix(run_token: str) -> str:
    """Title prefix that ties a sandbox issue to one run; the reconciliation key."""
    return f"[harness {run_token}]"


def _issue_title(run_token: str) -> str:
    return f"{_issue_prefix(run_token)} {_HARNESS_ISSUE_TITLE}"


def _issue_fields(*, branch: str, plan_path: str) -> dict[str, str]:
    """Issue-form fields for a live sandbox issue on *branch* planned at *plan_path*."""
    return _HARNESS_ISSUE_FIELDS | {"Branch": branch, "Plan": plan_path}


def _plan_document(todo_id: str) -> str:
    """The fixture plan re-addressed to *todo_id* (heading and manifest ``todo_id``)."""
    if _TODO_ID_RE.match(todo_id) is None:
        raise ValueError(f"invalid todo id: {todo_id!r}")
    return _HARNESS_PLAN.replace("TODO-1", todo_id)


def _harness_labels() -> list[str]:
    """Backlog labels plus the decision mirrors derived from the fixture fields."""
    from .github_issues import READY_LABEL, TODO_LABEL, phase_label

    fields = _HARNESS_ISSUE_FIELDS
    return [
        TODO_LABEL,
        READY_LABEL,
        f"priority:{fields['Priority']}",
        f"effort:{fields['Effort']}",
        phase_label(fields["Phase"]),
        f"test-coverage:{fields['Test Coverage']}",
        f"security-review:{fields['Security Review']}",
        f"ui-review:{fields['UI Review']}",
    ]


@dataclass(frozen=True)
class HarnessIssue:
    """The sandbox issue created for one live harness run."""

    number: int
    todo_id: str
    branch: str
    plan_path: str
    title: str
    run_token: str

    def __post_init__(self) -> None:
        number = self.number
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise ValueError(f"issue number must be a positive int, got {number!r}")
        # The path is interpolated into git argv and joined onto the clone root.
        if (
            self.plan_path.startswith("/")
            or ".." in self.plan_path.split("/")
            or _HARNESS_PLAN_PATH_RE.match(self.plan_path) is None
        ):
            raise ValueError(f"invalid harness plan path: {self.plan_path!r}")


def _issue_matches_run(project_dir: Path, sandbox: SandboxRepo, number: int, title: str) -> bool:
    """True when issue *number* in *sandbox* is a real issue (not a PR) titled *title*."""
    from .github_issues import _decode_json, _gh_api

    payload = _decode_json(_gh_api(project_dir, [f"repos/{sandbox.repo}/issues/{number}"]), "api")
    return (
        isinstance(payload, Mapping)
        and "pull_request" not in payload
        and payload.get("number") == number
        and payload.get("title") == title
    )


def create_harness_issue(
    project_dir: Path,
    sandbox: SandboxRepo,
    *,
    run_token: str,
    baseline: RunBaseline,
    sleep: Callable[[float], None] = time.sleep,
) -> HarnessIssue:
    """Create the run's issue in *sandbox*; reconcile against the remote if the create fails.

    The label vocabulary is ensured first (``ensure_labels`` is idempotent), so
    the create never fails on a missing mirror label. The number ``gh`` reports
    is verified by fetching the issue: only a real issue (not a PR) carrying this
    run's title is adopted. A create that fails locally without proving nothing
    was created (timeout, malformed output, transport error, rate limit) may
    still have succeeded remotely, so it is followed by an authoritative listing
    rather than a blind retry that could duplicate the issue. Failures that
    provably created nothing (auth, missing/old gh, unknown repo, rejected
    request) are re-raised as-is.
    """
    from .github_issues import (
        GitHubIssuesError,
        create_issue,
        ensure_labels,
        render_issue_body,
    )

    if _RUN_TOKEN_RE.match(run_token) is None:
        raise ValueError(f"invalid run token: {run_token!r}")
    branch = f"feat/harness-{run_token}"
    plan_path = f"docs/harness/{run_token}-plan.md"
    title = _issue_title(run_token)
    body = render_issue_body(_issue_fields(branch=branch, plan_path=plan_path), include_empty=False)
    ensure_labels(project_dir, repo=sandbox.repo)
    log.info("harness: creating sandbox issue %s in %s", title, sandbox.repo)
    number: int | None = None
    cause: Exception | None = None
    try:
        reported = create_issue(
            project_dir, title=title, body=body, labels=_harness_labels(), repo=sandbox.repo
        )
    except GitHubIssuesError as exc:
        if exc.code in _CREATE_CODES_WITHOUT_SIDE_EFFECT:
            raise
        cause = exc
    except OSError as exc:  # body temp file failures may follow a remote success
        cause = exc
    if cause is None:
        # Verify the reported number; any failure here (404, 403, timeout) is
        # inconclusive, so it falls through to reconciliation like a failed create.
        try:
            if _issue_matches_run(project_dir, sandbox, reported, title):
                number = reported
            else:
                cause = RuntimeError(f"issue #{reported} reported by gh is not this run's issue")
        except Exception as exc:
            cause = exc
    if number is None:
        assert cause is not None
        number = reconcile_created_issue(
            project_dir, sandbox, run_token=run_token, baseline=baseline, cause=cause, sleep=sleep
        )
    return HarnessIssue(
        number=number,
        todo_id=f"TODO-{number}",
        branch=branch,
        plan_path=plan_path,
        title=title,
        run_token=run_token,
    )


def _list_run_issues(
    project_dir: Path, sandbox: SandboxRepo, *, run_token: str, baseline: RunBaseline
) -> list[int]:
    """Numbers of the viewer's issues (not PRs) in *sandbox* titled for *run_token*."""
    from urllib.parse import quote

    from .github_issues import _LIST_TIMEOUT, _decode_json, _flatten_pages, _gh_api

    query = f"state=all&creator={quote(baseline.viewer, safe='')}&per_page=100"
    stdout = _gh_api(
        project_dir,
        ["--paginate", "--slurp", f"repos/{sandbox.repo}/issues?{query}"],
        timeout=_LIST_TIMEOUT,
    )
    prefix = _issue_prefix(run_token)
    numbers: list[int] = []
    for payload in _flatten_pages(_decode_json(stdout, "api", empty=[]), "api"):
        if not isinstance(payload, Mapping) or "pull_request" in payload:
            continue
        title = payload.get("title")
        number = payload.get("number")
        if (
            isinstance(title, str)
            and title.startswith(prefix)
            and isinstance(number, int)
            and not isinstance(number, bool)
            and number > 0
        ):
            numbers.append(number)
    return sorted(numbers)


def reconcile_created_issue(
    project_dir: Path,
    sandbox: SandboxRepo,
    *,
    run_token: str,
    baseline: RunBaseline,
    cause: Exception,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Find the issue a failed create may have made, by authoritative listing (not search).

    Lists the viewer's issues and keeps those titled ``[harness <run_token>]…``.
    Retries (with backoff) only while nothing matches, because GitHub's list
    index can lag a just-created issue; a listing failure counts as an empty
    attempt. Exactly one match is adopted as the created issue. None after all
    attempts means the remote state is unproven (``issue_unverified``). Several
    means a duplicate the operator must sort out (``issue_ambiguous``); this
    detection is best-effort: a duplicate that lags the index past the first
    non-empty listing is not seen, and the earlier match is adopted.
    """
    from .github_issues import GitHubIssuesError

    numbers: list[int] = []
    for attempt in range(1, _RECONCILE_ATTEMPTS + 1):
        try:
            numbers = _list_run_issues(project_dir, sandbox, run_token=run_token, baseline=baseline)
        except GitHubIssuesError as exc:
            log.warning("harness: reconcile listing attempt %d failed: %s", attempt, exc)
            numbers = []
        if numbers:
            break
        if attempt < _RECONCILE_ATTEMPTS:
            sleep(_RECONCILE_BACKOFF_SECONDS)
    prefix = _issue_prefix(run_token)
    if len(numbers) == 1:
        log.warning("harness: reconciled issue #%d after create failure (%s)", numbers[0], cause)
        return numbers[0]
    if not numbers:
        raise HarnessRemoteCleanupError(
            "issue_unverified",
            f"create failed ({cause}); could not prove no issue titled '{prefix}' exists; "
            f"check: gh issue list --repo {sandbox.repo} --state all --search '{prefix} in:title'",
        ) from cause
    raise HarnessRemoteCleanupError(
        "issue_ambiguous",
        f"{', '.join(f'#{number}' for number in numbers)} in {sandbox.repo} for {prefix}; "
        f"close duplicates: gh issue close <n> --repo {sandbox.repo}",
    ) from cause


def commit_plan(project_dir: Path, issue: HarnessIssue) -> str:
    """Write the plan for *issue* into the clone and commit only that file; return the HEAD sha.

    Idempotent: when HEAD already tracks an identical plan, nothing is committed.
    Other staged changes are left staged and untouched. Never pushes: publishing
    the branch is the pipeline's job, not the harness's.
    """
    document = _plan_document(issue.todo_id)
    target = project_dir / issue.plan_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(document, encoding="utf-8")
    tracked = _run_git(["cat-file", "-p", f"HEAD:{issue.plan_path}"], cwd=project_dir, check=False)
    if tracked.returncode == 0 and tracked.stdout == document:
        return _run_git(["rev-parse", "HEAD"], cwd=project_dir).stdout.strip()
    _run_git(["add", "-f", "--", issue.plan_path], cwd=project_dir)
    _run_git(
        [
            "-c", "commit.gpgsign=false", "commit", "--no-verify",
            "-m", f"docs(harness): plan for {issue.todo_id}", "--", issue.plan_path,
        ],
        cwd=project_dir,
    )
    return _run_git(["rev-parse", "HEAD"], cwd=project_dir).stdout.strip()


_RECORDED_BRANCH_RELPATH = Path(".hermes") / "pipeline_branch.txt"
_PR_VIEW_FIELDS = (
    "number,state,mergedAt,headRefName,baseRefName,author,createdAt,isCrossRepository,title,body"
)
_PR_STATES = frozenset({"OPEN", "CLOSED", "MERGED"})
_PROTECTED_BRANCHES = frozenset({"main", "master"})
_CANDIDATE_REF = "refs/harness/candidate"
_DEFAULT_REF = "refs/harness/default"
_PROVENANCE_OK = None


def _valid_branch_name(project_dir: Path, name: str) -> bool:
    """True when git accepts *name* as a branch name (``git check-ref-format --branch``)."""
    if not name or name.startswith("-"):
        return False
    result = _run_git(["check-ref-format", "--branch", name], cwd=project_dir, check=False)
    return result.returncode == 0


def read_recorded_branch(project_dir: Path) -> str | None:
    """The branch the pipeline recorded in ``.hermes/pipeline_branch.txt``, or None.

    Agent-written and therefore untrusted: it only widens PR *discovery* (an extra
    ``pulls?head=`` query) and never contributes to attribution or deletability.
    None when the file is missing, blank, multi-line, or not a valid branch name;
    a present-but-rejected value is logged at WARNING so an operator can inspect it.
    """
    path = project_dir / _RECORDED_BRANCH_RELPATH
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("harness: cannot read %s: %s", _RECORDED_BRANCH_RELPATH, exc)
        return None
    if not text.strip():
        return None
    lines = text.strip().splitlines()
    if len(lines) != 1:
        log.warning("harness: %s has %d lines; expected one branch name", _RECORDED_BRANCH_RELPATH, len(lines))
        return None
    value = lines[0].strip()
    if not _valid_branch_name(project_dir, value):
        log.warning("harness: %s names an invalid branch %r; ignoring", _RECORDED_BRANCH_RELPATH, value)
        return None
    return value


@dataclass(frozen=True)
class PullRequest:
    """One pull request in the sandbox, as reported by ``gh pr view --json``."""

    number: int
    state: str
    merged: bool
    head_ref: str
    base_ref: str
    author: str
    created_at: datetime
    cross_repository: bool
    title: str
    body: str


def _parse_github_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid timestamp: {value!r}")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"naive timestamp: {value!r}")
    return parsed.astimezone(UTC)


def _pr_from_payload(payload: Mapping[str, Any]) -> PullRequest:
    """Build a :class:`PullRequest` from the ``gh pr view --json`` shape; raises ValueError/TypeError/KeyError."""
    number = payload["number"]
    if not isinstance(number, int) or isinstance(number, bool):
        raise TypeError(f"invalid pr number: {number!r}")
    author = payload["author"]
    login = author.get("login") if isinstance(author, Mapping) else None
    if not isinstance(login, str) or not login:
        raise TypeError(f"invalid pr author: {author!r}")
    head_ref, base_ref, state = payload["headRefName"], payload["baseRefName"], payload["state"]
    for name, value in (("headRefName", head_ref), ("baseRefName", base_ref), ("state", state)):
        if not isinstance(value, str) or not value:
            raise TypeError(f"invalid pr {name}: {value!r}")
    if state not in _PR_STATES:
        raise ValueError(f"invalid pr state: {state!r}")
    cross = payload.get("isCrossRepository", False)
    if not isinstance(cross, bool):
        raise TypeError(f"invalid pr isCrossRepository: {cross!r}")
    title = payload.get("title") or ""
    body = payload.get("body") or ""
    if not isinstance(title, str) or not isinstance(body, str):
        raise TypeError("invalid pr title/body")
    return PullRequest(
        number=number,
        state=state,
        merged=payload.get("mergedAt") is not None,
        head_ref=head_ref,
        base_ref=base_ref,
        author=login,
        created_at=_parse_github_timestamp(payload["createdAt"]),
        cross_repository=cross,
        title=title,
        body=body,
    )


def _pr_numbers(items: object, verb: str) -> list[int]:
    """Integer ``number`` of every item in a REST list page; malformed input raises."""
    if not isinstance(items, list):
        raise HarnessRemoteCleanupError("pr_discovery_incomplete", f"{verb}: page is not a list")
    numbers: list[int] = []
    for item in items:
        number = item.get("number") if isinstance(item, Mapping) else None
        if not isinstance(number, int) or isinstance(number, bool):
            raise HarnessRemoteCleanupError("pr_discovery_incomplete", f"{verb}: item without a pr number")
        numbers.append(number)
    return numbers


def discover_candidate_prs(
    project_dir: Path, sandbox: SandboxRepo, *, head_refs: Sequence[str], run_token: str
) -> tuple[int, ...]:
    """Sorted unique PR numbers that *might* belong to the run (a superset; never a deletion decision).

    Union of every PR whose head is one of *head_refs* and every PR the search API
    matches on the phrase-quoted issue prefix. Any listing failure, empty output
    (``gh api --slurp`` always prints at least ``[]``), malformed page, or
    ``incomplete_results`` search page raises
    ``HarnessRemoteCleanupError("pr_discovery_incomplete")`` so callers fail closed
    and delete nothing.
    """
    from urllib.parse import quote

    from .github_issues import (
        _LIST_TIMEOUT,
        GitHubIssuesError,
        _decode_json,
        _flatten_pages,
        _gh_api,
    )

    owner = sandbox.repo.split("/", 1)[0]
    numbers: set[int] = set()
    try:
        for head_ref in dict.fromkeys(head_refs):
            head = quote(head_ref, safe="")
            stdout = _gh_api(
                project_dir,
                ["--paginate", "--slurp", f"repos/{sandbox.repo}/pulls?state=all&head={owner}:{head}&per_page=100"],
                timeout=_LIST_TIMEOUT,
            )
            numbers.update(_pr_numbers(_flatten_pages(_decode_json(stdout, "api"), "api"), "pulls"))
        query = quote(f'repo:{sandbox.repo} is:pr "{_issue_prefix(run_token)}"', safe="")
        stdout = _gh_api(
            project_dir,
            ["--paginate", "--slurp", f"search/issues?q={query}&per_page=100"],
            timeout=_LIST_TIMEOUT,
        )
        pages = _decode_json(stdout, "api")
        if not isinstance(pages, list):
            raise HarnessRemoteCleanupError("pr_discovery_incomplete", "search: response is not a page list")
        for page in pages:
            if not isinstance(page, Mapping) or "items" not in page:
                raise HarnessRemoteCleanupError("pr_discovery_incomplete", "search: page without items")
            if page.get("incomplete_results") is True:
                raise HarnessRemoteCleanupError("pr_discovery_incomplete", "search: incomplete_results")
            numbers.update(_pr_numbers(page["items"], "search"))
    except GitHubIssuesError as exc:
        raise HarnessRemoteCleanupError("pr_discovery_incomplete", f"listing pull requests failed: {exc}") from exc
    return tuple(sorted(numbers))


def fetch_pull_request(project_dir: Path, sandbox: SandboxRepo, number: int) -> PullRequest:
    """``gh pr view`` for PR *number*; failures raise ``pr_discovery_incomplete``."""
    from .github_issues import GitHubIssuesError, _decode_json, _gh

    try:
        stdout = _gh(
            project_dir,
            ["pr", "view", str(number), "--repo", sandbox.repo, "--json", _PR_VIEW_FIELDS],
        )
        payload = _decode_json(stdout, "pr view")
        if not isinstance(payload, Mapping):
            raise TypeError("pr view payload is not an object")
        return _pr_from_payload(payload)
    except (GitHubIssuesError, ValueError, TypeError, KeyError) as exc:
        raise HarnessRemoteCleanupError("pr_discovery_incomplete", f"pr #{number}: {exc}") from exc


def _is_ancestor(git_dir: Path, ancestor: str, descendant: str) -> bool:
    """``git merge-base --is-ancestor`` with replace refs disabled; exit codes other than 0/1 raise."""
    result = _run_git(
        ["-c", "core.useReplaceRefs=false", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=git_dir,
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    detail = _URL_USERINFO_RE.sub("://***@", (result.stderr or "").strip())[:200]
    raise HarnessPreflightError("git_error", f"git merge-base failed ({result.returncode}): {detail}")


_PROVENANCE_FORBIDDEN = ("info/grafts", "objects/info/alternates", "shallow")


def _planted_hooks_and_config(work: Path) -> list[str]:
    """Executable non-sample hooks and ``url.``/``hooksPath`` config lines in bare repo *work*."""
    found: list[str] = []
    hooks = work / "hooks"
    if hooks.is_dir():
        for hook in sorted(hooks.iterdir()):
            if hook.name.endswith(".sample") or not hook.is_file():
                continue
            if os.access(hook, os.X_OK):
                found.append(f"hooks/{hook.name}")
    config = work / "config"
    if config.is_file():
        text = config.read_text(errors="replace").lower()
        found.extend(f"config:{token}" for token in ("include", "hookspath", "insteadof") if token in text)
    return found


def _ensure_provenance_dir(root: Path) -> Path:
    """Create a FRESH bare repository under harness-owned *root* for one ancestry check.

    *root* is never deleted or reused as a repository: each check gets its own
    ``prov-*`` subdirectory (returned) so nothing left behind (grafts, alternates, a
    shallow file, stale refs) can shape ancestry, and the forgery vectors are asserted
    absent afterwards. The harness layout puts *root* beside the clone
    (``workspace/artifacts/provenance`` next to ``workspace/projects/<slug>``), so it
    is reachable from the clone via ``../..``; isolation therefore rests on the fresh
    per-check directory, the empty template, and the forbidden-file checks rather
    than on path unreachability (see :func:`_reject_nested_provenance_dir`). The
    caller removes the returned subdirectory when the check ends.
    """
    try:
        root.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="prov-", dir=root))
    except OSError as exc:
        raise HarnessRemoteCleanupError("pr_discovery_incomplete", f"cannot create provenance dir under {root}: {exc}") from exc
    # Empty template: nothing (hooks, config, info/exclude) is copied into the repo.
    _run_git(["init", "-q", "--bare", "--template="], cwd=work)
    leftover = [rel for rel in _PROVENANCE_FORBIDDEN if (work / rel).exists()]
    leftover.extend(_planted_hooks_and_config(work))
    if leftover:
        shutil.rmtree(work, ignore_errors=True)
        raise HarnessRemoteCleanupError(
            "pr_discovery_incomplete", f"fresh provenance dir carries {', '.join(leftover)}"
        )
    return work


def _branch_provenance_failure(
    sandbox: SandboxRepo,
    *,
    name: str,
    tip_sha: str,
    plan_sha: str,
    default_branch: str,
    provenance_dir: Path,
) -> str | None:
    """Why remote branch *name*@*tip_sha* is NOT provably this run's work, or None when it is.

    Run-scoped provenance (ruling R-8.1): the plan commit *plan_sha* (made by the
    harness itself) must be a non-vacuous ancestor of every commit the branch adds
    over the default branch. Everything is fetched from ``sandbox.url`` into
    a fresh bare repository created under *provenance_dir* for every check and removed
    afterwards (place the root outside the clone's parent tree, e.g.
    ``workspace/artifacts/provenance``), and
    ancestry is computed there with replace refs disabled: the agent-owned clone
    (``git replace --graft``, ``info/grafts``) can forge ancestry and is never
    consulted. The plan commit must therefore be reachable from the candidate tip
    on the remote; when it is absent after the fetch the branch cannot be the run's.
    Text, author identity, and agent-written files are never consulted either.
    """
    for label, value in (("tip", tip_sha), ("plan", plan_sha)):
        if _SHA_RE.match(value) is None:
            raise HarnessRemoteCleanupError("pr_discovery_incomplete", f"branch {name}: malformed {label} sha {value!r}")
    work: Path | None = None
    try:
        work = _ensure_provenance_dir(provenance_dir)
        if not _valid_branch_name(work, name) or not _valid_branch_name(work, default_branch):
            return "no run provenance"
        _run_git(
            [
                "fetch", "--no-tags", "--", sandbox.url,
                f"+refs/heads/{name}:{_CANDIDATE_REF}",
                f"+refs/heads/{default_branch}:{_DEFAULT_REF}",
            ],
            cwd=work,
        )
        fetched = _run_git(
            ["rev-parse", "--verify", "--quiet", f"{_CANDIDATE_REF}^{{commit}}"], cwd=work, check=False
        )
        if fetched.returncode != 0 or fetched.stdout.strip() != tip_sha:
            return "tip moved"
        present = _run_git(["cat-file", "-e", f"{plan_sha}^{{commit}}"], cwd=work, check=False)
        if present.returncode != 0:
            return "no run provenance"  # the plan commit was never pushed under this branch
        if _is_ancestor(work, plan_sha, _DEFAULT_REF):
            return "no run provenance"  # vacuous: the plan is already on the default branch
        if not _is_ancestor(work, plan_sha, tip_sha):
            return "no run provenance"
        added = _run_git(
            ["-c", "core.useReplaceRefs=false", "rev-list", tip_sha, f"^{_DEFAULT_REF}"], cwd=work
        )
        commits = added.stdout.split()
        if not commits or not all(_is_ancestor(work, plan_sha, commit) for commit in commits):
            return "no run provenance"
    except HarnessPreflightError as exc:
        raise HarnessRemoteCleanupError("pr_discovery_incomplete", f"branch {name}: {_preflight_detail(exc)}") from exc
    except OSError as exc:
        raise HarnessRemoteCleanupError("pr_discovery_incomplete", f"branch {name}: {exc}") from exc
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
    return _PROVENANCE_OK


def _preflight_detail(exc: HarnessPreflightError) -> str:
    return f"{exc.code}: {exc.detail}" if exc.detail else exc.code


def _reject_nested_provenance_dir(project_dir: Path, provenance_dir: Path) -> None:
    """The provenance root must be disjoint from the agent-owned clone.

    Inside the clone, the agent could reach it; containing the clone (e.g. the
    workspace root), a harness-owned directory would overlap agent-owned files.
    """
    clone, root = project_dir.resolve(), provenance_dir.resolve()
    if root.is_relative_to(clone):
        raise HarnessRemoteCleanupError("pr_discovery_incomplete", "provenance_dir must not be inside the clone")
    if clone.is_relative_to(root):
        raise HarnessRemoteCleanupError("pr_discovery_incomplete", "provenance_dir must not contain the clone")


def branch_has_run_provenance(
    project_dir: Path,
    sandbox: SandboxRepo,
    *,
    name: str,
    tip_sha: str,
    plan_sha: str,
    default_branch: str,
    provenance_dir: Path,
) -> bool:
    """True when every commit branch *name* adds over *default_branch* descends from *plan_sha*.

    Fetches the branch and the default branch from ``sandbox.url`` into the
    harness-owned *provenance_dir* (never *project_dir*, which the agent controls);
    a fetch failure raises ``pr_discovery_incomplete``. False when the tip no longer
    equals *tip_sha*, when *plan_sha* is not reachable on the remote, when it is
    already reachable from the default branch (vacuous), or when any added commit
    does not descend from the plan commit.
    """
    _reject_nested_provenance_dir(project_dir, provenance_dir)  # ancestry is never computed in the clone
    failure = _branch_provenance_failure(
        sandbox, name=name, tip_sha=tip_sha, plan_sha=plan_sha, default_branch=default_branch,
        provenance_dir=provenance_dir,
    )
    return failure is _PROVENANCE_OK


def _is_protected_branch(name: str, baseline: RunBaseline) -> bool:
    protected = {branch.casefold() for branch in _PROTECTED_BRANCHES | {baseline.default_branch}}
    return name.casefold() in protected or "refs" in name.split("/")


def is_attributable_pr(pr: PullRequest, *, baseline: RunBaseline, provenance_of_head: bool) -> bool:
    """The single predicate deciding whether *pr* was opened by this live run.

    Every condition must hold: same-repository head, authored by the baseline viewer,
    created no earlier than the baseline, head branch neither pre-existing nor the
    default branch, and the head branch carries run provenance
    (:func:`branch_has_run_provenance`). PR text is never consulted: the agent writes it.
    """
    if pr.cross_repository or pr.author != baseline.viewer or pr.created_at < baseline.started_at:
        return False
    if pr.head_ref in baseline.heads or pr.head_ref.casefold() == baseline.default_branch.casefold():
        return False
    return provenance_of_head is True


def is_deletable_branch(
    name: str, *, baseline: RunBaseline, project_dir: Path, provenance: bool
) -> bool:
    """Whether remote branch *name* may be deleted as an artifact of this run.

    Never a baseline head, the default branch or ``main``/``master`` (case-insensitive),
    anything in a ``refs`` namespace, or an invalid ref; and the branch must carry
    run provenance.
    """
    if _is_protected_branch(name, baseline) or name in baseline.heads:
        return False
    if not _valid_branch_name(project_dir, name):
        return False
    return provenance is True


@dataclass(frozen=True)
class RemoteArtifacts:
    """Remote state attributed to one live run, plus what was found but not attributable."""

    issue_number: int
    prs: tuple[PullRequest, ...]
    deletable_branches: tuple[tuple[str, str], ...]
    """``(name, verified tip sha)`` pairs so the deleter can compare-and-swap."""
    leftovers: tuple[str, ...]

    def __post_init__(self) -> None:
        number = self.issue_number
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise ValueError(f"issue_number must be a positive int, got {number!r}")
        for name, sha in self.deletable_branches:
            if not isinstance(sha, str) or _SHA_RE.match(sha) is None:
                raise ValueError(f"branch {name!r}: sha must be 40 lowercase hex, got {sha!r}")
            _screen_branch_name(name)


def _screen_branch_name(name: str) -> None:
    """Cheap syntactic screen for a branch name about to be interpolated into a refspec.

    Rejects whitespace, ``:``, ``*``, ``..``, a leading ``-`` (option injection), an
    empty name, any ``refs`` path segment, and the protected default branches.
    """
    if not isinstance(name, str) or not name or name.startswith("-"):
        raise ValueError(f"invalid branch name {name!r}")
    if any(ch.isspace() for ch in name) or ":" in name or "*" in name or ".." in name:
        raise ValueError(f"invalid branch name {name!r}")
    if "refs" in name.split("/"):
        raise ValueError(f"invalid branch name {name!r}")
    if name.casefold() in _PROTECTED_BRANCHES:
        raise ValueError(f"protected branch {name!r}")


class PullRequestInvariantError(RuntimeError):
    """The live run did not leave exactly one open, attributable pull request."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def verify_pull_request(artifacts: RemoteArtifacts, *, default_branch: str) -> PullRequest:
    """Return the single open, unmerged PR targeting *default_branch*, or raise the violated invariant.

    The base check (``pr_wrong_base``) is an invariant only: a PR that targets another
    branch is still attributable and is cleaned up by shutdown.
    """
    prs = artifacts.prs
    if not prs:
        raise PullRequestInvariantError("pr_missing", f"issue #{artifacts.issue_number} produced no attributable PR")
    if len(prs) != 1:
        raise PullRequestInvariantError("pr_ambiguous", ", ".join(f"#{pr.number}" for pr in prs))
    pr = prs[0]
    if pr.merged or pr.state == "MERGED":
        raise PullRequestInvariantError("pr_merged", f"#{pr.number}")
    if pr.state != "OPEN":
        raise PullRequestInvariantError("pr_closed", f"#{pr.number}")
    if pr.base_ref.casefold() != default_branch.casefold():
        raise PullRequestInvariantError("pr_wrong_base", f"#{pr.number} -> {pr.base_ref}")
    return pr


def pr_invariant_event(exc: PullRequestInvariantError) -> tuple[str, dict]:
    """Monitor event ``(name, payload)`` describing a failed PR invariant."""
    return ("pr_invariant_failed", {"code": exc.code, "detail": exc.detail})


_PR_CLOSE_COMMENT = "Closed by tpo test cleanup."


def _scrub_url(url: str) -> str:
    return _URL_USERINFO_RE.sub("://***@", url)


_LEASE_REFUSAL_TOKENS = ("stale info", "remote ref does not exist", "[rejected]")


def _branch_gone(url: str, name: str, *, cwd: Path) -> bool:
    """True when ``git ls-remote --heads`` lists no ``refs/heads/<name>`` on *url*."""
    result = _run_git(["ls-remote", "--heads", "--", url, name], cwd=cwd)
    return not any(line.endswith(f"\trefs/heads/{name}") for line in result.stdout.splitlines())


def cleanup_remote(
    project_dir: Path,
    sandbox: SandboxRepo,
    artifacts: RemoteArtifacts,
    *,
    staging_root: Path,
    log: logging.Logger = log,
) -> tuple[bool, tuple[str, ...]]:
    """Close the run's issue and open PRs and lease-delete its branches; never raise per operation.

    Returns ``(all_ok, leftovers)``. ``leftovers`` merges every failed operation
    (each with the manual command to finish it) with ``artifacts.leftovers``,
    sorted. ``all_ok`` is False only when an operation attempted here failed;
    discovery leftovers are already-reported non-artifacts and do not flip it.

    Branch deletion is a compare-and-swap: ``git push --force-with-lease=<ref>:<sha>
    :<ref>`` from a fresh bare staging repo refuses (``stale info``) when the
    remote tip no longer equals the sha recorded at discovery, so a branch that
    moved after discovery is reported, not deleted. A refusal is re-probed with
    ``ls-remote``: a branch that is already gone counts as deleted (idempotent).
    REST ref deletion has no such precondition and is deliberately not used; the
    manual command in a leftover keeps the lease too.

    Raises :class:`HarnessRemoteCleanupError` before anything remote is touched when
    *staging_root* overlaps the clone or the staging repo cannot be created (both
    ``cleanup_staging_failed``).
    """
    from .github_issues import GitHubIssuesError, _gh, close_issue

    try:
        _reject_nested_provenance_dir(project_dir, staging_root)
        work = _ensure_provenance_dir(staging_root)
    except (OSError, HarnessPreflightError, HarnessRemoteCleanupError) as exc:
        raise HarnessRemoteCleanupError("cleanup_staging_failed", f"{staging_root}: {exc}") from exc

    url = sandbox.url
    shown_url = _scrub_url(url)
    failures: list[str] = []
    try:
        issue = artifacts.issue_number
        log.info("harness cleanup: closing issue #%d in %s", issue, sandbox.repo)
        try:
            close_issue(project_dir, issue, repo=sandbox.repo)
        except (GitHubIssuesError, ValueError) as exc:
            code = getattr(exc, "code", "invalid")
            failures.append(f"issue #{issue}: close failed ({code}); run: gh issue close {issue} --repo {sandbox.repo}")

        for pr in artifacts.prs:
            if pr.merged or pr.state != "OPEN":
                continue
            log.info("harness cleanup: closing PR #%d in %s", pr.number, sandbox.repo)
            try:
                _gh(
                    project_dir,
                    ["pr", "close", str(pr.number), "--repo", sandbox.repo, "--comment", _PR_CLOSE_COMMENT],
                )
            except (GitHubIssuesError, ValueError) as exc:
                code = getattr(exc, "code", "invalid")
                failures.append(
                    f"pr #{pr.number}: close failed ({code}); run: gh pr close {pr.number} --repo {sandbox.repo}"
                )

        for name, sha in artifacts.deletable_branches:
            log.info("harness cleanup: deleting branch %s at %s from %s", name, sha[:7], sandbox.repo)
            try:
                _run_git(
                    [
                        "-c", f"core.hooksPath={os.devnull}",
                        "push", f"--force-with-lease=refs/heads/{name}:{sha}", "--", url, f":refs/heads/{name}",
                    ],
                    cwd=work,
                )
            except HarnessPreflightError as exc:
                # Only a ref-level refusal can mean "someone else already deleted it";
                # a transport or auth failure proves nothing about the remote ref.
                gone = False
                if any(token in exc.detail for token in _LEASE_REFUSAL_TOKENS):
                    try:
                        gone = _branch_gone(url, name, cwd=work)
                    except HarnessPreflightError:
                        gone = False
                if gone:
                    log.info("harness cleanup: branch %s already gone from %s", name, sandbox.repo)
                    continue
                failures.append(
                    f"branch {name} ({sha[:7]}): delete refused ({_preflight_detail(exc)});"
                    f" inspect: git ls-remote --heads -- {shown_url} {name}; then if safe:"
                    f" git push --force-with-lease=refs/heads/{name}:{sha} -- {shown_url} :refs/heads/{name}"
                )
    finally:
        shutil.rmtree(work, ignore_errors=True)

    return not failures, tuple(sorted((*failures, *artifacts.leftovers)))


def discover_remote_artifacts(
    project_dir: Path,
    sandbox: SandboxRepo,
    *,
    issue: HarnessIssue,
    baseline: RunBaseline,
    plan_sha: str,
    provenance_dir: Path,
) -> RemoteArtifacts:
    """Classify the sandbox's post-run PRs and new branches into attributable and leftover.

    Heads are enumerated at ``sandbox.url`` (not the clone's ``origin``); each new
    head's run provenance is checked once against *plan_sha* (the harness's own plan
    commit, see :func:`commit_plan`) inside *provenance_dir*, a harness-owned
    directory recreated fresh per check that callers must place outside the clone's
    parent tree (e.g. ``workspace/artifacts/provenance``). PR discovery queries the recorded branch, the
    issue branch, and every provenance head, so it is a superset of what deletion may
    touch. Raises ``HarnessRemoteCleanupError("pr_discovery_incomplete")`` when
    enumeration, fetch, ancestry, or PR discovery cannot be completed; callers must
    then delete nothing.

    Accepted residuals (coordinator rulings):

    (a) A branch an operator cuts from the default branch DURING a run and the agent
        then fast-forwards onto the run's commits is indistinguishable from a
        run-created branch. The sandbox is dedicated; operators must not create
        branches during an active run.
    (b) Once a run's PR is merged into the default branch, the vacuity guard
        (plan commit reachable from the default branch) disables deletion of that
        run's branch. This fails closed; the operator deletes the branch manually.
    """
    try:
        return _discover_remote_artifacts(
            project_dir, sandbox, issue=issue, baseline=baseline, plan_sha=plan_sha, provenance_dir=provenance_dir
        )
    except HarnessPreflightError as exc:
        raise HarnessRemoteCleanupError("pr_discovery_incomplete", _preflight_detail(exc)) from exc


def _discover_remote_artifacts(
    project_dir: Path,
    sandbox: SandboxRepo,
    *,
    issue: HarnessIssue,
    baseline: RunBaseline,
    plan_sha: str,
    provenance_dir: Path,
) -> RemoteArtifacts:
    _reject_nested_provenance_dir(project_dir, provenance_dir)
    recorded = read_recorded_branch(project_dir)
    provenance_dir.mkdir(parents=True, exist_ok=True)
    current = _ls_remote_heads(project_dir, sandbox.url, cwd=provenance_dir)  # never from the agent's clone
    new_heads = {name: sha for name, sha in sorted(current.items()) if name not in baseline.heads}
    failures: dict[str, str | None] = {}
    for name, sha in new_heads.items():
        if _is_protected_branch(name, baseline):
            failures[name] = "protected"
            continue
        failures[name] = _branch_provenance_failure(
            sandbox, name=name, tip_sha=sha, plan_sha=plan_sha,
            default_branch=baseline.default_branch, provenance_dir=provenance_dir,
        )
    provenance = {name: failure is _PROVENANCE_OK for name, failure in failures.items()}
    provenance_heads = [name for name, ok in provenance.items() if ok]
    head_refs = [ref for ref in (recorded, issue.branch, *provenance_heads) if ref is not None]

    prs: list[PullRequest] = []
    pr_leftovers: list[str] = []
    numbers = discover_candidate_prs(project_dir, sandbox, head_refs=head_refs, run_token=issue.run_token)
    for number in numbers:
        pr = fetch_pull_request(project_dir, sandbox, number)
        head = pr.head_ref
        if head in baseline.heads or head.casefold() == baseline.default_branch.casefold():
            pr_leftovers.append(f"pr #{number}: head {head} pre-existing (not created by this run)")
        elif head not in new_heads:
            pr_leftovers.append(f"pr #{number}: head {head} lacks run provenance")
        elif is_attributable_pr(pr, baseline=baseline, provenance_of_head=provenance[head]):
            prs.append(pr)
        else:
            pr_leftovers.append(f"pr #{number}: not attributable")

    deletable: list[tuple[str, str]] = []
    branch_leftovers: list[str] = []
    for name, sha in new_heads.items():
        if is_deletable_branch(name, baseline=baseline, project_dir=provenance_dir, provenance=provenance[name]):
            deletable.append((name, sha))
        else:
            reason = failures[name] if failures[name] is not None else "invalid ref name"
            branch_leftovers.append(f"branch {name} ({sha[:7]}): {reason}")
    return RemoteArtifacts(
        issue_number=issue.number,
        prs=tuple(prs),
        deletable_branches=tuple(deletable),
        leftovers=tuple(branch_leftovers + pr_leftovers),
    )


def preflight_check(
    *,
    prompt_client: PromptClient = "claude",
    profile_name: str | None = None,
    prerequisites=None,
) -> None:
    """Verify required CLI tools (git, gh, hermes, the selected prompt client) are available."""
    from .hermes_adapter import AgentClientDependencyError, HermesDependencyError

    if shutil.which("git") is None:
        raise RuntimeError(
            "Missing dependency: git — Git is not installed or not on PATH. "
            "Install: https://git-scm.com"
        )
    if shutil.which("gh") is None:
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


class HarnessRemoteCleanupError(HarnessCleanupError):
    """Remote sandbox state (issues, branches) cannot be proven clean or unambiguous."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


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

# Upper bound on waiting for the tick's kanban cards to reach a terminal status
# during shutdown before remote branch/PR cleanup is declared unsafe (seconds).
_SHUTDOWN_TIMEOUT = 300.0

# Maximum characters in error messages captured by the harness
_ERROR_MESSAGE_MAX = 500

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

    # Build predecessor map from the registered card set recovered from the tick:
    # load_phases() (all profile phases) could map predecessors onto phases that
    # exist as no kanban task, so gates would never match.
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


_PRE_RUN_STATUSES = (None, "todo", "ready", "blocked")
_UNSTARTED_STATUSES = (None, "todo", "ready")


def _emit_status_transitions(
    previous: dict[str, str],
    current: dict[str, str],
    *,
    monitor: _ConvergenceMonitor,
    todo_id: str,
    on_completed: Callable[[str], None],
) -> None:
    """Emit monitor events for every status transition between two snapshots.

    Shared by ``poll_registered_phases`` and ``poll_pinned_run``. Keys are
    ordinary keys — dynamic cards (``review:0``, ``finish``) that appear
    mid-run transition from ``None`` like any other. ``on_completed`` runs
    after each ``phase_completed`` emission. ``ConvergenceHaltError`` raised by
    the monitor propagates to the caller.
    """
    for phase_key, status in current.items():
        prev = previous.get(phase_key)

        if prev in _PRE_RUN_STATUSES and status == "running":
            log.info("phase %s: %s -> running", phase_key, prev or "none")
            monitor.current_phase_key = phase_key
            monitor("phase_started", {"phase_key": phase_key, "todo_id": todo_id})

        elif prev == "running" and status == "done":
            log.info("phase %s: running -> done", phase_key)
            monitor.current_phase_key = None
            monitor("phase_completed", {"phase_key": phase_key, "todo_id": todo_id, "duration_ms": 0})
            on_completed(phase_key)

        elif prev == "running" and status == "failed":
            log.info("phase %s: running -> failed", phase_key)
            monitor.current_phase_key = None
            # monitor() records the failure with the detector and raises
            # ConvergenceHaltError itself if the threshold is tripped —
            # see _ConvergenceMonitor.__call__.
            monitor("phase_failed", {"phase_key": phase_key, "todo_id": todo_id, "duration_ms": 0})

        elif prev == "running" and status == "blocked":
            log.info("phase %s: running -> blocked", phase_key)
            monitor.current_phase_key = None
            monitor("phase_blocked", {"phase_key": phase_key, "todo_id": todo_id})

        elif prev in _UNSTARTED_STATUSES and status == "blocked":
            log.info("phase %s: %s -> blocked", phase_key, prev or "none")
            monitor.current_phase_key = None
            monitor("phase_blocked", {"phase_key": phase_key, "todo_id": todo_id})

        elif prev in _PRE_RUN_STATUSES and status == "done":
            # Completed between polls without ever being observed as "running"
            # (fast phase, coarse poll interval). Still emit the event and run
            # the completion hook so downstream gates aren't left blocked.
            log.info("phase %s: %s -> done", phase_key, prev or "none")
            monitor.current_phase_key = None
            monitor("phase_completed", {"phase_key": phase_key, "todo_id": todo_id, "duration_ms": 0})
            on_completed(phase_key)

        elif prev in _PRE_RUN_STATUSES and status == "failed":
            log.info("phase %s: %s -> failed", phase_key, prev or "none")
            monitor.current_phase_key = None
            monitor("phase_failed", {"phase_key": phase_key, "todo_id": todo_id, "duration_ms": 0})


def poll_registered_phases(
    *,
    project_slug: str,
    tick_id: str,
    state_dir: Path,
    todo_id: str,
    cards: list[Phase],
    monitor: _ConvergenceMonitor,
    detector: ConvergenceDetector,
    poll_interval: float = 5.0,
    max_poll_interval: float = _KANBAN_POLL_MAX_INTERVAL,
    cancel_event: Any = None,
) -> bool:
    """Poll already-registered kanban cards to completion.

    Registration is the production ``tpo tick`` path's job; this function never
    creates cards. ``cards`` is the registered card set and keys every loop
    decision: completion, gate terminality, and gate auto-completion.

    1. Polls get_todo_kanban_status() until every card is terminal.
    2. Auto-completes gate tasks whose predecessor just finished.
    3. Emits JSONL events via monitor.
    4. Calls observe_outcomes() to write the decision store.

    Returns True if all cards completed successfully (all done), False otherwise.
    Raises ValueError when ``cards`` is empty.
    """
    if not cards:
        raise ValueError("poll_registered_phases requires a non-empty registered cards list")
    from .kanban_tasks import (
        TERMINAL_STATUSES,
        get_todo_kanban_status,
        observe_outcomes,
    )

    # Intentionally unguarded — fail fast before polling begins; the registration
    # being polled was recovered from the tick, so a failing status query here
    # means the board itself is unreachable.
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
    card_by_key = {card.phase_key: card for card in cards}
    # Completion means every *registered* card is terminal — keying this on the
    # profile phases would spin forever under a manifest fan-out.
    expected_phase_keys = frozenset(card_by_key)

    def _on_completed(phase_key: str) -> None:
        # Auto-complete any gate task whose predecessor just finished.
        _auto_complete_gate_tasks(project_slug, tick_id, completed_phase_key=phase_key, phases=cards)

    def _is_terminal_status(phase_key: str, status: str) -> bool:
        if status in TERMINAL_STATUSES:
            return True
        if status != "blocked":
            return False
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
            _emit_status_transitions(
                previous_status, status_map,
                monitor=monitor, todo_id=todo_id, on_completed=_on_completed,
            )
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


def poll_pinned_run(
    *,
    project_slug: str,
    tick_id: str,
    todo_id: str,
    step_keys: Iterable[str],
    monitor: _ConvergenceMonitor,
    detector: ConvergenceDetector,
    poll_interval: float = 5.0,
    max_poll_interval: float = _KANBAN_POLL_MAX_INTERVAL,
    cancel_event: Any = None,
) -> dict[str, str]:
    """Poll a ``requires_plan`` (native-sdd) run under one tick id until it settles.

    Unlike ``poll_registered_phases`` this never completes cards: gates
    (``validate:*``, ``review-acceptance``, ``human-gate``) belong to the
    ``tpo tick`` reconcilers, and dynamic cards (``review:0``, ``finish``)
    appear on later ticks under the same tick id. Transition events are
    emitted for every key that *changes* relative to the initial fetch, which
    seeds the baseline: cards already terminal when this call starts (settled by
    an earlier tick under the same tick id) are reported in the returned map but
    emit no event and are not re-recorded with the detector.

    Settled means the status map is non-empty, contains every ``step_keys``
    entry, and every status is in ``TERMINAL_STATUSES`` or ``BLOCKED``
    (``failed`` settles too; classification is the caller's job).

    Returns the settled map, the last observed map on convergence halt, or
    ``{}`` when ``cancel_event`` fires.

    Raises ValueError when ``step_keys`` is empty.
    """
    from .kanban_tasks import BLOCKED, TERMINAL_STATUSES, get_todo_kanban_status

    expected_keys = frozenset(step_keys)
    if not expected_keys:
        raise ValueError("poll_pinned_run requires a non-empty step_keys set")
    settled_statuses = TERMINAL_STATUSES | {BLOCKED}
    # Statuses this poller understands. Anything else (notably "unknown", which
    # get_todo_kanban_status substitutes for a card whose JSON omits "status")
    # can never settle, so it is logged per poll to keep a stuck run diagnosable.
    known_statuses = settled_statuses | {"todo", "ready", "running"}

    # Not a fail-fast probe: get_todo_kanban_status returns {} for the failures
    # it handles (TimeoutExpired, FileNotFoundError, JSONDecodeError), so an
    # unreachable board just yields an empty baseline and the loop keeps polling.
    # An OSError raised by subprocess.run itself still escapes to the caller.
    initial_status = get_todo_kanban_status(project_slug, tick_id)
    log.info(
        "initial pinned-run status: %s",
        ", ".join(f"{k}={v}" for k, v in sorted(initial_status.items())) or "(none)",
    )

    def _noop(_phase_key: str) -> None:
        return None

    # Seed from the initial snapshot: this poller is called once per tick against
    # a long-lived tick id, sharing one monitor and one ConvergenceDetector across
    # calls. Starting from {} would re-emit phase_completed/phase_failed for every
    # card an earlier tick already settled (None is a _PRE_RUN_STATUSES member) and
    # re-record those failures with the detector, which could halt the run
    # spuriously. poll_registered_phases runs once per run and keeps its {} start.
    previous_status: dict[str, str] = dict(initial_status)
    current_interval = poll_interval
    while True:
        if cancel_event is not None:
            if cancel_event.wait(current_interval):
                return {}
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

        unrecognized = {k: v for k, v in sorted(status_map.items()) if v not in known_statuses}
        if unrecognized:
            log.warning(
                "pinned-run poll sees unrecognized card status(es) that can never settle: %s",
                ", ".join(f"{k}={v}" for k, v in unrecognized.items()),
            )

        halted = False
        try:
            _emit_status_transitions(
                previous_status, status_map, monitor=monitor, todo_id=todo_id, on_completed=_noop
            )
        except ConvergenceHaltError:
            log.warning(
                "convergence detector: %d+ consecutive phase_failure, halting",
                detector.threshold,
            )
            halted = True

        # Defensive (R-H2.2): a card can reach a settled status with no emitted
        # transition — the shared emitter has no "-> archived" branch — which
        # would leave current_phase_key naming a finished phase and let an
        # overall-timeout partial report blame it. Clear it whenever the key it
        # names has settled. The emitter itself stays untouched.
        in_flight = monitor.current_phase_key
        if in_flight is not None and status_map.get(in_flight) in settled_statuses:
            monitor.current_phase_key = None

        if status_map != previous_status:
            current_interval = poll_interval
        previous_status = dict(status_map)

        if halted or (
            expected_keys.issubset(status_map)
            and all(status in settled_statuses for status in status_map.values())
        ):
            return previous_status


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
def isolate_config(*, state_dir: Path, projects_dir: Path, prompt_client: str = "claude"):
    """Context manager that points tpo at an isolated config file.

    Writes ``state_dir``/``projects_dir``/``prompt_client`` to
    ``<state_dir>/tpo-config.yaml`` and exports ``TPO_CONFIG_FILE`` so the
    production CLI (in-process or as a subprocess) reads only that file.

    HOME is left untouched — the harness invokes real hermes/claude CLI
    subprocesses, which need the real $HOME to read auth credentials.
    """
    saved = {}
    for key in ("TPO_CONFIG_FILE",):
        if key in os.environ:
            saved[key] = os.environ[key]

    state_dir.mkdir(parents=True, exist_ok=True)
    projects_dir.mkdir(parents=True, exist_ok=True)
    config_path = state_dir / "tpo-config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "state_dir": str(state_dir),
                "projects_dir": str(projects_dir),
                "prompt_client": prompt_client,
            },
            sort_keys=False,
        )
    )
    os.environ["TPO_CONFIG_FILE"] = str(config_path)

    try:
        yield
    finally:
        for key in ("TPO_CONFIG_FILE",):
            if key in saved:
                os.environ[key] = saved[key]
            else:
                os.environ.pop(key, None)


# Seam for tests: ``hermes_pipeline.harness._tick_runner`` is monkeypatched to a recorder.
_tick_runner = subprocess.run
_TICK_ENTRYPOINT = "import sys; from hermes_pipeline.cli import main; sys.exit(main(sys.argv[1:]))"


def run_tick(
    slug: str,
    *,
    cwd: Path,
    log_path: Path,
    timeout: float,
    env: Mapping[str, str] | None = None,
) -> int:
    """Run the production ``tpo tick <slug>`` as a subprocess and append its output to *log_path*.

    The subprocess runs in *cwd* and inherits ``os.environ`` (including
    ``TPO_CONFIG_FILE`` exported by :func:`isolate_config`) merged with *env*.
    The return code is informational only: ``tpo tick`` swallows per-project
    failures and returns 0, so callers must inspect the persisted tick state via
    :func:`recover_tick_registration`.

    On timeout the partial output is logged and ``HarnessTickError("tick_timeout")``
    is raised. The tick may already have spawned detached workers by then; the
    caller must run the quiescence check before treating the sandbox as idle.
    """
    argv = [sys.executable, "-c", _TICK_ENTRYPOINT, "tick", slug]
    merged_env = {**os.environ, **(env or {})}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        result = _tick_runner(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=merged_env,
        )
    except subprocess.TimeoutExpired as exc:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"# tpo tick {slug} rc=timeout {stamp}\n")
            fh.write(_decode_output(exc.output))
            fh.write(_decode_output(exc.stderr))
            fh.write(f"# timeout after {timeout}s\n")
        raise HarnessTickError("tick_timeout", f"{timeout}s") from exc
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"# tpo tick {slug} rc={result.returncode} {stamp}\n")
        fh.write(_decode_output(result.stdout))
        fh.write(_decode_output(result.stderr))
    return result.returncode


def _decode_output(chunk: str | bytes | None) -> str:
    """Normalize a captured stream chunk to text ending in exactly one newline (or empty)."""
    if chunk is None:
        return ""
    if isinstance(chunk, bytes):
        chunk = chunk.decode("utf-8", errors="replace")
    if not chunk:
        return ""
    return chunk if chunk.endswith("\n") else chunk + "\n"


@dataclass(frozen=True)
class TickRegistration:
    """What the production tick persisted under ``<project>/.hermes/`` before spawning."""

    tick_id: str
    todo_id: str
    phase_keys: tuple[str, ...]
    worktree: Path | None = None
    branch: str | None = None
    base_sha: str | None = None
    run_dir: Path | None = None
    pinned: bool = False


def _read_json_file(path: Path) -> Any:
    try:
        return _json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _log_tail(tick_log: Path | None, lines: int = 20) -> str:
    if tick_log is None:
        return ""
    try:
        text = tick_log.read_text()
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def read_current_tick_id(project_state: Path) -> str | None:
    """The tick id persisted in ``<project_state>/current_tick_id.txt``, or ``None``.

    Callers snapshot this before :func:`run_tick` and pass it as
    ``previous_tick_id`` to :func:`recover_tick_registration` so a stale id from
    an earlier tick is not mistaken for the new registration.
    """
    try:
        tick_id = (project_state / "current_tick_id.txt").read_text().strip()
    except OSError:
        return None
    return tick_id or None


def _compact_detail(payload: Mapping[str, Any]) -> str:
    return _json.dumps(payload.get("detail"), separators=(",", ":"), sort_keys=True)


def _find_new_spawn_failure(
    outcomes_dir: Path, previous_tick_id: str | None
) -> tuple[str | None, str] | None:
    """``(tick_id, compact detail)`` of the newest ``failed_to_spawn`` record not belonging to *previous_tick_id*.

    ``cli._record_failed_to_spawn`` may run before ``current_tick_id.txt`` is
    updated, so a spawn failure can exist for a tick that never persisted its id.
    """
    if not outcomes_dir.is_dir():
        return None
    candidates = [
        path
        for path in outcomes_dir.glob("*.json")
        if path.name != "expected-phases.json" and not path.name.endswith("-phases.json")
    ]

    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    candidates.sort(key=_mtime, reverse=True)
    for path in candidates:
        payload = _read_json_file(path)
        if not isinstance(payload, dict) or payload.get("outcome") != "failed_to_spawn":
            continue
        record_tick = payload.get("tick_id")
        # ``<tick_id>.json`` names the tick even when the payload omits it.
        tick_id = record_tick if isinstance(record_tick, str) else path.stem
        if tick_id != previous_tick_id:
            return (tick_id, _compact_detail(payload))
    return None


def recover_tick_registration(
    project_state: Path,
    *,
    expected_issue: int,
    tick_log: Path | None = None,
    previous_tick_id: str | None = None,
) -> TickRegistration:
    """Reconstruct the tick the production CLI registered from its state files.

    Reads, in order: ``current_tick_id.txt`` (must be present and differ from
    *previous_tick_id*), ``outcomes/<tick_id>-phases.json`` (first line:
    ``tick_started`` or ``picked_none``), ``outcomes/<tick_id>.json``
    (``failed_to_spawn``), and ``outcomes/expected-phases.json``.

    When no new tick id was persisted, ``outcomes/*.json`` is scanned for a
    ``failed_to_spawn`` record from a tick other than *previous_tick_id* so a
    pre-persist spawn failure is reported as ``failed_to_spawn`` rather than
    ``tick_not_persisted``. The ``tick_not_persisted`` detail is the tail of
    *tick_log* and may contain sensitive tool output; redact before sharing.

    Caveats: none of these files record which issue was picked for non-plan
    profiles, so the caller must guarantee via preflight that *expected_issue* is
    the only eligible issue; ``todo_id`` is derived from it. For
    ``requires_plan`` profiles the sentinel is written under the run worktree
    (``<worktree>/.hermes/outcomes/``), not *project_state*; pass the worktree's
    ``.hermes`` directory.

    Every ``HarnessTickError`` raised once a tick id is known (``tick_not_started``,
    ``failed_to_spawn``, ``expected_phases_missing``) carries it as ``exc.tick_id``
    so :func:`shutdown_run` can still quiesce cards that may exist for it.
    """
    tick_id, outcomes_dir = _recover_started_tick(
        project_state, tick_log=tick_log, previous_tick_id=previous_tick_id
    )
    return TickRegistration(
        tick_id=tick_id,
        todo_id=f"TODO-{expected_issue}",
        phase_keys=_read_expected_phases(outcomes_dir, tick_id),
    )


def _recover_started_tick(
    project_state: Path, *, tick_log: Path | None, previous_tick_id: str | None
) -> tuple[str, Path]:
    """``(tick_id, outcomes_dir)`` of the newly persisted, started, spawned tick.

    Raises ``tick_not_persisted``/``failed_to_spawn`` when no new tick id exists,
    ``picked_none``/``tick_not_started`` from the first ``<tick>-phases.json``
    entry, and ``failed_to_spawn`` from ``<tick>.json``.
    """
    tick_id = read_current_tick_id(project_state)
    outcomes_dir = project_state / "outcomes"
    if tick_id is None or tick_id == previous_tick_id:
        spawn_failure = _find_new_spawn_failure(outcomes_dir, previous_tick_id)
        if spawn_failure is not None:
            spawn_tick, spawn_detail = spawn_failure
            raise HarnessTickError("failed_to_spawn", spawn_detail, tick_id=spawn_tick)
        raise HarnessTickError("tick_not_persisted", _log_tail(tick_log))

    phases_path = outcomes_dir / f"{tick_id}-phases.json"
    first_entry: Any = None
    try:
        for line in phases_path.read_text().splitlines():
            if line.strip():
                first_entry = _json.loads(line)
                break
    except (OSError, ValueError):
        first_entry = None
    outcome = first_entry.get("outcome") if isinstance(first_entry, dict) else None
    if outcome == "picked_none":
        raise HarnessTickError("picked_none", tick_id)
    if outcome != "tick_started":
        raise HarnessTickError("tick_not_started", tick_id, tick_id=tick_id)

    spawn_record = _read_json_file(outcomes_dir / f"{tick_id}.json")
    if isinstance(spawn_record, dict) and spawn_record.get("outcome") == "failed_to_spawn":
        raise HarnessTickError("failed_to_spawn", _compact_detail(spawn_record), tick_id=tick_id)
    return tick_id, outcomes_dir


def _read_expected_phases(outcomes_dir: Path, tick_id: str) -> tuple[str, ...]:
    """Phase keys from ``<outcomes_dir>/expected-phases.json``, else ``expected_phases_missing``."""
    expected = _read_json_file(outcomes_dir / "expected-phases.json")
    if (
        not isinstance(expected, list)
        or not expected
        or not all(isinstance(key, str) for key in expected)
    ):
        raise HarnessTickError("expected_phases_missing", tick_id, tick_id=tick_id)
    return tuple(expected)


def recover_pinned_registration(
    project_dir: Path,
    project_state: Path,
    *,
    issue: HarnessIssue,
    repo: str,
    plan_sha: str,
    plan_text: str,
    tick_log: Path | None = None,
    previous_tick_id: str | None = None,
) -> TickRegistration:
    """Recover a plan-pinned (``requires_plan``) tick through the production trust boundary.

    After the started-tick checks of :func:`recover_tick_registration`, the
    persisted ``<project_state>/runs/<tick_id>/registration.json`` is loaded
    with ``load_validated_registration`` (schema, path containment, worktree
    identity, Plan hash, issue snapshot); any contract violation is reported as
    ``registration_invalid`` with the contract error message (code, plus its
    detail when present) as ``detail``. The registration must pin *issue*'s
    number, branch and plan path (``unexpected_registration``, detail naming the
    field); ``base_sha`` must be *plan_sha*, the commit the harness pushed the
    Plan in (``registration_base_mismatch``); ``plan_hash`` must equal the
    SHA-256 of *plan_text*, the Plan the harness committed itself
    (``registration_plan_mismatch``) -- the contract's own hash check reads the
    Plan from the agent-controlled clone, where ``git replace`` can forge it;
    and the ``expected-phases.json`` sentinel under the run worktree must list
    exactly the registered ``step_keys`` (``expected_phases_missing`` /
    ``unexpected_registration``, detail capped at ``_ERROR_MESSAGE_MAX``).
    """
    tick_id, _ = _recover_started_tick(
        project_state, tick_log=tick_log, previous_tick_id=previous_tick_id
    )
    try:
        validated = load_validated_registration(project_dir, project_state, tick_id, repo=repo)
    except ResultContractError as exc:
        raise HarnessTickError("registration_invalid", str(exc), tick_id=tick_id) from exc
    if validated.issue_number != issue.number:
        raise HarnessTickError(
            "unexpected_registration", f"issue {validated.issue_number}", tick_id=tick_id
        )
    for name, actual, wanted in (
        ("branch", validated.branch, issue.branch),
        ("plan_path", validated.plan_path, issue.plan_path),
    ):
        if actual != wanted:
            raise HarnessTickError(
                "unexpected_registration",
                f"{name} {actual} != {wanted}"[:_ERROR_MESSAGE_MAX],
                tick_id=tick_id,
            )
    if validated.base_sha != plan_sha:
        raise HarnessTickError(
            "registration_base_mismatch", f"{validated.base_sha} != {plan_sha}", tick_id=tick_id
        )
    expected_hash = hashlib.sha256(plan_text.encode("utf-8")).hexdigest()
    if validated.plan_hash != expected_hash:
        raise HarnessTickError(
            "registration_plan_mismatch",
            f"{validated.plan_hash} != {expected_hash}",
            tick_id=tick_id,
        )
    expected = _read_expected_phases(validated.worktree / ".hermes" / "outcomes", tick_id)
    step_keys = tuple(validated.step_keys)
    # result_contract already rejects duplicate step_keys; only the sentinel can repeat.
    if set(expected) != set(step_keys) or len(set(expected)) != len(expected):
        raise HarnessTickError(
            "unexpected_registration",
            f"expected phases {list(expected)} != step keys {list(step_keys)}"[:_ERROR_MESSAGE_MAX],
            tick_id=tick_id,
        )
    return TickRegistration(
        tick_id=tick_id,
        todo_id=issue.todo_id,
        phase_keys=step_keys,
        worktree=validated.worktree,
        branch=validated.branch,
        base_sha=validated.base_sha,
        run_dir=project_state / "runs" / tick_id,
        pinned=True,
    )


def assert_tick_id_unchanged(project_state: Path, *, expected: str) -> None:
    """Raise ``unexpected_selection`` unless ``current_tick_id.txt`` still reads *expected*.

    Later ticks of a plan-pinned run must not register a new tick; a missing or
    blank id is reported with detail ``"missing"``. ``tick_id`` is *expected*
    because that is the tick whose cards may still exist.
    """
    current = read_current_tick_id(project_state)
    if current is None:
        raise HarnessTickError("unexpected_selection", "missing", tick_id=expected)
    if current != expected:
        raise HarnessTickError("unexpected_selection", f"{current} != {expected}", tick_id=expected)


def cards_for_registered_keys(phases: Sequence[Phase], keys: Sequence[str]) -> list[Phase]:
    """Map registered phase keys back to profile ``Phase`` cards, preserving order.

    Unknown or duplicate keys raise ``HarnessTickError("unexpected_registration", key)``.
    """
    by_key = {phase.phase_key: phase for phase in phases}
    seen: set[str] = set()
    cards: list[Phase] = []
    for key in keys:
        if key in seen or key not in by_key:
            raise HarnessTickError("unexpected_registration", key)
        seen.add(key)
        cards.append(by_key[key])
    return cards


@dataclass
class HarnessResult:
    """Result of a harness run."""
    exit_code: int
    report_path: Path | None
    temp_dir: Path | None
    summary: str
    issue_number: int | None = None
    pr_numbers: tuple[int, ...] = ()
    cleanup_leftovers: tuple[str, ...] = ()


def _prune_retained_state(project_state: Path, state_dir: Path) -> None:
    """Remove only disposable harness state from a retained workspace.

    ``pipeline_branch.txt`` under *project_state* is kept on purpose: it is the
    pointer to the remote branch a ``--keep`` run leaves behind. The isolated
    ``tpo-config.yaml`` lives in *state_dir* (``workspace/state``), not in the clone.
    """
    config_path = state_dir / "tpo-config.yaml"
    try:
        config_path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not prune retained harness state %s: %s", config_path, exc)

    for dirname in ("outcomes", "pipeline_checkpoints", "ready_for_review"):
        path = project_state / dirname
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
    of returning normally. A ``KeyboardInterrupt`` raised on the worker propagates
    unchanged; any other ``BaseException`` (``SystemExit``, ...) is translated into
    ``PollCancellationError`` so the caller's shutdown obligation still holds.
    """
    import threading

    result_box: dict[str, Any] = {}

    def _run_and_capture() -> None:
        try:
            result_box["success"] = fn()
        except ConvergenceHaltError as e:
            result_box["convergence_error"] = e
        except (Exception, KeyboardInterrupt) as e:
            result_box["exception"] = e
        except BaseException as e:
            cancelled = PollCancellationError(f"poll worker exited: {type(e).__name__}: {e}")
            cancelled.__cause__ = e
            result_box["exception"] = cancelled

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


@dataclass(frozen=True)
class ShutdownReport:
    """What :func:`shutdown_run` proved and what it left for a human."""

    tick_id: str | None
    kanban_quiescent: bool
    remote_all_ok: bool
    leftovers: tuple[str, ...]
    branch_deletion_skipped: bool


def _archived_status_map(tenant: str, tick_id: str) -> dict[str, str] | None:
    """``{phase_key: status}`` for *tick_id* from the archived-inclusive snapshot, or ``None``.

    Reads the same ``hermes kanban list --tenant <t> --archived --json`` snapshot that
    :func:`kanban_tasks.cancel_todo_kanban_tasks` verifies against, so archived cards
    (the state cancel leaves them in) are visible; :func:`get_todo_kanban_status` omits
    them and would report a fully archived tick as an empty (vacuously terminal) map.
    ``None`` means the snapshot could not be read and proves nothing.
    """
    from .kanban_tasks import TERMINAL_STATUSES, _list_task_snapshot, _parse_task_header

    snapshot = _list_task_snapshot(tenant)
    if snapshot is None:
        return None
    status_map: dict[str, str] = {}
    for task in snapshot:
        header = _parse_task_header(task)
        if header is None or header.get("tick_id") != tick_id:
            continue
        phase_key = header.get("phase_key")
        if not isinstance(phase_key, str):
            continue
        status = task.get("status")
        if not isinstance(status, str):
            # Mirrors get_todo_kanban_status: a card whose status cannot be read
            # is still this tick's card and must block quiescence, never vanish.
            status = "unknown"
        # A duplicate card for one phase folds to the worst status so a live
        # duplicate can never be shadowed by an archived one (order-independent).
        previous = status_map.get(phase_key)
        if previous is None or (previous in TERMINAL_STATUSES and status not in TERMINAL_STATUSES):
            status_map[phase_key] = status
    return status_map


def _wait_for_kanban_quiescence(
    project_slug: str,
    tick_id: str,
    *,
    expected_phase_keys: tuple[str, ...] | None,
    timeout: float,
    poll_interval: float,
    sleep: Callable[[float], None],
    now: Callable[[], float],
    log: logging.Logger,
) -> bool:
    """Poll the archived-inclusive snapshot until the tick is provably terminal or *timeout* elapses.

    Quiescent iff the tick's cards are non-empty, every *expected_phase_keys* entry is
    present (when known), and every status is in ``TERMINAL_STATUSES``. An empty map,
    an unreadable snapshot, or a raising query proves nothing and counts as
    "not yet"; polling continues until the deadline. Iterations are additionally
    capped at ``ceil(timeout / poll_interval) + 1`` so a stalled clock cannot spin.
    """
    from .kanban_tasks import TERMINAL_STATUSES

    deadline = now() + timeout
    max_iterations = math.ceil(timeout / poll_interval) + 1
    for _ in range(max_iterations):
        try:
            status_map = _archived_status_map(project_slug, tick_id)
        except Exception as exc:
            log.warning(
                "harness shutdown: kanban snapshot failed for tick %s (%s: %s); treating as not quiescent",
                tick_id, type(exc).__name__, exc,
            )
            status_map = None
        if status_map is None:
            log.warning("harness shutdown: kanban snapshot unreadable for tick %s; not yet quiescent", tick_id)
        elif not status_map:
            log.info("harness shutdown: no cards visible yet for tick %s; not yet quiescent", tick_id)
        elif expected_phase_keys is not None and not set(expected_phase_keys) <= status_map.keys():
            log.info(
                "harness shutdown: tick %s missing cards for %s; not yet quiescent",
                tick_id, sorted(set(expected_phase_keys) - status_map.keys()),
            )
        elif all(status in TERMINAL_STATUSES for status in status_map.values()):
            return True
        remaining = deadline - now()
        if remaining <= 0:
            return False
        sleep(min(poll_interval, remaining))
    return False


def _close_issue_leftover(project_dir: Path, sandbox: SandboxRepo, number: int, *, log: logging.Logger) -> list[str]:
    """Close the run's issue on a path where :func:`cleanup_remote` will not run."""
    from .github_issues import GitHubIssuesError, close_issue

    log.info("harness shutdown: closing issue #%d in %s", number, sandbox.repo)
    try:
        close_issue(project_dir, number, repo=sandbox.repo)
    except (GitHubIssuesError, ValueError) as exc:
        code = getattr(exc, "code", "invalid")
        return [f"issue #{number}: close failed ({code}); run: gh issue close {number} --repo {sandbox.repo}"]
    return []


def shutdown_run(
    project_dir: Path,
    sandbox: SandboxRepo,
    *,
    issue: HarnessIssue,
    baseline: RunBaseline,
    plan_sha: str,
    tick_id: str | None,
    expected_phase_keys: tuple[str, ...] | None,
    provenance_dir: Path,
    staging_root: Path,
    quiescence_timeout: float = _SHUTDOWN_TIMEOUT,
    poll_interval: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    keep_remote: bool = False,
    assume_workers_may_exist: bool = False,
    log: logging.Logger = log,
) -> ShutdownReport:
    """Fail-closed shutdown for a live run once its sandbox issue exists.

    *tick_id* and *expected_phase_keys* are what the caller learned from
    :func:`recover_tick_registration` (never re-derived from disk here): on success
    pass ``registration.tick_id`` and ``registration.phase_keys``. When it raised
    ``HarnessTickError`` with a non-``None`` ``exc.tick_id`` (``tick_not_started``,
    ``failed_to_spawn``, ``expected_phases_missing``, or ``unexpected_registration``
    after registration), cards may exist for that tick: pass ``exc.tick_id`` with
    ``expected_phase_keys=None``. ``None`` for both is only correct for
    ``tick_not_persisted`` and ``picked_none``, where no card can exist.
    ``expected_phase_keys=None`` forfeits the completeness check (quiescence then
    needs only a non-empty, fully terminal set of cards) and is meant solely for
    those partial-registration callers. After ``tick_timeout`` (or any failure
    escaping :func:`run_tick`) the caller re-reads ``current_tick_id.txt``: a new id
    is passed like ``exc.tick_id`` above; when none is readable pass ``tick_id=None``
    with ``assume_workers_may_exist=True``, which closes the issue only (nothing
    remote is deleted) and reports ``kanban_quiescent=False`` with a leftover
    pointer, since a worker may have spawned under an unknown tick. Order:
    cancel the tick's kanban tasks, wait for proven quiescence on the
    archived-inclusive snapshot, then (only when proven) discover and delete remote
    artifacts. Destructive branch/PR cleanup never runs while an agent may still be
    pushing: on an unconfirmed cancel, a quiescence timeout, or any discovery/cleanup
    failure, only the issue is closed and everything else is reported as a leftover.
    With no *tick_id* no agent ever ran, so there is nothing to discover or delete:
    only the issue is closed.

    Quiescence accepts every ``TERMINAL_STATUSES`` member, not only ``archived``:
    after a confirmed cancel a card that reached ``done``/``failed`` on its own has
    no worker behind it either, and the confirmed cancel already proved no run is
    still executing.

    ``keep_remote`` still cancels and quiesces the kanban (workers must stop) but
    performs no remote operation at all, not even closing the issue; then
    ``remote_all_ok`` reflects kanban quiescence only, and the ``kept remote
    artifacts ...`` leftover is always emitted so the operator has the pointer.

    Never raises for operational failures. Any ``BaseException`` that is not an
    ``Exception`` (``KeyboardInterrupt``, ``SystemExit``) is logged at ERROR with
    the manual pointers and re-raised.
    """
    if not math.isfinite(poll_interval) or poll_interval <= 0:
        raise ValueError(f"poll_interval must be finite and > 0, got {poll_interval!r}")
    if not math.isfinite(quiescence_timeout) or quiescence_timeout < 0:
        raise ValueError(f"quiescence_timeout must be finite and >= 0, got {quiescence_timeout!r}")

    pointer = f"issue #{issue.number} in {sandbox.repo}, run {issue.run_token}"
    inspect_hint = f"hermes kanban list --tenant {sandbox.slug} --archived"
    remote_hint = f"gh pr list --repo {sandbox.repo}; git ls-remote --heads -- {_scrub_url(sandbox.url)}"
    kept_leftover = f"kept remote artifacts for issue #{issue.number} in {sandbox.repo} (run {issue.run_token})"

    if tick_id is None and assume_workers_may_exist:
        unknown = (
            f"tick id unknown after a tick failure ({pointer}); workers may exist; branch/PR cleanup"
            f" skipped; inspect: {inspect_hint}; {remote_hint}"
        )
        log.warning("harness shutdown: %s", unknown)
        if keep_remote:
            return ShutdownReport(
                tick_id=None, kanban_quiescent=False, remote_all_ok=False,
                leftovers=(kept_leftover, unknown), branch_deletion_skipped=True,
            )
        leftovers = _close_issue_leftover(project_dir, sandbox, issue.number, log=log)
        return ShutdownReport(
            tick_id=None, kanban_quiescent=False, remote_all_ok=False,
            leftovers=(*leftovers, unknown), branch_deletion_skipped=True,
        )

    if tick_id is None and keep_remote:
        log.info("harness shutdown: keep_remote and no tick registered; nothing touched (%s)", pointer)
        return ShutdownReport(
            tick_id=None, kanban_quiescent=True, remote_all_ok=True,
            leftovers=(kept_leftover,), branch_deletion_skipped=True,
        )

    if tick_id is None:
        # No kanban registration ever happened, so no worker exists and no remote
        # artifact beyond the issue can exist; nothing to discover or delete.
        log.info("harness shutdown: no tick registered; only the issue is closed (%s)", pointer)
        leftovers = _close_issue_leftover(project_dir, sandbox, issue.number, log=log)
        return ShutdownReport(
            tick_id=None, kanban_quiescent=True, remote_all_ok=not leftovers,
            leftovers=tuple(leftovers), branch_deletion_skipped=True,
        )

    try:
        log.info("harness shutdown: cancelling kanban tasks for tick %s (%s)", tick_id, pointer)
        confirmed = _cancel_registered_tasks(project_slug=sandbox.slug, tick_id=tick_id, project_dir=project_dir)
        if confirmed:
            log.info("harness shutdown: waiting up to %.0fs for tick %s to go quiescent", quiescence_timeout, tick_id)
            all_terminal = _wait_for_kanban_quiescence(
                sandbox.slug, tick_id, expected_phase_keys=expected_phase_keys,
                timeout=quiescence_timeout, poll_interval=poll_interval, sleep=sleep, now=now, log=log,
            )
        else:
            log.info("harness shutdown: cancel of tick %s not confirmed; skipping quiescence wait", tick_id)
            all_terminal = False
    except BaseException:
        log.error(
            "harness shutdown interrupted while quiescing tick %s (%s); inspect: %s; %s;"
            " then: gh issue close %d --repo %s",
            tick_id, pointer, inspect_hint, remote_hint, issue.number, sandbox.repo,
        )
        raise
    kanban_quiescent = confirmed and all_terminal
    log.info(
        "harness shutdown: tick %s cancel_confirmed=%s all_terminal=%s", tick_id, confirmed, all_terminal
    )

    if keep_remote:
        log.info("harness shutdown: keep_remote; leaving %s, its PRs and branches in place", pointer)
        return ShutdownReport(
            tick_id=tick_id, kanban_quiescent=kanban_quiescent, remote_all_ok=kanban_quiescent,
            leftovers=(kept_leftover,), branch_deletion_skipped=True,
        )

    if not kanban_quiescent:
        leftovers = _close_issue_leftover(project_dir, sandbox, issue.number, log=log)
        leftovers.append(
            f"kanban not quiescent for tick {tick_id} ({pointer}); branch/PR cleanup skipped;"
            f" inspect: {inspect_hint}; {remote_hint}"
        )
        log.warning(
            "harness shutdown: kanban not quiescent for tick %s; branch/PR cleanup skipped, live remote"
            " artifacts may remain (%s)", tick_id, pointer,
        )
        return ShutdownReport(
            tick_id=tick_id, kanban_quiescent=False, remote_all_ok=False,
            leftovers=tuple(leftovers), branch_deletion_skipped=True,
        )

    log.info("harness shutdown: discovering remote artifacts (%s)", pointer)
    try:
        artifacts = discover_remote_artifacts(
            project_dir, sandbox, issue=issue, baseline=baseline, plan_sha=plan_sha, provenance_dir=provenance_dir,
        )
        log.info("harness shutdown: cleaning up remote artifacts (%s)", pointer)
        all_ok, leftovers = cleanup_remote(project_dir, sandbox, artifacts, staging_root=staging_root, log=log)
    except Exception as exc:
        # Nothing was deleted: discovery is read-only and cleanup_remote raises only
        # before touching the remote; any other failure is reported, not propagated.
        code = getattr(exc, "code", None)
        detail = getattr(exc, "detail", None)
        reason = f"{code}: {detail}" if code and detail else f"{type(exc).__name__}: {exc}"
        reason = reason[:_ERROR_MESSAGE_MAX]
        log.warning(
            "harness shutdown: remote cleanup aborted (%s); closing issue only, live remote artifacts"
            " may remain (%s)", reason, pointer,
        )
        issue_leftovers = _close_issue_leftover(project_dir, sandbox, issue.number, log=log)
        return ShutdownReport(
            tick_id=tick_id, kanban_quiescent=True, remote_all_ok=False,
            leftovers=tuple((*issue_leftovers, f"{reason}; branch/PR cleanup skipped; inspect: {remote_hint}")),
            branch_deletion_skipped=True,
        )
    except BaseException:
        log.error(
            "harness shutdown interrupted during remote cleanup (%s); inspect: %s; then: gh issue close %d --repo %s",
            pointer, remote_hint, issue.number, sandbox.repo,
        )
        raise

    log.info("harness shutdown: remote cleanup all_ok=%s leftovers=%d (%s)", all_ok, len(leftovers), pointer)
    return ShutdownReport(
        tick_id=tick_id, kanban_quiescent=True, remote_all_ok=all_ok,
        leftovers=tuple(leftovers), branch_deletion_skipped=False,
    )


_HARNESS_FIXTURE = "happy-path"


def _harness_tmp_root() -> Path:
    """Directory harness workspaces are allocated under (seam for hermetic tests).

    ``~/.hermes/tmp`` rather than the OS default temp root: on macOS,
    ``tempfile.mkdtemp()`` resolves under ``/var/folders/...``, a symlink to
    ``/private/var/folders/...`` — a prefix the Hermes agent's write-tool
    sensitive-path guard blocks, causing every worker in ``--kanban hermes`` mode
    to crash-loop on writes inside the project clone.
    """
    return Path("~/.hermes/tmp").expanduser()


def run_harness(
    *,
    fixture_name: str,
    repo: str | None,
    loop: bool,
    keep_dir: bool,
    timeout: int,
    convergence_threshold: int,
    config: Any = None,
    profile_name: str = "gstack",
) -> HarnessResult:
    """Main orchestration: drive one production tick against a live GitHub sandbox.

    Preflight (profile, tools, gh, kanban), clone and verify the sandbox, create
    the run's issue and plan commit, run ``tpo tick`` and poll its registered
    cards, verify the pull request, then shut down (kanban quiescence, remote
    cleanup) and report. Once the issue exists :func:`shutdown_run` always runs,
    even when a later step raised (the original exception is re-raised
    afterwards) — except on an interrupt (``KeyboardInterrupt``/``SystemExit``),
    when no remote operation is started: the workspace is retained and the
    recovery pointers are logged. A ``HarnessTickError`` is not raised out of
    this function: it becomes exit code 1 with its code in the summary.
    """
    import threading

    from .contract import PROFILE_NAME_RE
    from .kanban_tasks import TERMINAL_STATUSES, get_todo_kanban_status
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
    all_phases = load_phases(profile_path)
    prerequisites = load_profile_prerequisites(profile_name)
    unverified = unverified_prerequisite_ids(prerequisites, prompt_client)
    if unverified:
        raise HarnessProfileError(
            "unverified_prerequisites",
            profile_name,
            ", ".join(unverified),
        )
    validate_live_profile(all_phases, profile_name)
    if fixture_name != _HARNESS_FIXTURE:
        raise HarnessPreflightError("unknown_fixture", fixture_name)
    _reject_gh_override()

    sandbox = resolve_sandbox_repo(repo)
    run_token = _run_token()

    harness_tmp_root = _harness_tmp_root()
    harness_tmp_root.mkdir(parents=True, exist_ok=True)
    workspace_dir = Path(tempfile.mkdtemp(prefix="harness-", dir=harness_tmp_root))
    projects_dir = workspace_dir / "projects"
    project_dir = projects_dir / sandbox.slug
    project_state = project_dir / ".hermes"
    state_dir = workspace_dir / "state"
    artifacts_dir = workspace_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    provenance_dir = artifacts_dir / "provenance"
    staging_root = artifacts_dir / "staging"
    tick_log = artifacts_dir / "tick.log"
    events_log = artifacts_dir / "events.jsonl"
    base_monitor = HarnessMonitor(events_log)
    detector = ConvergenceDetector(threshold=convergence_threshold)
    monitor = _ConvergenceMonitor(base_monitor, detector, {})

    workspace_quiescent = True
    cleanup_incomplete = False
    try:
        preflight_check(
            prompt_client=prompt_client,
            profile_name=profile_name,
            prerequisites=prerequisites,
        )
        gh_pre = github_preflight(workspace_dir, sandbox)
        _kanban_preflight(tenant=sandbox.slug)

        clone_sandbox(sandbox, project_dir, branch=gh_pre.default_branch or None)
        sandbox_seed_check(project_dir, sandbox)
        baseline = take_baseline(
            project_dir, sandbox, viewer=gh_pre.viewer, default_branch=gh_pre.default_branch
        )
        write_project_contract(project_dir, profile_name)
        try:
            issue = create_harness_issue(project_dir, sandbox, run_token=run_token, baseline=baseline)
        except HarnessRemoteCleanupError:
            # The issue may exist remotely but could not be reconciled: keep the
            # workspace as the only record of the run token for manual cleanup.
            cleanup_incomplete = True
            raise
        # From here on the sandbox carries this run's issue: shutdown_run is owed.

        pointer = (
            f"issue #{issue.number} in {sandbox.repo} (run {run_token}); workspace retained at {workspace_dir};"
            f" inspect: hermes kanban list --tenant {sandbox.slug} --archived; gh pr list --repo {sandbox.repo};"
            f" git ls-remote --heads -- {_scrub_url(sandbox.url)}; then: gh issue close {issue.number}"
            f" --repo {sandbox.repo}"
        )
        plan_sha: str | None = None
        registration: TickRegistration | None = None
        tick_error: HarnessTickError | None = None
        workers_unaccounted = False
        failure_code: str | None = None
        success = False
        timed_out = False
        poll_cancelled = False
        pr: PullRequest | None = None
        pending: Exception | None = None
        try:
            plan_sha = commit_plan(project_dir, issue)
            with isolate_config(
                state_dir=state_dir, projects_dir=projects_dir, prompt_client=prompt_client
            ):
                # Emit initial event so the events log file exists for report generation
                base_monitor(
                    "run_started",
                    {
                        "tick_id": None,
                        "kanban_mode": "hermes",
                        "profile": profile_name,
                        "fixture_name": fixture_name,
                        "prompt_client": prompt_client,
                        "repo": sandbox.repo,
                        "issue_number": issue.number,
                        "run_token": run_token,
                    },
                )
                # Visibility barrier: GitHub's label-filtered listing lags the create, and the
                # production tick compiles its candidates from that listing. Wait until exactly
                # our issue is ready before running the tick (also the quiescence re-check).
                wait_for_issue_visible(project_dir, sandbox, issue_number=issue.number)

                try:
                    previous_tick_id = read_current_tick_id(project_state)
                    try:
                        run_tick(sandbox.slug, cwd=workspace_dir, log_path=tick_log, timeout=timeout)
                    except Exception as exc:
                        # The subprocess may have persisted a tick and spawned workers
                        # before failing: re-read the id so shutdown can quiesce it.
                        if not isinstance(exc, HarnessTickError):
                            exc = HarnessTickError("tick_failed", f"{type(exc).__name__}: {exc}"[:_ERROR_MESSAGE_MAX])
                        current_tick_id = read_current_tick_id(project_state)
                        if current_tick_id is not None and current_tick_id != previous_tick_id:
                            exc.tick_id = current_tick_id
                        else:
                            workers_unaccounted = True
                        raise exc
                    registration = recover_tick_registration(
                        project_state,
                        expected_issue=issue.number,
                        tick_log=tick_log,
                        previous_tick_id=previous_tick_id,
                    )
                    cards = cards_for_registered_keys(all_phases, registration.phase_keys)
                except HarnessTickError as exc:
                    tick_error = exc
                    failure_code = exc.code
                    if exc.code == "tick_not_persisted":
                        # The detail is the tick log tail and may carry sensitive tool output.
                        log.error(
                            "harness: tick did not register a runnable run: %s; see %s",
                            exc.code, tick_log,
                        )
                        log.debug("harness: tick log tail:\n%s", exc.detail)
                    else:
                        log.error("harness: tick did not register a runnable run: %s %s", exc.code, exc.detail)

                if tick_error is None:
                    assert registration is not None
                    base_monitor(
                        "tick_registered",
                        {"tick_id": registration.tick_id, "phase_keys": list(registration.phase_keys)},
                    )
                    cancel_event = threading.Event()

                    def _poll() -> bool:
                        return poll_registered_phases(
                            project_slug=sandbox.slug,
                            tick_id=registration.tick_id,
                            state_dir=project_state,
                            todo_id=registration.todo_id,
                            cards=cards,
                            monitor=monitor,
                            detector=detector,
                            cancel_event=cancel_event,
                        )

                    success, timed_out, result_box = _run_with_timeout(
                        _poll, timeout=timeout, cancel_event=cancel_event
                    )
                    if timed_out:
                        timed_out_phase: str | None = None
                        try:
                            in_flight = get_todo_kanban_status(sandbox.slug, registration.tick_id)
                            timed_out_phase = next(
                                (k for k, v in in_flight.items() if v not in TERMINAL_STATUSES),
                                None,
                            )
                        except Exception:
                            pass
                        timed_out_phase = timed_out_phase or monitor.current_phase_key
                        if timed_out_phase:
                            base_monitor("phase_timed_out", {"phase_key": timed_out_phase})
                    elif "convergence_error" in result_box:
                        # Convergence-halt fired during polling; the cards are already
                        # terminal on the board. Name it in the summary.
                        log.warning("convergence-halt: %s", result_box["convergence_error"])
                        failure_code = "convergence_halt"

                    if success and not timed_out:
                        try:
                            artifacts = discover_remote_artifacts(
                                project_dir, sandbox, issue=issue, baseline=baseline,
                                plan_sha=plan_sha, provenance_dir=provenance_dir,
                            )
                            pr = verify_pull_request(artifacts, default_branch=baseline.default_branch)
                        except PullRequestInvariantError as exc:
                            base_monitor(*pr_invariant_event(exc))
                            failure_code = exc.code
                        except HarnessRemoteCleanupError as exc:
                            log.error("harness: PR discovery failed: %s %s", exc.code, exc.detail)
                            failure_code = exc.code
        except PollCancellationError as exc:
            poll_cancelled = True
            pending = exc
        except Exception as exc:
            pending = exc
        except BaseException:
            # Interrupt (KeyboardInterrupt/SystemExit): no remote operation is safe to
            # start now; retain the workspace and hand the operator the pointers.
            workspace_quiescent = False
            log.error("harness interrupted after %s", pointer)
            raise

        # Shutdown always runs once the issue exists, whatever happened above. The
        # workspace is presumed live until the report proves kanban quiescence.
        workspace_quiescent = False
        shutdown_tick_id = registration.tick_id if registration is not None else None
        shutdown_keys: tuple[str, ...] | None = (
            registration.phase_keys if registration is not None else None
        )
        if tick_error is not None:
            shutdown_tick_id = tick_error.tick_id or shutdown_tick_id
            shutdown_keys = None
        try:
            report = shutdown_run(
                project_dir,
                sandbox,
                issue=issue,
                baseline=baseline,
                plan_sha=plan_sha or "",
                tick_id=shutdown_tick_id,
                expected_phase_keys=shutdown_keys,
                provenance_dir=provenance_dir,
                staging_root=staging_root,
                keep_remote=keep_dir,
                assume_workers_may_exist=workers_unaccounted and shutdown_tick_id is None,
            )
        except Exception as exc:
            log.error("harness shutdown failed (%s: %s) after %s", type(exc).__name__, exc, pointer)
            if pending is not None:
                raise exc from pending
            raise
        except BaseException as exc:
            log.error("harness interrupted during shutdown after %s", pointer)
            if pending is not None:
                raise exc from pending
            raise
        leftovers = report.leftovers
        leftover_text = "; ".join(leftovers) or "none"
        pr_numbers = (pr.number,) if pr is not None else ()
        if events_log.exists():
            base_monitor(
                "run_finished",
                {
                    "issue_number": issue.number,
                    "pr_numbers": list(pr_numbers),
                    "kanban_quiescent": report.kanban_quiescent,
                    "remote_all_ok": report.remote_all_ok,
                    "leftovers": list(leftovers),
                },
            )
        workspace_quiescent = report.kanban_quiescent and not poll_cancelled
        if not report.remote_all_ok:
            cleanup_incomplete = True

        if pending is not None:
            if isinstance(pending, PollCancellationError):
                raise HarnessCleanupError(
                    f"{pending}; workspace retained at {workspace_dir}; shutdown leftovers: {leftover_text}"
                ) from pending
            pending.add_note(f"harness shutdown leftovers: {leftover_text}; workspace at {workspace_dir}")
            raise pending
        if not report.kanban_quiescent:
            tick_label = f"tick {shutdown_tick_id}" if shutdown_tick_id is not None else "an unknown tick"
            raise HarnessCleanupError(
                f"Hermes task or worker termination could not be confirmed for {tick_label};"
                f" workspace retained at {workspace_dir}; {leftover_text}"
            )
        if not report.remote_all_ok:
            raise HarnessRemoteCleanupError(
                "cleanup_incomplete", f"{leftover_text}; workspace retained at {workspace_dir}"
            )

        output_dir = artifacts_dir / "reports"
        generate_report(events_log, output_dir)
        report_json = output_dir / "report.json"
        summary = summarize_report(report_json)
        if timed_out:
            summary = f"[overall timeout after {timeout}s] " + summary
        if failure_code is not None:
            summary = f"[{failure_code}] " + summary

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

        # Cards are archived by shutdown, so read the archived-inclusive snapshot
        # (get_todo_kanban_status omits archived cards and would print ``{}``).
        status_map: dict[str, str] = {}
        if shutdown_tick_id is not None:
            try:
                status_map = _archived_status_map(sandbox.slug, shutdown_tick_id) or {}
            except Exception as exc:
                log.warning("kanban status summary unavailable: %s", exc)
        pr_display = f"#{pr.number}" if pr is not None else "none"
        print(
            f"[kanban] tenant={sandbox.slug} tick_id={shutdown_tick_id} "
            f"profile={profile_name} "
            f"repo={sandbox.repo} issue=#{issue.number} pr={pr_display} "
            f"phases={status_map} "
            f"report={report_json} keep={'yes' if keep_dir else 'no (temp dir will be removed)'}"
        )

        if keep_dir and not timed_out:
            _prune_retained_state(project_state, state_dir)

        exit_code = 0 if (success and not timed_out and failure_code is None) else 1

        return HarnessResult(
            exit_code=exit_code,
            report_path=report_json,
            temp_dir=workspace_dir if keep_dir else None,
            summary=summary,
            issue_number=issue.number,
            pr_numbers=pr_numbers,
            cleanup_leftovers=leftovers,
        )

    finally:
        if not keep_dir and workspace_quiescent and not cleanup_incomplete:
            shutil.rmtree(workspace_dir, ignore_errors=True)
