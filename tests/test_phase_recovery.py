"""Tick-owned recovery routing: a timed-out phase attempt is resumed by a new card generation."""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from hermes_pipeline import phase_recovery
from hermes_pipeline._agent_supervisor import execution_id
from hermes_pipeline.agent_checkpoint import ProgressJournal
from hermes_pipeline.agent_execution import ExecutionStore
from hermes_pipeline.agent_recovery import auto_approve_resume, recovery_state
from hermes_pipeline.kanban_tasks import RESULT_VALIDATION_BLOCKED_MARKER
from tests.test_profile_schedule import schedule_fixture

TICK = "tick"


@pytest.fixture(autouse=True)
def no_real_hermes(monkeypatch):
    """The routing's Hermes seam is the phase_recovery module; nothing here may reach the real CLI."""
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(phase_recovery, "subprocess",
                        SimpleNamespace(run=run, SubprocessError=subprocess.SubprocessError))
    return calls


def _worktree(tmp_path):
    tree = tmp_path / "worktree"
    tree.mkdir()
    subprocess.run(["git", "init", "-b", "task", str(tree)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tree), "-c", "user.name=Test", "-c", "user.email=test@example.org",
                    "commit", "--allow-empty", "-m", "base"], check=True, capture_output=True)
    return tree


def _run_dir(tmp_path):
    """The run directory exists in production (registration.json lives there)."""
    path = tmp_path / ".hermes" / "runs" / TICK
    path.mkdir(parents=True, exist_ok=True)
    return path


def _terminal_execution(tmp_path, key="design", *, status="timed_out", cleanup="confirmed"):
    """A registered execution for phase ``key`` whose first attempt ended terminally."""
    _run_dir(tmp_path)
    store = ExecutionStore(tmp_path / ".hermes" / "agent-executions")
    identity = execution_id(TICK, key)
    tree = _worktree(tmp_path)
    store.register(identity, registration_id=TICK, plan_identity="a" * 64, phase=key, prompt=b"prompt",
                   client={"name": "codex", "tools": []}, worktree=str(tree), branch="task",
                   result_contract={}, timeout=30)
    ProgressJournal(store, identity).initialize()
    store.admit(identity)
    if status == "exited":
        store.update_attempt(identity, 1, status=status, cleanup=cleanup, exit_code=0)
    elif status is not None:
        store.update_attempt(identity, 1, status=status, cleanup=cleanup)
    return store, identity, tree


def _blocked_marker(tmp_path):
    path = tmp_path / ".hermes" / "runs" / TICK / RESULT_VALIDATION_BLOCKED_MARKER
    return json.loads(path.read_text()) if path.exists() else None


def _approve(monkeypatch, verdict):
    calls = []

    def approve(store, identity):
        calls.append(identity)
        return verdict

    monkeypatch.setattr(phase_recovery, "approve_resume", approve)
    return calls


def _cards(monkeypatch, cards):
    monkeypatch.setattr(phase_recovery, "phase_cards", lambda **kw: cards)
    archived = []
    monkeypatch.setattr(phase_recovery, "archive_cards", lambda ids, *, tenant: archived.append(list(ids)) or True)
    return archived


APPROVED = {"approved": True, "event_id": "e" * 32, "reason": "recovery_approved", "generation": 1, "mode": "resume"}


def test_missing_card_with_terminal_record_reissues_generation_two(tmp_path, monkeypatch):
    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch)
    store, identity, _ = _terminal_execution(tmp_path)
    approvals = _approve(monkeypatch, APPROVED)
    archived = _cards(monkeypatch, [])
    # HEAD moved past the base while generation 1 ran: that is progress, not drift.
    monkeypatch.setattr("hermes_pipeline.result_contract._git", lambda *a: "ahead-of-base")

    assert tick() is True

    assert approvals == [identity]
    assert archived == []
    assert [(c["key"], c["generation"], c["execution_identity"]) for c in created] == [("design", 2, identity)]
    assert _blocked_marker(tmp_path) is None


def test_blocked_card_with_terminal_record_archives_then_creates(tmp_path, monkeypatch):
    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch)
    store, identity, _ = _terminal_execution(tmp_path)
    tasks["design"] = SimpleNamespace(task_id="t_11111111", status="blocked", generation=1)
    _approve(monkeypatch, APPROVED)
    order = []
    monkeypatch.setattr(phase_recovery, "phase_cards",
                        lambda **kw: [{"id": "t_11111111", "status": "blocked", "generation": 1}])
    monkeypatch.setattr(phase_recovery, "archive_cards", lambda ids, *, tenant: order.append(("archive", list(ids))) or True)
    monkeypatch.setattr("hermes_pipeline.review_reconciliation._create_task",
                        lambda **kw: order.append(("create", kw["generation"])))

    assert tick() is True

    assert order == [("archive", ["t_11111111"]), ("create", 2)]


def test_unconfirmed_archive_parks_the_run_without_a_card(tmp_path, monkeypatch):
    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch)
    _terminal_execution(tmp_path)
    tasks["design"] = SimpleNamespace(task_id="t_11111111", status="blocked", generation=1)
    _approve(monkeypatch, APPROVED)
    monkeypatch.setattr(phase_recovery, "phase_cards",
                        lambda **kw: [{"id": "t_11111111", "status": "blocked", "generation": 1}])
    monkeypatch.setattr(phase_recovery, "archive_cards", lambda ids, *, tenant: False)

    assert tick() is False

    assert created == []
    assert _blocked_marker(tmp_path)["code"] == "recovery_archive_unconfirmed"


def test_refusal_writes_blocked_marker_only(tmp_path, monkeypatch):
    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch)
    _terminal_execution(tmp_path)
    _approve(monkeypatch, {**APPROVED, "approved": False, "event_id": None, "reason": "recovery_worktree_unsafe"})
    archived = _cards(monkeypatch, [{"id": "t_11111111", "status": "blocked", "generation": 1}])

    assert tick() is False

    assert created == []
    assert archived == []
    assert _blocked_marker(tmp_path)["code"] == "recovery_worktree_unsafe"
    assert not (tmp_path / ".hermes" / "runs" / TICK / "abandoned").exists()


def test_generation_exhausted_abandons_run_and_archives_card(tmp_path, monkeypatch):
    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch)
    _terminal_execution(tmp_path)
    (tmp_path / ".hermes" / "runs" / TICK / "registration.json").write_text("{}")
    _approve(monkeypatch, {**APPROVED, "approved": False, "event_id": None,
                           "reason": "recovery_generation_exhausted", "generation": 3})
    archived = _cards(monkeypatch, [{"id": "t_33333333", "status": "blocked", "generation": 3}])

    assert tick() is False

    assert created == []
    assert archived == [["t_33333333"]]
    assert (tmp_path / ".hermes" / "runs" / TICK / "abandoned").read_text() == "recovery_generation_exhausted\n"
    assert _blocked_marker(tmp_path)["code"] == "recovery_generation_exhausted"


def test_no_record_missing_card_keeps_head_mismatch(tmp_path, monkeypatch):
    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch)
    _run_dir(tmp_path)
    monkeypatch.setattr("hermes_pipeline.result_contract._git", lambda *a: "ahead-of-base")
    monkeypatch.setattr(phase_recovery, "approve_resume", lambda *a, **k: pytest.fail("no record to resume"))

    assert tick() is False

    assert created == []
    assert _blocked_marker(tmp_path)["code"] == "head_mismatch"


def test_reissued_card_waits_without_second_approval(tmp_path, monkeypatch):
    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch)
    _terminal_execution(tmp_path)
    tasks["design"] = SimpleNamespace(task_id="t_22222222", status="ready", generation=2)
    monkeypatch.setattr(phase_recovery, "approve_resume", lambda *a, **k: pytest.fail("already reissued"))
    monkeypatch.setattr("hermes_pipeline.result_contract._git", lambda *a: "ahead-of-base")

    assert tick() is True

    assert created == []
    assert _blocked_marker(tmp_path) is None  # waited; a PROCEED would have raised head_mismatch


def test_terminal_attempt_with_live_card_waits_for_its_worker(tmp_path, monkeypatch):
    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch)
    _terminal_execution(tmp_path)
    tasks["design"] = SimpleNamespace(task_id="t_11111111", status="running", generation=1)
    monkeypatch.setattr(phase_recovery, "approve_resume", lambda *a, **k: pytest.fail("worker still reporting"))
    monkeypatch.setattr("hermes_pipeline.result_contract._git", lambda *a: "ahead-of-base")

    assert tick() is True

    assert created == []
    assert _blocked_marker(tmp_path) is None


def test_admission_failed_card_is_unblocked_once(tmp_path, monkeypatch, no_real_hermes):
    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch)
    store, identity, _ = _terminal_execution(tmp_path)
    assert auto_approve_resume(store, identity)["approved"] is True
    tasks["design"] = SimpleNamespace(task_id="t_22222222", status="blocked", generation=2)
    monkeypatch.setattr(phase_recovery, "approve_resume", lambda *a, **k: pytest.fail("approval is still open"))

    assert tick() is True
    assert no_real_hermes == [["hermes", "kanban", "unblock", "t_22222222", "--reason",
                               "tpo tick: resume of design as attempt g2 is approved; run the card command again"]]
    assert phase_recovery.unblock_marker(tmp_path / ".hermes", TICK, "design", 2).read_text() == "t_22222222\n"

    # Hermes routes a card blocked twice to triage with the approval still open:
    # one unblock per card, then the human boundary.
    tasks["design"] = SimpleNamespace(task_id="t_22222222", status="triage", generation=2)
    assert tick() is False
    assert len(no_real_hermes) == 1
    assert _blocked_marker(tmp_path)["code"] == "recovery_admission_stalled"
    assert created == []


def test_blocked_reissued_card_without_open_approval_is_reissued_again(tmp_path, monkeypatch):
    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch)
    _terminal_execution(tmp_path)
    tasks["design"] = SimpleNamespace(task_id="t_22222222", status="blocked", generation=2)
    _approve(monkeypatch, APPROVED)  # the tick re-approves (reissue) after recovery_invalidated
    archived = _cards(monkeypatch, [{"id": "t_22222222", "status": "blocked", "generation": 2}])

    assert tick() is True

    assert archived == [["t_22222222"]]
    assert [c["generation"] for c in created] == [2]


def test_unblock_once_per_generation(tmp_path, no_real_hermes):
    state_dir = tmp_path / ".hermes"
    (state_dir / "runs" / TICK).mkdir(parents=True)

    assert phase_recovery.unblock_once(state_dir=state_dir, tenant="board", tick_id=TICK, key="design",
                                       generation=2, task_id="t_22222222") is True
    assert phase_recovery.unblock_once(state_dir=state_dir, tenant="board", tick_id=TICK, key="design",
                                       generation=2, task_id="t_22222222") is False
    assert phase_recovery.unblock_once(state_dir=state_dir, tenant="board", tick_id=TICK, key="design",
                                       generation=3, task_id="t_33333333") is True

    assert [argv[3] for argv in no_real_hermes] == ["t_22222222", "t_33333333"]
    assert phase_recovery.unblock_marker(state_dir, TICK, "design", 2).exists()
    assert phase_recovery.unblock_marker(state_dir, TICK, "design", 3).exists()


def test_running_record_missing_card_waits(tmp_path, monkeypatch):
    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch)
    store, identity, _ = _terminal_execution(tmp_path, status=None)
    monkeypatch.setattr("hermes_pipeline.result_contract._git", lambda *a: "ahead-of-base")
    monkeypatch.setattr(phase_recovery, "approve_resume", lambda *a, **k: pytest.fail("attempt is running"))

    assert tick() is True

    assert created == []
    assert _blocked_marker(tmp_path) is None


def test_running_record_blocked_card_is_unblocked_once_when_daemon_alive(tmp_path, monkeypatch, no_real_hermes):
    import os

    from hermes_pipeline.agent_execution import process_identity

    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch)
    store, identity, _ = _terminal_execution(tmp_path, status=None)
    store.update_attempt(identity, 1, status="running", supervisor=process_identity(os.getpid()))
    tasks["design"] = SimpleNamespace(task_id="t_11111111", status="blocked", generation=1)

    assert tick() is True
    assert tick() is True

    assert [argv[3] for argv in no_real_hermes] == ["t_11111111"]
    assert created == []


def test_full_reconcile_path_approves_under_distinct_store_authority_lock(tmp_path, monkeypatch):
    from hermes_pipeline import authority_result

    real_authority = authority_result.locked_run_authority
    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(authority_result, "locked_run_authority", real_authority)
    store, identity, tree = _terminal_execution(tmp_path)
    registration.repository = tmp_path
    registration.worktree = tree
    monkeypatch.setattr("hermes_pipeline.result_contract._git", lambda *a: "ahead-of-base")
    archived = _cards(monkeypatch, [{"id": "t_11111111", "status": "blocked", "generation": 1}])

    assert tick() is True

    state = recovery_state(ExecutionStore(store.root), identity)
    assert state["state"] == "approved" and state["approver"] == "tick" and state["generation"] == 1
    assert archived == [["t_11111111"]]
    assert [(c["generation"], c["execution_identity"]) for c in created] == [(2, identity)]

    # A second tick before admission converges: same approval, no second card.
    tasks["design"] = SimpleNamespace(task_id="t_22222222", status="ready", generation=2)
    assert tick() is True
    assert len(created) == 1
    assert recovery_state(ExecutionStore(store.root), identity)["reissues"] == 0


def test_execution_lock_held_waits_for_the_next_tick(tmp_path, monkeypatch):
    """A waiter's status poll holds the execution lock briefly: transient, so no marker and no card."""
    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch)
    store, identity, _ = _terminal_execution(tmp_path)
    archived = _cards(monkeypatch, [])

    with ExecutionStore(store.root).locked(identity):
        assert tick() is True

    assert created == []
    assert archived == []
    assert _blocked_marker(tmp_path) is None
    assert recovery_state(ExecutionStore(store.root), identity) is None


@pytest.mark.parametrize("code", ["recovery_busy", "recovery_approval_failed", "recovery_worktree_busy"])
def test_transient_refusals_wait_without_a_marker(tmp_path, monkeypatch, code):
    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch)
    _terminal_execution(tmp_path)
    _approve(monkeypatch, {**APPROVED, "approved": False, "event_id": None, "reason": code})
    archived = _cards(monkeypatch, [{"id": "t_11111111", "status": "blocked", "generation": 1}])

    assert tick() is True

    assert created == [] and archived == []
    assert _blocked_marker(tmp_path) is None


def test_running_lower_generation_card_defers_the_reissue(tmp_path, monkeypatch):
    """Archiving a running card orphans its Hermes worker: wait for it to settle instead."""
    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch)
    _terminal_execution(tmp_path)
    tasks["design"] = SimpleNamespace(task_id="t_22222222", status="blocked", generation=2)
    _approve(monkeypatch, APPROVED)
    archived = _cards(monkeypatch, [{"id": "t_11111111", "status": "running", "generation": 1},
                                    {"id": "t_22222222", "status": "blocked", "generation": 2}])

    assert tick() is True

    assert created == [] and archived == []
    assert _blocked_marker(tmp_path) is None


def test_failed_unblock_keeps_the_allowance(tmp_path, monkeypatch):
    state_dir = tmp_path / ".hermes"
    (state_dir / "runs" / TICK).mkdir(parents=True)
    codes = [1, 0]
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=codes.pop(0), stdout="", stderr="")

    monkeypatch.setattr(phase_recovery, "subprocess", SimpleNamespace(run=run, SubprocessError=subprocess.SubprocessError))
    kwargs = dict(state_dir=state_dir, tenant="board", tick_id=TICK, key="design", generation=2, task_id="t_22222222")

    assert phase_recovery.unblock_once(**kwargs) is False
    assert not phase_recovery.unblock_marker(state_dir, TICK, "design", 2).exists()
    assert phase_recovery.unblock_once(**kwargs) is True
    assert phase_recovery.unblock_marker(state_dir, TICK, "design", 2).exists()
    assert len(calls) == 2


def test_unblock_markers_do_not_collide_across_similar_keys(tmp_path, no_real_hermes):
    state_dir = tmp_path / ".hermes"
    (state_dir / "runs" / TICK).mkdir(parents=True)

    assert phase_recovery.unblock_once(state_dir=state_dir, tenant="board", tick_id=TICK, key="a/b",
                                       generation=1, task_id="t_11111111") is True
    assert phase_recovery.unblock_once(state_dir=state_dir, tenant="board", tick_id=TICK, key="a-b",
                                       generation=1, task_id="t_22222222") is True

    assert [argv[3] for argv in no_real_hermes] == ["t_11111111", "t_22222222"]


def test_triaged_card_counts_as_a_worker_that_gave_up(tmp_path, monkeypatch):
    """Hermes parks a twice-blocked card in triage; no worker will ever report it again."""
    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch)
    _terminal_execution(tmp_path)
    tasks["design"] = SimpleNamespace(task_id="t_11111111", status="triage", generation=1)
    _approve(monkeypatch, APPROVED)
    archived = _cards(monkeypatch, [{"id": "t_11111111", "status": "triage", "generation": 1}])

    assert tick() is True

    assert archived == [["t_11111111"]]
    assert [c["generation"] for c in created] == [2]


def test_exhausted_with_unconfirmed_archive_does_not_abandon_yet(tmp_path, monkeypatch):
    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch)
    _terminal_execution(tmp_path)
    (tmp_path / ".hermes" / "runs" / TICK / "registration.json").write_text("{}")
    _approve(monkeypatch, {**APPROVED, "approved": False, "event_id": None,
                           "reason": "recovery_generation_exhausted", "generation": 3})
    monkeypatch.setattr(phase_recovery, "phase_cards", lambda **kw: [{"id": "t_33333333", "status": "blocked", "generation": 3}])
    monkeypatch.setattr(phase_recovery, "archive_cards", lambda ids, *, tenant: False)

    assert tick() is False

    assert not (tmp_path / ".hermes" / "runs" / TICK / "abandoned").exists()
    assert _blocked_marker(tmp_path)["code"] == "recovery_archive_unconfirmed"


def test_reissue_clears_a_stale_blocked_marker(tmp_path, monkeypatch):
    from hermes_pipeline.kanban_tasks import _record_validation_blocked

    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch)
    _terminal_execution(tmp_path)
    _record_validation_blocked(tmp_path / ".hermes", tick_id=TICK, step_key="design",
                               code="recovery_busy", reason="recovery_busy")
    _approve(monkeypatch, APPROVED)
    _cards(monkeypatch, [])

    assert tick() is True

    assert [c["generation"] for c in created] == [2]
    assert _blocked_marker(tmp_path) is None


def test_exited_attempt_proceeds_to_a_generation_one_card(tmp_path, monkeypatch):
    """A clean exit without a done card is the ordinary admission flow, not a resume."""
    registration, tasks, evidence, created, tick, complete = schedule_fixture(tmp_path, monkeypatch)
    _terminal_execution(tmp_path, status="exited")
    monkeypatch.setattr(phase_recovery, "approve_resume", lambda *a, **k: pytest.fail("nothing to resume"))

    assert tick() is True

    assert [(c["key"], c["generation"], c["execution_identity"]) for c in created] == [("design", 1, None)]
