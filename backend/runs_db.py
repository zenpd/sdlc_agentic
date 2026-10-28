import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from backend.workflow import PIPELINE_STAGES, initial_run_steps, manual_task


DB_PATH = Path(__file__).parent / "runs.db"
_lock = threading.Lock()


class ManualStageConflict(Exception):
    """Raised when a manual task changed before its result was committed."""


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            ticket_key TEXT NOT NULL,
            status TEXT NOT NULL,
            pr_url TEXT,
            started_at TEXT NOT NULL,
            duration_sec INTEGER,
            steps TEXT NOT NULL,
            logs TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS webhook_deliveries (
            app_id TEXT NOT NULL,
            delivery_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            received_at TEXT NOT NULL,
            PRIMARY KEY (app_id, delivery_id)
        )
        """
    )
    # Every run belongs to an onboarded application (apps_store). Runs from
    # before applications existed belong to the "default" one.
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    if "app_id" not in columns:
        conn.execute(
            "ALTER TABLE runs ADD COLUMN app_id TEXT NOT NULL DEFAULT 'default'"
        )
    # Who started the run (the signed-in user, not the agent persona).
    if "triggered_by" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN triggered_by TEXT")
    # Human sign-off before a PR opens (apps with REQUIRE_PR_APPROVAL).
    # approval_status: NULL (not needed) | pending | approved | rejected | expired
    for column in (
        "approval_status",
        "approval_by",
        "approval_note",
        "approval_at",
        "approval_requested_at",
    ):
        if column not in columns:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT")
    if "prompt_tokens" not in columns:
        conn.execute(
            "ALTER TABLE runs ADD COLUMN prompt_tokens INTEGER NOT NULL DEFAULT 0"
        )
    if "completion_tokens" not in columns:
        conn.execute(
            "ALTER TABLE runs ADD COLUMN completion_tokens INTEGER NOT NULL DEFAULT 0"
        )
    if "cost_usd" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN cost_usd REAL")
    if "llm_request_count" not in columns:
        conn.execute(
            "ALTER TABLE runs ADD COLUMN llm_request_count INTEGER NOT NULL DEFAULT 0"
        )
    if "priced_request_count" not in columns:
        conn.execute(
            "ALTER TABLE runs ADD COLUMN priced_request_count INTEGER NOT NULL DEFAULT 0"
        )
    if "cost_status" not in columns:
        conn.execute(
            "ALTER TABLE runs ADD COLUMN cost_status TEXT NOT NULL DEFAULT 'unavailable'"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS run_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL,
            cost_usd REAL,
            request_count INTEGER NOT NULL,
            priced_request_count INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_run_usage_recorded_at
        ON run_usage (recorded_at)
        """
    )
    conn.execute(
        """
        INSERT INTO run_usage (
            run_id, recorded_at, prompt_tokens, completion_tokens, cost_usd,
            request_count, priced_request_count
        )
        SELECT r.id, r.started_at, r.prompt_tokens, r.completion_tokens, r.cost_usd,
               r.llm_request_count, r.priced_request_count
        FROM runs r
        WHERE r.llm_request_count > 0
          AND NOT EXISTS (SELECT 1 FROM run_usage u WHERE u.run_id = r.id)
        """
    )
    if "workflow_mode" not in columns:
        conn.execute(
            "ALTER TABLE runs ADD COLUMN workflow_mode TEXT NOT NULL DEFAULT 'automatic'"
        )
    if "manual_stage" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN manual_stage TEXT")
    if "manual_results" not in columns:
        conn.execute(
            "ALTER TABLE runs ADD COLUMN manual_results TEXT NOT NULL DEFAULT '{}'"
        )
    if "manual_prefill" not in columns:
        conn.execute(
            "ALTER TABLE runs ADD COLUMN manual_prefill TEXT NOT NULL DEFAULT '{}'"
        )
    if "manual_task_data" not in columns:
        conn.execute(
            "ALTER TABLE runs ADD COLUMN manual_task_data TEXT NOT NULL DEFAULT '{}'"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_runs_app_started ON runs (app_id, started_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_runs_triggered_by ON runs (triggered_by, started_at)"
    )
    # A run's pipeline thread doesn't survive a server restart, so anything
    # still "running" or "awaiting approval" from before is dead — say so
    # instead of leaving it spinning (or offering Approve buttons) forever.
    interrupted_cancellations = conn.execute(
        "SELECT id, steps, logs FROM runs WHERE status = 'cancelling'"
    ).fetchall()
    for row in interrupted_cancellations:
        steps = json.loads(row["steps"])
        logs = json.loads(row["logs"])
        for step in steps:
            if step["state"] == "in-progress":
                step["state"] = "cancelled"
                logs.setdefault(step["key"], []).append("Stopped by user")
        conn.execute(
            "UPDATE runs SET steps = ?, logs = ? WHERE id = ?",
            (json.dumps(steps), json.dumps(logs), row["id"]),
        )
    conn.execute(
        "UPDATE runs SET status = 'cancelled' WHERE status = 'cancelling'"
    )
    conn.execute(
        "UPDATE runs SET status = 'error', "
        "approval_status = CASE WHEN approval_status = 'pending' THEN 'expired' ELSE approval_status END "
        "WHERE status IN ('in-progress', 'awaiting-approval') AND workflow_mode != 'manual'"
    )
    interrupted_manual_runs = conn.execute(
        """
        SELECT id, manual_stage, manual_task_data, steps, logs
        FROM runs
        WHERE workflow_mode = 'manual' AND status = 'awaiting-human'
          AND manual_stage IN ('agent', 'verify', 'pr', 'jira')
        """
    ).fetchall()
    for row in interrupted_manual_runs:
        task_data = json.loads(row["manual_task_data"] or "{}")
        phase = task_data.get("phase")
        task_key = row["manual_stage"]
        if task_key == "pr" and phase == "creating":
            task_data.update(
                {
                    "phase": "failed",
                    "error": (
                        "The backend restarted while the pull request was being "
                        "created. Retry creation; it will reuse an existing matching PR."
                    ),
                }
            )
        elif task_key == "jira" and phase == "updating":
            task_data.update(
                {
                    "phase": "failed",
                    "error": (
                        "The backend restarted while updating the ticket. Retry the "
                        "stage; the provider status check prevents a duplicate transition."
                    ),
                }
            )
        elif task_key == "agent" and phase == "applying":
            task_data.update(
                {
                    "phase": task_data.pop("apply_resume_phase", "review"),
                    "error": (
                        "The backend restarted while applying edits. Review the "
                        "proposal and apply your changes again before validation."
                    ),
                }
            )
        elif task_key == "verify" and phase not in (
            "verifying",
            "failed",
            "review",
        ):
            task_data.update(
                {
                    "phase": "failed",
                    "error": (
                        "This verification task predates automated review. "
                        "Retry verification to generate the independent report."
                    ),
                }
            )
        elif phase not in ("generating", "validating", "verifying"):
            continue
        elif phase == "generating":
            task_data.update(
                {
                    "phase": "failed",
                    "error": (
                        "The backend restarted while the coding agent was running. "
                        "Request another agent revision to resume from the saved branch."
                    ),
                }
            )
        elif phase == "validating":
            task_data.update(
                {
                    "phase": "validation_failed",
                    "test_output": (
                        "The backend restarted before validation completed. "
                        "Accept the edited files again to rerun validation."
                    ),
                }
            )
        else:
            task_data.update(
                {
                    "phase": "failed",
                    "error": (
                        "The backend restarted while verification was running. "
                        "Retry verification to resume the review."
                    ),
                }
            )
        steps = json.loads(row["steps"])
        for step in steps:
            if step["key"] == task_key:
                step["state"] = "human"
        logs = json.loads(row["logs"])
        logs.setdefault(task_key, []).append(
            "Interrupted manual work needs human retry after restart"
        )
        conn.execute(
            "UPDATE runs SET manual_task_data = ?, steps = ?, logs = ? WHERE id = ?",
            (json.dumps(task_data), json.dumps(steps), json.dumps(logs), row["id"]),
        )
    conn.commit()
    conn.close()


def _scope(app_id: Optional[str], app_ids: Optional[set] = None) -> tuple[str, tuple]:
    """WHERE-clause fragment limiting a query to one application, to a set of
    applications (what a user's teams can see), or to nothing."""
    if app_id:
        return " AND app_id = ?", (app_id,)
    if app_ids is not None:
        ids = sorted(app_ids)
        if not ids:
            return " AND 0", ()
        return f" AND app_id IN ({','.join('?' * len(ids))})", tuple(ids)
    return "", ()


def create_run(
    run_id: str,
    ticket_key: str,
    started_at: str,
    app_id: str = "default",
    triggered_by: Optional[str] = None,
    workflow_mode: str = "automatic",
) -> None:
    steps = initial_run_steps()
    manual_stage = None
    status = "in-progress"
    if workflow_mode == "manual":
        manual_stage = PIPELINE_STAGES[0].key
        steps[0]["state"] = "in-progress"
        status = "awaiting-human"
    with _lock:
        conn = _get_conn()
        conn.execute(
            """
            INSERT INTO runs (
                id, ticket_key, status, pr_url, started_at, duration_sec,
                steps, logs, app_id, triggered_by, workflow_mode, manual_stage
            )
            """
            "VALUES (?, ?, ?, NULL, ?, NULL, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                ticket_key,
                status,
                started_at,
                json.dumps(steps),
                json.dumps({}),
                app_id,
                triggered_by,
                workflow_mode,
                manual_stage,
            ),
        )
        conn.commit()
        conn.close()


def set_manual_prefill(run_id: str, values: dict[str, str]) -> None:
    """Store fetched ticket fields for the editable first Manual task."""
    with _lock:
        conn = _get_conn()
        try:
            cur = conn.execute(
                """
                UPDATE runs
                SET manual_prefill = ?
                WHERE id = ? AND workflow_mode = 'manual'
                  AND status = 'awaiting-human' AND manual_stage = 'fetch'
                """,
                (json.dumps(values), run_id),
            )
            if cur.rowcount != 1:
                raise ManualStageConflict(
                    "The run is no longer waiting for its ticket details"
                )
            conn.commit()
        finally:
            conn.close()


def complete_manual_stage(
    run_id: str,
    actor: str,
    expected_stage: str,
    result: dict[str, str],
    automatic_results: dict[str, dict[str, str]] | None = None,
    next_task_data: dict | None = None,
) -> dict:
    """Record a manual result and publish the next task atomically."""
    automatic_results = automatic_results or {}
    with _lock:
        conn = _get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["workflow_mode"] != "manual":
                raise ValueError("This run is not in manual mode")
            if row["status"] != "awaiting-human":
                raise ManualStageConflict(
                    "This run is no longer waiting for a manual stage result"
                )
            stage_key = row["manual_stage"]
            if not stage_key:
                raise ManualStageConflict("The run has no pending manual stage")
            if stage_key != expected_stage:
                raise ManualStageConflict(
                    "The pending manual stage changed; refresh the run and try again"
                )

            steps = json.loads(row["steps"])
            logs = json.loads(row["logs"])
            results = json.loads(row["manual_results"] or "{}")
            for step in steps:
                if step["key"] == stage_key:
                    step["state"] = "success"
            completion_message = f"Manual stage completed by @{actor}"
            if actor == "automation":
                completion_message = (
                    result.get("evidence") or "Stage completed automatically"
                )
            logs.setdefault(stage_key, []).append(completion_message)
            results[stage_key] = {
                "actor": actor,
                "result": result,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }

            for auto_stage, auto_result in automatic_results.items():
                if auto_stage == stage_key:
                    raise ValueError(
                        "An automatically completed stage cannot also be submitted manually"
                    )
                for step in steps:
                    if step["key"] == auto_stage:
                        step["state"] = "success"
                logs.setdefault(auto_stage, []).append(
                    auto_result.get("operation", "Completed automatically")
                )
                results[auto_stage] = {
                    "actor": "system",
                    "result": auto_result,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }

            if stage_key in ("gate", "verify") and result.get("decision") in (
                "needs_human",
                "fail",
            ):
                for step in steps:
                    if step["key"] == stage_key:
                        step["state"] = "human"
                status = "human"
                next_stage = None
            else:
                stage_keys = [stage.key for stage in PIPELINE_STAGES]
                next_index = stage_keys.index(stage_key) + 1
                while (
                    next_index < len(stage_keys)
                    and stage_keys[next_index] in automatic_results
                ):
                    next_index += 1
                if next_index < len(stage_keys):
                    next_stage = stage_keys[next_index]
                    for step in steps:
                        if step["key"] == next_stage:
                            running_phase = (next_task_data or {}).get("phase") in {
                                "generating",
                                "validating",
                                "verifying",
                                "creating",
                                "updating",
                            }
                            step["state"] = (
                                "in-progress"
                                if running_phase
                                else "human"
                                if next_task_data
                                else "in-progress"
                            )
                    status = "awaiting-human"
                else:
                    next_stage = None
                    status = "success"

            pr_url = result.get("url") if stage_key == "pr" else row["pr_url"]
            duration_sec = None
            if status in ("success", "human"):
                started = datetime.fromisoformat(row["started_at"])
                duration_sec = max(
                    0,
                    int((datetime.now(timezone.utc) - started).total_seconds()),
                )
            conn.execute(
                """
                UPDATE runs
                SET status = ?, steps = ?, logs = ?, manual_results = ?,
                    manual_stage = ?, manual_task_data = ?, pr_url = ?,
                    duration_sec = COALESCE(?, duration_sec)
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(steps),
                    json.dumps(logs),
                    json.dumps(results),
                    next_stage,
                    json.dumps(next_task_data or {}),
                    pr_url,
                    duration_sec,
                    run_id,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    updated = get_run(run_id)
    if updated is None:
        raise KeyError(run_id)
    return updated


def return_manual_stage(
    run_id: str,
    actor: str,
    expected_stage: str,
    target_stage: str,
    task_data: dict,
    log_message: str,
) -> dict:
    """Return a Manual run to an earlier stage without marking it complete."""
    stage_keys = [stage.key for stage in PIPELINE_STAGES]
    if (
        expected_stage not in stage_keys
        or target_stage not in stage_keys
        or stage_keys.index(target_stage) >= stage_keys.index(expected_stage)
    ):
        raise ValueError("A manual run can only return to an earlier stage")
    with _lock:
        conn = _get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, workflow_mode, manual_stage, steps, logs "
                "FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["workflow_mode"] != "manual" or row["status"] != "awaiting-human":
                raise ManualStageConflict(
                    "The run is no longer waiting for a manual stage"
                )
            if row["manual_stage"] != expected_stage:
                raise ManualStageConflict(
                    "The pending manual stage changed; refresh the run and try again"
                )
            steps = json.loads(row["steps"])
            for step in steps:
                if step["key"] == expected_stage:
                    step["state"] = "in-progress"
                elif step["key"] == target_stage:
                    step["state"] = "in-progress"
            logs = json.loads(row["logs"])
            logs.setdefault(expected_stage, []).append(
                f"@{actor} returned the workflow to {target_stage}: {log_message}"
            )
            conn.execute(
                """
                UPDATE runs
                SET steps = ?, logs = ?, manual_stage = ?, manual_task_data = ?
                WHERE id = ?
                """,
                (
                    json.dumps(steps),
                    json.dumps(logs),
                    target_stage,
                    json.dumps(task_data),
                    run_id,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    updated = get_run(run_id)
    if updated is None:
        raise KeyError(run_id)
    return updated


def update_manual_task_data(
    run_id: str,
    actor: str,
    expected_stage: str,
    task_data: dict,
    log_message: str,
    step_state: str = "human",
    expected_phase: str | None = None,
) -> dict:
    """Persist task data and its visible progress state without advancing the run."""
    with _lock:
        conn = _get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, manual_stage, manual_task_data, steps, logs "
                "FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if (
                row["status"] != "awaiting-human"
                or row["manual_stage"] != expected_stage
            ):
                raise ManualStageConflict(
                    "The pending manual stage changed; refresh the run and try again"
                )
            current_task_data = json.loads(row["manual_task_data"] or "{}")
            if (
                expected_phase is not None
                and current_task_data.get("phase") != expected_phase
            ):
                raise ManualStageConflict(
                    "The implementation task changed; refresh and try again"
                )
            steps = json.loads(row["steps"])
            for step in steps:
                if step["key"] == expected_stage:
                    step["state"] = step_state
            logs = json.loads(row["logs"])
            logs.setdefault(expected_stage, []).append(
                f"Task updated by @{actor}: {log_message}"
            )
            conn.execute(
                "UPDATE runs SET steps = ?, logs = ?, manual_task_data = ? WHERE id = ?",
                (json.dumps(steps), json.dumps(logs), json.dumps(task_data), run_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    updated = get_run(run_id)
    if updated is None:
        raise KeyError(run_id)
    return updated


def create_webhook_run(
    run_id: str,
    ticket_key: str,
    started_at: str,
    app_id: str,
    delivery_id: str | None,
) -> str:
    """Atomically queue a webhook run and claim its delivery ID.

    Returns ``queued``, ``duplicate_delivery``, or ``already_running``.
    """
    with _lock:
        conn = _get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if delivery_id is not None:
                existing = conn.execute(
                    """
                    SELECT 1 FROM webhook_deliveries
                    WHERE app_id = ? AND delivery_id = ?
                    """,
                    (app_id, delivery_id),
                ).fetchone()
                if existing:
                    conn.rollback()
                    return "duplicate_delivery"

            active = conn.execute(
                """
                SELECT id FROM runs
                WHERE app_id = ? AND ticket_key = ? AND status IN ('in-progress', 'awaiting-approval', 'awaiting-human')
                LIMIT 1
                """,
                (app_id, ticket_key),
            ).fetchone()
            if active:
                if delivery_id is not None:
                    conn.execute(
                        """
                        INSERT INTO webhook_deliveries
                            (app_id, delivery_id, run_id, received_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (app_id, delivery_id, active["id"], started_at),
                    )
                conn.commit()
                return "already_running"

            steps = initial_run_steps()
            conn.execute(
                """
                INSERT INTO runs
                    (id, ticket_key, status, pr_url, started_at, duration_sec, steps, logs, app_id)
                VALUES (?, ?, 'in-progress', NULL, ?, NULL, ?, ?, ?)
                """,
                (
                    run_id,
                    ticket_key,
                    started_at,
                    json.dumps(steps),
                    json.dumps({}),
                    app_id,
                ),
            )
            if delivery_id is not None:
                conn.execute(
                    """
                    INSERT INTO webhook_deliveries (app_id, delivery_id, run_id, received_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (app_id, delivery_id, run_id, started_at),
                )
            conn.commit()
            return "queued"
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def update_step(
    run_id: str, step_key: str, state: str, log: Optional[str] = None
) -> None:
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT steps, logs FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        if not row:
            conn.close()
            return
        steps = json.loads(row["steps"])
        for s in steps:
            if s["key"] == step_key:
                s["state"] = state
        logs = json.loads(row["logs"])
        if log:
            logs.setdefault(step_key, []).append(log)
        conn.execute(
            "UPDATE runs SET steps = ?, logs = ? WHERE id = ?",
            (json.dumps(steps), json.dumps(logs), run_id),
        )
        conn.commit()
        conn.close()


def set_status(run_id: str, status: str) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))
        conn.commit()
        conn.close()


def resume_approved_run(run_id: str) -> bool:
    with _lock:
        conn = _get_conn()
        try:
            cur = conn.execute(
                "UPDATE runs SET status = 'in-progress' "
                "WHERE id = ? AND status = 'awaiting-approval'",
                (run_id,),
            )
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()


def request_cancel(run_id: str) -> bool:
    """Request cooperative cancellation of an active automatic or hybrid run."""
    with _lock:
        conn = _get_conn()
        try:
            cur = conn.execute(
                """
                UPDATE runs
                SET status = 'cancelling',
                    approval_status = CASE
                        WHEN approval_status = 'pending' THEN 'expired'
                        ELSE approval_status
                    END
                WHERE id = ?
                  AND workflow_mode IN ('automatic', 'hybrid')
                  AND status IN ('in-progress', 'awaiting-approval')
                """,
                (run_id,),
            )
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()


def request_approval(run_id: str) -> bool:
    """The pipeline is paused before opening a PR until someone decides."""
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "UPDATE runs SET status = 'awaiting-approval', approval_status = 'pending', "
            "approval_by = NULL, approval_note = NULL, approval_at = NULL, approval_requested_at = ? "
            "WHERE id = ? AND status = 'in-progress'",
            (datetime.now(timezone.utc).isoformat(), run_id),
        )
        conn.commit()
        conn.close()
        return cur.rowcount == 1


def request_manual_pr_approval(run_id: str) -> bool:
    """Pause a Manual run at PR handoff until configured sign-off is recorded."""
    with _lock:
        conn = _get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, workflow_mode, manual_stage, steps, logs "
                "FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] != "awaiting-human"
                or row["workflow_mode"] != "manual"
                or row["manual_stage"] != "pr"
            ):
                return False
            steps = json.loads(row["steps"])
            for step in steps:
                if step["key"] == "pr":
                    step["state"] = "in-progress"
            logs = json.loads(row["logs"])
            logs.setdefault("pr", []).append(
                "[APPROVAL] Waiting for a maintainer before PR work can begin"
            )
            task_data = json.loads(
                conn.execute(
                    "SELECT manual_task_data FROM runs WHERE id = ?",
                    (run_id,),
                ).fetchone()["manual_task_data"]
                or "{}"
            )
            task_data["phase"] = "awaiting_approval"
            conn.execute(
                """
                UPDATE runs
                SET status = 'awaiting-approval', approval_status = 'pending',
                    approval_by = NULL, approval_note = NULL, approval_at = NULL,
                    approval_requested_at = ?, steps = ?, logs = ?,
                    manual_task_data = ?
                WHERE id = ?
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(steps),
                    json.dumps(logs),
                    json.dumps(task_data),
                    run_id,
                ),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def resolve_manual_pr_approval(
    run_id: str,
    decision: str,
    actor: str,
    note: str = "",
) -> bool:
    """Resolve PR approval and reveal the Manual PR task only after approval."""
    if decision not in ("approved", "rejected"):
        raise ValueError("decision must be approved or rejected")
    with _lock:
        conn = _get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, workflow_mode, manual_stage, approval_status, "
                "steps, logs, started_at FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] != "awaiting-approval"
                or row["workflow_mode"] != "manual"
                or row["manual_stage"] != "pr"
                or row["approval_status"] != "pending"
            ):
                return False
            status = "awaiting-human" if decision == "approved" else "human"
            task_data = json.loads(
                conn.execute(
                    "SELECT manual_task_data FROM runs WHERE id = ?",
                    (run_id,),
                ).fetchone()["manual_task_data"]
                or "{}"
            )
            if decision == "approved":
                task_data["phase"] = "creating"
            steps = json.loads(row["steps"])
            for step in steps:
                if step["key"] == "pr":
                    step["state"] = "human"
            logs = json.loads(row["logs"])
            logs.setdefault("pr", []).append(
                f"PR approval {decision} by @{actor}" + (f": {note}" if note else "")
            )
            duration_sec = None
            if status == "human":
                started = datetime.fromisoformat(row["started_at"])
                duration_sec = max(
                    0,
                    int((datetime.now(timezone.utc) - started).total_seconds()),
                )
            conn.execute(
                """
                UPDATE runs
                SET status = ?, approval_status = ?, approval_by = ?,
                    approval_note = ?, approval_at = ?, steps = ?, logs = ?,
                    duration_sec = COALESCE(?, duration_sec),
                    manual_task_data = ?
                WHERE id = ?
                """,
                (
                    status,
                    decision,
                    actor,
                    note,
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(steps),
                    json.dumps(logs),
                    duration_sec,
                    json.dumps(task_data),
                    run_id,
                ),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def decide_approval(run_id: str, decision: str, actor: str, note: str = "") -> bool:
    """Record an approve/reject. Only the first decision on a pending request
    counts — returns False if the run isn't waiting for one."""
    if decision not in ("approved", "rejected", "expired"):
        raise ValueError("decision must be approved, rejected or expired")
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "UPDATE runs SET approval_status = ?, approval_by = ?, approval_note = ?, approval_at = ? "
            "WHERE id = ? AND status = 'awaiting-approval' AND approval_status = 'pending'",
            (decision, actor, note, datetime.now(timezone.utc).isoformat(), run_id),
        )
        conn.commit()
        conn.close()
        return cur.rowcount == 1


def finish_run(
    run_id: str, status: str, pr_url: Optional[str], duration_sec: int
) -> str:
    with _lock:
        conn = _get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, steps, logs FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Run {run_id!r} does not exist")
            final_status = "cancelled" if row["status"] == "cancelling" else status
            steps = json.loads(row["steps"])
            logs = json.loads(row["logs"])
            if final_status == "cancelled":
                for step in steps:
                    if step["state"] == "in-progress":
                        step["state"] = "cancelled"
                        logs.setdefault(step["key"], []).append("Stopped by user")
            conn.execute(
                """
                UPDATE runs
                SET status = ?, pr_url = ?, duration_sec = ?, steps = ?, logs = ?,
                    cost_status = CASE WHEN cost_status = 'pending'
                                       THEN 'unavailable' ELSE cost_status END
                WHERE id = ?
                """,
                (
                    final_status,
                    pr_url,
                    duration_sec,
                    json.dumps(steps),
                    json.dumps(logs),
                    run_id,
                ),
            )
            conn.commit()
            return final_status
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def record_usage(
    run_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float | None,
    request_count: int,
    priced_request_count: int,
) -> None:
    """Add one pipeline phase's LiteLLM metrics to its run totals."""
    if min(prompt_tokens, completion_tokens, request_count, priced_request_count) < 0:
        raise ValueError("Usage counters cannot be negative")
    if priced_request_count > request_count:
        raise ValueError("Priced request count cannot exceed request count")
    if cost_usd is not None and cost_usd < 0:
        raise ValueError("Cost cannot be negative")

    with _lock:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT llm_request_count, priced_request_count, cost_usd "
                "FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Run {run_id!r} does not exist")

            requests_total = row["llm_request_count"] + request_count
            priced_total = row["priced_request_count"] + priced_request_count
            if requests_total == 0:
                cost_status = "pending"
            elif priced_total == 0:
                cost_status = "unavailable"
            elif priced_total < requests_total:
                cost_status = "partial"
            else:
                cost_status = "estimated"

            total_cost = row["cost_usd"]
            if cost_usd is not None:
                total_cost = (total_cost or 0.0) + cost_usd

            conn.execute(
                """
                UPDATE runs
                SET prompt_tokens = prompt_tokens + ?,
                    completion_tokens = completion_tokens + ?,
                    cost_usd = ?,
                    llm_request_count = ?,
                    priced_request_count = ?,
                    cost_status = ?
                WHERE id = ?
                """,
                (
                    prompt_tokens,
                    completion_tokens,
                    total_cost,
                    requests_total,
                    priced_total,
                    cost_status,
                    run_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO run_usage (
                    run_id, recorded_at, prompt_tokens, completion_tokens, cost_usd,
                    request_count, priced_request_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    datetime.now(timezone.utc).isoformat(),
                    prompt_tokens,
                    completion_tokens,
                    cost_usd,
                    request_count,
                    priced_request_count,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict:
    mode = row["workflow_mode"] if "workflow_mode" in row.keys() else "automatic"
    manual_stage_key = row["manual_stage"] if "manual_stage" in row.keys() else None
    manual_results = json.loads(row["manual_results"] or "{}")
    manual_prefill = json.loads(row["manual_prefill"] or "{}")
    manual_task_data = json.loads(row["manual_task_data"] or "{}")
    return {
        "id": row["id"],
        "app_id": row["app_id"],
        "triggered_by": row["triggered_by"],
        "ticket_key": row["ticket_key"],
        "status": row["status"],
        "pr_url": row["pr_url"],
        "started_at": row["started_at"],
        "duration_sec": row["duration_sec"],
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "total_tokens": row["prompt_tokens"] + row["completion_tokens"],
        "cost_usd": row["cost_usd"],
        "cost_status": row["cost_status"],
        "llm_request_count": row["llm_request_count"],
        "priced_request_count": row["priced_request_count"],
        "steps": json.loads(row["steps"]),
        "logs": json.loads(row["logs"]),
        "approval": None
        if not row["approval_status"]
        else {
            "status": row["approval_status"],
            "by": row["approval_by"],
            "note": row["approval_note"],
            "at": row["approval_at"],
            "requested_at": row["approval_requested_at"],
        },
        "workflow_mode": mode,
        "manual_task": (
            manual_task(
                manual_stage_key,
                manual_results,
                manual_prefill,
                manual_task_data,
            )
            if manual_stage_key
            else None
        ),
    }


def get_run(run_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def get_manual_results(run_id: str) -> dict[str, dict]:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT manual_results FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise KeyError(run_id)
    return json.loads(row["manual_results"] or "{}")


def get_manual_task_data(run_id: str) -> dict:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT manual_task_data FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise KeyError(run_id)
    return json.loads(row["manual_task_data"] or "{}")


def get_runs(
    limit: int = 50,
    app_id: Optional[str] = None,
    triggered_by: Optional[str] = None,
    app_ids: Optional[set] = None,
) -> list[dict]:
    where, params = _scope(app_id, app_ids)
    if triggered_by:
        where += " AND triggered_by = ?"
        params = (*params, triggered_by)
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT * FROM runs WHERE 1 = 1{where} ORDER BY started_at DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_stats(app_id: Optional[str] = None, app_ids: Optional[set] = None) -> dict:
    where, params = _scope(app_id, app_ids)
    conn = _get_conn()
    total = conn.execute(
        f"SELECT COUNT(*) c FROM runs WHERE 1 = 1{where}", params
    ).fetchone()["c"]
    success = conn.execute(
        f"SELECT COUNT(*) c FROM runs WHERE status = 'success'{where}", params
    ).fetchone()["c"]
    failed = conn.execute(
        f"SELECT COUNT(*) c FROM runs WHERE status IN ('failed', 'error'){where}",
        params,
    ).fetchone()["c"]
    human = conn.execute(
        f"SELECT COUNT(*) c FROM runs WHERE status = 'human'{where}", params
    ).fetchone()["c"]

    success_rate = round((success / total) * 100) if total else 0

    avg_row = conn.execute(
        f"SELECT AVG(duration_sec) a FROM runs WHERE duration_sec IS NOT NULL{where}",
        params,
    ).fetchone()
    avg_duration_sec = int(avg_row["a"]) if avg_row["a"] is not None else None

    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    monthly_usage = conn.execute(
        f"""
        SELECT
          SUM(u.cost_usd) cost,
          SUM(u.prompt_tokens) prompt_tokens,
          SUM(u.completion_tokens) completion_tokens,
          SUM(CASE WHEN u.request_count > 0 THEN 1 ELSE 0 END) usage_runs,
          SUM(CASE WHEN u.request_count > 0
                        AND u.priced_request_count < u.request_count
                   THEN 1 ELSE 0 END) incomplete_runs
        FROM (
          SELECT run_id,
                 SUM(prompt_tokens) prompt_tokens,
                 SUM(completion_tokens) completion_tokens,
                 SUM(cost_usd) cost_usd,
                 SUM(request_count) request_count,
                 SUM(priced_request_count) priced_request_count
          FROM run_usage
          WHERE substr(recorded_at, 1, 7) = ?
          GROUP BY run_id
        ) u
        JOIN runs r ON r.id = u.run_id
        WHERE 1 = 1{where}
        """,
        (current_month, *params),
    ).fetchone()

    daily = []
    today = datetime.now(timezone.utc).date()
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        row = conn.execute(
            f"""
            SELECT
              SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) s,
              SUM(CASE WHEN status IN ('failed', 'error') THEN 1 ELSE 0 END) f
            FROM runs WHERE substr(started_at, 1, 10) = ?{where}
            """,
            (day.isoformat(), *params),
        ).fetchone()
        daily.append(
            {
                "date": day.strftime("%b %d"),
                "success": row["s"] or 0,
                "failed": row["f"] or 0,
            }
        )

    conn.close()
    return {
        "total": total,
        "success": success,
        "failed": failed,
        "human": human,
        "success_rate": success_rate,
        "avg_duration_sec": avg_duration_sec,
        "monthly_cost_usd": monthly_usage["cost"],
        "monthly_prompt_tokens": monthly_usage["prompt_tokens"] or 0,
        "monthly_completion_tokens": monthly_usage["completion_tokens"] or 0,
        "monthly_usage_runs": monthly_usage["usage_runs"] or 0,
        "monthly_incomplete_cost_runs": monthly_usage["incomplete_runs"] or 0,
        "daily": daily,
    }
