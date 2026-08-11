from __future__ import annotations

from subprocess import CompletedProcess, TimeoutExpired

from hermes_pipeline.phases import load_profile_prerequisites
from hermes_pipeline.profile_prerequisites import (
    unverified_prerequisite_ids,
    verify_hermes_skill_registry_prerequisite,
)


def test_unverified_prerequisite_ids_uses_selected_client_contract():
    prerequisites = load_profile_prerequisites("agent-skills")

    assert unverified_prerequisite_ids(prerequisites, "claude")
    assert unverified_prerequisite_ids(prerequisites, "codex")


def test_verify_registry_skill_uses_selected_assignee():
    calls = []

    def runner(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return CompletedProcess(cmd, 0, stdout="todos-manager enabled\n", stderr="")

    assert verify_hermes_skill_registry_prerequisite(
        assignee="pipeline", skill_id="todos-manager", runner=runner
    ) == (True, "")
    assert calls[0][0] == [
        "hermes",
        "-p",
        "pipeline",
        "skills",
        "list",
        "--enabled-only",
    ]


def test_verify_registry_skill_fails_closed_on_timeout():
    def runner(cmd, **kwargs):
        raise TimeoutExpired(cmd, 10)

    verified, detail = verify_hermes_skill_registry_prerequisite(
        assignee="pipeline", skill_id="todos-manager", runner=runner
    )

    assert verified is False
    assert "timed out" in detail
