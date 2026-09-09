"""Provider-free safety checks for the optional live registration contract."""

from __future__ import annotations

import json
import subprocess

import pytest

from tests import test_hermes_registration_contract as contract


@pytest.mark.parametrize("opt_in", [None, "", "0", "true"])
def test_live_contract_requires_explicit_opt_in(monkeypatch, tmp_path, opt_in):
    if opt_in is None:
        monkeypatch.delenv("TPO_RUN_LIVE_HERMES_CONTRACT", raising=False)
    else:
        monkeypatch.setenv("TPO_RUN_LIVE_HERMES_CONTRACT", opt_in)

    def unexpected_call(*args, **kwargs):
        pytest.fail("Opted-out contract must not discover or invoke Hermes")

    monkeypatch.setattr(contract.shutil, "which", unexpected_call)
    monkeypatch.setattr(contract.subprocess, "run", unexpected_call)
    with pytest.raises(pytest.skip.Exception, match="TPO_RUN_LIVE_HERMES_CONTRACT"):
        contract.test_live_hermes_registration_barrier_contract(tmp_path)


def test_opted_in_contract_isolates_every_subprocess(monkeypatch, tmp_path):
    monkeypatch.setenv("TPO_RUN_LIVE_HERMES_CONTRACT", "1")
    monkeypatch.setenv("HERMES_HOME", "/live/hermes")
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "0")
    monkeypatch.setenv("HERMES_MANAGED_DIR", "/live/managed")
    for key in (
        "HOME", "DB", "WORKSPACES_ROOT", "BOARD", "TASK_ID", "WORKER_TOKEN",
        "FUTURE_OVERRIDE",
    ):
        monkeypatch.setenv(f"HERMES_KANBAN_{key}", f"/live/{key}")
    monkeypatch.setenv("REGISTRATION_UNRELATED", "preserved")
    monkeypatch.setattr(contract.shutil, "which", lambda name: "/fake/hermes")
    responses = iter([
        {"id": "barrier", "status": "ready"},
        {"id": "child", "status": "todo"},
        {"skipped_nonspawnable": ["barrier"], "spawned": []},
        {},
        [{"id": "barrier", "status": "done"}, {"id": "child", "status": "ready"}],
    ])
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        env = kwargs["env"]
        expected = {
            "HERMES_KANBAN_HOME": str(tmp_path),
            "HERMES_KANBAN_DB": str(tmp_path / "kanban.db"),
            "HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path / "workspaces"),
            "HERMES_KANBAN_BOARD": "default",
        }
        assert {k: v for k, v in env.items() if k.startswith("HERMES_KANBAN_")} == expected
        assert env["HERMES_HOME"] == str(tmp_path)
        assert env["PYTHON_DOTENV_DISABLED"] == "1"
        assert env["HERMES_MANAGED_DIR"] == str(tmp_path / "managed")
        assert env["REGISTRATION_UNRELATED"] == "preserved"
        assert 0 < kwargs["timeout"] <= 60
        assert kwargs["check"] is True
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(next(responses)))

    monkeypatch.setattr(contract.subprocess, "run", fake_run)
    contract.test_live_hermes_registration_barrier_contract(tmp_path)
    assert [command[2] for command in commands] == ["create", "create", "dispatch", "complete", "list"]
    assert "--dry-run" in commands[2]
    assert commands[1][commands[1].index("--parent") + 1] == "barrier"
