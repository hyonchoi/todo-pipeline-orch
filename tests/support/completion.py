"""Shared helpers for delivery and completion testing."""

import json
from types import SimpleNamespace

from tests.gh_fakes import REPO
from tests.support.projects import run_dir as shared_run_dir

API = ("gh", "api", "-H", "Accept: application/vnd.github+json")
PR_URL = f"https://github.com/{REPO}/pull/7"
# A DIFFERENT pull request in the same repository, so it clears the `pr_url`
# regex and only the echoed `url` can tell it apart from the delivered PR.
OTHER_PR_URL = f"https://github.com/{REPO}/pull/8"
MARKER = "<!-- tpo-completed tick=01TICK pr=7 -->"

# gh 2.89.0 `gh pr view --json` field vocabulary, transcribed verbatim from
# `gh pr view --help`. Requesting anything outside this set makes gh exit 1 with
# `Unknown JSON field`, which _pr_view can only report as `pr_missing`.
#
# P0-LOAD-BEARING. Every integration test in this repository mocks `gh`, so this
# frozenset and its sibling argv test are the only checks standing between the
# codebase and a silent reintroduction of the `baseRepository` defect, where
# delivery verification could never succeed against a real PR. Update it only
# from `gh pr view --help` output, never to make a failing test pass.
GH_PR_VIEW_JSON_FIELDS = frozenset(
    """
    additions assignees author autoMergeRequest baseRefName baseRefOid body
    changedFiles closed closedAt closingIssuesReferences comments commits
    createdAt deletions files fullDatabaseId headRefName headRefOid
    headRepository headRepositoryOwner id isCrossRepository isDraft labels
    latestReviews maintainerCanModify mergeCommit mergeStateStatus mergeable
    mergedAt mergedBy milestone number potentialMergeCommit projectCards
    projectItems reactionGroups reviewDecision reviewRequests reviews state
    statusCheckRollup title updatedAt url
    """.split()
)


class FakeRemoteIssue:
    """Stateful GitHub issue behind ``fake_gh``: reads reflect earlier writes."""

    def __init__(self, fake, number=3, *, state="open", labels=("tpo:todo", "tpo:in-progress"),
                 comments=(), crash_after=None, state_reason=None):
        from tests.gh_fakes import issue_payload

        self.number = number
        self.state = state
        self.state_reason = state_reason
        self.labels = list(labels)
        self.comments = list(comments)
        self.writes: list[str] = []
        self.crash_after = crash_after
        base = f"repos/{REPO}/issues/{number}"
        fake.on(*API, base, handler=lambda argv: (
            0, json.dumps(issue_payload(
                number, state=self.state, labels=self.labels, state_reason=self.state_reason,
            )), ""
        ))
        fake.on(*API, "--paginate", "--slurp", f"{base}/comments", handler=lambda argv: (
            0, json.dumps([[self._comment(entry) for entry in self.comments]]), ""
        ))
        fake.on(*API, "user", "--jq", ".login", stdout=f"{self.login}\n")

        def comment(argv):
            with open(argv[argv.index("--body-file") + 1]) as handle:
                self.comments.append(handle.read())
            return self._wrote("comment")

        def close(argv):
            self.state = "closed"
            self.state_reason = "completed"
            return self._wrote("close")

        def edit(argv):
            self.labels.remove(argv[argv.index("--remove-label") + 1])
            return self._wrote("edit")

        fake.on("gh", "issue", "comment", handler=comment)
        fake.on("gh", "issue", "close", handler=close)
        fake.on("gh", "issue", "edit", handler=edit)

    login = "tpo-bot"

    @classmethod
    def _comment(cls, entry):
        """``str`` entries are TPO's own comments; ``(login, body)`` pairs name another author."""
        login, body = entry if isinstance(entry, tuple) else (cls.login, entry)
        return {"body": body, "user": {"login": login}}

    def _wrote(self, verb):
        self.writes.append(verb)
        if self.crash_after == verb:
            raise RuntimeError(f"crash after {verb}")
        return 0, "", ""


def _run_dir(tmp_path):
    """Get the run directory for 01TICK."""
    run_dir = shared_run_dir(tmp_path, tick_id="01TICK")
    return tmp_path / ".hermes", run_dir


def finish_done_fixture(tmp_path, mocker, *, tasks, view):
    """Create a fixture for testing finish phase (delivery) functionality.

    Returns the state directory.
    """
    state, run_dir = _run_dir(tmp_path)
    (run_dir / "registration.json").write_text("{}")
    (run_dir / "accepted-review-head").write_text("a" * 40)
    (run_dir / "delivery-authority.json").write_text(
        '{"base_branch":"main","origin_repository":"acme/repo"}\n'
    )
    registration = SimpleNamespace(
        todo_id="TODO-3", worktree=tmp_path, branch="feat/native",
        assignee="worker", prompt_client="codex", issue_number=3,
        issue_url="https://github.com/acme/repo/issues/3",
    )
    delivery = SimpleNamespace(pr_url=PR_URL, branch="feat/native", head_sha="a" * 40)
    finish_result = SimpleNamespace(
        delivery=delivery,
        git=SimpleNamespace(
            expected_parent_sha="a" * 40, resulting_head_sha="a" * 40,
            task_commit_sha="a" * 40, changed_files=(),
        ),
    )
    mocker.patch("hermes_pipeline.todos_completion.load_validated_registration",
                 return_value=registration)
    mocker.patch("hermes_pipeline.todos_completion.get_todo_kanban_tasks",
                 side_effect=lambda *_a, **_k: tasks)
    mocker.patch("hermes_pipeline.todos_completion.parse_worker_result",
                 return_value=finish_result)
    mocker.patch("hermes_pipeline.todos_completion._verify_finish")
    mocker.patch("hermes_pipeline.todos_completion._verify_pr_identity")
    mocker.patch("hermes_pipeline.todos_completion._github_identity",
                 return_value=("acme/repo", "main"))
    mocker.patch("hermes_pipeline.todos_completion._remote_head", return_value="a" * 40)
    mocker.patch("hermes_pipeline.todos_completion._pr_view", side_effect=lambda *_a: dict(view))
    mocker.patch("hermes_pipeline.todos_completion.github_issues.check_issue_drift", return_value=None)
    return state


def finish_tasks():
    """Create a fixture for kanban tasks in finish phase."""
    return {
        "review:0": SimpleNamespace(task_id="review", status="done"),
        "finish": SimpleNamespace(task_id="finish-id", status="done"),
    }


def view(state="OPEN", head="a" * 40, url=PR_URL):
    """Create a pull request view dict for testing."""
    return {"state": state, "url": url, "headRefName": "feat/native", "headRefOid": head}
