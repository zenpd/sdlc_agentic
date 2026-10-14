import os
import secrets
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Optional

import requests
from fastapi import Cookie, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from backend import runs_db, skills_store, config_store, oauth_store, apps_store, users_store, audit_store
from backend.jira_bot import PipelineConfig, PipelineHalt, run_pipeline

runs_db.init_db()
oauth_store.init_db()
apps_store.init_db()
users_store.init_db()
audit_store.init_db()

GITHUB_OAUTH_CLIENT_ID = os.getenv("GITHUB_OAUTH_CLIENT_ID", "")
GITHUB_OAUTH_CLIENT_SECRET = os.getenv("GITHUB_OAUTH_CLIENT_SECRET", "")
GITHUB_OAUTH_CALLBACK_URL = os.getenv("GITHUB_OAUTH_CALLBACK_URL", "http://localhost:8040/api/auth/github/callback")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
SESSION_COOKIE = "sdlc_session"

app = FastAPI(title="Jira SDLC Pipeline API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Who is calling, and what may they do ────────────────────────────
# Every /api route except the login flow requires a signed-in user, checked
# here on the server — hiding a button in the UI is not access control.
def _current_github_session(sdlc_session: Optional[str]) -> dict:
    session = oauth_store.get_session(sdlc_session) if sdlc_session else None
    if not session:
        raise HTTPException(status_code=401, detail="Not signed in")
    return session


def current_user(sdlc_session: Optional[str] = Cookie(default=None)) -> dict:
    session = _current_github_session(sdlc_session)
    user = users_store.get_user(session["github_login"])
    if not user:
        # Session from before the users table existed — register it now.
        users_store.ensure_user(session["github_login"], session["avatar_url"])
        user = users_store.get_user(session["github_login"])
    return {**user, "session_id": sdlc_session}


def require(minimum: str):
    def dependency(user: dict = Depends(current_user)) -> dict:
        if not users_store.has_role(user, minimum):
            raise HTTPException(
                status_code=403,
                detail=f"Your role ({user['role']}) can't do this — it needs {minimum} or higher. Ask an admin.",
            )
        return user
    return dependency


viewer = Depends(require("viewer"))
runner = Depends(require("runner"))
maintainer = Depends(require("maintainer"))
admin = Depends(require("admin"))


def _changes(before: dict, updates: dict, secret_fields: list[str]) -> dict:
    """Audit diff for a config PUT: old -> new for settings, "updated" for a
    secret that was given a new value (blank secret = unchanged)."""
    after = dict(before)
    for key, value in updates.items():
        if key in secret_fields:
            if value:
                after[key] = object()  # always "different" -> recorded as updated
        else:
            after[key] = value or ""
    return audit_store.config_changes(before, after, secret_fields)


# ── Pipeline runs ───────────────────────────────────────────────────
class RunTicketRequest(BaseModel):
    ticket_key: str
    app_id: Optional[str] = None


def _require_app(app_id: Optional[str]) -> str:
    app_id = app_id or apps_store.DEFAULT_APP_ID
    if not apps_store.app_exists(app_id):
        raise HTTPException(status_code=404, detail=f"No application named {app_id!r}")
    return app_id


def _pipeline_config(app_id: str) -> PipelineConfig:
    """Snapshot of one application's settings, taken when the run is queued —
    a config edit made while it's running can't change it mid-flight, and it
    never touches any other application's run."""
    app = apps_store.get_app(app_id)
    try:
        return PipelineConfig.from_settings(apps_store.resolve_settings(app_id), app_id=app_id, app_name=app["name"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _execute(run_id: str, ticket_key: str, config: PipelineConfig) -> None:
    start = time.monotonic()

    def on_step(step_key: str, state: str, log: str | None = None) -> None:
        runs_db.update_step(run_id, step_key, state, log)

    try:
        result = run_pipeline(ticket_key=ticket_key, on_step=on_step, config=config)
        runs_db.finish_run(run_id, "success", result.get("pr_url"), int(time.monotonic() - start))
    except PipelineHalt as e:
        runs_db.finish_run(run_id, e.status, None, int(time.monotonic() - start))
    except Exception:
        print(f"!! Unhandled error running {ticket_key} (run {run_id}):")
        traceback.print_exc()
        runs_db.finish_run(run_id, "error", None, int(time.monotonic() - start))


@app.post("/api/run-ticket")
def run_ticket(req: RunTicketRequest, user: dict = runner):
    app_id = _require_app(req.app_id)
    config = _pipeline_config(app_id)
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    runs_db.create_run(run_id, req.ticket_key, started_at, app_id, triggered_by=user["login"])
    threading.Thread(target=_execute, args=(run_id, req.ticket_key, config), daemon=True).start()
    return {"run_id": run_id, "app_id": app_id}


@app.get("/api/run-status/{run_id}")
def run_status(run_id: str, user: dict = viewer):
    run = runs_db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.get("/api/runs")
def list_runs(app_id: Optional[str] = Query(default=None), user: dict = viewer):
    return runs_db.get_runs(app_id=app_id)


@app.get("/api/stats")
def stats(app_id: Optional[str] = Query(default=None), user: dict = viewer):
    return runs_db.get_stats(app_id=app_id)


# ── Onboarded applications ──────────────────────────────────────────
class AppConfigFields(BaseModel):
    TICKET_PROVIDER: Optional[str] = None
    JIRA_BASE_URL: Optional[str] = None
    JIRA_USER_EMAIL: Optional[str] = None
    JIRA_API_TOKEN: Optional[str] = None
    JIRA_JQL: Optional[str] = None
    ADO_ORG_URL: Optional[str] = None
    ADO_PROJECT: Optional[str] = None
    ADO_PAT: Optional[str] = None
    ADO_ASSIGNEE: Optional[str] = None
    ADO_WIQL: Optional[str] = None
    TARGET_REPO: Optional[str] = None
    TARGET_BRANCH: Optional[str] = None
    GITHUB_TOKEN: Optional[str] = None
    AZURE_OPENAI_ENDPOINT: Optional[str] = None
    AZURE_OPENAI_DEPLOYMENT: Optional[str] = None
    AZURE_OPENAI_API_KEY: Optional[str] = None
    STATUS_DONE: Optional[str] = None
    STATUS_IN_PROGRESS: Optional[str] = None
    STATUS_FAILED: Optional[str] = None
    DEFAULT_PERSONA: Optional[str] = None
    BRANCH_PREFIX: Optional[str] = None
    GATE_MIN_WORDS: Optional[str] = None


class AppCreateRequest(BaseModel):
    name: str
    team: str = ""
    description: str = ""
    config: Optional[AppConfigFields] = None


class AppUpdateRequest(BaseModel):
    name: Optional[str] = None
    team: Optional[str] = None
    description: Optional[str] = None


def _set_fields(model: Optional[BaseModel]) -> dict:
    return {k: v for k, v in model.model_dump().items() if v is not None} if model else {}


def _app_or_404(app_id: str) -> dict:
    try:
        return apps_store.get_app(app_id)
    except apps_store.AppNotFound:
        raise HTTPException(status_code=404, detail=f"No application named {app_id!r}")


@app.get("/api/apps")
def list_apps(user: dict = viewer):
    return apps_store.list_apps()


@app.post("/api/apps")
def create_app(req: AppCreateRequest, user: dict = admin):
    config = _set_fields(req.config)
    try:
        created = apps_store.create_app(req.name, req.team, req.description, config)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_store.record(user["login"], "app.onboard", created["name"], created["id"], {
        "team": created["team"],
        "config": _changes({}, config, apps_store.SECRET_FIELDS),
    })
    return created


@app.get("/api/apps/{app_id}")
def get_app(app_id: str, user: dict = viewer):
    return _app_or_404(app_id)


@app.patch("/api/apps/{app_id}")
def update_app(app_id: str, req: AppUpdateRequest, user: dict = maintainer):
    before = _app_or_404(app_id)
    try:
        after = apps_store.update_app(app_id, req.name, req.team, req.description)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    fields = ("name", "team", "description")
    changes = audit_store.config_changes({k: before[k] for k in fields}, {k: after[k] for k in fields}, [])
    if changes:
        audit_store.record(user["login"], "app.update", after["name"], app_id, changes)
    return after


@app.delete("/api/apps/{app_id}")
def delete_app(app_id: str, user: dict = admin):
    before = _app_or_404(app_id)
    try:
        apps_store.delete_app(app_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_store.record(user["login"], "app.remove", before["name"], app_id)
    return {"ok": True}


@app.get("/api/apps/{app_id}/config")
def get_app_config(app_id: str, user: dict = viewer):
    try:
        cfg = apps_store.get_app_config(app_id)
    except apps_store.AppNotFound:
        raise HTTPException(status_code=404, detail=f"No application named {app_id!r}")
    if not users_store.has_role(user, "maintainer"):
        cfg["secrets"] = None  # viewers/runners don't even learn which tokens are set
    return cfg


@app.put("/api/apps/{app_id}/config")
def update_app_config(app_id: str, req: AppConfigFields, user: dict = maintainer):
    updates = _set_fields(req)
    try:
        before = apps_store.get_app_config(app_id)["values"]
        apps_store.update_app_config(app_id, updates)
        after = apps_store.get_app_config(app_id)
    except apps_store.AppNotFound:
        raise HTTPException(status_code=404, detail=f"No application named {app_id!r}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    changes = _changes(before, updates, apps_store.SECRET_FIELDS)
    if changes:
        audit_store.record(user["login"], "app.config", after["app"]["name"], app_id, changes)
    return after


# ── Persona skills ──────────────────────────────────────────────────
class SkillWriteRequest(BaseModel):
    content: str


@app.get("/api/skills")
def list_skills(user: dict = viewer):
    return skills_store.list_skills()


@app.get("/api/skills/{slug}")
def get_skill(slug: str, user: dict = viewer):
    try:
        return {"slug": slug, "content": skills_store.read_skill(slug)}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No skill named {slug!r}")


@app.put("/api/skills/{slug}")
def update_skill(slug: str, req: SkillWriteRequest, user: dict = maintainer):
    try:
        before = skills_store.read_skill(slug)
    except FileNotFoundError:
        before = ""
    try:
        skills_store.write_skill(slug, req.content)
    except skills_store.SkillValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if before != req.content:
        audit_store.record(user["login"], "skill.update", slug, None, {
            "lines_before": len(before.splitlines()), "lines_after": len(req.content.splitlines()),
        })
    return {"slug": slug, "content": skills_store.read_skill(slug)}


@app.post("/api/skills")
def create_skill(slug: str, req: SkillWriteRequest, user: dict = maintainer):
    try:
        skills_store.create_skill(slug, req.content)
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except skills_store.SkillValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_store.record(user["login"], "skill.create", slug, None, {"lines": len(req.content.splitlines())})
    return {"slug": slug, "content": skills_store.read_skill(slug)}


# ── Server defaults (.env) ──────────────────────────────────────────
class ConfigUpdateRequest(BaseModel):
    TICKET_PROVIDER: Optional[str] = None
    JIRA_BASE_URL: Optional[str] = None
    JIRA_USER_EMAIL: Optional[str] = None
    JIRA_API_TOKEN: Optional[str] = None
    JIRA_JQL: Optional[str] = None
    ADO_ORG_URL: Optional[str] = None
    ADO_PROJECT: Optional[str] = None
    ADO_PAT: Optional[str] = None
    ADO_ASSIGNEE: Optional[str] = None
    ADO_WIQL: Optional[str] = None
    TARGET_REPO: Optional[str] = None
    TARGET_BRANCH: Optional[str] = None
    GITHUB_TOKEN: Optional[str] = None
    AZURE_OPENAI_ENDPOINT: Optional[str] = None
    AZURE_OPENAI_DEPLOYMENT: Optional[str] = None
    AZURE_OPENAI_API_KEY: Optional[str] = None
    STATUS_DONE: Optional[str] = None
    STATUS_IN_PROGRESS: Optional[str] = None
    STATUS_FAILED: Optional[str] = None


@app.get("/api/config")
def get_config(user: dict = maintainer):
    return config_store.get_config()


@app.put("/api/config")
def update_config(req: ConfigUpdateRequest, user: dict = admin):
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    before = {k: v for k, v in config_store.get_config().items() if k in config_store.NON_SECRET_FIELDS}
    try:
        config_store.update_config(updates)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    changes = _changes(before, updates, config_store.SECRET_FIELDS)
    if changes:
        audit_store.record(user["login"], "server.config", "Server defaults (.env)", None, changes)
    return config_store.get_config()


# ── Users & roles ───────────────────────────────────────────────────
class RoleUpdateRequest(BaseModel):
    role: str


@app.get("/api/users")
def list_users(user: dict = admin):
    return users_store.list_users()


@app.patch("/api/users/{login}")
def update_user_role(login: str, req: RoleUpdateRequest, user: dict = admin):
    before = users_store.get_user(login)
    if not before:
        raise HTTPException(status_code=404, detail=f"No user named {login!r}")
    try:
        after = users_store.set_role(login, req.role)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if before["role"] != after["role"]:
        audit_store.record(user["login"], "user.role", login, None, {"role": {"from": before["role"], "to": after["role"]}})
    return after


@app.get("/api/audit")
def list_audit(
    app_id: Optional[str] = Query(default=None),
    actor: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    user: dict = maintainer,
):
    return audit_store.list_entries(limit=limit, app_id=app_id, actor=actor)


# ── GitHub OAuth login + repo picker ─────────────────────────────────
@app.get("/api/auth/github/login")
def github_login():
    if not GITHUB_OAUTH_CLIENT_ID:
        raise HTTPException(status_code=500, detail="GITHUB_OAUTH_CLIENT_ID is not configured")
    params = {
        "client_id": GITHUB_OAUTH_CLIENT_ID,
        "redirect_uri": GITHUB_OAUTH_CALLBACK_URL,
        "scope": "repo read:user",
        "state": secrets.token_urlsafe(16),
    }
    query = "&".join(f"{k}={requests.utils.quote(v)}" for k, v in params.items())
    return RedirectResponse(f"https://github.com/login/oauth/authorize?{query}")


@app.get("/api/auth/github/callback")
def github_callback(code: str):
    token_resp = requests.post(
        "https://github.com/login/oauth/access_token",
        headers={"Accept": "application/json"},
        data={
            "client_id": GITHUB_OAUTH_CLIENT_ID,
            "client_secret": GITHUB_OAUTH_CLIENT_SECRET,
            "code": code,
            "redirect_uri": GITHUB_OAUTH_CALLBACK_URL,
        },
        timeout=15,
    )
    token_data = token_resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail=f"GitHub OAuth failed: {token_data}")

    user_resp = requests.get(
        "https://api.github.com/user",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
        timeout=15,
    )
    user_data = user_resp.json()
    login = user_data.get("login", "")
    if not login:
        raise HTTPException(status_code=400, detail="GitHub didn't return a username")

    users_store.ensure_user(login, user_data.get("avatar_url"))
    session_id = oauth_store.create_session(access_token, login, user_data.get("avatar_url"))

    redirect = RedirectResponse(f"{FRONTEND_URL}/")
    redirect.set_cookie(
        key=SESSION_COOKIE,
        value=session_id,
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=oauth_store.SESSION_MAX_AGE_SEC,
    )
    return redirect


@app.get("/api/auth/github/me")
def github_me(user: dict = Depends(current_user)):
    return {"login": user["login"], "avatar_url": user["avatar_url"], "role": user["role"]}


@app.post("/api/auth/github/logout")
def github_logout(sdlc_session: Optional[str] = Cookie(default=None)):
    if sdlc_session:
        oauth_store.delete_session(sdlc_session)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/api/github/repos")
def github_repos(user: dict = viewer):
    session = _current_github_session(user["session_id"])
    repos: list[dict] = []
    page = 1
    while True:
        resp = requests.get(
            "https://api.github.com/user/repos",
            headers={"Authorization": f"Bearer {session['access_token']}", "Accept": "application/vnd.github+json"},
            params={"per_page": 100, "page": page, "sort": "updated"},
            timeout=15,
        )
        if resp.status_code == 401:
            # The stored GitHub token no longer works (revoked on GitHub's side,
            # or the OAuth app's credentials changed since login) — drop the
            # dead session so the frontend re-prompts a fresh login instead of
            # surfacing a raw 500 from GitHub's API.
            oauth_store.delete_session(user["session_id"])
            raise HTTPException(status_code=401, detail="GitHub session expired — please log in again.")
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        repos.extend(batch)
        if len(batch) < 100 or page >= 5:  # cap at 500 repos
            break
        page += 1

    return [
        {
            "full_name": r["full_name"],
            "private": r["private"],
            "default_branch": r.get("default_branch", "main"),
            "updated_at": r.get("updated_at"),
            "description": r.get("description"),
        }
        for r in repos
    ]


class SelectRepoRequest(BaseModel):
    full_name: str
    default_branch: Optional[str] = None
    app_id: Optional[str] = None


@app.post("/api/github/select-repo")
def github_select_repo(req: SelectRepoRequest, user: dict = maintainer):
    """Points one application at a repository, using the signed-in user's
    GitHub token for that application's clone/push/PR calls."""
    session = _current_github_session(user["session_id"])
    app_id = _require_app(req.app_id)
    before = apps_store.get_app_config(app_id)["values"]
    updates = {"TARGET_REPO": req.full_name, "GITHUB_TOKEN": session["access_token"]}
    if req.default_branch:
        updates["TARGET_BRANCH"] = req.default_branch
    try:
        apps_store.update_app_config(app_id, updates)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    after = apps_store.get_app_config(app_id)
    audit_store.record(user["login"], "app.repo", after["app"]["name"], app_id, _changes(before, updates, apps_store.SECRET_FIELDS))
    return after
