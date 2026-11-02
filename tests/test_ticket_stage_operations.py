from types import SimpleNamespace

from backend.ado_tool import ADOAction, ADOExecutor
from backend.jira_tool import JiraAction, JiraExecutor


class _Response:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _Session:
    def __init__(self, current):
        self.current = current
        self.patch_calls = []

    def get(self, *_args, **_kwargs):
        return _Response({"fields": self.current})

    def patch(self, *args, **kwargs):
        self.patch_calls.append((args, kwargs))
        raise AssertionError("Already-satisfied operations must not patch")


def test_ado_assignment_and_status_do_nothing_when_already_correct(monkeypatch):
    executor = ADOExecutor(
        org_url="https://dev.azure.com/example",
        project="project",
        pat="pat",
    )
    session = _Session(
        {
            "System.AssignedTo": {
                "displayName": "Target User",
                "uniqueName": "target@example.com",
            },
            "System.State": "Active",
        }
    )
    monkeypatch.setattr(executor, "_session", lambda: session)

    assigned = executor(
        ADOAction(
            command="assign_to_user",
            ticket_key="123",
            assignee="target@example.com",
        )
    )
    status = executor(
        ADOAction(
            command="update_status",
            ticket_key="123",
            target_status="Active",
        )
    )

    assert not assigned.is_error
    assert "Assignee: Target User" in assigned.text
    assert "Operation: already_assigned" in assigned.text
    assert not status.is_error
    assert "Status: Active" in status.text
    assert "Operation: already_in_target_status" in status.text
    assert session.patch_calls == []


def test_jira_assignment_and_status_do_nothing_when_already_correct(monkeypatch):
    target_user = SimpleNamespace(
        accountId="account-123",
        emailAddress="target@example.com",
        displayName="Target User",
        name="target",
    )
    current_issue = SimpleNamespace(
        fields=SimpleNamespace(
            assignee=SimpleNamespace(
                accountId="account-123",
                displayName="Target User",
            ),
            status=SimpleNamespace(name="In Progress"),
        )
    )
    jira = SimpleNamespace(
        search_users=lambda **_kwargs: [target_user],
        issue=lambda *_args, **_kwargs: current_issue,
        transitions=lambda *_args: (_ for _ in ()).throw(
            AssertionError("Already-in-progress issues need no transition lookup")
        ),
        assign_issue=lambda *_args: (_ for _ in ()).throw(
            AssertionError("Already-assigned issues must not be reassigned")
        ),
    )
    monkeypatch.setattr("backend.jira_tool.JIRA", lambda **_kwargs: jira)
    executor = JiraExecutor(
        base_url="https://example.atlassian.net",
        user_email="bot@example.com",
        api_token="token",
    )

    assigned = executor(
        JiraAction(
            command="assign_to_user",
            ticket_key="PROJ-123",
            assignee="target@example.com",
        )
    )
    status = executor(
        JiraAction(
            command="update_status",
            ticket_key="PROJ-123",
            target_status="In Progress",
        )
    )

    assert not assigned.is_error
    assert "Assignee: Target User" in assigned.text
    assert "Operation: already_assigned" in assigned.text
    assert not status.is_error
    assert "Status: In Progress" in status.text
    assert "Operation: already_in_target_status" in status.text


def test_ado_get_comments_returns_provider_comment_text(monkeypatch):
    executor = ADOExecutor(
        org_url="https://dev.azure.com/example",
        project="project",
        pat="pat",
    )

    class CommentSession:
        def __init__(self):
            self.closed = False

        def get(self, *_args, **_kwargs):
            return _Response(
                {"comments": [{"text": "Build report [sdlc-agentic-run:run-1]"}]}
            )

        def close(self):
            self.closed = True

    session = CommentSession()
    monkeypatch.setattr(executor, "_session", lambda: session)

    result = executor(ADOAction(command="get_comments", ticket_key="123"))

    assert not result.is_error
    assert "[sdlc-agentic-run:run-1]" in result.text
    assert session.closed


def test_jira_get_comments_returns_provider_comment_text(monkeypatch):
    jira = SimpleNamespace(
        issue=lambda *_args, **_kwargs: SimpleNamespace(
            fields=SimpleNamespace(
                comment=SimpleNamespace(
                    comments=[
                        SimpleNamespace(body="Build report [sdlc-agentic-run:run-1]")
                    ]
                )
            )
        )
    )
    monkeypatch.setattr("backend.jira_tool.JIRA", lambda **_kwargs: jira)
    executor = JiraExecutor(
        base_url="https://example.atlassian.net",
        user_email="bot@example.com",
        api_token="token",
    )

    result = executor(JiraAction(command="get_comments", ticket_key="PROJ-123"))

    assert not result.is_error
    assert "[sdlc-agentic-run:run-1]" in result.text
