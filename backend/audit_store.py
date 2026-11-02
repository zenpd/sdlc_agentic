"""Append-only record of who changed what: onboarding, config edits, skill
edits, repository picks, role changes. Answers questions like "who changed
TARGET_BRANCH to main last Tuesday?".

Secret values are never written here — a changed token is recorded as
"updated", never its value.
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "runs.db"
_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            app_id TEXT,
            target TEXT,
            details TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log (at)")
    conn.commit()
    conn.close()


def record(actor: str, action: str, target: str = "", app_id: Optional[str] = None, details: Optional[dict] = None) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO audit_log (at, actor, action, app_id, target, details) VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), actor, action, app_id, target, json.dumps(details or {})),
        )
        conn.commit()
        conn.close()


def list_entries(limit: int = 200, app_id: Optional[str] = None, actor: Optional[str] = None) -> list[dict]:
    where, params = [], []
    if app_id:
        where.append("app_id = ?")
        params.append(app_id)
    if actor:
        where.append("actor = ?")
        params.append(actor)
    sql = "SELECT * FROM audit_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    conn = _get_conn()
    rows = conn.execute(sql, (*params, limit)).fetchall()
    conn.close()
    return [{**dict(r), "details": json.loads(r["details"] or "{}")} for r in rows]


def config_changes(before: dict, after: dict, secret_fields: list[str]) -> dict:
    """{field: {"from": old, "to": new}} for every setting that changed, and
    {field: "updated"} for a changed secret — never a secret's value."""
    changes: dict = {}
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        changes[key] = "updated" if key in secret_fields else {"from": old or "", "to": new or ""}
    return changes
