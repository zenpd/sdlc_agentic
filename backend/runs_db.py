import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "runs.db"
_lock = threading.Lock()

STEP_KEYS = ["fetch", "assign", "in_progress", "gate", "agent", "pr", "jira"]


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            ticket_key TEXT NOT NULL,
            status TEXT NOT NULL,
            pr_url TEXT,
            started_at TEXT NOT NULL,
            duration_sec INTEGER,
            steps TEXT NOT NULL,
            logs TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def create_run(run_id: str, ticket_key: str, started_at: str) -> None:
    steps = [{"key": k, "label": k, "state": "pending"} for k in STEP_KEYS]
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO runs (id, ticket_key, status, pr_url, started_at, duration_sec, steps, logs) "
            "VALUES (?, ?, 'in-progress', NULL, ?, NULL, ?, ?)",
            (run_id, ticket_key, started_at, json.dumps(steps), json.dumps({})),
        )
        conn.commit()
        conn.close()


def update_step(run_id: str, step_key: str, state: str, log: Optional[str] = None) -> None:
    with _lock:
        conn = _get_conn()
        row = conn.execute("SELECT steps, logs FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            conn.close()
            return
        steps = json.loads(row["steps"])
        for s in steps:
            if s["key"] == step_key:
                s["state"] = state
        logs = json.loads(row["logs"])
        if log:
            logs.setdefault(step_key, []).append(log)
        conn.execute(
            "UPDATE runs SET steps = ?, logs = ? WHERE id = ?",
            (json.dumps(steps), json.dumps(logs), run_id),
        )
        conn.commit()
        conn.close()


def finish_run(run_id: str, status: str, pr_url: Optional[str], duration_sec: int) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE runs SET status = ?, pr_url = ?, duration_sec = ? WHERE id = ?",
            (status, pr_url, duration_sec, run_id),
        )
        conn.commit()
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "ticket_key": row["ticket_key"],
        "status": row["status"],
        "pr_url": row["pr_url"],
        "started_at": row["started_at"],
        "duration_sec": row["duration_sec"],
        "steps": json.loads(row["steps"]),
        "logs": json.loads(row["logs"]),
    }


def get_run(run_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def get_runs(limit: int = 50) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_stats() -> dict:
    conn = _get_conn()
    total = conn.execute("SELECT COUNT(*) c FROM runs").fetchone()["c"]
    success = conn.execute(
        "SELECT COUNT(*) c FROM runs WHERE status = 'success'"
    ).fetchone()["c"]
    failed = conn.execute(
        "SELECT COUNT(*) c FROM runs WHERE status IN ('failed', 'error')"
    ).fetchone()["c"]
    human = conn.execute(
        "SELECT COUNT(*) c FROM runs WHERE status = 'human'"
    ).fetchone()["c"]

    success_rate = round((success / total) * 100) if total else 0

    avg_row = conn.execute(
        "SELECT AVG(duration_sec) a FROM runs WHERE duration_sec IS NOT NULL"
    ).fetchone()
    avg_duration_sec = int(avg_row["a"]) if avg_row["a"] is not None else None

    daily = []
    today = datetime.now(timezone.utc).date()
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        row = conn.execute(
            """
            SELECT
              SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) s,
              SUM(CASE WHEN status IN ('failed', 'error') THEN 1 ELSE 0 END) f
            FROM runs WHERE substr(started_at, 1, 10) = ?
            """,
            (day.isoformat(),),
        ).fetchone()
        daily.append({
            "date": day.strftime("%b %d"),
            "success": row["s"] or 0,
            "failed": row["f"] or 0,
        })

    conn.close()
    return {
        "total": total,
        "success": success,
        "failed": failed,
        "human": human,
        "success_rate": success_rate,
        "avg_duration_sec": avg_duration_sec,
        "daily": daily,
    }
