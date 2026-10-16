"""People who sign in to the dashboard, and the role each one has.

A user is a *human* (identified by their login — GitHub today, Entra ID
later); it has nothing to do with the coding agent's persona
(skills_store), which is picked per ticket.

Roles, lowest to highest — each one can do everything the ones below it can:
  viewer      watch runs, logs, dashboards
  runner      + trigger pipeline runs
  maintainer  + edit application config, persona skills, pick repositories
  admin       + onboard/remove applications, edit server defaults, manage users

Who becomes what on first sign-in: anyone listed in ADMIN_LOGINS (.env,
comma-separated), or the very first person to ever sign in, is an admin;
everyone else starts as DEFAULT_USER_ROLE (viewer unless overridden) until an
admin raises them on the Users page.
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "runs.db"
_lock = threading.Lock()

ROLES = ["viewer", "runner", "maintainer", "admin"]

# Per-user settings kept server-side so they follow the person to a new
# device or browser (localStorage alone doesn't).
THEMES = ("light", "dark", "system")
PREFERENCE_KEYS = ("default_app_id", "theme", "active_team_id")


class LastAdminError(ValueError):
    pass


def role_rank(role: str) -> int:
    return ROLES.index(role) if role in ROLES else -1


def has_role(user: dict, minimum: str) -> bool:
    return role_rank(user.get("role", "")) >= role_rank(minimum)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    conn = _get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            login TEXT PRIMARY KEY,
            avatar_url TEXT,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_login_at TEXT NOT NULL
        )
        """
    )
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "preferences" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN preferences TEXT NOT NULL DEFAULT '{}'")
    conn.commit()
    conn.close()


def _with_prefs(row: sqlite3.Row) -> dict:
    user = dict(row)
    user["preferences"] = json.loads(user.get("preferences") or "{}")
    return user


def _admin_logins() -> set[str]:
    return {x.strip().lower() for x in os.getenv("ADMIN_LOGINS", "").split(",") if x.strip()}


def _default_role() -> str:
    role = os.getenv("DEFAULT_USER_ROLE", "viewer").strip().lower()
    return role if role in ROLES else "viewer"


def ensure_user(login: str, avatar_url: Optional[str] = None) -> dict:
    """Get-or-create the user record for someone who just signed in (or whose
    session predates this table), refreshing avatar and last-login time."""
    with _lock:
        conn = _get_conn()
        try:
            row = conn.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE users SET avatar_url = COALESCE(?, avatar_url), last_login_at = ? WHERE login = ?",
                    (avatar_url, _now(), login),
                )
            else:
                first_ever = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0
                role = "admin" if first_ever or login.lower() in _admin_logins() else _default_role()
                conn.execute(
                    "INSERT INTO users (login, avatar_url, role, created_at, last_login_at) VALUES (?, ?, ?, ?, ?)",
                    (login, avatar_url, role, _now(), _now()),
                )
            conn.commit()
            return _with_prefs(conn.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone())
        finally:
            conn.close()


def get_user(login: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()
    conn.close()
    if not row:
        return None
    user = _with_prefs(row)
    # ADMIN_LOGINS is authoritative, so locking yourself out is always recoverable from .env.
    if login.lower() in _admin_logins():
        user["role"] = "admin"
    return user


def list_users() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
    conn.close()
    return [_with_prefs(r) for r in rows]


def set_role(login: str, role: str) -> dict:
    if role not in ROLES:
        raise ValueError(f"Role must be one of: {', '.join(ROLES)}")
    with _lock:
        conn = _get_conn()
        try:
            row = conn.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()
            if not row:
                raise KeyError(login)
            if row["role"] == "admin" and role != "admin":
                admins = conn.execute("SELECT COUNT(*) c FROM users WHERE role = 'admin'").fetchone()["c"]
                if admins <= 1:
                    raise LastAdminError("Can't remove the last admin — make someone else an admin first")
            conn.execute("UPDATE users SET role = ? WHERE login = ?", (role, login))
            conn.commit()
            return _with_prefs(conn.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone())
        finally:
            conn.close()


def update_preferences(login: str, updates: dict) -> dict:
    """Merge validated preference values into the user's saved preferences.
    A value of "" or None removes that preference (back to the default)."""
    unknown = set(updates) - set(PREFERENCE_KEYS)
    if unknown:
        raise ValueError(f"Unknown preference(s): {', '.join(sorted(unknown))}")
    if updates.get("theme") and updates["theme"] not in THEMES:
        raise ValueError(f"Theme must be one of: {', '.join(THEMES)}")
    with _lock:
        conn = _get_conn()
        try:
            row = conn.execute("SELECT preferences FROM users WHERE login = ?", (login,)).fetchone()
            if not row:
                raise KeyError(login)
            prefs = json.loads(row["preferences"] or "{}")
            for key, value in updates.items():
                if value:
                    prefs[key] = value
                else:
                    prefs.pop(key, None)
            conn.execute("UPDATE users SET preferences = ? WHERE login = ?", (json.dumps(prefs), login))
            conn.commit()
            return prefs
        finally:
            conn.close()
