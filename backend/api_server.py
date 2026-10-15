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
from fastapi import Cookie, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from backend import (
    apps_store,
    config_store,
    notifications,
    oauth_store,
    run_events,
    runs_db,
    skills_store,
)
from backend.ado_tool import DEFAULT_WIQL, ado_ticket_matches_wiql
from backend.jira_bot import PipelineConfig, PipelineHalt, run_pipeline
from backend.jira_tool import jira_ticket_matches_jql


logger = logging.getLogger(__name__)

runs_db.init_db()
oauth_store.init_db()
apps_store.init_db()

GITHUB_OAUTH_CLIENT_ID = os.getenv("GITHUB_OAUTH_CLIENT_ID", "")
GITHUB_OAUTH_CLIENT_SECRET = os.getenv("GITHUB_OAUTH_CLIENT_SECRET", "")
GITHUB_OAUTH_CALLBACK_URL = os.getenv("GITHUB_OAUTH_CALLBACK_URL", "http://localhost:8040/api/auth/github/callback")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
SESSION_COOKIE = "sdlc_session"
ADO_WEBHOOK_USERNAME = "sdlc-agentic"

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

    try:
        result = run_pipeline(
            ticket_key=ticket_key,
            on_step=on_step,
            on_log=on_log,
            config=config,
            on_usage=on_usage,
        )
        pr_url = result.get("pr_url")
        finish("success", pr_url, pr_url)
    except PipelineHalt as e:
        finish(e.status, None, e.message)
    except Exception as e:
        logger.exception(
            "Unhandled error during pipeline run",
            extra={"run_id": run_id, "ticket_key": ticket_key, "app_id": config.app_id},
        )
        finish("error", None, f"{type(e).__name__}: {str(e)[:300]}")


@app.post("/api/run-ticket")
def run_ticket(
    req: RunTicketRequest,
    sdlc_session: Optional[str] = Cookie(default=None),
):
    app_id = _require_app(req.app_id)
    config = _pipeline_config(app_id)
    notification_settings = apps_store.resolve_settings(app_id)
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    runs_db.create_run(run_id, req.ticket_key, started_at, app_id)
    session = oauth_store.get_session(sdlc_session) if sdlc_session else None
    user_id = session.get("github_login") if session else None
    threading.Thread(
        target=_execute,
        args=(run_id, req.ticket_key, config, user_id, notification_settings),
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
):
    """List structured run events with optional app, user, stage and time filters."""
    try:
        return run_events.query(
            app_id=app_id,
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
):
    """Count final stage outcomes for a time range and optional app/user."""
    try:
        return run_events.stage_outcomes(
            app_id=app_id,
            user_id=user_id,
            since=since,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


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
    JIRA_WEBHOOK_TOKEN: Optional[str] = None
    ADO_WEBHOOK_TOKEN: Optional[str] = None
    NOTIFY_ON_SUCCESS: Optional[str] = None
    NOTIFY_ON_FAILURE: Optional[str] = None
    NOTIFY_EMAIL_TO: Optional[str] = None
    SMTP_HOST: Optional[str] = None
    SMTP_PORT: Optional[str] = None
    SMTP_FROM: Optional[str] = None
    SMTP_USERNAME: Optional[str] = None
    SMTP_STARTTLS: Optional[str] = None
    SLACK_WEBHOOK_URL: Optional[str] = None
    TEAMS_WEBHOOK_URL: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None


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
        skills_store.delete_app_skills(app_id)
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


class MarketplaceSkillInstallRequest(BaseModel):
    stage: str
    app_id: Optional[str] = None


@app.get("/api/skills")
def list_skills(
    stage: Optional[str] = Query(default=None),
    app_id: Optional[str] = Query(default=None),
):
    try:
        resolved_app_id = _require_app(app_id) if app_id is not None else None
        return skills_store.list_skills(stage=stage, app_id=resolved_app_id)
    except skills_store.SkillValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/stages")
def list_stage_map():
    try:
        return skills_store.load_stage_map()
    except skills_store.SkillValidationError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/skills/marketplace")
def list_marketplace_skills(app_id: Optional[str] = Query(default=None)):
    try:
        resolved_app_id = _require_app(app_id) if app_id is not None else None
        return skills_store.list_public_skills(resolved_app_id)
    except skills_store.SkillMarketplaceUnavailable as e:
        raise HTTPException(status_code=502, detail=str(e))
    except skills_store.SkillValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/skills/marketplace/{slug}")
def get_marketplace_skill(slug: str):
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
):
    try:
        resolved_app_id = _require_app(req.app_id)
        skills_store.install_public_skill(slug, req.stage, resolved_app_id)
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except skills_store.SkillMarketplaceUnavailable as e:
        raise HTTPException(status_code=502, detail=str(e))
    except skills_store.SkillValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"slug": slug, "app_id": resolved_app_id, "stage": req.stage}


@app.get("/api/skills/{slug}")
def get_skill(slug: str, app_id: Optional[str] = Query(default=None)):
    try:
        resolved_app_id = _require_app(app_id) if app_id is not None else None
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
):
    try:
        resolved_app_id = _require_app(app_id) if app_id is not None else None
        skills_store.write_skill(slug, req.content, resolved_app_id)
    except skills_store.SkillValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "slug": slug,
        "content": skills_store.read_skill(slug, resolved_app_id),
    }


@app.post("/api/skills")
def create_skill(
    slug: str,
    req: SkillWriteRequest,
    app_id: Optional[str] = Query(default=None),
):
    try:
        resolved_app_id = _require_app(app_id) if app_id is not None else None
        skills_store.create_skill(slug, req.content, resolved_app_id)
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except skills_store.SkillValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "slug": slug,
        "content": skills_store.read_skill(slug, resolved_app_id),
    }


# ── Persona skill version history ────────────────────────────────────
# Each save of an existing skill keeps the previous text (see
# skills_store.write_skill), so a bad edit can be looked at and undone.
@app.get("/api/skills/{slug}/versions")
def list_skill_versions(slug: str, app_id: Optional[str] = Query(default=None)):
    try:
        resolved_app_id = _require_app(app_id) if app_id is not None else None
        return skills_store.list_versions(slug, resolved_app_id)
    except skills_store.SkillValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/skills/{slug}/versions/{version_id}")
def get_skill_version(
    slug: str,
    version_id: str,
    app_id: Optional[str] = Query(default=None),
):
    try:
        content = skills_store.read_version(
            slug,
            version_id,
            _require_app(app_id) if app_id is not None else None,
        )
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
):
    try:
        resolved_app_id = _require_app(app_id) if app_id is not None else None
        skills_store.restore_version(slug, version_id, resolved_app_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except skills_store.SkillValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "slug": slug,
        "content": skills_store.read_skill(slug, resolved_app_id),
    }


# ── Stack modifiers (read-only list) ─────────────────────────────────
@app.get("/api/modifiers")
def list_modifiers():
    return skills_store.list_modifiers()


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
    JIRA_WEBHOOK_TOKEN: Optional[str] = None
    ADO_WEBHOOK_TOKEN: Optional[str] = None


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