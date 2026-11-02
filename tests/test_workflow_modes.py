import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import client, sign_in

from backend import api_server, apps_store, runs_db
from backend.jira_bot import PipelineConfig
from backend.runs_db import ManualStageConflict


@pytest.fixture
def mock_manual_ticket_fetch(monkeypatch):
    monkeypatch.setattr(
        api_server,
        "_fetch_manual_ticket_fields",
        lambda ticket_key, _config: {
            "summary": f"Fetched ticket {ticket_key}",
            "description": "Fetched ticket description.",
        },
    )


@pytest.fixture(autouse=True)
def mock_manual_clarity(monkeypatch):
    monkeypatch.setattr(
        api_server,
        "_analyze_manual_clarity",
        lambda _config, _ticket_key, _values: {
            "clear": True,
            "reason": "Ticket is clear and actionable.",
            "questions": [],
            "suggested_summary": "",
            "suggested_description": "",
        },
    )


@pytest.fixture
def mock_manual_ticket_operations(monkeypatch):
    calls = []

    def run_operation(run, stage_key, command, config, **fields):
        calls.append((run["ticket_key"], stage_key, command, fields))
        if stage_key == "assign":
            return {
                "actual_assignee": fields.get("assignee", "Integration User"),
                "operation": (
                    "Assigned" if fields.get("assignee") else "Already assigned"
                ),
            }
        return {
            "status": fields["target_status"],
            "operation": "Moved to target status",
        }

    monkeypatch.setattr(api_server, "_run_manual_ticket_operation", run_operation)
    return calls


@pytest.fixture(autouse=True)
def mock_manual_implementation(monkeypatch):
    proposed_files = [
        {
            "path": "src/health.py",
            "content": "def health():\n    return 'ok'\n",
            "deleted": False,
        }
    ]
    reviewed_files = {}

    def publish_proposal(run_id, actor, _feedback=""):
        task_data = runs_db.get_manual_task_data(run_id)
        task_data.update(
            {
                "phase": "review",
                "branch": "feature/manual-test",
                "base_sha": "1" * 40,
                "summary": "Implement the approved ticket.",
                "agent_summary": "Implemented the approved change.",
                "implementation_score": 90,
                "implementation_evaluation": {
                    "version": 6,
                    "score": 90,
                    "scored_weight": 100,
                    "threshold": 85,
                    "checks": [
                        {
                            "key": "requirements",
                            "weight": 25,
                            "score": 25,
                            "critical": False,
                            "status": "passed",
                            "summary": "Covered.",
                        },
                        {
                            "key": "syntax",
                            "weight": 15,
                            "score": 15,
                            "critical": False,
                            "status": "passed",
                            "summary": "Compiled.",
                        },
                        {
                            "key": "static_analysis",
                            "weight": 10,
                            "score": 10,
                            "critical": False,
                            "status": "passed",
                            "summary": "Clean.",
                        },
                        {
                            "key": "scope_safety",
                            "weight": 10,
                            "score": 10,
                            "critical": False,
                            "status": "passed",
                            "summary": "Within approved scope.",
                        },
                        {
                            "key": "robustness",
                            "weight": 20,
                            "score": 20,
                            "critical": False,
                            "status": "passed",
                            "summary": "Strong logic, edge handling, and integration.",
                        },
                        {
                            "key": "regression",
                            "weight": 10,
                            "score": 10,
                            "critical": False,
                            "status": "passed",
                            "summary": "Clean.",
                        },
                        {
                            "key": "security",
                            "weight": 10,
                            "score": 0,
                            "critical": False,
                            "status": "failed",
                            "summary": "Medium finding.",
                        },
                    ],
                    "critical_failures": [],
                    "summary": "Evidence-based score: 90/100.",
                },
                "changed_files": [file["path"] for file in proposed_files],
                "proposed_files": proposed_files,
                "proposed_patch": "diff --git a/src/health.py b/src/health.py\n",
            }
        )
        runs_db.update_manual_task_data(
            run_id,
            actor,
            "agent",
            task_data,
            "Fake coding agent proposal is ready",
            step_state="human",
            expected_phase="generating",
        )

    def apply_files(run_id, _config, _task_data, files):
        reviewed_files[run_id] = files

    def snapshot(run_id, _config, _task_data):
        files = reviewed_files[run_id]
        return {
            "changed_files": list(files),
            "proposed_files": [
                {
                    "path": path,
                    "content": content or "",
                    "deleted": content is None,
                }
                for path, content in files.items()
            ],
            "proposed_patch": "diff --git a/final b/final\n",
        }

    def validate(run_id, actor, task_data):
        implementation = {
            "summary": task_data["agent_summary"],
            "branch": task_data["branch"],
            "base_sha": task_data["base_sha"],
            "commit_sha": "a" * 40,
            "changed_files": "\n".join(task_data["changed_files"]),
            "test_results": "`pytest` passed.",
            "diff": task_data["proposed_patch"],
            "source": "manual_agent",
            "approved_by": actor,
        }
        runs_db.complete_manual_stage(
            run_id,
            actor,
            "agent",
            implementation,
            next_task_data={
                "phase": "verifying",
                "branch": implementation["branch"],
                "base_sha": implementation["base_sha"],
            },
        )
        api_server._start_manual_verification(run_id, actor)

    def publish_verification(run_id, actor):
        task_data = runs_db.get_manual_task_data(run_id)
        implementation = (
            runs_db.get_manual_results(run_id).get("agent", {}).get("result", {})
        )
        task_data.update(
            {
                "phase": "review",
                "report": {
                    "decision": "pass",
                    "reviewer_verdict": "PASS",
                    "summary": "Implementation satisfies the approved requirements.",
                    "branch": implementation.get("branch", "feature/manual-test"),
                    "changed_files": implementation.get(
                        "changed_files", "src/health.py"
                    ),
                    "test_results": "`pytest` passed.",
                    "checks": [
                        {
                            "name": "Tests",
                            "status": "passed",
                            "summary": "Tests passed.",
                            "output": "",
                        }
                    ],
                    "review_report": (
                        "VERDICT: PASS\nSUMMARY\nLooks good.\n"
                        "REQUIREMENT COVERAGE\nAll requirements covered.\n"
                        "FINDINGS\nNone."
                    ),
                },
            }
        )
        runs_db.update_manual_task_data(
            run_id,
            actor,
            "verify",
            task_data,
            "Fake independent verification report is ready",
            step_state="human",
            expected_phase="verifying",
        )

    monkeypatch.setattr(api_server, "_start_manual_implementation", publish_proposal)
    monkeypatch.setattr(api_server, "apply_reviewed_files", apply_files)
    monkeypatch.setattr(api_server, "get_review_snapshot", snapshot)
    monkeypatch.setattr(api_server, "_start_manual_validation", validate)
    monkeypatch.setattr(api_server, "_start_manual_verification", publish_verification)
    monkeypatch.setattr(api_server, "_start_manual_pr_creation", lambda *_args: None)
    monkeypatch.setattr(api_server, "_start_manual_ticket_update", lambda *_args: None)
    return {
        "set_proposed_files": lambda files: proposed_files.__setitem__(
            slice(None), files
        ),
        "reviewed_files": reviewed_files,
    }


def _unexpected_automation(*_args, **_kwargs):
    raise AssertionError("Manual runs must not execute automation")


def test_manual_mode_advances_one_persisted_stage_at_a_time(
    monkeypatch,
    mock_manual_ticket_fetch,
    mock_manual_ticket_operations,
    mock_manual_implementation,
):
    sign_in("admin")
    sign_in("developer", "runner")
    apps_store.update_app_config("default", {"TARGET_ASSIGNEE": "agent@example.com"})
    monkeypatch.setattr(
        api_server,
        "_execute",
        _unexpected_automation,
    )
    monkeypatch.setattr(
        api_server,
        "gh_api",
        lambda _token, _method, path, _json_data=None: (
            {
                "commits": [{"sha": "a" * 40}],
                "files": [{"filename": "src/health.py"}],
            }
            if "/compare/" in path
            else [{"filename": "src/health.py"}]
            if "/files?" in path
            else {
                "state": "open",
                "html_url": "https://github.com/global/repo/pull/42",
                "number": 42,
                "head": {
                    "repo": {"full_name": "global/repo"},
                    "ref": "feature/manual-test",
                    "sha": "a" * 40,
                },
                "base": {
                    "repo": {"full_name": "global/repo"},
                    "ref": "dev",
                },
            }
        ),
    )

    response = client.post(
        "/api/run-ticket",
        json={"ticket_key": "701", "app_id": "default", "mode": "manual"},
    )

    assert response.status_code == 200
    run_id = response.json()["run_id"]
    run = client.get(f"/api/run-status/{run_id}").json()
    assert run["status"] == "awaiting-human"
    assert run["workflow_mode"] == "manual"
    assert run["manual_task"]["stage_key"] == "fetch"
    assert run["manual_task"]["initial_values"] == {
        "summary": "Fetched ticket 701",
        "description": "Fetched ticket description.",
    }

    fetch_result = {
        "summary": "Edited ticket summary",
        "description": "Edited ticket description after reviewing it.",
    }
    expected_pr = {
        "url": "https://github.com/global/repo/pull/42",
        "number": "42",
        "source_branch": "feature/manual-test",
        "target_branch": "dev",
        "creation_status": "created",
        "state": "open",
        "evidence": "Pull request #42 created; source feature/manual-test -> target dev. GitHub confirmed it is open.",
    }
    monkeypatch.setattr(
        api_server,
        "create_manual_pull_request",
        lambda *_args: {**expected_pr, "number": 42},
    )

    assert (
        client.post(
            f"/api/runs/{run_id}/manual-stage",
            json={"result": {"summary": ""}},
        ).status_code
        == 400
    )

    response = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={"result": fetch_result},
    )
    assert response.status_code == 200
    current = client.get(f"/api/run-status/{run_id}").json()
    assert current["manual_task"]["stage_key"] == "agent"
    assert current["manual_task"]["phase"] == "review"
    assert current["manual_task"]["context"]["fetch"] == fetch_result
    accepted = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "action": "accept_implementation",
            "files": {"src/health.py": "def health():\n    return 'reviewed'\n"},
        },
    )
    assert accepted.status_code == 200, accepted.text
    current = client.get(f"/api/run-status/{run_id}").json()
    assert current["manual_task"]["stage_key"] == "verify"
    assert current["manual_task"]["context"]["agent"]["test_results"] == (
        "`pytest` passed."
    )
    assert current["manual_task"]["context"]["agent"]["changed_files"] == (
        "src/health.py"
    )
    assert current["manual_task"]["phase"] == "review"
    assert current["manual_task"]["report"]["decision"] == "pass"

    response = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={"action": "accept_verification"},
    )
    assert response.status_code == 200, response.text
    creating = response.json()
    assert creating["manual_task"]["stage_key"] == "pr"
    assert creating["manual_task"]["phase"] == "creating"
    assert (
        "Tests: passed"
        in creating["manual_task"]["context"]["verify"]["verification_checks"]
    )

    api_server._run_manual_pr_creation(run_id, "developer")
    updated = client.get(f"/api/run-status/{run_id}").json()
    assert updated["status"] == "awaiting-human"
    assert updated["manual_task"]["stage_key"] == "jira"
    assert updated["manual_task"]["phase"] == "updating"
    assert updated["steps"][6]["state"] == "success"
    assert updated["steps"][7]["state"] == "in-progress"
    assert (
        runs_db.get_manual_results(run_id)["pr"]["result"]["url"] == expected_pr["url"]
    )
    assert "GitHub confirmed it is open" in updated["logs"]["pr"][-1]
    assert mock_manual_ticket_operations == [
        ("701", "assign", "assign_to_user", {"assignee": "agent@example.com"}),
        ("701", "in_progress", "update_status", {"target_status": "Active"}),
    ]


def test_manual_code_editor_apply_saves_edits_without_validating(
    mock_manual_ticket_fetch,
    mock_manual_ticket_operations,
    mock_manual_implementation,
):
    sign_in("admin")
    sign_in("developer", "runner")
    run_id = client.post(
        "/api/run-ticket",
        json={"ticket_key": "704", "mode": "manual"},
    ).json()["run_id"]
    fetched = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "result": {
                "summary": "Implement behavior",
                "description": "Approved behavior details.",
            }
        },
    )
    assert fetched.status_code == 200

    edited_code = "def health():\n    return 'applied edit'\n"
    applied = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "action": "apply_edits",
            "files": {"src/health.py": edited_code},
        },
    )
    assert applied.status_code == 200, applied.text
    task = applied.json()["manual_task"]
    assert task["stage_key"] == "agent"
    assert task["phase"] == "review"
    assert task["proposed_files"] == [
        {"path": "src/health.py", "content": edited_code, "deleted": False}
    ]
    assert task["implementation_score"] is None
    assert task["implementation_evaluation"] is None
    assert applied.json()["steps"][4]["state"] == "human"
    assert "agent" not in runs_db.get_manual_results(run_id)
    assert mock_manual_implementation["reviewed_files"][run_id] == {
        "src/health.py": edited_code
    }


def test_review_skip_requires_evidence_score_and_no_critical_failures(
    mock_manual_ticket_fetch,
    mock_manual_ticket_operations,
    mock_manual_implementation,
):
    sign_in("admin")
    sign_in("developer", "runner")
    response = client.post(
        "/api/run-ticket",
        json={"ticket_key": "702", "app_id": "default", "mode": "manual"},
    )
    assert response.status_code == 200
    run_id = response.json()["run_id"]

    fetched = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "result": {
                "summary": "Implement the approved behavior",
                "description": "Detailed approved requirements.",
            }
        },
    )
    assert fetched.status_code == 200
    assert fetched.json()["manual_task"]["stage_key"] == "agent"

    task_data = runs_db.get_manual_task_data(run_id)
    task_data.pop("implementation_score", None)
    task_data.pop("implementation_evaluation", None)
    runs_db.update_manual_task_data(
        run_id,
        "developer",
        "agent",
        task_data,
        "Remove evaluation evidence for negative case",
        expected_phase="review",
    )
    missing_score = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={"action": "skip_high_confidence_review"},
    )
    assert missing_score.status_code == 409
    assert "evidence-based score" in missing_score.json()["detail"]

    task_data["implementation_score"] = 84
    task_data["implementation_evaluation"] = {
        "version": 6,
        "score": 84,
        "scored_weight": 90,
        "critical_failures": [],
        "checks": [
            {
                "key": "requirements",
                "weight": 25,
                "score": 19,
                "critical": False,
                "status": "warning",
                "summary": "Partly covered.",
            },
            {
                "key": "syntax",
                "weight": 15,
                "score": 15,
                "critical": False,
                "status": "passed",
                "summary": "Compiled.",
            },
            {
                "key": "static_analysis",
                "weight": 10,
                "score": 10,
                "critical": False,
                "status": "passed",
                "summary": "Clean.",
            },
            {
                "key": "scope_safety",
                "weight": 10,
                "score": 10,
                "critical": False,
                "status": "passed",
                "summary": "Within approved scope.",
            },
            {
                "key": "robustness",
                "weight": 20,
                "score": 20,
                "critical": False,
                "status": "passed",
                "summary": "Passed.",
            },
            {
                "key": "regression",
                "weight": 10,
                "score": 10,
                "critical": False,
                "status": "passed",
                "summary": "Clean.",
            },
            {
                "key": "security",
                "weight": 10,
                "score": 0,
                "critical": False,
                "status": "not_run",
                "summary": "Scanner unavailable.",
            },
        ],
    }
    runs_db.update_manual_task_data(
        run_id,
        "developer",
        "agent",
        task_data,
        "Test evidence score below threshold",
        expected_phase="review",
    )
    denied = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={"action": "skip_high_confidence_review"},
    )
    assert denied.status_code == 409
    assert "at least 85" in denied.json()["detail"]
    assert (
        client.get(f"/api/run-status/{run_id}").json()["manual_task"]["stage_key"]
        == "agent"
    )

    task_data["implementation_score"] = 59
    task_data["implementation_evaluation"] = {
        "version": 6,
        "score": 59,
        "scored_weight": 60,
        "critical_failures": [],
        "checks": [
            {
                "key": "requirements",
                "weight": 25,
                "score": 25,
                "critical": False,
                "status": "passed",
                "summary": "Covered.",
            },
            {
                "key": "syntax",
                "weight": 15,
                "score": 15,
                "critical": False,
                "status": "passed",
                "summary": "Compiled.",
            },
            {
                "key": "static_analysis",
                "weight": 10,
                "score": 9,
                "critical": False,
                "status": "warning",
                "summary": "One lint issue.",
            },
            {
                "key": "scope_safety",
                "weight": 10,
                "score": 0,
                "critical": False,
                "status": "not_run",
                "summary": "No scope/safety assessment available.",
            },
            {
                "key": "robustness",
                "weight": 20,
                "score": 0,
                "critical": False,
                "status": "not_run",
                "summary": "No robustness assessment available.",
            },
            {
                "key": "regression",
                "weight": 10,
                "score": 10,
                "critical": False,
                "status": "passed",
                "summary": "Clean.",
            },
            {
                "key": "security",
                "weight": 10,
                "score": 0,
                "critical": False,
                "status": "not_run",
                "summary": "Scanner unavailable.",
            },
        ],
    }
    runs_db.update_manual_task_data(
        run_id,
        "developer",
        "agent",
        task_data,
        "Reject score when robustness assessment is unavailable",
        expected_phase="review",
    )
    insufficient_evidence = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={"action": "skip_high_confidence_review"},
    )
    assert insufficient_evidence.status_code == 409
    assert "evidence-based score" in insufficient_evidence.json()["detail"]

    task_data["implementation_score"] = 85
    task_data["implementation_evaluation"] = {
        "version": 6,
        "score": 85,
        "scored_weight": 85,
        "critical_failures": [],
        "checks": [
            {
                "key": "requirements",
                "weight": 25,
                "score": 25,
                "critical": False,
                "status": "passed",
                "summary": "Covered.",
            },
            {
                "key": "syntax",
                "weight": 15,
                "score": 0,
                "critical": False,
                "status": "not_run",
                "summary": "No compatible syntax checker.",
            },
            {
                "key": "static_analysis",
                "weight": 10,
                "score": 10,
                "critical": False,
                "status": "passed",
                "summary": "Static quality assessment complete.",
            },
            {
                "key": "scope_safety",
                "weight": 10,
                "score": 10,
                "critical": False,
                "status": "passed",
                "summary": "Within approved scope.",
            },
            {
                "key": "robustness",
                "weight": 20,
                "score": 20,
                "critical": False,
                "status": "passed",
                "summary": "Robustness assessment complete.",
            },
            {
                "key": "regression",
                "weight": 10,
                "score": 10,
                "critical": False,
                "status": "passed",
                "summary": "Regression assessment complete.",
            },
            {
                "key": "security",
                "weight": 10,
                "score": 10,
                "critical": False,
                "status": "passed",
                "summary": "Security assessment complete.",
            },
        ],
    }
    runs_db.update_manual_task_data(
        run_id,
        "developer",
        "agent",
        task_data,
        "Reject review skip when syntax checker is unavailable",
        expected_phase="review",
    )
    unsupported_syntax_denied = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={"action": "skip_high_confidence_review"},
    )
    assert unsupported_syntax_denied.status_code == 409
    assert "syntax" in unsupported_syntax_denied.json()["detail"]

    task_data["implementation_evaluation"]["checks"][1]["score"] = 15
    task_data["implementation_evaluation"]["checks"][1]["status"] = "passed"
    task_data["implementation_evaluation"]["checks"][3]["score"] = 5
    task_data["implementation_evaluation"]["checks"][3]["status"] = "failed"
    task_data["implementation_score"] = 95
    task_data["implementation_evaluation"]["score"] = 95
    task_data["implementation_evaluation"]["scored_weight"] = 100
    runs_db.update_manual_task_data(
        run_id,
        "developer",
        "agent",
        task_data,
        "Reject review skip when scope/safety has a major finding",
        expected_phase="review",
    )
    scope_safety_denied = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={"action": "skip_high_confidence_review"},
    )
    assert scope_safety_denied.status_code == 409
    assert "scope/safety" in scope_safety_denied.json()["detail"]

    task_data["implementation_evaluation"]["checks"][3]["score"] = 0
    task_data["implementation_evaluation"]["checks"][3]["critical"] = True
    task_data["implementation_evaluation"]["critical_failures"] = ["Scope and safety"]
    task_data["implementation_score"] = 90
    task_data["implementation_evaluation"]["score"] = 90
    runs_db.update_manual_task_data(
        run_id,
        "developer",
        "agent",
        task_data,
        "Reject review skip after critical safety finding",
        expected_phase="review",
    )
    critical_scope_safety_denied = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={"action": "skip_high_confidence_review"},
    )
    assert critical_scope_safety_denied.status_code == 409
    assert "no critical failures" in critical_scope_safety_denied.json()["detail"]

    task_data["implementation_score"] = 85
    task_data["implementation_evaluation"] = {
        "version": 6,
        "score": 85,
        "scored_weight": 100,
        "critical_failures": ["Built-in Python security pattern checks"],
        "checks": [
            {
                "key": "requirements",
                "weight": 25,
                "score": 10,
                "critical": False,
                "status": "passed",
                "summary": "Covered.",
            },
            {
                "key": "syntax",
                "weight": 15,
                "score": 15,
                "critical": False,
                "status": "passed",
                "summary": "Compiled.",
            },
            {
                "key": "static_analysis",
                "weight": 10,
                "score": 10,
                "critical": False,
                "status": "passed",
                "summary": "Clean.",
            },
            {
                "key": "scope_safety",
                "weight": 10,
                "score": 10,
                "critical": False,
                "status": "passed",
                "summary": "Within approved scope.",
            },
            {
                "key": "robustness",
                "weight": 20,
                "score": 20,
                "critical": False,
                "status": "warning",
                "summary": "Some robustness areas are partial.",
            },
            {
                "key": "regression",
                "weight": 10,
                "score": 10,
                "critical": False,
                "status": "passed",
                "summary": "Clean.",
            },
            {
                "key": "security",
                "weight": 10,
                "score": 10,
                "critical": True,
                "status": "failed",
                "summary": "High-confidence unsafe dynamic execution.",
            },
        ],
    }
    runs_db.update_manual_task_data(
        run_id,
        "developer",
        "agent",
        task_data,
        "Test critical security finding blocks review skip",
        expected_phase="review",
    )
    critical_failure_denied = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={"action": "skip_high_confidence_review"},
    )
    assert critical_failure_denied.status_code == 409
    assert "no critical failures" in critical_failure_denied.json()["detail"]

    task_data["implementation_evaluation"]["critical_failures"] = []
    task_data["implementation_evaluation"]["checks"][6]["critical"] = False
    task_data["implementation_evaluation"]["checks"][6]["status"] = "warning"
    runs_db.update_manual_task_data(
        run_id,
        "developer",
        "agent",
        task_data,
        "Test clean score at the 85 threshold",
        expected_phase="review",
    )
    skipped = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={"action": "skip_high_confidence_review"},
    )
    assert skipped.status_code == 200, skipped.text
    assert skipped.json()["manual_task"]["stage_key"] == "verify"
    assert mock_manual_implementation["reviewed_files"][run_id] == {
        "src/health.py": "def health():\n    return 'ok'\n"
    }


def test_manual_mode_keeps_protected_path_guardrail(
    mock_manual_ticket_fetch,
    mock_manual_ticket_operations,
    mock_manual_implementation,
    monkeypatch,
):
    sign_in("admin")
    sign_in("developer", "runner")
    apps_store.update_app_config("default", {"TARGET_ASSIGNEE": "agent@example.com"})
    apps_store.update_app_config("default", {"PROTECTED_PATHS": "ci/*"})
    mock_manual_implementation["set_proposed_files"](
        [{"path": "ci/deploy.yml", "content": "name: deploy\n", "deleted": False}]
    )
    response = client.post(
        "/api/run-ticket",
        json={"ticket_key": "702", "mode": "manual"},
    )
    run_id = response.json()["run_id"]

    client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "result": {
                "summary": "Change CI",
                "description": "Update the CI configuration.",
            }
        },
    )
    response = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "action": "accept_implementation",
            "files": {"ci/deploy.yml": "name: deploy\n"},
        },
    )
    assert response.status_code == 200

    response = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={"action": "accept_verification"},
    )
    assert response.status_code == 400
    assert "protected paths" in response.json()["detail"]
    current = client.get(f"/api/run-status/{run_id}").json()
    assert current["manual_task"]["stage_key"] == "verify"


def test_verification_findings_return_manual_work_to_implementation(
    mock_manual_ticket_fetch,
    mock_manual_ticket_operations,
    mock_manual_implementation,
):
    sign_in("admin")
    sign_in("developer", "runner")
    response = client.post(
        "/api/run-ticket",
        json={"ticket_key": "714", "mode": "manual"},
    )
    run_id = response.json()["run_id"]
    client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "result": {
                "summary": "Implement behavior",
                "description": "Implement the approved behavior safely.",
            }
        },
    )
    client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "action": "accept_implementation",
            "files": {"src/health.py": "def health():\n    return 'reviewed'\n"},
        },
    )
    task_data = runs_db.get_manual_task_data(run_id)
    task_data["report"]["decision"] = "fail"
    runs_db.update_manual_task_data(
        run_id,
        "developer",
        "verify",
        task_data,
        "Simulated independent verification finding",
        expected_phase="review",
    )
    cannot_accept = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={"action": "accept_verification"},
    )
    assert cannot_accept.status_code == 409

    response = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "action": "request_verification_fixes",
            "feedback": "Handle the missing edge case found during verification.",
        },
    )

    assert response.status_code == 200, response.text
    current = client.get(f"/api/run-status/{run_id}").json()
    assert current["status"] == "awaiting-human"
    assert current["manual_task"]["stage_key"] == "agent"
    assert current["manual_task"]["phase"] == "review"
    assert current["manual_task"]["feedback"] == (
        "Handle the missing edge case found during verification."
    )
    assert current["steps"][5]["state"] == "in-progress"


def test_manual_verification_waits_for_configured_pr_approval(
    mock_manual_ticket_fetch,
    mock_manual_ticket_operations,
    mock_manual_implementation,
):
    sign_in("admin")
    sign_in("developer", "runner")
    apps_store.update_app_config("default", {"REQUIRE_PR_APPROVAL": "true"})
    response = client.post(
        "/api/run-ticket",
        json={"ticket_key": "715", "mode": "manual"},
    )
    run_id = response.json()["run_id"]
    client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "result": {
                "summary": "Implement behavior",
                "description": "Implement the approved behavior safely.",
            }
        },
    )
    client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "action": "accept_implementation",
            "files": {"src/health.py": "def health():\n    return 'reviewed'\n"},
        },
    )

    response = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={"action": "accept_verification"},
    )
    assert response.status_code == 200, response.text
    waiting = response.json()
    assert waiting["status"] == "awaiting-approval"
    assert waiting["manual_task"]["stage_key"] == "pr"
    assert waiting["approval"]["status"] == "pending"

    sign_in("admin")
    approved = client.post(
        f"/api/runs/{run_id}/approval",
        json={"decision": "approve", "note": "Verification report reviewed."},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "awaiting-human"
    assert approved.json()["manual_task"]["stage_key"] == "pr"
    assert approved.json()["approval"]["status"] == "approved"


def test_manual_pr_failure_is_retryable_and_fallback_requires_provider_confirmation(
    monkeypatch,
    mock_manual_ticket_fetch,
    mock_manual_ticket_operations,
    mock_manual_implementation,
):
    sign_in("admin")
    sign_in("developer", "runner")
    run_id = client.post(
        "/api/run-ticket",
        json={"ticket_key": "716", "mode": "manual"},
    ).json()["run_id"]
    client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "result": {
                "summary": "Implement behavior",
                "description": "Implement the approved behavior safely.",
            }
        },
    )
    client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "action": "accept_implementation",
            "files": {"src/health.py": "def health():\n    return 'reviewed'\n"},
        },
    )
    client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={"action": "accept_verification"},
    )

    def fail_creation(*_args):
        raise RuntimeError("Provider unavailable")

    monkeypatch.setattr(api_server, "create_manual_pull_request", fail_creation)
    api_server._run_manual_pr_creation(run_id, "developer")
    failed = client.get(f"/api/run-status/{run_id}").json()
    assert failed["manual_task"]["phase"] == "failed"
    assert "Provider unavailable" in failed["manual_task"]["error"]
    assert failed["steps"][6]["state"] == "human"

    retried = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={"action": "retry_pr_creation"},
    )
    assert retried.status_code == 200
    assert retried.json()["manual_task"]["phase"] == "creating"
    assert retried.json()["steps"][6]["state"] == "in-progress"

    task_data = runs_db.get_manual_task_data(run_id)
    task_data.update({"phase": "failed", "error": "Provider unavailable"})
    runs_db.update_manual_task_data(
        run_id,
        "developer",
        "pr",
        task_data,
        "Simulated second PR creation failure",
        expected_phase="creating",
    )
    monkeypatch.setattr(
        api_server,
        "gh_api",
        lambda _token, _method, path, _payload=None: (
            [{"filename": "src/health.py"}]
            if "/files?" in path
            else {
                "state": "open",
                "html_url": "https://github.com/global/repo/pull/43",
                "number": 43,
                "head": {
                    "repo": {"full_name": "global/repo"},
                    "ref": "feature/manual-test",
                    "sha": "a" * 40,
                },
                "base": {
                    "repo": {"full_name": "global/repo"},
                    "ref": "dev",
                },
            }
        ),
    )
    fallback = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "result": {
                "url": "https://github.com/global/repo/pull/43",
                "evidence": "Created manually after automatic creation failed.",
            }
        },
    )
    assert fallback.status_code == 200, fallback.text
    assert fallback.json()["manual_task"]["stage_key"] == "jira"
    assert fallback.json()["manual_task"]["phase"] == "updating"
    assert fallback.json()["manual_task"]["context"]["pr"]["number"] == "43"
    assert fallback.json()["pr_url"] == "https://github.com/global/repo/pull/43"


def test_manual_ticket_update_collects_prior_results_and_finishes_automatically(
    monkeypatch,
    mock_manual_ticket_operations,
):
    sign_in("admin")
    sign_in("developer", "runner")
    run_id = str(uuid4())
    runs_db.create_run(
        run_id,
        "717",
        "2026-10-26T00:00:00+00:00",
        workflow_mode="manual",
    )
    pr_url = "https://github.com/global/repo/pull/44"
    prior_results = {
        "fetch": {
            "result": {
                "summary": "Fetched summary",
                "description": "Fetched description.",
            }
        },
        "gate": {
            "result": {
                "summary": "Approved summary",
                "description": "Approved requirements.",
            }
        },
        "agent": {
            "result": {
                "summary": "Implemented approved behavior.",
                "changed_files": "src/health.py",
                "test_results": "Implementation tests passed.",
            }
        },
        "verify": {
            "result": {
                "decision": "pass",
                "evidence": "Independent review passed.",
                "test_results": "Verification tests passed.",
                "report": {"summary": "Requirements satisfied."},
            }
        },
        "pr": {
            "result": {
                "url": pr_url,
                "source_branch": "feature/717",
                "target_branch": "main",
            }
        },
    }
    conn = runs_db._get_conn()
    conn.execute(
        """
        UPDATE runs
        SET manual_stage = 'jira', status = 'awaiting-human',
            manual_results = ?, manual_task_data = ?, pr_url = ?
        WHERE id = ?
        """,
        (
            json.dumps(prior_results),
            json.dumps({"phase": "updating"}),
            pr_url,
            run_id,
        ),
    )
    conn.commit()
    conn.close()

    comments = []

    class TicketResult:
        is_error = False

        def __init__(self, text=""):
            self.text = text

    def ticket_executor(_self):
        def execute(action):
            if action.command == "get_comments":
                return TicketResult("\n".join(comments))
            if action.command == "add_comment":
                comments.append(action.comment_text)
                return TicketResult("Comment added")
            if action.command == "get_ticket":
                return TicketResult("Key: 717\nStatus: Closed")
            raise AssertionError(f"Unexpected ticket command: {action.command}")

        return execute

    monkeypatch.setattr(PipelineConfig, "ticket_executor", ticket_executor)
    api_server._run_manual_ticket_update(run_id, "developer")

    completed = client.get(f"/api/run-status/{run_id}").json()
    update = runs_db.get_manual_results(run_id)["jira"]["result"]
    assert completed["status"] == "success"
    assert completed["manual_task"] is None
    assert "Ticket 717 confirmed in status 'Closed'" in completed["logs"]["jira"][-1]
    assert update["status"] == "Closed"
    assert update["comment_status"] == "added"
    assert update["pr_url"] == pr_url
    assert "Approved summary" in update["comment"]
    assert "Implemented approved behavior." in update["comment"]
    assert "Requirements satisfied." in update["comment"]
    assert pr_url in update["comment"]
    assert f"[sdlc-agentic-run:{run_id}]" in update["comment"]
    assert len(comments) == 1
    assert ("717", "jira", "update_status", {"target_status": "Closed"}) in (
        mock_manual_ticket_operations
    )

    api_server._run_manual_ticket_update(run_id, "developer")
    retried = client.get(f"/api/run-status/{run_id}").json()
    assert retried["status"] == "success"
    assert len(comments) == 1
    assert retried["pr_url"] == pr_url


def test_manual_verification_concern_stops_the_run_with_a_recorded_note(
    mock_manual_ticket_fetch,
    mock_manual_ticket_operations,
    mock_manual_implementation,
):
    sign_in("admin")
    sign_in("developer", "runner")
    run_id = client.post(
        "/api/run-ticket",
        json={"ticket_key": "716", "mode": "manual"},
    ).json()["run_id"]
    client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "result": {
                "summary": "Implement behavior",
                "description": "Implement the approved behavior safely.",
            }
        },
    )
    client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "action": "accept_implementation",
            "files": {"src/health.py": "def health():\n    return 'reviewed'\n"},
        },
    )

    response = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "action": "flag_verification_concern",
            "feedback": "A reviewer must confirm the authorization behavior.",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "human"
    assert response.json()["manual_task"] is None
    result = runs_db.get_manual_results(run_id)["verify"]["result"]
    assert result["decision"] == "needs_human"
    assert result["evidence"] == ("A reviewer must confirm the authorization behavior.")


def test_human_can_submit_verification_evidence_after_automated_failure(
    mock_manual_ticket_fetch,
    mock_manual_ticket_operations,
    mock_manual_implementation,
):
    sign_in("admin")
    sign_in("developer", "runner")
    run_id = client.post(
        "/api/run-ticket",
        json={"ticket_key": "717", "mode": "manual"},
    ).json()["run_id"]
    client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "result": {
                "summary": "Implement behavior",
                "description": "Implement the approved behavior safely.",
            }
        },
    )
    client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "action": "accept_implementation",
            "files": {"src/health.py": "def health():\n    return 'reviewed'\n"},
        },
    )
    task_data = runs_db.get_manual_task_data(run_id)
    task_data["report"]["decision"] = "fail"
    task_data["report"]["reviewer_verdict"] = "FAIL"
    task_data["report"]["decision_reason"] = (
        "Reviewer returned FAIL. Reviewer did not identify a blocking defect."
    )
    runs_db.update_manual_task_data(
        run_id,
        "developer",
        "verify",
        task_data,
        "Simulated automated reviewer false positive",
        expected_phase="review",
    )

    response = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "action": "submit_manual_verification",
            "feedback": (
                "I reran the endpoint tests, reviewed the changed code against "
                "each acceptance criterion, and confirmed all expected responses."
            ),
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["manual_task"]["stage_key"] == "pr"
    result = runs_db.get_manual_results(run_id)["verify"]["result"]
    assert result["decision"] == "pass"
    assert result["review_method"] == "human_after_automated_report"
    assert result["manual_evidence"].startswith("I reran the endpoint tests")
    assert result["report"]["decision"] == "pass"
    assert result["report"]["reviewer_verdict"] == "FAIL"


@pytest.mark.usefixtures(
    "mock_manual_ticket_fetch",
    "mock_manual_ticket_operations",
)
def test_human_can_rerun_a_saved_verification_report():
    sign_in("admin")
    sign_in("developer", "runner")
    run_id = client.post(
        "/api/run-ticket",
        json={"ticket_key": "718", "mode": "manual"},
    ).json()["run_id"]
    client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "result": {
                "summary": "Implement behavior",
                "description": "Implement the approved behavior safely.",
            }
        },
    )
    client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "action": "accept_implementation",
            "files": {"src/health.py": "def health():\n    return 'reviewed'\n"},
        },
    )
    task_data = runs_db.get_manual_task_data(run_id)
    task_data["report"].update(
        {
            "decision": "fail",
            "reviewer_verdict": "INCOMPLETE",
            "decision_reason": "Old report format was rejected.",
        }
    )
    runs_db.update_manual_task_data(
        run_id,
        "developer",
        "verify",
        task_data,
        "Simulated report from the older reviewer format",
        expected_phase="review",
    )

    response = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={"action": "retry_verification"},
    )

    assert response.status_code == 200, response.text
    current = response.json()
    assert current["manual_task"]["stage_key"] == "verify"
    assert current["manual_task"]["phase"] == "review"
    assert current["manual_task"]["report"]["decision"] == "pass"


def test_manual_implementation_can_rerun_agent_with_human_feedback(
    mock_manual_ticket_fetch,
    mock_manual_ticket_operations,
    mock_manual_implementation,
):
    sign_in("admin")
    sign_in("developer", "runner")
    response = client.post(
        "/api/run-ticket",
        json={"ticket_key": "712", "mode": "manual"},
    )
    run_id = response.json()["run_id"]
    client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "result": {
                "summary": "Implement behavior",
                "description": "Implement the approved behavior safely.",
            }
        },
    )

    revised = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "action": "request_changes",
            "feedback": "Add a regression test for the empty input case.",
            "files": {"src/health.py": "def health():\n    return 'edited'\n"},
        },
    )

    assert revised.status_code == 200
    current = client.get(f"/api/run-status/{run_id}").json()
    assert current["manual_task"]["stage_key"] == "agent"
    assert current["manual_task"]["phase"] == "review"
    assert current["manual_task"]["feedback"] == (
        "Add a regression test for the empty input case."
    )
    assert mock_manual_implementation["reviewed_files"][run_id] == {
        "src/health.py": "def health():\n    return 'edited'\n"
    }


def test_manual_implementation_validation_failure_keeps_final_code_editable(
    mock_manual_ticket_fetch,
    mock_manual_ticket_operations,
    monkeypatch,
):
    sign_in("admin")
    sign_in("developer", "runner")

    def report_failure(run_id, actor, _task_data):
        data = runs_db.get_manual_task_data(run_id)
        data.update(
            {
                "phase": "validation_failed",
                "test_output": "Configured tests failed on the human-edited code.",
            }
        )
        runs_db.update_manual_task_data(
            run_id,
            actor,
            "agent",
            data,
            "Configured tests failed",
            step_state="human",
            expected_phase="validating",
        )

    monkeypatch.setattr(api_server, "_start_manual_validation", report_failure)
    response = client.post(
        "/api/run-ticket",
        json={"ticket_key": "713", "mode": "manual"},
    )
    run_id = response.json()["run_id"]
    client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "result": {
                "summary": "Implement behavior",
                "description": "Implement the approved behavior safely.",
            }
        },
    )

    response = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "action": "accept_implementation",
            "files": {"src/health.py": "def health():\n    return 'human-final'\n"},
        },
    )

    assert response.status_code == 200
    current = client.get(f"/api/run-status/{run_id}").json()
    assert current["manual_task"]["stage_key"] == "agent"
    assert current["manual_task"]["phase"] == "validation_failed"
    assert "Configured tests failed" in current["manual_task"]["test_output"]
    assert current["manual_task"]["proposed_files"][0]["content"].endswith(
        "return 'human-final'\n"
    )
    assert "agent" not in runs_db.get_manual_results(run_id)


def test_manual_clarity_gate_requests_and_rechecks_human_clarification(
    mock_manual_ticket_fetch, mock_manual_ticket_operations, monkeypatch
):
    sign_in("admin")
    sign_in("developer", "runner")
    apps_store.update_app_config("default", {"TARGET_ASSIGNEE": "agent@example.com"})
    analyses = iter(
        [
            {
                "clear": False,
                "reason": "Acceptance criteria are not stated.",
                "questions": ["What behavior should happen when the input is empty?"],
                "suggested_summary": "Handle empty input",
                "suggested_description": (
                    "Define the expected behavior for empty input."
                ),
            },
            {
                "clear": False,
                "reason": "The edge case is answered, but expected output is unclear.",
                "questions": ["Should empty input return an error or an empty result?"],
                "suggested_summary": "Handle empty input consistently",
                "suggested_description": "Specify the response for empty input.",
            },
            {
                "clear": True,
                "reason": "The expected empty-input behavior is now explicit.",
                "questions": [],
                "suggested_summary": "",
                "suggested_description": "",
            },
        ]
    )
    seen_values = []

    def analyze(_config, _ticket_key, values):
        seen_values.append(values.copy())
        return next(analyses)

    monkeypatch.setattr(api_server, "_analyze_manual_clarity", analyze)
    response = client.post(
        "/api/run-ticket",
        json={"ticket_key": "703", "mode": "manual"},
    )
    run_id = response.json()["run_id"]
    fetch_response = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "result": {
                "summary": "Handle input",
                "description": "Implement input processing.",
            }
        },
    )
    assert fetch_response.status_code == 200
    gate_task = fetch_response.json()["manual_task"]
    assert gate_task["stage_key"] == "gate"
    assert gate_task["analysis"] == "Acceptance criteria are not stated."
    assert gate_task["questions"] == [
        "What behavior should happen when the input is empty?"
    ]
    assert gate_task["initial_values"]["summary"] == "Handle empty input"
    assert [field["name"] for field in gate_task["fields"]] == [
        "summary",
        "description",
        "relevant_context",
        "answers",
    ]

    still_unclear = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "result": {
                "summary": "Handle empty input",
                "description": "Define the expected behavior for empty input.",
                "relevant_context": "The API receives a string parameter.",
                "answers": "Empty input is an edge case that must be handled.",
            }
        },
    )
    assert still_unclear.status_code == 200
    assert still_unclear.json()["manual_task"]["stage_key"] == "gate"
    assert still_unclear.json()["manual_task"]["questions"] == [
        "Should empty input return an error or an empty result?"
    ]
    runs_db.init_db()
    persisted = client.get(f"/api/run-status/{run_id}").json()
    assert persisted["manual_task"]["stage_key"] == "gate"
    assert persisted["manual_task"]["questions"] == [
        "Should empty input return an error or an empty result?"
    ]

    clarified = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "result": {
                "summary": "Handle empty input consistently",
                "description": "Return an empty list when the input is empty.",
                "relevant_context": "The API receives a string parameter.",
                "answers": "Return an empty list for empty input.",
            }
        },
    )
    assert clarified.status_code == 200
    assert clarified.json()["manual_task"]["stage_key"] == "agent"
    assert clarified.json()["manual_task"]["context"]["in_progress"] == {
        "status": "Active",
        "operation": "Moved to target status",
    }
    assert clarified.json()["manual_task"]["context"]["gate"]["summary"] == (
        "Handle empty input consistently"
    )
    assert clarified.json()["manual_task"]["context"]["gate"]["answers"] == (
        "Return an empty list for empty input."
    )
    assert len(seen_values) == 3
    assert seen_values[-1]["answers"] == "Return an empty list for empty input."
    assert [call[1] for call in mock_manual_ticket_operations] == [
        "assign",
        "in_progress",
    ]


def test_manual_clarity_gate_failure_does_not_advance(
    mock_manual_ticket_fetch, mock_manual_ticket_operations, monkeypatch
):
    sign_in("admin")
    sign_in("developer", "runner")
    monkeypatch.setattr(
        api_server,
        "_analyze_manual_clarity",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("model unavailable")),
    )
    response = client.post(
        "/api/run-ticket",
        json={"ticket_key": "711", "mode": "manual"},
    )
    run_id = response.json()["run_id"]

    failed_analysis = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "result": {
                "summary": "Ticket summary",
                "description": "Ticket description.",
            }
        },
    )

    assert failed_analysis.status_code == 502
    assert "could not analyze" in failed_analysis.json()["detail"]
    current = client.get(f"/api/run-status/{run_id}").json()
    assert current["status"] == "awaiting-human"
    assert current["manual_task"]["stage_key"] == "fetch"
    assert [call[1] for call in mock_manual_ticket_operations] == ["assign"]


def test_manual_run_remains_waiting_after_database_reinitialization(
    mock_manual_ticket_fetch,
):
    sign_in("admin")
    sign_in("developer", "runner")
    response = client.post(
        "/api/run-ticket",
        json={"ticket_key": "704", "mode": "manual"},
    )
    run_id = response.json()["run_id"]

    runs_db.init_db()

    run = client.get(f"/api/run-status/{run_id}").json()
    assert run["status"] == "awaiting-human"
    assert run["manual_task"]["stage_key"] == "fetch"
    assert run["manual_task"]["initial_values"] == {
        "summary": "Fetched ticket 704",
        "description": "Fetched ticket description.",
    }


@pytest.mark.parametrize(
    ("stage_key", "phase", "recovered_phase"),
    [
        ("agent", "generating", "failed"),
        ("agent", "applying", "review"),
        ("agent", "validating", "validation_failed"),
        ("verify", "verifying", "failed"),
        ("verify", "", "failed"),
    ],
)
def test_interrupted_manual_implementation_is_recoverable_after_restart(
    stage_key, phase, recovered_phase
):
    run_id = str(uuid4())
    runs_db.create_run(
        run_id,
        "705",
        "2026-10-26T00:00:00+00:00",
        workflow_mode="manual",
    )
    run = runs_db.get_run(run_id)
    steps = run["steps"]
    for step in steps:
        if step["key"] == stage_key:
            step["state"] = "in-progress"
    conn = runs_db._get_conn()
    conn.execute(
        """
        UPDATE runs
        SET manual_stage = ?, manual_task_data = ?, steps = ?
        WHERE id = ?
        """,
        (stage_key, json.dumps({"phase": phase}), json.dumps(steps), run_id),
    )
    conn.commit()
    conn.close()

    runs_db.init_db()

    recovered = runs_db.get_run(run_id)
    assert recovered["status"] == "awaiting-human"
    assert recovered["manual_task"]["stage_key"] == stage_key
    assert recovered["manual_task"]["phase"] == recovered_phase
    if phase == "applying":
        assert "restarted while applying edits" in recovered["manual_task"]["error"]
    assert (
        next(step for step in recovered["steps"] if step["key"] == stage_key)["state"]
        == "human"
    )


def test_manual_mode_uses_integration_identity_when_target_is_unconfigured(
    mock_manual_ticket_fetch, mock_manual_ticket_operations
):
    sign_in("admin")
    sign_in("developer", "runner")
    response = client.post(
        "/api/run-ticket",
        json={"ticket_key": "709", "mode": "manual"},
    )
    run_id = response.json()["run_id"]
    fetch_response = client.post(
        f"/api/runs/{run_id}/manual-stage",
        json={
            "result": {
                "summary": "Ticket summary",
                "description": "Ticket description.",
            }
        },
    )
    assert fetch_response.status_code == 200
    next_task = fetch_response.json()["manual_task"]
    assert next_task["stage_key"] == "agent"
    assert next_task["context"]["assign"] == {
        "actual_assignee": "Integration User",
        "operation": "Already assigned",
    }
    assert mock_manual_ticket_operations[0] == (
        "709",
        "assign",
        "assign_to_self",
        {},
    )


def test_manual_fetch_uses_ticket_provider_and_parses_details(monkeypatch):
    config = PipelineConfig.from_settings(
        {
            "TICKET_PROVIDER": "ado",
            "ADO_ORG_URL": "https://dev.azure.com/example",
            "ADO_PROJECT": "project",
            "ADO_PAT": "pat",
        },
        app_id="default",
    )
    actions = []

    class TicketResult:
        is_error = False
        result = "704"
        text = (
            "Key: 704\n"
            "Summary: Provider summary\n"
            "Status: New\n"
            "Description: Provider description."
        )

    monkeypatch.setattr(
        PipelineConfig,
        "ticket_executor",
        lambda _self: lambda action: (actions.append(action) or TicketResult()),
    )

    assert api_server._fetch_manual_ticket_fields("704", config) == {
        "summary": "Provider summary",
        "description": "Provider description.",
    }
    assert actions[0].command == "get_ticket"
    assert actions[0].ticket_key == "704"


def test_manual_ticket_operation_returns_provider_assignee_and_operation(monkeypatch):
    config = PipelineConfig.from_settings(
        {
            "TICKET_PROVIDER": "ado",
            "ADO_ORG_URL": "https://dev.azure.com/example",
            "ADO_PROJECT": "project",
            "ADO_PAT": "pat",
        },
        app_id="default",
    )
    monkeypatch.setattr(
        PipelineConfig,
        "ticket_executor",
        lambda _self: lambda _action: SimpleNamespace(
            is_error=False,
            text="Assignee: Target User\nOperation: already_assigned",
        ),
    )
    runs_db.create_run(
        "manual-operation",
        "710",
        "2026-10-22T00:00:00+00:00",
        workflow_mode="manual",
    )
    run = {
        "id": "manual-operation",
        "app_id": "default",
        "ticket_key": "710",
    }

    result = api_server._run_manual_ticket_operation(
        run,
        "assign",
        "assign_to_user",
        config,
        assignee="target@example.com",
    )

    assert result == {
        "actual_assignee": "Target User",
        "operation": "Already assigned",
    }


def test_manual_final_status_operation_returns_confirmed_provider_status(monkeypatch):
    config = PipelineConfig.from_settings(
        {
            "TICKET_PROVIDER": "ado",
            "ADO_ORG_URL": "https://dev.azure.com/example",
            "ADO_PROJECT": "project",
            "ADO_PAT": "pat",
        },
        app_id="default",
    )
    monkeypatch.setattr(
        PipelineConfig,
        "ticket_executor",
        lambda _self: lambda _action: SimpleNamespace(
            is_error=False,
            text="Status: Closed\nOperation: already_in_target_status",
        ),
    )
    run_id = str(uuid4())
    runs_db.create_run(
        run_id,
        "718",
        "2026-10-26T00:00:00+00:00",
        workflow_mode="manual",
    )

    result = api_server._run_manual_ticket_operation(
        {
            "id": run_id,
            "app_id": "default",
            "ticket_key": "718",
        },
        "jira",
        "update_status",
        config,
        target_status="Closed",
    )

    assert result == {
        "status": "Closed",
        "operation": "Already in target status",
    }


def test_manual_run_does_not_create_run_when_ticket_fetch_fails(monkeypatch):
    from fastapi import HTTPException

    sign_in("admin")
    sign_in("developer", "runner")

    def fail_fetch(_ticket_key, _config):
        raise HTTPException(status_code=502, detail="Ticket provider unavailable")

    monkeypatch.setattr(api_server, "_fetch_manual_ticket_fields", fail_fetch)
    before = {run["id"] for run in runs_db.get_runs()}

    response = client.post(
        "/api/run-ticket",
        json={"ticket_key": "708", "mode": "manual"},
    )

    assert response.status_code == 502
    assert {run["id"] for run in runs_db.get_runs()} == before


def test_concurrent_manual_submission_cannot_complete_a_later_stage():
    runs_db.create_run(
        "manual-race",
        "707",
        "2026-10-22T00:00:00+00:00",
        workflow_mode="manual",
    )
    runs_db.complete_manual_stage(
        "manual-race",
        "developer",
        "fetch",
        {"summary": "Task", "description": "Description"},
    )

    try:
        runs_db.complete_manual_stage(
            "manual-race",
            "developer",
            "fetch",
            {"evidence": "Incorrectly replayed fetch result"},
        )
    except ManualStageConflict:
        pass
    else:
        raise AssertionError(
            "A stale manual stage submission must not advance the workflow"
        )

    assert runs_db.get_run("manual-race")["manual_task"]["stage_key"] == "assign"


def test_removed_hybrid_mode_is_rejected():
    sign_in("admin")
    sign_in("developer", "runner")
    response = client.post(
        "/api/run-ticket",
        json={"ticket_key": "705", "mode": "hybrid"},
    )
    assert response.status_code == 422
