"""Lane F.3: CLI subcommands for auto/merge/status.

Implements:
  TF.3: cli.py — argparse subcommands: auto/merge/status (T6, T13)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

import subprocess as _cli_sp

from .config import Config
from .decision import store as _cli_dec_store
from .kanban import NullKanbanAdapter, HermesKanbanAdapter
from .logging_setup import configure as configure_logging
from .merge import run_phase9, make_default_bump_fn
from .status import collect_pending, format_table
from .watcher import auto_tick

log = logging.getLogger(__name__)


def _hermes_run_kill(job_id: str) -> int:
    """Send hermes run kill for a job."""
    try:
        r = _cli_sp.run(["hermes", "run", "kill", job_id], timeout=10, check=False)
        return r.returncode
    except (_cli_sp.TimeoutExpired, FileNotFoundError):
        return 1

def _release_tick_lock(state_dir: Path) -> None:
    """Release tick.lock if held."""
    lock_dir = state_dir / "tick.lock"
    if lock_dir.exists():
        holder = lock_dir / "holder.json"
        if holder.exists():
            holder.unlink()
        try:
            lock_dir.rmdir()
        except OSError:
            pass

def cmd_kill(*, state_dir: Path, all_: bool = False, todo: str | None = None) -> int:
    """Kill in-flight phase(s) and write killed_by_operator outcome sidecars.

    - Reads phase_started/* markers
    - Sends hermes run kill <job-id> for each
    - Writes killed_by_operator outcome sidecars
    - Deletes markers
    - Releases tick.lock if held
    """
    ps_dir = state_dir / "phase_started"
    if not ps_dir.exists():
        print("no in-flight phases")
        _release_tick_lock(state_dir)
        return 0

    targets = []
    if todo:
        p = ps_dir / f"{todo}.json"
        if p.exists():
            targets.append(p)
        else:
            print(f"no in-flight phase for {todo}")
            _release_tick_lock(state_dir)
            return 2
    elif all_:
        targets = [f for f in ps_dir.iterdir() if f.is_file() and f.suffix == ".json"]
    else:
        print("error: specify --all or --todo TODO-N")
        return 2

    for p in targets:
        data = json.loads(p.read_text())
        job_id = data.get("job_id")
        tick_id = data.get("tick_id", "")

        if job_id:
            _hermes_run_kill(job_id)

        if tick_id:
            _cli_dec_store.append_outcome(
                state_dir, tick_id,
                outcome="killed_by_operator",
                detail={"todo_id": p.stem},
            )

        p.unlink()

    # Release tick.lock if held
    _release_tick_lock(state_dir)
    return 0

def _parse_todo_id(value: str) -> int:
    """Parse todo_id argument with helpful error message."""
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"todo_id must be a number (you provided '{value}'). "
            f"Example: pipeline-watch merge myproject 123"
        )


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="pipeline-watch",
        description="Hermes pipeline orchestrator: auto-tick, merge, and status commands.",
    )
    parser.add_argument(
        "--version", action="version", version="%(prog)s 0.1.0"
    )

    subparsers = parser.add_subparsers(dest="command", help="Subcommand to execute")

    # auto: Run one tick
    auto_parser = subparsers.add_parser(
        "auto",
        help="Run one auto-tick: discover projects, detect changes, select eligible TODOs",
    )
    auto_parser.set_defaults(func=_cmd_auto)

    # merge: Phase 9 merge
    merge_parser = subparsers.add_parser(
        "merge",
        help="Execute Phase 9: merge a ready TODO to main",
    )
    merge_parser.add_argument("project", help="Project name")
    merge_parser.add_argument("todo_id", type=_parse_todo_id, help="TODO ID to merge (must be a number)")
    merge_parser.add_argument(
        "--abandon",
        action="store_true",
        help="Abandon the merge without confirmation",
    )
    merge_parser.set_defaults(func=_cmd_merge)

    # status: List pending records
    status_parser = subparsers.add_parser(
        "status",
        help="Display pending ready-for-review records",
    )
    status_parser.set_defaults(func=_cmd_status)

    # kill: Kill in-flight phases
    kill_parser = subparsers.add_parser(
        "kill",
        help="Kill in-flight phase(s)",
    )
    kill_group = kill_parser.add_mutually_exclusive_group(required=True)
    kill_group.add_argument("--all", dest="all_", action="store_true", help="Kill all in-flight phases")
    kill_group.add_argument("--todo", help="Kill a specific TODO (e.g., TODO-1)")
    kill_parser.set_defaults(func=_cmd_kill)

    return parser


def _cmd_auto(args, config: Config) -> int:
    """Handle 'auto' subcommand."""
    try:
        # Placeholder callbacks
        def on_selected(project: str, todo_id: int) -> None:
            log.info(f"Selected {project}/{todo_id} for pipeline execution")

        def notify_fn(msg: str) -> None:
            log.error(f"Notification: {msg}")

        auto_tick(
            projects_dir=config.projects_dir,
            lock_dir=config.lock_dir,
            state_dir=config.state_dir,
            on_selected=on_selected,
            notify_fn=notify_fn,
            slack_channel=config.slack_channel,
        )
        return 0
    except Exception as e:
        log.error(f"auto command failed: {e}", exc_info=True)
        return 2


def _cmd_merge(args, config: Config) -> int:
    """Handle 'merge' subcommand."""
    try:
        project = args.project
        todo_id = args.todo_id
        abandon = args.abandon

        # Find the project directory
        project_dir = config.projects_dir / project
        if not project_dir.exists():
            log.error(f"project not found: {project}")
            return 2

        # Build kanban adapter
        if config.kanban_adapter == "hermes":
            kanban = HermesKanbanAdapter()
        else:
            kanban = NullKanbanAdapter()

        # Prepare State for reading ready-for-review record
        checkpoint_dir = config.state_dir / "pipeline_checkpoints"
        ready_dir = config.state_dir / "ready_for_review"

        from .state import State
        state = State(
            project=project,
            lock_dir=config.lock_dir,
            checkpoint_dir=checkpoint_dir,
            ready_dir=ready_dir,
        )

        # Run Phase 9
        if abandon:
            # Abandon: reject without confirmation
            def confirm_fn(tid: int) -> bool:
                return False
        else:
            # Normal: use default confirmation
            from .merge import default_confirm_fn
            confirm_fn = default_confirm_fn

        bump_fn = make_default_bump_fn(project_dir)

        run_phase9(
            state=state,
            project_dir=project_dir,
            todo_id=todo_id,
            kanban=kanban,
            confirm_fn=confirm_fn,
            bump_fn=bump_fn,
        )
        return 0
    except Exception as e:
        log.error(f"merge command failed: {e}", exc_info=True)
        return 2


def _cmd_status(args, config: Config) -> int:
    """Handle 'status' subcommand."""
    try:
        rows = collect_pending(config.projects_dir, config.lock_dir)
        table = format_table(rows)
        print(table, end="")
        return 0
    except Exception as e:
        log.error(f"status command failed: {e}", exc_info=True)
        return 2

def _cmd_kill(args, config: Config) -> int:
    """Handle 'kill' subcommand."""
    return cmd_kill(
        state_dir=config.state_dir,
        all_=args.all_,
        todo=args.todo,
    )


def main(argv: Optional[list[str]] = None) -> int:
    """
    Main entry point for the CLI.

    Args:
        argv: Command-line arguments (default: sys.argv[1:]).

    Returns:
        Exit code (0 on success, 2 on error).
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # Load config
    config = Config.from_env()

    # Configure logging
    log_path = config.state_dir / config.log_file_subpath
    configure_logging(log_path, config.log_retention_days)

    # Dispatch to subcommand
    if hasattr(args, "func"):
        return args.func(args, config)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
