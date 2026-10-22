import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


DB_PATH = Path(__file__).parent / "runs.db"
_lock = threading.Lock()

STEP_KEYS = ["fetch", "assign", "in_progress", "gate", "agent", "verify", "pr", "jira"]


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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS webhook_deliveries (
            app_id TEXT NOT NULL,
            delivery_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            received_at TEXT NOT NULL,
            PRIMARY KEY (app_id, delivery_id)
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
    for column in ("approval_status", "approval_by", "approval_note", "approval_at", "approval_requested_at"):
        if column not in columns:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT")
    if "prompt_tokens" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN prompt_tokens INTEGER NOT NULL DEFAULT 0")
    if "completion_tokens" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN completion_tokens INTEGER NOT NULL DEFAULT 0")
    if "cost_usd" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN cost_usd REAL")
    if "llm_request_count" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN llm_request_count INTEGER NOT NULL DEFAULT 0")
    if "priced_request_count" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN priced_request_count INTEGER NOT NULL DEFAULT 0")
    if "cost_status" not in columns:
        conn.execute(
            "ALTER TABLE runs ADD COLUMN cost_status TEXT NOT NULL DEFAULT 'unavailable'"
        )
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


def create_webhook_run(
    run_id: str,
    ticket_key: str,
    started_at: str,
    app_id: str,
    delivery_id: str | None,
) -> str:
    """Atomically queue a webhook run and claim its delivery ID.

    Returns ``queued``, ``duplicate_delivery``, or ``already_running``.
    """
    with _lock:
        conn = _get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if delivery_id is not None:
                existing = conn.execute(
                    """
                    SELECT 1 FROM webhook_deliveries
                    WHERE app_id = ? AND delivery_id = ?
                    """,
                    (app_id, delivery_id),
                ).fetchone()
                if existing:
                    conn.rollback()
                    return "duplicate_delivery"

            active = conn.execute(
                """
                SELECT id FROM runs
                WHERE app_id = ? AND ticket_key = ? AND status IN ('in-progress', 'awaiting-approval')
                LIMIT 1
                """,
                (app_id, ticket_key),
            ).fetchone()
            if active:
                if delivery_id is not None:
                    conn.execute(
                        """
                        INSERT INTO webhook_deliveries
                            (app_id, delivery_id, run_id, received_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (app_id, delivery_id, active["id"], started_at),
                    )
                conn.commit()
                return "already_running"

            steps = [{"key": key, "label": key, "state": "pending"} for key in STEP_KEYS]
            conn.execute(
                """
                INSERT INTO runs
                    (id, ticket_key, status, pr_url, started_at, duration_sec, steps, logs, app_id)
                VALUES (?, ?, 'in-progress', NULL, ?, NULL, ?, ?, ?)
                """,
                (run_id, ticket_key, started_at, json.dumps(steps), json.dumps({}), app_id),
            )
            if delivery_id is not None:
                conn.execute(
                    """
                    INSERT INTO webhook_deliveries (app_id, delivery_id, run_id, received_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (app_id, delivery_id, run_id, started_at),
                )
            conn.commit()
            return "queued"
        except Exception:
            conn.rollback()
            raise
        finally:
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
            "approval_by = NULL, approval_note = NULL, approval_at = NULL, approval_requested_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), run_id),
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
            """
            UPDATE runs
            SET status = ?, pr_url = ?, duration_sec = ?,
                cost_status = CASE WHEN cost_status = 'pending'
                                   THEN 'unavailable' ELSE cost_status END
            WHERE id = ?
            """,
            (status, pr_url, duration_sec, run_id),
        )
        conn.commit()
        conn.close()


def record_usage(
    run_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float | None,
    request_count: int,
    priced_request_count: int,
) -> None:
    """Add one pipeline phase's LiteLLM metrics to its run totals."""
    if min(prompt_tokens, completion_tokens, request_count, priced_request_count) < 0:
        raise ValueError("Usage counters cannot be negative")
    if priced_request_count > request_count:
        raise ValueError("Priced request count cannot exceed request count")
    if cost_usd is not None and cost_usd < 0:
        raise ValueError("Cost cannot be negative")

    with _lock:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT llm_request_count, priced_request_count, cost_usd "
                "FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Run {run_id!r} does not exist")

            requests_total = row["llm_request_count"] + request_count
            priced_total = row["priced_request_count"] + priced_request_count
            if requests_total == 0:
                cost_status = "pending"
            elif priced_total == 0:
                cost_status = "unavailable"
            elif priced_total < requests_total:
                cost_status = "partial"
            else:
                cost_status = "estimated"

            total_cost = row["cost_usd"]
            if cost_usd is not None:
                total_cost = (total_cost or 0.0) + cost_usd

            conn.execute(
                """
                UPDATE runs
                SET prompt_tokens = prompt_tokens + ?,
                    completion_tokens = completion_tokens + ?,
                    cost_usd = ?,
                    llm_request_count = ?,
                    priced_request_count = ?,
                    cost_status = ?
                WHERE id = ?
                """,
                (
                    prompt_tokens,
                    completion_tokens,
                    total_cost,
                    requests_total,
                    priced_total,
                    cost_status,
                    run_id,
                ),
            )
            conn.commit()
        finally:
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
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "total_tokens": row["prompt_tokens"] + row["completion_tokens"],
        "cost_usd": row["cost_usd"],
        "cost_status": row["cost_status"],
        "llm_request_count": row["llm_request_count"],
        "priced_request_count": row["priced_request_count"],
        "steps": json.loads(row["steps"]),
        "logs": json.loads(row["logs"]),
        "approval": None if not row["approval_status"] else {
            "status": row["approval_status"],
            "by": row["approval_by"],
            "note": row["approval_note"],
            "at": row["approval_at"],
            "requested_at": row["approval_requested_at"],
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

    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    monthly_usage = conn.execute(
        f"""
        SELECT
          SUM(cost_usd) cost,
          SUM(prompt_tokens) prompt_tokens,
          SUM(completion_tokens) completion_tokens,
          SUM(CASE WHEN llm_request_count > 0 THEN 1 ELSE 0 END) usage_runs,
          SUM(CASE WHEN cost_status IN ('partial', 'unavailable')
                   AND llm_request_count > 0 THEN 1 ELSE 0 END) incomplete_runs
        FROM runs
        WHERE substr(started_at, 1, 7) = ?{where}
        """,
        (current_month, *params),
    ).fetchone()

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
        "monthly_cost_usd": monthly_usage["cost"],
        "monthly_prompt_tokens": monthly_usage["prompt_tokens"] or 0,
        "monthly_completion_tokens": monthly_usage["completion_tokens"] or 0,
        "monthly_usage_runs": monthly_usage["usage_runs"] or 0,
        "monthly_incomplete_cost_runs": monthly_usage["incomplete_runs"] or 0,
        "daily": daily,
    }
