"""Internal registered-execution interface used by thin Hermes workers.

The installed launcher owns external processes; Kanban remains the authority
for card transitions. No operation accepts a shell command or agent prompt.
"""
from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from pathlib import Path

from .agent_execution import (
    TERMINAL,
    ExecutionError,
    ExecutionStore,
    LockUnconfirmed,
    _atomic_write,
    _open_directory,
    _safe_read,
    identity_matches,
    process_identity,
)
from .agent_git import check_collection_deadline, collection_deadline
from .agent_process import (
    ProcessLaunchError,
    ProcessOwnershipError,
    cleanup_processes,
    confirm_process_capability,
    run_process,
)
from .result_contract import (
    MAX_METADATA_BYTES,
    ResultContractError,
    _git,
    _git_bytes,
    _reject_unsafe_strings,
    load_validated_registration,
    manifest_acceptance_criteria,
    parse_worker_result,
    render_result_template,
    verify_optional_single_commit,
    verify_worker_git_result,
)

_FAILURE_CODES = frozenset({
    "supervisor_unavailable", "client_unavailable", "client_sandbox_unavailable",
    "client_sandbox_unconfirmed", "process_capability_unavailable", "invalid_client_tools",
    "invalid_client", "registration_invalid", "registration_drift", "execution_identity_mismatch",
    "profile_authority_root_unconfirmed", "registration_root_mismatch", "branch_drift",
    "phase_identity_mismatch", "git_metadata_drift", "git_metadata_unconfirmed",
    "git_permissions_unconfirmed", "execution_invalid", "launch_unavailable",
    "verification_sandbox_unavailable",
})

DEADLINE_COLLECTION_CAP = 600.0


def deadline_collection_budget(timeout: float) -> float:
    """Compute collection budget as 10% of timeout, capped at DEADLINE_COLLECTION_CAP.

    Rationale: pinned checks already ran once per task during the phase; 10% keeps 30s
    test phases at 3s; 600s caps the 7200s class.
    """
    return min(DEADLINE_COLLECTION_CAP, 0.1 * timeout)


def wait_ceiling_tail(registration: dict) -> float:
    """Compute the tail duration added to wait ceilings.

    When a manifest is present, collection can happen: deadline + cleanup allowance + collection budget.
    Without a manifest, only the cleanup allowance is added.
    """
    if registration["manifest"] is not None:
        return 60 + deadline_collection_budget(registration["timeout"]) + 60
    return 60


def _failure_code(error: Exception) -> str:
    if isinstance(error, LockUnconfirmed):
        return "lock_unconfirmed"
    if isinstance(error, ProcessLaunchError):
        return "process_capability_unavailable"
    # Only exact, closed vocabulary matches may cross the reporting boundary.
    if isinstance(error, ExecutionError) and error.args and isinstance(error.args[0], str) and error.args[0] in _FAILURE_CODES:
        return error.args[0]
    return "launch_unavailable" if isinstance(error, OSError) else "execution_invalid"


def _registration_digest(record: dict) -> str:
    return hashlib.sha256(json.dumps(record["registration"], sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _remember_launch_refusal(store: ExecutionStore, identity: str, error: Exception) -> None:
    """Called under both admission locks; a refusal never consumes an attempt."""
    record = store.load(identity)
    if record["attempts"] or isinstance(error, LockUnconfirmed):
        return
    receipt = {"version": 1, "execution_id": identity, "generation": 1,
               "registration_sha256": _registration_digest(record), "reason": _failure_code(error)}
    with store._directory_handle(identity) as directory:
        _atomic_write(Path("launch-refusal.json"), receipt, directory_fd=directory)


def _launch_refusal(store: ExecutionStore, identity: str, record: dict) -> str | None:
    try:
        with store._directory_handle(identity) as directory:
            raw = _safe_read(Path("launch-refusal.json"), directory_fd=directory)
    except FileNotFoundError:
        return None
    if len(raw) > 1024:
        raise ExecutionError("execution_invalid")
    receipt = json.loads(raw)
    fields = {"version", "execution_id", "generation", "registration_sha256", "reason"}
    if (not isinstance(receipt, dict) or set(receipt) != fields
            or type(receipt["version"]) is not int or receipt["version"] != 1
            or type(receipt["generation"]) is not int or receipt["generation"] < 1
            or not isinstance(receipt["execution_id"], str)
            or not isinstance(receipt["registration_sha256"], str)
            or len(receipt["registration_sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in receipt["registration_sha256"])
            or not isinstance(receipt["reason"], str) or receipt["reason"] not in _FAILURE_CODES):
        raise ExecutionError("execution_invalid")
    if (receipt["execution_id"] != identity or receipt["generation"] != 1
            or receipt["registration_sha256"] != _registration_digest(record)):
        return None
    return receipt["reason"]


def _prepare_launch(store: ExecutionStore, identity: str, record: dict) -> tuple[Path, list[str]]:
    validate_registration(store, identity)
    if not record["attempts"]:
        from .agent_checkpoint import ProgressJournal

        ProgressJournal(store, identity).validate_fresh()
    confirm_process_capability()
    staging = staging_directory(store, identity, len(record["attempts"]) + 1)
    arguments = client_argv(record["registration"], staging)
    executable = shutil.which(arguments[0])
    if executable is None:
        raise ExecutionError("client_unavailable")
    arguments[0] = executable
    # A repaired prerequisite permits the first admission; status must not keep
    # returning an older refusal while the new daemon is being started.
    if not record["attempts"]:
        with store._directory_handle(identity) as directory:
            try:
                os.unlink("launch-refusal.json", dir_fd=directory)
                os.fsync(directory)
            except FileNotFoundError:
                pass
    return staging, arguments


def installed_entrypoint() -> str:
    # Keep the launcher paired with this package's environment even when Hermes
    # supplies a different PATH. Do not resolve the interpreter's venv symlink.
    sibling = Path(sys.executable).absolute().parent / "tpo-agent-supervisor"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    executable = shutil.which("tpo-agent-supervisor")
    if executable is None:
        raise ExecutionError("supervisor_unavailable")
    return str(Path(executable).absolute())


def execution_id(tick_id: str, phase: str) -> str:
    return hashlib.sha256((tick_id + "\0" + phase).encode()).hexdigest()


def staging_directory(store: ExecutionStore, identity: str, generation: int) -> Path:
    # Validate identity through the record reader before interpolating it.
    store.load(identity)
    if type(generation) is not int or generation < 1:
        raise ExecutionError("invalid_generation")
    path = store.root.parent / "agent-submissions" / identity / str(generation)
    with _open_directory(path, create=True):
        pass
    return path


def client_argv(registration: dict, staging: Path) -> list[str]:
    from .agent_client import build_client_argv

    return build_client_argv(registration, staging)


def worker_instructions(identity: str, root: str) -> str:
    return (
        "You are the Hermes dispatcher. Invoke or reconnect to this registered execution:\n"
        + shlex.join([installed_entrypoint(), "run", "--wait", "--root", root, "--execution", identity]) + "\n"
        "Use only this installed interface. A missing supervisor blocks dispatch. "
        "Automatic worker retry reconnects to the same generation; never authorize a new attempt. "
        "The supervisor owns monitoring, deadline, cleanup and result validation. "
        "Await this command until it finishes; if the terminal tool returns a background session, "
        "keep polling that session until the command exits. Do not end the worker while it runs. "
        "Never use kanban_block for running_detached or waiting_for_admission. "
        "If its bounded wait returns either status, reconnect using the same command without changing card state. "
        "waiting_for_admission is not a terminal failure: the command owns bounded "
        "admission retries without allocating an attempt or refreshing its execution budget. "
        "For terminal outcomes, refresh this card "
        "through supported Kanban worker tools and check its registered execution and generation. "
        "Do not overwrite a completed card, a newer attempt, or an unrelated/manual block. "
        "Require HERMES_KANBAN_TASK to identify this card and a valid HERMES_KANBAN_RUN_ID "
        "before any worker transition. If either is unavailable, report and leave card state unchanged. "
        "Only when completion_allowed is true, use the kanban_complete worker tool (which binds "
        "the current worker run identity; never the unguarded CLI) and set its metadata argument to the entire returned report.metadata object: "
        'metadata={"tpo_result": <validated result>}. '
        "Keep the nested tpo_result unchanged. Never pass report.metadata.tpo_result alone as the metadata argument. "
        "Otherwise report its structured status through Kanban "
        "kanban_comment and kanban_block worker tools; retain interrupted, timed_out, cleanup_unconfirmed and lock_unconfirmed "
        "as distinct reasons. Never infer completion from zero exit or missing processes.\n"
    )


def register_execution(*, project_dir: Path, state_dir: Path, root: Path, tick_id: str,
                       phase: str, prompt: str, client: str, tools: str, worktree: Path,
                       timeout: float, todo_id: str, result_template: str | None = None,
                       phase_role: str = "worker") -> str:
    installed_entrypoint()
    from .agent_client import git_metadata_identity

    git_metadata = git_metadata_identity(worktree)
    authority_path = state_dir / "runs" / tick_id / "registration.json"
    registration = None
    if authority_path.exists():
        registration = load_validated_registration(project_dir, state_dir, tick_id)
        if registration.worktree != worktree.resolve() or registration.prompt_client != client:
            raise ExecutionError("registration_drift")
        if (root.resolve() != (project_dir / ".hermes/agent-executions").resolve()
                or state_dir.resolve() != (project_dir / ".hermes").resolve()):
            raise ExecutionError("registration_root_mismatch")
    else:
        from .agent_authority import profile_root

        if root.resolve() != profile_root(project_dir).resolve():
            raise ExecutionError("profile_authority_root_unconfirmed: use trusted conventional configuration")
    if registration is not None:
        from .phase_schedule import execution_keys
        from .phase_schedule import phase_role as registered_phase_role

        if phase not in execution_keys(registration):
            raise ExecutionError("phase_identity_mismatch")
        phase_role = registered_phase_role(registration, phase)
    branch = _git(worktree, "branch", "--show-current")
    identity = execution_id(tick_id, phase)
    store = ExecutionStore(root)
    existing_contract = None
    if (root / identity / "record.json").exists():
        existing_contract = store.load(identity)["registration"]["result_contract"]
        base = existing_contract["base_sha"]
    elif registration is not None and registration.manifest is not None and phase_role == "implementation" and not getattr(registration, "phase_definitions", ()):
        base = registration.base_sha
    else:
        base = _git(worktree, "rev-parse", "HEAD")
    criteria = manifest_acceptance_criteria(registration.manifest) if registration and registration.manifest and phase_role == "implementation" else ()
    worker_result = bool(registration and registration.manifest and phase_role in {"implementation", "review", "delivery"})
    contract = {
        "kind": "registered" if registration else "profile", "project_dir": str(project_dir.resolve()),
        "state_dir": str(state_dir.resolve()), "todo_id": todo_id, "tick_id": tick_id,
        "phase": phase, "phase_role": phase_role, "base_sha": base, "acceptance": list(criteria),
        "registration_sha256": hashlib.sha256(_safe_read(authority_path)).hexdigest() if registration else None,
        "expected_commits": len(registration.manifest.tasks) if criteria else None,
        "result_kind": "worker" if worker_result else "phase",
        "progress_version": 1,
        "git_metadata": git_metadata,
        "result_template": result_template or render_result_template(
            tick_id=tick_id, todo_id=todo_id, step_key=phase, acceptance_criteria=criteria,
            allow_no_changes=not bool(criteria)),
    }
    if existing_contract is not None and "phase_role" not in existing_contract:
        contract.pop("phase_role")
    if not worker_result:
        contract["result_template"] = "Write this bounded phase result, replacing placeholders:\n" + json.dumps({
            "schema_version": 1, "execution_id": identity, "generation": "integer from TPO_ATTEMPT_GENERATION",
            "tick_id": tick_id, "todo_id": todo_id, "step_key": phase,
            "verdict": "success", "head_sha": "actual Git HEAD SHA",
        }, sort_keys=True)
    full_prompt = (prompt + "\n\nSupervisor result delivery:\n"
                   "Write the filled tpo_result object from this template as JSON to the absolute path "
                   "in environment variable TPO_RESULT_PATH. Do not publish provider output. "
                   "TPO_CHECKPOINT_DIR is the only additional writable submission directory. "
                   "If TPO_RECOVERY_CONTEXT_PATH is set, read it before working and preserve partial work.\n"
                   + contract["result_template"] + "\n")
    if registration and registration.manifest and phase_role == "implementation":
        full_prompt += (
            "After each complete task commit, write checkpoint-TASK_ID.json in TPO_CHECKPOINT_DIR "
            "with exactly this JSON schema, substituting the task ID and actual commit SHA: "
            + json.dumps({"version": 1, "execution_id": identity,
                          "generation": "integer from TPO_ATTEMPT_GENERATION",
                          "plan_identity": registration.plan_hash, "task_id": "manifest task ID",
                          "commit": "actual full commit SHA"}, sort_keys=True)
            + ". These are submissions only; skip tasks only when recovery context lists them as accepted. "
            "The supervisor runs pinned argv verification commands and independent review before acceptance.\n"
        )
    if registration is not None:
        from .authority_result import mark_supervised_run

        mark_supervised_run(state_dir, tick_id)
    store.register(
        identity, registration_id=tick_id, plan_identity=registration.plan_hash if registration else hashlib.sha256(full_prompt.encode()).hexdigest(),
        phase=phase, prompt=full_prompt.encode("utf-8"), client={"name": client, "tools": [t.strip() for t in tools.split(",") if t.strip()]},
        worktree=str(worktree.resolve()), branch=branch, result_contract=contract, timeout=timeout,
        manifest=json.loads(json.dumps(asdict(registration.manifest))) if registration and registration.manifest and phase_role == "implementation" else None,
    )
    from .agent_checkpoint import ProgressJournal

    ProgressJournal(store, identity).initialize()
    return identity


def validate_registration(store: ExecutionStore, identity: str) -> None:
    pinned = store.load(identity)["registration"]
    from .agent_client import validate_git_metadata

    validate_git_metadata(pinned)
    contract = pinned["result_contract"]
    if contract.get("kind") not in {"registered", "profile"}:
        raise ExecutionError("registration_invalid")
    if (contract["tick_id"] != pinned["registration_id"] or contract["phase"] != pinned["phase"]
            or execution_id(contract["tick_id"], contract["phase"]) != identity):
        raise ExecutionError("execution_identity_mismatch")
    worktree = Path(pinned["worktree"])
    store._outside_worktree(str(worktree))
    if contract["kind"] == "profile":
        from .agent_authority import profile_root

        if store.root.resolve() != profile_root(Path(contract["project_dir"])).resolve():
            raise ExecutionError("profile_authority_root_unconfirmed")
    if _git(worktree, "branch", "--show-current") != pinned["branch"]:
        raise ExecutionError("branch_drift")
    if contract["kind"] == "registered":
        state_dir = Path(contract["state_dir"])
        if (store.root.resolve() != (Path(contract["project_dir"]) / ".hermes" / "agent-executions").resolve()
                or state_dir.resolve() != (Path(contract["project_dir"]) / ".hermes").resolve()):
            raise ExecutionError("registration_root_mismatch")
        path = state_dir / "runs" / pinned["registration_id"] / "registration.json"
        if hashlib.sha256(_safe_read(path)).hexdigest() != contract["registration_sha256"]:
            raise ExecutionError("registration_drift")
        validated = load_validated_registration(Path(contract["project_dir"]), state_dir, pinned["registration_id"])
        if (str(validated.worktree) != pinned["worktree"] or validated.branch != pinned["branch"]
                or validated.plan_hash != pinned["plan_identity"] or validated.prompt_client != pinned["client"]["name"]):
            raise ExecutionError("registration_drift")
        from .phase_schedule import execution_keys

        if pinned["phase"] not in execution_keys(validated):
            raise ExecutionError("phase_identity_mismatch")


def validated_result(store: ExecutionStore, identity: str, generation: int, *, promote: bool = False,
                     deadline_monotonic: float | None = None) -> dict:
    with collection_deadline(deadline_monotonic):
        return _validated_result(store, identity, generation, promote=promote)


def _validated_result(store: ExecutionStore, identity: str, generation: int, *, promote: bool) -> dict:
    validate_registration(store, identity)
    from .agent_checkpoint import ProgressJournal

    journal = ProgressJournal(store, identity)
    journal._check_git(journal._load())
    registration = store.load(identity)["registration"]
    contract = registration["result_contract"]
    if registration["manifest"] is not None:
        progress = journal.recovery_context(generation)
        if (progress["legacy_evidence_absent"]
                or len(progress["accepted"]) != len(registration["manifest"]["tasks"])):
            raise ExecutionError("checkpoint_evidence_incomplete")
    path = (staging_directory(store, identity, generation) / "result.json" if promote
            else store.root / identity / f"result-{generation}.json")
    if path.stat().st_size > MAX_METADATA_BYTES:
        raise ExecutionError("result_size_limit")
    encoded = _safe_read(path)
    if len(encoded) > MAX_METADATA_BYTES:
        raise ExecutionError("result_size_limit")
    raw = json.loads(encoded)
    if contract["result_kind"] == "phase":
        expected = {"schema_version": 1, "execution_id": identity, "generation": generation,
                    "tick_id": contract["tick_id"], "todo_id": contract["todo_id"], "step_key": contract["phase"],
                    "verdict": "success", "head_sha": _git(Path(registration["worktree"]), "rev-parse", "HEAD")}
        if raw != expected or type(raw.get("schema_version")) is not int or type(raw.get("generation")) is not int:
            raise ExecutionError("phase_result_invalid")
        _reject_unsafe_strings(raw)
        if _git_bytes(Path(registration["worktree"]), "status", "--porcelain=v1", "--untracked-files=all", "-z"):
            raise ExecutionError("worktree_dirty")
        _git(Path(registration["worktree"]), "merge-base", "--is-ancestor", contract["base_sha"], raw["head_sha"])
    else:
        _validate_worker_result(registration, contract, raw)
    if promote:
        with store._directory_handle(identity) as directory:
            accepted = Path(f"result-{generation}.json")
            try:
                existing = json.loads(_safe_read(accepted, directory_fd=directory))
            except FileNotFoundError:
                check_collection_deadline()
                _atomic_write(accepted, raw, directory_fd=directory)
            else:
                if existing != raw:
                    raise ExecutionError("result_already_promoted")
    return raw


def _validate_worker_result(registration: dict, contract: dict, raw: dict) -> None:
    result = parse_worker_result(
        {"runs": [{"status": "completed", "metadata": {"tpo_result": raw}}]},
        tick_id=contract["tick_id"], todo_id=contract["todo_id"], step_key=contract["phase"],
        acceptance_criteria=tuple(contract["acceptance"]), allow_no_changes=not bool(contract["expected_commits"]),
    )
    if contract["expected_commits"]:
        verify_worker_git_result(Path(registration["worktree"]), result.git,
                                   expected_parent_sha=contract["base_sha"], expected_commits=contract["expected_commits"])
    else:
        verify_optional_single_commit(Path(registration["worktree"]), result.git, expected_parent_sha=contract["base_sha"])


def status(store: ExecutionStore, identity: str) -> dict:
    record = store.load(identity)
    if not record["attempts"] or record["attempts"][-1]["status"] not in TERMINAL:
        return _status(store, identity)
    try:
        with store.locked(identity):
            return _status(store, identity)
    except LockUnconfirmed:
        record = store.load(identity)
        attempt = record["attempts"][-1] if record["attempts"] else None
        return {"version": 1, "execution_id": identity, "generation": attempt["generation"] if attempt else 0,
                "status": "running_detached" if attempt and identity_matches(attempt["supervisor"]) else "lock_unconfirmed",
                "completion_allowed": False}


def _status(store: ExecutionStore, identity: str, *, revalidate: bool = True) -> dict:
    record = store.load(identity)
    report = {"version": 1, "execution_id": identity, "generation": 0,
              "status": "registered", "completion_allowed": False}
    if not record["attempts"]:
        refusal = _launch_refusal(store, identity, record)
        if refusal is not None:
            report.update(status=refusal, reason=refusal)
        return report
    attempt = record["attempts"][-1]
    report.update(generation=attempt["generation"], status=attempt["status"],
                  exit_code=attempt["exit_code"], signal=attempt["exit_signal"], cleanup=attempt["cleanup"])
    # For terminal attempts, include reason so operators can distinguish collection outcomes
    if attempt["status"] in TERMINAL and attempt["reason"] is not None:
        report["reason"] = attempt["reason"]
    if attempt["status"] not in TERMINAL:
        report["status"] = "running_detached" if identity_matches(attempt["supervisor"]) else "lock_unconfirmed"
    elif attempt["cleanup"] != "confirmed":
        report["status"] = "cleanup_unconfirmed"
    elif revalidate and attempt["status"] == "exited" and attempt["exit_code"] == 0:
        try:
            raw = validated_result(store, identity, attempt["generation"])
        except (ExecutionError, ResultContractError, OSError, ValueError, KeyError):
            report["status"] = "result_invalid"
        else:
            report.update(status="completed", completion_allowed=True, metadata={"tpo_result": raw})
    return report



class _AdmissionBusy(ExecutionError):
    """Verified pre-admission worktree contention, rather than unknown ownership."""


@contextmanager
def _admission_worktree_lock(store: ExecutionStore, identity: str):
    with ExitStack() as stack:
        try:
            stack.enter_context(store.worktree_locked(identity))
        except LockUnconfirmed as exc:
            cause = exc.__cause__
            if isinstance(cause, OSError) and cause.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise _AdmissionBusy from exc
            raise
        # Do not translate contention on the execution lock or inside preflight.
        yield


def _waiting_for_admission(store: ExecutionStore, identity: str, *, reason: str) -> dict:
    record = store.load(identity)
    return {"version": 1, "execution_id": identity, "generation": len(record["attempts"]),
            "status": "waiting_for_admission", "reason": reason, "completion_allowed": False}

def supervise(store: ExecutionStore, identity: str, *, recovery_event: str | None = None) -> dict:
    try:
        with _admission_worktree_lock(store, identity), store.locked(identity):
            try:
                return _supervise_locked(store, identity, recovery_event=recovery_event)
            except (ExecutionError, ProcessLaunchError, OSError, ValueError) as error:
                _remember_launch_refusal(store, identity, error)
                raise
    except _AdmissionBusy:
        return _waiting_for_admission(store, identity, reason="worktree_busy")


def _supervise_locked(store: ExecutionStore, identity: str, *, recovery_event: str | None) -> dict:
    current = store.load(identity)
    if current["attempts"] and recovery_event is None:
        return status(store, identity)
    staging, arguments = _prepare_launch(store, identity, current)
    if recovery_event is not None:
        from .agent_recovery import consume_recovery

        preview = consume_recovery(store, identity, recovery_event)
        store.authorize_retry(identity, expected_generation=preview["generation"], event_id=recovery_event,
                              recovery_context=json.dumps(preview["context"], sort_keys=True))
    record, created = store.admit(identity, recovery_event=recovery_event)
    if not created:
        return status(store, identity)
    generation = record["attempts"][-1]["generation"]
    registration = record["registration"]
    store.update_attempt(identity, generation, supervisor=process_identity(os.getpid()))
    context_path = ""
    if generation > 1:
        from .agent_checkpoint import ProgressJournal

        context = ProgressJournal(store, identity).recovery_context(generation)
        context["approved_intent"] = record["attempts"][-1]["recovery_context"]
        with _open_directory(staging) as directory:
            _atomic_write(Path("recovery-context.json"), context, directory_fd=directory)
        context_path = str(staging / "recovery-context.json")
    try:
        result = run_process(
            arguments, cwd=Path(registration["worktree"]),
            stdin_bytes=base64.b64decode(registration["prompt_base64"]), timeout=registration["timeout"],
            env={**os.environ, "TPO_RESULT_PATH": str(staging / "result.json"), "TPO_CHECKPOINT_DIR": str(staging),
                 "TPO_ATTEMPT_GENERATION": str(generation),
                 "TPO_RECOVERY_CONTEXT_PATH": context_path},
            on_launch=lambda receipt: store.update_attempt(
                identity, generation, status="running", client_process=receipt["identity"],
                started_monotonic=receipt["launched_monotonic"], deadline_monotonic=receipt["deadline"]),
            on_processes=lambda processes: store.update_attempt(identity, generation, direct_processes=processes),
        )
    except ProcessLaunchError as exc:
        store.update_attempt(identity, generation, status="blocked", reason="client_not_launched",
                             cleanup="confirmed" if exc.cleanup == "confirmed" else "unconfirmed")
    except ProcessOwnershipError as exc:
        store.update_attempt(identity, generation, status="interrupted", reason="exit_unobservable",
                             cleanup="unconfirmed", direct_processes=exc.processes)
    except Exception:
        store.update_attempt(identity, generation, status="interrupted", reason="exit_unobservable", cleanup="unconfirmed")
    else:
        collecting = result["outcome"] == "exited" and result["exit_code"] == 0 and result["cleanup"] == "confirmed"
        deadline_collecting = (result["outcome"] == "timed_out" and result["cleanup"] == "confirmed"
                              and registration["manifest"] is not None)

        # The terminal status is written once, after any collection; a pending
        # reason lets recover() finish an interrupted deadline collection.
        store.update_attempt(identity, generation, status="running" if (collecting or deadline_collecting) else result["outcome"],
                             exit_code=result["exit_code"],
                             exit_signal=result["signal"], cleanup="confirmed" if result["cleanup"] == "confirmed" else "unconfirmed",
                             direct_processes=result["processes"],
                             **({"reason": "deadline_collection_pending"} if deadline_collecting else {}))

        if collecting:
            from .agent_collector import (
                CollectionInterrupted,
                CollectionTimedOut,
                collect_checkpoints,
            )

            try:
                with collection_deadline(result["deadline"]):
                    collected = collect_checkpoints(store, identity, generation,
                                                    deadline_monotonic=result["deadline"])
                    if not collected["complete"]:
                        raise ExecutionError("checkpoint_evidence_incomplete")
                    raw = validated_result(store, identity, generation, promote=True,
                                           deadline_monotonic=result["deadline"])
                    # This exact result was just validated while holding both locks.
                    # Historical status calls revalidate independently of this budget.
                    report = _status(store, identity, revalidate=False)
                    report.update(status="completed", completion_allowed=True, metadata={"tpo_result": raw})
            except CollectionTimedOut:
                store.update_attempt(identity, generation, status="timed_out", reason="checkpoint_deadline_exceeded")
            except CollectionInterrupted:
                store.update_attempt(identity, generation, status="interrupted", reason="exit_unobservable")
            except (ExecutionError, ResultContractError, OSError, ValueError, KeyError):
                store.update_attempt(identity, generation, status="exited", reason="result_invalid")
                report = _status(store, identity, revalidate=False)
                report["status"] = "result_invalid"
                return report
            else:
                # Eligibility is final before the immutable terminal write. A
                # slow fsync cannot turn an eligible success into a late timeout.
                store.update_attempt(identity, generation, status="exited")
                return report

        if deadline_collecting:
            from .agent_collector import collect_checkpoints
            from .agent_execution import execution_logger

            logger = execution_logger(store, identity)
            budget_deadline = time.monotonic() + deadline_collection_budget(registration["timeout"])
            reason = "deadline_collection_incomplete"

            logger.info("deadline_collection_start generation=%s budget_s=%s", generation,
                        deadline_collection_budget(registration["timeout"]))

            try:
                with collection_deadline(budget_deadline):
                    collected = collect_checkpoints(store, identity, generation,
                                                    deadline_monotonic=budget_deadline)
                    reason = "deadline_collection_complete" if collected["complete"] else "deadline_collection_partial"
            except Exception as exc:
                logger.warning("deadline_collection_failed generation=%s error=%s", generation, type(exc).__name__)

            logger.info("deadline_collection_end generation=%s reason=%s", generation, reason)

            store.update_attempt(identity, generation, status="timed_out", reason=reason)
            return status(store, identity)

    return status(store, identity)


def attach(store: ExecutionStore, identity: str, *, recovery_event: str | None = None) -> dict:
    record = store.load(identity)
    if record["attempts"] and recovery_event is None:
        return status(store, identity)
    try:
        with _admission_worktree_lock(store, identity), store.locked(identity):
            record = store.load(identity)
            if record["attempts"] and recovery_event is None:
                return status(store, identity)
            try:
                if recovery_event is not None:
                    from .agent_recovery import validate_recovery

                    validate_recovery(store, identity, recovery_event)
                _prepare_launch(store, identity, record)
            except (ExecutionError, ProcessLaunchError, OSError, ValueError) as error:
                _remember_launch_refusal(store, identity, error)
                raise
    except _AdmissionBusy:
        return _waiting_for_admission(store, identity, reason="worktree_busy")
    # Each daemon races only for kernel locks; only the winner admits a client.
    try:
        command = [installed_entrypoint(), "_supervise", "--root", str(store.root), "--execution", identity]
        if recovery_event is not None:
            command += ["--recovery-event", recovery_event]
        subprocess.Popen(command,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    except (ExecutionError, OSError, ValueError) as error:
        with store.worktree_locked(identity), store.locked(identity):
            latest = store.load(identity)
            if _registration_digest(latest) != _registration_digest(record):
                raise ExecutionError("registration_drift") from None
            if len(latest["attempts"]) > len(record["attempts"]):
                return status(store, identity)
            _remember_launch_refusal(store, identity, error)
        raise
    report = status(store, identity)
    if report["status"] == "registered" or (
        recovery_event is not None and report["generation"] == len(record["attempts"])
    ):
        # An explicitly requested recovery may still be waiting for admission.
        # Retain the existing generation's durable outcome while polling.
        return _waiting_for_admission(store, identity, reason="launch_pending")
    return report


def recover(store: ExecutionStore, identity: str, *, cleanup_timeout: float = 60) -> dict:
    try:
        worktree = Path(store.load(identity)["registration"]["worktree"]).resolve()
        worktree_lock = "worktree-" + hashlib.sha256(os.fsencode(worktree)).hexdigest()
        # Recovery serializes ownership without the launch-admission scan:
        # unresolved peers must not mutually prevent their own cleanup.
        with store.locked(worktree_lock), store.locked(identity):
            record = store.load(identity)
            if not record["attempts"]:
                return status(store, identity)
            attempt = record["attempts"][-1]
            from .agent_collector import collector_launch_pending

            pending_launch = collector_launch_pending(store, identity)
            if attempt["status"] in TERMINAL and attempt["cleanup"] == "confirmed" and not pending_launch:
                return status(store, identity)
            if identity_matches(attempt["supervisor"]):
                return {**status(store, identity), "status": "lock_unconfirmed"}
            known = list(attempt["direct_processes"])
            direct = attempt["client_process"]
            if direct is not None and not any(
                all(process.get(key) == direct.get(key) for key in ("pid", "start_ticks", "host", "boot_id"))
                for process in known
            ):
                known.append(direct)
            outcome = cleanup_processes(known, cleanup_timeout=cleanup_timeout)
            confirmed = bool(known) and outcome["cleanup"] == "confirmed"
            if pending_launch and confirmed:
                pending_launch = collector_launch_pending(store, identity, cleaned_processes=known)
            changes = {"cleanup": "confirmed" if confirmed and not pending_launch else "unconfirmed"}
            if attempt["status"] not in TERMINAL:
                from .agent_collector import collector_timed_out

                # Check collector evidence FIRST (it survives across supervisor crashes)
                if collector_timed_out(store, identity, attempt["generation"]):
                    changes.update(status="timed_out", reason="checkpoint_deadline_exceeded")
                # Then check deadline_collection_pending (in-progress collection)
                elif attempt["reason"] == "deadline_collection_pending":
                    changes.update(status="timed_out", reason="deadline_collection_incomplete")
                else:
                    changes.update(status="interrupted", reason="exit_unobservable")
            store.update_attempt(identity, attempt["generation"], **changes)
            return status(store, identity)
    except LockUnconfirmed:
        report = status(store, identity)
        return {**report, "status": "running_detached" if report["status"] == "running_detached" else "lock_unconfirmed"}


def sweep(root: Path, *, cleanup_timeout: float = 0) -> list[dict]:
    if not root.exists():
        return []
    store = ExecutionStore(root)
    reports = []
    for path in sorted(root.glob("*/record.json")):
        try:
            reports.append(recover(store, path.parent.name, cleanup_timeout=cleanup_timeout))
        except (ExecutionError, OSError, ValueError):
            reports.append({"execution_id": path.parent.name, "status": "lock_unconfirmed", "completion_allowed": False})
    return reports


def diagnostics(root: Path) -> list[dict]:
    """Read-only status for existing TPO diagnostics; never signal processes."""
    if not root.exists():
        return []
    store = ExecutionStore(root)
    reports = []
    for path in sorted(root.glob("*/record.json")):
        try:
            reports.append(status(store, path.parent.name))
        except (ExecutionError, OSError, ValueError):
            reports.append({"execution_id": path.parent.name, "status": "lock_unconfirmed", "completion_allowed": False})
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Internal TPO registered execution supervisor")
    parser.add_argument("--version", action="version", version="tpo-agent-supervisor 1")
    parser.add_argument("operation", choices=("run", "status", "_supervise", "prepare-recovery", "approve-recovery"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--execution", required=True)
    parser.add_argument("--recovery-event")
    parser.add_argument("--wait", action="store_true", help="Wait for the registered attempt within its existing deadline and cleanup allowance")
    parser.add_argument("--mode", choices=("recovery_only", "resume"), default="recovery_only")
    parser.add_argument("--preview-file", type=Path)
    args = parser.parse_args(argv)
    try:
        started = time.monotonic()
        store = ExecutionStore(args.root)
        if args.operation == "prepare-recovery":
            from .agent_recovery import prepare_recovery

            print(json.dumps(prepare_recovery(store, args.execution, mode=args.mode), sort_keys=True))
            return 0
        if args.operation == "approve-recovery":
            from .agent_recovery import approve_recovery

            if args.preview_file is None:
                raise ExecutionError("recovery_preview_required")
            event = approve_recovery(store, args.execution, json.loads(_safe_read(args.preview_file)))
            print(json.dumps({"recovery_event": event}, sort_keys=True))
            return 0
        if args.operation == "status":
            report = status(store, args.execution)
        else:
            expected_generation = (len(store.load(args.execution)["attempts"]) + 1
                                   if args.operation == "run" and args.recovery_event is not None else None)
            report = {"run": attach, "_supervise": supervise}[args.operation](store, args.execution, recovery_event=args.recovery_event)
        if args.operation == "run":
            registration = store.load(args.execution)["registration"]
            timeout = registration["timeout"]
            ceiling_tail = wait_ceiling_tail(registration)
            stop = started + (timeout + ceiling_tail if args.wait else 5)
            caller = process_identity(os.getpid()) if args.wait else None
            while report["status"] in {"running_detached", "registered", "lock_unconfirmed", "waiting_for_admission"} and time.monotonic() < stop:
                if args.wait:
                    attempts = store.load(args.execution)["attempts"]
                    attempt = attempts[-1] if attempts else None
                    if (attempt is not None and (expected_generation is None or attempt["generation"] >= expected_generation)
                            and attempt["deadline_monotonic"] is not None and attempt["supervisor"] is not None
                            and all(attempt["supervisor"][key] == caller[key] for key in ("host", "boot_id"))):
                        stop = min(stop, attempt["deadline_monotonic"] + ceiling_tail)
                    if time.monotonic() >= stop:
                        break
                time.sleep(min(0.1, max(0, stop - time.monotonic())))
                if report["status"] == "waiting_for_admission" and report.get("reason") == "worktree_busy":
                    # Only verified worktree contention is retried, without
                    # admitting a generation or starting its execution budget.
                    report = attach(store, args.execution, recovery_event=args.recovery_event)
                    continue
                observed = status(store, args.execution)
                if (expected_generation is not None and observed["generation"] < expected_generation
                        and report["status"] in {"running_detached", "waiting_for_admission"}):
                    # The previous terminal outcome remains authoritative for
                    # that generation, but cannot settle this requested retry.
                    continue
                if observed["status"] == "registered" and report["status"] == "waiting_for_admission":
                    # A detached daemon has not admitted yet. Do not turn that
                    # launch window into a terminal dispatcher refusal.
                    continue
                report = observed
    except (ExecutionError, ProcessLaunchError, ResultContractError, OSError, ValueError) as error:
        report = {"version": 1, "status": _failure_code(error), "completion_allowed": False}
        try:
            record = ExecutionStore(args.root).load(args.execution)
            report.update(execution_id=record["execution_id"], generation=len(record["attempts"]))
        except (ExecutionError, OSError, ValueError):
            pass
    print(json.dumps(report, sort_keys=True))
    return 0 if report["completion_allowed"] or report["status"] in {"running_detached", "waiting_for_admission"} else 1
