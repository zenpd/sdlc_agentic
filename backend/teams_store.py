"""Teams: who works on which applications (new_req.md point 3).

An application belongs to at most one team (applications.team_id). Members
of that team — and admins — can see and use it; everyone else doesn't even
see it in their list. An application with no team is shared with everyone
(the "default" application, and anything onboarded as shared).

A user can be in several teams; the active-team switcher in the UI only
narrows the list they see, it never grants access.
"""

import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "runs.db"
_lock = threading.Lock()


class TeamNotFound(Exception):
    pass


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-") or "team"


def init_db() -> None:
    conn = _get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS teams (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS team_members (
            team_id TEXT NOT NULL,
            login TEXT NOT NULL,
            added_at TEXT NOT NULL,
            PRIMARY KEY (team_id, login)
        )
        """
    )
    conn.commit()
    conn.close()


def get_or_create(name: str, conn: Optional[sqlite3.Connection] = None) -> str:
    """Team id for this name, creating the team if needed (used for migration)."""
    own = conn is None
    conn = conn or _get_conn()
    try:
        row = conn.execute("SELECT id FROM teams WHERE name = ?", (name.strip(),)).fetchone()
        if row:
            return row["id"]
        base = _slugify(name)
        team_id, n = base, 2
        while conn.execute("SELECT 1 FROM teams WHERE id = ?", (team_id,)).fetchone():
            team_id, n = f"{base}-{n}", n + 1
        conn.execute("INSERT INTO teams (id, name, created_at) VALUES (?, ?, ?)", (team_id, name.strip(), _now()))
        if own:
            conn.commit()
        return team_id
    finally:
        if own:
            conn.close()


def list_teams(only_for: Optional[str] = None) -> list[dict]:
    """Every team (or just the ones `only_for` belongs to), with members and app counts."""
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM teams ORDER BY name COLLATE NOCASE").fetchall()
    members: dict[str, list[str]] = {}
    for m in conn.execute("SELECT team_id, login FROM team_members ORDER BY login"):
        members.setdefault(m["team_id"], []).append(m["login"])
    app_counts = {
        r["team_id"]: r["c"]
        for r in conn.execute("SELECT team_id, COUNT(*) c FROM applications WHERE team_id IS NOT NULL GROUP BY team_id")
    }
    conn.close()
    teams = [
        {"id": r["id"], "name": r["name"], "created_at": r["created_at"],
         "members": members.get(r["id"], []), "app_count": app_counts.get(r["id"], 0)}
        for r in rows
    ]
    return [t for t in teams if only_for in t["members"]] if only_for else teams


def get_team(team_id: str) -> dict:
    for t in list_teams():
        if t["id"] == team_id:
            return t
    raise TeamNotFound(team_id)


def team_exists(team_id: str) -> bool:
    conn = _get_conn()
    row = conn.execute("SELECT 1 FROM teams WHERE id = ?", (team_id,)).fetchone()
    conn.close()
    return row is not None


def teams_of(login: str) -> set[str]:
    conn = _get_conn()
    rows = conn.execute("SELECT team_id FROM team_members WHERE login = ?", (login,)).fetchall()
    conn.close()
    return {r["team_id"] for r in rows}


def create_team(name: str) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Team name is required")
    if len(name) > 60:
        raise ValueError("Team name is too long (max 60 characters)")
    with _lock:
        conn = _get_conn()
        try:
            if conn.execute("SELECT 1 FROM teams WHERE name = ?", (name,)).fetchone():
                raise ValueError(f"A team named {name!r} already exists")
            team_id = get_or_create(name, conn)
            conn.commit()
        finally:
            conn.close()
    return get_team(team_id)


def delete_team(team_id: str) -> None:
    with _lock:
        conn = _get_conn()
        try:
            if not conn.execute("SELECT 1 FROM teams WHERE id = ?", (team_id,)).fetchone():
                raise TeamNotFound(team_id)
            apps = conn.execute("SELECT COUNT(*) c FROM applications WHERE team_id = ?", (team_id,)).fetchone()["c"]
            if apps:
                raise ValueError(f"This team still owns {apps} application(s) — move them to another team (or make them shared) first")
            conn.execute("DELETE FROM team_members WHERE team_id = ?", (team_id,))
            conn.execute("DELETE FROM teams WHERE id = ?", (team_id,))
            conn.commit()
        finally:
            conn.close()


def add_member(team_id: str, login: str) -> None:
    with _lock:
        conn = _get_conn()
        try:
            if not conn.execute("SELECT 1 FROM teams WHERE id = ?", (team_id,)).fetchone():
                raise TeamNotFound(team_id)
            if not conn.execute("SELECT 1 FROM users WHERE login = ?", (login,)).fetchone():
                raise ValueError(f"No user named {login!r} — they need to sign in once first")
            conn.execute(
                "INSERT OR IGNORE INTO team_members (team_id, login, added_at) VALUES (?, ?, ?)",
                (team_id, login, _now()),
            )
            conn.commit()
        finally:
            conn.close()


def remove_member(team_id: str, login: str) -> bool:
    with _lock:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM team_members WHERE team_id = ? AND login = ?", (team_id, login))
        conn.commit()
        conn.close()
        return cur.rowcount == 1
