"""Phase 9: merge orchestration with e2e confirmation, version bump, and git merge."""

from __future__ import annotations
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Tuple

from .state import State, ReadyForReview


class MergeError(Exception):
    """Error during merge phase."""
    pass


def default_confirm_fn(todo_id: int) -> bool:
    """
    Default e2e confirmation: ask user to type TODO-<id>.

    Args:
        todo_id: The TODO ID to confirm.

    Returns:
        True if user typed the correct string, False otherwise.
    """
    prompt = f"Type TODO-{todo_id} to confirm merge: "
    user_input = input(prompt).strip()
    return user_input == f"TODO-{todo_id}"


def make_default_bump_fn(project_dir: Path | str) -> Callable[[ReadyForReview], Tuple[str, str]]:
    """
    Create a default bump function for a specific project directory.

    Args:
        project_dir: Path to project directory.

    Returns:
        A bump function that increments the patch version.
    """
    project_dir = Path(project_dir)

    def bump_fn(rec: ReadyForReview) -> Tuple[str, str]:
        # Read current version (default to 0.1.0 if not found)
        version_file = project_dir / "VERSION"
        if version_file.exists():
            current = version_file.read_text().strip()
        else:
            current = "0.1.0"

        # Parse and bump patch
        parts = current.split(".")
        if len(parts) >= 3:
            major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
            patch += 1
            new_version = f"{major}.{minor}.{patch}"
        else:
            new_version = "0.1.1"

        return new_version, f"bump to {new_version}"

    return bump_fn


def default_bump_fn(rec: ReadyForReview) -> Tuple[str, str]:
    """
    Deprecated: use make_default_bump_fn instead.
    Default semver bump: increment patch version (reads from cwd).

    Args:
        rec: ReadyForReview record.

    Returns:
        Tuple of (new_version_string, bump_label).
    """
    return make_default_bump_fn(Path.cwd())(rec)


def run_phase9(
    state: State,
    project_dir: Path | str,
    todo_id: int,
    kanban,
    confirm_fn: Optional[Callable[[int], bool]] = None,
    bump_fn: Optional[Callable[[ReadyForReview], Tuple[str, str]]] = None,
) -> None:
    """
    Execute Phase 9: e2e confirmation → semver bump → VERSION/CHANGELOG update → git merge.

    Workflow:
    1. Read ready_for_review record; raise MergeError if missing.
    2. Check merge_status ∈ ("pending", "failed"); raise if already done.
    3. Call confirm_fn(todo_id) for typed e2e confirmation (default: ask user).
    4. If !confirmed: set status="rejected" + clear kanban + unlock.
    5. If confirmed:
       - bump_fn(rec) → (new_version, bump_label)
       - Write VERSION file
       - Append CHANGELOG.md entry
       - `git merge --no-ff <branch>`
       - If merge succeeds: set status="merged" + clear kanban + unlock
       - If merge fails: set status="failed" with error message; KEEP LOCK HELD

    Args:
        state: State instance (with lock already held).
        project_dir: Path to project directory.
        todo_id: TODO ID being merged.
        kanban: Kanban adapter (must have clear_active_task(kanban_task_id) method).
        confirm_fn: E2E confirmation function (default: ask user to type TODO-<id>).
        bump_fn: Version bump function (default: increment patch version).

    Raises:
        MergeError: If preconditions fail (no record, wrong status, etc.).
    """
    if confirm_fn is None:
        confirm_fn = default_confirm_fn
    if bump_fn is None:
        bump_fn = make_default_bump_fn(project_dir)

    project_dir = Path(project_dir)

    # 1. Read ready_for_review record
    rec = state.read_ready_for_review(todo_id)
    if rec is None:
        raise MergeError(f"No ready_for_review record found for TODO {todo_id}")

    # 2. Check merge_status
    if rec.merge_status not in ("pending", "failed"):
        raise MergeError(
            f"Cannot merge TODO {todo_id}: "
            f"merge_status={rec.merge_status} (expected 'pending' or 'failed')"
        )

    # 3. Call confirm_fn
    confirmed = confirm_fn(todo_id)

    # 4. If not confirmed
    if not confirmed:
        state.set_merge_status(todo_id, "rejected")
        if rec.kanban_task_id and kanban:
            try:
                kanban.clear_active_task(rec.kanban_task_id)
            except Exception:
                pass  # Kanban sync is not critical; best-effort only
        state.unlock()
        return

    # 5. If confirmed
    # Get version bump (bump_fn reads from cwd, so we need to set cwd)
    new_version, bump_label = bump_fn(rec)

    # Write VERSION file
    version_file = project_dir / "VERSION"
    version_file.write_text(f"{new_version}\n")

    # Append CHANGELOG.md entry
    changelog_file = project_dir / "CHANGELOG.md"
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    changelog_entry = f"""
## [{new_version}] - {timestamp}
- Merged {rec.branch} (TODO-{todo_id}): {bump_label}
"""
    if changelog_file.exists():
        changelog_file.write_text(changelog_entry + "\n" + changelog_file.read_text())
    else:
        changelog_file.write_text(f"# Changelog\n{changelog_entry}")

    # git merge --no-ff <branch>
    try:
        subprocess.run(
            ["git", "merge", "--no-ff", rec.branch],
            cwd=project_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        # Merge failed: set status="failed" but keep lock held
        error_msg = f"git merge failed: {e.stderr}"
        state.set_merge_status(todo_id, "failed", error_msg)
        return

    # Merge succeeded
    state.set_merge_status(todo_id, "merged")
    if rec.kanban_task_id and kanban:
        try:
            kanban.clear_active_task(rec.kanban_task_id)
        except Exception:
            pass  # Kanban sync is not critical; best-effort only
    state.unlock()


def abandon(
    state: State,
    todo_id: int,
    kanban,
) -> None:
    """
    Mark a ready-for-review record as abandoned and clean up.

    Args:
        state: State instance (with lock already held).
        todo_id: TODO ID being abandoned.
        kanban: Kanban adapter.
    """
    rec = state.read_ready_for_review(todo_id)
    if rec is not None:
        state.set_merge_status(todo_id, "abandoned")
        if rec.kanban_task_id and kanban:
            try:
                kanban.clear_active_task(rec.kanban_task_id)
            except Exception:
                pass
    state.unlock()
