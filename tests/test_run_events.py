"""Unit tests for structured, queryable run events."""

import logging
import sqlite3

import pytest

from backend import run_events, runs_db


@pytest.fixture
def events(tmp_path, monkeypatch):
    monkeypatch.setattr(runs_db, "DB_PATH", tmp_path / "events.db")
    return run_events


def _record(
    events=run_events,
    *,
    stage="gate",
    state="success",
    message=None,
    app_id="checkout",
    user_id="octocat",
    run_id="r1",
    ticket_key="T-1",
    level=None,
):
    events.record(
        run_id,
        app_id,
        ticket_key,
        stage,
        state,
        message,
        user_id=user_id,
        level=level,
    )


def test_recorded_event_has_structured_fields(events):
    _record(events, state="human", message="Ticket needs more detail")

    [row] = events.query()
    assert row["run_id"] == "r1"
    assert row["app_id"] == "checkout"
    assert row["user_id"] == "octocat"
    assert row["ticket_key"] == "T-1"
    assert (row["stage"], row["state"], row["level"]) == (
        "gate",
        "human",
        "warning",
    )
    assert row["message"] == "Ticket needs more detail"
    assert row["is_trace"] is False
    assert row["created_at"].endswith("+00:00")


def test_events_are_returned_newest_first(events):
    _record(events, stage="fetch")
    _record(events, stage="gate")

    assert [event["stage"] for event in events.query()] == ["gate", "fetch"]


@pytest.mark.parametrize(
    ("state", "message", "level"),
    [
        ("success", "ok", "info"),
        ("in-progress", None, "info"),
        ("human", "needs a person", "warning"),
        ("failed", "boom", "error"),
        ("error", "crash", "error"),
        ("in-progress", "[WARN] Comment not posted", "warning"),
        ("in-progress", "[AGENT-ERROR] invalid tool call", "error"),
        ("in-progress", "[RESULT:terminal] FAILED", "warning"),
    ],
)
def test_level_is_derived_from_state_and_message(events, state, message, level):
    assert events.level_for(state, message) == level


def test_explicit_event_level_is_preserved(events):
    _record(
        state="in-progress",
        message="Retrying the ticket-provider request",
        level="warning",
    )

    [event] = events.query()
    assert (event["state"], event["level"]) == ("in-progress", "warning")


def test_agent_trace_is_hidden_unless_requested(events):
    _record(stage="agent", state="in-progress", message="[TOOL:terminal] git status")
    _record(stage="agent", state="in-progress", message="[RESULT:terminal] ok")
    _record(stage="agent", state="success", message="Modified: app.py")

    assert [event["message"] for event in events.query()] == ["Modified: app.py"]
    all_events = events.query(include_trace=True)
    assert len(all_events) == 3
    assert sum(event["is_trace"] for event in all_events) == 2


def test_filters_support_app_user_stage_and_comma_separated_values(events):
    _record(app_id="checkout", user_id="octocat", stage="gate", state="human")
    _record(
        app_id="checkout",
        user_id="octocat",
        stage="gate",
        state="failed",
        run_id="r2",
        ticket_key="T-2",
    )
    _record(
        app_id="checkout",
        user_id="other-user",
        stage="pr",
        state="success",
        run_id="r3",
        ticket_key="T-3",
    )
    _record(
        app_id="billing",
        user_id="octocat",
        stage="gate",
        state="human",
        run_id="r4",
    )

    assert len(events.query(app_id="checkout")) == 3
    assert len(events.query(user_id="octocat")) == 3
    assert len(events.query(app_id="checkout", user_id="octocat")) == 2
    assert len(events.query(stage="gate", state="failed,human")) == 3
    assert [event["ticket_key"] for event in events.query(run_id="r4")] == ["T-1"]
    assert [event["run_id"] for event in events.query(ticket_key="T-2")] == ["r2"]
    assert len(events.query(level="error")) == 1


def test_time_filters_and_shorthands(events, monkeypatch):
    stamps = iter(
        [
            "2026-10-01T10:00:00.000000+00:00",
            "2026-10-10T10:00:00.000000+00:00",
        ]
    )
    monkeypatch.setattr(events, "_now", lambda: next(stamps))
    _record(stage="fetch")
    _record(stage="gate")

    assert [event["stage"] for event in events.query(since="2026-10-05")] == ["gate"]
    assert [event["stage"] for event in events.query(until="2026-10-05")] == ["fetch"]
    assert events.query(since="1h") == []
    assert events.parse_time("24h") < events.parse_time("1h")
    assert events.parse_time("7d") < events.parse_time("24h")
    assert events.parse_time("2026-10-14T09:00:00Z") == (
        "2026-10-14T09:00:00.000000+00:00"
    )
    with pytest.raises(ValueError, match="Invalid time"):
        events.parse_time("last week")


@pytest.mark.parametrize("limit", [0, -1, 501])
def test_limit_is_validated(events, limit):
    with pytest.raises(ValueError, match="limit"):
        events.query(limit=limit)


def test_limit_caps_results(events):
    for index in range(5):
        _record(stage=f"stage-{index}")

    assert len(events.query(limit=2)) == 2


def test_filters_are_parameterized(events):
    _record()

    assert events.query(stage="x'; DROP TABLE run_events; --") == []
    assert len(events.query()) == 1


def test_long_messages_are_truncated(events):
    _record(message="x" * 10_000)

    assert len(events.query()[0]["message"]) == events.MAX_MESSAGE_CHARS


def test_record_failure_is_logged_without_raising(events, monkeypatch, caplog):
    def fail_insert(_row):
        raise RuntimeError("disk unavailable")

    monkeypatch.setattr(events, "_insert", fail_insert)
    with caplog.at_level(logging.ERROR, logger="backend.run_events"):
        _record()

    assert "Run event could not be recorded" in caplog.text
    assert "disk unavailable" in caplog.text


def test_record_recreates_a_missing_events_table(events):
    _record(stage="fetch")
    conn = runs_db._get_conn()
    conn.execute("DROP TABLE run_events")
    conn.commit()
    conn.close()

    _record(stage="gate")

    assert [event["stage"] for event in events.query()] == ["gate"]


def test_stage_outcomes_count_only_final_states(events):
    _record(state="human", user_id="octocat")
    _record(state="human", user_id="octocat", run_id="r2")
    _record(state="success", user_id="octocat", run_id="r3")
    _record(state="in-progress", user_id="octocat", run_id="r4")
    _record(
        app_id="billing",
        user_id="octocat",
        state="human",
        run_id="r5",
    )

    rows = {
        (row["stage"], row["state"]): row["count"]
        for row in events.stage_outcomes(app_id="checkout", user_id="octocat")
    }
    assert rows == {("gate", "human"): 2, ("gate", "success"): 1}
    assert {
        (row["stage"], row["state"]): row["count"]
        for row in events.stage_outcomes()
    }[("gate", "human")] == 3


def test_prune_removes_only_old_events(events, monkeypatch):
    with monkeypatch.context() as old_clock:
        old_clock.setattr(
            events,
            "_now",
            lambda: "2020-01-01T00:00:00.000000+00:00",
        )
        _record(stage="old")
    _record(stage="new")

    assert events.prune(30) == 1
    assert [event["stage"] for event in events.query()] == ["new"]
    with pytest.raises(ValueError, match="at least 1"):
        events.prune(0)


def test_event_table_shares_the_runs_database(events):
    _record(events)

    conn = sqlite3.connect(runs_db.DB_PATH)
    table_names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    conn.close()

    assert "run_events" in table_names


def test_existing_event_table_is_migrated_for_user_id(events):
    conn = runs_db._get_conn()
    conn.execute(
        """
        CREATE TABLE run_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            app_id TEXT NOT NULL,
            ticket_key TEXT,
            stage TEXT NOT NULL,
            state TEXT NOT NULL,
            level TEXT NOT NULL,
            message TEXT,
            is_trace INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    _record(events)

    assert events.query()[0]["user_id"] == "octocat"
