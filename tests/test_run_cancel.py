import threading
import time

import pytest
from conftest import client, sign_in

from backend import api_server, apps_store, runs_db
from backend.jira_bot import PipelineConfig


def _wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def active_run(monkeypatch):
    app = apps_store.create_app("Cancellation")
    sign_in("run-stopper", "admin")
    run_id = "cancellable-run"
    runs_db.create_run(
        run_id,
        "CANCEL-1",
        "2026-10-26T10:00:00+00:00",
        app["id"],
        triggered_by="run-stopper",
    )
    monkeypatch.setattr(api_server, "_run_slots", threading.BoundedSemaphore(1))
    config = PipelineConfig.from_settings({}, app_id=app["id"], app_name=app["name"])
    return run_id, app, config


def test_stop_request_interrupts_pipeline_at_next_stage_checkpoint(
    active_run,
    monkeypatch,
):
    run_id, _app, config = active_run
    stage_started = threading.Event()
    resume_pipeline = threading.Event()
    pipeline_calls = []

    def pipeline(on_step, **_kwargs):
        pipeline_calls.append("started")
        on_step("fetch", "in-progress", "Fetching")
        stage_started.set()
        assert resume_pipeline.wait(5)
        on_step("agent", "in-progress", "Starting implementation")
        raise AssertionError("Cancellation should stop before implementation")

    monkeypatch.setattr(api_server, "run_pipeline", pipeline)
    worker = threading.Thread(
        target=api_server._execute,
        args=(run_id, "CANCEL-1", config, "run-stopper"),
    )
    worker.start()
    assert stage_started.wait(5)

    response = client.post(f"/api/runs/{run_id}/cancel")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelling"

    resume_pipeline.set()
    worker.join(5)

    assert not worker.is_alive()
    assert pipeline_calls == ["started"]
    stopped = runs_db.get_run(run_id)
    assert stopped["status"] == "cancelled"
    assert stopped["steps"][0]["state"] == "cancelled"


def test_queued_run_can_be_cancelled_without_starting_pipeline(active_run, monkeypatch):
    run_id, _app, config = active_run
    semaphore = api_server._run_slots
    assert semaphore.acquire(blocking=False)
    pipeline_calls = []
    monkeypatch.setattr(
        api_server,
        "run_pipeline",
        lambda **_kwargs: pipeline_calls.append("started"),
    )
    worker = threading.Thread(
        target=api_server._execute,
        args=(run_id, "CANCEL-1", config, "run-stopper"),
    )
    worker.start()
    assert _wait_until(
        lambda: any(
            "Queued" in entry
            for entry in runs_db.get_run(run_id)["logs"].get("fetch", [])
        )
    )

    response = client.post(f"/api/runs/{run_id}/cancel")
    assert response.status_code == 200, response.text
    worker.join(5)
    semaphore.release()

    assert not worker.is_alive()
    assert pipeline_calls == []
    assert runs_db.get_run(run_id)["status"] == "cancelled"


def test_run_can_be_cancelled_while_waiting_for_approval(
    active_run,
    monkeypatch,
):
    run_id, _app, config = active_run
    monkeypatch.setattr(api_server, "_APPROVAL_POLL_SEC", 0.02)

    def pipeline(wait_for_approval, **_kwargs):
        wait_for_approval({"modified_files": ["src/app.py"]})

    monkeypatch.setattr(api_server, "run_pipeline", pipeline)
    worker = threading.Thread(
        target=api_server._execute,
        args=(run_id, "CANCEL-1", config, "run-stopper"),
    )
    worker.start()
    assert _wait_until(
        lambda: runs_db.get_run(run_id)["status"] == "awaiting-approval"
    )

    response = client.post(f"/api/runs/{run_id}/cancel")
    assert response.status_code == 200, response.text
    worker.join(5)

    assert not worker.is_alive()
    assert runs_db.get_run(run_id)["status"] == "cancelled"
    assert api_server._run_slots.acquire(blocking=False)
    api_server._run_slots.release()


def test_stop_endpoint_rejects_finished_run(active_run):
    run_id, _app, _config = active_run
    runs_db.finish_run(run_id, "success", None, 1)

    response = client.post(f"/api/runs/{run_id}/cancel")

    assert response.status_code == 409
    assert runs_db.get_run(run_id)["status"] == "success"


def test_restart_recovers_a_pending_stop_as_cancelled(active_run):
    run_id, _app, _config = active_run
    runs_db.update_step(run_id, "fetch", "in-progress", "Fetching ticket")
    assert runs_db.request_cancel(run_id)

    runs_db.init_db()

    stopped = runs_db.get_run(run_id)
    assert stopped["status"] == "cancelled"
    assert stopped["steps"][0]["state"] == "cancelled"
