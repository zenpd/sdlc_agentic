import os
import re
from collections.abc import Sequence
from typing import Literal

import requests
from pydantic import Field

from openhands.sdk import (
    Action,
    Observation,
    ToolDefinition,
)
from openhands.sdk.tool import ToolExecutor, register_tool
from openhands.sdk.logger import get_logger


logger = get_logger(__name__)


ENV_VARS = {"ADO_ORG_URL", "ADO_PROJECT", "ADO_PAT"}

# Default WIQL used when no specific ticket_key is given — mirrors JIRA_JQL's
# "pick up the next ready ticket" behaviour, but tag-based since a fresh ADO
# project won't have a custom "Agent-ready" field without a process-template change.
DEFAULT_WIQL = (
    "SELECT [System.Id] FROM WorkItems "
    "WHERE [System.TeamProject] = @project "
    "AND [System.Tags] CONTAINS 'Agent-ready' "
    "AND [System.State] = 'New' "
    "ORDER BY [System.ChangedDate] DESC"
)


class ADOAction(Action):
    command: Literal["get_ticket", "add_comment", "update_status", "assign_to_self"] = Field(
        description="The Azure DevOps command to execute."
    )
    jql_filter: str = Field(
        default="",
        description=(
            "WIQL query for get_ticket when ticket_key is not given "
            "(e.g. \"...WHERE [System.Tags] CONTAINS 'Agent-ready'\"). "
            "Named jql_filter for parity with the Jira tool."
        ),
    )
    ticket_key: str = Field(
        default="",
        description="ADO work item ID (e.g. '616') for get_ticket, add_comment, update_status.",
    )
    comment_text: str = Field(
        default="",
        description="Comment body for add_comment.",
    )
    target_status: str = Field(
        default="",
        description="Target state name for update_status (e.g. 'Active', 'Closed').",
    )


class ADOObservation(Observation):
    command: str = Field(description="The command that was executed.")
    result: str = Field(default="", description="Human-readable result text.")

    @property
    def visualize(self):
        from rich.text import Text

        text = Text()
        if self.is_error:
            text.append("Azure DevOps Error: ", style="red bold")
        text.append(self.result)
        return text


class ADOExecutor(ToolExecutor[ADOAction, ADOObservation]):
    def __init__(self):
        self._org_url: str = ""
        self._project: str = ""
        self._pat: str = ""
        self._me: dict | None = None
        self._ensure_env()
        self._connect()

    @staticmethod
    def _ensure_env():
        missing = [v for v in ENV_VARS if not os.getenv(v)]
        if missing:
            raise RuntimeError(
                "ADOTool: Missing required environment variables: "
                f"{', '.join(missing)}. "
                "Set ADO_ORG_URL, ADO_PROJECT, and ADO_PAT."
            )

    def _connect(self):
        self._org_url = os.environ["ADO_ORG_URL"].rstrip("/")
        self._project = os.environ["ADO_PROJECT"]
        self._pat = os.environ["ADO_PAT"]
        logger.info(f"Connecting to Azure DevOps at {self._org_url}/{self._project}")

    def _session(self) -> requests.Session:
        s = requests.Session()
        s.auth = ("", self._pat)  # ADO PATs go in the password slot of basic auth
        s.headers.update({"Content-Type": "application/json-patch+json"})
        return s

    def __call__(
        self, action: ADOAction, conversation=None
    ) -> ADOObservation:
        if action.command == "get_ticket":
            return self._get_ticket(action)
        if action.command == "add_comment":
            return self._add_comment(action)
        if action.command == "update_status":
            return self._update_status(action)
        if action.command == "assign_to_self":
            return self._assign_to_self(action)
        return ADOObservation.from_text(
            text=f"Unknown command: {action.command}",
            is_error=True,
            command=action.command,
        )

    def _work_item_url(self, work_item_id: str) -> str:
        return f"{self._org_url}/{self._project}/_apis/wit/workitems/{work_item_id}"

    @staticmethod
    def _normalize_id(raw: str) -> str:
        """ADO shows work items as 'Task 616', '#616', etc. in its own UI, so accept
        any of those and pull out the bare numeric ID the REST API actually wants."""
        match = re.search(r"(\d+)\s*$", raw.strip())
        return match.group(1) if match else raw.strip()

    def _get_ticket(self, action: ADOAction) -> ADOObservation:
        s = self._session()
        try:
            if action.ticket_key:
                work_item_id = self._normalize_id(action.ticket_key)
                resp = s.get(
                    self._work_item_url(work_item_id),
                    params={"api-version": "7.1", "$expand": "all"},
                )
                if resp.status_code == 404:
                    return ADOObservation.from_text(
                        text=f"No work item found with ID {work_item_id} (from {action.ticket_key!r})",
                        is_error=False,
                        command=action.command,
                        result="",
                    )
                resp.raise_for_status()
                item = resp.json()
            else:
                wiql = action.jql_filter or DEFAULT_WIQL
                wiql_resp = s.post(
                    f"{self._org_url}/{self._project}/_apis/wit/wiql",
                    params={"api-version": "7.1"},
                    json={"query": wiql},
                )
                wiql_resp.raise_for_status()
                ids = [w["id"] for w in wiql_resp.json().get("workItems", [])]
                if not ids:
                    return ADOObservation.from_text(
                        text=f"No work items found matching WIQL: {wiql}",
                        is_error=False,
                        command=action.command,
                        result="",
                    )
                resp = s.get(
                    self._work_item_url(str(ids[0])),
                    params={"api-version": "7.1", "$expand": "all"},
                )
                resp.raise_for_status()
                item = resp.json()
        except requests.RequestException as e:
            return ADOObservation.from_text(
                text=f"Azure DevOps request failed: {e}",
                is_error=True,
                command=action.command,
            )

        fields = item.get("fields", {})
        desc = fields.get("System.Description", "") or ""
        return ADOObservation.from_text(
            text=(
                f"Key: {item['id']}\n"
                f"Summary: {fields.get('System.Title', '')}\n"
                f"Status: {fields.get('System.State', '')}\n"
                f"Priority: {fields.get('Microsoft.VSTS.Common.Priority', '')}\n"
                f"Description: {desc[:2000]}"
            ),
            command=action.command,
            result=str(item["id"]),
        )

    def _add_comment(self, action: ADOAction) -> ADOObservation:
        if not action.ticket_key or not action.comment_text:
            return ADOObservation.from_text(
                text="add_comment requires both ticket_key and comment_text.",
                is_error=True,
                command=action.command,
            )
        s = self._session()
        try:
            resp = s.post(
                f"{self._org_url}/{self._project}/_apis/wit/workItems/"
                f"{action.ticket_key}/comments",
                params={"api-version": "7.1-preview.3"},
                headers={"Content-Type": "application/json"},
                json={"text": action.comment_text},
            )
            resp.raise_for_status()
            return ADOObservation.from_text(
                text=f"Comment added to work item {action.ticket_key}.",
                command=action.command,
            )
        except requests.RequestException as e:
            return ADOObservation.from_text(
                text=f"Failed to add comment to {action.ticket_key}: {e}",
                is_error=True,
                command=action.command,
            )

    def _update_status(self, action: ADOAction) -> ADOObservation:
        if not action.ticket_key or not action.target_status:
            return ADOObservation.from_text(
                text="update_status requires both ticket_key and target_status.",
                is_error=True,
                command=action.command,
            )
        s = self._session()
        try:
            resp = s.patch(
                self._work_item_url(action.ticket_key),
                params={"api-version": "7.1"},
                json=[
                    {
                        "op": "add",
                        "path": "/fields/System.State",
                        "value": action.target_status,
                    }
                ],
            )
            if resp.status_code >= 400:
                return ADOObservation.from_text(
                    text=(
                        f"Failed to transition {action.ticket_key} to "
                        f"'{action.target_status}': {resp.text[:500]}"
                    ),
                    is_error=True,
                    command=action.command,
                )
            return ADOObservation.from_text(
                text=f"{action.ticket_key} transitioned to '{action.target_status}'.",
                command=action.command,
            )
        except requests.RequestException as e:
            return ADOObservation.from_text(
                text=(
                    f"Failed to transition {action.ticket_key} "
                    f"to '{action.target_status}': {e}"
                ),
                is_error=True,
                command=action.command,
            )

    def _whoami(self, s: requests.Session) -> dict:
        if self._me is None:
            resp = s.get(
                "https://app.vssps.visualstudio.com/_apis/profile/profiles/me",
                params={"api-version": "7.1"},
            )
            resp.raise_for_status()
            self._me = resp.json()
        return self._me

    def _assign_to_self(self, action: ADOAction) -> ADOObservation:
        if not action.ticket_key:
            return ADOObservation.from_text(
                text="assign_to_self requires ticket_key.",
                is_error=True,
                command=action.command,
            )
        s = self._session()

        # Identity lookup needs a broader (Identity/Profile) PAT scope than the
        # Work-Items-only scope this tool otherwise needs — don't hard-fail the
        # whole step over that; fall back to ADO_ASSIGNEE, or skip cleanly.
        display_name = None
        try:
            me = self._whoami(s)
            identity = me.get("emailAddress") or me.get("displayName")
            display_name = me.get("displayName", identity)
        except requests.RequestException:
            identity = os.getenv("ADO_ASSIGNEE")
            if not identity:
                return ADOObservation.from_text(
                    text=(
                        f"Skipped assigning {action.ticket_key}: PAT lacks Identity/Profile "
                        "scope to look up the current user, and ADO_ASSIGNEE isn't set. "
                        "Set ADO_ASSIGNEE=<email> in .env to assign directly, or widen the "
                        "PAT's scope."
                    ),
                    is_error=False,
                    command=action.command,
                )

        try:
            resp = s.patch(
                self._work_item_url(action.ticket_key),
                params={"api-version": "7.1"},
                json=[
                    {
                        "op": "add",
                        "path": "/fields/System.AssignedTo",
                        "value": identity,
                    }
                ],
            )
            resp.raise_for_status()
            return ADOObservation.from_text(
                text=f"{action.ticket_key} assigned to {display_name or identity}.",
                command=action.command,
            )
        except requests.RequestException as e:
            return ADOObservation.from_text(
                text=f"Failed to assign {action.ticket_key}: {e}",
                is_error=True,
                command=action.command,
            )

    def close(self):
        pass


_ADO_DESCRIPTION = """Azure DevOps Boards integration tool for interacting with work items.

Supports four commands:
1. get_ticket — Fetch a work item by ID (ticket_key), or the next match for a WIQL
   query (jql_filter, named for parity with the Jira tool) when no ID is given.
   Returns id, title, description, state, and priority.
2. add_comment — Post a comment to a work item.
3. update_status — Set a work item's System.State field (e.g. 'Active', 'Closed').
4. assign_to_self — Assign a work item to the authenticated (PAT owner) account.

Authentication is via environment variables: ADO_ORG_URL, ADO_PROJECT, ADO_PAT.
"""


class ADOTool(ToolDefinition[ADOAction, ADOObservation]):
    @classmethod
    def create(cls, conv_state=None, **params) -> Sequence["ADOTool"]:
        executor = ADOExecutor()
        return [
            cls(
                description=_ADO_DESCRIPTION,
                action_type=ADOAction,
                observation_type=ADOObservation,
                executor=executor,
            )
        ]


register_tool(ADOTool.name, ADOTool)
