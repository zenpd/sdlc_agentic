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
    # Every run belongs to an onboarded application (apps_store). Runs from
    # before applications existed belong to the "default" one.
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    if "app_id" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN app_id TEXT NOT NULL DEFAULT 'default'")
    # Who started the run (the signed-in user, not the agent persona).
    if "triggered_by" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN triggered_by TEXT")
    # Human sign-off before a PR opens (apps with REQUIRE_PR_APPROVAL).
    # approval_status: NULL (not needed) | pending | approved | rejected | expired
    for column in ("approval_status", "approval_by", "approval_note", "approval_at"):
        if column not in columns:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_app_started ON runs (app_id, started_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_triggered_by ON runs (triggered_by, started_at)")
    # A run's pipeline thread doesn't survive a server restart, so anything
    # still "running" or "awaiting approval" from before is dead — say so
    # instead of leaving it spinning (or offering Approve buttons) forever.
    conn.execute(
        "UPDATE runs SET status = 'error', "
        "approval_status = CASE WHEN approval_status = 'pending' THEN 'expired' ELSE approval_status END "
        "WHERE status IN ('in-progress', 'awaiting-approval')"
    )
    conn.commit()
    conn.close()


def _scope(app_id: Optional[str], app_ids: Optional[set] = None) -> tuple[str, tuple]:
    """WHERE-clause fragment limiting a query to one application, to a set of
    applications (what a user's teams can see), or to nothing."""
    if app_id:
        return " AND app_id = ?", (app_id,)
    if app_ids is not None:
        ids = sorted(app_ids)
        if not ids:
            return " AND 0", ()
        return f" AND app_id IN ({','.join('?' * len(ids))})", tuple(ids)
    return "", ()


def create_run(run_id: str, ticket_key: str, started_at: str, app_id: str = "default", triggered_by: Optional[str] = None) -> None:
    steps = [{"key": k, "label": k, "state": "pending"} for k in STEP_KEYS]
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO runs (id, ticket_key, status, pr_url, started_at, duration_sec, steps, logs, app_id, triggered_by) "
            "VALUES (?, ?, 'in-progress', NULL, ?, NULL, ?, ?, ?, ?)",
            (run_id, ticket_key, started_at, json.dumps(steps), json.dumps({}), app_id, triggered_by),
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


def set_status(run_id: str, status: str) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))
        conn.commit()
        conn.close()


def request_approval(run_id: str) -> None:
    """The pipeline is paused before opening a PR until someone decides."""
    with _lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE runs SET status = 'awaiting-approval', approval_status = 'pending', "
            "approval_by = NULL, approval_note = NULL, approval_at = NULL WHERE id = ?",
            (run_id,),
        )
        conn.commit()
        conn.close()


def decide_approval(run_id: str, decision: str, actor: str, note: str = "") -> bool:
    """Record an approve/reject. Only the first decision on a pending request
    counts — returns False if the run isn't waiting for one."""
    if decision not in ("approved", "rejected", "expired"):
        raise ValueError("decision must be approved, rejected or expired")
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "UPDATE runs SET approval_status = ?, approval_by = ?, approval_note = ?, approval_at = ? "
            "WHERE id = ? AND approval_status = 'pending'",
            (decision, actor, note, datetime.now(timezone.utc).isoformat(), run_id),
        )
        conn.commit()
        conn.close()
        return cur.rowcount == 1


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
        "app_id": row["app_id"],
        "triggered_by": row["triggered_by"],
        "ticket_key": row["ticket_key"],
        "status": row["status"],
        "pr_url": row["pr_url"],
        "started_at": row["started_at"],
        "duration_sec": row["duration_sec"],
        "steps": json.loads(row["steps"]),
        "logs": json.loads(row["logs"]),
        "approval": None if not row["approval_status"] else {
            "status": row["approval_status"],
            "by": row["approval_by"],
            "note": row["approval_note"],
            "at": row["approval_at"],
        },
    }


def get_run(run_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def get_runs(limit: int = 50, app_id: Optional[str] = None, triggered_by: Optional[str] = None,
             app_ids: Optional[set] = None) -> list[dict]:
    where, params = _scope(app_id, app_ids)
    if triggered_by:
        where += " AND triggered_by = ?"
        params = (*params, triggered_by)
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT * FROM runs WHERE 1 = 1{where} ORDER BY started_at DESC LIMIT ?", (*params, limit)
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_stats(app_id: Optional[str] = None, app_ids: Optional[set] = None) -> dict:
    where, params = _scope(app_id, app_ids)
    conn = _get_conn()
    total = conn.execute(f"SELECT COUNT(*) c FROM runs WHERE 1 = 1{where}", params).fetchone()["c"]
    success = conn.execute(
        f"SELECT COUNT(*) c FROM runs WHERE status = 'success'{where}", params
    ).fetchone()["c"]
    failed = conn.execute(
        f"SELECT COUNT(*) c FROM runs WHERE status IN ('failed', 'error'){where}", params
    ).fetchone()["c"]
    human = conn.execute(
        f"SELECT COUNT(*) c FROM runs WHERE status = 'human'{where}", params
    ).fetchone()["c"]

    success_rate = round((success / total) * 100) if total else 0

    avg_row = conn.execute(
        f"SELECT AVG(duration_sec) a FROM runs WHERE duration_sec IS NOT NULL{where}", params
    ).fetchone()
    avg_duration_sec = int(avg_row["a"]) if avg_row["a"] is not None else None

    daily = []
    today = datetime.now(timezone.utc).date()
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        row = conn.execute(
            f"""
            SELECT
              SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) s,
              SUM(CASE WHEN status IN ('failed', 'error') THEN 1 ELSE 0 END) f
            FROM runs WHERE substr(started_at, 1, 10) = ?{where}
            """,
            (day.isoformat(), *params),
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
