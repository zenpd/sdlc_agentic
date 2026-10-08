"""Onboarded applications — one row per team/project the pipeline works for.

Each application carries its own ticket-provider, repository, model and
per-app defaults (persona, branch prefix, gate strictness, status names).
Any field an application leaves blank is *inherited* from the server-wide
.env (config_store), so the "default" application created on first start —
which has no overrides at all — behaves exactly like the single-config setup
this replaced.

Secret values (tokens/keys) are stored server-side only and are never
returned by any function here except resolve_settings(), which exists purely
to build the pipeline's config for a run. They currently live in this SQLite
file alongside the settings; _load_secrets/_save_secrets are the only two
functions that touch them, so a real secrets backend can replace those later.
"""

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend import config_store

DB_PATH = Path(__file__).parent / "runs.db"
_lock = threading.Lock()

DEFAULT_APP_ID = "default"

# Per-app defaults layered on top of the integration fields config_store
# already manages. Blank = use the pipeline's built-in behaviour.
DEFAULT_FIELDS = [
    "DEFAULT_PERSONA",   # persona used when the ticket doesn't name one (else keyword match)
    "BRANCH_PREFIX",     # e.g. "feat/" -> feat/<ticket>-<summary>-<id>
    "GATE_MIN_WORDS",    # clarity gate: minimum description length before the LLM check
]

SETTING_FIELDS = config_store.NON_SECRET_FIELDS + DEFAULT_FIELDS
SECRET_FIELDS = config_store.SECRET_FIELDS


class AppNotFound(Exception):
    pass


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
        CREATE TABLE IF NOT EXISTS applications (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            team TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            settings TEXT NOT NULL DEFAULT '{}',
            secrets TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    # The pre-existing single configuration becomes the "default" application:
    # no overrides, so every field resolves to today's .env value.
    conn.execute(
        "INSERT OR IGNORE INTO applications (id, name, team, description, created_at, updated_at) "
        "VALUES (?, ?, '', ?, ?, ?)",
        (DEFAULT_APP_ID, "Default application", "Uses the server-wide settings from .env", _now(), _now()),
    )
    conn.commit()
    conn.close()


# ── secrets (single swap point for a future vault) ──────────────────
def _load_secrets(row: sqlite3.Row) -> dict[str, str]:
    return json.loads(row["secrets"] or "{}")


def _save_secrets(conn: sqlite3.Connection, app_id: str, secrets: dict[str, str]) -> None:
    conn.execute("UPDATE applications SET secrets = ? WHERE id = ?", (json.dumps(secrets), app_id))


# ── helpers ─────────────────────────────────────────────────────────
def _row(conn: sqlite3.Connection, app_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    if not row:
        raise AppNotFound(app_id)
    return row


def _global_value(key: str) -> str:
    """Server-wide value from .env (already loaded into os.environ by
    config_store / jira_bot), with hand-edited <placeholders> treated as unset."""
    val = os.getenv(key, "")
    return "" if (val.startswith("<") and val.endswith(">")) else val


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
    return slug or "app"


def _validate_settings(settings: dict[str, str]) -> None:
    provider = settings.get("TICKET_PROVIDER")
    if provider and provider not in ("jira", "ado"):
        raise ValueError("TICKET_PROVIDER must be 'jira' or 'ado'")
    gate = settings.get("GATE_MIN_WORDS")
    if gate:
        if not gate.isdigit() or not 0 <= int(gate) <= 200:
            raise ValueError("GATE_MIN_WORDS must be a whole number between 0 and 200")
    repo = settings.get("TARGET_REPO")
    if repo and not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError("TARGET_REPO must look like owner/repo")


def _public(row: sqlite3.Row) -> dict:
    settings = json.loads(row["settings"] or "{}")
    effective = {k: settings.get(k) or _global_value(k) for k in ("TICKET_PROVIDER", "TARGET_REPO", "TARGET_BRANCH", "DEFAULT_PERSONA")}
    return {
        "id": row["id"],
        "name": row["name"],
        "team": row["team"],
        "description": row["description"],
        "is_default": row["id"] == DEFAULT_APP_ID,
        "ticket_provider": effective["TICKET_PROVIDER"] or "jira",
        "target_repo": effective["TARGET_REPO"],
        "target_branch": effective["TARGET_BRANCH"],
        "default_persona": effective["DEFAULT_PERSONA"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ── applications ────────────────────────────────────────────────────
def list_apps() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM applications ORDER BY (id = ?) DESC, created_at ASC", (DEFAULT_APP_ID,)
    ).fetchall()
    conn.close()
    return [_public(r) for r in rows]


def get_app(app_id: str) -> dict:
    conn = _get_conn()
    try:
        return _public(_row(conn, app_id))
    finally:
        conn.close()


def app_exists(app_id: str) -> bool:
    conn = _get_conn()
    row = conn.execute("SELECT 1 FROM applications WHERE id = ?", (app_id,)).fetchone()
    conn.close()
    return row is not None


def create_app(
    name: str,
    team: str = "",
    description: str = "",
    config: Optional[dict[str, Optional[str]]] = None,
) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Application name is required")
    base = _slugify(name)
    with _lock:
        conn = _get_conn()
        try:
            app_id, n = base, 2
            while conn.execute("SELECT 1 FROM applications WHERE id = ?", (app_id,)).fetchone():
                app_id, n = f"{base}-{n}", n + 1
            conn.execute(
                "INSERT INTO applications (id, name, team, description, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (app_id, name, team.strip(), description.strip(), _now(), _now()),
            )
            conn.commit()
        finally:
            conn.close()
    if config:
        try:
            update_app_config(app_id, config)
        except ValueError:
            delete_app(app_id)
            raise
    return get_app(app_id)


def update_app(app_id: str, name: Optional[str] = None, team: Optional[str] = None, description: Optional[str] = None) -> dict:
    with _lock:
        conn = _get_conn()
        try:
            row = _row(conn, app_id)
            new_name = name.strip() if name is not None else row["name"]
            if not new_name:
                raise ValueError("Application name is required")
            conn.execute(
                "UPDATE applications SET name = ?, team = ?, description = ?, updated_at = ? WHERE id = ?",
                (
                    new_name,
                    team.strip() if team is not None else row["team"],
                    description.strip() if description is not None else row["description"],
                    _now(),
                    app_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    return get_app(app_id)


def delete_app(app_id: str) -> None:
    if app_id == DEFAULT_APP_ID:
        raise ValueError("The default application can't be deleted")
    with _lock:
        conn = _get_conn()
        try:
            _row(conn, app_id)
            conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))
            conn.commit()
        finally:
            conn.close()


# ── per-app config ──────────────────────────────────────────────────
def get_app_config(app_id: str) -> dict:
    """What the Config page needs for one application — never a raw secret.

    values:    this app's own overrides ("" = not overridden)
    inherited: the server-wide value a blank field falls back to
    effective: what a run will actually use
    secrets:   {set, inherited} per secret field
    """
    conn = _get_conn()
    try:
        row = _row(conn, app_id)
    finally:
        conn.close()
    settings = json.loads(row["settings"] or "{}")
    secrets = _load_secrets(row)

    values = {k: settings.get(k, "") for k in SETTING_FIELDS}
    inherited = {k: _global_value(k) for k in SETTING_FIELDS}
    effective = {k: values[k] or inherited[k] for k in SETTING_FIELDS}
    secret_state = {}
    for k in SECRET_FIELDS:
        own = bool(secrets.get(k))
        secret_state[k] = {"set": own or bool(_global_value(k)), "inherited": not own and bool(_global_value(k))}
    return {
        "app": _public(row),
        "values": values,
        "inherited": inherited,
        "effective": effective,
        "secrets": secret_state,
    }


def update_app_config(app_id: str, updates: dict[str, Optional[str]]) -> None:
    """Same contract as config_store.update_config, but scoped to one app:
    a blank secret is left untouched, a blank setting removes the override
    (so it inherits the server-wide value again)."""
    unknown = set(updates) - set(SETTING_FIELDS) - set(SECRET_FIELDS)
    if unknown:
        raise ValueError(f"Unrecognized config field(s): {', '.join(sorted(unknown))}")

    with _lock:
        conn = _get_conn()
        try:
            row = _row(conn, app_id)
            settings = json.loads(row["settings"] or "{}")
            secrets = _load_secrets(row)
            for key, value in updates.items():
                value = (value or "").strip()
                if key in SECRET_FIELDS:
                    if value:
                        secrets[key] = value
                elif value:
                    settings[key] = value
                else:
                    settings.pop(key, None)
            if "TICKET_PROVIDER" in settings:
                settings["TICKET_PROVIDER"] = settings["TICKET_PROVIDER"].lower()
            _validate_settings(settings)
            conn.execute(
                "UPDATE applications SET settings = ?, updated_at = ? WHERE id = ?",
                (json.dumps(settings), _now(), app_id),
            )
            _save_secrets(conn, app_id, secrets)
            conn.commit()
        finally:
            conn.close()


def resolve_settings(app_id: str) -> dict[str, str]:
    """Every field (secrets included) as a run of this app should see it —
    server-side only, used to build jira_bot.PipelineConfig."""
    conn = _get_conn()
    try:
        row = _row(conn, app_id)
    finally:
        conn.close()
    settings = json.loads(row["settings"] or "{}")
    secrets = _load_secrets(row)
    out = {k: settings.get(k) or _global_value(k) for k in SETTING_FIELDS}
    out.update({k: secrets.get(k) or _global_value(k) for k in SECRET_FIELDS})
    return out
