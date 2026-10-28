import os
import re
from collections.abc import Sequence
from typing import Literal

from jira import JIRA
from jira.exceptions import JIRAError
from pydantic import Field

from openhands.sdk import (
    Action,
    Observation,
    ToolDefinition,
)
from openhands.sdk.tool import ToolExecutor, register_tool
from openhands.sdk.logger import get_logger


logger = get_logger(__name__)

_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")


ENV_VARS = {"JIRA_BASE_URL", "JIRA_USER_EMAIL", "JIRA_API_TOKEN"}


def jira_ticket_matches_jql(
    *,
    base_url: str,
    user_email: str,
    api_token: str,
    jql: str,
    ticket_key: str,
) -> bool:
    """Return whether this Jira ticket satisfies an app's configured JQL."""
    if not _ISSUE_KEY_RE.fullmatch(ticket_key):
        raise ValueError("Invalid Jira issue key")
    if not jql.strip():
        raise ValueError("No Jira JQL is configured for this application")

    query = re.sub(
        r"\s+ORDER\s+BY\b.*$",
        "",
        jql.strip(),
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not query:
        raise ValueError("The configured Jira JQL has no filter clause")
    jql_for_ticket = f"({query}) AND key = {ticket_key}"
    jira = JIRA(server=base_url, basic_auth=(user_email, api_token))
    issues = jira.search_issues(jql_for_ticket, maxResults=1, fields="key")
    return any(issue.key == ticket_key for issue in issues)


class JiraAction(Action):
    command: Literal[
        "get_ticket",
        "get_comments",
        "add_comment",
        "update_status",
        "assign_to_self",
        "assign_to_user",
    ] = Field(description="The Jira command to execute.")
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
    assignee: str = Field(default="", description="Target user for assign_to_user.")


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
    def __init__(
        self,
        base_url: str | None = None,
        user_email: str | None = None,
        api_token: str | None = None,
    ):
        # Explicit values come from the onboarded application's config (see
        # jira_bot.PipelineConfig); any left as None fall back to the process
        # environment so the standalone JiraTool registration keeps working.
        self._base_url = (
            base_url if base_url is not None else os.getenv("JIRA_BASE_URL", "")
        )
        self._user_email = (
            user_email if user_email is not None else os.getenv("JIRA_USER_EMAIL", "")
        )
        self._api_token = (
            api_token if api_token is not None else os.getenv("JIRA_API_TOKEN", "")
        )
        self._jira: JIRA | None = None
        self._ensure_settings()
        self._connect()

    def _ensure_settings(self):
        values = {
            "JIRA_BASE_URL": self._base_url,
            "JIRA_USER_EMAIL": self._user_email,
            "JIRA_API_TOKEN": self._api_token,
        }
        missing = [k for k in ENV_VARS if not values[k]]
        if missing:
            raise RuntimeError(
                "JiraTool: Missing required settings: "
                f"{', '.join(sorted(missing))}. "
                "Set JIRA_BASE_URL, JIRA_USER_EMAIL, and JIRA_API_TOKEN."
            )

    def _connect(self):
        logger.info(f"Connecting to Jira at {self._base_url}")
        self._jira = JIRA(
            server=self._base_url, basic_auth=(self._user_email, self._api_token)
        )

    def __call__(self, action: JiraAction, conversation=None) -> JiraObservation:
        if action.command == "get_ticket":
            return self._get_ticket(action)
        if action.command == "get_comments":
            return self._get_comments(action)
        if action.command == "add_comment":
            return self._add_comment(action)
        if action.command == "update_status":
            return self._update_status(action)
        if action.command == "assign_to_self":
            return self._assign_to_self(action)
        if action.command == "assign_to_user":
            return self._assign_to_user(action)
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
        assignee = getattr(fields, "assignee", None)
        assignee_name = (
            getattr(assignee, "displayName", "") if assignee else "Unassigned"
        )
        return JiraObservation.from_text(
            text=(
                f"Key: {issue.key}\n"
                f"Summary: {fields.summary}\n"
                f"Status: {fields.status.name}\n"
                f"Assignee: {assignee_name or 'Unassigned'}\n"
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

    def _get_comments(self, action: JiraAction) -> JiraObservation:
        if not action.ticket_key:
            return JiraObservation.from_text(
                text="get_comments requires a ticket_key.",
                is_error=True,
                command=action.command,
            )
        try:
            issue = self._jira.issue(action.ticket_key, fields="comment")
            comments = getattr(issue.fields.comment, "comments", []) or []
            text = "\n".join(str(getattr(comment, "body", "")) for comment in comments)
            return JiraObservation.from_text(
                text=text,
                command=action.command,
                result=action.ticket_key,
            )
        except Exception as exc:
            return JiraObservation.from_text(
                text=f"Failed to read comments from {action.ticket_key}: {exc}",
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
            issue = self._jira.issue(action.ticket_key, fields="status")
            current_status = issue.fields.status.name
            if (
                current_status.strip().casefold()
                == action.target_status.strip().casefold()
            ):
                return JiraObservation.from_text(
                    text=(
                        f"Status: {current_status}\nOperation: already_in_target_status"
                    ),
                    command=action.command,
                    result=current_status,
                )
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
            updated = self._jira.issue(action.ticket_key, fields="status")
            resulting_status = updated.fields.status.name
            return JiraObservation.from_text(
                text=f"Status: {resulting_status}\nOperation: transitioned",
                command=action.command,
                result=resulting_status,
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
            issue = self._jira.issue(action.ticket_key, fields="assignee")
            current = getattr(issue.fields, "assignee", None)
            if current and getattr(current, "accountId", None) == account_id:
                name = getattr(current, "displayName", account_id)
                return JiraObservation.from_text(
                    text=f"Assignee: {name}\nOperation: already_assigned",
                    command=action.command,
                    result=name,
                )
            self._jira.assign_issue(action.ticket_key, account_id)
            return JiraObservation.from_text(
                text=(
                    f"Assignee: {me.get('displayName', account_id)}\n"
                    "Operation: assigned"
                ),
                command=action.command,
                result=me.get("displayName", account_id),
            )
        except Exception as e:
            return JiraObservation.from_text(
                text=f"Failed to assign {action.ticket_key}: {e}",
                is_error=True,
                command=action.command,
            )

    def _assign_to_user(self, action: JiraAction) -> JiraObservation:
        target = action.assignee.strip()
        if not action.ticket_key or not target:
            return JiraObservation.from_text(
                text="assign_to_user requires ticket_key and assignee.",
                is_error=True,
                command=action.command,
            )
        try:
            users = self._jira.search_users(query=target, maxResults=100)
            target_lower = target.casefold()
            matches = [
                user
                for user in users
                if target_lower
                in {
                    str(getattr(user, "accountId", "")).casefold(),
                    str(getattr(user, "emailAddress", "")).casefold(),
                    str(getattr(user, "displayName", "")).casefold(),
                    str(getattr(user, "name", "")).casefold(),
                }
            ]
            if not matches:
                try:
                    matches = [self._jira.user(target)]
                except JIRAError as e:
                    if e.status_code not in (400, 404):
                        raise
                    raise ValueError(f"No Jira user exactly matches {target!r}") from e
            if len(matches) > 1:
                raise ValueError(
                    f"More than one Jira user exactly matches {target!r}; use an account ID"
                )
            user = matches[0]
            account_id = getattr(user, "accountId", None) or getattr(user, "name", None)
            if not account_id:
                raise ValueError("The matched Jira user has no assignable account ID")
            issue = self._jira.issue(action.ticket_key, fields="assignee")
            current = getattr(issue.fields, "assignee", None)
            if current and getattr(current, "accountId", None) == account_id:
                actual = getattr(current, "displayName", account_id)
                return JiraObservation.from_text(
                    text=f"Assignee: {actual}\nOperation: already_assigned",
                    command=action.command,
                    result=actual,
                )
            self._jira.assign_issue(action.ticket_key, account_id)
            updated = self._jira.issue(action.ticket_key, fields="assignee")
            assigned = getattr(updated.fields, "assignee", None)
            actual = (
                getattr(assigned, "displayName", account_id)
                if assigned
                else getattr(user, "displayName", account_id)
            )
            return JiraObservation.from_text(
                text=f"Assignee: {actual}\nOperation: assigned",
                command=action.command,
                result=actual,
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

Supports five commands:
1. get_ticket — Search for tickets using JQL (e.g. 'status = "Agent-ready"').
   Returns key, summary, description, status, and priority.
2. add_comment — Post a comment to a ticket.
3. update_status — Transition a ticket to a new status (looks up transition ID by name).
4. assign_to_self — Assign a ticket to the authenticated (bot) account.
5. assign_to_user — Resolve and assign an exact account ID, email, or display name,
   skipping the operation when the ticket is already assigned to that user.

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
