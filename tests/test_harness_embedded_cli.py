"""Provider-free command integration; Git and all TPO authorities remain real."""
from __future__ import annotations

import hashlib
import json
import runpy
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_pipeline import cli, harness, phases
from hermes_pipeline.config import Config
from hermes_pipeline.github_issues import LABEL_VOCABULARY
from hermes_pipeline.result_contract import load_validated_registration
from tests.gh_fakes import API_ARGV, ORIGIN_ARGV, issue_payload

REPO = "acme/repo"
TRANSACTION = "12345678-1234-4234-9234-123456789abc"
GOLDEN = '''# Mock Name Normalization Plan

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
  "todo_id": "TODO-42",
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
      "verification": [
        "uv run pytest"
      ],
      "commit_message": "feat: add normalize_names mock transform"
    }
  ]
}
```
'''


def _git(project, *args):
    return subprocess.run(["git", *args], cwd=project, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize("line_endings,client,policy_mode,review_fix", [
    (line, client, mode, fix)
    for line in ("lf", "crlf-extra-trailing-newlines")
    for client in ("claude", "codex")
    for mode in ("inherit", "delegated")
    for fix in (False, True)
] + [("lf", client, "delegated", "blocked") for client in ("claude", "codex")])
def test_real_cli_pins_harness_embedded_plan_across_ticks(
    tmp_path, monkeypatch, fake_gh, line_endings, client, policy_mode, review_fix,
):
    project = tmp_path / "projects" / "sandbox"
    state = project / ".hermes"
    state.mkdir(parents=True)
    (project / "README.md").write_text("fixture\n")
    (project / ".gitignore").write_text(".hermes/\n.worktrees/\n")
    (state / "pipeline.toml").write_text(
        'schema_version = 2\nassignee = "default"\nprofile = "native-sdd"\n'
        'capabilities = ["Read", "Write", "Edit", "Bash"]\n'
    )
    for args in [("init", "-b", "main"), ("config", "user.name", "Fixture"),
                 ("config", "user.email", "fixture@example.invalid"),
                 ("config", "commit.gpgsign", "false"), ("add", "README.md", ".gitignore"),
                 ("commit", "-m", "seed"),
                 ("remote", "add", "origin", f"https://github.com/{REPO}.git"),
                 ("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")]:
        _git(project, *args)
    base = _git(project, "rev-parse", "HEAD")
    current_mode = [policy_mode]
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: Config(
        projects_dir=project.parent, state_dir=tmp_path / "global", prompt_client=client,
        agent_policy_mode=current_mode[0])))
    # Prerequisite installation is an external agent fact; profile prompts stay real.
    monkeypatch.setattr(phases, "load_profile_prerequisites", lambda *_: SimpleNamespace(skills=()))
    remote = {"body": "", "title": "", "labels": [], "created": False, "state": "open"}

    def payload():
        return issue_payload(42, title=remote["title"], body=remote["body"],
                             labels=remote["labels"], state=remote["state"])

    def create(argv):
        remote.update(created=True, title=argv[argv.index("--title") + 1],
                      body=Path(argv[argv.index("--body-file") + 1]).read_text())
        remote["labels"] = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--label"]
        return 0, f"https://github.com/{REPO}/issues/42\n", ""

    def edit(argv):
        if "--body-file" in argv:
            remote["body"] = Path(argv[argv.index("--body-file") + 1]).read_text()
        for flag, add in [("--add-label", True), ("--remove-label", False)]:
            if flag in argv:
                label = argv[argv.index(flag) + 1]
                if add and label not in remote["labels"]:
                    remote["labels"].append(label)
                if not add and label in remote["labels"]:
                    remote["labels"].remove(label)
        return 0, "", ""

    fake_gh.on(*ORIGIN_ARGV, stdout=f"https://github.com/{REPO}.git\n")
    fake_gh.on("gh", "auth", "status")
    fake_gh.on("gh", "issue", "create", handler=create)
    fake_gh.on("gh", "issue", "edit", handler=edit)
    fake_gh.on(*API_ARGV, f"repos/{REPO}/issues/42", handler=lambda _: (0, json.dumps(payload()), ""))
    fake_gh.on(*API_ARGV, "--paginate", "--slurp", handler=lambda _: (
        0, json.dumps([[payload()] if remote["created"] and remote["state"] == "open" else []]), ""))
    fake_gh.on("gh", "label", "list", stdout=json.dumps([{"name": n} for n, _, _ in LABEL_VOCABULARY]))

    cards = []
    real_run = subprocess.run

    def external(argv, **kwargs):
        if argv == ["hermes", "--version"]:
            return SimpleNamespace(returncode=0, stdout="hermes 0.19.0\n", stderr="")
        if argv[0] == "gh":
            return fake_gh(argv, **kwargs)
        if argv[:2] != ["hermes", "kanban"]:
            assert argv[0] == "git", f"unexpected executable: {argv}"
            assert argv[1] not in {"push", "fetch", "pull", "clone", "ls-remote"}
            return real_run(argv, **kwargs)
        command = argv[2]
        result = ""
        if command == "list":
            result = json.dumps(cards)
        elif command == "show":
            result = json.dumps(next(c for c in cards if c["id"] == argv[3]))
        elif command == "create":
            card = {"id": f"t_{len(cards):08x}", "status": "ready",
                    "body": argv[argv.index("--body") + 1]}
            cards.append(card)
            result = json.dumps(card)
        elif command == "complete":
            next(c for c in cards if c["id"] == argv[-1])["status"] = "done"
        else:
            raise AssertionError(f"unexpected external command: {argv}")
        return SimpleNamespace(returncode=0, stdout=result, stderr="")

    monkeypatch.setattr(subprocess, "run", external)
    monkeypatch.setattr(cli, "run_selection", lambda **_: SimpleNamespace(
        picked="TODO-42", rationale="only eligible fixture", candidates_considered=[]))
    harness._harness_create_request(project, run_token="cli00042", transaction_id=TRANSACTION)
    request_path = state / "todo-create-input" / f"{TRANSACTION}.json"
    if line_endings != "lf":
        request = json.loads(request_path.read_text())
        request["plan_markdown"] = request["plan_markdown"].replace("\n", "\r\n") + "\r\n\r\n"
        request_path.write_text(json.dumps(request))
    assert cli.main(["todos", "create", "sandbox", "--request-file", str(request_path),
                     "--yes", "--approved-repo", REPO]) == 0
    assert GOLDEN in remote["body"]
    published = remote["body"]
    assert "needs-triage" not in remote["labels"]
    assert "ready-for-agent" in remote["labels"]
    assert cli.main(["plan", "validate", "sandbox", "--todo", "42", "--require-manifest"]) == 0
    assert cli.main(["todos", "audit", "sandbox"]) == 0
    assert cli.main(["doctor", "sandbox"]) == 0
    seed = base
    base = harness.create_run_anchor(project, harness.HarnessIssue(
        number=42, todo_id="TODO-42", branch="feat/harness-cli00042",
        title=remote["title"], run_token="cli00042", transaction_id=TRANSACTION,
    ))
    assert base != seed
    assert _git(project, "rev-parse", f"{base}^{{tree}}") == _git(project, "rev-parse", f"{seed}^{{tree}}")
    assert _git(project, "rev-parse", f"{base}^") == seed
    assert cli.main(["tick", "sandbox"]) == 0
    tick = (state / "current_tick_id.txt").read_text().strip()
    registration_file = state / "runs" / tick / "registration.json"
    pinned = registration_file.read_bytes()
    registration = load_validated_registration(project, state, tick, repo=REPO)
    assert registration.plan_hash == hashlib.sha256(GOLDEN.encode()).hexdigest()
    assert Path(registration.plan_reference.value).read_text() == GOLDEN
    assert registration.base_sha == base
    assert json.loads(pinned)["schema_version"] == (4 if policy_mode == "delegated" else 3)
    assert registration.agent_policy_mode == policy_mode
    current_mode[0] = "inherit" if policy_mode == "delegated" else "delegated"
    assert json.loads(pinned)["plan_path"] is None
    implementation = next(c for c in cards if json.loads(c["body"].splitlines()[0]).get("phase_key") == phases.IMPLEMENTATION_KEY)
    assert registration.plan_reference.value in implementation["body"]
    assert cli.main(["tick", "sandbox"]) == 0
    assert registration_file.read_bytes() == pinned
    assert remote["body"] == published
    assert _git(project, "rev-parse", "HEAD") == base
    assert _git(project, "ls-files") == ".gitignore\nREADME.md"

    def worker_result(card, parent, head, *, changed=(), acceptance=(), delivery=None):
        value = {
            "schema_version": 1, "tick_id": tick, "todo_id": "TODO-42",
            "step_key": json.loads(card["body"].splitlines()[0])["phase_key"],
            "verdict": "success",
            "git": {"expected_parent_sha": parent, "resulting_head_sha": head,
                    "task_commit_sha": head, "changed_files": list(changed)},
            "acceptance": [{"criterion": c, "status": "passed"} for c in acceptance],
        }
        if delivery is not None:
            value["delivery"] = delivery
        card["status"] = "done"
        card["runs"] = [{"status": "succeeded", "metadata": {"tpo_result": value}}]

    worktree = registration.worktree
    (worktree / "mock_transform.py").write_text(
        'def normalize_names(names):\n    return [name.strip().lower() for name in names if name.strip()]\n'
    )
    normalize = runpy.run_path(str(worktree / "mock_transform.py"))["normalize_names"]
    assert normalize([" Alice ", "", "BOB"]) == ["alice", "bob"]
    assert normalize([" b ", "\t", "A"]) == ["b", "a"]
    assert normalize([]) == []
    _git(worktree, "add", "mock_transform.py")
    _git(worktree, "commit", "-m", "feat: add normalize_names mock transform")
    head = _git(worktree, "rev-parse", "HEAD")
    worker_result(implementation, base, head, changed=("mock_transform.py",),
                  acceptance=registration.manifest.tasks[0].acceptance_criteria)
    assert cli.main(["tick", "sandbox"]) == 0
    review = next(c for c in cards if json.loads(c["body"].splitlines()[0]).get("phase_key") == "review:0")
    if review_fix == "blocked":
        # Model the dispatcher's needs_input card after its client exits 17.
        # This checks TPO attribution, not that live Hermes honors the contract.
        review["status"] = "blocked"
        review["runs"] = [{"status": "failed", "metadata": {"external_agent_exit_code": 17}}]
        assert cli.main(["tick", "sandbox"]) == 0
        workers = [c for c in cards if "BEGIN EXTERNAL AGENT PROMPT" in c["body"]]
        assert workers == [implementation, review]
        assert not (registration_file.parent / "accepted-review-head").exists()
        assert registration_file.read_bytes() == pinned
        outcomes = [json.loads(line) for line in (state / "outcomes" / f"{tick}-phases.json").read_text().splitlines()]
        assert any(o["outcome"] == "failed_at_phase_review:0" for o in outcomes)
        assert not any("finish" in o["outcome"] or "human" in o["outcome"] for o in outcomes)
        _, _, worker_prompt = review["body"].partition("BEGIN EXTERNAL AGENT PROMPT\n")
        assert worker_prompt.startswith("AGENT-POLICY-MODE: delegated\n\n")
        return
    reviewed_parent = head
    changed_files = ()
    if review_fix:
        (worktree / "review.txt").write_text("Reviewed correction\n")
        _git(worktree, "add", "review.txt")
        _git(worktree, "commit", "-m", "fix: review correction")
        head = _git(worktree, "rev-parse", "HEAD")
        changed_files = ("review.txt",)
    worker_result(review, reviewed_parent, head, changed=changed_files)
    assert cli.main(["tick", "sandbox"]) == 0
    finish = next(c for c in cards if json.loads(c["body"].splitlines()[0]).get("phase_key") == "finish")
    assert registration.plan_reference.value in review["body"]
    assert registration.plan_reference.value in finish["body"]
    workers = [implementation, review, finish]
    for worker in workers:
        dispatcher, _, worker_prompt = worker["body"].partition("BEGIN EXTERNAL AGENT PROMPT\n")
        assert "AGENT-POLICY-MODE" not in dispatcher
        assert worker_prompt.startswith("AGENT-POLICY-MODE: delegated\n\n") == (policy_mode == "delegated")
    # The controller barrier gets no payload; no human-gate or remediation worker exists.
    assert all(c in workers or "BEGIN EXTERNAL AGENT PROMPT" not in c["body"] for c in cards)
    assert registration_file.read_bytes() == pinned

    pr_url = f"https://github.com/{REPO}/pull/17"
    fake_gh.on("gh", "pr", "view", stdout=json.dumps({
        "state": "MERGED", "url": pr_url, "headRefName": registration.branch,
        "headRefOid": head, "baseRefName": "main",
        "headRepository": {"nameWithOwner": REPO}, "isCrossRepository": False,
    }))
    fake_gh.on("gh", "pr", "checks", stdout=json.dumps([{"state": "SUCCESS"}]))
    comments = []
    fake_gh.on(*API_ARGV, "user", "--jq", ".login", stdout="fixture-bot\n")
    fake_gh.on(*API_ARGV, "--paginate", "--slurp", f"repos/{REPO}/issues/42/comments",
               handler=lambda _: (0, json.dumps([comments]), ""))

    def comment(argv):
        comments.append({"body": Path(argv[argv.index("--body-file") + 1]).read_text(),
                         "user": {"login": "fixture-bot"}})
        return 0, "", ""

    def close(_):
        remote["state"] = "closed"
        return 0, "", ""

    fake_gh.on("gh", "issue", "comment", handler=comment)
    fake_gh.on("gh", "issue", "close", handler=close)
    worker_result(finish, head, head, delivery={
        "pr_url": pr_url, "branch": registration.branch, "head_sha": head,
        "checks": [{"command": "uv run pytest", "exit_code": 0}],
    })
    assert cli.main(["tick", "sandbox"]) == 0
    assert remote["state"] == "closed"
    assert (registration_file.parent / "issue-closed").exists()
    assert cli.main(["todos", "complete", "sandbox", "--todo", "42", "--pr", "17"]) == 0
    assert remote["body"] == published
    assert registration_file.read_bytes() == pinned
    assert Path(registration.plan_reference.value).read_bytes() == GOLDEN.encode()
    assert _git(project, "ls-files") == ".gitignore\nREADME.md"
    assert _git(worktree, "log", "--format=%s", f"{base}..HEAD") == (
        ("fix: review correction\n" if review_fix else "")
        + "feat: add normalize_names mock transform"
    )
