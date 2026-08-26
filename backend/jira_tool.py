import os
from collections.abc import Sequence
from typing import Literal

from jira import JIRA
from pydantic import Field

from openhands.sdk import (
    Action,
    Observation,
    ToolDefinition,
)
from openhands.sdk.tool import ToolExecutor, register_tool
from openhands.sdk.logger import get_logger


logger = get_logger(__name__)


ENV_VARS = {"JIRA_BASE_URL", "JIRA_USER_EMAIL", "JIRA_API_TOKEN"}


class JiraAction(Action):
    command: Literal["get_ticket", "add_comment", "update_status", "assign_to_self"] = Field(
        description="The Jira command to execute."
    )
    jql_filter: str = Field(
        default="",
        description="JQL query for get_ticket (e.g. 'status = \"Agent-ready\"').",
    )
    ticket_key: str = Field(
        default="",
        description="Jira ticket key (e.g. PROJ-123) for add_comment or update_status.",
    )
    comment_text: str = Field(
        default="",
        description="Comment body for add_comment.",
    )
    target_status: str = Field(
        default="",
        description="Target status name for update_status (e.g. 'In Review', 'Done').",
    )


class JiraObservation(Observation):
    command: str = Field(description="The command that was executed.")
    result: str = Field(default="", description="Human-readable result text.")

    @property
    def visualize(self):
        from rich.text import Text

        text = Text()
        if self.is_error:
            text.append("Jira Error: ", style="red bold")
        text.append(self.result)
        return text


class JiraExecutor(ToolExecutor[JiraAction, JiraObservation]):
    def __init__(self):
        self._jira: JIRA | None = None
        self._ensure_env()
        self._connect()

    @staticmethod
    def _ensure_env():
        missing = [v for v in ENV_VARS if not os.getenv(v)]
        if missing:
            raise RuntimeError(
                "JiraTool: Missing required environment variables: "
                f"{', '.join(missing)}. "
                "Set JIRA_BASE_URL, JIRA_USER_EMAIL, and JIRA_API_TOKEN."
            )

    def _connect(self):
        url = os.environ["JIRA_BASE_URL"]
        email = os.environ["JIRA_USER_EMAIL"]
        token = os.environ["JIRA_API_TOKEN"]
        logger.info(f"Connecting to Jira at {url}")
        self._jira = JIRA(server=url, basic_auth=(email, token))

    def __call__(
        self, action: JiraAction, conversation=None
    ) -> JiraObservation:
        if action.command == "get_ticket":
            return self._get_ticket(action)
        if action.command == "add_comment":
            return self._add_comment(action)
        if action.command == "update_status":
            return self._update_status(action)
        if action.command == "assign_to_self":
            return self._assign_to_self(action)
        return JiraObservation.from_text(
            text=f"Unknown command: {action.command}",
            is_error=True,
            command=action.command,
        )

    def _get_ticket(self, action: JiraAction) -> JiraObservation:
        if not action.jql_filter:
            return JiraObservation.from_text(
                text="get_ticket requires a jql_filter (e.g. 'status = \"Agent-ready\"').",
                is_error=True,
                command=action.command,
            )
        try:
            issues = self._jira.search_issues(action.jql_filter, maxResults=1)
        except Exception as e:
            return JiraObservation.from_text(
                text=f"Jira search failed: {e}",
                is_error=True,
                command=action.command,
            )
        if not issues:
            return JiraObservation.from_text(
                text=f"No tickets found matching JQL: {action.jql_filter}",
                is_error=False,
                command=action.command,
                result="",
            )
        issue = issues[0]
        fields = issue.fields
        desc = getattr(fields, "description", "") or ""
        return JiraObservation.from_text(
            text=(
                f"Key: {issue.key}\n"
                f"Summary: {fields.summary}\n"
                f"Status: {fields.status.name}\n"
                f"Priority: {getattr(fields, 'priority', None)}\n"
                f"Description: {desc[:2000]}"
            ),
            command=action.command,
            result=issue.key,
        )

    def _add_comment(self, action: JiraAction) -> JiraObservation:
        if not action.ticket_key or not action.comment_text:
            return JiraObservation.from_text(
                text="add_comment requires both ticket_key and comment_text.",
                is_error=True,
                command=action.command,
            )
        try:
            self._jira.add_comment(action.ticket_key, action.comment_text)
            return JiraObservation.from_text(
                text=f"Comment added to {action.ticket_key}.",
                command=action.command,
            )
        except Exception as e:
            return JiraObservation.from_text(
                text=f"Failed to add comment to {action.ticket_key}: {e}",
                is_error=True,
                command=action.command,
            )

    def _update_status(self, action: JiraAction) -> JiraObservation:
        if not action.ticket_key or not action.target_status:
            return JiraObservation.from_text(
                text="update_status requires both ticket_key and target_status.",
                is_error=True,
                command=action.command,
            )
        try:
            transitions = self._jira.transitions(action.ticket_key)
            target_lower = action.target_status.strip().lower()
            match = None
            for t in transitions:
                if t["name"].strip().lower() == target_lower:
                    match = t["id"]
                    break
            if not match:
                available = [t["name"] for t in transitions]
                return JiraObservation.from_text(
                    text=(
                        f"Status '{action.target_status}' not found. "
                        f"Available transitions: {available}"
                    ),
                    is_error=True,
                    command=action.command,
                )
            self._jira.transition_issue(action.ticket_key, match)
            return JiraObservation.from_text(
                text=(
                    f"{action.ticket_key} transitioned to "
                    f"'{action.target_status}'."
                ),
                command=action.command,
            )
        except Exception as e:
            return JiraObservation.from_text(
                text=(
                    f"Failed to transition {action.ticket_key} "
                    f"to '{action.target_status}': {e}"
                ),
                is_error=True,
                command=action.command,
            )

    def _assign_to_self(self, action: JiraAction) -> JiraObservation:
        if not action.ticket_key:
            return JiraObservation.from_text(
                text="assign_to_self requires ticket_key.",
                is_error=True,
                command=action.command,
            )
        try:
            me = self._jira.myself()
            account_id = me["accountId"]
            self._jira.assign_issue(action.ticket_key, account_id)
            return JiraObservation.from_text(
                text=f"{action.ticket_key} assigned to {me.get('displayName', account_id)}.",
                command=action.command,
            )
        except Exception as e:
            return JiraObservation.from_text(
                text=f"Failed to assign {action.ticket_key}: {e}",
                is_error=True,
                command=action.command,
            )

    def close(self):
        pass


_JIRA_DESCRIPTION = """Jira integration tool for interacting with Jira issues.

Supports four commands:
1. get_ticket — Search for tickets using JQL (e.g. 'status = "Agent-ready"').
   Returns key, summary, description, status, and priority.
2. add_comment — Post a comment to a ticket.
3. update_status — Transition a ticket to a new status (looks up transition ID by name).
4. assign_to_self — Assign a ticket to the authenticated (bot) account.

Authentication is via environment variables: JIRA_BASE_URL, JIRA_USER_EMAIL, JIRA_API_TOKEN.
"""


class JiraTool(ToolDefinition[JiraAction, JiraObservation]):
    @classmethod
    def create(cls, conv_state=None, **params) -> Sequence["JiraTool"]:
        executor = JiraExecutor()
        return [
            cls(
                description=_JIRA_DESCRIPTION,
                action_type=JiraAction,
                observation_type=JiraObservation,
                executor=executor,
            )
        ]


register_tool(JiraTool.name, JiraTool)
