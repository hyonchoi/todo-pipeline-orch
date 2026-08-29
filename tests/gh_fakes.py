"""Reusable ``subprocess.run`` stand-in for ``gh``/``git`` calls in client tests."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

Handler = Callable[[list[str]], tuple[int, str, str]]


@dataclass
class _Rule:
    prefix: tuple[str, ...]
    handler: Handler | None = None
    rc: int = 0
    stdout: str = ""
    stderr: str = ""
    raises: BaseException | None = None


@dataclass
class FakeGh:
    """Callable replacement for ``subprocess.run`` matching on the full argv.

    Rules are matched by the longest registered argv prefix (binary included, so
    ``on("gh", "api", ...)`` and ``on("git", "remote", ...)`` both work). When
    two rules of equal prefix length match, the later registration wins, so a
    test can re-``on()`` to override an earlier default.
    Unmatched argv fails with rc 1 and ``stderr="fake gh: unsupported"``.
    """

    calls: list[list[str]] = field(default_factory=list)
    kwargs: list[dict[str, Any]] = field(default_factory=list)
    _rules: list[_Rule] = field(default_factory=list)

    def on(
        self,
        *prefix: str,
        rc: int = 0,
        stdout: str = "",
        stderr: str = "",
        handler: Handler | None = None,
        raises: BaseException | None = None,
    ) -> FakeGh:
        self._rules.append(_Rule(tuple(prefix), handler, rc, stdout, stderr, raises))
        return self

    def _match(self, argv: list[str]) -> _Rule | None:
        best: _Rule | None = None
        for rule in self._rules:
            if tuple(argv[: len(rule.prefix)]) != rule.prefix:
                continue
            if best is None or len(rule.prefix) >= len(best.prefix):
                best = rule
        return best

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> SimpleNamespace:
        argv = list(argv)
        self.calls.append(argv)
        self.kwargs.append(kwargs)
        rule = self._match(argv)
        if rule is None:
            return SimpleNamespace(returncode=1, stdout="", stderr="fake gh: unsupported")
        if rule.raises is not None:
            raise rule.raises
        if rule.handler is not None:
            rc, stdout, stderr = rule.handler(argv)
            return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)
        return SimpleNamespace(returncode=rule.rc, stdout=rule.stdout, stderr=rule.stderr)

    def gh_calls(self) -> list[list[str]]:
        """argv lists (binary stripped) for every ``gh`` invocation, in order."""
        return [call[1:] for call in self.calls if call and call[0] == "gh"]


def issue_payload(
    number: int = 7,
    *,
    title: str = "Ship the widget",
    body: str = "### What\n\nWidget\n",
    state: str = "open",
    labels: Sequence[str] = ("tpo:todo", "ready-for-agent"),
    pull_request: bool = False,
    blocked_by: int | None = 0,
    **extra: Any,
) -> dict[str, Any]:
    """Minimal REST issue JSON in the shape ``issue_from_api`` consumes."""
    payload: dict[str, Any] = {
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "labels": [{"name": name} for name in labels],
        "assignees": [],
        "html_url": f"https://github.com/acme/repo/issues/{number}",
    }
    if blocked_by is not None:
        payload["issue_dependencies_summary"] = {"blocked_by": blocked_by}
    if pull_request:
        payload["pull_request"] = {"url": f"https://api.github.com/repos/acme/repo/pulls/{number}"}
    payload.update(extra)
    return payload


def make_issue(
    number: int = 7,
    *,
    repo: str = "acme/repo",
    title: str = "Ship the widget",
    body: str = "### What\n\nWidget\n",
    **extra: Any,
):
    """Build an ``IssueTodo`` the way the client does, from a synthetic REST payload."""
    from hermes_pipeline.github_issues import issue_from_api

    return issue_from_api(issue_payload(number, title=title, body=body, **extra), repo=repo)
