"""Session store for GitHub OAuth logins. Each browser session that connects
via /api/auth/github/* gets a random session id (set as an httpOnly cookie);
the actual GitHub access token stays server-side, keyed by that session id,
and is never sent to the frontend.
"""

import secrets
import sqlite3
import threading
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
        CREATE TABLE IF NOT EXISTS github_sessions (
            session_id TEXT PRIMARY KEY,
            access_token TEXT NOT NULL,
            github_login TEXT NOT NULL,
            avatar_url TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    conn.close()


def create_session(access_token: str, github_login: str, avatar_url: Optional[str]) -> str:
    session_id = secrets.token_urlsafe(32)
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO github_sessions (session_id, access_token, github_login, avatar_url) "
            "VALUES (?, ?, ?, ?)",
            (session_id, access_token, github_login, avatar_url),
        )
        conn.commit()
        conn.close()
    return session_id


def get_session(session_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM github_sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_session(session_id: str) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM github_sessions WHERE session_id = ?", (session_id,))
        conn.commit()
        conn.close()
