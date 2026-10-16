"""Inbound Azure DevOps Service Hook authentication, qualification, and queueing tests."""

import base64
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import ado_tool, api_server, apps_store, oauth_store, runs_db
from backend.jira_bot import PipelineConfig


client = TestClient(api_server.app)
WEBHOOK_TOKEN = "ado-webhook-secret"
WIQL = (
    "SELECT [System.Id] FROM WorkItems "
    "WHERE [System.Tags] CONTAINS 'Agent-ready' AND [System.State] = 'New'"
)


@pytest.fixture
def database(tmp_path, monkeypatch):
    database_path = tmp_path / "ado-webhooks.db"
    for module in (runs_db, oauth_store, apps_store):
        monkeypatch.setattr(module, "DB_PATH", database_path)
    runs_db.init_db()
    oauth_store.init_db()
    apps_store.init_db()
    apps_store.create_app(
        "Checkout",
        config={
            "TICKET_PROVIDER": "ado",
            "ADO_ORG_URL": "https://dev.azure.com/example",
            "ADO_PROJECT": "Checkout",
            "ADO_PAT": "ado-pat",
            "ADO_WIQL": WIQL,
            "ADO_WEBHOOK_TOKEN": WEBHOOK_TOKEN,
        },
    )


def _config():
    return PipelineConfig.from_settings(
        {"TICKET_PROVIDER": "ado", "ADO_WIQL": WIQL},
        app_id="checkout",
        app_name="Checkout",
    )


def _auth(token=WEBHOOK_TOKEN, username="sdlc-agentic"):
    credentials = base64.b64encode(f"{username}:{token}".encode()).decode()
    return {"Authorization": f"Basic {credentials}"}


def _post(payload=None, token=WEBHOOK_TOKEN, username="sdlc-agentic"):
    return client.post(
        "/api/webhooks/ado/checkout",
        json=payload
        or {
            "id": "delivery-1",
            "eventType": "workitem.updated",
            "resource": {"id": 616},
        },
        headers=_auth(token, username),
    )


@pytest.mark.usefixtures("database")
def test_authenticated_matching_work_item_queues_pipeline(monkeypatch):
    wiql_checks = []
    queued = []
    monkeypatch.setattr(
        api_server,
        "ado_ticket_matches_wiql",
        lambda **kwargs: wiql_checks.append(kwargs) or True,
    )
    monkeypatch.setattr(api_server, "_pipeline_config", lambda _app_id: _config())

    class DeferredThread:
        def __init__(self, *, target, args, daemon):
            queued.append((target, args))
            assert daemon is True

        def start(self):
            return None

    monkeypatch.setattr(api_server, "threading", SimpleNamespace(Thread=DeferredThread))

    response = _post()

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["app_id"] == "checkout"
    assert wiql_checks == [
        {
            "org_url": "https://dev.azure.com/example",
            "project": "Checkout",
            "pat": "ado-pat",
            "wiql": WIQL,
            "work_item_id": "616",
        }
    ]
    assert len(queued) == 1
    assert queued[0][1][1] == "616"
    assert queued[0][1][3] is None


@pytest.mark.usefixtures("database")
def test_delivery_identifier_prevents_replaying_a_service_hook(monkeypatch):
    monkeypatch.setattr(api_server, "ado_ticket_matches_wiql", lambda **_kwargs: True)
    monkeypatch.setattr(api_server, "_pipeline_config", lambda _app_id: _config())
    queued = []

    class DeferredThread:
        def __init__(self, *, target, args, daemon):
            queued.append(args)
            assert callable(target)
            assert daemon is True

        def start(self):
            return None

    monkeypatch.setattr(api_server, "threading", SimpleNamespace(Thread=DeferredThread))

    first = _post()
    duplicate = _post()

    assert first.json()["status"] == "queued"
    assert duplicate.json() == {
        "status": "ignored",
        "reason": "duplicate_delivery",
    }
    assert len(queued) == 1


@pytest.mark.usefixtures("database")
def test_nonmatching_and_unsupported_events_are_ignored(monkeypatch):
    monkeypatch.setattr(api_server, "ado_ticket_matches_wiql", lambda **_kwargs: False)
    monkeypatch.setattr(api_server, "_pipeline_config", lambda _app_id: _config())

    nonmatching = _post(payload={"id": "delivery-2", "eventType": "workitem.created", "resource": {"id": 616}})
    unsupported = _post(
        payload={"id": "delivery-3", "eventType": "workitem.deleted", "resource": {"id": 616}}
    )

    assert nonmatching.json() == {
        "status": "ignored",
        "reason": "work_item_does_not_match_wiql",
    }
    assert unsupported.json() == {"status": "ignored", "reason": "unsupported_event"}
    assert runs_db.get_runs(app_id="checkout") == []


@pytest.mark.usefixtures("database")
def test_bad_credentials_do_not_query_ado_or_queue(monkeypatch):
    checked = []
    monkeypatch.setattr(
        api_server,
        "ado_ticket_matches_wiql",
        lambda **kwargs: checked.append(kwargs) or True,
    )

    response = _post(token="wrong-token")

    assert response.status_code == 401
    assert checked == []
    assert runs_db.get_runs(app_id="checkout") == []
    assert _post(username="wrong-user").status_code == 401


@pytest.mark.usefixtures("database")
def test_missing_secret_is_reported_as_not_configured():
    apps_store.create_app("No token", config={"TICKET_PROVIDER": "ado"})

    response = client.post(
        "/api/webhooks/ado/no-token",
        json={"id": "delivery-1", "eventType": "workitem.updated", "resource": {"id": 616}},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Azure DevOps webhook authentication is not configured for this application"
    )


@pytest.mark.usefixtures("database")
def test_invalid_work_item_payload_does_not_queue():
    response = _post(
        payload={"id": "delivery-1", "eventType": "workitem.updated", "resource": {"id": "bad"}}
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid Azure DevOps work item ID"
    assert runs_db.get_runs(app_id="checkout") == []


@pytest.mark.usefixtures("database")
def test_malformed_event_type_and_oversized_work_item_id_are_rejected():
    invalid_event = _post(payload={"eventType": [], "resource": {"id": 616}})
    oversized_id = _post(
        payload={
            "id": "delivery-2",
            "eventType": "workitem.updated",
            "resource": {"id": "9" * 10000},
        }
    )

    assert invalid_event.status_code == 400
    assert invalid_event.json()["detail"] == "Webhook payload is missing eventType"
    assert oversized_id.status_code == 400
    assert oversized_id.json()["detail"] == "Invalid Azure DevOps work item ID"


def test_wiql_matcher_adds_exact_work_item_id_and_preserves_ordering(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"workItems": [{"id": 616}]}

    class FakeSession:
        auth = None

        def post(self, url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return Response()

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(ado_tool.requests, "Session", FakeSession)

    assert ado_tool.ado_ticket_matches_wiql(
        org_url="https://dev.azure.com/example/",
        project="Checkout",
        pat="ado-pat",
        wiql=f"{WIQL} ORDER BY [System.ChangedDate] DESC",
        work_item_id="616",
    )
    assert captured["url"] == (
        "https://dev.azure.com/example/Checkout/_apis/wit/wiql"
    )
    assert captured["kwargs"]["params"] == {"api-version": "7.1"}
    assert captured["kwargs"]["json"] == {
        "query": f"({WIQL}) AND [System.Id] = 616"
    }
    assert captured["kwargs"]["headers"] == {"Content-Type": "application/json"}
    assert captured["closed"] is True


@pytest.mark.parametrize("work_item_id", ["0", "-1", "1" * 11, "abc"])
def test_wiql_matcher_rejects_invalid_work_item_ids(work_item_id):
    with pytest.raises(ValueError, match="Invalid Azure DevOps work item ID"):
        ado_tool.ado_ticket_matches_wiql(
            org_url="https://dev.azure.com/example",
            project="Checkout",
            pat="ado-pat",
            wiql=WIQL,
            work_item_id=work_item_id,
        )
