import base64
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from urllib.parse import urlparse

import requests
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from jira.exceptions import JIRAError
from pydantic import BaseModel, ConfigDict, Field, create_model

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
from backend.jira_bot import (
    PipelineConfig,
    PipelineHalt,
    gh_api,
    protected_path_violations,
    redact,
    run_pipeline,
)
from backend.jira_tool import jira_ticket_matches_jql
from backend.manual_implementation import (
    IMPLEMENTATION_SCORE_THRESHOLD,
    apply_reviewed_files,
    generate_implementation,
    get_review_snapshot,
    validate_and_commit,
)
from backend.manual_pr import create_manual_pull_request
from backend.manual_verification import verify_implementation
from backend.stages.ticket import parse_ticket
from backend.workflow import validate_manual_result


logger = logging.getLogger(__name__)


def _has_complete_implementation_evaluation(evaluation: dict[str, Any]) -> bool:
    checks = evaluation.get("checks")
    if not isinstance(checks, list):
        return False
    expected = {
        "requirements": 25,
        "syntax": 15,
        "static_analysis": 10,
        "scope_safety": 10,
        "robustness": 20,
        "regression": 10,
        "security": 10,
    }
    if len(checks) != len(expected):
        return False
    seen = set()
    score = 0
    scored_weight = 0
    for check in checks:
        if not isinstance(check, dict):
            return False
        key = check.get("key")
        points = check.get("score")
        if (
            not isinstance(key, str)
            or key not in expected
            or key in seen
            or check.get("weight") != expected.get(key)
            or not isinstance(points, int)
            or isinstance(points, bool)
            or not 0 <= points <= expected[key]
            or not isinstance(check.get("critical"), bool)
            or check.get("critical")
            or check.get("status")
            not in {"passed", "warning", "failed", "not_run", "error"}
            or (check.get("status") in {"not_run", "error"} and points != 0)
            or not isinstance(check.get("summary"), str)
        ):
            return False
        seen.add(key)
        score += points
        if check["status"] not in {"not_run", "error"}:
            scored_weight += expected[key]
    return (
        seen == set(expected)
        and scored_weight == evaluation.get("scored_weight")
        and score == evaluation.get("score")
    )


def _has_required_implementation_evidence(evaluation: dict[str, Any]) -> bool:
    checks = evaluation.get("checks")
    if not isinstance(checks, list):
        return False
    check_statuses = {
        check.get("key"): check.get("status")
        for check in checks
        if isinstance(check, dict)
    }
    return all(
        check_statuses.get(key) in {"passed", "warning"}
        for key in (
            "requirements",
            "syntax",
            "static_analysis",
            "scope_safety",
            "robustness",
            "regression",
            "security",
        )
    )


runs_db.init_db()
oauth_store.init_db()
apps_store.init_db()
users_store.init_db()
audit_store.init_db()

GITHUB_OAUTH_CLIENT_ID = os.getenv("GITHUB_OAUTH_CLIENT_ID", "")
GITHUB_OAUTH_CLIENT_SECRET = os.getenv("GITHUB_OAUTH_CLIENT_SECRET", "")
GITHUB_OAUTH_CALLBACK_URL = os.getenv(
    "GITHUB_OAUTH_CALLBACK_URL", "http://localhost:8040/api/auth/github/callback"
)
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
SESSION_COOKIE = "sdlc_session"
ADO_WEBHOOK_USERNAME = "sdlc-agentic"
# How long a run waits for someone to approve its PR before giving up.
APPROVAL_TIMEOUT_SEC = int(os.getenv("APPROVAL_TIMEOUT_HOURS", "24")) * 3600
_APPROVAL_POLL_SEC = 2
# Every run shares one Azure OpenAI per-minute token quota. Two agents at once
# ran it dry on 16 Oct (429s slowed both runs), so runs queue for a slot.
# A run waiting for a human approval gives its slot up while it waits.
MAX_PARALLEL_RUNS = max(1, int(os.getenv("MAX_PARALLEL_RUNS", "1") or 1))
_run_slots = threading.BoundedSemaphore(MAX_PARALLEL_RUNS)

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
    return {
        a["id"]
        for a in apps_store.list_apps()
        if not a["team_id"] or a["team_id"] in mine
    }


def _check_app_access(user: dict, app_id: str) -> None:
    visible = visible_app_ids(user)
    if not apps_store.app_exists(app_id) or (
        visible is not None and app_id not in visible
    ):
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
    mode: Literal["automatic", "manual"] = "automatic"


def _require_app(app_id: Optional[str], user: dict) -> str:
    app_id = app_id or apps_store.DEFAULT_APP_ID
    _check_app_access(user, app_id)
    return app_id


def _pipeline_config(app_id: str) -> PipelineConfig:
    """Resolve pipeline settings for one application."""
    app = apps_store.get_app(app_id)
    try:
        return PipelineConfig.from_settings(
            apps_store.resolve_settings(app_id), app_id=app_id, app_name=app["name"]
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _fetch_manual_ticket_fields(
    ticket_key: str,
    config: PipelineConfig,
) -> dict[str, str]:
    if config.ticket_provider == "ado":
        action_fields = {
            "command": "get_ticket",
            "ticket_key": ticket_key,
            "jql_filter": config.ticket_query,
        }
    else:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", ticket_key):
            raise HTTPException(
                status_code=400,
                detail="Enter a valid Jira issue key, such as PROJECT-123",
            )
        action_fields = {
            "command": "get_ticket",
            "jql_filter": f'key = "{ticket_key}"',
        }

    try:
        ticket = config.ticket_executor()
        result = ticket(config.ticket_action(**action_fields))
    except (JIRAError, requests.RequestException, RuntimeError) as e:
        logger.warning(
            "Could not fetch ticket for manual workflow",
            extra={
                "app_id": config.app_id,
                "provider": config.ticket_provider,
                "error_type": type(e).__name__,
            },
        )
        raise HTTPException(
            status_code=502,
            detail="Could not fetch ticket details from the configured ticket provider",
        ) from e

    if result.is_error:
        logger.warning(
            "Ticket provider returned an error for manual workflow",
            extra={
                "app_id": config.app_id,
                "provider": config.ticket_provider,
                "error_type": "TicketProviderError",
            },
        )
        raise HTTPException(
            status_code=502,
            detail="The configured ticket provider could not fetch this ticket",
        )
    if not result.result:
        raise HTTPException(
            status_code=404,
            detail=f"No ticket found matching {ticket_key}",
        )

    details = parse_ticket(result.text)
    return {"summary": details.summary, "description": details.description}


def _run_manual_ticket_operation(
    run: dict,
    stage_key: str,
    command: str,
    config: PipelineConfig,
    **fields: str,
) -> dict[str, str]:
    messages = {
        "assign": "Could not assign the ticket to the target user",
        "in_progress": "Could not update the ticket status",
        "jira": "Could not update the ticket status",
    }
    try:
        ticket = config.ticket_executor()
        observation = ticket(
            config.ticket_action(
                command=command,
                ticket_key=run["ticket_key"],
                **fields,
            )
        )
    except (JIRAError, requests.RequestException, RuntimeError) as e:
        logger.warning(
            "Manual workflow ticket operation failed",
            extra={
                "run_id": run["id"],
                "app_id": run["app_id"],
                "stage": stage_key,
                "error_type": type(e).__name__,
            },
        )
        run_events.record(
            run["id"],
            run["app_id"],
            run["ticket_key"],
            stage_key,
            "failed",
            messages[stage_key],
        )
        raise HTTPException(status_code=502, detail=messages[stage_key]) from e

    if observation.is_error:
        logger.warning(
            "Ticket provider rejected a manual workflow operation",
            extra={
                "run_id": run["id"],
                "app_id": run["app_id"],
                "stage": stage_key,
                "error_type": "TicketProviderError",
            },
        )
        run_events.record(
            run["id"],
            run["app_id"],
            run["ticket_key"],
            stage_key,
            "failed",
            messages[stage_key],
        )
        raise HTTPException(status_code=502, detail=messages[stage_key])

    parsed = {
        key.lower(): value.strip()
        for key, value in re.findall(
            r"^(Assignee|Status|Operation):\s*(.+)$",
            observation.text,
            flags=re.MULTILINE,
        )
    }
    actual_key = "assignee" if stage_key == "assign" else "status"
    if not parsed.get(actual_key) or not parsed.get("operation"):
        logger.error(
            "Ticket provider returned incomplete operation details",
            extra={
                "run_id": run["id"],
                "app_id": run["app_id"],
                "stage": stage_key,
            },
        )
        run_events.record(
            run["id"],
            run["app_id"],
            run["ticket_key"],
            stage_key,
            "failed",
            "The ticket provider returned incomplete operation details",
        )
        raise HTTPException(
            status_code=502,
            detail="The ticket provider returned incomplete operation details",
        )

    operation = {
        "assigned": "Assigned",
        "already_assigned": "Already assigned",
        "transitioned": "Moved to target status",
        "already_in_target_status": "Already in target status",
    }.get(parsed["operation"], parsed["operation"])
    result = (
        {"actual_assignee": parsed[actual_key], "operation": operation}
        if stage_key == "assign"
        else {"status": parsed[actual_key], "operation": operation}
    )
    run_events.record(
        run["id"],
        run["app_id"],
        run["ticket_key"],
        stage_key,
        "success",
        f"{operation}: {parsed[actual_key]}",
    )
    return result


def _analyze_manual_clarity(
    config: PipelineConfig,
    ticket_key: str,
    values: dict[str, str],
) -> dict:
    from pydantic import SecretStr

    from openhands.sdk import LLM
    from openhands.sdk.llm import Message, TextContent

    llm = LLM(
        model=config.clarity_gate_model,
        api_key=SecretStr(config.azure_key),
        base_url=config.azure_url,
    )
    prompt = f"""You are the SDLC ticket clarity gate.
Assess whether the ticket is sufficiently clear and actionable to implement safely
in the configured repository. Check that the summary and description identify the
requested change, relevant behavior/acceptance criteria, and any important
constraints. Use the supplied ticket/repository context and clarification answers.
Do not invent requirements. If the ticket is already clear, set clear=true.
Otherwise set clear=false and provide specific, answerable questions that would
resolve the ambiguity. Suggest an improved summary and description without
changing the intended requirement.

Ticket ID: {ticket_key}
Target repository: {config.target_repo}
Target branch: {config.target_branch}
Summary: {values.get("summary", "")}
Description: {values.get("description", "")}
Relevant context: {values.get("relevant_context", "")}
Human answers: {values.get("answers", "")}

Return only a JSON object with this exact shape:
{{"clear": true, "reason": "brief analysis", "questions": [],
  "suggested_summary": "summary", "suggested_description": "description"}}
For unclear tickets, clear must be false and questions must contain at least one
specific question. For clear tickets, questions must be an empty array."""
    response = llm.completion(
        messages=[Message(role="user", content=[TextContent(text=prompt)])]
    )
    content = response.message.content or []
    answer = next(
        (item.text.strip() for item in content if isinstance(item, TextContent)),
        "",
    )
    if answer.startswith("```"):
        answer = re.sub(r"^```(?:json)?\s*|\s*```$", "", answer, flags=re.IGNORECASE)
    try:
        analysis = json.loads(answer)
    except json.JSONDecodeError as e:
        raise ValueError("Clarity analysis did not return valid JSON") from e
    if (
        not isinstance(analysis, dict)
        or not isinstance(analysis.get("clear"), bool)
        or not isinstance(analysis.get("reason"), str)
        or not isinstance(analysis.get("questions"), list)
        or any(not isinstance(question, str) for question in analysis["questions"])
    ):
        raise ValueError("Clarity analysis returned an invalid result")
    analysis["questions"] = [
        question.strip() for question in analysis["questions"] if question.strip()
    ]
    if not analysis["clear"] and not analysis["questions"]:
        raise ValueError("Unclear-ticket analysis must include clarification questions")
    for key in ("suggested_summary", "suggested_description"):
        if key not in analysis or not isinstance(analysis[key], str):
            analysis[key] = values.get(
                "summary" if key.endswith("summary") else "description", ""
            )
    return analysis


def _manual_clarity_task_data(
    analysis: dict,
    values: dict[str, str],
    attempts: list[dict],
) -> dict:
    return {
        "instructions": (
            "The clarity gate found unresolved questions. Review the suggested "
            "summary and description; leave them unchanged to accept the proposed "
            "resolution, or edit them. Add relevant ticket or repository context "
            "and answer the questions below. Submitting reruns the gate; the gate, "
            "not a manual pass selection, decides whether the workflow continues."
        ),
        "analysis": analysis["reason"],
        "questions": analysis["questions"],
        "attempts": attempts,
        "initial_values": {
            "summary": analysis["suggested_summary"] or values["summary"],
            "description": analysis["suggested_description"] or values["description"],
            "relevant_context": values["relevant_context"],
            "answers": "",
        },
    }


def _execute(
    run_id: str,
    ticket_key: str,
    config: PipelineConfig,
    user_id: str | None = None,
    notification_settings: dict[str, str] | None = None,
) -> None:
    start = time.monotonic()

    def check_cancelled() -> None:
        run = runs_db.get_run(run_id)
        if run and run["status"] == "cancelling":
            raise PipelineHalt("cancelled", "Run stopped by user")

    def on_step(step_key: str, state: str, log: str | None = None) -> None:
        check_cancelled()
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
        check_cancelled()
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
        # Final messages go to events and outbound notifications — never with a credential in them.
        message = redact(
            message,
            config.github_token,
            config.ado_pat,
            config.jira_api_token,
            config.azure_key,
        )
        status = runs_db.finish_run(
            run_id, status, pr_url, int(time.monotonic() - start)
        )
        if status == "cancelled":
            message = "Run stopped by user"
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

    slot = {"held": False}

    def take_slot(step_key: str) -> None:
        check_cancelled()
        if not _run_slots.acquire(blocking=False):
            on_step(
                step_key,
                "in-progress",
                "Queued — waiting for the run ahead of this one to finish",
            )
            waited = time.monotonic()
            while not _run_slots.acquire(timeout=0.2):
                check_cancelled()
            slot["held"] = True
            check_cancelled()
            on_step(
                step_key,
                "in-progress",
                f"Started after {int(time.monotonic() - waited)}s in the queue",
            )
        else:
            slot["held"] = True
        check_cancelled()

    def give_up_slot() -> None:
        if slot["held"]:
            slot["held"] = False
            _run_slots.release()

    def wait_for_approval(_summary: dict):
        """Park this run as awaiting-approval and block until a maintainer
        decides via POST /api/runs/{id}/approval (or the timeout passes).
        The run's slot is free while it waits, so other runs aren't held up."""
        if not runs_db.request_approval(run_id):
            check_cancelled()
            raise RuntimeError("Run could not enter the approval wait")
        give_up_slot()
        try:
            deadline = time.monotonic() + APPROVAL_TIMEOUT_SEC
            while time.monotonic() < deadline:
                check_cancelled()
                approval = (runs_db.get_run(run_id) or {}).get("approval") or {}
                if approval.get("status") in ("approved", "rejected"):
                    check_cancelled()
                    if not runs_db.resume_approved_run(run_id):
                        check_cancelled()
                        raise RuntimeError("Run could not resume after approval")
                    return (
                        approval["status"] == "approved",
                        approval.get("by") or "",
                        approval.get("note") or "",
                    )
                time.sleep(_APPROVAL_POLL_SEC)
            runs_db.decide_approval(run_id, "expired", "system")
            check_cancelled()
            if not runs_db.resume_approved_run(run_id):
                check_cancelled()
                raise RuntimeError("Run could not resume after approval timeout")
            return None, "", ""
        finally:
            if (runs_db.get_run(run_id) or {}).get("status") != "cancelling":
                take_slot("pr")

    try:
        take_slot("fetch")
        result = run_pipeline(
            ticket_key=ticket_key,
            on_step=on_step,
            on_log=on_log,
            config=config,
            wait_for_approval=wait_for_approval,
            on_usage=on_usage,
        )
        check_cancelled()
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
    finally:
        give_up_slot()


@app.post("/api/run-ticket")
def run_ticket(req: RunTicketRequest, user: dict = runner):
    app_id = _require_app(req.app_id, user)
    config = _pipeline_config(app_id)
    notification_settings = apps_store.resolve_settings(app_id)
    manual_ticket_fields = (
        _fetch_manual_ticket_fields(req.ticket_key, config)
        if req.mode == "manual"
        else None
    )
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    runs_db.create_run(
        run_id,
        req.ticket_key,
        started_at,
        app_id,
        triggered_by=user["login"],
        workflow_mode=req.mode,
    )
    if req.mode == "manual":
        if manual_ticket_fields is None:
            raise RuntimeError("Manual workflow ticket details were not fetched")
        runs_db.set_manual_prefill(run_id, manual_ticket_fields)
        run_events.record(
            run_id,
            app_id,
            req.ticket_key,
            "fetch",
            "in-progress",
            "Ticket details loaded; review or edit before continuing",
            user_id=user["login"],
        )
        return {"run_id": run_id, "app_id": app_id}
    threading.Thread(
        target=_execute,
        args=(run_id, req.ticket_key, config, user["login"], notification_settings),
        daemon=True,
    ).start()
    return {"run_id": run_id, "app_id": app_id}


class ManualStageResultRequest(BaseModel):
    action: Literal[
        "submit",
        "request_changes",
        "apply_edits",
        "accept_implementation",
        "skip_high_confidence_review",
        "accept_verification",
        "request_verification_fixes",
        "flag_verification_concern",
        "submit_manual_verification",
        "retry_verification",
        "retry_pr_creation",
        "retry_ticket_update",
    ] = "submit"
    result: dict[str, str] = Field(default_factory=dict)
    files: dict[str, Optional[str]] | None = None
    feedback: str = ""


def _manual_implementation_event(
    run_id: str,
    config: PipelineConfig,
    state: str,
    message: str,
    user_id: str | None = None,
) -> None:
    runs_db.update_step(
        run_id, "agent", state, redact(message, config.github_token, config.azure_key)
    )
    run_events.record(
        run_id,
        config.app_id,
        (runs_db.get_run(run_id) or {}).get("ticket_key", ""),
        "agent",
        state,
        redact(message, config.github_token, config.azure_key),
        user_id=user_id,
    )


def _run_manual_implementation(run_id: str, actor: str, feedback: str = "") -> None:
    run = runs_db.get_run(run_id)
    if (
        not run
        or run["status"] != "awaiting-human"
        or not run["manual_task"]
        or run["manual_task"]["stage_key"] != "agent"
    ):
        return
    config = _pipeline_config(run["app_id"])
    if not _run_slots.acquire(blocking=False):
        _manual_implementation_event(
            run_id,
            config,
            "in-progress",
            "Queued while another run uses the configured AI capacity",
            actor,
        )
        _run_slots.acquire()
    try:
        _run_manual_implementation_work(run_id, actor, feedback, config)
    finally:
        _run_slots.release()


def _run_manual_implementation_work(
    run_id: str,
    actor: str,
    feedback: str,
    config: PipelineConfig,
) -> None:
    run = runs_db.get_run(run_id)
    if not run:
        return
    task_data = dict(runs_db.get_manual_task_data(run_id))

    def on_agent_event(_stage: str, state: str, message: str | None = None) -> None:
        if message:
            _manual_implementation_event(run_id, config, state, message, actor)

    def on_usage(
        _stage: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float | None,
        request_count: int,
        priced_request_count: int,
    ) -> None:
        runs_db.record_usage(
            run_id,
            prompt_tokens,
            completion_tokens,
            cost_usd,
            request_count,
            priced_request_count,
        )

    def on_checkpoint(checkpoint: dict) -> None:
        runs_db.update_manual_task_data(
            run_id,
            actor,
            "agent",
            checkpoint,
            "Implementation workspace and branch checkpoint saved",
            step_state="in-progress",
            expected_phase="generating",
        )

    try:
        _manual_implementation_event(
            run_id,
            config,
            "in-progress",
            "Loading approved ticket, repository instructions, and target branch",
            actor,
        )
        proposal = generate_implementation(
            run,
            config,
            runs_db.get_manual_results(run_id),
            task_data,
            feedback,
            on_agent_event,
            on_usage,
            on_checkpoint,
        )
        runs_db.update_manual_task_data(
            run_id,
            actor,
            "agent",
            proposal,
            "Coding agent prepared a proposal for human review",
            step_state="human",
            expected_phase="generating",
        )
        _manual_implementation_event(
            run_id,
            config,
            "human",
            "Proposed code is ready for review and editing",
            actor,
        )
    except Exception as exc:
        logger.exception(
            "Manual implementation agent failed",
            extra={"run_id": run_id, "app_id": run["app_id"]},
        )
        task_data.update(
            {
                "phase": "failed",
                "error": redact(str(exc)[:2000], config.github_token, config.azure_key),
            }
        )
        try:
            runs_db.update_manual_task_data(
                run_id,
                actor,
                "agent",
                task_data,
                "Coding agent could not prepare the proposal",
                step_state="human",
                expected_phase="generating",
            )
        except runs_db.ManualStageConflict:
            logger.info(
                "Manual implementation task changed while agent was stopping",
                extra={"run_id": run_id},
            )
        _manual_implementation_event(
            run_id,
            config,
            "human",
            "Coding agent could not prepare the proposal; review the error and retry",
            actor,
        )


def _start_manual_implementation(run_id: str, actor: str, feedback: str = "") -> None:
    threading.Thread(
        target=_run_manual_implementation,
        args=(run_id, actor, feedback),
        daemon=True,
    ).start()


def _validate_manual_implementation(
    run_id: str,
    actor: str,
    task_data: dict,
) -> None:
    run = runs_db.get_run(run_id)
    if (
        not run
        or run["status"] != "awaiting-human"
        or not run["manual_task"]
        or run["manual_task"]["stage_key"] != "agent"
    ):
        return
    config = _pipeline_config(run["app_id"])
    if not _run_slots.acquire(blocking=False):
        _manual_implementation_event(
            run_id,
            config,
            "in-progress",
            "Queued while another run uses the configured validation capacity",
            actor,
        )
        _run_slots.acquire()
    try:
        _validate_manual_implementation_work(
            run_id,
            actor,
            task_data,
            run,
            config,
        )
    finally:
        _run_slots.release()


def _validate_manual_implementation_work(
    run_id: str,
    actor: str,
    task_data: dict,
    run: dict,
    config: PipelineConfig,
) -> None:
    try:
        _manual_implementation_event(
            run_id,
            config,
            "in-progress",
            "Validating the human-approved files and running configured checks",
            actor,
        )
        implementation = validate_and_commit(run_id, config, task_data)
        implementation["approved_by"] = actor
        next_task_data = {
            "phase": "verifying",
            "branch": implementation["branch"],
            "base_sha": implementation["base_sha"],
        }
        updated = runs_db.complete_manual_stage(
            run_id,
            actor,
            "agent",
            implementation,
            next_task_data=next_task_data,
        )
        _manual_implementation_event(
            run_id,
            config,
            "success",
            (
                "High-confidence implementation validated and saved"
                if task_data.get("review_skipped")
                else "Human-reviewed implementation validated and saved"
            ),
            actor,
        )
        run_events.record(
            run_id,
            run["app_id"],
            run["ticket_key"],
            "verify",
            "in-progress",
            "Implementation artifacts and final test results are available for verification",
            user_id=actor,
        )
        audit_store.record(
            actor,
            "run.manual_implementation_approved",
            f"{run['ticket_key']} · agent",
            run["app_id"],
            {
                "run_id": run_id,
                "branch": implementation["branch"],
                "commit_sha": implementation["commit_sha"],
                "changed_files": implementation["changed_files"].splitlines(),
            },
        )
        if updated["manual_task"] is None:
            return
        if updated["manual_task"]["stage_key"] == "verify":
            _start_manual_verification(run_id, actor)
    except Exception as exc:
        logger.exception(
            "Manual implementation validation failed",
            extra={"run_id": run_id, "app_id": run["app_id"]},
        )
        refreshed = runs_db.get_run(run_id)
        if not refreshed or not refreshed.get("manual_task"):
            return
        current_task_data = dict(runs_db.get_manual_task_data(run_id))
        current_task_data.update(
            {
                "phase": "validation_failed",
                "test_output": redact(
                    str(exc)[:6000],
                    config.github_token,
                    config.azure_key,
                ),
            }
        )
        try:
            runs_db.update_manual_task_data(
                run_id,
                actor,
                "agent",
                current_task_data,
                "Validation failed; edit the code or request agent changes",
                step_state="human",
                expected_phase="validating",
            )
        except runs_db.ManualStageConflict:
            logger.info(
                "Manual implementation changed while validation was stopping",
                extra={"run_id": run_id},
            )
        _manual_implementation_event(
            run_id,
            config,
            "human",
            "Validation failed; fix or request changes before continuing",
            actor,
        )


def _start_manual_validation(run_id: str, actor: str, task_data: dict) -> None:
    threading.Thread(
        target=_validate_manual_implementation,
        args=(run_id, actor, task_data),
        daemon=True,
    ).start()


def _manual_verification_event(
    run_id: str,
    config: PipelineConfig,
    state: str,
    message: str,
    user_id: str | None = None,
) -> None:
    safe_message = redact(message, config.github_token, config.azure_key)
    runs_db.update_step(run_id, "verify", state, safe_message)
    run = runs_db.get_run(run_id) or {}
    run_events.record(
        run_id,
        config.app_id,
        run.get("ticket_key", ""),
        "verify",
        state,
        safe_message,
        user_id=user_id,
    )


def _run_manual_verification(run_id: str, actor: str) -> None:
    run = runs_db.get_run(run_id)
    if (
        not run
        or run["status"] != "awaiting-human"
        or not run["manual_task"]
        or run["manual_task"]["stage_key"] != "verify"
    ):
        return
    task_data = dict(runs_db.get_manual_task_data(run_id))
    if task_data.get("phase") != "verifying":
        return
    config = _pipeline_config(run["app_id"])
    if not _run_slots.acquire(blocking=False):
        _manual_verification_event(
            run_id,
            config,
            "in-progress",
            "Queued while another run uses the configured verification capacity",
            actor,
        )
        _run_slots.acquire()
    try:
        results = runs_db.get_manual_results(run_id)
        implementation = results.get("agent", {}).get("result", {})
        gate_result = results.get("gate", {}).get("result", {})
        ticket_result = results.get("fetch", {}).get("result", {})
        requirements = {
            "ticket_key": run["ticket_key"],
            "summary": gate_result.get("summary") or ticket_result.get("summary", ""),
            "description": gate_result.get("description")
            or ticket_result.get("description", ""),
        }

        def on_event(state: str, message: str | None, _unused: None = None) -> None:
            if message:
                _manual_verification_event(run_id, config, state, message, actor)

        def on_usage(
            _stage: str,
            prompt_tokens: int,
            completion_tokens: int,
            cost_usd: float | None,
            request_count: int,
            priced_request_count: int,
        ) -> None:
            runs_db.record_usage(
                run_id,
                prompt_tokens,
                completion_tokens,
                cost_usd,
                request_count,
                priced_request_count,
            )

        _manual_verification_event(
            run_id,
            config,
            "in-progress",
            "Collecting approved implementation artifacts and running independent checks",
            actor,
        )
        report = verify_implementation(
            run_id,
            config,
            requirements,
            implementation,
            on_event,
            on_usage,
        )
        refreshed_task = dict(runs_db.get_manual_task_data(run_id))
        refreshed_task.update({"phase": "review", "report": report, "error": ""})
        runs_db.update_manual_task_data(
            run_id,
            actor,
            "verify",
            refreshed_task,
            "Independent verification report is ready for human review",
            step_state="human",
            expected_phase="verifying",
        )
        _manual_verification_event(
            run_id,
            config,
            "human",
            f"Verification report ready: {report['decision'].upper()}",
            actor,
        )
    except Exception as exc:
        logger.exception(
            "Manual verification failed",
            extra={"run_id": run_id, "app_id": run["app_id"]},
        )
        refreshed_task = dict(runs_db.get_manual_task_data(run_id))
        refreshed_task.update(
            {
                "phase": "failed",
                "error": redact(
                    str(exc)[:6000],
                    config.github_token,
                    config.azure_key,
                    config.ado_pat,
                    config.jira_api_token,
                ),
            }
        )
        try:
            runs_db.update_manual_task_data(
                run_id,
                actor,
                "verify",
                refreshed_task,
                "Independent verification could not complete",
                step_state="human",
                expected_phase="verifying",
            )
        except runs_db.ManualStageConflict:
            logger.info(
                "Manual verification task changed while verifier was stopping",
                extra={"run_id": run_id},
            )
        _manual_verification_event(
            run_id,
            config,
            "human",
            "Verification could not complete; retry or return the work for fixes",
            actor,
        )
    finally:
        _run_slots.release()


def _start_manual_verification(run_id: str, actor: str) -> None:
    threading.Thread(
        target=_run_manual_verification,
        args=(run_id, actor),
        daemon=True,
    ).start()


def _verify_manual_pull_request(
    run_id: str,
    config: PipelineConfig,
    implementation: dict,
    result: dict[str, str],
) -> dict[str, str]:
    parsed = urlparse(result["url"])
    segments = parsed.path.strip("/").split("/")
    expected_repo = config.target_repo.split("/")
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or segments[:2] != expected_repo
        or len(segments) != 4
        or segments[2] != "pull"
        or not segments[3].isdigit()
    ):
        raise ValueError(
            "Pull request URL must be an HTTPS pull request in this application's "
            "configured repository"
        )
    pr_number = segments[3]
    try:
        pull_request = gh_api(
            config.github_token,
            "GET",
            f"/repos/{config.target_repo}/pulls/{pr_number}",
        )
    except RuntimeError as exc:
        logger.warning(
            "Could not verify Manual pull request with the repository provider",
            extra={
                "run_id": run_id,
                "app_id": config.app_id,
                "error_type": type(exc).__name__,
            },
        )
        raise RuntimeError(
            "Could not verify the pull request with the repository provider"
        ) from exc
    if not isinstance(pull_request, dict):
        raise RuntimeError("The repository provider returned an invalid pull request")
    head_data = pull_request.get("head") or {}
    base_data = pull_request.get("base") or {}
    if not isinstance(head_data, dict) or not isinstance(base_data, dict):
        raise RuntimeError(
            "The repository provider returned invalid branch information"
        )
    head_repo = head_data.get("repo") or {}
    base_repo = base_data.get("repo") or {}
    if not isinstance(head_repo, dict) or not isinstance(base_repo, dict):
        raise RuntimeError(
            "The repository provider returned invalid repository information"
        )
    canonical_url = pull_request.get("html_url")
    provider_pr_number = pull_request.get("number")
    if (
        pull_request.get("state") != "open"
        or not isinstance(canonical_url, str)
        or canonical_url.rstrip("/") != result["url"].rstrip("/")
        or isinstance(provider_pr_number, bool)
        or str(provider_pr_number) != pr_number
        or str(head_repo.get("full_name", "")).lower() != config.target_repo.lower()
        or str(base_repo.get("full_name", "")).lower() != config.target_repo.lower()
        or head_data.get("ref") != implementation.get("branch")
        or head_data.get("sha") != implementation.get("commit_sha")
        or base_data.get("ref") != config.target_branch
    ):
        raise ValueError(
            "Pull request must be open and match the approved commit, source branch, "
            "repository, and target branch"
        )
    try:
        pull_files = gh_api(
            config.github_token,
            "GET",
            f"/repos/{config.target_repo}/pulls/{pr_number}/files?per_page=100",
        )
    except RuntimeError as exc:
        logger.warning(
            "Could not verify files in Manual pull request",
            extra={
                "run_id": run_id,
                "app_id": config.app_id,
                "error_type": type(exc).__name__,
            },
        )
        raise RuntimeError("Could not verify the pull request's changed files") from exc
    if (
        not isinstance(pull_files, list)
        or len(pull_files) >= 100
        or any(
            not isinstance(item, dict) or not isinstance(item.get("filename"), str)
            for item in pull_files
        )
    ):
        raise RuntimeError(
            "The repository provider returned invalid pull request files"
        )
    expected_files = set(implementation.get("changed_files", "").splitlines())
    actual_files = {item["filename"] for item in pull_files}
    if not expected_files or actual_files != expected_files:
        raise ValueError(
            "Pull request files do not match the human-approved "
            "implementation artifacts"
        )
    return {
        **result,
        "url": canonical_url,
        "number": str(pull_request.get("number", pr_number)),
        "source_branch": str(head_data.get("ref", "")),
        "target_branch": str(base_data.get("ref", "")),
        "state": str(pull_request.get("state", "")),
        "creation_status": result.get("creation_status", "manual_fallback"),
    }


def _run_manual_pr_creation(run_id: str, actor: str) -> None:
    run = runs_db.get_run(run_id)
    if (
        not run
        or run["status"] != "awaiting-human"
        or not run["manual_task"]
        or run["manual_task"]["stage_key"] != "pr"
        or run["manual_task"].get("phase") != "creating"
    ):
        return
    config: PipelineConfig | None = None
    slot_acquired = False
    try:
        config = _pipeline_config(run["app_id"])
        _run_slots.acquire()
        slot_acquired = True
        results = runs_db.get_manual_results(run_id)
        implementation = results.get("agent", {}).get("result", {})
        verification = results.get("verify", {}).get("result", {})
        ticket = results.get("fetch", {}).get("result", {})
        pr = create_manual_pull_request(
            run_id,
            config,
            run["ticket_key"],
            ticket.get("summary", run["ticket_key"]),
            ticket.get("description", ""),
            implementation,
            verification,
        )
        pr = _verify_manual_pull_request(
            run_id,
            config,
            implementation,
            {
                "url": pr["url"],
                "evidence": pr["evidence"],
                "number": str(pr["number"]),
                "source_branch": pr["source_branch"],
                "target_branch": pr["target_branch"],
                "creation_status": pr["creation_status"],
                "state": pr["state"],
            },
        )
        runs_db.complete_manual_stage(
            run_id,
            "automation",
            "pr",
            pr,
            next_task_data={"phase": "updating"},
        )
        run_events.record(
            run_id,
            run["app_id"],
            run["ticket_key"],
            "pr",
            "success",
            f"Pull request #{pr['number']} created and confirmed open",
        )
        _start_manual_ticket_update(run_id, actor)
    except Exception as exc:
        logger.exception(
            "Manual pull request creation failed",
            extra={"run_id": run_id, "app_id": run["app_id"]},
        )
        try:
            task_data = dict(runs_db.get_manual_task_data(run_id))
            secrets_to_redact = (
                (
                    config.github_token,
                    config.azure_key,
                    config.ado_pat,
                    config.jira_api_token,
                )
                if config
                else ()
            )
            task_data.update(
                {
                    "phase": "failed",
                    "error": redact(
                        str(exc)[:3000],
                        *secrets_to_redact,
                    ),
                }
            )
            runs_db.update_manual_task_data(
                run_id,
                actor,
                "pr",
                task_data,
                (
                    "Pull request creation failed; retry or use the validated "
                    "manual fallback"
                ),
                step_state="human",
                expected_phase="creating",
            )
            run_events.record(
                run_id,
                run["app_id"],
                run["ticket_key"],
                "pr",
                "human",
                (
                    "Pull request creation failed; retry or use the validated "
                    "manual fallback"
                ),
            )
        except runs_db.ManualStageConflict:
            logger.info(
                "Manual pull request task changed while creation was stopping",
                extra={"run_id": run_id},
            )
    finally:
        if slot_acquired:
            _run_slots.release()


def _start_manual_pr_creation(run_id: str, actor: str) -> None:
    threading.Thread(
        target=_run_manual_pr_creation,
        args=(run_id, actor),
        daemon=True,
    ).start()


def _handoff_manual_pr(run_id: str, app_id: str, actor: str) -> None:
    config = _pipeline_config(app_id)
    if config.require_pr_approval:
        if not runs_db.request_manual_pr_approval(run_id):
            raise HTTPException(
                status_code=409,
                detail="Could not pause this Manual run for required PR approval",
            )
    else:
        _start_manual_pr_creation(run_id, actor)


def _manual_ticket_comment(
    run_id: str,
    ticket_key: str,
    ticket_summary: str,
    implementation: dict,
    verification: dict,
    pull_request: dict,
) -> str:
    report = verification.get("report") or {}
    changed_files = implementation.get("changed_files", "")
    return (
        f"Implementation complete for {ticket_key}: {ticket_summary}\n\n"
        f"Implementation summary: {implementation.get('summary', 'Not provided')}\n"
        f"Changed files: {changed_files or 'Not available'}\n"
        f"Verification: {verification.get('decision', 'unknown').upper()}\n"
        f"Verification summary: {report.get('summary') or verification.get('evidence', 'Not provided')}\n"
        f"Verification checks: {verification.get('test_results', 'Not provided')}\n"
        f"Pull request: {pull_request.get('url', 'Not available')}\n"
        f"Source branch: {pull_request.get('source_branch', 'Not available')}\n"
        f"Target branch: {pull_request.get('target_branch', 'Not available')}\n"
        f"[sdlc-agentic-run:{run_id}]"
    )[:10_000]


def _run_manual_ticket_update(run_id: str, actor: str) -> None:
    run = runs_db.get_run(run_id)
    if (
        not run
        or run["status"] != "awaiting-human"
        or not run["manual_task"]
        or run["manual_task"]["stage_key"] != "jira"
        or run["manual_task"].get("phase") != "updating"
    ):
        return
    slot_acquired = False
    config: PipelineConfig | None = None
    try:
        config = _pipeline_config(run["app_id"])
        _run_slots.acquire()
        slot_acquired = True
        results = runs_db.get_manual_results(run_id)
        fetched = results.get("fetch", {}).get("result", {})
        approved = results.get("gate", {}).get("result", fetched)
        implementation = results.get("agent", {}).get("result", {})
        verification = results.get("verify", {}).get("result", {})
        pull_request = results.get("pr", {}).get("result", {})
        if not config.status_done.strip():
            raise RuntimeError(
                "No final ticket status is configured for this application"
            )
        if not pull_request.get("url"):
            raise RuntimeError("The confirmed pull request URL is missing")

        status_result = _run_manual_ticket_operation(
            run,
            "jira",
            "update_status",
            config,
            target_status=config.status_done,
        )
        comment = _manual_ticket_comment(
            run_id,
            run["ticket_key"],
            approved.get("summary", run["ticket_key"]),
            implementation,
            verification,
            pull_request,
        )
        ticket = config.ticket_executor()
        existing_comments = ticket(
            config.ticket_action(
                command="get_comments",
                ticket_key=run["ticket_key"],
            )
        )
        if existing_comments.is_error:
            raise RuntimeError(
                "The ticket provider could not check prior completion comments"
            )
        comment_already_exists = (
            f"[sdlc-agentic-run:{run_id}]" in existing_comments.text
        )
        if not comment_already_exists:
            ticket = config.ticket_executor()
            comment_result = ticket(
                config.ticket_action(
                    command="add_comment",
                    ticket_key=run["ticket_key"],
                    comment_text=comment,
                )
            )
        else:
            comment_result = existing_comments
        if comment_result.is_error:
            raise RuntimeError("The ticket provider rejected the completion comment")

        ticket = config.ticket_executor()
        comments_confirmation = ticket(
            config.ticket_action(
                command="get_comments",
                ticket_key=run["ticket_key"],
            )
        )
        if (
            comments_confirmation.is_error
            or f"[sdlc-agentic-run:{run_id}]" not in comments_confirmation.text
        ):
            raise RuntimeError(
                "The ticket provider did not confirm the completion comment"
            )
        ticket = config.ticket_executor()
        ticket_fields = (
            {"jql_filter": f"key = {run['ticket_key']}"}
            if config.ticket_provider == "jira"
            else {}
        )
        confirmation = ticket(
            config.ticket_action(
                command="get_ticket",
                ticket_key=run["ticket_key"],
                **ticket_fields,
            )
        )
        if confirmation.is_error:
            raise RuntimeError(
                "The ticket provider could not confirm the final ticket state"
            )
        match = re.search(
            r"^Status:\s*(.+)$",
            confirmation.text,
            flags=re.MULTILINE,
        )
        confirmed_status = match.group(1).strip() if match else ""
        if confirmed_status.casefold() != config.status_done.strip().casefold():
            raise RuntimeError(
                "The ticket provider did not confirm the configured final status"
            )
        evidence = (
            f"Ticket {run['ticket_key']} confirmed in status "
            f"'{confirmed_status}'. {status_result['operation']}. "
            "Completion comment was accepted by the ticket provider and includes "
            "the implementation summary, verification outcome, and pull request link."
        )
        update_result = {
            "ticket_key": run["ticket_key"],
            "status": confirmed_status,
            "operation": status_result["operation"],
            "comment_status": (
                "already present" if comment_already_exists else "added"
            ),
            "comment": comment,
            "pr_url": pull_request["url"],
            "evidence": evidence,
        }
        runs_db.complete_manual_stage(
            run_id,
            "automation",
            "jira",
            update_result,
        )
        run_events.record(
            run_id,
            run["app_id"],
            run["ticket_key"],
            "jira",
            "success",
            "Final ticket status and completion comment confirmed by the provider",
        )
        audit_store.record(
            actor,
            "run.manual_ticket_update_completed",
            f"{run['ticket_key']} · jira",
            run["app_id"],
            {"run_id": run_id, "ticket_update": update_result},
        )
    except Exception as exc:
        logger.exception(
            "Manual final ticket update failed",
            extra={"run_id": run_id, "app_id": run["app_id"]},
        )
        try:
            task_data = dict(runs_db.get_manual_task_data(run_id))
            secrets_to_redact = (
                (
                    config.github_token,
                    config.azure_key,
                    config.ado_pat,
                    config.jira_api_token,
                )
                if config
                else ()
            )
            task_data.update(
                {
                    "phase": "failed",
                    "error": redact(str(exc)[:3000], *secrets_to_redact),
                }
            )
            runs_db.update_manual_task_data(
                run_id,
                actor,
                "jira",
                task_data,
                "Final ticket update failed; retry it after resolving the reported issue",
                step_state="human",
                expected_phase="updating",
            )
            run_events.record(
                run_id,
                run["app_id"],
                run["ticket_key"],
                "jira",
                "human",
                "Final ticket update failed; retry after resolving the reported issue",
            )
        except runs_db.ManualStageConflict:
            logger.info(
                "Manual ticket update task changed while the operation was stopping",
                extra={"run_id": run_id},
            )
    finally:
        if slot_acquired:
            _run_slots.release()


def _start_manual_ticket_update(run_id: str, actor: str) -> None:
    threading.Thread(
        target=_run_manual_ticket_update,
        args=(run_id, actor),
        daemon=True,
    ).start()


@app.post("/api/runs/{run_id}/manual-stage")
def complete_manual_stage(
    run_id: str,
    req: ManualStageResultRequest,
    user: dict = runner,
):
    """Record a human-performed stage and expose the next manual task."""
    run = _run_or_404(user, run_id)
    if run["workflow_mode"] != "manual":
        raise HTTPException(status_code=409, detail="This run is not in manual mode")
    if run["status"] != "awaiting-human" or not run["manual_task"]:
        raise HTTPException(
            status_code=409,
            detail="This run is not waiting for a manual stage",
        )

    stage_key = run["manual_task"]["stage_key"]
    if stage_key == "pr":
        task_data = runs_db.get_manual_task_data(run_id)
        phase = task_data.get("phase")
        if req.action == "retry_pr_creation":
            if phase != "failed":
                raise HTTPException(
                    status_code=409,
                    detail="Pull request creation can only be retried after a failure",
                )
            retry_data = {**task_data, "phase": "creating", "error": ""}
            try:
                runs_db.update_manual_task_data(
                    run_id,
                    user["login"],
                    "pr",
                    retry_data,
                    "Human requested a pull request creation retry",
                    step_state="in-progress",
                    expected_phase="failed",
                )
            except runs_db.ManualStageConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            _start_manual_pr_creation(run_id, user["login"])
            return runs_db.get_run(run_id)
        if phase != "failed":
            raise HTTPException(
                status_code=409,
                detail=(
                    "Manual PR fallback is available only after automatic "
                    "creation fails"
                ),
            )

    if stage_key == "jira":
        task_data = runs_db.get_manual_task_data(run_id)
        phase = task_data.get("phase")
        if req.action == "retry_ticket_update":
            if phase != "failed":
                raise HTTPException(
                    status_code=409,
                    detail="Ticket update can only be retried after an execution failure",
                )
            retry_data = {
                **task_data,
                "phase": "updating",
                "error": "",
                "ticket_update": None,
            }
            try:
                runs_db.update_manual_task_data(
                    run_id,
                    user["login"],
                    "jira",
                    retry_data,
                    "Human requested a final ticket update retry",
                    step_state="in-progress",
                    expected_phase="failed",
                )
            except runs_db.ManualStageConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            _start_manual_ticket_update(run_id, user["login"])
            return runs_db.get_run(run_id)
        raise HTTPException(
            status_code=409,
            detail="The run completes automatically after provider confirmation",
        )

    if stage_key == "verify":
        task_data = runs_db.get_manual_task_data(run_id)
        phase = task_data.get("phase")
        if req.action == "retry_verification":
            if phase not in ("failed", "review"):
                raise HTTPException(
                    status_code=409,
                    detail="Verification can only be rerun from a failed or reviewed report",
                )
            retry_data = {
                **task_data,
                "phase": "verifying",
                "report": None,
                "error": "",
            }
            try:
                runs_db.update_manual_task_data(
                    run_id,
                    user["login"],
                    "verify",
                    retry_data,
                    "Human requested verification to run again",
                    step_state="in-progress",
                    expected_phase=phase,
                )
            except runs_db.ManualStageConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            _start_manual_verification(run_id, user["login"])
            return runs_db.get_run(run_id)

        if req.action == "accept_verification":
            report = task_data.get("report", {})
            if phase != "review" or report.get("decision") != "pass":
                raise HTTPException(
                    status_code=409,
                    detail="Only a successful verification report can be accepted",
                )
            config = _pipeline_config(run["app_id"])
            violations = protected_path_violations(
                report.get("changed_files", "").splitlines(),
                config.protected_paths,
            )
            if violations:
                raise HTTPException(
                    status_code=400,
                    detail="Verification cannot pass because protected paths were changed: "
                    + ", ".join(violations),
                )
            try:
                updated = runs_db.complete_manual_stage(
                    run_id,
                    user["login"],
                    "verify",
                    {
                        "decision": "pass",
                        "evidence": report.get("review_report", ""),
                        "reviewer_verdict": report.get("reviewer_verdict", ""),
                        "verification_checks": "\n".join(
                            f"{check.get('name', 'Check')}: "
                            f"{check.get('status', 'unknown')} — "
                            f"{check.get('summary', '')}"
                            for check in report.get("checks", [])
                        ),
                        "test_results": report.get("test_results", ""),
                        "branch": report.get("branch", ""),
                        "changed_files": report.get("changed_files", ""),
                        "report": report,
                    },
                    next_task_data={"phase": "creating"},
                )
            except runs_db.ManualStageConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            _handoff_manual_pr(run_id, run["app_id"], user["login"])
            audit_store.record(
                user["login"],
                "run.manual_verification_accepted",
                f"{run['ticket_key']} · verify",
                run["app_id"],
                {"run_id": run_id, "report": report},
            )
            return runs_db.get_run(run_id)

        if req.action == "submit_manual_verification":
            evidence = req.feedback.strip()
            if phase != "review" or len(evidence) < 30 or len(evidence) > 3000:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Describe the manual checks performed and why the change "
                        "satisfies the requirements (30-3000 characters)"
                    ),
                )
            report = task_data.get("report", {})
            if not report:
                raise HTTPException(
                    status_code=409,
                    detail="An automated verification report is required before manual review",
                )
            config = _pipeline_config(run["app_id"])
            violations = protected_path_violations(
                report.get("changed_files", "").splitlines(),
                config.protected_paths,
            )
            if violations:
                raise HTTPException(
                    status_code=400,
                    detail="Verification cannot pass because protected paths were changed: "
                    + ", ".join(violations),
                )
            manual_report = {
                **report,
                "decision": "pass",
                "reviewer_verdict": report.get("reviewer_verdict", "FAIL"),
                "manual_review": {
                    "decision": "pass",
                    "evidence": evidence,
                    "reviewed_by": user["login"],
                },
            }
            verification_result = {
                "decision": "pass",
                "evidence": (
                    "Human-performed verification:\n"
                    + evidence
                    + "\n\nAutomated report:\n"
                    + report.get("review_report", "")
                ),
                "review_method": "human_after_automated_report",
                "reviewer_verdict": report.get("reviewer_verdict", ""),
                "verification_checks": "\n".join(
                    f"{check.get('name', 'Check')}: "
                    f"{check.get('status', 'unknown')} — "
                    f"{check.get('summary', '')}"
                    for check in report.get("checks", [])
                ),
                "test_results": report.get("test_results", ""),
                "branch": report.get("branch", ""),
                "changed_files": report.get("changed_files", ""),
                "manual_evidence": evidence,
                "report": manual_report,
            }
            try:
                updated = runs_db.complete_manual_stage(
                    run_id,
                    user["login"],
                    "verify",
                    verification_result,
                    next_task_data={"phase": "creating"},
                )
            except runs_db.ManualStageConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            _handoff_manual_pr(run_id, run["app_id"], user["login"])
            audit_store.record(
                user["login"],
                "run.manual_verification_override",
                f"{run['ticket_key']} · verify",
                run["app_id"],
                {
                    "run_id": run_id,
                    "automated_decision": report.get("decision", "unknown"),
                    "automated_reviewer_verdict": report.get(
                        "reviewer_verdict", "unknown"
                    ),
                    "evidence": evidence,
                },
            )
            return runs_db.get_run(run_id)

        if req.action == "flag_verification_concern":
            note = req.feedback.strip()
            if phase not in ("review", "failed") or not note or len(note) > 3000:
                raise HTTPException(
                    status_code=400,
                    detail="Provide a verification concern (up to 3000 characters)",
                )
            try:
                updated = runs_db.complete_manual_stage(
                    run_id,
                    user["login"],
                    "verify",
                    {
                        "decision": "needs_human",
                        "evidence": note,
                        "report": task_data.get("report", {}),
                    },
                )
            except runs_db.ManualStageConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            audit_store.record(
                user["login"],
                "run.manual_verification_concern",
                f"{run['ticket_key']} · verify",
                run["app_id"],
                {"run_id": run_id, "concern": note},
            )
            return updated

        if req.action == "request_verification_fixes":
            feedback = req.feedback.strip()
            if phase != "review" or not feedback or len(feedback) > 3000:
                raise HTTPException(
                    status_code=400,
                    detail="Provide specific implementation fixes (up to 3000 characters)",
                )
            implementation = (
                runs_db.get_manual_results(run_id).get("agent", {}).get("result", {})
            )
            if not implementation:
                raise HTTPException(
                    status_code=409,
                    detail="Saved implementation artifacts are unavailable",
                )
            try:
                config = _pipeline_config(run["app_id"])
                proposal = get_review_snapshot(run_id, config, implementation)
                agent_task = {
                    "phase": "generating",
                    "branch": implementation["branch"],
                    "base_sha": implementation["base_sha"],
                    "summary": implementation.get("summary", ""),
                    "agent_summary": implementation.get("summary", ""),
                    "feedback": feedback,
                    **proposal,
                }
                updated = runs_db.return_manual_stage(
                    run_id,
                    user["login"],
                    "verify",
                    "agent",
                    agent_task,
                    "Human requested implementation fixes from verification findings",
                )
            except (KeyError, RuntimeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except runs_db.ManualStageConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            _start_manual_implementation(run_id, user["login"], feedback)
            audit_store.record(
                user["login"],
                "run.manual_verification_fixes_requested",
                f"{run['ticket_key']} · verify",
                run["app_id"],
                {
                    "run_id": run_id,
                    "feedback": feedback,
                    "verification_report": task_data.get("report", {}),
                },
            )
            return updated

        raise HTTPException(
            status_code=409,
            detail="Choose a verification action: accept, request fixes, or flag a concern",
        )

    if stage_key == "agent":
        task_data = runs_db.get_manual_task_data(run_id)
        phase = task_data.get("phase")
        if req.action == "apply_edits":
            if phase not in ("review", "validation_failed") or req.files is None:
                raise HTTPException(
                    status_code=409,
                    detail="A reviewable proposal and edited files are required to apply edits",
                )
            applying_data = {
                **task_data,
                "phase": "applying",
                "apply_resume_phase": phase,
                "error": "",
            }
            applying_data.pop("implementation_score", None)
            applying_data.pop("implementation_evaluation", None)
            try:
                runs_db.update_manual_task_data(
                    run_id,
                    user["login"],
                    "agent",
                    applying_data,
                    "Applying human edits to the implementation workspace",
                    step_state="in-progress",
                    expected_phase=phase,
                )
            except runs_db.ManualStageConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            try:
                config = _pipeline_config(run["app_id"])
                apply_reviewed_files(run_id, config, task_data, req.files)
                snapshot = get_review_snapshot(run_id, config, task_data)
                applying_data.update(
                    {
                        "phase": phase,
                        "proposed_files": snapshot["proposed_files"],
                        "changed_files": snapshot["changed_files"],
                    }
                )
                applying_data.pop("apply_resume_phase", None)
                updated = runs_db.update_manual_task_data(
                    run_id,
                    user["login"],
                    "agent",
                    applying_data,
                    "Human edits applied to the implementation workspace",
                    step_state="human",
                    expected_phase="applying",
                )
            except (RuntimeError, ValueError) as exc:
                runs_db.update_manual_task_data(
                    run_id,
                    user["login"],
                    "agent",
                    {**task_data, "phase": phase},
                    "Could not apply the human-edited files",
                    step_state="human",
                    expected_phase="applying",
                )
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            audit_store.record(
                user["login"],
                "run.manual_implementation_edits_applied",
                f"{run['ticket_key']} · agent",
                run["app_id"],
                {"run_id": run_id, "changed_files": snapshot["changed_files"]},
            )
            return updated
        if req.action == "submit":
            raise HTTPException(
                status_code=409,
                detail="Implementation is generated automatically; review, edit, and accept the proposal",
            )
        if req.action == "request_changes":
            if phase not in ("review", "validation_failed", "failed"):
                raise HTTPException(
                    status_code=409,
                    detail="The implementation proposal is not ready for changes",
                )
            feedback = req.feedback.strip()
            if not feedback or len(feedback) > 3000:
                raise HTTPException(
                    status_code=400,
                    detail="Provide specific agent feedback (up to 3000 characters)",
                )
            if phase != "failed" and req.files is None:
                raise HTTPException(
                    status_code=400,
                    detail="Submit the reviewed file contents with your change request",
                )
            next_data = {
                **task_data,
                "phase": "generating",
                "feedback": feedback,
                "error": "",
                "test_output": "",
                "test_results": "",
                "proposed_patch": "",
                "proposed_files": [],
                "changed_files": [],
            }
            next_data.pop("implementation_score", None)
            next_data.pop("implementation_evaluation", None)
            try:
                runs_db.update_manual_task_data(
                    run_id,
                    user["login"],
                    "agent",
                    next_data,
                    "Human requested another coding-agent revision",
                    step_state="in-progress",
                    expected_phase=phase,
                )
            except runs_db.ManualStageConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if req.files is not None:
                try:
                    apply_reviewed_files(
                        run_id, _pipeline_config(run["app_id"]), task_data, req.files
                    )
                    refreshed_config = _pipeline_config(run["app_id"])
                    next_data.update(
                        get_review_snapshot(run_id, refreshed_config, task_data)
                    )
                    runs_db.update_manual_task_data(
                        run_id,
                        user["login"],
                        "agent",
                        next_data,
                        "Human-edited files applied before agent revision",
                        step_state="in-progress",
                        expected_phase="generating",
                    )
                except (RuntimeError, ValueError) as exc:
                    restored = {**task_data, "phase": phase}
                    runs_db.update_manual_task_data(
                        run_id,
                        user["login"],
                        "agent",
                        restored,
                        "Could not apply the human-edited files",
                        step_state="human",
                        expected_phase="generating",
                    )
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
            _start_manual_implementation(run_id, user["login"], feedback)
            audit_store.record(
                user["login"],
                "run.manual_implementation_revision",
                f"{run['ticket_key']} · agent",
                run["app_id"],
                {"run_id": run_id, "feedback": feedback},
            )
            return runs_db.get_run(run_id)

        if req.action in ("accept_implementation", "skip_high_confidence_review"):
            skipping_review = req.action == "skip_high_confidence_review"
            if skipping_review:
                evaluation = task_data.get("implementation_evaluation") or {}
                implementation_score = task_data.get("implementation_score")
                if phase != "review":
                    raise HTTPException(
                        status_code=409,
                        detail="Only a new proposal can skip implementation review",
                    )
                if (
                    not isinstance(implementation_score, int)
                    or isinstance(implementation_score, bool)
                    or evaluation.get("version") != 6
                    or evaluation.get("score") != implementation_score
                    or implementation_score < IMPLEMENTATION_SCORE_THRESHOLD
                    or not isinstance(evaluation.get("critical_failures"), list)
                    or evaluation.get("critical_failures")
                    or not _has_complete_implementation_evaluation(evaluation)
                    or not _has_required_implementation_evidence(evaluation)
                ):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Skipping implementation review requires an evidence-based "
                            f"score of at least {IMPLEMENTATION_SCORE_THRESHOLD} with "
                            "no critical failures and complete requirement, syntax, "
                            "static-quality, scope/safety, robustness, regression, "
                            "and security evidence"
                        ),
                    )
                reviewed_files = {
                    item["path"]: None if item.get("deleted") else item["content"]
                    for item in task_data.get("proposed_files", [])
                }
            else:
                if phase not in ("review", "validation_failed") or req.files is None:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "A completed proposal and its edited files are required "
                            "before validation"
                        ),
                    )
                reviewed_files = req.files
            if not reviewed_files:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "A completed proposal with changed files is required "
                        "before validation"
                    ),
                )
            validating_data = {
                **task_data,
                "phase": "validating",
                "approved_by": user["login"],
                "review_skipped": skipping_review,
                "test_output": "",
            }
            original_files = {
                item["path"]: None if item.get("deleted") else item["content"]
                for item in task_data.get("proposed_files", [])
            }
            if not skipping_review and reviewed_files != original_files:
                validating_data.pop("implementation_score", None)
                validating_data.pop("implementation_evaluation", None)
            try:
                runs_db.update_manual_task_data(
                    run_id,
                    user["login"],
                    "agent",
                    validating_data,
                    (
                        "User chose to skip implementation review after a passing "
                        "evidence-based evaluation; "
                        "validating the agent proposal"
                        if skipping_review
                        else "Human accepted the proposal; validating final files"
                    ),
                    step_state="in-progress",
                    expected_phase=phase,
                )
            except runs_db.ManualStageConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            try:
                apply_reviewed_files(
                    run_id,
                    _pipeline_config(run["app_id"]),
                    task_data,
                    reviewed_files,
                )
                validating_data.update(
                    get_review_snapshot(
                        run_id,
                        _pipeline_config(run["app_id"]),
                        task_data,
                    )
                )
                runs_db.update_manual_task_data(
                    run_id,
                    user["login"],
                    "agent",
                    validating_data,
                    (
                        "High-confidence proposal saved for validation without "
                        "manual code review"
                        if skipping_review
                        else "Final human-edited files saved for validation"
                    ),
                    step_state="in-progress",
                    expected_phase="validating",
                )
            except (RuntimeError, ValueError) as exc:
                runs_db.update_manual_task_data(
                    run_id,
                    user["login"],
                    "agent",
                    {**task_data, "phase": phase},
                    "Could not apply the edited files",
                    step_state="human",
                    expected_phase="validating",
                )
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            _start_manual_validation(run_id, user["login"], validating_data)
            return runs_db.get_run(run_id)

    automatic_results: dict[str, dict[str, str]] = {}
    next_task_data = None
    try:
        result = validate_manual_result(stage_key, req.result)
        config = PipelineConfig.from_settings(
            apps_store.resolve_settings(run["app_id"]),
            app_id=run["app_id"],
        )
        if stage_key in ("fetch", "assign"):
            assignment_command = (
                "assign_to_user" if config.target_assignee else "assign_to_self"
            )
            assignment_fields = (
                {"assignee": config.target_assignee} if config.target_assignee else {}
            )
            assignment_result = _run_manual_ticket_operation(
                run,
                "assign",
                assignment_command,
                config,
                **assignment_fields,
            )
            if stage_key == "fetch":
                automatic_results["assign"] = assignment_result
            else:
                result = assignment_result
        if stage_key == "fetch":
            context = (
                f"Ticket: {run['ticket_key']}\n"
                f"Target repository: {config.target_repo}\n"
                f"Target branch: {config.target_branch}"
            )
            gate_values = {
                **result,
                "relevant_context": context,
                "answers": "",
            }
            try:
                analysis = _analyze_manual_clarity(
                    config,
                    run["ticket_key"],
                    gate_values,
                )
            except Exception as e:
                logger.exception(
                    "Manual clarity gate analysis failed",
                    extra={"run_id": run_id, "app_id": run["app_id"]},
                )
                run_events.record(
                    run_id,
                    run["app_id"],
                    run["ticket_key"],
                    "gate",
                    "failed",
                    "The clarity gate could not analyze the ticket",
                )
                raise HTTPException(
                    status_code=502,
                    detail="The clarity gate could not analyze the ticket; retry the stage",
                ) from e
            if analysis["clear"]:
                automatic_results["gate"] = {
                    "decision": "pass",
                    "evidence": analysis["reason"],
                    **gate_values,
                }
                run_events.record(
                    run_id,
                    run["app_id"],
                    run["ticket_key"],
                    "gate",
                    "success",
                    analysis["reason"],
                )
            else:
                next_task_data = _manual_clarity_task_data(
                    analysis,
                    gate_values,
                    [
                        {
                            "actor": user["login"],
                            "input": gate_values,
                            "analysis": analysis["reason"],
                        }
                    ],
                )
                run_events.record(
                    run_id,
                    run["app_id"],
                    run["ticket_key"],
                    "gate",
                    "human",
                    "Clarification needed: " + "; ".join(analysis["questions"]),
                    user_id=user["login"],
                )
        elif stage_key == "gate":
            try:
                analysis = _analyze_manual_clarity(
                    config,
                    run["ticket_key"],
                    result,
                )
            except Exception as e:
                logger.exception(
                    "Manual clarity gate re-analysis failed",
                    extra={"run_id": run_id, "app_id": run["app_id"]},
                )
                run_events.record(
                    run_id,
                    run["app_id"],
                    run["ticket_key"],
                    "gate",
                    "failed",
                    "The clarity gate could not re-analyze the clarification",
                )
                raise HTTPException(
                    status_code=502,
                    detail="The clarity gate could not re-analyze the clarification; retry the stage",
                ) from e
            if not analysis["clear"]:
                attempts = run["manual_task"].get("attempts", [])
                attempts.append(
                    {
                        "actor": user["login"],
                        "input": result,
                        "analysis": analysis["reason"],
                    }
                )
                task_data = _manual_clarity_task_data(
                    analysis,
                    result,
                    attempts,
                )
                updated = runs_db.update_manual_task_data(
                    run_id,
                    user["login"],
                    stage_key,
                    task_data,
                    "The clarity gate still needs clarification",
                )
                run_events.record(
                    run_id,
                    run["app_id"],
                    run["ticket_key"],
                    "gate",
                    "human",
                    "Clarification needed: " + "; ".join(analysis["questions"]),
                    user_id=user["login"],
                )
                audit_store.record(
                    user["login"],
                    "run.manual_clarity",
                    f"{run['ticket_key']} · gate",
                    run["app_id"],
                    {"run_id": run_id, "questions": analysis["questions"]},
                )
                return updated
            result = {
                "decision": "pass",
                "evidence": analysis["reason"],
                "attempts": run["manual_task"].get("attempts", []),
                **result,
            }
            run_events.record(
                run_id,
                run["app_id"],
                run["ticket_key"],
                "gate",
                "success",
                analysis["reason"],
            )
            automatic_results["in_progress"] = _run_manual_ticket_operation(
                run,
                "in_progress",
                "update_status",
                config,
                target_status=config.status_in_progress,
            )
        if stage_key == "fetch" and analysis["clear"]:
            automatic_results["in_progress"] = _run_manual_ticket_operation(
                run,
                "in_progress",
                "update_status",
                config,
                target_status=config.status_in_progress,
            )
        if stage_key in ("fetch", "gate") and not next_task_data:
            next_task_data = {"phase": "generating"}
        if stage_key == "pr":
            next_task_data = {"phase": "updating"}
            prior_results = runs_db.get_manual_results(run_id)
            implementation = prior_results.get("agent", {}).get("result", {})
            try:
                result = _verify_manual_pull_request(
                    run_id,
                    config,
                    implementation,
                    result,
                )
            except (RuntimeError, ValueError) as exc:
                raise ValueError(str(exc)) from exc
        updated = runs_db.complete_manual_stage(
            run_id,
            user["login"],
            stage_key,
            result,
            automatic_results=automatic_results,
            next_task_data=next_task_data,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Run not found") from e
    except runs_db.ManualStageConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    completed_step_state = next(
        step["state"] for step in updated["steps"] if step["key"] == stage_key
    )
    run_events.record(
        run_id,
        run["app_id"],
        run["ticket_key"],
        stage_key,
        completed_step_state,
        f"Manual result submitted by @{user['login']}",
        user_id=user["login"],
    )
    if next_task_data and next_task_data.get("questions"):
        run_events.record(
            run_id,
            run["app_id"],
            run["ticket_key"],
            "gate",
            "human",
            "Clarity gate needs human clarification: "
            + "; ".join(next_task_data["questions"]),
            user_id=user["login"],
        )
    if stage_key == "pr":
        _start_manual_ticket_update(run_id, user["login"])
    audit_store.record(
        user["login"],
        "run.manual_stage",
        f"{run['ticket_key']} · {stage_key}",
        run["app_id"],
        {"run_id": run_id, "stage": stage_key},
    )
    if updated["status"] == "success":
        run_events.record(
            run_id,
            run["app_id"],
            run["ticket_key"],
            "run",
            "success",
            "Manual workflow completed",
            user_id=user["login"],
        )
    if (
        updated.get("manual_task")
        and updated["manual_task"]["stage_key"] == "agent"
        and stage_key != "agent"
    ):
        _start_manual_implementation(run_id, user["login"])
    return updated


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
        raise HTTPException(
            status_code=400, detail="Webhook payload is missing issue.key"
        )
    ticket_key = ticket_key.strip().upper()

    delivery_id = (x_atlassian_webhook_identifier or "").strip() or None
    if delivery_id and len(delivery_id) > 200:
        raise HTTPException(
            status_code=400, detail="Webhook delivery identifier is too long"
        )

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
            extra={
                "app_id": app_id,
                "ticket_key": ticket_key,
                "error_type": type(e).__name__,
            },
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
        decoded_credentials = base64.b64decode(
            encoded_credentials, validate=True
        ).decode("utf-8")
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
        raise HTTPException(
            status_code=400, detail="Webhook payload is missing eventType"
        )
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
        raise HTTPException(
            status_code=400, detail="Invalid webhook delivery identifier"
        )
    delivery_id = str(raw_delivery_id).strip() if raw_delivery_id is not None else None
    if delivery_id == "":
        delivery_id = None
    if delivery_id and len(delivery_id) > 200:
        raise HTTPException(
            status_code=400, detail="Webhook delivery identifier is too long"
        )

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
            extra={
                "app_id": app_id,
                "ticket_key": ticket_key,
                "error_type": type(e).__name__,
            },
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


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str, user: dict = runner):
    run = _run_or_404(user, run_id)
    if not runs_db.request_cancel(run_id):
        raise HTTPException(
            status_code=409,
            detail="This run is no longer active or cannot be cancelled",
        )
    run_events.record(
        run_id,
        run["app_id"],
        run["ticket_key"],
        "run",
        "cancelling",
        f"Stop requested by @{user['login']}",
        user_id=user["login"],
    )
    return runs_db.get_run(run_id)


@app.get("/api/runs")
def list_runs(
    app_id: Optional[str] = Query(default=None),
    mine: bool = Query(default=False),
    user: dict = viewer,
):
    """mine=true -> only runs the signed-in user started, across every application."""
    if app_id:
        _check_app_access(user, app_id)
    return runs_db.get_runs(
        app_id=app_id,
        triggered_by=user["login"] if mine else None,
        app_ids=None if app_id else visible_app_ids(user),
    )


class ApprovalRequest(BaseModel):
    decision: str  # "approve" | "reject"
    note: str = ""


@app.post("/api/runs/{run_id}/approval")
def decide_run_approval(run_id: str, req: ApprovalRequest, user: dict = maintainer):
    """Human sign-off for an app that requires approval before a PR opens."""
    if req.decision not in ("approve", "reject"):
        raise HTTPException(
            status_code=400, detail="decision must be 'approve' or 'reject'"
        )
    run = _run_or_404(user, run_id)
    decision = "approved" if req.decision == "approve" else "rejected"
    note = req.note.strip()[:500]
    if run["workflow_mode"] == "manual":
        updated = runs_db.resolve_manual_pr_approval(
            run_id,
            decision,
            user["login"],
            note,
        )
    else:
        updated = runs_db.decide_approval(
            run_id,
            decision,
            user["login"],
            note,
        )
    if not updated:
        raise HTTPException(
            status_code=409,
            detail="This run isn't waiting for approval (already decided, expired, or finished).",
        )
    audit_store.record(
        user["login"],
        f"run.{req.decision}",
        f"Ticket {run['ticket_key']}",
        run["app_id"],
        {
            "run_id": run_id,
            **({"note": note} if note else {}),
        },
    )
    updated_run = runs_db.get_run(run_id)
    if (
        run["workflow_mode"] == "manual"
        and decision == "approved"
        and updated_run
        and updated_run.get("manual_task", {}).get("phase") == "creating"
    ):
        _start_manual_pr_creation(run_id, user["login"])
    return updated_run


@app.get("/api/stats")
def stats(app_id: Optional[str] = Query(default=None), user: dict = viewer):
    if app_id:
        _check_app_access(user, app_id)
    return runs_db.get_stats(
        app_id=app_id, app_ids=None if app_id else visible_app_ids(user)
    )


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
    **{
        field: (Optional[str], None)
        for field in apps_store.SETTING_FIELDS + apps_store.SECRET_FIELDS
    },
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
    return (
        {k: v for k, v in model.model_dump().items() if v is not None} if model else {}
    )


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
        created = apps_store.create_app(
            req.name,
            description=req.description,
            config=config,
            team_id=req.team_id or None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_store.record(
        user["login"],
        "app.onboard",
        created["name"],
        created["id"],
        {
            "team": created["team"] or "shared",
            "config": _changes({}, config, apps_store.SECRET_FIELDS),
        },
    )
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
            raise HTTPException(
                status_code=403,
                detail="Only admins can change which team owns an application.",
            )
        kwargs["team_id"] = req.team_id or None
    try:
        after = apps_store.update_app(app_id, req.name, req.description, **kwargs)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    fields = ("name", "team", "description")
    changes = audit_store.config_changes(
        {k: before[k] for k in fields}, {k: after[k] for k in fields}, []
    )
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
        settings.get("GITHUB_TOKEN", ""),
        settings.get("TARGET_REPO", ""),
        settings.get("TARGET_BRANCH", ""),
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
        audit_store.record(
            user["login"], "app.config", after["app"]["name"], app_id, changes
        )
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
def list_marketplace_skills(
    app_id: Optional[str] = Query(default=None), user: dict = viewer
):
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
    audit_store.record(
        user["login"], "skill.install", slug, resolved_app_id, {"stage": req.stage}
    )
    return {"slug": slug, "app_id": resolved_app_id, "stage": req.stage}


@app.get("/api/skills/{slug}")
def get_skill(
    slug: str, app_id: Optional[str] = Query(default=None), user: dict = viewer
):
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
        audit_store.record(
            user["login"],
            "skill.update",
            slug,
            resolved_app_id,
            {
                "lines_before": len(before.splitlines()),
                "lines_after": len(req.content.splitlines()),
            },
        )
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
    audit_store.record(
        user["login"],
        "skill.create",
        slug,
        resolved_app_id,
        {"lines": len(req.content.splitlines())},
    )
    return {
        "slug": slug,
        "content": skills_store.read_skill(slug, resolved_app_id),
    }


# ── Persona skill version history ────────────────────────────────────
# Each save of an existing skill keeps the previous text (see
# skills_store.write_skill), so a bad edit can be looked at and undone.
@app.get("/api/skills/{slug}/versions")
def list_skill_versions(
    slug: str, app_id: Optional[str] = Query(default=None), user: dict = viewer
):
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
    audit_store.record(
        user["login"], "skill.restore", slug, resolved_app_id, {"version": version_id}
    )
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
    before = {
        k: v
        for k, v in config_store.get_config().items()
        if k in config_store.NON_SECRET_FIELDS
    }
    try:
        config_store.update_config(updates)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    changes = _changes(before, updates, config_store.SECRET_FIELDS)
    if changes:
        audit_store.record(
            user["login"], "server.config", "Server defaults (.env)", None, changes
        )
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
        audit_store.record(
            user["login"],
            "user.role",
            login,
            None,
            {"role": {"from": before["role"], "to": after["role"]}},
        )
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
    return [
        e
        for e in entries
        if visible is None or not e["app_id"] or e["app_id"] in visible
    ]


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
    audit_store.record(
        user["login"], "team.member_add", team["name"], None, {"member": req.login}
    )
    return team


@app.delete("/api/teams/{team_id}/members/{login}")
def remove_team_member(team_id: str, login: str, user: dict = admin):
    try:
        team = teams_store.get_team(team_id)
    except teams_store.TeamNotFound:
        raise HTTPException(status_code=404, detail="No such team")
    if not teams_store.remove_member(team_id, login):
        raise HTTPException(status_code=404, detail=f"@{login} isn't in {team['name']}")
    audit_store.record(
        user["login"], "team.member_remove", team["name"], None, {"member": login}
    )
    return teams_store.get_team(team_id)


# ── GitHub OAuth login + repo picker ─────────────────────────────────
@app.get("/api/auth/github/login")
def github_login():
    if not GITHUB_OAUTH_CLIENT_ID:
        raise HTTPException(
            status_code=500, detail="GITHUB_OAUTH_CLIENT_ID is not configured"
        )
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
        raise HTTPException(
            status_code=400, detail=f"GitHub OAuth failed: {token_data}"
        )

    user_resp = requests.get(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=15,
    )
    user_data = user_resp.json()
    login = user_data.get("login", "")
    if not login:
        raise HTTPException(status_code=400, detail="GitHub didn't return a username")

    users_store.ensure_user(login, user_data.get("avatar_url"))
    session_id = oauth_store.create_session(
        access_token,
        login,
        user_data.get("avatar_url"),
        request.headers.get("user-agent"),
    )

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
        "login": user["login"],
        "avatar_url": user["avatar_url"],
        "role": user["role"],
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
        if not apps_store.app_exists(updates["default_app_id"]) or (
            visible is not None and updates["default_app_id"] not in visible
        ):
            raise HTTPException(
                status_code=400,
                detail=f"No application named {updates['default_app_id']!r}",
            )
    team = updates.get("active_team_id")
    if team and not (
        teams_store.team_exists(team)
        and (
            users_store.has_role(user, "admin")
            or team in teams_store.teams_of(user["login"])
        )
    ):
        raise HTTPException(
            status_code=400, detail=f"You're not in a team named {team!r}"
        )
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
    return {
        "signed_out": oauth_store.revoke_all(
            user["login"], except_session_id=user["session_id"]
        )
    }


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
            headers={
                "Authorization": f"Bearer {session['access_token']}",
                "Accept": "application/vnd.github+json",
            },
            params={"per_page": 100, "page": page, "sort": "updated"},
            timeout=15,
        )
        if resp.status_code == 401:
            # The stored GitHub token no longer works (revoked on GitHub's side,
            # or the OAuth app's credentials changed since login) — drop the
            # dead session so the frontend re-prompts a fresh login instead of
            # surfacing a raw 500 from GitHub's API.
            oauth_store.delete_session(user["session_id"])
            raise HTTPException(
                status_code=401, detail="GitHub session expired — please log in again."
            )
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
    audit_store.record(
        user["login"],
        "app.repo",
        after["app"]["name"],
        app_id,
        _changes(before, updates, apps_store.SECRET_FIELDS),
    )
    return after
