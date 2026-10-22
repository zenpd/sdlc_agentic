"""Runs share one Azure OpenAI token quota, so they queue for a slot —
except while a run waits for a human approval, when it gives its slot up."""

import threading
import time

from backend import api_server, runs_db
from backend.jira_bot import PipelineConfig


def _cfg():
    return PipelineConfig.from_settings({}, app_id="default", app_name="Default")


def _wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _start(run_id):
    runs_db.create_run(run_id, run_id.upper(), "2026-10-21T10:00:00+00:00", "default")
    thread = threading.Thread(target=api_server._execute, args=(run_id, run_id.upper(), _cfg()), daemon=True)
    thread.start()
    return thread


def test_second_run_waits_for_the_first(monkeypatch):
    monkeypatch.setattr(api_server, "_run_slots", threading.BoundedSemaphore(1))
    started, release_first = [], threading.Event()

    def pipeline(ticket_key, **_):
        started.append(ticket_key)
        if ticket_key == "R1":
            release_first.wait(5)
        return {"pr_url": None}

    monkeypatch.setattr(api_server, "run_pipeline", pipeline)
    first = _start("r1")
    assert _wait_until(lambda: started == ["R1"])
    second = _start("r2")

    assert _wait_until(lambda: "Queued" in " ".join(runs_db.get_run("r2")["logs"].get("fetch", [])))
    assert started == ["R1"]  # r2 hasn't started while r1 holds the slot

    release_first.set()
    first.join(5); second.join(5)
    assert started == ["R1", "R2"]
    assert any("in the queue" in line for line in runs_db.get_run("r2")["logs"]["fetch"])
    assert runs_db.get_run("r2")["status"] == "success"


def test_a_run_waiting_for_approval_does_not_block_the_queue(monkeypatch):
    monkeypatch.setattr(api_server, "_run_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(api_server, "_APPROVAL_POLL_SEC", 0.02)
    started = []

    def pipeline(ticket_key, wait_for_approval, **_):
        started.append(ticket_key)
        if ticket_key == "R1":
            wait_for_approval({"modified_files": ["src/app.py"]})
        return {"pr_url": None}

    monkeypatch.setattr(api_server, "run_pipeline", pipeline)
    first = _start("r1")
    assert _wait_until(lambda: runs_db.get_run("r1")["status"] == "awaiting-approval")
    assert runs_db.get_run("r1")["approval"]["requested_at"]  # the UI splits out the approval wait from this

    second = _start("r2")  # gets the slot r1 gave up
    second.join(5)
    assert started == ["R1", "R2"] and runs_db.get_run("r2")["status"] == "success"

    runs_db.decide_approval("r1", "approved", "priya")
    first.join(5)
    assert runs_db.get_run("r1")["status"] == "success"
    assert api_server._run_slots.acquire(blocking=False)  # every slot was handed back
