"""Optional live Hermes registration-barrier contract test."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest


def _run_hermes(home, *args):
    return subprocess.run(
        ["hermes", *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "HERMES_HOME": str(home)},
    )


def test_live_hermes_registration_barrier_contract(tmp_path):
    """Hermes must keep an unassigned ready barrier and its child nonspawnable."""
    if shutil.which("hermes") is None:
        pytest.skip("hermes CLI is not installed; live barrier contract unavailable")

    tenant = "tpo-registration-contract"
    barrier = json.loads(
        _run_hermes(
            tmp_path,
            "kanban",
            "create",
            "--tenant",
            tenant,
            "Registration barrier",
            "--body",
            '{"infrastructure":"registration_barrier","tick_id":"LIVE"}',
            "--assignee",
            "-",
            "--idempotency-key",
            "LIVE:__registration_barrier__",
            "--json",
        ).stdout
    )
    child = json.loads(
        _run_hermes(
            tmp_path,
            "kanban",
            "create",
            "--tenant",
            tenant,
            "Phase one",
            "--body",
            '{"phase_key":"phase_1","tick_id":"LIVE"}',
            "--assignee",
            "default",
            "--parent",
            barrier["id"],
            "--idempotency-key",
            "LIVE:phase_1",
            "--json",
        ).stdout
    )

    dispatch = json.loads(
        _run_hermes(
            tmp_path,
            "kanban",
            "dispatch",
            "--dry-run",
            "--json",
        ).stdout
    )
    assert barrier["status"] == "ready"
    assert child["status"] == "todo"
    assert barrier["id"] in dispatch["skipped_nonspawnable"]
    assert child["id"] not in dispatch["spawned"]

    _run_hermes(tmp_path, "kanban", "complete", barrier["id"])
    tasks = json.loads(
        _run_hermes(
            tmp_path,
            "kanban",
            "list",
            "--tenant",
            tenant,
            "--archived",
            "--json",
        ).stdout
    )
    statuses = {task["id"]: task["status"] for task in tasks}
    assert statuses[child["id"]] == "ready"
