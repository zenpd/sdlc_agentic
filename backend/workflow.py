"""Canonical metadata for the current SDLC workflow."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StageDefinition:
    key: str
    label: str
    description: str
    manual_fields: tuple[dict[str, Any], ...] = ()


PIPELINE_STAGES = (
    StageDefinition(
        "fetch",
        "Fetch ticket",
        "Load and normalize the requested ticket.",
        (
            {"name": "summary", "label": "Ticket summary", "required": True},
            {
                "name": "description",
                "label": "Ticket description",
                "required": True,
                "multiline": True,
            },
        ),
    ),
    StageDefinition(
        "assign",
        "Assign ticket",
        "Assign the ticket to the configured target or integration user.",
    ),
    StageDefinition(
        "gate",
        "Clarity gate",
        (
            "Automatically assess the ticket and pause for specific human "
            "clarification only when needed."
        ),
        (
            {
                "name": "summary",
                "label": "Improved ticket summary",
                "required": True,
            },
            {
                "name": "description",
                "label": "Improved ticket description",
                "required": True,
                "multiline": True,
            },
            {
                "name": "relevant_context",
                "label": "Relevant ticket / repository context",
                "required": True,
                "multiline": True,
            },
            {
                "name": "answers",
                "label": "Answers to the clarification questions",
                "required": False,
                "multiline": True,
            },
        ),
    ),
    StageDefinition(
        "in_progress",
        "Mark in progress",
        "Move the ticket into its active state when implementation is ready to start.",
    ),
    StageDefinition(
        "agent",
        "Implementation",
        (
            "Load the approved ticket and repository instructions, generate a "
            "proposed change, and let a human review or edit it before validation."
        ),
    ),
    StageDefinition(
        "verify",
        "Verification",
        (
            "Run independent checks against the approved implementation, then "
            "review the automated report. Successful verification can continue "
            "to the pull-request stage; concerns can return to implementation."
        ),
    ),
    StageDefinition(
        "pr",
        "Pull request",
        (
            "Publish the approved implementation branch and create a pull request "
            "automatically and confirm it with the Git provider."
        ),
        (
            {"name": "url", "label": "Pull request URL", "required": True},
            {"name": "evidence", "label": "PR creation evidence", "required": True},
        ),
    ),
    StageDefinition(
        "jira",
        "Report ticket outcome",
        (
            "Collect the approved requirements, implementation, verification, "
            "and pull-request details; update the ticket and confirm the provider "
            "outcome automatically."
        ),
    ),
)

_STAGES_BY_KEY = {stage.key: stage for stage in PIPELINE_STAGES}


def get_stage(stage_key: str) -> StageDefinition:
    """Return a registered stage or raise a clear error for an unknown key."""
    try:
        return _STAGES_BY_KEY[stage_key]
    except KeyError as exc:
        raise ValueError(f"Unregistered workflow stage: {stage_key}") from exc


def initial_run_steps() -> list[dict[str, str]]:
    """Build the persisted run-step shape, keeping existing API labels stable."""
    return [
        {"key": stage.key, "label": stage.key, "state": "pending"}
        for stage in PIPELINE_STAGES
    ]


def manual_task(
    stage_key: str,
    completed_results: dict[str, dict] | None = None,
    initial_values: dict[str, str] | None = None,
    task_data: dict | None = None,
) -> dict[str, Any]:
    stage = get_stage(stage_key)
    context_fields = {
        "fetch": ("summary", "description"),
        "assign": ("actual_assignee", "operation"),
        "gate": (
            "decision",
            "evidence",
            "summary",
            "description",
            "relevant_context",
            "answers",
        ),
        "in_progress": ("status", "operation"),
        "agent": (
            "summary",
            "branch",
            "commit_sha",
            "changed_files",
            "test_results",
            "test_output",
            "diff",
        ),
        "verify": (
            "decision",
            "evidence",
            "summary",
            "branch",
            "changed_files",
            "reviewer_verdict",
            "verification_checks",
            "test_results",
        ),
        "pr": (
            "url",
            "number",
            "source_branch",
            "target_branch",
            "creation_status",
            "state",
            "evidence",
            "reviewed_by",
        ),
        "jira": (),
    }
    stage_index = [item.key for item in PIPELINE_STAGES].index(stage_key)
    context = {}
    for completed_stage in PIPELINE_STAGES[:stage_index]:
        stored = (completed_results or {}).get(completed_stage.key, {})
        completed_values = stored.get("result", {})
        allowed = context_fields[completed_stage.key]
        selected = {
            key: completed_values[key] for key in allowed if key in completed_values
        }
        if completed_stage.key == "agent" and stage_key != "verify":
            selected.pop("diff", None)
        if selected:
            context[completed_stage.key] = selected
    return {
        "stage_key": stage.key,
        "label": stage.label,
        "instructions": (task_data or {}).get("instructions", stage.description),
        "fields": [dict(field) for field in stage.manual_fields],
        "initial_values": (task_data or {}).get(
            "initial_values",
            initial_values if stage_key == "fetch" else {},
        ),
        "questions": (task_data or {}).get("questions", []),
        "analysis": (task_data or {}).get("analysis", ""),
        "attempts": (task_data or {}).get("attempts", []),
        "context": context,
        "phase": (task_data or {}).get("phase", ""),
        "branch": (task_data or {}).get("branch", ""),
        "base_sha": (task_data or {}).get("base_sha", ""),
        "proposed_files": (
            []
            if (task_data or {}).get("phase") in (
                "generating",
                "validating",
                "applying",
            )
            else (task_data or {}).get("proposed_files", [])
        ),
        "proposed_patch": (
            ""
            if (task_data or {}).get("phase") in (
                "generating",
                "validating",
                "applying",
            )
            else (task_data or {}).get("proposed_patch", "")
        ),
        "changed_files": (task_data or {}).get("changed_files", []),
        "agent_summary": (task_data or {}).get("agent_summary", ""),
        "implementation_score": (task_data or {}).get("implementation_score"),
        "implementation_evaluation": (task_data or {}).get(
            "implementation_evaluation"
        ),
        "test_results": (task_data or {}).get("test_results", ""),
        "test_output": (task_data or {}).get("test_output", ""),
        "feedback": (task_data or {}).get("feedback", ""),
        "error": (task_data or {}).get("error", ""),
        "report": (task_data or {}).get("report") or None,
        "pr": (task_data or {}).get("pr") or None,
        "ticket_update": (task_data or {}).get("ticket_update") or None,
    }


def validate_manual_result(stage_key: str, result: dict[str, str]) -> dict[str, str]:
    """Validate a human-submitted stage result and return normalized values."""
    stage = get_stage(stage_key)
    allowed = {field["name"] for field in stage.manual_fields}
    unknown = set(result) - allowed
    if unknown:
        raise ValueError(f"Unexpected result field(s): {', '.join(sorted(unknown))}")

    normalized = {
        name: value.strip() for name, value in result.items() if isinstance(value, str)
    }
    for field in stage.manual_fields:
        name = field["name"]
        value = normalized.get(name, "")
        if field.get("required") and not value:
            raise ValueError(f"{field['label']} is required")
        if len(value) > 10_000:
            raise ValueError(f"{field['label']} is too long")
        options = field.get("options")
        if value and options and value not in options:
            raise ValueError(f"{field['label']} must be one of: {', '.join(options)}")

    if stage_key == "gate":
        if len(normalized["relevant_context"]) < 5:
            raise ValueError(
                "Relevant ticket or repository context must include useful details"
            )
    elif stage_key == "agent":
        raise ValueError(
            "Implementation is generated automatically and cannot be "
            "submitted as a manual result"
        )
    elif stage_key == "pr":
        from urllib.parse import urlparse

        parsed = urlparse(normalized["url"])
        if (
            parsed.scheme != "https"
            or parsed.netloc.lower() != "github.com"
            or len(parsed.path.strip("/").split("/")) != 4
            or parsed.path.strip("/").split("/")[2] != "pull"
            or not parsed.path.strip("/").split("/")[3].isdigit()
        ):
            raise ValueError(
                "Pull request URL must be an HTTPS GitHub pull request URL"
            )
    return normalized
