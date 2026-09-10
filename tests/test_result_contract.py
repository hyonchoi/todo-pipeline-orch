from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.gh_fakes import make_issue

REPO = "acme/repo"
ISSUE_BODY = "### What\n\nResult contract.\n\n### Plan\n\nplan.md\n\n### Branch\n\ntodo-42\n"

from hermes_pipeline.github_issues import (
    MAX_ISSUE_SNAPSHOT_CHARS,
    canonical_issue_snapshot,
    snapshot_hash,
)
from hermes_pipeline.phases import IMPLEMENTATION_KEY
from hermes_pipeline.result_contract import (
    _PLACEHOLDER_RE,
    ResultContractError,
    load_validated_registration,
    parse_worker_result,
    sanitize_result_text,
    verify_optional_single_commit,
    verify_worker_git_result,
    verify_worker_git_topology,
)

PLAN = '''# Plan

```json tpo-plan
{"schema_version":1,"todo_id":"TODO-42","tasks":[{"id":"task-1","title":"Do it","instructions":"Implement it.","acceptance_criteria":["Observable criterion"],"verification":["uv run pytest"],"commit_message":"feat: do it"}]}
```
'''

PLAN_TWO_TASKS = '''# Plan

```json tpo-plan
{"schema_version":1,"todo_id":"TODO-42","tasks":[{"id":"task-1","title":"Do it","instructions":"Implement it.","acceptance_criteria":["Observable criterion"],"verification":["uv run pytest"],"commit_message":"feat: do it"},{"id":"task-2","title":"Do it again","instructions":"Implement it again.","acceptance_criteria":["Observable criterion"],"verification":["uv run pytest"],"commit_message":"feat: do it again"}]}
```
'''


def _result(**updates):
    value = {
        "schema_version": 1,
        "tick_id": "01TICK",
        "todo_id": "TODO-42",
        "step_key": "plan:task-1",
        "verdict": "success",
        "git": {
            "expected_parent_sha": "a" * 40,
            "resulting_head_sha": "b" * 40,
            "task_commit_sha": "b" * 40,
            "changed_files": ["src/example.py"],
        },
        "acceptance": [{"criterion": "Observable criterion", "status": "passed"}],
    }
    value.update(updates)
    return value


def _delivery(*, command="uv run pytest", **check_extra):
    """A structurally valid ``delivery`` block, mutable one field at a time."""
    return {
        "pr_url": "https://github.com/acme/repo/pull/7",
        "branch": "todo-42",
        "head_sha": "b" * 40,
        "checks": [{"command": command, "exit_code": 0, **check_extra}],
    }


def test_parse_valid_final_successful_run():
    payload = {
        "runs": [
            {"status": "failed", "metadata": {"tpo_result": _result()}},
            {"status": "succeeded", "summary": "done", "metadata": {"tpo_result": _result()}},
        ]
    }
    parsed = parse_worker_result(
        payload,
        tick_id="01TICK",
        todo_id="TODO-42",
        step_key="plan:task-1",
        acceptance_criteria=("Observable criterion",),
    )
    assert parsed.step_key == "plan:task-1"
    assert parsed.git.changed_files == ("src/example.py",)


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda p: p.update(tick_id="wrong"), "identity_mismatch"),
        (lambda p: p["git"].update(task_commit_sha="c" * 40), "invalid_git"),
        (lambda p: p["acceptance"][0].update(status="pending"), "invalid_acceptance"),
        (lambda p: p.update(delivery=_delivery(command="x" * 501)), "size_limit"),
        # ``tpo_result`` stays exact-key-checked at every level: that strictness
        # is the whole reason the enclosing envelope need not be key-checked.
        (lambda p: p.update(unexpected="x"), "malformed_result"),
        (lambda p: p.pop("git"), "malformed_result"),
        (lambda p: p["git"].update(unexpected="x"), "invalid_git"),
        (lambda p: p.update(delivery=_delivery(unexpected="x")), "invalid_command"),
        (lambda p: p["acceptance"][0].update(unexpected="x"), "invalid_acceptance"),
    ],
)
def test_parse_rejects_invalid_contract(mutation, code):
    result = _result()
    mutation(result)
    with pytest.raises(ResultContractError, match=code):
        parse_worker_result(
            {"runs": [{"status": "succeeded", "metadata": {"tpo_result": result}}]},
            tick_id="01TICK",
            todo_id="TODO-42",
            step_key="plan:task-1",
            acceptance_criteria=("Observable criterion",),
        )


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        pytest.param({}, "malformed_payload", id="no-runs"),
        pytest.param({"runs": [{}]}, "missing_successful_run", id="no-successful-run"),
        pytest.param(
            {"runs": [{"status": "succeeded", "metadata": {}}]},
            "missing_result",
            id="empty-metadata",
        ),
        # Siblings alone are not a result: presence of ``tpo_result`` is required.
        pytest.param(
            {"runs": [{"status": "succeeded", "metadata": {"worker_session_id": "x"}}]},
            "missing_result",
            id="siblings-without-result",
        ),
        pytest.param(
            {"runs": [{"status": "succeeded", "metadata": {"tpo_result": _result(), 1: "x"}}]},
            "missing_result",
            id="non-string-metadata-key",
        ),
    ],
)
def test_parse_reports_the_code_for_missing_and_malformed_metadata(payload, code):
    """The rejection code is durable operator output, not just a raise.

    ``kanban_tasks`` records it in the validation-blocked marker and
    ``todos_completion`` hands it to ``_needs_input``, so it must be pinned.
    """
    with pytest.raises(ResultContractError, match=code):
        parse_worker_result(
            payload,
            tick_id="01TICK",
            todo_id="TODO-42",
            step_key="plan:task-1",
            acceptance_criteria=("Observable criterion",),
        )


def test_parse_rejects_oversized_metadata():
    oversized = _result(extra="x" * 65536)
    with pytest.raises(ResultContractError, match="size_limit"):
        parse_worker_result(
            {"runs": [{"status": "succeeded", "metadata": {"tpo_result": oversized}}]},
            tick_id="01TICK",
            todo_id="TODO-42",
            step_key="plan:task-1",
            acceptance_criteria=("Observable criterion",),
        )


def test_parse_bounds_the_size_of_the_enclosing_run_metadata():
    """Replaces the former ``test_parse_validates_entire_enclosing_run_metadata``.

    That test also asserted that a secret-shaped or control-bearing *sibling*
    rejected the result. That half was the wedge: siblings are never read, the
    closed run is immutable, and a rejection opens no card, so one stray
    ``notes`` line permanently stalled the step. Content tolerance now lives in
    ``test_parse_accepts_platform_injected_metadata_siblings``; only the size
    bound survives here, because it guards memory rather than content.
    """
    metadata = {"tpo_result": _result(), "padding": "x" * 65536}
    with pytest.raises(ResultContractError, match="size_limit"):
        parse_worker_result(
            {"runs": [{"status": "succeeded", "metadata": metadata}]},
            tick_id="01TICK",
            todo_id="TODO-42",
            step_key="plan:task-1",
            acceptance_criteria=("Observable criterion",),
        )


def test_parse_accepts_platform_injected_metadata_siblings():
    """The envelope is worker-supplied and live runs carry extra keys.

    Every completed run observed live carries the Hermes-stamped
    ``worker_session_id``, and nearly all carry worker-authored siblings such as
    ``notes``. Only ``tpo_result`` is read, so siblings must neither reject an
    otherwise valid result nor leak into it -- and that holds whatever they
    contain, including secret-shaped prose and control characters, because
    unread text cannot be echoed anywhere it could do harm.
    """
    metadata = {
        "tpo_result": _result(),
        "worker_session_id": "20260904_154528_b0f01d",
        "notes": "free-form worker commentary: added token: refresh",
        "provider_body": "password=super-secret",
        "unsafe\x00key": "value",
        # Named like a ``tpo_result`` field on purpose: a sibling must never
        # reach the parse, least of all one that could override a checked field.
        # Structurally valid on purpose: an invalid forgery would be caught by
        # the nested key check before either assertion below is reached, so the
        # assertions -- not validator ordering -- are what prove no leak.
        "git": {
            "expected_parent_sha": "c" * 40,
            "resulting_head_sha": "d" * 40,
            "task_commit_sha": "d" * 40,
            "changed_files": ["forged.py"],
        },
    }
    parsed = parse_worker_result(
        {"runs": [{"status": "succeeded", "metadata": metadata}]},
        tick_id="01TICK",
        todo_id="TODO-42",
        step_key="plan:task-1",
        acceptance_criteria=("Observable criterion",),
    )
    # Tolerated means unread: no sibling may reach the parsed result.
    assert parsed.git.changed_files == ("src/example.py",)


def test_summary_and_diagnostics_are_sanitized():
    secret = "gh" + "p_abcdefghijklmnopqrstuvwxyz1234567890"
    sanitized = sanitize_result_text(f"bad\x00 token {secret}", maximum=8192)
    assert "\x00" not in sanitized
    assert secret not in sanitized
    assert "[REDACTED]" in sanitized
    assert sanitize_result_text("a\u2028b\u2029c\u202ed\u2066e\r\n\tf", maximum=100) == "a b cde f"


# Every credential shape below is assembled from fragments so no literal
# credential-shaped string appears in this source file: the repository's
# secret guardrails match the shape, not the value.
_JWT = "eyJhbGciOiJIUzI1NiJ9.payload.sig"
_FINE_GRAINED_PAT = "github" + "_pat_" + "11ABCDEFG0123456789abcdefghijklmnop"
_ANTHROPIC_KEY = "sk-" + "ant-api03-" + "SECRETVALUE1234567890"
_AWS_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"
_PEM_BODY = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAA"
_PEM_BLOCK = (
    "-----BEGIN " + "OPENSSH PRIVATE KEY-----\n"
    + _PEM_BODY
    + "\n-----END " + "OPENSSH PRIVATE KEY-----"
)


# One row per alternative of ``SECRET_RE``: deleting any single alternative
# must fail exactly the row named for it. Rows are not interchangeable
# documentation -- each is a shape a real crashed tick carries.
@pytest.mark.parametrize(
    ("text", "leaked"),
    [
        pytest.param(f"Authorization: Bearer {_JWT}", _JWT, id="auth-header-bearer"),
        pytest.param(f"token: Bearer {_JWT}", _JWT, id="keyword-then-bearer"),
        pytest.param(f"Proxy-Authenticate Bearer {_JWT}", _JWT, id="bare-bearer-value"),
        pytest.param(
            "fatal: repository 'https://" "hyon:s3cr3t-pw" "@github.com/o/r.git' not found",
            "s3cr3t-pw",
            id="url-userinfo",
        ),
        pytest.param("api_key=abcdefghijklmnop", "abcdefghijklmnop", id="api-key"),
        pytest.param("apikey = abcdefghijklmnop", "abcdefghijklmnop", id="apikey-spaced"),
        pytest.param(
            "access_token=abcdefghijklmnop", "abcdefghijklmnop", id="access-token",
        ),
        # ``gh auth login`` issues fine-grained PATs by default and this repo
        # drives ``gh`` and ``git`` throughout, so this is the shape a real
        # subprocess error is most likely to echo. ``gh[pousr]_`` misses it.
        pytest.param(
            f"fatal: unable to access the repository as {_FINE_GRAINED_PAT}",
            _FINE_GRAINED_PAT,
            id="github-fine-grained-pat",
        ),
        # A bare Anthropic key carries no keyword and no ``[:=]`` before it.
        pytest.param(
            f"anthropic call failed with {_ANTHROPIC_KEY}",
            _ANTHROPIC_KEY,
            id="anthropic-key-bare",
        ),
        pytest.param(
            f"botocore.exceptions: {_AWS_KEY_ID} is not authorized",
            _AWS_KEY_ID,
            id="aws-access-key-id",
        ),
        # ``.netrc`` is space-separated, so the ``[:=]`` requirement misses it.
        pytest.param(
            "machine github.com login alice password s3cr3tpw",
            "s3cr3tpw",
            id="netrc-space-separated",
        ),
        # The whole block must go, not just the header: the base64 body is the
        # key. ``sanitize_result_text`` collapses the newlines before the
        # substitution runs, so the block arrives as one line.
        pytest.param(_PEM_BLOCK, _PEM_BODY, id="pem-private-key-block"),
    ],
)
def test_credentials_the_tick_traceback_can_carry_are_redacted(text, leaked):
    r"""Shapes a crashed tick's traceback really carries must not reach the log.

    The per-project catch-all now logs a full traceback, so a subprocess error
    echoing a remote URL or an auth header is a new exposure. Each case here is
    a real leak some earlier revision of the pattern passed through: ``\S+``
    stopped at the space after ``Bearer``, userinfo carries no keyword at all,
    the keyword list omitted ``api_key``, ``gh[pousr]_`` does not match
    ``github_pat_``, and a bare vendor key or a ``.netrc`` line carries no
    ``[:=]`` delimiter for the keyword branch to anchor on.
    """
    sanitized = sanitize_result_text(text, maximum=8192)
    assert leaked not in sanitized, text
    assert "[REDACTED]" in sanitized, text


def test_redaction_keeps_the_diagnostic_usable():
    """Redaction must not swallow the host, so the error still says where."""
    # The host must survive so the diagnostic is still usable.
    assert "github.com/o/r.git" in sanitize_result_text(
        "https://" "hyon:s3cr3t-pw" "@github.com/o/r.git", maximum=8192
    )
    # A ``.netrc`` line keeps its machine, and an AWS diagnostic its verb.
    assert "machine github.com" in sanitize_result_text(
        "machine github.com login alice password s3cr3tpw", maximum=8192
    )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        # ``git.changed_files`` entries are the only ``tpo_result`` strings the
        # scan alone guards: nothing validates their content, so with the scan
        # removed each of these payloads parses clean. Secret and control are
        # separated on purpose -- a value tripping both at once cannot tell
        # ``SECRET_RE`` and ``_CONTROL_RE`` apart, so dropping either half of
        # the scan's condition would survive.
        pytest.param(
            ("git", "changed_files", 0), "password=super-secret", id="secret-in-list",
        ),
        pytest.param(("git", "changed_files", 0), "bad\x00name", id="c0-control-in-list"),
        pytest.param(
            ("git", "changed_files", 0), "bad\u202ename", id="bidi-override-in-list",
        ),
        # The scan reaches dict KEYS, and it runs before the ``_TOP_KEYS``
        # check, so this is observable at the public boundary -- but only
        # through WHICH code is raised, not accept-vs-reject: an unsafe key is
        # rejected either way, as ``malformed_result`` once the key branch of
        # ``_reject_unsafe_strings`` stops seeing it. The code is what the
        # reconciler records in its sticky marker, so it is worth pinning. The
        # value here is deliberately benign; only the key is unsafe.
        pytest.param(("bad\x00key",), "value", id="unsafe-top-level-key"),
        # The exemption is keyed on the top level of ``tpo_result`` only. A
        # stray ``acceptance`` key nested inside a checked block must still be
        # scanned: applying the exemption recursively by key name would skip
        # this and leave ``_exact_keys`` to reject with ``invalid_git`` instead.
        pytest.param(
            ("git", "acceptance"),
            "password=super-secret",
            id="stray-nested-acceptance-key",
        ),
        # The list-of-dicts shape: it pins that the scan recurses through a
        # list *into* a dict. It used to be pinned by ``review.findings[].
        # priority``, whose bad value fell through to ``invalid_review`` once
        # the scan stopped reaching it; no such field survives now that the
        # review section is gone, so this is the only remaining case for that
        # shape -- and ``_bounded_string`` raises ``unsafe_metadata`` for it
        # even with the scan removed, so it no longer isolates the scan.
        pytest.param(
            ("delivery", "checks", 0, "command"),
            "password=super-secret\x00",
            id="delivery-check-double-covered",
        ),
    ],
)
def test_parse_rejects_secrets_and_controls_at_any_metadata_depth(path, value):
    """Every ``tpo_result`` value TPO consumes or compares stays scanned.

    ``acceptance[].criterion`` is deliberately absent: see
    ``test_acceptance_criteria_are_plan_text_and_are_never_scanned``.
    """
    result = _result()
    result["delivery"] = {
        "pr_url": "https://github.com/example/repo/pull/1",
        "branch": "todo-42",
        "head_sha": "b" * 40,
        "checks": [{"command": "uv run pytest", "exit_code": 0}],
    }
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ResultContractError, match="unsafe_metadata"):
        parse_worker_result(
            {"runs": [{"status": "succeeded", "metadata": {"tpo_result": result}}]},
            tick_id="01TICK",
            todo_id="TODO-42",
            step_key="plan:task-1",
            acceptance_criteria=("Observable criterion",),
        )


# Four of these trip only ``SECRET_RE`` and the fifth trips no guard at all, so
# they are documentation of the reported field failures, not five independent
# cases. The coverage in this test comes from the negative half below.
@pytest.mark.parametrize(
    "criterion",
    [
        "Expired token: request returns 401",
        "Login rejects a bad password: no session is created",
        "Sends authorization: Bearer a-token on every call",
        "The secret: rotation job runs nightly",
        "normalize_names([]) returns []",
    ],
)
def test_acceptance_criteria_are_plan_text_and_are_never_scanned(criterion):
    """Scanning this text can only reject TPO's own words.

    ``acceptance_criteria`` reaches the parser from the hash-pinned Plan
    manifest (``kanban_tasks`` passes ``plan_task.acceptance_criteria``) and TPO
    renders the same strings into the worker-facing card itself. Scanning them
    made any TODO whose criteria mention a token, password, authorization header
    or secret impossible to complete: the required text is dictated by the Plan,
    so no worker output could pass.

    The negative half is what pins the exemption's *scope*: acceptance being
    tolerated proves nothing on its own, because "scan nothing at all" would
    also pass. The first mutation below is the discriminator -- with the scan
    gone that payload parses clean.
    """
    result = _result(acceptance=[{"criterion": criterion, "status": "passed"}])
    parsed = parse_worker_result(
        {"runs": [{"status": "succeeded", "metadata": {"tpo_result": result}}]},
        tick_id="01TICK",
        todo_id="TODO-42",
        step_key="plan:task-1",
        acceptance_criteria=(criterion,),
    )
    assert parsed.step_key == "plan:task-1"

    for mutation in (
        lambda item: item["git"].update(changed_files=["password=super-secret"]),
        # Belt and braces: this one also rejects via ``_bounded_string`` with
        # the scan removed, so it pins the contract rather than the exemption.
        lambda item: item.update(delivery=_delivery(command="pytest password=hunter2")),
    ):
        unsafe = _result(acceptance=[{"criterion": criterion, "status": "passed"}])
        mutation(unsafe)
        with pytest.raises(ResultContractError, match="unsafe_metadata"):
            parse_worker_result(
                {"runs": [{"status": "succeeded", "metadata": {"tpo_result": unsafe}}]},
                tick_id="01TICK",
                todo_id="TODO-42",
                step_key="plan:task-1",
                acceptance_criteria=(criterion,),
            )


def test_acceptance_criteria_must_still_echo_the_plan_exactly():
    """Exempting the scan must not weaken the equality gate that justifies it."""
    result = _result(
        acceptance=[
            {"criterion": "Expired token: request returns 401", "status": "passed"}
        ]
    )
    with pytest.raises(ResultContractError, match="invalid_acceptance"):
        parse_worker_result(
            {"runs": [{"status": "succeeded", "metadata": {"tpo_result": result}}]},
            tick_id="01TICK",
            todo_id="TODO-42",
            step_key="plan:task-1",
            acceptance_criteria=("Observable criterion",),
        )


def test_delivery_evidence_accepts_only_successful_checks_and_exact_pr_identity():
    delivery = {
        "pr_url": "https://github.com/acme/repo/pull/7",
        "branch": "feat/native",
        "head_sha": "b" * 40,
        "checks": [{"command": "uv run pytest", "exit_code": 0}],
    }
    result = _result(delivery=delivery)
    parsed = parse_worker_result(
        {"runs": [{"status": "succeeded", "metadata": {"tpo_result": result}}]},
        tick_id="01TICK",
        todo_id="TODO-42",
        step_key="plan:task-1",
        acceptance_criteria=("Observable criterion",),
    )
    assert parsed.delivery.pr_url.endswith("/pull/7")
    assert parsed.delivery.checks[0].exit_code == 0

    for mutation in (
        lambda value: value.update(pr_url="https://github.com/acme/repo/issues/7"),
        lambda value: value.update(pr_url="https://evil.example/acme/repo/pull/7"),
        lambda value: value.update(pr_url="https://github.com/acme/repo/pull/7?x=1"),
        lambda value: value.update(pr_url="https://github.com/acme/repo/extra/pull/7"),
        lambda value: value.update(head_sha="short"),
        lambda value: value.update(checks=[]),
        lambda value: value.update(
            checks=[{"command": "uv run pytest", "exit_code": 1}]
        ),
        lambda value: value.update(unexpected="x"),
    ):
        invalid = dict(delivery)
        mutation(invalid)
        with pytest.raises(ResultContractError, match="invalid_delivery"):
            parse_worker_result(
                {
                    "runs": [
                        {
                            "status": "succeeded",
                            "metadata": {"tpo_result": _result(delivery=invalid)},
                        }
                    ]
                },
                tick_id="01TICK",
                todo_id="TODO-42",
                step_key="plan:task-1",
                acceptance_criteria=("Observable criterion",),
            )


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def test_verify_git_requires_exactly_one_commit_and_matching_changed_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    parent = _git(repo, "rev-parse", "HEAD")
    (repo / "change.txt").write_text("change")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "change")
    head = _git(repo, "rev-parse", "HEAD")
    result = _result()
    result["git"] = {
        "expected_parent_sha": parent,
        "resulting_head_sha": head,
        "task_commit_sha": head,
        "changed_files": ["change.txt"],
    }
    parsed = parse_worker_result(
        {"runs": [{"status": "succeeded", "metadata": {"tpo_result": result}}]},
        tick_id="01TICK",
        todo_id="TODO-42",
        step_key="plan:task-1",
        acceptance_criteria=("Observable criterion",),
    )
    verify_worker_git_result(repo, parsed.git, expected_parent_sha=parent)
    with pytest.raises(ResultContractError, match="changed_files_mismatch"):
        verify_worker_git_result(
            repo,
            parsed.git.__class__(parent, head, head, ("wrong.txt",)),
            expected_parent_sha=parent,
        )
    (repo / "untracked.txt").write_text("keep")
    with pytest.raises(ResultContractError, match="worktree_dirty"):
        verify_worker_git_result(repo, parsed.git, expected_parent_sha=parent)


def _repo(tmp_path, name):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


def _add_commit(repo, name):
    (repo / name).write_text(name)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", name)
    return _git(repo, "rev-parse", "HEAD")


def _git_block(parent, head, changed):
    from hermes_pipeline.result_contract import GitResult

    return GitResult(parent, head, head, tuple(changed))


def test_optional_single_commit_accepts_no_commit_and_exactly_one(tmp_path):
    """``phase_5_review`` and ``phase_8_finish_branch`` may each add one commit.

    Or none: a review with no valid finding and a finish with no required
    metadata change are both legitimate, so the anchor bound is 0-or-1, not the
    exactly-one a Plan task owes.
    """
    repo = _repo(tmp_path, "optional-one")
    anchor = _git(repo, "rev-parse", "HEAD")

    verify_optional_single_commit(
        repo, _git_block(anchor, anchor, ()), expected_parent_sha=anchor
    )

    head = _add_commit(repo, "metadata.txt")
    verify_optional_single_commit(
        repo, _git_block(anchor, head, ("metadata.txt",)), expected_parent_sha=anchor
    )


def test_optional_single_commit_rejects_a_second_commit(tmp_path):
    """One commit is the whole allowance; two is unreviewed work riding along."""
    repo = _repo(tmp_path, "optional-two")
    anchor = _git(repo, "rev-parse", "HEAD")
    _add_commit(repo, "first.txt")
    head = _add_commit(repo, "second.txt")

    with pytest.raises(ResultContractError, match="commit_count_mismatch"):
        verify_optional_single_commit(
            repo, _git_block(anchor, head, ("first.txt", "second.txt")),
            expected_parent_sha=anchor,
        )


def test_optional_single_commit_rejects_a_head_off_the_anchor(tmp_path):
    """A head that does not descend from the anchor is a forged history."""
    repo = _repo(tmp_path, "optional-forked")
    anchor = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "sidetrack", anchor + "^{commit}")
    _git(repo, "checkout", "-q", "--orphan", "elsewhere")
    _git(repo, "rm", "-q", "-rf", ".")
    foreign = _add_commit(repo, "foreign.txt")

    with pytest.raises(ResultContractError, match="parent_mismatch"):
        verify_optional_single_commit(
            repo, _git_block(anchor, foreign, ("foreign.txt",)),
            expected_parent_sha=anchor,
        )


def test_optional_single_commit_rejects_a_claimed_commit_it_did_not_make(tmp_path):
    """The reported changed files must be the commit's real diff."""
    repo = _repo(tmp_path, "optional-lying")
    anchor = _git(repo, "rev-parse", "HEAD")
    head = _add_commit(repo, "real.txt")

    with pytest.raises(ResultContractError, match="changed_files_mismatch"):
        verify_optional_single_commit(
            repo, _git_block(anchor, head, ("claimed.txt",)),
            expected_parent_sha=anchor,
        )
    # And a "changed nothing" claim cannot sit on an advanced head.
    with pytest.raises(ResultContractError, match="changed_files_mismatch"):
        verify_optional_single_commit(
            repo, _git_block(anchor, head, ()), expected_parent_sha=anchor
        )
    # The mirror image, and the untested half of the zero-commit branch's
    # ``git.resulting_head_sha != expected_parent_sha or git.changed_files``:
    # the head really IS the anchor, so no commit was made, but the report
    # claims a file changed anyway. Every other zero-commit case passes an empty
    # list, so dropping ``or git.changed_files`` survived them all.
    with pytest.raises(ResultContractError, match="changed_files_mismatch"):
        verify_optional_single_commit(
            repo, _git_block(anchor, anchor, ("invented.py",)),
            expected_parent_sha=anchor,
        )


def test_historical_git_topology_remains_valid_after_later_fix_commit(tmp_path):
    repo = tmp_path / "repo-history"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    parent = _git(repo, "rev-parse", "HEAD")
    (repo / "first.txt").write_text("first")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "first")
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "later.txt").write_text("later")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "later")

    from hermes_pipeline.result_contract import GitResult

    verify_worker_git_topology(
        repo,
        GitResult(parent, first, first, ("first.txt",)),
        expected_parent_sha=parent,
    )
    with pytest.raises(ResultContractError, match="head_mismatch"):
        verify_worker_git_result(
            repo,
            GitResult(parent, first, first, ("first.txt",)),
            expected_parent_sha=parent,
        )


@pytest.mark.skipif(os.name == "nt", reason="byte filenames require POSIX")
def test_verify_git_reports_invalid_byte_untracked_filename_as_dirty(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    parent = _git(repo, "rev-parse", "HEAD")
    (repo / "change.txt").write_text("change")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "change")
    head = _git(repo, "rev-parse", "HEAD")
    try:
        fd = os.open(
            os.fsencode(repo) + b"/invalid-\xff", os.O_WRONLY | os.O_CREAT, 0o600
        )
    except OSError as exc:
        pytest.skip(f"filesystem rejects invalid-byte filenames: errno={exc.errno}")
    os.close(fd)
    git_result = _result()["git"]
    git_result.update(
        expected_parent_sha=parent,
        resulting_head_sha=head,
        task_commit_sha=head,
        changed_files=["change.txt"],
    )
    parsed = parse_worker_result(
        {"runs": [{"status": "succeeded", "metadata": {"tpo_result": {**_result(), "git": git_result}}}]},
        tick_id="01TICK",
        todo_id="TODO-42",
        step_key="plan:task-1",
        acceptance_criteria=("Observable criterion",),
    )
    with pytest.raises(ResultContractError, match="worktree_dirty"):
        verify_worker_git_result(repo, parsed.git, expected_parent_sha=parent)


def test_registration_rejects_unknown_keys_and_mutable_plan_drift(tmp_path):
    repo, worktree, state, _parent = _registered_repo(tmp_path)
    registration = state / "runs" / "01TICK" / "registration.json"
    payload = json.loads(registration.read_text())
    payload["unknown"] = True
    registration.write_text(json.dumps(payload))
    with pytest.raises(ResultContractError, match="registration_invalid"):
        load_validated_registration(repo, state, "01TICK")

    del payload["unknown"]
    registration.write_text(json.dumps(payload))
    (worktree / "plan.md").write_text("mutable drift")
    authority = load_validated_registration(repo, state, "01TICK")
    assert authority.manifest.tasks[0].id == "task-1"


def _registered_repo(
    tmp_path, *, issue_body: str = ISSUE_BODY, plan_path: str | None = "plan.md",
    embedded: bool = False, plan: str = PLAN,
    step_keys: tuple[str, ...] = (IMPLEMENTATION_KEY,),
):
    from hermes_pipeline.plan_manifest import render_embedded_plan
    from hermes_pipeline.run_registration import register_pinned_run

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "remote", "add", "origin", f"git@github.com:{REPO}.git")
    (repo / "plan.md").write_text(plan)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    parent = _git(repo, "rev-parse", "HEAD")
    state = repo / ".hermes"
    if embedded:
        issue_body = ISSUE_BODY.replace("### Plan\n\nplan.md\n\n", "")
        issue_body += render_embedded_plan(plan, expected_todo_id="TODO-42")
        plan_path = None
    registration = register_pinned_run(
        project_dir=repo,
        state_dir=state,
        tick_id="01TICK",
        selected_issue=make_issue(42, repo=REPO, title="Do it", body=issue_body),
        plan_path=plan_path,
        profile="native-sdd",
        prompt_client="claude",
        assignee="pipeline",
        review_assignee=None,
        step_keys=step_keys,
    )
    return repo, registration.worktree, state, parent


def _rewrite_registration(state, mutate):
    path = state / "runs" / "01TICK" / "registration.json"
    payload = json.loads(path.read_text())
    mutate(payload)
    path.write_text(json.dumps(payload))
    return payload


def test_registration_authority_is_the_issue_snapshot(tmp_path):
    repo, worktree, state, parent = _registered_repo(tmp_path)

    authority = load_validated_registration(repo, state, "01TICK")

    assert authority.issue_number == 42
    assert authority.issue_url == "https://github.com/acme/repo/issues/42"
    assert authority.branch == "todo-42"
    assert authority.plan_path == "plan.md"
    assert authority.worktree == (repo / ".worktrees" / "todo-42-do-it").resolve()
    assert authority.manifest.tasks[0].id == "task-1"
    assert authority.plan_hash == json.loads(
        (state / "runs" / "01TICK" / "registration.json").read_text()
    )["plan_hash"]
    assert not (repo / "TODOS.md").exists()


def test_registration_accepts_step_keys_beyond_the_plan_tasks(tmp_path):
    """``required_steps <= steps`` is deliberate, in both directions.

    A profile that registers phases the manifest does not name, and a run
    registered before the per-task controller gate was dropped, both carry extra
    keys. Neither may be rejected -- an equality check here would refuse to load
    an in-flight run's own authority. The other direction still fails closed:
    a registration missing the implementation card's key cannot be verified.
    """
    extra = (IMPLEMENTATION_KEY, "review", "finish", "human", "validate:task-1")
    repo, _worktree, state, _parent = _registered_repo(tmp_path, step_keys=extra)

    authority = load_validated_registration(repo, state, "01TICK")

    assert authority.step_keys == extra

    _rewrite_registration(state, lambda payload: payload.__setitem__("step_keys", ["review"]))
    with pytest.raises(ResultContractError, match="registration_invalid"):
        load_validated_registration(repo, state, "01TICK")


def test_embedded_registration_exposes_verified_artifact_reference(tmp_path):
    from hermes_pipeline.plan_manifest import render_embedded_plan
    from hermes_pipeline.run_registration import register_pinned_run

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "remote", "add", "origin", f"git@github.com:{REPO}.git")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    body = ISSUE_BODY.replace("### Plan\n\nplan.md\n\n", "")
    body += render_embedded_plan(PLAN, expected_todo_id="TODO-42")
    state = repo / ".hermes"
    registration = register_pinned_run(
        project_dir=repo, state_dir=state, tick_id="01TICK",
        selected_issue=make_issue(42, repo=REPO, title="Do it", body=body),
        plan_path=None, profile="native-sdd", prompt_client="claude",
        assignee="pipeline", review_assignee=None,
        step_keys=(IMPLEMENTATION_KEY,),
    )

    authority = load_validated_registration(repo, state, "01TICK")

    artifact = (state / "runs" / "01TICK" / "plan.md").resolve()
    assert authority.plan_source_kind == "embedded"
    assert authority.plan_path is None
    assert authority.plan_reference is not None
    assert authority.plan_reference.value == str(artifact)
    assert authority.plan_source is not None
    assert authority.plan_source.kind == "embedded"
    assert Path(authority.plan_reference.value).read_text() == PLAN
    assert registration.plan_path is None


def test_embedded_plan_result_validation_scenario(tmp_path):
    repo, _worktree, state, _parent = _registered_repo(tmp_path, embedded=True)

    authority = load_validated_registration(repo, state, "01TICK", repo=REPO)

    assert authority.plan_source is not None
    assert authority.plan_source.kind == "embedded"
    assert authority.manifest is not None
    assert authority.manifest.tasks[0].id == "task-1"


def test_embedded_plan_reconciliation_scenario(tmp_path, mocker):
    from hermes_pipeline.kanban_tasks import reconcile_plan_task_results

    repo, _worktree, state, _parent = _registered_repo(tmp_path, embedded=True)
    mocker.patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
        return_value={
            IMPLEMENTATION_KEY: SimpleNamespace(task_id="worker", status="todo"),
        },
    )

    assert reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK", repo=REPO
    )


def test_embedded_plan_closeout_scenario(tmp_path, mocker):
    from hermes_pipeline.todos_completion import reconcile_todo_completion

    repo, _worktree, state, _parent = _registered_repo(tmp_path, embedded=True)
    mocker.patch("hermes_pipeline.todos_completion.get_todo_kanban_tasks", return_value={})

    assert reconcile_todo_completion(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK", repo=REPO
    )


def test_doctor_accepts_valid_embedded_artifact(tmp_path, mocker, capsys):
    from hermes_pipeline.cli import _doctor_active_registration

    repo, _worktree, state, _parent = _registered_repo(tmp_path, embedded=True)
    (state / "current_tick_id.txt").write_text("01TICK\n")
    mocker.patch("hermes_pipeline.github_issues.check_issue_drift", return_value=None)

    assert _doctor_active_registration(repo, state)
    assert "Issue authority: pinned" in capsys.readouterr().out


def test_doctor_surfaces_a_blocked_result_validation(tmp_path, mocker, capsys):
    """``tpo doctor`` is where an operator looks; the stall must be visible there.

    It is a stalled run, not corrupt authority, so the verdict is unchanged.
    """
    from hermes_pipeline.cli import _doctor_active_registration
    from hermes_pipeline.kanban_tasks import RESULT_VALIDATION_BLOCKED_MARKER

    repo, _worktree, state, _parent = _registered_repo(tmp_path, embedded=True)
    (state / "current_tick_id.txt").write_text("01TICK\n")
    (state / "runs" / "01TICK" / RESULT_VALIDATION_BLOCKED_MARKER).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tick_id": "01TICK",
                "step_key": "plan:task-1",
                "code": "worktree_dirty",
                "reason": "worktree_dirty",
            }
        )
        + "\n"
    )
    mocker.patch("hermes_pipeline.github_issues.check_issue_drift", return_value=None)

    assert _doctor_active_registration(repo, state)
    out = capsys.readouterr().out
    assert "RESULT VALIDATION BLOCKED: plan:task-1 worktree_dirty" in out
    assert RESULT_VALIDATION_BLOCKED_MARKER in out


def test_doctor_rejects_embedded_artifact_digest_drift(tmp_path, mocker, capsys):
    from hermes_pipeline.cli import _doctor_active_registration

    repo, _worktree, state, _parent = _registered_repo(tmp_path, embedded=True)
    (state / "current_tick_id.txt").write_text("01TICK\n")
    (state / "runs" / "01TICK" / "plan.md").write_text("drift\n")
    mocker.patch("hermes_pipeline.github_issues.check_issue_drift", return_value=None)

    assert not _doctor_active_registration(repo, state)
    assert "REGISTRATION DRIFT: plan_hash" in capsys.readouterr().out


def test_registration_rejects_schema_v1_payload(tmp_path):
    repo, _worktree, state, _parent = _registered_repo(tmp_path)

    def downgrade(payload):
        payload["schema_version"] = 1
        for key in ("issue_number", "issue_url", "issue_snapshot"):
            del payload[key]

    _rewrite_registration(state, downgrade)
    with pytest.raises(ResultContractError, match="registration_invalid.*schema_version"):
        load_validated_registration(repo, state, "01TICK")


def _retitle(payload):
    payload["issue_snapshot"] = payload["issue_snapshot"].replace("title: Do it", "title: Other")


def _renumber(payload):
    payload["issue_snapshot"] = payload["issue_snapshot"].replace("number: 42", "number: 43")


def _rebody(payload):
    payload["issue_snapshot"] = payload["issue_snapshot"].replace("todo-42", "todo-43")


def _replan(payload):
    payload["issue_snapshot"] = payload["issue_snapshot"].replace("\nplan.md\n", "\nother.md\n")


def _rehash(mutate):
    def apply(payload):
        mutate(payload)
        payload["selected_entry_hash"] = snapshot_hash(payload["issue_snapshot"])

    return apply


def _consistent_renumber(payload):
    _renumber(payload)
    payload["issue_number"] = 43
    payload["issue_url"] = "https://github.com/acme/repo/issues/43"
    payload["selected_entry_hash"] = snapshot_hash(payload["issue_snapshot"])


def _foreign_repo(payload):
    payload["issue_snapshot"] = canonical_issue_snapshot("other/repo", 42, "Do it", ISSUE_BODY)
    payload["issue_url"] = "https://github.com/other/repo/issues/42"
    payload["selected_entry_hash"] = snapshot_hash(payload["issue_snapshot"])


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_renumber, id="tampered-number-line"),
        pytest.param(_rebody, id="tampered-body"),
        pytest.param(_retitle, id="tampered-title"),
        pytest.param(_rehash(_renumber), id="rehashed-number-mismatch"),
        pytest.param(_rehash(_rebody), id="rehashed-branch-from-snapshot"),
        pytest.param(_rehash(_replan), id="rehashed-plan-from-snapshot"),
        pytest.param(_rehash(_retitle), id="rehashed-worktree-slug-from-title"),
        pytest.param(_consistent_renumber, id="todo-id-mismatch"),
        pytest.param(_foreign_repo, id="foreign-repo"),
        pytest.param(
            lambda payload: payload.update(issue_url="https://github.com/acme/repo/issues/7"),
            id="url-mismatch",
        ),
        pytest.param(
            lambda payload: payload.update(issue_snapshot="not a snapshot\n"),
            id="malformed-snapshot",
        ),
    ],
)
def test_registration_rejects_snapshot_tampering(tmp_path, mutate):
    repo, _worktree, state, _parent = _registered_repo(tmp_path)
    _rewrite_registration(state, mutate)

    with pytest.raises(ResultContractError, match="registration_invalid"):
        load_validated_registration(repo, state, "01TICK")


def test_registration_rejects_stale_hash_after_body_edit(tmp_path):
    repo, _worktree, state, _parent = _registered_repo(tmp_path)
    _rewrite_registration(
        state,
        lambda payload: payload.update(
            issue_snapshot=payload["issue_snapshot"].replace(
                "Result contract.", "Result contract!"
            )
        ),
    )

    with pytest.raises(ResultContractError, match="issue snapshot hash"):
        load_validated_registration(repo, state, "01TICK")


def test_registration_bounds_snapshot_size_instead_of_scanning_it(tmp_path):
    repo, _worktree, state, _parent = _registered_repo(
        tmp_path, issue_body=ISSUE_BODY + "\n### Why\n\ntoken: ghp_abcdefghijklmnopqrstuvwxyz\x0c\n"
    )
    assert load_validated_registration(repo, state, "01TICK").issue_number == 42

    def oversize(payload):
        payload["issue_snapshot"] = payload["issue_snapshot"] + "x" * MAX_ISSUE_SNAPSHOT_CHARS
        payload["selected_entry_hash"] = snapshot_hash(payload["issue_snapshot"])

    _rewrite_registration(state, oversize)
    with pytest.raises(ResultContractError, match="registration_invalid"):
        load_validated_registration(repo, state, "01TICK")


def test_registration_repo_identity_is_case_insensitive(tmp_path):
    repo, _worktree, state, _parent = _registered_repo(tmp_path)
    _git(repo, "remote", "set-url", "origin", "git@github.com:ACME/REPO.git")

    assert load_validated_registration(repo, state, "01TICK").issue_number == 42


def test_registration_repo_must_match_live_identity(tmp_path):
    repo, _worktree, state, _parent = _registered_repo(tmp_path)

    load_validated_registration(repo, state, "01TICK", repo=REPO)
    with pytest.raises(ResultContractError, match="registration_invalid"):
        load_validated_registration(repo, state, "01TICK", repo="other/repo")
    _git(repo, "remote", "remove", "origin")
    with pytest.raises(ResultContractError, match="git_verification_failed"):
        load_validated_registration(repo, state, "01TICK")


def _commit(worktree, name: str) -> str:
    (worktree / name).write_text(name)
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-qm", name)
    return _git(worktree, "rev-parse", "HEAD")


def _worker_payload(*, step_key: str, parent: str, head: str, changed: list[str]):
    result = _result(step_key=step_key)
    result["git"] = {
        "expected_parent_sha": parent,
        "resulting_head_sha": head,
        "task_commit_sha": head,
        "changed_files": changed,
    }
    # Hermes stamps ``worker_session_id`` on its own tool path only; live runs
    # also carry worker-authored siblings. The reconciler ignores them all.
    return {
        "runs": [
            {
                "status": "succeeded",
                "metadata": {
                    "tpo_result": result,
                    "worker_session_id": "20260904_154528_b0f01d",
                },
            }
        ]
    }


def test_reconcile_completed_worker_validates_without_a_controller_gate(
    tmp_path, mocker
):
    """A validated worker needs no gate card: nothing is completed or blocked."""
    from hermes_pipeline.kanban_tasks import KanbanTaskInfo, reconcile_plan_task_results

    repo, worktree, state, parent = _registered_repo(tmp_path)
    head = _commit(worktree, "change.txt")
    mocker.patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
        return_value={
            IMPLEMENTATION_KEY: KanbanTaskInfo(
                "worker", IMPLEMENTATION_KEY, "done", "TODO-42"
            ),
        },
    )
    mocker.patch(
        "hermes_pipeline.kanban_tasks._show_task_payload",
        return_value=_worker_payload(
            step_key=IMPLEMENTATION_KEY, parent=parent, head=head,
            changed=["change.txt"],
        ),
    )
    complete = mocker.patch(
        "hermes_pipeline.kanban_tasks.complete_todo_kanban_task", return_value=True
    )

    for _ in range(2):  # Reconciliation is idempotent across ticks.
        assert reconcile_plan_task_results(
            project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
        )
    complete.assert_not_called()


def test_reconcile_requires_exactly_one_commit_per_plan_task(tmp_path, mocker):
    """The bound is the Plan's task count, taken from the profile's own words.

    ``phase_4_development`` tells the implementation agent to "create exactly
    one atomic commit per Plan task", so a two-task Plan owes exactly two
    commits measured from ``base_sha`` -- not one, and not three. Deleting the
    per-task fan-out removed the per-commit anchor chain that used to prove this
    one task at a time; this is the replacement, and it is derived from the
    profile rather than loosened to "at least one".
    """
    from hermes_pipeline.kanban_tasks import KanbanTaskInfo, reconcile_plan_task_results

    repo, worktree, state, base = _registered_repo(tmp_path, plan=PLAN_TWO_TASKS)
    _commit(worktree, "one.txt")
    second = _commit(worktree, "two.txt")
    board = {
        IMPLEMENTATION_KEY: KanbanTaskInfo(
            "worker", IMPLEMENTATION_KEY, "done", "TODO-42"
        )
    }
    mocker.patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
        side_effect=lambda *_args: board,
    )

    def payload(*, head, changed):
        result = _worker_payload(
            step_key=IMPLEMENTATION_KEY, parent=base, head=head, changed=changed
        )
        # One card answers for every task's criteria, in Plan order.
        result["runs"][0]["metadata"]["tpo_result"]["acceptance"] = [
            {"criterion": "Observable criterion", "status": "passed"},
            {"criterion": "Observable criterion", "status": "passed"},
        ]
        return result

    show = mocker.patch(
        "hermes_pipeline.kanban_tasks._show_task_payload",
        return_value=payload(head=second, changed=["one.txt", "two.txt"]),
    )
    assert reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
    )

    marker = state / "runs" / "01TICK" / "result-validation-blocked"
    # One commit for two tasks is short of what the profile obliges, and it must
    # read as the worker's fault -- not as a git that could not answer.
    first_only = _git(worktree, "rev-parse", "HEAD~1")
    show.return_value = payload(head=first_only, changed=["one.txt"])
    assert not reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    assert json.loads(marker.read_text())["code"] == "commit_count_mismatch"

    # A third commit is one more than the Plan asked for.
    third = _commit(worktree, "three.txt")
    marker.unlink()
    show.return_value = payload(
        head=third, changed=["one.txt", "three.txt", "two.txt"]
    )
    assert not reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    assert json.loads(marker.read_text())["code"] == "commit_count_mismatch"


def test_reconcile_falls_back_to_topology_once_review_builds_on_the_chain(
    tmp_path, mocker
):
    """Review-fix commits advance HEAD; the chain must stay reconcilable."""
    from hermes_pipeline.kanban_tasks import KanbanTaskInfo, reconcile_plan_task_results

    repo, worktree, state, base = _registered_repo(tmp_path)
    head = _commit(worktree, "change.txt")
    mocker.patch(
        "hermes_pipeline.kanban_tasks._show_task_payload",
        return_value=_worker_payload(
            step_key=IMPLEMENTATION_KEY, parent=base, head=head,
            changed=["change.txt"],
        ),
    )
    board = {
        IMPLEMENTATION_KEY: KanbanTaskInfo(
            "worker", IMPLEMENTATION_KEY, "done", "TODO-42"
        ),
    }
    tasks = mocker.patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
        side_effect=lambda *_args: board,
    )
    _commit(worktree, "review-fix.txt")

    assert not reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
    )

    board["review:0"] = KanbanTaskInfo("review", "review:0", "done", "TODO-42")
    assert reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    assert tasks.called


def test_topology_rejects_a_commit_no_longer_reachable_from_head(tmp_path):
    """Parentage, count and changed files all still hold for a discarded commit.

    ``git reset --hard`` leaves the object in the repository, so every immutable
    topology fact keeps passing; only reachability proves the work is on the
    branch the run is delivering.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    parent = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "change.txt")
    _git(repo, "reset", "--hard", "-q", parent)
    git = parse_worker_result(
        _worker_payload(
            step_key="plan:task-1", parent=parent, head=head, changed=["change.txt"]
        ),
        tick_id="01TICK", todo_id="TODO-42", step_key="plan:task-1",
        acceptance_criteria=("Observable criterion",),
    ).git

    with pytest.raises(ResultContractError, match="unreachable_commit"):
        verify_worker_git_topology(repo, git, expected_parent_sha=parent)


def test_reconcile_rejects_a_discarded_commit_even_when_a_decoy_review_card_exists(
    tmp_path, mocker, caplog
):
    """A board card can be forged by a worker that knows its own tick id.

    ``review:0`` only decides which *extra* checks apply; the ancestry anchor is
    unconditional, so a decoy cannot strip verification off a task.
    """
    from hermes_pipeline.kanban_tasks import KanbanTaskInfo, reconcile_plan_task_results

    repo, worktree, state, parent = _registered_repo(tmp_path)
    head = _commit(worktree, "change.txt")
    _git(worktree, "reset", "--hard", "-q", parent)
    mocker.patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
        return_value={
            IMPLEMENTATION_KEY: KanbanTaskInfo(
                "worker", IMPLEMENTATION_KEY, "done", "TODO-42"
            ),
            "review:0": KanbanTaskInfo("decoy", "review:0", "done", "TODO-42"),
        },
    )
    mocker.patch(
        "hermes_pipeline.kanban_tasks._show_task_payload",
        return_value=_worker_payload(
            step_key=IMPLEMENTATION_KEY, parent=parent, head=head,
            changed=["change.txt"],
        ),
    )

    with caplog.at_level(logging.ERROR, logger="hermes_pipeline.kanban_tasks"):
        assert not reconcile_plan_task_results(
            project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
        )

    assert "unreachable_commit" in caplog.text


def test_reconcile_records_a_durable_blocked_marker_and_clears_it_on_success(
    tmp_path, mocker
):
    """Plain cron ``tpo tick`` has no budget: the stall must leave evidence."""
    from hermes_pipeline.kanban_tasks import (
        RESULT_VALIDATION_BLOCKED_MARKER,
        KanbanTaskInfo,
        reconcile_plan_task_results,
    )

    repo, worktree, state, parent = _registered_repo(tmp_path)
    head = _commit(worktree, "change.txt")
    board = {
        IMPLEMENTATION_KEY: KanbanTaskInfo(
            "worker", IMPLEMENTATION_KEY, "done", "TODO-42"
        )
    }
    mocker.patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
        side_effect=lambda *_args: board,
    )
    payload = mocker.patch(
        "hermes_pipeline.kanban_tasks._show_task_payload", return_value={"runs": []}
    )
    marker = state / "runs" / "01TICK" / RESULT_VALIDATION_BLOCKED_MARKER

    for _ in range(2):  # Repeated no-progress ticks converge on one marker.
        assert not reconcile_plan_task_results(
            project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
        )
    recorded = json.loads(marker.read_text())
    assert recorded["step_key"] == IMPLEMENTATION_KEY
    assert recorded["code"] == "missing_successful_run"
    assert recorded["tick_id"] == "01TICK"
    assert isinstance(recorded["reason"], str) and recorded["reason"]

    payload.return_value = _worker_payload(
        step_key=IMPLEMENTATION_KEY, parent=parent, head=head, changed=["change.txt"]
    )
    assert reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    assert not marker.exists()


def test_git_predicate_reports_a_real_git_failure_rather_than_false(tmp_path):
    """Exit 1 is "no"; anything above it is a broken git, not an answer.

    Collapsing the two would let an unusable repository read as a clean
    non-ancestor verdict, which is the wrong direction to fail in.
    """
    from hermes_pipeline.result_contract import _git_predicate

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    head = _git(repo, "rev-parse", "HEAD")

    assert _git_predicate(repo, "merge-base", "--is-ancestor", head, "HEAD") is True

    with pytest.raises(ResultContractError, match="git_verification_failed"):
        _git_predicate(repo, "merge-base", "--is-ancestor", "0" * 40, "HEAD")


def test_reconcile_records_a_structural_marker_when_the_chain_is_not_wired(
    tmp_path, mocker
):
    """A missing card stalls the run exactly like a rejected result.

    The code has to say which, or an operator cannot tell a wiring problem from
    work TPO refused.
    """
    from hermes_pipeline.kanban_tasks import (
        CHAIN_WIRING_INCOMPLETE_CODE,
        reconcile_plan_task_results,
    )

    repo, _worktree, state, _parent = _registered_repo(tmp_path)
    marker = state / "runs" / "01TICK" / "result-validation-blocked"
    board: dict[str, object] = {}
    mocker.patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
        side_effect=lambda *_args: board,
    )

    # No implementation card on the board at all.
    assert not reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    recorded = json.loads(marker.read_text())
    assert recorded["code"] == CHAIN_WIRING_INCOMPLETE_CODE
    assert recorded["step_key"] == IMPLEMENTATION_KEY


def test_reconcile_records_a_marker_when_a_registered_step_key_is_absent(
    tmp_path, mocker
):
    from hermes_pipeline.kanban_tasks import (
        CHAIN_WIRING_INCOMPLETE_CODE,
        reconcile_plan_task_results,
    )

    repo, worktree, state, parent = _registered_repo(tmp_path)
    mocker.patch(
        "hermes_pipeline.result_contract.load_validated_registration",
        return_value=SimpleNamespace(
            manifest=SimpleNamespace(tasks=(SimpleNamespace(id="task-1"),)),
            step_keys=("plan:task-1",),
            base_sha=parent,
            todo_id="TODO-42",
            worktree=worktree,
        ),
    )
    tasks = mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_tasks")

    assert not reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    recorded = json.loads(
        (state / "runs" / "01TICK" / "result-validation-blocked").read_text()
    )
    assert recorded["code"] == CHAIN_WIRING_INCOMPLETE_CODE
    assert recorded["step_key"] == IMPLEMENTATION_KEY
    assert tasks.called


def test_reconcile_invalid_result_reports_no_progress_without_a_blocking_card(
    tmp_path, mocker, caplog
):
    from hermes_pipeline.kanban_tasks import KanbanTaskInfo, reconcile_plan_task_results

    repo, _worktree, state, _parent = _registered_repo(tmp_path)
    mocker.patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
        return_value={
            IMPLEMENTATION_KEY: KanbanTaskInfo(
                "worker", IMPLEMENTATION_KEY, "done", "TODO-42"
            ),
        },
    )
    mocker.patch("hermes_pipeline.kanban_tasks._show_task_payload", return_value={"runs": []})
    complete = mocker.patch("hermes_pipeline.kanban_tasks.complete_todo_kanban_task")

    with caplog.at_level(logging.ERROR, logger="hermes_pipeline.kanban_tasks"):
        assert not reconcile_plan_task_results(
            project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
        )

    assert "TPO result validation failed" in caplog.text
    assert IMPLEMENTATION_KEY in caplog.text
    complete.assert_not_called()


def test_legacy_registration_bypasses_manifest_only_reconciliation(tmp_path, mocker):
    from hermes_pipeline.kanban_tasks import reconcile_plan_task_results
    from hermes_pipeline.review_reconciliation import reconcile_reviews
    from hermes_pipeline.run_registration import register_pinned_run
    from hermes_pipeline.todos_completion import reconcile_todo_completion

    repo = tmp_path / "legacy"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "remote", "add", "origin", f"https://github.com/{REPO}")
    (repo / "plan.md").write_text("# Legacy Plan\n\nImplement it.\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    state = repo / ".hermes"
    registration = register_pinned_run(
        project_dir=repo,
        state_dir=state,
        tick_id="LEGACY-TICK",
        selected_issue=make_issue(
            7,
            repo=REPO,
            title="Legacy work",
            body="### Plan\n\nplan.md\n\n### Branch\n\ntodo-7\n",
        ),
        plan_path="plan.md",
        profile="native-sdd",
        prompt_client="claude",
        assignee="pipeline",
        review_assignee=None,
        step_keys=(
            "phase_4_development",
            "phase_5_review",
            "phase_8_finish_branch",
            "phase_9_human_review",
        ),
    )

    authority = load_validated_registration(repo, state, "LEGACY-TICK")
    assert authority.manifest is None
    kanban = mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_tasks")
    review = mocker.patch("hermes_pipeline.review_reconciliation.get_todo_kanban_tasks")
    delivery = mocker.patch("hermes_pipeline.todos_completion.get_todo_kanban_tasks")

    assert reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="legacy", tick_id="LEGACY-TICK"
    )
    assert reconcile_reviews(
        project_dir=repo, state_dir=state, tenant="legacy", tick_id="LEGACY-TICK"
    )
    assert reconcile_todo_completion(
        project_dir=repo, state_dir=state, tenant="legacy", tick_id="LEGACY-TICK", repo=REPO
    )
    kanban.assert_not_called()
    review.assert_not_called()
    delivery.assert_not_called()
    assert registration.worktree.is_dir()

    registration_path = state / "runs" / "LEGACY-TICK" / "registration.json"
    drifted = json.loads(registration_path.read_text())
    drifted["plan_hash"] = "0" * 64
    registration_path.write_text(json.dumps(drifted))
    with pytest.raises(ResultContractError, match="registration_invalid"):
        load_validated_registration(repo, state, "LEGACY-TICK")


# --- Published result-metadata template -------------------------------------
#
# The template is the only thing a worker is told about the contract, so these
# tests round-trip every rendered template through the real parser and assert
# the published keys are derived from the contract constants themselves.

_JSON_BLOCK_RE = re.compile(r"```json\n(.*?)\n```", re.DOTALL)
# Placeholders are detected with the contract's own pattern, so a template
# string these tests treat as a placeholder is exactly one the validator
# would reject as unfilled.

_TEMPLATE_FILL = {
    ("git", "expected_parent_sha"): "a" * 40,
    ("git", "resulting_head_sha"): "b" * 40,
    ("git", "task_commit_sha"): "b" * 40,
    ("git", "changed_files", 0): "src/example.py",
    ("delivery", "pr_url"): "https://github.com/acme/repo/pull/7",
    ("delivery", "head_sha"): "b" * 40,
    ("delivery", "checks", 0, "command"): "uv run pytest",
}


def _template_blocks(text: str) -> list[dict]:
    blocks = [json.loads(block) for block in _JSON_BLOCK_RE.findall(text)]
    assert blocks, f"template published no JSON block: {text}"
    return blocks


def _fill_template(value, path=()):
    """Substitute worker-supplied values for every ``<...>`` placeholder."""
    if isinstance(value, dict):
        return {key: _fill_template(item, (*path, key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill_template(item, (*path, index)) for index, item in enumerate(value)]
    if isinstance(value, str) and _PLACEHOLDER_RE.fullmatch(value):
        assert path in _TEMPLATE_FILL, f"unfillable placeholder at {path}: {value}"
        return _TEMPLATE_FILL[path]
    return value


def _template_envelope(result: dict) -> dict:
    return {"runs": [{"status": "succeeded", "metadata": {"tpo_result": result}}]}


def test_plan_template_round_trips_through_the_parser():
    from hermes_pipeline.result_contract import render_result_template

    text = render_result_template(
        tick_id="01TICK",
        todo_id="TODO-42",
        step_key="plan:task-1",
        acceptance_criteria=("Observable criterion",),
    )
    template, = _template_blocks(text)

    parsed = parse_worker_result(
        _template_envelope(_fill_template(template)),
        tick_id="01TICK",
        todo_id="TODO-42",
        step_key="plan:task-1",
        acceptance_criteria=("Observable criterion",),
    )

    assert parsed.git.changed_files == ("src/example.py",)
    assert parsed.step_key == "plan:task-1"
    # The criteria are pipeline-known facts, so the template pre-fills them.
    assert "<" not in json.dumps(template["acceptance"])
    # The failure path is the dispatcher's ``kanban_block`` protocol, stated
    # once in the delegation block. The template must not restate it as advice
    # to whoever is reading -- that instruction is what a card in ``needs_input``
    # is for.
    assert "do not report a result object" not in text


def test_delivery_template_round_trips_through_the_parser():
    from hermes_pipeline.result_contract import render_result_template

    text = render_result_template(
        tick_id="01TICK",
        todo_id="TODO-42",
        step_key="finish",
        section="delivery",
        branch="todo-42",
        allow_no_changes=True,
    )
    template, = _template_blocks(text)

    parsed = parse_worker_result(
        _template_envelope(_fill_template(template)),
        tick_id="01TICK", todo_id="TODO-42", step_key="finish",
        acceptance_criteria=(), allow_no_changes=True,
    )

    assert parsed.delivery is not None
    # The published pr_url placeholder describes the value, and still validates
    # once the worker replaces the whole token with the URL it opened.
    assert _PLACEHOLDER_RE.fullmatch(template["delivery"]["pr_url"])
    assert parsed.delivery.pr_url == "https://github.com/acme/repo/pull/7"
    # Delivery reconciliation demands the registered branch verbatim, and the
    # pushed head must be the head the worker's own git block reports. The head
    # is no longer pre-filled: ``phase_8_finish_branch`` may add one metadata
    # commit, so the delivered SHA is not knowable when the card is written.
    assert parsed.delivery.branch == "todo-42"
    assert _PLACEHOLDER_RE.fullmatch(template["delivery"]["head_sha"])
    assert parsed.delivery.head_sha == parsed.git.resulting_head_sha == "b" * 40
    # ``changed_files`` is a placeholder, never a pre-filled ``[]``. The card
    # instructs the worker to keep pre-filled values verbatim, and the profile
    # MANDATES a commit from ``phase_8_finish_branch``, so a pre-filled empty
    # list is a value an obedient worker keeps and a real diff then
    # contradicts. Filling the slot is what a compliant worker does.
    assert _PLACEHOLDER_RE.fullmatch(template["git"]["changed_files"][0])
    assert parsed.git.changed_files == ("src/example.py",)


def test_template_publishes_every_key_the_contract_constants_require():
    from hermes_pipeline import result_contract as contract

    plan, = _template_blocks(
        contract.render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="plan:task-1",
            acceptance_criteria=("Observable criterion",),
        )
    )
    assert set(plan) == contract._TOP_KEYS
    assert set(plan["git"]) == contract._GIT_KEYS
    assert set(plan["acceptance"][0]) == contract._ACCEPTANCE_ENTRY_KEYS

    # A review card publishes one block and no optional section: with the
    # review-round machinery gone, nothing reads a verdict or findings object,
    # so the contract carries none to be filled in or misread.
    review, = _template_blocks(
        contract.render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="review:0",
            allow_no_changes=True,
        )
    )
    assert set(review) == contract._TOP_KEYS
    assert set(review["git"]) == contract._GIT_KEYS

    delivery, = _template_blocks(
        contract.render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="finish",
            section="delivery", branch="todo-42", allow_no_changes=True,
        )
    )
    assert set(delivery) == contract._TOP_KEYS | {"delivery"}
    assert set(delivery["delivery"]) == contract._DELIVERY_KEYS
    assert set(delivery["delivery"]["checks"][0]) == contract._COMMAND_KEYS


def _template_placeholders(value, path=()):
    """Yield ``(path, described)`` for every ``<...>`` the renderer emits."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _template_placeholders(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _template_placeholders(item, (*path, index))
    elif isinstance(value, str) and _PLACEHOLDER_RE.fullmatch(value):
        yield path, value[1:-1]


_CAPS_RUN_RE = re.compile(r"[A-Z]{2,}")


def _metavariable_slots(described: str) -> list[str]:
    """Return the ALL-CAPS slots in a placeholder's text.

    A slot is an ALL-CAPS run standing for one *part* of the value, glued into
    a larger token: OWNER, REPO and NUMBER in
    ``https://github.com/OWNER/REPO/pull/NUMBER``. A standalone capitalised
    word -- SHA, HEAD, URL -- is domain vocabulary in an English sentence, not
    a slot: nothing can be substituted for it in place. Inside a path-like
    token a single capital (``.../pull/N``) is a slot too, which a bare ``P1``
    in prose is not.
    """
    slots: list[str] = []
    for word in described.split():
        if _CAPS_RUN_RE.fullmatch(word.strip(".,;:")):
            continue
        pattern = r"[A-Z]+" if "/" in word else r"[A-Z]{2,}"
        slots.extend(re.findall(pattern, word))
    return slots


# Every placeholder the renderer can publish, by path. Named exhaustively so
# that pre-filling or dropping one is caught rather than absorbed by a sibling
# that happens to share a field name.
_EXPECTED_PLACEHOLDER_PATHS = {
    ("git", "expected_parent_sha"),
    ("git", "resulting_head_sha"),
    ("git", "task_commit_sha"),
    ("git", "changed_files", 0),
    ("delivery", "pr_url"),
    # Not pre-filled: ``phase_8_finish_branch`` may add one metadata commit, so
    # the pushed head is not knowable when the card is written.
    ("delivery", "head_sha"),
    ("delivery", "checks", 0, "command"),
}


def test_template_placeholders_describe_the_value_instead_of_wrapping_it():
    """Every ``<...>`` must mean "replace this whole token", never "fill in the parts".

    A placeholder whose bracketed content is the value's own syntax with the
    parts renamed -- ``<https://github.com/OWNER/REPO/pull/NUMBER>`` -- teaches
    the opposite: a worker substitutes its real owner, repo and number in place,
    keeps the brackets, and publishes a natural-looking value the contract then
    rejects as an unfilled placeholder, blocking a delivery that happened.

    Two independent rules, both evaluated for every placeholder so neither can
    shadow the other: no metavariable slot, and more than one word of prose.
    """
    from hermes_pipeline import result_contract as contract

    blocks = [
        *_template_blocks(
            contract.render_result_template(
                tick_id="01TICK", todo_id="TODO-42", step_key="plan:task-1",
                acceptance_criteria=("Observable criterion",),
            )
        ),
        *_template_blocks(
            contract.render_result_template(
                tick_id="01TICK", todo_id="TODO-42", step_key="review:0",
                allow_no_changes=True,
            )
        ),
        *_template_blocks(
            contract.render_result_template(
                tick_id="01TICK", todo_id="TODO-42", step_key="finish",
                section="delivery", branch="todo-42", allow_no_changes=True,
            )
        ),
    ]
    placeholders = [pair for block in blocks for pair in _template_placeholders(block)]
    assert {path for path, _ in placeholders} == _EXPECTED_PLACEHOLDER_PATHS

    offences = []
    for path, described in placeholders:
        slots = _metavariable_slots(described)
        if slots:
            offences.append(
                f"{path} publishes the shape of the answer, with "
                f"{'/'.join(slots)} to fill in place: <{described}>"
            )
        # Cheap secondary smoke test: a description is prose about the value;
        # a single bare token is the value itself.
        if len(described.split()) < 2:
            offences.append(f"{path} publishes a bare token: <{described}>")
    assert not offences, "\n".join(offences)


def test_template_rejects_an_unknown_optional_section():
    from hermes_pipeline.result_contract import render_result_template

    with pytest.raises(ResultContractError, match="unknown_result_section"):
        render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="finish", section="nope",
        )


def test_parser_rejects_unfilled_template_placeholders():
    from hermes_pipeline.result_contract import render_result_template

    template, = _template_blocks(
        render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="plan:task-1",
            acceptance_criteria=("Observable criterion",),
        )
    )
    for path, code in (
        (("git", "expected_parent_sha"), "invalid_git"),
        (("git", "changed_files"), "invalid_git"),
    ):
        result = _fill_template(template)
        target = result
        for key in path[:-1]:
            target = target[key]
        # Restore the published placeholder for exactly one field.
        published = template
        for key in path:
            published = published[key]
        target[path[-1]] = published
        with pytest.raises(ResultContractError, match=code):
            parse_worker_result(
                _template_envelope(result),
                tick_id="01TICK", todo_id="TODO-42", step_key="plan:task-1",
                acceptance_criteria=("Observable criterion",),
            )


def test_bounded_string_allows_a_real_value_containing_angle_brackets():
    from hermes_pipeline.result_contract import _bounded_string

    assert _bounded_string(
        "uv run pytest -k 'a<b'", maximum=100, code="invalid_command"
    ) == "uv run pytest -k 'a<b'"


def test_delivery_template_requires_the_pipeline_known_branch_and_head():
    from hermes_pipeline.result_contract import render_result_template

    with pytest.raises(ResultContractError, match="incomplete_result_section"):
        render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="finish", section="delivery",
            allow_no_changes=True,
        )


@pytest.mark.parametrize(
    "summary",
    [
        pytest.param("<none>", id="bracketed-not-a-template-field"),
        pytest.param("\x1b[32mall tests passed\x1b[0m", id="ansi-colour"),
        pytest.param("token: expired", id="secret-shaped-prose"),
        # Hermes' own redactor rewrites "Token: refreshed" to this, which still
        # matched the secret pattern: redaction upstream made rejection likelier.
        pytest.param("Token: *** successfully", id="upstream-redacted"),
        pytest.param("x" * 100_000, id="longer-than-any-bound"),
        pytest.param({"unexpected": "shape"}, id="not-a-string"),
    ],
)
def test_summary_is_discarded_diagnostics_and_never_rejects_the_result(summary):
    """Hermes requires a closing summary; TPO reads it nowhere.

    It is not a template field and its value was already discarded after
    bounding, so scanning or bounding it could only convert healthy dispatcher
    output into a permanent wedge on an immutable closed run.
    """
    parsed = parse_worker_result(
        {"runs": [{"status": "succeeded", "summary": summary,
                   "metadata": {"tpo_result": _result()}}]},
        tick_id="01TICK", todo_id="TODO-42", step_key="plan:task-1",
        acceptance_criteria=("Observable criterion",),
    )
    assert parsed.step_key == "plan:task-1"


def test_every_rendered_placeholder_is_caught_by_the_placeholder_rule():
    from hermes_pipeline.result_contract import _PLACEHOLDER_RE, render_result_template

    texts = (
        render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="plan:task-1",
            acceptance_criteria=("Observable criterion",),
        ),
        render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="finish", section="delivery",
            branch="todo-42", allow_no_changes=True,
        ),
    )
    seen = 0
    for text in texts:
        for block in _template_blocks(text):
            for value in re.findall(r'"(<[^"]*>)"', json.dumps(block)):
                assert _PLACEHOLDER_RE.fullmatch(value), value
                seen += 1
    assert seen >= 10


def _all_templates():
    from hermes_pipeline.result_contract import render_result_template

    return (
        render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="plan:task-1",
            acceptance_criteria=("Observable criterion",),
        ),
        render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="finish", section="delivery",
            branch="todo-42", allow_no_changes=True,
        ),
    )


def test_template_addresses_the_dispatcher_that_closes_the_card():
    """The template never reaches the external client, so it speaks to Hermes.

    Only the dispatcher can write ``metadata.tpo_result`` at all, and it is the
    party that reads this block. Second-person instructions that only an
    implementation agent could follow are what leaked the schema across the
    prompt boundary in the first place.
    """
    client_only = (
        "external session",
        "session id",
        "your commit",
        "you launched",
        "at the end of your output",
    )
    for text in _all_templates():
        lowered = text.lower()
        assert "close this card" in lowered
        for phrase in client_only:
            assert phrase not in lowered, phrase


def test_delivery_body_demands_every_gate_it_ran():
    text = _all_templates()[1]

    assert "every required gate" in text



# --- remediation wave: the checks a mutation pass found unpinned ------------


def _scratch_repo(tmp_path, name):
    repo = tmp_path / name
    repo.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo


def _scratch_commit(repo, *paths, message="c"):
    for path in paths:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(path))
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", message], cwd=repo, check=True, capture_output=True
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True,
        text=True,
    ).stdout.strip()


def _git_result(parent, head, changed=()):
    from hermes_pipeline.result_contract import GitResult

    return GitResult(parent, head, head, tuple(changed))


def _merge_with_parents(repo, tree_of, first, second, message):
    """A real merge commit whose parent ORDER is chosen, via ``commit-tree``."""
    tree = subprocess.run(
        ["git", "rev-parse", f"{tree_of}^{{tree}}"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    return subprocess.run(
        ["git", "commit-tree", tree, "-p", first, "-p", second, "-m", message],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.mark.parametrize(
    "verifier",
    [
        pytest.param("optional-single", id="verify_optional_single_commit"),
        pytest.param("topology", id="verify_worker_git_topology"),
    ],
)
def test_a_merge_hiding_the_anchor_as_its_second_parent_is_rejected(tmp_path, verifier):
    """``rev-parse <sha>^`` is the ONLY guard against a forged merge.

    A merge commit that lists the anchor as its SECOND parent smuggles in a tree
    the anchor never produced while satisfying everything else: the anchor is a
    real ancestor of it, exactly one commit separates them, and -- as the premise
    below asserts -- ``_on_first_parent_mainline`` answers True, because it walks
    from HEAD and HEAD *is* the forged merge. So this check is load-bearing and
    alone, and no test killed its mutation.

    The accepted control is the same tree with the parents in the honest order,
    which proves the rejection is about parent ORDER and not about merges.
    """
    repo = _scratch_repo(tmp_path, "forged-merge")
    base = _scratch_commit(repo, "base.txt")
    anchor = _scratch_commit(repo, "impl.py")
    # A side tree the anchor never produced: it drops impl.py and adds a
    # backdoor. Its commit is never a parent of either merge -- only its tree is
    # reused -- so both merges sit exactly one commit past the anchor.
    subprocess.run(["git", "checkout", "-q", "-b", "side", base], cwd=repo,
                   check=True, capture_output=True)
    side = _scratch_commit(repo, "backdoor.py")
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True,
                   capture_output=True)

    forged = _merge_with_parents(repo, side, base, anchor, "anchor as 2nd parent")
    honest = _merge_with_parents(repo, side, anchor, base, "anchor as 1st parent")
    reported = ("backdoor.py", "impl.py")

    def check(head):
        report = _git_result(anchor, head, reported)
        if verifier == "optional-single":
            verify_optional_single_commit(
                repo, report, expected_parent_sha=anchor, require_current=False,
            )
        else:
            verify_worker_git_topology(repo, report, expected_parent_sha=anchor)

    subprocess.run(["git", "reset", "--hard", "-q", forged], cwd=repo, check=True,
                   capture_output=True)
    # Premise 1: the anchor really is an ancestor, so ancestry proves nothing.
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", anchor, forged], cwd=repo,
        capture_output=True,
    ).returncode == 0
    # Premise 2: exactly one commit separates them, so the count proves nothing.
    assert subprocess.run(
        ["git", "rev-list", "--count", f"{anchor}..{forged}"], cwd=repo,
        check=True, capture_output=True, text=True,
    ).stdout.strip() == "1"
    # Premise 3: and the mainline predicate says YES to the forged input.
    from hermes_pipeline.result_contract import _on_first_parent_mainline

    assert _on_first_parent_mainline(repo, anchor, forged) is True

    with pytest.raises(ResultContractError) as exc_info:
        check(forged)
    assert exc_info.value.code == "parent_mismatch"

    # The control: identical tree, honest parent order, accepted.
    subprocess.run(["git", "reset", "--hard", "-q", honest], cwd=repo, check=True,
                   capture_output=True)
    check(honest)


def test_a_compliant_finish_worker_filling_only_the_template_slots_verifies(tmp_path):
    """A worker that did exactly what the profile mandates must be accepted.

    The profile orders ``phase_8_finish_branch`` to "commit those as one
    separate atomic commit", so a compliant finish worker's diff is never
    empty. This builds its report by filling ONLY the ``<...>`` slots of the
    template the card really publishes -- keeping every pre-filled value
    verbatim, exactly as the template instructs -- and then verifies it against
    a real repository. A pre-filled ``"changed_files": []`` made this
    impossible: it is a pre-filled value, so an obedient worker keeps it, and
    ``verify_optional_single_commit`` then raises ``changed_files_mismatch`` and
    delivery can never complete.
    """
    from hermes_pipeline.result_contract import render_result_template

    repo = _scratch_repo(tmp_path, "compliant-finish")
    accepted = _scratch_commit(repo, "impl.py")
    head = _scratch_commit(repo, "CHANGELOG.md")

    template, = _template_blocks(
        render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="finish",
            section="delivery", branch="todo-42", allow_no_changes=True,
        )
    )
    report = _fill_template(
        template,
    )
    # The only worker-supplied facts, substituted into the template's own slots.
    report["git"]["expected_parent_sha"] = accepted
    report["git"]["resulting_head_sha"] = head
    report["git"]["task_commit_sha"] = head
    report["git"]["changed_files"] = ["CHANGELOG.md"]
    report["delivery"]["head_sha"] = head

    parsed = parse_worker_result(
        _template_envelope(report), tick_id="01TICK", todo_id="TODO-42",
        step_key="finish", acceptance_criteria=(), allow_no_changes=True,
    )
    verify_optional_single_commit(
        repo, parsed.git, expected_parent_sha=accepted, require_current=True
    )


def test_changed_files_guidance_is_published_in_both_template_modes():
    """The one conditional field must never be the one field with no prose."""
    from hermes_pipeline.result_contract import render_result_template

    for allow_no_changes in (False, True):
        text = render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="finish",
            allow_no_changes=allow_no_changes,
        )
        prose = text.split("```")[-1]
        assert "changed_files lists every repo-relative path" in prose
        assert "[] only when no commit was made" in prose


def test_an_in_flight_card_still_reporting_review_is_parsed_and_ignored():
    """A card opened before the review section was removed must still land.

    Its 2400s timeout outlives an upgrade, so the card is already published
    with a ``review`` key. Rejecting that key as ``malformed_result`` leaves a
    card whose closing metadata TPO can never accept: ``reconcile_reviews``
    returns False every tick forever with nothing blocked. The value is not
    validated and not read -- including no unsafe-string scan, because findings
    prose naming a token would then reject the very card the tolerance exists
    to admit.
    """
    payload = {
        **_result(step_key="review:0"),
        "review": {
            "verdict": "pass",
            "findings": [{"severity": "high", "detail": "authorization: Bearer x"}],
        },
    }
    payload["acceptance"] = []
    parsed = parse_worker_result(
        _template_envelope(payload), tick_id="01TICK", todo_id="TODO-42",
        step_key="review:0", acceptance_criteria=(), allow_no_changes=True,
    )

    assert not hasattr(parsed, "review")
    assert parsed.step_key == "review:0"


def test_review_is_not_a_section_the_template_can_publish():
    """Tolerating the key on the way in must not resurrect it on the way out."""
    from hermes_pipeline.result_contract import render_result_template

    with pytest.raises(ResultContractError, match="unknown_result_section"):
        render_result_template(
            tick_id="01TICK", todo_id="TODO-42", step_key="review:0",
            section="review",
        )


def test_a_side_merge_parent_is_not_on_the_branch_mainline(tmp_path):
    """``merge-base --is-ancestor`` is satisfied by a merge's SECOND parent.

    Anchor ``A``, a legitimate commit ``R1``, and a forged commit ``X`` built
    off ``A`` with arbitrary content; ``HEAD`` is the merge of ``(R1, X)``. ``X``
    is honestly reported, has ``A`` as its real parent, and sits one commit past
    the anchor, so every other check passes -- and ancestry from HEAD passes
    too, even though ``X`` was never on the mainline the run delivers. Only a
    first-parent walk excludes it.
    """
    repo = _scratch_repo(tmp_path, "side-merge")
    anchor = _scratch_commit(repo, "impl.py")
    subprocess.run(["git", "checkout", "-q", "-b", "legit"], cwd=repo, check=True)
    legit = _scratch_commit(repo, "review-fix.py")
    subprocess.run(["git", "checkout", "-q", "-b", "forged", anchor], cwd=repo, check=True)
    forged = _scratch_commit(repo, "backdoor.py")
    subprocess.run(["git", "checkout", "-q", "legit"], cwd=repo, check=True)
    subprocess.run(
        ["git", "merge", "-q", "--no-ff", "forged", "-m", "merge"],
        cwd=repo, check=True, capture_output=True,
    )

    # The premise: git really does call the side parent an ancestor of HEAD.
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", forged, "HEAD"], cwd=repo,
        capture_output=True,
    ).returncode == 0

    with pytest.raises(ResultContractError, match="unreachable_commit"):
        verify_optional_single_commit(
            repo, _git_result(anchor, forged, ("backdoor.py",)),
            expected_parent_sha=anchor, require_current=False,
        )
    # The mainline commit at the same distance is still accepted.
    verify_optional_single_commit(
        repo, _git_result(anchor, legit, ("review-fix.py",)),
        expected_parent_sha=anchor, require_current=False,
    )


@pytest.mark.parametrize(
    "verifier",
    [
        pytest.param("optional-single", id="verify_optional_single_commit"),
        pytest.param("topology", id="verify_worker_git_topology"),
    ],
)
@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("invented", id="sha-never-existed"),
        pytest.param("tree", id="real-object-of-the-wrong-type"),
    ],
)
def test_a_reported_sha_that_is_no_commit_is_a_worker_fault(tmp_path, verifier, kind):
    """A valid-shaped SHA naming no commit is a bad report, not broken git.

    Every topology query -- ``merge-base``, ``rev-parse <sha>^`` -- exits 128 on
    an unknown object, and every git-failure helper here collapses that into
    ``git_verification_failed``, whose whole meaning is "git could not answer".
    ``_verify_finish`` deliberately passes that code through so an operator
    reads a broken worktree as broken, so a worker that simply invented its SHA
    was attributed to broken infrastructure. The old string comparison called it
    ``finish_review_head_mismatch``, so this is an attribution regression.

    The ``real-object-of-the-wrong-type`` row is why the check is
    ``rev-parse --verify <sha>^{{commit}}`` and not ``cat-file -e <sha>``: a
    tree's own SHA is a perfectly valid object, so ``cat-file -e`` answers yes
    for it while no commit by that name exists.
    """
    repo = _scratch_repo(tmp_path, "no-such-commit")
    anchor_sha = _scratch_commit(repo, "impl.py")
    if kind == "invented":
        bogus = "9" * 40
    else:
        bogus = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        # The premise for this row: it really is an object git knows.
        assert subprocess.run(
            ["git", "cat-file", "-e", bogus], cwd=repo, capture_output=True
        ).returncode == 0

    report = _git_result(anchor_sha, bogus, ("invented.py",))
    with pytest.raises(ResultContractError) as exc_info:
        if verifier == "optional-single":
            verify_optional_single_commit(
                repo, report, expected_parent_sha=anchor_sha, require_current=False,
            )
        else:
            verify_worker_git_topology(
                repo, report, expected_parent_sha=anchor_sha,
            )
    assert exc_info.value.code == "unknown_commit"


def test_a_commit_discarded_by_git_reset_hard_is_unreachable(tmp_path):
    """The one fact that survives a discarded commit is mainline membership.

    Parentage, commit count and the real diff all keep holding for a commit
    ``git reset --hard`` threw away -- the object is still in the repository.
    So this rejection is the whole value of the reachability check, and it is
    exercised with a real reset rather than a fabricated SHA.
    """
    repo = _scratch_repo(tmp_path, "reset-hard")
    anchor = _scratch_commit(repo, "impl.py")
    discarded = _scratch_commit(repo, "review-fix.py")

    verify_optional_single_commit(
        repo, _git_result(anchor, discarded, ("review-fix.py",)),
        expected_parent_sha=anchor, require_current=False,
    )

    subprocess.run(
        ["git", "reset", "--hard", "-q", anchor], cwd=repo, check=True,
        capture_output=True,
    )
    # Still a real object with the right parent, count and diff...
    assert subprocess.run(
        ["git", "cat-file", "-e", discarded], cwd=repo, capture_output=True
    ).returncode == 0

    with pytest.raises(ResultContractError, match="unreachable_commit"):
        verify_optional_single_commit(
            repo, _git_result(anchor, discarded, ("review-fix.py",)),
            expected_parent_sha=anchor, require_current=False,
        )


def test_require_current_demands_the_live_head_equals_the_reported_head(tmp_path):
    """The first verification measures the live branch, not just the report.

    ``verify_read_only_review`` used to prove this and was deleted with nothing
    replacing it. Without it a report can name any mainline commit while HEAD
    has already moved on.
    """
    repo = _scratch_repo(tmp_path, "live-head")
    anchor = _scratch_commit(repo, "impl.py")
    reviewed = _scratch_commit(repo, "review-fix.py")
    _scratch_commit(repo, "later.py")

    with pytest.raises(ResultContractError, match="head_mismatch"):
        verify_optional_single_commit(
            repo, _git_result(anchor, reviewed, ("review-fix.py",)),
            expected_parent_sha=anchor, require_current=True,
        )
    # Relaxed on a later tick, the same report is accepted.
    verify_optional_single_commit(
        repo, _git_result(anchor, reviewed, ("review-fix.py",)),
        expected_parent_sha=anchor, require_current=False,
    )


def test_require_current_demands_a_clean_worktree(tmp_path):
    """Uncommitted work at the reviewed head is work no report accounts for.

    The other half of the deleted read-only-review check: an untracked file is
    enough, because ``--untracked-files=all`` is what makes "clean" mean it.
    """
    repo = _scratch_repo(tmp_path, "dirty-worktree")
    anchor = _scratch_commit(repo, "impl.py")
    reviewed = _scratch_commit(repo, "review-fix.py")

    verify_optional_single_commit(
        repo, _git_result(anchor, reviewed, ("review-fix.py",)),
        expected_parent_sha=anchor, require_current=True,
    )

    (repo / "unstaged.py").write_text("left behind")
    with pytest.raises(ResultContractError, match="worktree_dirty"):
        verify_optional_single_commit(
            repo, _git_result(anchor, reviewed, ("review-fix.py",)),
            expected_parent_sha=anchor, require_current=True,
        )


def test_a_commit_touching_a_non_ascii_path_can_be_reported_at_all(tmp_path):
    """``core.quotePath`` defaults true, which made such a commit unreportable.

    ``git diff --name-only`` renders ``docs/한글.md`` as
    ``"docs/\\355\\225\\234\\352\\270\\200.md"``, and ``parse_worker_result``
    rejects any reported path containing a backslash -- so neither the real name
    nor the quoted name could ever match, and any commit touching such a path
    wedged the run permanently.
    """
    repo = _scratch_repo(tmp_path, "non-ascii")
    anchor = _scratch_commit(repo, "impl.py")
    head = _scratch_commit(repo, "docs/한글-파일.md", "café.txt")

    # The premise: the default rendering is C-quoted and unreportable.
    quoted = subprocess.run(
        ["git", "diff", "--name-only", anchor, head], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout
    assert "\\" in quoted

    reported = ("docs/한글-파일.md", "café.txt")
    verify_optional_single_commit(
        repo, _git_result(anchor, head, reported),
        expected_parent_sha=anchor, require_current=True,
    )
    verify_worker_git_topology(
        repo, _git_result(anchor, head, reported), expected_parent_sha=anchor
    )
    # A wrong report on the same commit still fails, so the pass above is not
    # the checker having stopped looking.
    with pytest.raises(ResultContractError, match="changed_files_mismatch"):
        verify_optional_single_commit(
            repo, _git_result(anchor, head, ("docs/한글-파일.md",)),
            expected_parent_sha=anchor, require_current=True,
        )


def test_a_commit_touching_a_path_with_a_control_character_can_be_reported(tmp_path):
    r"""``-z`` alone carries this case; ``core.quotePath`` does not govern it.

    ``core.quotePath`` governs non-ASCII bytes and nothing else, so this is not
    the non-ASCII test wearing a different hat: git C-quotes a path holding a
    control character no matter what that setting says, which the first premise
    below asserts against real git. Meanwhile such a path IS reportable --
    ``_CONTROL_RE`` matches none of ``\n``, ``\r``, ``\t`` and the
    ``changed_files`` validator rejects only a backslash, an absolute path and
    ``..`` -- which the second premise asserts through the public parser. So
    without ``-z`` any commit touching such a path is a permanent
    ``changed_files_mismatch`` with no reportable alternative.
    """
    repo = _scratch_repo(tmp_path, "control-char")
    anchor = _scratch_commit(repo, "impl.py")
    newline_path = "docs/two\nline.md"
    head = _scratch_commit(repo, newline_path)

    # Premise 1: C-quoted with AND without quotePath disabled, so disabling
    # quotePath cannot stand in for ``-z`` here.
    for extra in ([], ["-c", "core.quotePath=false"]):
        listed = subprocess.run(
            ["git", *extra, "diff", "--name-only", anchor, head], cwd=repo,
            check=True, capture_output=True, text=True,
        ).stdout
        assert "\\n" in listed, extra
        assert newline_path not in listed, extra

    # Premise 2: the real name survives the public parser, so the worker really
    # can report it and a mismatch here is TPO's fault, not the path's.
    parsed = parse_worker_result(
        {"runs": [{
            "status": "succeeded",
            "summary": "done",
            "metadata": {"tpo_result": _result(git={
                "expected_parent_sha": anchor,
                "resulting_head_sha": head,
                "task_commit_sha": head,
                "changed_files": [newline_path],
            })},
        }]},
        tick_id="01TICK",
        todo_id="TODO-42",
        step_key="plan:task-1",
        acceptance_criteria=("Observable criterion",),
    )
    assert parsed.git.changed_files == (newline_path,)

    reported = (newline_path,)
    verify_optional_single_commit(
        repo, _git_result(anchor, head, reported),
        expected_parent_sha=anchor, require_current=True,
    )
    verify_worker_git_topology(
        repo, _git_result(anchor, head, reported), expected_parent_sha=anchor
    )
    # Negative control: the same commit with the control character flattened to
    # a space still fails, so the pass above is not the checker giving up.
    with pytest.raises(ResultContractError, match="changed_files_mismatch"):
        verify_optional_single_commit(
            repo, _git_result(anchor, head, ("docs/two line.md",)),
            expected_parent_sha=anchor, require_current=True,
        )


def test_reject_unsafe_strings_recurses_through_a_list_of_dicts():
    """Directly, because no other test isolates the scan any more.

    The surviving ``delivery.checks[0].command`` case is caught downstream by
    ``_bounded_string``, so it no longer proves the recursive walk reaches a
    dict nested inside a list.
    """
    from hermes_pipeline.result_contract import _reject_unsafe_strings

    with pytest.raises(ResultContractError, match="unsafe_metadata"):
        _reject_unsafe_strings({"x": [{"y": "password=s\x00"}]})
    with pytest.raises(ResultContractError, match="unsafe_metadata"):
        _reject_unsafe_strings([[{"deep": "‮txt.exe"}]])
    with pytest.raises(ResultContractError, match="unsafe_metadata"):
        _reject_unsafe_strings({"token=abc": "harmless"})
    # A safe nest of the same shape is not rejected.
    _reject_unsafe_strings({"x": [{"y": "src/example.py"}], "n": [1, None, True]})


def test_a_broken_git_during_the_worktree_check_reports_a_git_failure(tmp_path, mocker):
    """``_git_bytes`` is a git-failure helper, so its code says so.

    It used to raise ``registration_invalid``, which blamed the registration
    for a git that could not answer -- and, wrapped by ``_verify_finish``,
    produced ``finish_review_head_mismatch: registration_invalid``, blaming the
    worker too. Both wrong.
    """
    from hermes_pipeline.result_contract import _git_bytes

    # Prevent discovery of the developer checkout when pytest's temporary
    # directory is inside that checkout; this fixture is deliberately broken.
    (tmp_path / ".git").write_text("invalid metadata pointer")
    with pytest.raises(ResultContractError) as exc_info:
        _git_bytes(tmp_path, "status", "--porcelain=v1")
    assert exc_info.value.code == "git_verification_failed"

    repo = _scratch_repo(tmp_path, "broken-status")
    anchor = _scratch_commit(repo, "impl.py")
    reviewed = _scratch_commit(repo, "review-fix.py")
    real_run = subprocess.run

    def fail_status(cmd, *args, **kwargs):
        if "status" in cmd:
            return SimpleNamespace(returncode=128, stdout=b"", stderr=b"fatal")
        return real_run(cmd, *args, **kwargs)

    mocker.patch(
        "hermes_pipeline.result_contract.subprocess.run", side_effect=fail_status
    )
    with pytest.raises(ResultContractError) as exc_info:
        verify_optional_single_commit(
            repo, _git_result(anchor, reviewed, ("review-fix.py",)),
            expected_parent_sha=anchor, require_current=True,
        )
    assert exc_info.value.code == "git_verification_failed"


def test_a_registration_profile_cannot_walk_out_of_the_bundled_data_directory(tmp_path):
    """``profile`` selects a bundled directory, so it must stay a plain name."""
    repo, _worktree, state, _parent = _registered_repo(tmp_path)

    for hostile in ("../../etc", "..", "a/b", "/abs", "Native-SDD", ""):
        _rewrite_registration(
            state, lambda payload, value=hostile: payload.__setitem__("profile", value)
        )
        with pytest.raises(ResultContractError, match="registration_invalid"):
            load_validated_registration(repo, state, "01TICK")


def test_a_plan_path_the_base_commit_does_not_carry_is_a_registration_error(tmp_path):
    """Here a git failure really is the registration's claim being false.

    ``_git_bytes`` raises ``git_verification_failed`` for every other caller,
    because a git that cannot answer is not the same claim as a fact being
    wrong. This one call site is the exception: ``git show <base>:<plan>``
    failing means the registration names a Plan path the pinned base commit
    does not carry, so the code must stay ``registration_invalid``.
    """
    repo, _worktree, state, _parent = _registered_repo(tmp_path)
    _rewrite_registration(
        state, lambda payload: payload.__setitem__("plan_path", "never-committed.md")
    )

    with pytest.raises(ResultContractError) as exc_info:
        load_validated_registration(repo, state, "01TICK")
    assert exc_info.value.code == "registration_invalid"


@pytest.mark.parametrize("embedded", [False, True])
def test_v4_registration_exposes_delegated_mode(tmp_path, embedded):
    repo, _worktree, state, _parent = _registered_repo(tmp_path, embedded=embedded)
    _rewrite_registration(state, lambda payload: payload.update(
        schema_version=4, agent_policy_mode="delegated"
    ))
    authority = load_validated_registration(repo, state, "01TICK")
    assert authority.agent_policy_mode == "delegated"
    assert authority.profile == "native-sdd"


@pytest.mark.parametrize("schema_version", [2, 3])
def test_legacy_registration_inherits_and_rejects_mode_field(tmp_path, schema_version):
    repo, _worktree, state, _parent = _registered_repo(tmp_path)

    def legacy(payload):
        payload["schema_version"] = schema_version
        if schema_version == 2:
            del payload["plan_source_kind"]
            del payload["plan_artifact"]

    _rewrite_registration(state, legacy)
    assert load_validated_registration(repo, state, "01TICK").agent_policy_mode == "inherit"
    _rewrite_registration(state, lambda payload: payload.update(agent_policy_mode="delegated"))
    with pytest.raises(ResultContractError, match="registration_invalid"):
        load_validated_registration(repo, state, "01TICK")


@pytest.mark.parametrize("mutate", [
    lambda p: p.pop("agent_policy_mode"),
    lambda p: p.update(agent_policy_mode="inherit"),
    lambda p: p.update(agent_policy_mode=None),
    lambda p: p.update(agent_policy_mode="Delegated"),
    lambda p: p.update(agent_policy_mode=[]),
    lambda p: p.update(profile="sdd"),
    lambda p: p.update(profile="Native-SDD"),
    lambda p: p.update(profile="native-sdd "),
    lambda p: p.update(unexpected=True),
    lambda p: p.pop("plan_artifact"),
])
def test_v4_registration_rejects_invalid_mode_profile_and_keys(tmp_path, mutate):
    repo, _worktree, state, _parent = _registered_repo(tmp_path)

    def v4(payload):
        payload.update(schema_version=4, agent_policy_mode="delegated")
        mutate(payload)

    _rewrite_registration(state, v4)
    with pytest.raises(ResultContractError, match="registration_invalid"):
        load_validated_registration(repo, state, "01TICK")


@pytest.mark.parametrize("schema_version", [4.0, [], {}, "4", None, True])
def test_registration_rejects_non_integer_schema_version(tmp_path, schema_version):
    repo, _worktree, state, _parent = _registered_repo(tmp_path)
    _rewrite_registration(state, lambda payload: payload.update(
        schema_version=schema_version, agent_policy_mode="delegated"
    ))
    with pytest.raises(ResultContractError, match="registration_invalid"):
        load_validated_registration(repo, state, "01TICK")
