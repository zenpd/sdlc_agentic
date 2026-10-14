"""Sign-in enforcement, roles (viewer < runner < maintainer < admin),
who-triggered-a-run, and the audit log."""

import pytest

from conftest import client, sign_in
from backend import api_server, audit_store, oauth_store, runs_db, users_store


@pytest.fixture(autouse=True)
def no_real_pipeline(monkeypatch):
    """Runs are queued but the pipeline thread is a no-op — no ADO/LLM/git."""
    monkeypatch.setattr(api_server, "_execute", lambda *a, **k: None)


# ── sign-in ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("method,path", [
    ("get", "/api/runs"), ("get", "/api/stats"), ("get", "/api/apps"),
    ("get", "/api/apps/default/config"), ("get", "/api/skills"), ("get", "/api/config"),
    ("post", "/api/run-ticket"), ("put", "/api/apps/default/config"), ("get", "/api/audit"),
    ("get", "/api/users"), ("get", "/api/auth/github/me"),
])
def test_every_api_needs_a_signed_in_user(method, path):
    assert getattr(client, method)(path).status_code == 401


def test_expired_or_unknown_session_is_rejected():
    client.cookies.set("sdlc_session", "made-up-session-id")
    assert client.get("/api/runs").status_code == 401

    users_store.ensure_user("old-laptop")
    sid = oauth_store.create_session("tok", "old-laptop", None)
    conn = oauth_store._get_conn()
    conn.execute("UPDATE github_sessions SET created_at = datetime('now', '-31 days') WHERE session_id = ?", (sid,))
    conn.commit()
    conn.close()
    client.cookies.set("sdlc_session", sid)
    assert client.get("/api/runs").status_code == 401


# ── who gets which role ──────────────────────────────────────────────
def test_first_user_is_admin_and_later_users_are_viewers():
    assert users_store.ensure_user("nikhil")["role"] == "admin"
    assert users_store.ensure_user("priya")["role"] == "viewer"


def test_admin_logins_env_always_wins(monkeypatch):
    users_store.ensure_user("nikhil")
    monkeypatch.setenv("ADMIN_LOGINS", "Rahul, someone-else")
    assert users_store.ensure_user("rahul")["role"] == "admin"
    sign_in("rahul")
    assert client.get("/api/auth/github/me").json()["role"] == "admin"


def test_last_admin_cannot_be_demoted():
    sign_in("nikhil")
    r = client.patch("/api/users/nikhil", json={"role": "viewer"})
    assert r.status_code == 400 and "last admin" in r.json()["detail"]


# ── what each role can do ────────────────────────────────────────────
def _status(method, path, **kw):
    return getattr(client, method)(path, **kw).status_code


ACTIONS = {
    # action: (method, path, kwargs, minimum role)
    "view runs": ("get", "/api/runs", {}, "viewer"),
    "trigger run": ("post", "/api/run-ticket", {"json": {"ticket_key": "701"}}, "runner"),
    "edit app config": ("put", "/api/apps/default/config", {"json": {"BRANCH_PREFIX": "x/"}}, "maintainer"),
    "edit skill": ("put", "/api/skills/qa-test-engineer", {"json": {"content": "not valid frontmatter"}}, "maintainer"),
    "read audit log": ("get", "/api/audit", {}, "maintainer"),
    "onboard app": ("post", "/api/apps", {"json": {"name": "Checkout"}}, "admin"),
    "edit server defaults": ("get", "/api/config", {}, "maintainer"),
    "manage users": ("get", "/api/users", {}, "admin"),
}


@pytest.mark.parametrize("role", users_store.ROLES)
def test_role_matrix(role):
    users_store.ensure_user("first-admin")
    sign_in("priya", role)
    for name, (method, path, kw, minimum) in ACTIONS.items():
        allowed = users_store.role_rank(role) >= users_store.role_rank(minimum)
        code = _status(method, path, **kw)
        if allowed:
            assert code != 403, f"{role} should be able to {name} (got {code})"
        else:
            assert code == 403, f"{role} must NOT be able to {name} (got {code})"


def test_viewer_does_not_learn_which_tokens_are_set():
    users_store.ensure_user("first-admin")
    sign_in("rahul", "viewer")
    assert client.get("/api/apps/default/config").json()["secrets"] is None
    sign_in("priya", "maintainer")
    assert client.get("/api/apps/default/config").json()["secrets"]["ADO_PAT"]["set"] is True


# ── user vs persona, audit ───────────────────────────────────────────
def test_run_records_the_human_who_triggered_it():
    users_store.ensure_user("first-admin")
    sign_in("priya", "runner")
    run_id = client.post("/api/run-ticket", json={"ticket_key": "701"}).json()["run_id"]
    assert runs_db.get_run(run_id)["triggered_by"] == "priya"


def test_audit_log_answers_who_changed_what():
    sign_in("nikhil")  # first user -> admin
    client.post("/api/apps", json={"name": "Checkout", "config": {"ADO_PAT": "secret-pat-value"}})
    client.put("/api/apps/checkout/config", json={"TARGET_BRANCH": "main", "GITHUB_TOKEN": "ghp_secret"})
    client.put("/api/apps/checkout/config", json={"TARGET_BRANCH": "main"})  # no-op: not logged
    client.patch("/api/users/nikhil", json={"role": "admin"})  # no-op: not logged
    users_store.ensure_user("priya")
    client.patch("/api/users/priya", json={"role": "runner"})

    entries = client.get("/api/audit").json()
    assert [e["action"] for e in entries] == ["user.role", "app.config", "app.onboard"]
    config_change = entries[1]
    assert config_change["actor"] == "nikhil" and config_change["app_id"] == "checkout"
    assert config_change["details"]["TARGET_BRANCH"] == {"from": "", "to": "main"}
    assert config_change["details"]["GITHUB_TOKEN"] == "updated"
    assert entries[0]["details"] == {"role": {"from": "viewer", "to": "runner"}}

    raw = repr(audit_store.list_entries())
    assert "secret-pat-value" not in raw and "ghp_secret" not in raw
    assert [e["action"] for e in client.get("/api/audit", params={"app_id": "checkout"}).json()] == ["app.config", "app.onboard"]
