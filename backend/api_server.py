import base64
import logging
import os
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import requests
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, create_model

from backend import (
    apps_store,
    audit_store,
    config_store,
    notifications,
    oauth_store,
    repo_suggest,
    run_events,
    runs_db,
    skills_store,
    teams_store,
    users_store,
)
from backend.ado_tool import DEFAULT_WIQL, ado_ticket_matches_wiql
from backend.jira_bot import PipelineConfig, PipelineHalt, run_pipeline
from backend.jira_tool import jira_ticket_matches_jql


logger = logging.getLogger(__name__)

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
ADO_WEBHOOK_USERNAME = "sdlc-agentic"
# How long a run waits for someone to approve its PR before giving up.
APPROVAL_TIMEOUT_SEC = int(os.getenv("APPROVAL_TIMEOUT_HOURS", "24")) * 3600
_APPROVAL_POLL_SEC = 2

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
    oauth_store.touch(sdlc_session)
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


# ── Which applications a user may see (new_req.md point 3) ───────────
# Admins see everything. Everyone else sees shared applications (no team)
# plus the ones owned by a team they belong to. A hidden application is
# answered with 404, exactly like one that doesn't exist.
def visible_app_ids(user: dict) -> Optional[set[str]]:
    """None = no restriction (admin)."""
    if users_store.has_role(user, "admin"):
        return None
    mine = teams_store.teams_of(user["login"])
    return {a["id"] for a in apps_store.list_apps() if not a["team_id"] or a["team_id"] in mine}


def _check_app_access(user: dict, app_id: str) -> None:
    visible = visible_app_ids(user)
    if not apps_store.app_exists(app_id) or (visible is not None and app_id not in visible):
        raise HTTPException(status_code=404, detail=f"No application named {app_id!r}")


def _run_or_404(user: dict, run_id: str) -> dict:
    run = runs_db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    visible = visible_app_ids(user)
    if visible is not None and run["app_id"] not in visible:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


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


def _require_app(app_id: Optional[str], user: dict) -> str:
    app_id = app_id or apps_store.DEFAULT_APP_ID
    _check_app_access(user, app_id)
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


def _execute(
    run_id: str,
    ticket_key: str,
    config: PipelineConfig,
    user_id: str | None = None,
    notification_settings: dict[str, str] | None = None,
) -> None:
    start = time.monotonic()

    def on_step(step_key: str, state: str, log: str | None = None) -> None:
        runs_db.update_step(run_id, step_key, state, log)
        run_events.record(
            run_id,
            config.app_id,
            ticket_key,
            step_key,
            state,
            log,
            user_id=user_id,
        )

    def on_log(stage: str, level: str, message: str) -> None:
        run_events.record(
            run_id,
            config.app_id,
            ticket_key,
            stage,
            "in-progress",
            message,
            user_id=user_id,
            level=level,
        )

    def on_usage(
        stage: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float | None,
        request_count: int,
        priced_request_count: int,
    ) -> None:
        try:
            runs_db.record_usage(
                run_id,
                prompt_tokens,
                completion_tokens,
                cost_usd,
                request_count,
                priced_request_count,
            )
        except Exception:
            logger.exception(
                "LLM usage could not be recorded",
                extra={"run_id": run_id, "app_id": config.app_id, "stage": stage},
            )

    def finish(status: str, pr_url: str | None, message: str | None) -> None:
        runs_db.finish_run(run_id, status, pr_url, int(time.monotonic() - start))
        run_events.record(
            run_id,
            config.app_id,
            ticket_key,
            "run",
            status,
            message,
            user_id=user_id,
        )
        if notification_settings is not None:
            try:
                notifications.send_run_notifications(
                    settings=notification_settings,
                    app_id=config.app_id,
                    app_name=config.app_name,
                    run_id=run_id,
                    ticket_key=ticket_key,
                    status=status,
                    message=message,
                    pr_url=pr_url,
                )
            except Exception as e:
                logger.error(
                    "Outbound run notifications failed",
                    extra={
                        "run_id": run_id,
                        "app_id": config.app_id,
                        "error_type": type(e).__name__,
                    },
                )

    def wait_for_approval(_summary: dict):
        """Park this run as awaiting-approval and block until a maintainer
        decides via POST /api/runs/{id}/approval (or the timeout passes)."""
        runs_db.request_approval(run_id)
        deadline = time.monotonic() + APPROVAL_TIMEOUT_SEC
        while time.monotonic() < deadline:
            approval = (runs_db.get_run(run_id) or {}).get("approval") or {}
            if approval.get("status") in ("approved", "rejected"):
                runs_db.set_status(run_id, "in-progress")
                return approval["status"] == "approved", approval.get("by") or "", approval.get("note") or ""
            time.sleep(_APPROVAL_POLL_SEC)
        runs_db.decide_approval(run_id, "expired", "system")
        runs_db.set_status(run_id, "in-progress")
        return None, "", ""

    try:
        result = run_pipeline(
            ticket_key=ticket_key,
            on_step=on_step,
            on_log=on_log,
            config=config,
            wait_for_approval=wait_for_approval,
            on_usage=on_usage,
        )
        pr_url = result.get("pr_url")
        finish("success", pr_url, pr_url)
    except PipelineHalt as e:
        # pr_url is set when a PR opened but a later check (CI) failed.
        finish(e.status, e.pr_url, e.message)
    except Exception as e:
        logger.exception(
            "Unhandled error during pipeline run",
            extra={"run_id": run_id, "ticket_key": ticket_key, "app_id": config.app_id},
        )
        finish("error", None, f"{type(e).__name__}: {str(e)[:300]}")


@app.post("/api/run-ticket")
def run_ticket(req: RunTicketRequest, user: dict = runner):
    app_id = _require_app(req.app_id, user)
    config = _pipeline_config(app_id)
    notification_settings = apps_store.resolve_settings(app_id)
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    runs_db.create_run(run_id, req.ticket_key, started_at, app_id, triggered_by=user["login"])
    threading.Thread(
        target=_execute,
        args=(run_id, req.ticket_key, config, user["login"], notification_settings),
        daemon=True,
    ).start()
    return {"run_id": run_id, "app_id": app_id}


@app.post("/api/webhooks/jira/{app_id}")
def jira_webhook(
    app_id: str,
    payload: dict,
    authorization: Optional[str] = Header(default=None),
    x_atlassian_webhook_identifier: Optional[str] = Header(default=None),
):
    """Authenticate a Jira event, apply the app's JQL filter, and queue its issue."""
    if not apps_store.app_exists(app_id):
        raise HTTPException(status_code=404, detail="Webhook not found")

    settings = apps_store.resolve_settings(app_id)
    webhook_token = settings.get("JIRA_WEBHOOK_TOKEN", "")
    if not webhook_token:
        raise HTTPException(
            status_code=503,
            detail="Jira webhook authentication is not configured for this application",
        )
    scheme, separator, supplied_token = (authorization or "").partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not secrets.compare_digest(
            supplied_token.encode("utf-8"),
            webhook_token.encode("utf-8"),
        )
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid webhook credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if settings.get("TICKET_PROVIDER", "jira").lower() != "jira":
        raise HTTPException(
            status_code=400,
            detail="This application is not configured to use Jira",
        )

    event = payload.get("webhookEvent")
    if event and event not in {"jira:issue_created", "jira:issue_updated"}:
        return {"status": "ignored", "reason": "unsupported_event"}

    issue = payload.get("issue")
    ticket_key = issue.get("key") if isinstance(issue, dict) else None
    if not isinstance(ticket_key, str):
        raise HTTPException(status_code=400, detail="Webhook payload is missing issue.key")
    ticket_key = ticket_key.strip().upper()

    delivery_id = (x_atlassian_webhook_identifier or "").strip() or None
    if delivery_id and len(delivery_id) > 200:
        raise HTTPException(status_code=400, detail="Webhook delivery identifier is too long")

    config = _pipeline_config(app_id)
    try:
        qualifies = jira_ticket_matches_jql(
            base_url=settings.get("JIRA_BASE_URL", ""),
            user_email=settings.get("JIRA_USER_EMAIL", ""),
            api_token=settings.get("JIRA_API_TOKEN", ""),
            jql=config.jira_jql,
            ticket_key=ticket_key,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.warning(
            "Could not validate Jira webhook issue against application JQL",
            extra={"app_id": app_id, "ticket_key": ticket_key, "error_type": type(e).__name__},
        )
        raise HTTPException(
            status_code=502,
            detail="Could not validate the Jira issue; the pipeline was not started",
        ) from e

    if not qualifies:
        return {"status": "ignored", "reason": "issue_does_not_match_jql"}

    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    queue_status = runs_db.create_webhook_run(
        run_id,
        ticket_key,
        started_at,
        app_id,
        delivery_id,
    )
    if queue_status != "queued":
        return {"status": "ignored", "reason": queue_status}

    threading.Thread(
        target=_execute,
        args=(run_id, ticket_key, config, None, settings),
        daemon=True,
    ).start()
    return JSONResponse(
        status_code=202,
        content={"status": "queued", "run_id": run_id, "app_id": app_id},
    )


@app.post("/api/webhooks/ado/{app_id}")
def ado_webhook(
    app_id: str,
    payload: dict,
    authorization: Optional[str] = Header(default=None),
):
    """Authenticate an Azure DevOps service hook, apply app WIQL, and queue its item."""
    if not apps_store.app_exists(app_id):
        raise HTTPException(status_code=404, detail="Webhook not found")

    settings = apps_store.resolve_settings(app_id)
    webhook_token = settings.get("ADO_WEBHOOK_TOKEN", "")
    if not webhook_token:
        raise HTTPException(
            status_code=503,
            detail="Azure DevOps webhook authentication is not configured for this application",
        )

    scheme, separator, encoded_credentials = (authorization or "").partition(" ")
    try:
        decoded_credentials = base64.b64decode(encoded_credentials, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        decoded_credentials = ""
    username, colon, supplied_token = decoded_credentials.partition(":")
    if (
        not separator
        or scheme.lower() != "basic"
        or not colon
        or username != ADO_WEBHOOK_USERNAME
        or not secrets.compare_digest(
            supplied_token.encode("utf-8"),
            webhook_token.encode("utf-8"),
        )
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid webhook credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    if (settings.get("TICKET_PROVIDER") or "jira").lower() != "ado":
        raise HTTPException(
            status_code=400,
            detail="This application is not configured to use Azure DevOps",
        )

    event = payload.get("eventType")
    if not isinstance(event, str):
        raise HTTPException(status_code=400, detail="Webhook payload is missing eventType")
    if event not in {"workitem.created", "workitem.updated"}:
        return {"status": "ignored", "reason": "unsupported_event"}

    resource = payload.get("resource")
    raw_id = resource.get("id") if isinstance(resource, dict) else None
    if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)):
        raise HTTPException(
            status_code=400,
            detail="Webhook payload is missing resource.id",
        )
    ticket_key = str(raw_id).strip()
    if not ticket_key.isdigit() or len(ticket_key) > 10 or int(ticket_key) < 1:
        raise HTTPException(status_code=400, detail="Invalid Azure DevOps work item ID")

    raw_delivery_id = payload.get("id")
    if raw_delivery_id is not None and (
        isinstance(raw_delivery_id, bool) or not isinstance(raw_delivery_id, (int, str))
    ):
        raise HTTPException(status_code=400, detail="Invalid webhook delivery identifier")
    delivery_id = str(raw_delivery_id).strip() if raw_delivery_id is not None else None
    if delivery_id == "":
        delivery_id = None
    if delivery_id and len(delivery_id) > 200:
        raise HTTPException(status_code=400, detail="Webhook delivery identifier is too long")

    config = _pipeline_config(app_id)
    try:
        qualifies = ado_ticket_matches_wiql(
            org_url=settings.get("ADO_ORG_URL", ""),
            project=settings.get("ADO_PROJECT", ""),
            pat=settings.get("ADO_PAT", ""),
            wiql=config.ado_wiql or DEFAULT_WIQL,
            work_item_id=ticket_key,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.warning(
            "Could not validate Azure DevOps webhook work item against application WIQL",
            extra={"app_id": app_id, "ticket_key": ticket_key, "error_type": type(e).__name__},
        )
        raise HTTPException(
            status_code=502,
            detail="Could not validate the Azure DevOps work item; the pipeline was not started",
        ) from e

    if not qualifies:
        return {"status": "ignored", "reason": "work_item_does_not_match_wiql"}

    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    queue_status = runs_db.create_webhook_run(
        run_id,
        ticket_key,
        started_at,
        app_id,
        delivery_id,
    )
    if queue_status != "queued":
        return {"status": "ignored", "reason": queue_status}

    threading.Thread(
        target=_execute,
        args=(run_id, ticket_key, config, None, settings),
        daemon=True,
    ).start()
    return JSONResponse(
        status_code=202,
        content={"status": "queued", "run_id": run_id, "app_id": app_id},
    )


@app.get("/api/run-status/{run_id}")
def run_status(run_id: str, user: dict = viewer):
    return _run_or_404(user, run_id)


@app.get("/api/runs")
def list_runs(app_id: Optional[str] = Query(default=None), mine: bool = Query(default=False), user: dict = viewer):
    """mine=true -> only runs the signed-in user started, across every application."""
    if app_id:
        _check_app_access(user, app_id)
    return runs_db.get_runs(app_id=app_id, triggered_by=user["login"] if mine else None,
                            app_ids=None if app_id else visible_app_ids(user))


class ApprovalRequest(BaseModel):
    decision: str  # "approve" | "reject"
    note: str = ""


@app.post("/api/runs/{run_id}/approval")
def decide_run_approval(run_id: str, req: ApprovalRequest, user: dict = maintainer):
    """Human sign-off for an app that requires approval before a PR opens."""
    if req.decision not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="decision must be 'approve' or 'reject'")
    run = _run_or_404(user, run_id)
    decision = "approved" if req.decision == "approve" else "rejected"
    note = req.note.strip()[:500]
    if not runs_db.decide_approval(run_id, decision, user["login"], note):
        raise HTTPException(status_code=409, detail="This run isn't waiting for approval (already decided, expired, or finished).")
    audit_store.record(user["login"], f"run.{req.decision}", f"Ticket {run['ticket_key']}", run["app_id"], {
        "run_id": run_id, **({"note": note} if note else {}),
    })
    return runs_db.get_run(run_id)


@app.get("/api/stats")
def stats(app_id: Optional[str] = Query(default=None), user: dict = viewer):
    if app_id:
        _check_app_access(user, app_id)
    return runs_db.get_stats(app_id=app_id, app_ids=None if app_id else visible_app_ids(user))


def _event_scope(app_id: Optional[str], user: dict) -> Optional[str]:
    """The comma-separated app filter run_events understands, limited to what
    this user may see. None = nothing visible; "" = no restriction (admin)."""
    visible = visible_app_ids(user)
    requested = [a.strip() for a in (app_id or "").split(",") if a.strip()]
    for requested_id in requested:
        _check_app_access(user, requested_id)
    if requested:
        return ",".join(requested)
    if visible is None:
        return ""
    return ",".join(sorted(visible)) or None


@app.get("/api/events")
def list_events(
    app_id: Optional[str] = Query(default=None),
    user_id: Optional[str] = Query(default=None),
    stage: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    level: Optional[str] = Query(default=None),
    run_id: Optional[str] = Query(default=None),
    ticket_key: Optional[str] = Query(default=None),
    since: Optional[str] = Query(default=None),
    until: Optional[str] = Query(default=None),
    include_trace: bool = Query(default=False),
    limit: int = Query(default=100),
    user: dict = viewer,
):
    """List structured run events with optional app, user, stage and time filters."""
    scope = _event_scope(app_id, user)
    if scope is None:
        return []
    try:
        return run_events.query(
            app_id=scope,
            user_id=user_id,
            stage=stage,
            state=state,
            level=level,
            run_id=run_id,
            ticket_key=ticket_key,
            since=since,
            until=until,
            include_trace=include_trace,
            limit=limit,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/events/summary")
def events_summary(
    app_id: Optional[str] = Query(default=None),
    user_id: Optional[str] = Query(default=None),
    since: Optional[str] = Query(default=None),
    user: dict = viewer,
):
    """Count final stage outcomes for a time range and optional app/user."""
    scope = _event_scope(app_id, user)
    if scope is None:
        return []
    try:
        return run_events.stage_outcomes(
            app_id=scope,
            user_id=user_id,
            since=since,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Onboarded applications ──────────────────────────────────────────
# Built from apps_store's own field list so a new per-app setting can never be
# silently dropped by the API (pydantic ignores fields a model doesn't declare).
AppConfigFields = create_model(
    "AppConfigFields",
    __config__=ConfigDict(extra="forbid"),
    **{field: (Optional[str], None) for field in apps_store.SETTING_FIELDS + apps_store.SECRET_FIELDS},
)


class AppCreateRequest(BaseModel):
    name: str
    team_id: Optional[str] = None  # None = shared with everyone
    description: str = ""
    config: Optional[AppConfigFields] = None


class AppUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    team_id: Optional[str] = None  # only applied when sent; "" or null = shared


def _set_fields(model: Optional[BaseModel]) -> dict:
    return {k: v for k, v in model.model_dump().items() if v is not None} if model else {}


def _app_or_404(app_id: str, user: dict) -> dict:
    _check_app_access(user, app_id)
    return apps_store.get_app(app_id)


@app.get("/api/apps")
def list_apps(user: dict = viewer):
    visible = visible_app_ids(user)
    return [a for a in apps_store.list_apps() if visible is None or a["id"] in visible]


@app.post("/api/apps")
def create_app(req: AppCreateRequest, user: dict = admin):
    config = _set_fields(req.config)
    try:
        created = apps_store.create_app(req.name, description=req.description, config=config, team_id=req.team_id or None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_store.record(user["login"], "app.onboard", created["name"], created["id"], {
        "team": created["team"] or "shared",
        "config": _changes({}, config, apps_store.SECRET_FIELDS),
    })
    return created


@app.get("/api/apps/{app_id}")
def get_app(app_id: str, user: dict = viewer):
    return _app_or_404(app_id, user)


@app.patch("/api/apps/{app_id}")
def update_app(app_id: str, req: AppUpdateRequest, user: dict = maintainer):
    before = _app_or_404(app_id, user)
    kwargs = {}
    if "team_id" in req.model_fields_set:
        # Moving an application between teams changes who can see it.
        if not users_store.has_role(user, "admin"):
            raise HTTPException(status_code=403, detail="Only admins can change which team owns an application.")
        kwargs["team_id"] = req.team_id or None
    try:
        after = apps_store.update_app(app_id, req.name, req.description, **kwargs)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    fields = ("name", "team", "description")
    changes = audit_store.config_changes({k: before[k] for k in fields}, {k: after[k] for k in fields}, [])
    if changes:
        audit_store.record(user["login"], "app.update", after["name"], app_id, changes)
    return after


@app.delete("/api/apps/{app_id}")
def delete_app(app_id: str, user: dict = admin):
    before = _app_or_404(app_id, user)
    try:
        apps_store.delete_app(app_id)
        skills_store.delete_app_skills(app_id)
    except apps_store.AppNotFound:
        raise HTTPException(status_code=404, detail=f"No application named {app_id!r}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_store.record(user["login"], "app.remove", before["name"], app_id)
    return {"ok": True}


@app.get("/api/apps/{app_id}/config")
def get_app_config(app_id: str, user: dict = viewer):
    _check_app_access(user, app_id)
    try:
        cfg = apps_store.get_app_config(app_id)
    except apps_store.AppNotFound:
        raise HTTPException(status_code=404, detail=f"No application named {app_id!r}")
    if not users_store.has_role(user, "maintainer"):
        cfg["secrets"] = None  # viewers/runners don't even learn which tokens are set
    return cfg


@app.get("/api/apps/{app_id}/suggestions")
def get_app_suggestions(app_id: str, user: dict = maintainer):
    """Suggested test command / CI workflow / protected paths, detected from
    the application's target repository."""
    _check_app_access(user, app_id)
    settings = apps_store.resolve_settings(app_id)
    return repo_suggest.suggest_for_repo(
        settings.get("GITHUB_TOKEN", ""), settings.get("TARGET_REPO", ""), settings.get("TARGET_BRANCH", ""),
    )


@app.put("/api/apps/{app_id}/config")
def update_app_config(app_id: str, req: AppConfigFields, user: dict = maintainer):
    _check_app_access(user, app_id)
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


class MarketplaceSkillInstallRequest(BaseModel):
    stage: str
    app_id: Optional[str] = None


def _skill_app(app_id: Optional[str], user: dict) -> Optional[str]:
    """None = the shared skill library; otherwise an app the user may see."""
    return _require_app(app_id, user) if app_id is not None else None


def _read_skill_or_blank(slug: str, app_id: Optional[str]) -> str:
    try:
        return skills_store.read_skill(slug, app_id)
    except (FileNotFoundError, skills_store.SkillValidationError):
        return ""


@app.get("/api/skills")
def list_skills(
    stage: Optional[str] = Query(default=None),
    app_id: Optional[str] = Query(default=None),
    user: dict = viewer,
):
    try:
        return skills_store.list_skills(stage=stage, app_id=_skill_app(app_id, user))
    except skills_store.SkillValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/stages")
def list_stage_map(user: dict = viewer):
    try:
        return skills_store.load_stage_map()
    except skills_store.SkillValidationError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/skills/marketplace")
def list_marketplace_skills(app_id: Optional[str] = Query(default=None), user: dict = viewer):
    try:
        return skills_store.list_public_skills(_skill_app(app_id, user))
    except skills_store.SkillMarketplaceUnavailable as e:
        raise HTTPException(status_code=502, detail=str(e))
    except skills_store.SkillValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/skills/marketplace/{slug}")
def get_marketplace_skill(slug: str, user: dict = viewer):
    try:
        return {"slug": slug, "content": skills_store.read_public_skill(slug)}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except skills_store.SkillMarketplaceUnavailable as e:
        raise HTTPException(status_code=502, detail=str(e))
    except skills_store.SkillValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/skills/marketplace/{slug}/install")
def install_marketplace_skill(
    slug: str,
    req: MarketplaceSkillInstallRequest,
    user: dict = maintainer,
):
    resolved_app_id = _require_app(req.app_id, user)
    try:
        skills_store.install_public_skill(slug, req.stage, resolved_app_id)
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except skills_store.SkillMarketplaceUnavailable as e:
        raise HTTPException(status_code=502, detail=str(e))
    except skills_store.SkillValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_store.record(user["login"], "skill.install", slug, resolved_app_id, {"stage": req.stage})
    return {"slug": slug, "app_id": resolved_app_id, "stage": req.stage}


@app.get("/api/skills/{slug}")
def get_skill(slug: str, app_id: Optional[str] = Query(default=None), user: dict = viewer):
    resolved_app_id = _skill_app(app_id, user)
    try:
        return {
            "slug": slug,
            "content": skills_store.read_skill(slug, resolved_app_id),
        }
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No skill named {slug!r}")
    except skills_store.SkillValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/skills/{slug}")
def update_skill(
    slug: str,
    req: SkillWriteRequest,
    app_id: Optional[str] = Query(default=None),
    user: dict = maintainer,
):
    resolved_app_id = _skill_app(app_id, user)
    before = _read_skill_or_blank(slug, resolved_app_id)
    try:
        skills_store.write_skill(slug, req.content, resolved_app_id)
    except skills_store.SkillValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if before != req.content:
        audit_store.record(user["login"], "skill.update", slug, resolved_app_id, {
            "lines_before": len(before.splitlines()), "lines_after": len(req.content.splitlines()),
        })
    return {
        "slug": slug,
        "content": skills_store.read_skill(slug, resolved_app_id),
    }


@app.post("/api/skills")
def create_skill(
    slug: str,
    req: SkillWriteRequest,
    app_id: Optional[str] = Query(default=None),
    user: dict = maintainer,
):
    resolved_app_id = _skill_app(app_id, user)
    try:
        skills_store.create_skill(slug, req.content, resolved_app_id)
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except skills_store.SkillValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_store.record(user["login"], "skill.create", slug, resolved_app_id, {"lines": len(req.content.splitlines())})
    return {
        "slug": slug,
        "content": skills_store.read_skill(slug, resolved_app_id),
    }


# ── Persona skill version history ────────────────────────────────────
# Each save of an existing skill keeps the previous text (see
# skills_store.write_skill), so a bad edit can be looked at and undone.
@app.get("/api/skills/{slug}/versions")
def list_skill_versions(slug: str, app_id: Optional[str] = Query(default=None), user: dict = viewer):
    resolved_app_id = _skill_app(app_id, user)
    try:
        return skills_store.list_versions(slug, resolved_app_id)
    except skills_store.SkillValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/skills/{slug}/versions/{version_id}")
def get_skill_version(
    slug: str,
    version_id: str,
    app_id: Optional[str] = Query(default=None),
    user: dict = viewer,
):
    resolved_app_id = _skill_app(app_id, user)
    try:
        content = skills_store.read_version(slug, version_id, resolved_app_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except skills_store.SkillValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"slug": slug, "version_id": version_id, "content": content}


@app.post("/api/skills/{slug}/versions/{version_id}/restore")
def restore_skill_version(
    slug: str,
    version_id: str,
    app_id: Optional[str] = Query(default=None),
    user: dict = maintainer,
):
    resolved_app_id = _skill_app(app_id, user)
    try:
        skills_store.restore_version(slug, version_id, resolved_app_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except skills_store.SkillValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_store.record(user["login"], "skill.restore", slug, resolved_app_id, {"version": version_id})
    return {
        "slug": slug,
        "content": skills_store.read_skill(slug, resolved_app_id),
    }


# ── Stack modifiers (read-only list) ─────────────────────────────────
@app.get("/api/modifiers")
def list_modifiers(user: dict = viewer):
    return skills_store.list_modifiers()


# ── Server defaults (.env) ──────────────────────────────────────────
ConfigUpdateRequest = create_model(
    "ConfigUpdateRequest",
    **{field: (Optional[str], None) for field in config_store.ALL_MANAGED_FIELDS},
)


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
    if app_id:
        _check_app_access(user, app_id)
    entries = audit_store.list_entries(limit=limit, app_id=app_id, actor=actor)
    visible = visible_app_ids(user)
    return [e for e in entries if visible is None or not e["app_id"] or e["app_id"] in visible]


# ── Teams (new_req.md point 3) ──────────────────────────────────────
class TeamCreateRequest(BaseModel):
    name: str


class TeamMemberRequest(BaseModel):
    login: str


@app.get("/api/teams")
def list_teams(user: dict = viewer):
    """Admins see every team; everyone else sees the teams they're in."""
    if users_store.has_role(user, "admin"):
        return teams_store.list_teams()
    return teams_store.list_teams(only_for=user["login"])


@app.post("/api/teams")
def create_team(req: TeamCreateRequest, user: dict = admin):
    try:
        team = teams_store.create_team(req.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_store.record(user["login"], "team.create", team["name"])
    return team


@app.delete("/api/teams/{team_id}")
def delete_team(team_id: str, user: dict = admin):
    try:
        team = teams_store.get_team(team_id)
        teams_store.delete_team(team_id)
    except teams_store.TeamNotFound:
        raise HTTPException(status_code=404, detail="No such team")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_store.record(user["login"], "team.delete", team["name"])
    return {"ok": True}


@app.post("/api/teams/{team_id}/members")
def add_team_member(team_id: str, req: TeamMemberRequest, user: dict = admin):
    try:
        teams_store.add_member(team_id, req.login.strip().lstrip("@"))
        team = teams_store.get_team(team_id)
    except teams_store.TeamNotFound:
        raise HTTPException(status_code=404, detail="No such team")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_store.record(user["login"], "team.member_add", team["name"], None, {"member": req.login})
    return team


@app.delete("/api/teams/{team_id}/members/{login}")
def remove_team_member(team_id: str, login: str, user: dict = admin):
    try:
        team = teams_store.get_team(team_id)
    except teams_store.TeamNotFound:
        raise HTTPException(status_code=404, detail="No such team")
    if not teams_store.remove_member(team_id, login):
        raise HTTPException(status_code=404, detail=f"@{login} isn't in {team['name']}")
    audit_store.record(user["login"], "team.member_remove", team["name"], None, {"member": login})
    return teams_store.get_team(team_id)


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
def github_callback(code: str, request: Request):
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
    session_id = oauth_store.create_session(access_token, login, user_data.get("avatar_url"), request.headers.get("user-agent"))

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
    return {
        "login": user["login"], "avatar_url": user["avatar_url"], "role": user["role"],
        "preferences": user.get("preferences") or {},
    }


# ── The signed-in user's own account (new_req.md point 4) ───────────
class PreferencesRequest(BaseModel):
    default_app_id: Optional[str] = None
    theme: Optional[str] = None
    active_team_id: Optional[str] = None


@app.put("/api/me/preferences")
def update_my_preferences(req: PreferencesRequest, user: dict = viewer):
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if updates.get("default_app_id"):
        visible = visible_app_ids(user)
        if not apps_store.app_exists(updates["default_app_id"]) or (visible is not None and updates["default_app_id"] not in visible):
            raise HTTPException(status_code=400, detail=f"No application named {updates['default_app_id']!r}")
    team = updates.get("active_team_id")
    if team and not (teams_store.team_exists(team) and (users_store.has_role(user, "admin") or team in teams_store.teams_of(user["login"]))):
        raise HTTPException(status_code=400, detail=f"You're not in a team named {team!r}")
    try:
        return users_store.update_preferences(user["login"], updates)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/me/sessions")
def my_sessions(user: dict = viewer):
    return oauth_store.list_sessions(user["login"], user["session_id"])


@app.delete("/api/me/sessions/{session_public_id}")
def revoke_my_session(session_public_id: str, user: dict = viewer):
    if not oauth_store.revoke_session(user["login"], session_public_id):
        raise HTTPException(status_code=404, detail="No such session")
    return {"ok": True}


@app.post("/api/me/sessions/sign-out-others")
def sign_out_other_sessions(user: dict = viewer):
    """Sign out every other device/browser, keeping this one."""
    return {"signed_out": oauth_store.revoke_all(user["login"], except_session_id=user["session_id"])}


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
    app_id = _require_app(req.app_id, user)
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
