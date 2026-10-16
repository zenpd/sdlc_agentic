"""Inbound Jira webhook authentication, qualification, and queueing tests."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import api_server, apps_store, oauth_store, runs_db
from backend.jira_bot import PipelineConfig


client = TestClient(api_server.app)


@pytest.fixture
def database(tmp_path, monkeypatch):
    database_path = tmp_path / "webhooks.db"
    for module in (runs_db, oauth_store, apps_store):
        monkeypatch.setattr(module, "DB_PATH", database_path)
    runs_db.init_db()
    oauth_store.init_db()
    apps_store.init_db()
    apps_store.create_app(
        "Checkout",
        config={
            "TICKET_PROVIDER": "jira",
            "JIRA_BASE_URL": "https://example.atlassian.net",
            "JIRA_USER_EMAIL": "bot@example.com",
            "JIRA_API_TOKEN": "jira-api-token",
            "JIRA_JQL": 'labels = "Agent-ready" AND status = "Ready for Agent"',
            "JIRA_WEBHOOK_TOKEN": "webhook-secret",
        },
    )


def _config():
    return PipelineConfig.from_settings(
        {"TICKET_PROVIDER": "jira"},
        app_id="checkout",
        app_name="Checkout",
    )


def _post(payload=None, token="webhook-secret", delivery_id="delivery-1"):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if delivery_id is not None:
        headers["X-Atlassian-Webhook-Identifier"] = delivery_id
    return client.post(
        "/api/webhooks/jira/checkout",
        json=payload or {
            "webhookEvent": "jira:issue_updated",
            "issue": {"key": "PAY-42"},
        },
        headers=headers,
    )


@pytest.mark.usefixtures("database")
def test_authenticated_qualifying_issue_queues_pipeline_once(monkeypatch):
    jql_checks = []
    queued = []
    monkeypatch.setattr(
        api_server,
        "jira_ticket_matches_jql",
        lambda **kwargs: jql_checks.append(kwargs) or True,
    )
    monkeypatch.setattr(api_server, "_pipeline_config", lambda _app_id: _config())

    class DeferredThread:
        def __init__(self, *, target, args, daemon):
            queued.append((target, args))
            assert daemon is True

        def start(self):
            return None

    monkeypatch.setattr(
        api_server,
        "threading",
        SimpleNamespace(Thread=DeferredThread),
    )

    response = _post()

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["app_id"] == "checkout"
    assert jql_checks[0]["ticket_key"] == "PAY-42"
    assert jql_checks[0]["jql"] == (
        'labels = "Agent-ready" AND status = "Ready for Agent"'
    )
    assert len(queued) == 1
    assert queued[0][1][1] == "PAY-42"
    assert queued[0][1][3] is None
    assert queued[0][1][4]["JIRA_WEBHOOK_TOKEN"] == "webhook-secret"


@pytest.mark.usefixtures("database")
def test_delivery_identifier_prevents_replaying_a_webhook(monkeypatch):
    monkeypatch.setattr(api_server, "jira_ticket_matches_jql", lambda **_kwargs: True)
    monkeypatch.setattr(api_server, "_pipeline_config", lambda _app_id: _config())
    queued = []

    class DeferredThread:
        def __init__(self, *, target, args, daemon):
            assert callable(target)
            assert daemon is True
            queued.append(args)

        def start(self):
            return None

    monkeypatch.setattr(
        api_server,
        "threading",
        SimpleNamespace(Thread=DeferredThread),
    )

    first = _post()
    duplicate = _post()

    assert first.json()["status"] == "queued"
    assert duplicate.json() == {
        "status": "ignored",
        "reason": "duplicate_delivery",
    }
    assert len(queued) == 1


@pytest.mark.usefixtures("database")
def test_active_ticket_suppresses_new_events_and_remembers_delivery(monkeypatch):
    monkeypatch.setattr(api_server, "jira_ticket_matches_jql", lambda **_kwargs: True)
    monkeypatch.setattr(api_server, "_pipeline_config", lambda _app_id: _config())

    queued = []

    class DeferredThread:
        def __init__(self, *, target, args, daemon):
            assert callable(target)
            assert daemon is True
            queued.append(args)

        def start(self):
            return None

    monkeypatch.setattr(
        api_server,
        "threading",
        SimpleNamespace(Thread=DeferredThread),
    )

    first = _post(delivery_id="delivery-1")
    while_running = _post(delivery_id="delivery-2")
    retried = _post(delivery_id="delivery-2")

    assert first.json()["status"] == "queued"
    assert while_running.json()["reason"] == "already_running"
    assert retried.json()["reason"] == "duplicate_delivery"
    assert len(queued) == 1
    assert len(runs_db.get_runs(app_id="checkout")) == 1


@pytest.mark.usefixtures("database")
def test_bad_credentials_do_not_reach_jira_or_queue_run(monkeypatch):
    checked = []
    monkeypatch.setattr(
        api_server,
        "jira_ticket_matches_jql",
        lambda **kwargs: checked.append(kwargs) or True,
    )

    response = _post(token="wrong-token")

    assert response.status_code == 401
    assert checked == []
    assert runs_db.get_runs(app_id="checkout") == []


@pytest.mark.usefixtures("database")
def test_non_matching_or_unsupported_events_are_ignored(monkeypatch):
    monkeypatch.setattr(api_server, "jira_ticket_matches_jql", lambda **_kwargs: False)
    monkeypatch.setattr(api_server, "_pipeline_config", lambda _app_id: _config())

    not_ready = _post(delivery_id=None)
    unsupported = _post(
        payload={
            "webhookEvent": "jira:issue_deleted",
            "issue": {"key": "PAY-42"},
        },
        delivery_id=None,
    )

    assert not_ready.json()["reason"] == "issue_does_not_match_jql"
    assert unsupported.json()["reason"] == "unsupported_event"
    assert runs_db.get_runs(app_id="checkout") == []


@pytest.mark.usefixtures("database")
def test_invalid_payload_does_not_queue_run():
    response = _post(payload={"issue": {"key": "not-a-valid-key"}})

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid Jira issue key"
    assert runs_db.get_runs(app_id="checkout") == []


@pytest.mark.usefixtures("database")
def test_missing_webhook_token_is_reported_as_not_configured():
    apps_store.create_app(
        "No token",
        config={
            "TICKET_PROVIDER": "jira",
            "JIRA_BASE_URL": "https://example.atlassian.net",
            "JIRA_USER_EMAIL": "bot@example.com",
            "JIRA_API_TOKEN": "jira-api-token",
        },
    )

    response = client.post(
        "/api/webhooks/jira/no-token",
        json={"issue": {"key": "PAY-42"}},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Jira webhook authentication is not configured for this application"
    )


def test_jira_jql_matcher_adds_exact_key_and_preserves_ordering(monkeypatch):
    captured = {}

    class Issue:
        key = "PAY-42"

    class FakeJira:
        def __init__(self, *, server, basic_auth):
            captured["connection"] = (server, basic_auth)

        def search_issues(self, jql, **kwargs):
            captured["jql"] = jql
            captured["kwargs"] = kwargs
            return [Issue()]

    monkeypatch.setattr("backend.jira_tool.JIRA", FakeJira)

    assert api_server.jira_ticket_matches_jql(
        base_url="https://example.atlassian.net",
        user_email="bot@example.com",
        api_token="api-token",
        jql='labels = "Agent-ready" ORDER BY created DESC',
        ticket_key="PAY-42",
    )
    assert captured["connection"] == (
        "https://example.atlassian.net",
        ("bot@example.com", "api-token"),
    )
    assert captured["jql"] == '(labels = "Agent-ready") AND key = PAY-42'
    assert captured["kwargs"] == {"maxResults": 1, "fields": "key"}
