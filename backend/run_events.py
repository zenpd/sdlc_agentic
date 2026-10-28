"""Structured, queryable events emitted by pipeline runs."""

import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta

from backend import runs_db


logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 4000
MAX_LIMIT = 500
DEFAULT_LIMIT = 100

_TRACE_PREFIXES = ("[TOOL:", "[RESULT:", "[DIFF:", "[AGENT-ERROR]")
_SINCE_RE = re.compile(r"^(\d+)([hd])$")
_TERMINAL_STATES: tuple[str, ...] = (
    "success",
    "failed",
    "human",
    "error",
    "cancelled",
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def is_trace(message: str | None) -> bool:
    """Identify agent tool chatter that is useful only in trace views."""
    return bool(message) and message.startswith(_TRACE_PREFIXES)


def level_for(state: str, message: str | None) -> str:
    """Classify an event as informational, a warning, or an error."""
    text = message or ""
    if state in ("failed", "error") or text.startswith("[AGENT-ERROR]"):
        return "error"
    if state == "human" or text.startswith("[WARN]") or (
        text.startswith("[RESULT:") and text.rstrip().endswith("FAILED")
    ):
        return "warning"
    return "info"


def parse_time(value: str) -> str:
    """Normalize a duration shorthand or ISO date/time to UTC."""
    text = value.strip().lower()
    match = _SINCE_RE.fullmatch(text)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        delta = timedelta(hours=amount) if unit == "h" else timedelta(days=amount)
        moment = datetime.now(UTC) - delta
    else:
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(
                f"Invalid time {value!r}; use something like 24h, 7d or 2026-10-14."
            ) from None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _ensure() -> None:
    """Create or upgrade the events table in the current runs database."""
    with runs_db._lock:
        conn = runs_db._get_conn()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    app_id TEXT NOT NULL,
                    user_id TEXT,
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
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(run_events)").fetchall()
            }
            if "user_id" not in columns:
                conn.execute("ALTER TABLE run_events ADD COLUMN user_id TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_app_stage_time "
                "ON run_events (app_id, stage, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_user_time "
                "ON run_events (user_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_run ON run_events (run_id)"
            )
            conn.commit()
        finally:
            conn.close()


def _insert(row: tuple[object, ...]) -> None:
    _ensure()
    with runs_db._lock:
        conn = runs_db._get_conn()
        try:
            conn.execute(
                """
                INSERT INTO run_events
                    (run_id, app_id, user_id, ticket_key, stage, state, level,
                     message, is_trace, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            conn.commit()
        finally:
            conn.close()


def record(
    run_id: str,
    app_id: str | None,
    ticket_key: str | None,
    stage: str,
    state: str,
    message: str | None = None,
    user_id: str | None = None,
    level: str | None = None,
) -> None:
    """Persist an event without allowing a logging failure to fail a run."""
    text = (message or "")[:MAX_MESSAGE_CHARS] or None
    event_level = level or level_for(state, message)
    if event_level not in {"info", "warning", "error"}:
        raise ValueError(f"Unsupported event level: {event_level}")
    row = (
        run_id,
        app_id or "default",
        user_id,
        ticket_key,
        stage,
        state,
        event_level,
        text,
        int(is_trace(message)),
        _now(),
    )
    try:
        _insert(row)
    except Exception:
        logger.exception(
            "Run event could not be recorded",
            extra={"run_id": run_id, "app_id": app_id or "default", "stage": stage},
        )


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "app_id": row["app_id"],
        "user_id": row["user_id"],
        "ticket_key": row["ticket_key"],
        "stage": row["stage"],
        "state": row["state"],
        "level": row["level"],
        "message": row["message"],
        "is_trace": bool(row["is_trace"]),
        "created_at": row["created_at"],
    }


def _in_clause(column: str, value: str, clauses: list[str], params: list[str]) -> None:
    values = [item.strip() for item in value.split(",") if item.strip()]
    if values:
        clauses.append(f"{column} IN ({', '.join('?' for _ in values)})")
        params.extend(values)


def query(
    *,
    app_id: str | None = None,
    user_id: str | None = None,
    stage: str | None = None,
    state: str | None = None,
    level: str | None = None,
    run_id: str | None = None,
    ticket_key: str | None = None,
    since: str | None = None,
    until: str | None = None,
    include_trace: bool = False,
    limit: int = DEFAULT_LIMIT,
) -> list[dict]:
    """Return newest events matching the supplied filters."""
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")

    clauses: list[str] = []
    params: list[str] = []
    for column, value in (
        ("app_id", app_id),
        ("user_id", user_id),
        ("stage", stage),
        ("state", state),
        ("level", level),
        ("run_id", run_id),
        ("ticket_key", ticket_key),
    ):
        if value:
            _in_clause(column, value, clauses, params)
    if since:
        clauses.append("created_at >= ?")
        params.append(parse_time(since))
    if until:
        clauses.append("created_at < ?")
        params.append(parse_time(until))
    if not include_trace:
        clauses.append("is_trace = 0")

    where = " AND ".join(clauses) or "1 = 1"
    _ensure()
    conn = runs_db._get_conn()
    try:
        rows = conn.execute(
            f"SELECT * FROM run_events WHERE {where} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(row) for row in rows]


def stage_outcomes(
    app_id: str | None = None,
    user_id: str | None = None,
    since: str | None = None,
) -> list[dict]:
    """Count final stage outcomes, excluding intermediate and trace events."""
    clauses = [
        f"state IN ({', '.join('?' for _ in _TERMINAL_STATES)})",
        "is_trace = 0",
    ]
    params = list(_TERMINAL_STATES)
    if app_id:
        _in_clause("app_id", app_id, clauses, params)
    if user_id:
        _in_clause("user_id", user_id, clauses, params)
    if since:
        clauses.append("created_at >= ?")
        params.append(parse_time(since))

    _ensure()
    conn = runs_db._get_conn()
    try:
        rows = conn.execute(
            "SELECT stage, state, COUNT(*) AS count FROM run_events "
            f"WHERE {' AND '.join(clauses)} "
            "GROUP BY stage, state ORDER BY stage, state",
            params,
        ).fetchall()
    finally:
        conn.close()
    return [
        {"stage": row["stage"], "state": row["state"], "count": row["count"]}
        for row in rows
    ]


def prune(older_than_days: int) -> int:
    """Delete events older than the requested retention period."""
    if older_than_days < 1:
        raise ValueError("older_than_days must be at least 1")
    cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat(
        timespec="microseconds"
    )
    _ensure()
    with runs_db._lock:
        conn = runs_db._get_conn()
        try:
            cursor = conn.execute(
                "DELETE FROM run_events WHERE created_at < ?", (cutoff,)
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()
