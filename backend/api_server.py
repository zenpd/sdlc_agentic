import os
import secrets
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Optional

import requests
from fastapi import Cookie, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from backend import runs_db, skills_store, config_store, oauth_store, apps_store
from backend.jira_bot import PipelineConfig, PipelineHalt, run_pipeline

runs_db.init_db()
oauth_store.init_db()
apps_store.init_db()

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
def run_ticket(req: RunTicketRequest):
    app_id = _require_app(req.app_id)
    config = _pipeline_config(app_id)
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    runs_db.create_run(run_id, req.ticket_key, started_at, app_id)
    threading.Thread(target=_execute, args=(run_id, req.ticket_key, config), daemon=True).start()
    return {"run_id": run_id, "app_id": app_id}


@app.get("/api/run-status/{run_id}")
def run_status(run_id: str):
    run = runs_db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.get("/api/runs")
def list_runs(app_id: Optional[str] = Query(default=None)):
    return runs_db.get_runs(app_id=app_id)


@app.get("/api/stats")
def stats(app_id: Optional[str] = Query(default=None)):
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


@app.get("/api/apps")
def list_apps():
    return apps_store.list_apps()


@app.post("/api/apps")
def create_app(req: AppCreateRequest):
    try:
        return apps_store.create_app(req.name, req.team, req.description, _set_fields(req.config))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/apps/{app_id}")
def get_app(app_id: str):
    try:
        return apps_store.get_app(app_id)
    except apps_store.AppNotFound:
        raise HTTPException(status_code=404, detail=f"No application named {app_id!r}")


@app.patch("/api/apps/{app_id}")
def update_app(app_id: str, req: AppUpdateRequest):
    try:
        return apps_store.update_app(app_id, req.name, req.team, req.description)
    except apps_store.AppNotFound:
        raise HTTPException(status_code=404, detail=f"No application named {app_id!r}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/apps/{app_id}")
def delete_app(app_id: str):
    try:
        apps_store.delete_app(app_id)
    except apps_store.AppNotFound:
        raise HTTPException(status_code=404, detail=f"No application named {app_id!r}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.get("/api/apps/{app_id}/config")
def get_app_config(app_id: str):
    try:
        return apps_store.get_app_config(app_id)
    except apps_store.AppNotFound:
        raise HTTPException(status_code=404, detail=f"No application named {app_id!r}")


@app.put("/api/apps/{app_id}/config")
def update_app_config(app_id: str, req: AppConfigFields):
    try:
        apps_store.update_app_config(app_id, _set_fields(req))
        return apps_store.get_app_config(app_id)
    except apps_store.AppNotFound:
        raise HTTPException(status_code=404, detail=f"No application named {app_id!r}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Persona skills ──────────────────────────────────────────────────
class SkillWriteRequest(BaseModel):
    content: str


@app.get("/api/skills")
def list_skills():
    return skills_store.list_skills()


@app.get("/api/skills/{slug}")
def get_skill(slug: str):
    try:
        return {"slug": slug, "content": skills_store.read_skill(slug)}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No skill named {slug!r}")


@app.put("/api/skills/{slug}")
def update_skill(slug: str, req: SkillWriteRequest):
    try:
        skills_store.write_skill(slug, req.content)
    except skills_store.SkillValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"slug": slug, "content": skills_store.read_skill(slug)}


@app.post("/api/skills")
def create_skill(slug: str, req: SkillWriteRequest):
    try:
        skills_store.create_skill(slug, req.content)
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except skills_store.SkillValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"slug": slug, "content": skills_store.read_skill(slug)}


# ── Integration config ──────────────────────────────────────────────
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
def get_config():
    return config_store.get_config()


@app.put("/api/config")
def update_config(req: ConfigUpdateRequest):
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    try:
        config_store.update_config(updates)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return config_store.get_config()


# ── GitHub OAuth login + repo picker ─────────────────────────────────
def _current_github_session(sdlc_session: Optional[str]) -> dict:
    session = oauth_store.get_session(sdlc_session) if sdlc_session else None
    if not session:
        raise HTTPException(status_code=401, detail="Not connected to GitHub")
    return session


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

    session_id = oauth_store.create_session(access_token, user_data.get("login", ""), user_data.get("avatar_url"))

    redirect = RedirectResponse(f"{FRONTEND_URL}/")
    redirect.set_cookie(
        key=SESSION_COOKIE,
        value=session_id,
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=60 * 60 * 24 * 30,
    )
    return redirect


@app.get("/api/auth/github/me")
def github_me(sdlc_session: Optional[str] = Cookie(default=None)):
    session = _current_github_session(sdlc_session)
    return {"login": session["github_login"], "avatar_url": session["avatar_url"]}


@app.post("/api/auth/github/logout")
def github_logout(sdlc_session: Optional[str] = Cookie(default=None)):
    if sdlc_session:
        oauth_store.delete_session(sdlc_session)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/api/github/repos")
def github_repos(sdlc_session: Optional[str] = Cookie(default=None)):
    session = _current_github_session(sdlc_session)
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
            oauth_store.delete_session(sdlc_session)
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
def github_select_repo(req: SelectRepoRequest, sdlc_session: Optional[str] = Cookie(default=None)):
    """Points one application at a repository, using the logged-in user's
    GitHub token for that application's clone/push/PR calls."""
    session = _current_github_session(sdlc_session)
    app_id = _require_app(req.app_id)
    updates = {"TARGET_REPO": req.full_name, "GITHUB_TOKEN": session["access_token"]}
    if req.default_branch:
        updates["TARGET_BRANCH"] = req.default_branch
    try:
        apps_store.update_app_config(app_id, updates)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return apps_store.get_app_config(app_id)
