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

from backend import config_store, teams_store

DB_PATH = Path(__file__).parent / "runs.db"
_lock = threading.Lock()

DEFAULT_APP_ID = "default"

# Per-app defaults layered on top of the integration fields config_store
# already manages. Blank = use the pipeline's built-in behaviour.
DEFAULT_FIELDS = [
    "DEFAULT_PERSONA",   # persona used when the ticket doesn't name one (else keyword match)
    "BRANCH_PREFIX",     # e.g. "feat/" -> feat/<ticket>-<summary>-<id>
    "GATE_MIN_WORDS",    # clarity gate: minimum description length before the LLM check
    "AGENT_TIMEOUT_MIN", # coding agent time limit; it's stopped (nothing pushed) when exceeded
    # Governance (new_req.md point 10)
    "REQUIRE_PR_APPROVAL",  # "true" -> a maintainer must approve before any PR is opened
    "PROTECTED_PATHS",      # glob patterns the agent may never change, e.g. ".github/workflows/*, *.tf"
    # Verification (new_req.md point 11)
    "VERIFY_MODE",          # "" (review the diff only) | "command" | "github_actions"
    "TEST_SETUP_COMMAND",   # command mode: install deps before the agent starts, e.g. "pip install -r requirements.txt"
    "TEST_COMMAND",         # command mode: e.g. "pytest -q" or "npm test"
    "TEST_TIMEOUT_MIN",     # minutes to allow the tests / CI to finish
    "CI_WORKFLOW",          # github_actions mode: optional workflow file to dispatch, e.g. "ci.yml"
]

VERIFY_MODES = ("", "command", "github_actions")

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
    # Point 3: an application belongs to one team (or none = shared with
    # everyone). The old free-text "team" column becomes a real team.
    teams_store.init_db()
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(applications)").fetchall()}
    if "team_id" not in columns:
        conn.execute("ALTER TABLE applications ADD COLUMN team_id TEXT")
        for row in conn.execute("SELECT id, team FROM applications WHERE team != ''").fetchall():
            conn.execute("UPDATE applications SET team_id = ? WHERE id = ?", (teams_store.get_or_create(row["team"], conn), row["id"]))
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
    approval = settings.get("REQUIRE_PR_APPROVAL")
    if approval and approval not in ("true", "false"):
        raise ValueError("REQUIRE_PR_APPROVAL must be 'true' or 'false'")
    if len(settings.get("PROTECTED_PATHS", "")) > 2000:
        raise ValueError("PROTECTED_PATHS is too long (max 2000 characters)")
    mode = settings.get("VERIFY_MODE", "")
    if mode not in VERIFY_MODES:
        raise ValueError("VERIFY_MODE must be blank, 'command' or 'github_actions'")
    if mode == "command" and not (settings.get("TEST_COMMAND") or _global_value("TEST_COMMAND")):
        raise ValueError("Set a TEST_COMMAND (e.g. 'pytest -q') to verify with a command")
    agent_timeout = settings.get("AGENT_TIMEOUT_MIN")
    if agent_timeout and (not agent_timeout.isdigit() or not 1 <= int(agent_timeout) <= 30):
        raise ValueError("AGENT_TIMEOUT_MIN must be a whole number of minutes between 1 and 30")
    timeout = settings.get("TEST_TIMEOUT_MIN")
    if timeout and (not timeout.isdigit() or not 1 <= int(timeout) <= 120):
        raise ValueError("TEST_TIMEOUT_MIN must be a whole number of minutes between 1 and 120")
    for key in ("TEST_SETUP_COMMAND", "TEST_COMMAND", "CI_WORKFLOW"):
        if len(settings.get(key, "")) > 500:
            raise ValueError(f"{key} is too long (max 500 characters)")
    workflow = settings.get("CI_WORKFLOW")
    if workflow and not re.fullmatch(r"[\w.-]+\.ya?ml|\d+", workflow):
        raise ValueError("CI_WORKFLOW must be a workflow file name like 'ci.yml' (or its numeric id)")
    repo = settings.get("TARGET_REPO")
    if repo and not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError("TARGET_REPO must look like owner/repo")


def _team_names() -> dict[str, str]:
    return {t["id"]: t["name"] for t in teams_store.list_teams()}


def _public(row: sqlite3.Row, team_names: Optional[dict[str, str]] = None) -> dict:
    settings = json.loads(row["settings"] or "{}")
    keys = ("TICKET_PROVIDER", "TARGET_REPO", "TARGET_BRANCH", "DEFAULT_PERSONA", "REQUIRE_PR_APPROVAL",
            "PROTECTED_PATHS", "VERIFY_MODE", "TEST_COMMAND", "CI_WORKFLOW")
    effective = {k: settings.get(k) or _global_value(k) for k in keys}
    team_names = team_names if team_names is not None else _team_names()
    return {
        "id": row["id"],
        "name": row["name"],
        "team_id": row["team_id"],
        "team": team_names.get(row["team_id"], "") if row["team_id"] else "",
        "description": row["description"],
        "is_default": row["id"] == DEFAULT_APP_ID,
        "ticket_provider": effective["TICKET_PROVIDER"] or "jira",
        "target_repo": effective["TARGET_REPO"],
        "target_branch": effective["TARGET_BRANCH"],
        "default_persona": effective["DEFAULT_PERSONA"],
        "require_pr_approval": effective["REQUIRE_PR_APPROVAL"] == "true",
        "protected_paths": effective["PROTECTED_PATHS"],
        "verify_mode": effective["VERIFY_MODE"],
        "test_command": effective["TEST_COMMAND"],
        "ci_workflow": effective["CI_WORKFLOW"],
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
    names = _team_names()
    return [_public(r, names) for r in rows]


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
    team_id: Optional[str] = None,
) -> dict:
    """team_id assigns an existing team; `team` (a name) finds or creates one.
    Neither -> shared with everyone."""
    name = name.strip()
    if not name:
        raise ValueError("Application name is required")
    if team_id and not teams_store.team_exists(team_id):
        raise ValueError(f"No team named {team_id!r}")
    if not team_id and team.strip():
        team_id = teams_store.get_or_create(team)
    base = _slugify(name)
    with _lock:
        conn = _get_conn()
        try:
            app_id, n = base, 2
            while conn.execute("SELECT 1 FROM applications WHERE id = ?", (app_id,)).fetchone():
                app_id, n = f"{base}-{n}", n + 1
            conn.execute(
                "INSERT INTO applications (id, name, team, description, team_id, created_at, updated_at) "
                "VALUES (?, ?, '', ?, ?, ?, ?)",
                (app_id, name, description.strip(), team_id or None, _now(), _now()),
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


_UNCHANGED = object()


def update_app(app_id: str, name: Optional[str] = None, description: Optional[str] = None, team_id=_UNCHANGED) -> dict:
    """team_id: a team's id, "" / None to make the app shared, or leave out to keep it."""
    if team_id is not _UNCHANGED and team_id and not teams_store.team_exists(team_id):
        raise ValueError(f"No team named {team_id!r}")
    with _lock:
        conn = _get_conn()
        try:
            row = _row(conn, app_id)
            new_name = name.strip() if name is not None else row["name"]
            if not new_name:
                raise ValueError("Application name is required")
            conn.execute(
                "UPDATE applications SET name = ?, description = ?, team_id = ?, updated_at = ? WHERE id = ?",
                (
                    new_name,
                    description.strip() if description is not None else row["description"],
                    row["team_id"] if team_id is _UNCHANGED else (team_id or None),
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
            for key in ("TICKET_PROVIDER", "REQUIRE_PR_APPROVAL", "VERIFY_MODE"):
                if key in settings:
                    settings[key] = settings[key].lower()
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
