"""API tests for structured run-event recording and querying."""

from datetime import datetime, timedelta, timezone

import pytest

from conftest import client, sign_in
from backend import api_server, apps_store, runs_db
from backend.jira_bot import PipelineConfig, PipelineHalt


@pytest.fixture
def database():
    """conftest gives every test a fresh throwaway DB; these tests also need
    the applications their runs belong to, and a signed-in admin to query them."""
    apps_store.create_app("Checkout")
    apps_store.create_app("Billing")
    sign_in("events-admin", "admin")


def _config(app_id="default"):
    return PipelineConfig.from_settings({}, app_id=app_id, app_name=app_id)


def _events(**params):
    response = client.get("/api/events", params=params)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.usefixtures("database")
def test_halted_run_records_stage_events_and_final_status(monkeypatch):
    def fake_pipeline(ticket_key, on_step, **_kwargs):
        on_step("fetch", "success", f"{ticket_key}: fetched")
        on_step("agent", "in-progress", "[TOOL:terminal] git status")
        on_step("gate", "human", "Ticket needs more detail")
        raise PipelineHalt("human", "Ticket needs more detail")

    monkeypatch.setattr(api_server, "run_pipeline", fake_pipeline)
    runs_db.create_run("r1", "T-1", "2026-10-14T10:00:00+00:00", "checkout")
    api_server._execute("r1", "T-1", _config("checkout"), "octocat")

    events = _events(app_id="checkout", user_id="octocat")
    assert [
        (event["run_id"], event["stage"], event["state"])
        for event in reversed(events)
    ] == [
        ("r1", "fetch", "success"),
        ("r1", "gate", "human"),
        ("r1", "run", "human"),
    ]
    assert len(_events(include_trace=True, run_id="r1")) == 4
    assert runs_db.get_run("r1")["status"] == "human"


@pytest.mark.usefixtures("database")
def test_pipeline_progress_logs_are_queryable_by_app_user_and_stage(monkeypatch):
    def fake_pipeline(ticket_key, on_step, on_log, **_kwargs):
        assert ticket_key == "T-1"
        on_log("agent", "warning", "Stack modifier detection skipped")
        on_step("agent", "success", "Modified: app.py")
        return {"pr_url": None}

    monkeypatch.setattr(api_server, "run_pipeline", fake_pipeline)
    runs_db.create_run("r1", "T-1", "2026-10-14T10:00:00+00:00", "checkout")
    api_server._execute("r1", "T-1", _config("checkout"), "octocat")

    [event] = _events(
        app_id="checkout",
        user_id="octocat",
        stage="agent",
        level="warning",
    )
    assert event["run_id"] == "r1"
    assert event["state"] == "in-progress"
    assert event["message"] == "Stack modifier detection skipped"


@pytest.mark.usefixtures("database")
def test_failed_gate_query_can_be_scoped_to_app_user_and_week(monkeypatch):
    def fake_pipeline(**kwargs):
        kwargs["on_step"]("gate", "human", "Ticket needs more detail")
        raise PipelineHalt("human", "Ticket needs more detail")

    monkeypatch.setattr(api_server, "run_pipeline", fake_pipeline)
    for run_id, app_id, user_id in (
        ("r1", "checkout", "octocat"),
        ("r2", "billing", "octocat"),
        ("r3", "checkout", "another-user"),
    ):
        runs_db.create_run(
            run_id,
            "T-1",
            "2026-10-14T10:00:00+00:00",
            app_id,
        )
        api_server._execute(run_id, "T-1", _config(app_id), user_id)

    found = _events(
        app_id="checkout",
        user_id="octocat",
        stage="gate",
        state="failed,human",
        since="7d",
    )
    assert [(event["run_id"], event["stage"]) for event in found] == [
        ("r1", "gate")
    ]


@pytest.mark.usefixtures("database")
def test_success_and_unhandled_failure_record_run_outcomes(monkeypatch):
    runs_db.create_run("ok", "T-1", "2026-10-14T10:00:00+00:00", "checkout")
    monkeypatch.setattr(
        api_server,
        "run_pipeline",
        lambda **_: {"pr_url": "https://github.com/example/repo/pull/1"},
    )
    api_server._execute("ok", "T-1", _config("checkout"), "octocat")

    def fail_pipeline(**_kwargs):
        raise RuntimeError("pipeline crashed")

    runs_db.create_run("bad", "T-2", "2026-10-14T10:00:00+00:00", "checkout")
    monkeypatch.setattr(api_server, "run_pipeline", fail_pipeline)
    api_server._execute("bad", "T-2", _config("checkout"), "octocat")

    outcomes = {
        (event["run_id"], event["state"])
        for event in _events(stage="run", user_id="octocat")
    }
    assert outcomes == {("ok", "success"), ("bad", "error")}
    bad_event = _events(run_id="bad", stage="run")[0]
    assert "pipeline crashed" in bad_event["message"]
    assert runs_db.get_run("bad")["status"] == "error"
    assert runs_db.get_run("ok")["cost_status"] == "unavailable"


@pytest.mark.usefixtures("database")
def test_run_token_and_cost_usage_is_saved_and_scoped_to_dashboard_stats(monkeypatch):
    def fake_pipeline(ticket_key, on_usage, **_kwargs):
        assert ticket_key == "T-1"
        on_usage("gate", 120, 30, 0.004, 1, 1)
        on_usage("agent", 800, 200, None, 2, 1)
        return {"pr_url": "https://github.com/example/repo/pull/1"}

    monkeypatch.setattr(api_server, "run_pipeline", fake_pipeline)
    started_at = datetime.now(timezone.utc).isoformat()
    runs_db.create_run("r1", "T-1", started_at, "checkout")
    api_server._execute("r1", "T-1", _config("checkout"), "octocat")

    run = runs_db.get_run("r1")
    assert run is not None
    assert (run["prompt_tokens"], run["completion_tokens"], run["total_tokens"]) == (
        920,
        230,
        1150,
    )
    assert run["cost_usd"] == pytest.approx(0.004)
    assert run["cost_status"] == "partial"
    assert (run["llm_request_count"], run["priced_request_count"]) == (3, 2)

    response = client.get("/api/stats", params={"app_id": "checkout"})
    assert response.status_code == 200
    stats = response.json()
    assert stats["monthly_cost_usd"] == pytest.approx(0.004)
    assert stats["monthly_prompt_tokens"] == 920
    assert stats["monthly_completion_tokens"] == 230
    assert stats["monthly_usage_runs"] == 1
    assert stats["monthly_incomplete_cost_runs"] == 1
    billing_stats = client.get("/api/stats", params={"app_id": "billing"}).json()
    assert billing_stats["monthly_cost_usd"] is None


@pytest.mark.usefixtures("database")
def test_monthly_usage_is_attributed_to_usage_month_not_run_start():
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1)
    previous_month = month_start - timedelta(days=1)
    runs_db.create_run(
        "started-previous-month",
        "T-1",
        previous_month.isoformat(),
        "checkout",
    )
    runs_db.record_usage("started-previous-month", 500, 100, 0.25, 1, 1)

    runs_db.create_run(
        "used-previous-month",
        "T-2",
        now.isoformat(),
        "checkout",
    )
    runs_db.record_usage("used-previous-month", 200, 50, 0.10, 1, 1)
    conn = runs_db._get_conn()
    conn.execute(
        "UPDATE run_usage SET recorded_at = ? WHERE run_id = ?",
        (previous_month.isoformat(), "used-previous-month"),
    )
    conn.commit()
    conn.close()

    stats = client.get("/api/stats", params={"app_id": "checkout"}).json()
    assert stats["monthly_cost_usd"] == pytest.approx(0.25)
    assert stats["monthly_prompt_tokens"] == 500
    assert stats["monthly_completion_tokens"] == 100
    assert stats["monthly_usage_runs"] == 1


@pytest.mark.usefixtures("database")
def test_legacy_run_usage_is_backfilled_once_using_run_start_month():
    started_at = datetime.now(timezone.utc).isoformat()
    runs_db.create_run("legacy", "T-1", started_at, "checkout")
    conn = runs_db._get_conn()
    conn.execute(
        """
        UPDATE runs
        SET prompt_tokens = 300, completion_tokens = 75, cost_usd = 0.12,
            llm_request_count = 2, priced_request_count = 2, cost_status = 'estimated'
        WHERE id = 'legacy'
        """
    )
    conn.commit()
    conn.close()

    runs_db.init_db()
    runs_db.init_db()
    stats = client.get("/api/stats", params={"app_id": "checkout"}).json()
    assert stats["monthly_cost_usd"] == pytest.approx(0.12)
    assert stats["monthly_prompt_tokens"] == 300
    assert stats["monthly_completion_tokens"] == 75
    assert stats["monthly_usage_runs"] == 1


@pytest.mark.usefixtures("database")
def test_run_outcome_notifies_configured_app_destinations(monkeypatch):
    sent = []
    monkeypatch.setattr(
        api_server.notifications,
        "send_run_notifications",
        lambda **kwargs: sent.append(kwargs),
    )
    monkeypatch.setattr(
        api_server,
        "run_pipeline",
        lambda **_: {"pr_url": "https://github.com/example/repo/pull/1"},
    )
    runs_db.create_run("notify", "T-1", "2026-10-14T10:00:00+00:00", "checkout")

    api_server._execute(
        "notify",
        "T-1",
        _config("checkout"),
        notification_settings={"NOTIFY_ON_SUCCESS": "true"},
    )

    assert len(sent) == 1
    assert sent[0]["app_id"] == "checkout"
    assert sent[0]["ticket_key"] == "T-1"
    assert sent[0]["status"] == "success"
    assert sent[0]["pr_url"] == "https://github.com/example/repo/pull/1"


@pytest.mark.usefixtures("database")
def test_notification_errors_do_not_change_pipeline_outcome(monkeypatch):
    def fail_delivery(**_kwargs):
        raise RuntimeError("notification service unavailable")

    monkeypatch.setattr(
        api_server.notifications,
        "send_run_notifications",
        fail_delivery,
    )
    monkeypatch.setattr(
        api_server,
        "run_pipeline",
        lambda **_: {"pr_url": None},
    )
    runs_db.create_run("notify-failure", "T-1", "2026-10-14T10:00:00+00:00")

    api_server._execute(
        "notify-failure",
        "T-1",
        _config(),
        notification_settings={"NOTIFY_ON_SUCCESS": "true"},
    )

    assert runs_db.get_run("notify-failure")["status"] == "success"


@pytest.mark.usefixtures("database")
def test_run_ticket_captures_github_login_as_event_user(monkeypatch):
    apps_store.update_app_config(
        "checkout",
        {
            "NOTIFY_ON_SUCCESS": "true",
            "SLACK_WEBHOOK_URL": "https://hooks.slack.test/app",
        },
    )
    config = _config("checkout")
    sent_notifications = []
    monkeypatch.setattr(api_server, "_pipeline_config", lambda _app_id: config)
    monkeypatch.setattr(
        api_server.notifications,
        "send_run_notifications",
        lambda **kwargs: sent_notifications.append(kwargs),
    )
    monkeypatch.setattr(
        api_server,
        "run_pipeline",
        lambda **_: {"pr_url": "https://github.com/example/repo/pull/1"},
    )

    class SynchronousThread:
        def __init__(self, *, target, args, daemon):
            self.target = target
            self.args = args
            assert daemon is True

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(api_server.threading, "Thread", SynchronousThread)
    sign_in("octocat", "runner")
    response = client.post(
        "/api/run-ticket",
        json={"ticket_key": "T-1", "app_id": "checkout"},
    )

    assert response.status_code == 200
    [event] = _events(user_id="octocat", stage="run")
    assert event["state"] == "success"
    assert event["ticket_key"] == "T-1"
    assert event["app_id"] == "checkout"
    assert len(sent_notifications) == 1
    assert sent_notifications[0]["settings"]["SLACK_WEBHOOK_URL"] == (
        "https://hooks.slack.test/app"
    )


@pytest.mark.usefixtures("database")
def test_summary_endpoint_counts_final_states_by_app_and_user(monkeypatch):
    monkeypatch.setattr(
        api_server,
        "run_pipeline",
        lambda **_: {"pr_url": "pull-request"},
    )
    runs_db.create_run("r1", "T-1", "2026-10-14T10:00:00+00:00", "checkout")
    api_server._execute("r1", "T-1", _config("checkout"), "octocat")

    response = client.get(
        "/api/events/summary",
        params={"app_id": "checkout", "user_id": "octocat"},
    )

    assert response.status_code == 200
    assert {"stage": "run", "state": "success", "count": 1} in response.json()


@pytest.mark.parametrize(
    "params",
    [
        {"since": "last week"},
        {"limit": 0},
        {"limit": 9999},
    ],
)
@pytest.mark.usefixtures("database")
def test_invalid_event_query_parameters_return_400(params):
    response = client.get("/api/events", params=params)

    assert response.status_code == 400


@pytest.mark.usefixtures("database")
def test_invalid_summary_time_returns_400():
    response = client.get("/api/events/summary", params={"since": "last week"})

    assert response.status_code == 400
