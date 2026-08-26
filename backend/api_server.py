import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from backend import runs_db, skills_store, config_store
from backend.jira_bot import PipelineHalt, run_pipeline

runs_db.init_db()

app = FastAPI(title="Jira SDLC Pipeline API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class RunTicketRequest(BaseModel):
    ticket_key: str


def _execute(run_id: str, ticket_key: str) -> None:
    start = time.monotonic()

    def on_step(step_key: str, state: str, log: str | None = None) -> None:
        runs_db.update_step(run_id, step_key, state, log)

    try:
        result = run_pipeline(ticket_key=ticket_key, on_step=on_step)
        runs_db.finish_run(run_id, "success", result.get("pr_url"), int(time.monotonic() - start))
    except PipelineHalt as e:
        runs_db.finish_run(run_id, e.status, None, int(time.monotonic() - start))
    except Exception:
        print(f"!! Unhandled error running {ticket_key} (run {run_id}):")
        traceback.print_exc()
        runs_db.finish_run(run_id, "error", None, int(time.monotonic() - start))


@app.post("/api/run-ticket")
def run_ticket(req: RunTicketRequest):
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    runs_db.create_run(run_id, req.ticket_key, started_at)
    threading.Thread(target=_execute, args=(run_id, req.ticket_key), daemon=True).start()
    return {"run_id": run_id}


@app.get("/api/run-status/{run_id}")
def run_status(run_id: str):
    run = runs_db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.get("/api/runs")
def list_runs():
    return runs_db.get_runs()


@app.get("/api/stats")
def stats():
    return runs_db.get_stats()


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
