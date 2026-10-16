"""Shared test setup: every test runs against a throwaway SQLite file (never
backend/runs.db) with a known server-wide (.env) baseline."""

import tempfile
from pathlib import Path

import pytest

from backend import apps_store, audit_store, oauth_store, runs_db, teams_store, users_store

_TMP_DB = Path(tempfile.mkdtemp(prefix="sdlc_test_")) / "runs.db"
for _mod in (runs_db, oauth_store, apps_store, users_store, audit_store, teams_store):
    _mod.DB_PATH = _TMP_DB

from fastapi.testclient import TestClient  # noqa: E402

from backend import api_server  # noqa: E402  (import after DB_PATH is redirected)

client = TestClient(api_server.app)

GLOBAL_ENV = {
    "TICKET_PROVIDER": "ado",
    "ADO_ORG_URL": "https://dev.azure.com/global-org",
    "ADO_PROJECT": "Global-Project",
    "ADO_PAT": "global-pat",
    "TARGET_REPO": "global/repo",
    "TARGET_BRANCH": "dev",
    "GITHUB_TOKEN": "global-gh-token",
    "AZURE_OPENAI_ENDPOINT": "https://global.openai.azure.com/",
    "AZURE_OPENAI_DEPLOYMENT": "gpt-4.1-mini",
    "AZURE_OPENAI_API_KEY": "global-azure-key",
}


def sign_in(login: str, role: str | None = None) -> None:
    """Give the shared test client a real server-side session for `login`."""
    users_store.ensure_user(login)
    if role:
        users_store.set_role(login, role)
    client.cookies.set("sdlc_session", oauth_store.create_session("fake-gh-token", login, None))


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    conn = apps_store._get_conn()
    for table in ("applications", "runs", "users", "audit_log", "github_sessions", "teams", "team_members"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()
    conn.close()
    for store in (runs_db, teams_store, apps_store, users_store, audit_store, oauth_store):
        store.init_db()
    client.cookies.clear()
    monkeypatch.delenv("ADMIN_LOGINS", raising=False)
    monkeypatch.delenv("DEFAULT_USER_ROLE", raising=False)
    for key in apps_store.SETTING_FIELDS + apps_store.SECRET_FIELDS:
        monkeypatch.delenv(key, raising=False)
    for key, value in GLOBAL_ENV.items():
        monkeypatch.setenv(key, value)
