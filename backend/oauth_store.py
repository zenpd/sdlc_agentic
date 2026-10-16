"""Session store for GitHub OAuth logins. Each browser session that connects
via /api/auth/github/* gets a random session id (set as an httpOnly cookie);
the actual GitHub access token stays server-side, keyed by that session id,
and is never sent to the frontend.

Sessions are listed back to their owner (Account page) under a *public id* —
a hash of the session id — so the real cookie value never leaves the server.
"""

import hashlib
import secrets
import sqlite3
import threading
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "runs.db"
_lock = threading.Lock()

# A session stops working server-side after this long, matching the cookie's
# max-age, so an old cookie copied off a laptop can't be replayed forever.
SESSION_MAX_AGE_SEC = 60 * 60 * 24 * 30

# last_seen_at is refreshed at most this often, so polling pages don't turn
# every API call into a database write.
_TOUCH_INTERVAL_SEC = 60


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def public_id(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()[:16]


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
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(github_sessions)").fetchall()}
    if "user_agent" not in columns:
        conn.execute("ALTER TABLE github_sessions ADD COLUMN user_agent TEXT")
    if "last_seen_at" not in columns:
        conn.execute("ALTER TABLE github_sessions ADD COLUMN last_seen_at TEXT")
    conn.commit()
    conn.close()


def create_session(access_token: str, github_login: str, avatar_url: Optional[str], user_agent: Optional[str] = None) -> str:
    session_id = secrets.token_urlsafe(32)
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO github_sessions (session_id, access_token, github_login, avatar_url, user_agent, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now'))",
            (session_id, access_token, github_login, avatar_url, (user_agent or "")[:300]),
        )
        conn.commit()
        conn.close()
    return session_id


def get_session(session_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM github_sessions WHERE session_id = ? "
        "AND created_at >= datetime('now', ?)",
        (session_id, f"-{SESSION_MAX_AGE_SEC} seconds"),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def touch(session_id: str) -> None:
    """Record that this session was just used (throttled)."""
    with _lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE github_sessions SET last_seen_at = datetime('now') "
            "WHERE session_id = ? AND (last_seen_at IS NULL OR last_seen_at < datetime('now', ?))",
            (session_id, f"-{_TOUCH_INTERVAL_SEC} seconds"),
        )
        conn.commit()
        conn.close()


def list_sessions(github_login: str, current_session_id: Optional[str] = None) -> list[dict]:
    """A user's live sessions, newest first — never the session id or token."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT session_id, user_agent, created_at, last_seen_at FROM github_sessions "
        "WHERE github_login = ? AND created_at >= datetime('now', ?) ORDER BY COALESCE(last_seen_at, created_at) DESC",
        (github_login, f"-{SESSION_MAX_AGE_SEC} seconds"),
    ).fetchall()
    conn.close()
    return [
        {
            "id": public_id(r["session_id"]),
            "user_agent": r["user_agent"] or "",
            "created_at": r["created_at"],
            "last_seen_at": r["last_seen_at"] or r["created_at"],
            "current": r["session_id"] == current_session_id,
        }
        for r in rows
    ]


def revoke_session(github_login: str, session_public_id: str) -> bool:
    """Sign one of the user's own sessions out. Returns False if no such session."""
    with _lock:
        conn = _get_conn()
        try:
            for (sid,) in conn.execute("SELECT session_id FROM github_sessions WHERE github_login = ?", (github_login,)):
                if public_id(sid) == session_public_id:
                    conn.execute("DELETE FROM github_sessions WHERE session_id = ?", (sid,))
                    conn.commit()
                    return True
            return False
        finally:
            conn.close()


def revoke_all(github_login: str, except_session_id: Optional[str] = None) -> int:
    """Sign the user out everywhere (optionally keeping the current session)."""
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "DELETE FROM github_sessions WHERE github_login = ? AND session_id != ?",
            (github_login, except_session_id or ""),
        )
        conn.commit()
        conn.close()
        return cur.rowcount


def delete_session(session_id: str) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM github_sessions WHERE session_id = ?", (session_id,))
        conn.commit()
        conn.close()
