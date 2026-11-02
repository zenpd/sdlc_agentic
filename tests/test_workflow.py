import pytest

from backend.stages.ticket import parse_ticket
from backend.workflow import PIPELINE_STAGES, get_stage, initial_run_steps


def test_workflow_registry_matches_persisted_run_steps():
    assert [stage.key for stage in PIPELINE_STAGES] == [
        "fetch",
        "assign",
        "gate",
        "in_progress",
        "agent",
        "verify",
        "pr",
        "jira",
    ]
    assert initial_run_steps() == [
        {"key": stage.key, "label": stage.key, "state": "pending"}
        for stage in PIPELINE_STAGES
    ]
    assert get_stage("verify").label == "Verification"


def test_workflow_rejects_unregistered_stage_keys():
    with pytest.raises(ValueError, match="Unregistered workflow stage"):
        get_stage("unknown")


def test_ticket_stage_parses_fields_and_removes_routing_metadata():
    ticket = parse_ticket(
        "Key: PROJ-12\n"
        "Summary: Add health endpoint\n"
        "Status: Ready\n"
        "Description: Add GET /health to the service.\n"
        "Repository: org/service\n"
        "Branch: main\n"
        "Persona: backend-engineer\n"
        "Priority: High\n"
    )

    assert ticket.summary == "Add health endpoint"
    assert ticket.status == "Ready"
    assert ticket.description == "Add GET /health to the service."
    assert ticket.target_repo == "org/service"
    assert ticket.target_branch == "main"
    assert ticket.persona == "backend-engineer"


def test_ticket_stage_preserves_unrouted_ticket_values():
    ticket = parse_ticket(
        "Key: PROJ-13\n"
        "Summary: Add logging\n"
        "Status: Open\n"
        "Description: Add structured logging.\n"
        "Priority: Medium\n"
    )

    assert ticket.description == "Add structured logging."
    assert ticket.target_repo is None
    assert ticket.target_branch is None
    assert ticket.persona is None
