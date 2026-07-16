"""
Lane E: PipelineRunner with phase-loop orchestration and branch naming.

Implements:
  TE.1: decide_branch_name with scan+max+1 collision avoidance (T14)
  TE.2: PipelineRunner class with phase loop and kanban wiring
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from hermes_pipeline.phases import Phase
    from hermes_pipeline.state import State
    from hermes_pipeline.kanban import KanbanClient

log = logging.getLogger(__name__)

_ATTEMPT_RE = re.compile(r"-attempt(\d+)$")


def _scan_existing_attempt_numbers(project_dir: Path, prefix: str) -> list[int]:
    """
    Scan git branches matching {prefix}-attempt* and extract attempt numbers.

    Args:
        project_dir: Project directory (passed to git cwd).
        prefix: Branch prefix to scan (e.g., "feat/0.1.0-cool").

    Returns:
        Sorted list of attempt numbers found, or empty list if git fails.
    """
    pattern = f"{prefix}-attempt*"
    try:
        r = subprocess.run(
            ["git", "branch", "--list", pattern],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as e:
        log.warning("git branch --list failed: %s", e)
        return []

    if r.returncode != 0:
        return []

    out: list[int] = []
    for line in (r.stdout or "").splitlines():
        # Remove git branch markers (*, spaces)
        name = line.replace("*", "").strip()
        m = _ATTEMPT_RE.search(name)
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


def decide_branch_name(
    *,
    project_dir: Path,
    base_version: str,
    slug: str,
    is_new_attempt: bool,
    prior_attempt_branch_existed: bool,
) -> str:
    r"""
    Decide the feature branch name per design doc §runner.py branch naming (T14).

    **Logic:**
    - First attempt (is_new_attempt=False): return f"feat/{base_version}-{slug}"
    - Checkpoint resume (is_new_attempt=False, prior existed): reuse same base name
    - New attempt after rejection (is_new_attempt=True, prior existed):
      Scan for existing `feat/{base}-{slug}-attemptN` branches using `git branch --list`.
      Extract attempt numbers with regex `-attempt(\d+)$` and return
      f"feat/{base}-{slug}-attempt{max(existing)+1}" or "-attempt2" if no -attemptN exist.

    Args:
        project_dir: Project directory for git branch scanning.
        base_version: Base version string (e.g., "0.1.0").
        slug: TODO slug derived from title.
        is_new_attempt: True if this is a new attempt after rejection/abandon.
        prior_attempt_branch_existed: True if a prior branch was ever created for this TODO.

    Returns:
        Feature branch name (e.g., "feat/0.1.0-cool" or "feat/0.1.0-cool-attempt2").
    """
    base = f"feat/{base_version}-{slug}"

    # First attempt or checkpoint resume: just return the base name
    if not is_new_attempt:
        return base

    # New attempt: check if there's a prior branch to increment from
    if not prior_attempt_branch_existed:
        # No prior branch, so this is the first attempt (shouldn't happen often, but handle it)
        return base

    # Scan for existing -attemptN branches
    existing = _scan_existing_attempt_numbers(project_dir, base)
    next_n = max(existing) + 1 if existing else 2

    return f"{base}-attempt{next_n}"


@dataclass
class PipelineRunner:
    """
    Orchestrates a TODO pipeline run through phases with kanban wiring.

    Attributes:
        project: Project name/slug.
        project_dir: Path to project directory.
        branch: Feature branch name (set by decide_branch_name).
        todo_id: Stable TODO ID.
        title: TODO title.
        phases: List of Phase objects to execute in order.
        state: State object for checkpoint and ready_for_review persistence.
        kanban: KanbanClient for kanban board updates.
        run_phase_fn: Callable that runs a phase and returns return code (0=success).
        pr_url_resolver: Callable that returns PR URL (injected for testing).
    """

    project: str
    project_dir: Path
    branch: str
    todo_id: int
    title: str
    phases: list[Phase]
    state: State
    kanban: KanbanClient
    run_phase_fn: Callable[[Phase], int]
    tick_id: str = ""
    pr_url_resolver: Callable[[], str] = lambda: ""
    continue_on_failure: bool = False
    monitor: Callable | None = None
    kanban_metadata: dict[str, str] | None = None

    def run(self) -> bool:
        """
        Execute the phase loop with kanban wiring.

        **Flow:**
        1. Call kanban.set_active_task(project, todo_id, title, phase=first_phase.name)
           (failure is best-effort; don't block).
        2. Loop through phases:
           - kanban.update_phase(project, phase=phase.name, status="running")
           - rc = run_phase_fn(phase)
           - If rc != 0:
             * kanban.update_phase(..., status="failed") + log error + return False
           - If rc == 0:
             * state.mark_phase_done(todo_id, phase_key, phase_index)
             * kanban.update_phase(..., status="done")
        3. After loop completes (all phases succeeded):
           - pr_url = pr_url_resolver()
           - state.write_ready_for_review_min(todo_id, branch, pr_url, kanban_task_id=...)
           - kanban.update_phase(..., phase="Phase 8: Finish Branch", status="ready_for_review")
           - Return True
           - KEEP THE LOCK HELD — Phase 9 (merge) will unlock.

        Returns:
            True if all phases succeeded, False otherwise.
        """
        if not self.phases:
            log.error("No phases to execute")
            return False

        # Step 1: Set active task on kanban (best-effort)
        first_phase = self.phases[0]
        try:
            self.kanban.set_active_task(
                project=self.project,
                todo_id=self.todo_id,
                title=self.title,
                phase=first_phase.name,
                metadata=self.kanban_metadata,
            )
        except Exception as e:
            log.warning("kanban.set_active_task failed (non-blocking): %s", e)

        # Step 2: Loop through phases
        import time as _time
        had_failures = False

        for phase_index, phase in enumerate(self.phases):
            log.info(
                "Running phase %d/%d: %s (key=%s)",
                phase_index + 1,
                len(self.phases),
                phase.name,
                phase.phase_key,
            )

            # Monitor: phase started
            if self.monitor:
                self.monitor("phase_started", {"phase_key": phase.phase_key, "todo_id": self.todo_id})

            # Update kanban to "running"
            try:
                self.kanban.update_phase(
                    project=self.project,
                    phase=phase.name,
                    status="running",
                )
            except Exception as e:
                log.warning("kanban.update_phase (running) failed: %s", e)

            # Auto-approve gate phases when continue_on_failure=True
            if phase.gate and self.continue_on_failure:
                log.info("gate %s auto-approved (continue_on_failure mode)", phase.phase_key)
                if self.monitor:
                    self.monitor("phase_completed", {"phase_key": phase.phase_key, "todo_id": self.todo_id, "duration_ms": 0})
                try:
                    self.state.mark_phase_done(self.todo_id, phase.phase_key, phase_index)
                except Exception as e:
                    log.warning("state.mark_phase_done failed: %s", e)
                try:
                    self.kanban.update_phase(
                        project=self.project,
                        phase=phase.name,
                        status="done",
                    )
                except Exception as e:
                    log.warning("kanban.update_phase (done) failed: %s", e)
                continue

            # Run the phase with timing
            phase_start = _time.time()
            rc = self.run_phase_fn(phase)
            duration_ms = int((_time.time() - phase_start) * 1000)

            if rc != 0:
                # Phase failed
                log.error(
                    "Phase %s failed with return code %d",
                    phase.name,
                    rc,
                )
                if self.monitor:
                    self.monitor("phase_failed", {"phase_key": phase.phase_key, "todo_id": self.todo_id, "duration_ms": duration_ms, "return_code": rc})
                try:
                    self.kanban.update_phase(
                        project=self.project,
                        phase=phase.name,
                        status="failed",
                    )
                except Exception as e:
                    log.warning("kanban.update_phase (failed) failed: %s", e)
                if not self.continue_on_failure:
                    try:
                        self.kanban.clear_active_task(project=self.project, outcome="abandoned")
                    except Exception as e:
                        log.warning("kanban.clear_active_task failed: %s", e)
                    return False
                had_failures = True
                continue

            # Phase succeeded
            if self.monitor:
                self.monitor("phase_completed", {"phase_key": phase.phase_key, "todo_id": self.todo_id, "duration_ms": duration_ms})
            try:
                self.state.mark_phase_done(self.todo_id, phase.phase_key, phase_index)
            except Exception as e:
                log.warning("state.mark_phase_done failed: %s", e)

            try:
                self.kanban.update_phase(
                    project=self.project,
                    phase=phase.name,
                    status="done",
                )
            except Exception as e:
                log.warning("kanban.update_phase (done) failed: %s", e)

        # Check if any phase failed during continue_on_failure run
        if had_failures:
            log.warning("Pipeline completed with phase failures (continue_on_failure)")
            try:
                self.kanban.clear_active_task(project=self.project, outcome="abandoned")
            except Exception as e:
                log.warning("kanban.clear_active_task failed: %s", e)
            return False

        # Step 3: All phases succeeded; move to ready_for_review
        log.info("All phases completed successfully; moving to ready_for_review")

        pr_url = self.pr_url_resolver()

        try:
            self.state.write_ready_for_review_min(
                todo_id=self.todo_id,
                branch=self.branch,
                pr_url=pr_url,
                kanban_task_id=None,  # Will be set by kanban adapter if available
            )
        except Exception as e:
            log.error("state.write_ready_for_review_min failed: %s", e)
            return False

        try:
            self.kanban.update_phase(
                project=self.project,
                phase="Phase 8: Finish Branch",
                status="ready_for_review",
            )
        except Exception as e:
            log.warning("kanban.update_phase (ready_for_review) failed: %s", e)

        # Keep the lock held — Phase 9 (merge) will unlock
        log.info("TODO %d ready for review; lock held for Phase 9", self.todo_id)
        return True
