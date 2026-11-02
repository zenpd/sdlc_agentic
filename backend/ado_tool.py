import html
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

_BLOCK_BREAK_RE = re.compile(r"</?(?:br|p|div|li|ul|ol|h[1-6])\b[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_BLANKLINES_RE = re.compile(r"\n{3,}")
_WORK_ITEM_ID_RE = re.compile(r"^[1-9]\d{0,9}$")


def _html_to_text(raw: str) -> str:
    """ADO stores System.Description as HTML. Downstream code (the Repo:/Branch:
    override regex in jira_bot.py, and the LLM prompts) expects plain text with
    real line breaks — a literal '<br>' left in a "Repo: owner/repo<br>" line
    gets parsed as part of the repo name, which then breaks git clone."""
    text = _BLOCK_BREAK_RE.sub("\n", raw)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    return _BLANKLINES_RE.sub("\n\n", text).strip()


def ado_ticket_matches_wiql(
    *,
    org_url: str,
    project: str,
    pat: str,
    wiql: str,
    work_item_id: str,
) -> bool:
    """Return whether this Azure DevOps work item satisfies the app's WIQL."""
    if not _WORK_ITEM_ID_RE.fullmatch(work_item_id):
        raise ValueError("Invalid Azure DevOps work item ID")
    if not wiql.strip():
        raise ValueError("No Azure DevOps WIQL is configured for this application")

    query = re.sub(
        r"\s+ORDER\s+BY\b.*$",
        "",
        wiql.strip(),
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not query:
        raise ValueError("The configured Azure DevOps WIQL has no filter clause")

    session = requests.Session()
    session.auth = ("", pat)
    try:
        response = session.post(
            f"{org_url.rstrip('/')}/{project}/_apis/wit/wiql",
            params={"api-version": "7.1"},
            json={"query": f"({query}) AND [System.Id] = {work_item_id}"},
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        items = response.json().get("workItems", [])
        return any(str(item.get("id")) == work_item_id for item in items)
    finally:
        session.close()


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
    command: Literal[
        "get_ticket",
        "get_comments",
        "add_comment",
        "update_status",
        "assign_to_self",
        "assign_to_user",
    ] = Field(description="The Azure DevOps command to execute.")
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
    assignee: str = Field(default="", description="Target user for assign_to_user.")


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
    def __init__(
        self,
        org_url: str | None = None,
        project: str | None = None,
        pat: str | None = None,
        assignee: str | None = None,
    ):
        # Explicit values come from the onboarded application's config (see
        # jira_bot.PipelineConfig); any left as None fall back to the process
        # environment so the standalone ADOTool registration keeps working.
        self._org_url: str = (
            org_url if org_url is not None else os.getenv("ADO_ORG_URL", "")
        ).rstrip("/")
        self._project: str = (
            project if project is not None else os.getenv("ADO_PROJECT", "")
        )
        self._pat: str = pat if pat is not None else os.getenv("ADO_PAT", "")
        self._assignee: str = (
            assignee if assignee is not None else os.getenv("ADO_ASSIGNEE", "")
        )
        self._me: dict | None = None
        self._ensure_settings()
        logger.info(f"Connecting to Azure DevOps at {self._org_url}/{self._project}")

    def _ensure_settings(self):
        values = {
            "ADO_ORG_URL": self._org_url,
            "ADO_PROJECT": self._project,
            "ADO_PAT": self._pat,
        }
        missing = [k for k in ENV_VARS if not values[k]]
        if missing:
            raise RuntimeError(
                "ADOTool: Missing required settings: "
                f"{', '.join(sorted(missing))}. "
                "Set ADO_ORG_URL, ADO_PROJECT, and ADO_PAT."
            )

    def _session(self) -> requests.Session:
        s = requests.Session()
        s.auth = ("", self._pat)  # ADO PATs go in the password slot of basic auth
        s.headers.update({"Content-Type": "application/json-patch+json"})
        return s

    def __call__(self, action: ADOAction, conversation=None) -> ADOObservation:
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
        desc = _html_to_text(fields.get("System.Description", "") or "")
        assigned_to = fields.get("System.AssignedTo")
        if isinstance(assigned_to, dict):
            assignee = (
                assigned_to.get("displayName") or assigned_to.get("uniqueName") or ""
            )
        else:
            assignee = str(assigned_to or "")
        return ADOObservation.from_text(
            text=(
                f"Key: {item['id']}\n"
                f"Summary: {fields.get('System.Title', '')}\n"
                f"Status: {fields.get('System.State', '')}\n"
                f"Assignee: {assignee or 'Unassigned'}\n"
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

    def _get_comments(self, action: ADOAction) -> ADOObservation:
        if not action.ticket_key:
            return ADOObservation.from_text(
                text="get_comments requires a ticket_key.",
                is_error=True,
                command=action.command,
            )
        session = self._session()
        try:
            response = session.get(
                f"{self._org_url}/{self._project}/_apis/wit/workItems/"
                f"{action.ticket_key}/comments",
                params={"api-version": "7.1-preview.3"},
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            comments = response.json().get("comments", [])
            text = "\n".join(
                str(comment.get("text", ""))
                for comment in comments
                if isinstance(comment, dict)
            )
            return ADOObservation.from_text(
                text=text,
                command=action.command,
                result=action.ticket_key,
            )
        except requests.RequestException as exc:
            return ADOObservation.from_text(
                text=f"Failed to read comments from {action.ticket_key}: {exc}",
                is_error=True,
                command=action.command,
            )
        finally:
            session.close()

    def _update_status(self, action: ADOAction) -> ADOObservation:
        if not action.ticket_key or not action.target_status:
            return ADOObservation.from_text(
                text="update_status requires both ticket_key and target_status.",
                is_error=True,
                command=action.command,
            )
        s = self._session()
        try:
            current = s.get(
                self._work_item_url(action.ticket_key),
                params={"api-version": "7.1", "fields": "System.State"},
            )
            current.raise_for_status()
            current_status = str(
                current.json().get("fields", {}).get("System.State", "")
            )
            if current_status.casefold() == action.target_status.strip().casefold():
                return ADOObservation.from_text(
                    text=(
                        f"Status: {current_status}\nOperation: already_in_target_status"
                    ),
                    command=action.command,
                    result=current_status,
                )
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
            fields = resp.json().get("fields", {})
            resulting_status = str(fields.get("System.State", action.target_status))
            return ADOObservation.from_text(
                text=(f"Status: {resulting_status}\nOperation: transitioned"),
                command=action.command,
                result=resulting_status,
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
            identity = self._assignee
            if not identity:
                try:
                    current_response = s.get(
                        self._work_item_url(action.ticket_key),
                        params={
                            "api-version": "7.1",
                            "fields": "System.AssignedTo",
                        },
                    )
                    current_response.raise_for_status()
                    current = (
                        current_response.json()
                        .get("fields", {})
                        .get("System.AssignedTo")
                    )
                    actual = (
                        current.get("displayName") or current.get("uniqueName")
                        if isinstance(current, dict)
                        else str(current or "Unassigned")
                    )
                except requests.RequestException as e:
                    return ADOObservation.from_text(
                        text=f"Failed to read assignee for {action.ticket_key}: {e}",
                        is_error=True,
                        command=action.command,
                    )
                return ADOObservation.from_text(
                    text=(
                        f"Assignee: {actual}\n"
                        "Operation: skipped_identity_lookup_unavailable"
                    ),
                    is_error=False,
                    command=action.command,
                    result=actual,
                )

        try:
            current_response = s.get(
                self._work_item_url(action.ticket_key),
                params={"api-version": "7.1", "fields": "System.AssignedTo"},
            )
            current_response.raise_for_status()
            current = current_response.json().get("fields", {}).get("System.AssignedTo")
            current_values = (
                {
                    str(current.get(key, "")).strip().casefold()
                    for key in ("displayName", "uniqueName", "id")
                }
                if isinstance(current, dict)
                else {str(current or "").strip().casefold()}
            )
            if identity.strip().casefold() in current_values:
                actual = (
                    current.get("displayName") or current.get("uniqueName") or identity
                    if isinstance(current, dict)
                    else str(current or identity)
                )
                return ADOObservation.from_text(
                    text=f"Assignee: {actual}\nOperation: already_assigned",
                    command=action.command,
                    result=actual,
                )
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
                text=(f"Assignee: {display_name or identity}\nOperation: assigned"),
                command=action.command,
                result=display_name or identity,
            )
        except requests.RequestException as e:
            return ADOObservation.from_text(
                text=f"Failed to assign {action.ticket_key}: {e}",
                is_error=True,
                command=action.command,
            )

    def _assign_to_user(self, action: ADOAction) -> ADOObservation:
        if not action.ticket_key or not action.assignee.strip():
            return ADOObservation.from_text(
                text="assign_to_user requires ticket_key and assignee.",
                is_error=True,
                command=action.command,
            )
        s = self._session()
        try:
            work_item = s.get(
                self._work_item_url(action.ticket_key),
                params={"api-version": "7.1", "fields": "System.AssignedTo"},
            )
            work_item.raise_for_status()
            current = work_item.json().get("fields", {}).get("System.AssignedTo")
            current_values = (
                {
                    str(current.get(key, "")).strip().casefold()
                    for key in ("displayName", "uniqueName", "id")
                }
                if isinstance(current, dict)
                else {str(current or "").strip().casefold()}
            )
            target = action.assignee.strip()
            if target.casefold() in current_values:
                actual = (
                    current.get("displayName") or current.get("uniqueName") or target
                    if isinstance(current, dict)
                    else str(current or target)
                )
                return ADOObservation.from_text(
                    text=(f"Assignee: {actual}\nOperation: already_assigned"),
                    command=action.command,
                    result=actual,
                )
            response = s.patch(
                self._work_item_url(action.ticket_key),
                params={"api-version": "7.1"},
                json=[
                    {
                        "op": "add",
                        "path": "/fields/System.AssignedTo",
                        "value": target,
                    }
                ],
            )
            response.raise_for_status()
            assigned = response.json().get("fields", {}).get("System.AssignedTo")
            actual = (
                assigned.get("displayName") or assigned.get("uniqueName") or target
                if isinstance(assigned, dict)
                else str(assigned or target)
            )
            return ADOObservation.from_text(
                text=f"Assignee: {actual}\nOperation: assigned",
                command=action.command,
                result=actual,
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

Supports five commands:
1. get_ticket — Fetch a work item by ID (ticket_key), or the next match for a WIQL
   query (jql_filter, named for parity with the Jira tool) when no ID is given.
   Returns id, title, description, state, and priority.
2. add_comment — Post a comment to a work item.
3. update_status — Set a work item's System.State field (e.g. 'Active', 'Closed').
4. assign_to_self — Assign a work item to the authenticated (PAT owner) account.
5. assign_to_user — Assign a work item to the specified identity, skipping it
   when the work item is already assigned to that identity.

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
