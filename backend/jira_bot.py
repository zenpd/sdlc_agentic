import fnmatch
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import requests
from dotenv import load_dotenv

# Windows consoles / non-interactive threads often default stdout to cp1252,
# which can't encode the box-drawing and arrow characters used in step logs below.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from backend.jira_tool import JiraExecutor, JiraAction
from backend.ado_tool import ADOExecutor, ADOAction
from backend import skills_store

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────
# Everything a run needs to know about *which* application it's working for
# lives in one immutable PipelineConfig, built per run and passed into
# run_pipeline(). Nothing here is module-level state, so two applications'
# pipelines can run at the same time (bulk runs, parallel API calls) without
# one silently picking up the other's provider, repo, token, or status names.
#
# api_server.py builds it from the onboarded application (apps_store), where
# any field the app leaves blank falls back to the server-wide .env.
# PipelineConfig.from_env() keeps the CLI entry point (`python -m
# backend.jira_bot`) working off .env alone.
_DEFAULT_JQL = 'labels = "Agent-ready" AND status = "Ready for Agent"'
_DEFAULT_REPO = "nikhilbajaj12/Lighthouse-Pharos"

# Settings key -> PipelineConfig attribute, for the missing-settings check.
_REQUIRED_COMMON = {
    "AZURE_OPENAI_API_KEY": "azure_key",
    "AZURE_OPENAI_ENDPOINT": "azure_url",
    "AZURE_OPENAI_DEPLOYMENT": "azure_deployment",
    "GITHUB_TOKEN": "github_token",
}
_REQUIRED_ADO = {"ADO_ORG_URL": "ado_org_url", "ADO_PROJECT": "ado_project", "ADO_PAT": "ado_pat"}
_REQUIRED_JIRA = {"JIRA_BASE_URL": "jira_base_url", "JIRA_USER_EMAIL": "jira_user_email", "JIRA_API_TOKEN": "jira_api_token"}


@dataclass(frozen=True)
class PipelineConfig:
    ticket_provider: str = "jira"
    jira_base_url: str = ""
    jira_user_email: str = ""
    jira_api_token: str = ""
    jira_jql: str = _DEFAULT_JQL
    ado_org_url: str = ""
    ado_project: str = ""
    ado_pat: str = ""
    ado_assignee: str = ""
    ado_wiql: str = ""  # empty -> ado_tool.DEFAULT_WIQL (tag-based)
    target_repo: str = _DEFAULT_REPO
    target_branch: str = "dev"
    github_token: str = ""
    azure_deployment: str = ""
    azure_key: str = ""
    azure_url: str = ""
    status_failed: str = ""
    status_done: str = ""
    status_in_progress: str = ""
    # Per-application defaults
    default_persona: str = ""  # "" -> keyword-based persona selection
    branch_prefix: str = "feat/"
    gate_min_words: int = 5
    # Governance (new_req.md point 10)
    require_pr_approval: bool = False
    protected_paths: tuple[str, ...] = ()
    # Verification (new_req.md point 11)
    verify_mode: str = ""  # "" | "command" | "github_actions"
    test_setup_command: str = ""
    test_command: str = ""
    test_timeout_min: int = 10
    ci_workflow: str = ""
    app_id: str = "default"
    app_name: str = "Default application"

    @classmethod
    def from_settings(cls, settings: Mapping[str, Optional[str]], app_id: str = "default", app_name: str = "") -> "PipelineConfig":
        """Build from a flat KEY -> value mapping (an app's resolved settings,
        or os.environ). Blank values get the same defaults the old env-based
        config used."""
        def get(key: str, default: str = "") -> str:
            return (settings.get(key) or "").strip() or default

        provider = get("TICKET_PROVIDER", "jira").lower()
        if provider not in ("jira", "ado"):
            raise ValueError(f"TICKET_PROVIDER must be 'jira' or 'ado', got: {provider!r}")

        # Status/state names — defaults match a stock Jira workflow or a stock
        # ADO "Task" work item type; override per app if a process template differs.
        failed, done, in_progress = ("New", "Closed", "Active") if provider == "ado" else ("Human In Loop", "Done", "In Progress")

        prefix = re.sub(r"[^A-Za-z0-9._/-]+", "-", get("BRANCH_PREFIX", "feat/")).lstrip("/-")
        if prefix and not prefix.endswith(("/", "-")):
            prefix += "/"

        gate_raw = get("GATE_MIN_WORDS", "5")
        timeout_raw = get("TEST_TIMEOUT_MIN", "10")

        return cls(
            ticket_provider=provider,
            jira_base_url=get("JIRA_BASE_URL"),
            jira_user_email=get("JIRA_USER_EMAIL"),
            jira_api_token=get("JIRA_API_TOKEN"),
            jira_jql=get("JIRA_JQL", _DEFAULT_JQL),
            ado_org_url=get("ADO_ORG_URL"),
            ado_project=get("ADO_PROJECT"),
            ado_pat=get("ADO_PAT"),
            ado_assignee=get("ADO_ASSIGNEE"),
            ado_wiql=get("ADO_WIQL"),
            target_repo=get("TARGET_REPO", _DEFAULT_REPO),
            target_branch=get("TARGET_BRANCH", "dev"),
            github_token=get("GITHUB_TOKEN"),
            azure_deployment=get("AZURE_OPENAI_DEPLOYMENT"),
            azure_key=get("AZURE_OPENAI_API_KEY"),
            azure_url=get("AZURE_OPENAI_ENDPOINT"),
            status_failed=get("STATUS_FAILED", failed),
            status_done=get("STATUS_DONE", done),
            status_in_progress=get("STATUS_IN_PROGRESS", in_progress),
            default_persona=get("DEFAULT_PERSONA"),
            branch_prefix=prefix,
            gate_min_words=int(gate_raw) if gate_raw.isdigit() else 5,
            require_pr_approval=get("REQUIRE_PR_APPROVAL").lower() == "true",
            protected_paths=parse_protected_paths(get("PROTECTED_PATHS")),
            verify_mode=get("VERIFY_MODE").lower(),
            test_setup_command=get("TEST_SETUP_COMMAND"),
            test_command=get("TEST_COMMAND"),
            test_timeout_min=int(timeout_raw) if timeout_raw.isdigit() and int(timeout_raw) > 0 else 10,
            ci_workflow=get("CI_WORKFLOW"),
            app_id=app_id,
            app_name=app_name or app_id,
        )

    @classmethod
    def from_env(cls) -> "PipelineConfig":
        return cls.from_settings(os.environ)

    @property
    def ticket_query(self) -> str:
        return self.ado_wiql if self.ticket_provider == "ado" else self.jira_jql

    @property
    def azure_model(self) -> str:
        return f"azure/{self.azure_deployment or 'gpt-4.1-mini'}"

    @property
    def ticket_url_base(self) -> str:
        if self.ticket_provider == "ado":
            return f"{self.ado_org_url.rstrip('/')}/{self.ado_project}/_workitems/edit/"
        return f"{self.jira_base_url.rstrip('/')}/browse/"

    def missing_settings(self) -> list[str]:
        required = dict(_REQUIRED_COMMON)
        required.update(_REQUIRED_ADO if self.ticket_provider == "ado" else _REQUIRED_JIRA)
        if self.verify_mode == "command":
            required["TEST_COMMAND"] = "test_command"
        return [key for key, attr in required.items() if not getattr(self, attr)]

    def ticket_executor(self):
        """A ticket-provider client bound to this app's own credentials."""
        if self.ticket_provider == "ado":
            return ADOExecutor(
                org_url=self.ado_org_url, project=self.ado_project,
                pat=self.ado_pat, assignee=self.ado_assignee,
            )
        return JiraExecutor(base_url=self.jira_base_url, user_email=self.jira_user_email, api_token=self.jira_api_token)

    def ticket_action(self, **fields):
        # JiraAction and ADOAction share the same field names, so every call
        # site in run_pipeline stays provider-agnostic.
        return (ADOAction if self.ticket_provider == "ado" else JiraAction)(**fields)


OnStep = Callable[[str, str, Optional[str]], None]

# Called when an app requires sign-off before a PR opens. Receives a summary
# of the change and blocks until someone decides: returns (approved, actor,
# note), where approved is None if nobody decided before the timeout.
WaitForApproval = Callable[[dict], tuple[Optional[bool], str, str]]


def parse_protected_paths(raw: str) -> tuple[str, ...]:
    """Comma- or newline-separated glob patterns -> normalized tuple."""
    parts = re.split(r"[,\n]", raw or "")
    return tuple(p.strip().replace("\\", "/") for p in parts if p.strip())


def protected_path_violations(files: list[str], patterns: tuple[str, ...]) -> list[str]:
    """Files matching any protected pattern. Globs follow fnmatch, where `*`
    also crosses folders (so `ci/*` covers everything under ci/); `**` is
    accepted as an alias, and a pattern ending in `/` means "this folder".
    Each file is also tried with a leading `/`, so `*/ci/*` covers a root ci/."""
    hits = []
    for f in files:
        path = f.replace("\\", "/").lstrip("/")
        for pattern in patterns:
            pat = pattern.replace("**", "*")
            if pat.endswith("/"):
                pat += "*"
            if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch("/" + path, pat):
                hits.append(f)
                break
    return hits


class PipelineHalt(Exception):
    """Raised to stop the pipeline early with a final run status."""

    def __init__(self, status: str, message: str, pr_url: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.pr_url = pr_url  # set when a PR was opened but a later check (CI) failed


def _noop_on_step(step: str, state: str, log: Optional[str] = None) -> None:
    pass


def _make_agent_event_logger(on_step_fn: OnStep):
    """Builds a Conversation `callbacks=` function that turns the agent's live
    tool calls/results into short log lines pushed onto the 'agent' step, so
    the UI can show what the agent is actually doing (which command, which
    file) instead of one opaque spinner for the whole run.

    Tagged with `[TOOL:...]` / `[RESULT:...]` / `[DIFF:...]` / `[AGENT-ERROR]`
    prefixes so the frontend can parse out an animated "what's happening now"
    view; still reads fine as a plain log if left untouched.

    `[DIFF:file_editor]` is the detailed one: FileEditorObservation carries the
    full before/after file content, so a real unified diff (exact line numbers,
    +/- lines) can be computed here — not just "file_editor touched app.py: ok".
    """
    import difflib

    from openhands.sdk.event import ActionEvent, AgentErrorEvent, ObservationEvent

    def _on_agent_event(event) -> None:
        if isinstance(event, ActionEvent):
            action = event.action
            if event.tool_name == "terminal" and action is not None:
                cmd = (getattr(action, "command", "") or "")[:160]
                on_step_fn("agent", "in-progress", f"[TOOL:terminal] $ {cmd}")
            elif event.tool_name == "file_editor" and action is not None:
                path = getattr(action, "path", "")
                cmd = getattr(action, "command", "")
                detail = ""
                if cmd == "insert" and getattr(action, "insert_line", None) is not None:
                    detail = f" (at line {action.insert_line})"
                elif cmd == "view" and getattr(action, "view_range", None):
                    a, b = action.view_range
                    detail = f" (lines {a}-{b})"
                on_step_fn("agent", "in-progress", f"[TOOL:file_editor] {cmd} -> {path}{detail}")
            elif event.tool_name == "task_tracker" and action is not None:
                cmd = getattr(action, "command", "")
                on_step_fn("agent", "in-progress", f"[TOOL:task_tracker] {cmd}")
            else:
                on_step_fn("agent", "in-progress", f"[TOOL:{event.tool_name}] called")
        elif isinstance(event, ObservationEvent):
            obs = event.observation
            status = "FAILED" if obs.is_error else "ok"
            on_step_fn("agent", "in-progress", f"[RESULT:{event.tool_name}] {status}")

            if (
                event.tool_name == "file_editor"
                and not obs.is_error
                and getattr(obs, "command", None) in ("str_replace", "insert", "create")
                and getattr(obs, "new_content", None) is not None
            ):
                old_lines = (getattr(obs, "old_content", None) or "").splitlines()
                new_lines = obs.new_content.splitlines()
                hunks = list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=1))
                body = [ln for ln in hunks if not (ln.startswith("---") or ln.startswith("+++"))]
                if body:
                    truncated = len(body) > 24
                    snippet = "\n".join(body[:24])
                    if truncated:
                        snippet += f"\n... ({len(body) - 24} more lines)"
                    path = getattr(obs, "path", "") or ""
                    on_step_fn("agent", "in-progress", f"[DIFF:file_editor] {path}\n{snippet}")
        elif isinstance(event, AgentErrorEvent):
            on_step_fn("agent", "in-progress", f"[AGENT-ERROR] {event.error[:200]}")

    return _on_agent_event


# ── Helpers ─────────────────────────────────────────────────────────
def run_cmd(cmd: list[str], cwd: Path | None = None, silent: bool = False) -> tuple[int, str, str]:
    """Run a command, return (exit_code, stdout, stderr)."""
    if not silent:
        print(f"  $ {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if r.stdout:
        print(r.stdout[:1500])
    if r.stderr:
        print(r.stderr[:500])
    return r.returncode, r.stdout, r.stderr


# Anything that looks like a credential is stripped from the environment the
# repository's own setup/test commands run in — that's code we didn't write.
_SECRET_ENV_RE = re.compile(r"TOKEN|SECRET|PASSWORD|API_KEY|_PAT$|^ADO_PAT$", re.IGNORECASE)


def _safe_env() -> dict:
    return {k: v for k, v in os.environ.items() if not _SECRET_ENV_RE.search(k)}


def run_shell(command: str, cwd: Path, timeout_sec: int) -> tuple[Optional[int], str]:
    """Run an app's setup/test command. Returns (exit code, combined output);
    exit code is None if it timed out, in which case the whole process tree
    is killed (a hung test runner must not outlive the pipeline)."""
    kwargs: dict = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(
        command, shell=True, cwd=cwd, env=_safe_env(),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", **kwargs,
    )
    try:
        out, _ = proc.communicate(timeout=timeout_sec)
        return proc.returncode, out or ""
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        return None, out or ""


def _tail(text: str, lines: int = 25) -> str:
    rows = text.rstrip().splitlines()
    return "\n".join(rows[-lines:])


# GitHub Actions polling — module-level so tests can shrink them.
CI_POLL_SEC = 15
CI_FIRST_CHECK_GRACE_SEC = 180
_CI_FAILED_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}


def collect_ci_checks(token: str, repo: str, sha: str) -> list[dict]:
    """Every CI signal on a commit — GitHub Actions check runs plus classic
    commit statuses — as [{name, state: pending|success|failure, url}]."""
    checks = []
    runs = gh_api(token, "GET", f"/repos/{repo}/commits/{sha}/check-runs?per_page=100")
    for c in runs.get("check_runs", []):
        if c.get("status") != "completed":
            state = "pending"
        else:
            state = "failure" if c.get("conclusion") in _CI_FAILED_CONCLUSIONS else "success"
        checks.append({"name": c.get("name", "check"), "state": state, "url": c.get("html_url")})
    combined = gh_api(token, "GET", f"/repos/{repo}/commits/{sha}/status")
    for st in combined.get("statuses", []):
        state = {"success": "success", "pending": "pending"}.get(st.get("state"), "failure")
        checks.append({"name": st.get("context", "status"), "state": state, "url": st.get("target_url")})
    return checks


def wait_for_ci(token: str, repo: str, sha: str, timeout_sec: int, on_progress: Callable[[str], None]) -> tuple[str, list[dict]]:
    """Block until the commit's CI settles. Returns (outcome, checks) where
    outcome is passed | failed | none (no CI ever showed up) | timeout."""
    start = time.monotonic()
    last_summary = ""
    while True:
        checks = collect_ci_checks(token, repo, sha)
        elapsed = time.monotonic() - start
        if checks:
            counts = {s: sum(c["state"] == s for c in checks) for s in ("pending", "success", "failure")}
            summary = f"[CI] {counts['success']} passed · {counts['failure']} failed · {counts['pending']} running"
            if summary != last_summary:
                on_progress(summary)
                last_summary = summary
            if not counts["pending"]:
                return ("failed" if counts["failure"] else "passed"), checks
        elif elapsed > CI_FIRST_CHECK_GRACE_SEC:
            return "none", []
        if elapsed > timeout_sec:
            return "timeout", checks
        time.sleep(CI_POLL_SEC)


def gh_api(token: str, method: str, path: str, json_data: dict | None = None) -> dict | list:
    """Call GitHub REST API with the given (per-application) token."""
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    resp = requests.request(method, url, headers=headers, json=json_data, timeout=30)
    if resp.status_code >= 400:
        try:
            message = resp.json().get("message", resp.text[:300])
        except ValueError:
            message = resp.text[:300]
        raise RuntimeError(f"GitHub API {resp.status_code}: {message}")
    if resp.status_code == 204 or not resp.content:
        return {}  # e.g. a workflow_dispatch trigger
    return resp.json()


def step(msg: str):
    print(f"\n{'─' * 60}")
    print(f"  [{msg}]")
    print(f"{'─' * 60}")


# ── Pipeline ────────────────────────────────────────────────────────
def run_pipeline(
    ticket_key: Optional[str] = None,
    on_step: OnStep = _noop_on_step,
    target_repo: Optional[str] = None,
    target_branch: Optional[str] = None,
    config: Optional[PipelineConfig] = None,
    wait_for_approval: Optional[WaitForApproval] = None,
) -> dict:
    """Runs the 6-stage ticket -> PR pipeline for one ticket (Jira or Azure DevOps,
    per the application's ticket provider).

    ticket_key: if given, targets that specific ticket/work item directly.
    Otherwise picks up the next ticket matching the app's JQL (Jira) or WIQL (ADO).
    target_repo / target_branch: "owner/repo" and branch to clone + PR against.
    Falls back to the application's TARGET_REPO / TARGET_BRANCH if not given.
    config: the onboarded application's settings for this run; defaults to .env.
    wait_for_approval: blocks for a human decision when the app requires PR approval.
    Returns {status: "success", ticket_key, pr_url, modified_files} on success.
    Raises PipelineHalt(status, message) on any early stop ("failed" or "human").
    """
    if config is None:
        try:
            config = PipelineConfig.from_env()
        except ValueError as e:
            raise PipelineHalt("failed", str(e))
    cfg = config

    workspace_dir = Path(tempfile.mkdtemp(prefix="jira_bot_"))

    missing = cfg.missing_settings()
    if missing:
        raise PipelineHalt("failed", f"Missing settings for application '{cfg.app_name}': {', '.join(missing)}")

    print(f"  Application: {cfg.app_name} ({cfg.app_id}) · provider: {cfg.ticket_provider}")
    ticket = cfg.ticket_executor()
    TicketAction = cfg.ticket_action

    # ── STEP 1: FETCH ───────────────────────────────────────────────
    on_step("fetch", "in-progress")
    step("1/8  FETCH — Searching for ticket")

    if cfg.ticket_provider == "ado":
        query_desc = str(ticket_key) if ticket_key else (cfg.ticket_query or "tag 'Agent-ready', state 'New'")
        result = ticket(TicketAction(
            command="get_ticket",
            ticket_key=str(ticket_key) if ticket_key else "",
            jql_filter=cfg.ticket_query,
        ))
    else:
        query_desc = f'key = "{ticket_key}"' if ticket_key else cfg.ticket_query
        result = ticket(TicketAction(command="get_ticket", jql_filter=query_desc))

    if result.is_error or not result.result:
        msg = f"No ticket found matching: {query_desc}"
        on_step("fetch", "failed", msg)
        raise PipelineHalt("failed", msg)

    lines = result.text.strip().split("\n")
    resolved_key = result.result

    def post_comment(text: str, step_key: str, state: str) -> None:
        """Post a comment on the ticket. Any failure (bad scope, transient
        network error, etc.) is printed and pushed into that step's own log
        instead of being silently discarded — a swallowed failure here used to
        look identical to "worked fine" from the ticket's side."""
        obs = ticket(TicketAction(command="add_comment", ticket_key=resolved_key, comment_text=text))
        if obs.is_error:
            print(f"  !! Failed to post comment on {resolved_key}: {obs.text}")
            on_step(step_key, state, f"[WARN] Comment not posted to ticket: {obs.text[:200]}")

    def set_ticket_status(target_status: str, step_key: str, state: str) -> None:
        obs = ticket(TicketAction(command="update_status", ticket_key=resolved_key, target_status=target_status))
        if obs.is_error:
            print(f"  !! Failed to update status on {resolved_key}: {obs.text}")
            on_step(step_key, state, f"[WARN] Status not updated on ticket: {obs.text[:200]}")

    summary = ""
    description = ""
    status_name = ""
    in_desc = False
    desc_lines: list[str] = []
    for line in lines:
        if line.startswith("Summary:"):
            summary = line.split(":", 1)[1].strip()
        elif line.startswith("Status:"):
            status_name = line.split(":", 1)[1].strip()
        elif line.startswith("Description:"):
            in_desc = True
            desc_lines.append(line.split(":", 1)[1].strip())
        elif in_desc and line.strip() and not any(line.startswith(p) for p in ["Key:", "Summary:", "Status:", "Priority:"]):
            desc_lines.append(line.strip())
    description = "\n".join(desc_lines)

    # The ticket itself can name its target repo/branch/persona, e.g.:
    #   Repo: owner/repo
    #   Branch: main
    #   Persona: frontend-engineer
    # Repo/branch fall back to explicit run_pipeline args, then the app's
    # TARGET_REPO/TARGET_BRANCH. Persona falls back to the app's default
    # persona, then keyword-based selection (skills_store.select_persona).
    repo_match = re.search(r"(?im)^\s*repo(?:sitory)?\s*:\s*(\S+)\s*$", description)
    branch_match = re.search(r"(?im)^\s*branch\s*:\s*(\S+)\s*$", description)
    persona_match = re.search(r"(?im)^\s*persona\s*:\s*(\S+)\s*$", description)
    if repo_match:
        target_repo = repo_match.group(1)
    if branch_match:
        target_branch = branch_match.group(1)
    target_repo = target_repo or cfg.target_repo
    target_branch = target_branch or cfg.target_branch
    # Strip those lines from the working description so they don't confuse the gate/agent prompts.
    description = re.sub(r"(?im)^\s*(repo(?:sitory)?|branch|persona)\s*:\s*\S+\s*$", "", description).strip()

    known_personas = {s["slug"] for s in skills_store.list_skills() if "error" not in s}
    if persona_match and persona_match.group(1) in known_personas:
        persona = persona_match.group(1)
    elif cfg.default_persona in known_personas:
        persona = cfg.default_persona
    else:
        persona = skills_store.select_persona(summary, description)

    repo_dir = workspace_dir / target_repo.split("/")[-1]
    print(f"  Target repo: {target_repo} ({target_branch})")

    ticket_url = f"{cfg.ticket_url_base}{resolved_key}"

    print(f"  Ticket: {resolved_key}")
    print(f"  Summary: {summary}")
    print(f"  Status: {status_name}")
    print(f"  Persona: {persona}")
    on_step("fetch", "success", f"{resolved_key}: {summary} (persona: {persona})")

    # ── STEP 2: ASSIGN — hand the ticket to the bot account ────────
    on_step("assign", "in-progress")
    step("2/8  ASSIGN — Assigning ticket to bot")

    assign_result = ticket(TicketAction(command="assign_to_self", ticket_key=resolved_key))
    if assign_result.is_error:
        print(f"  !! {assign_result.text}")
        on_step("assign", "failed", assign_result.text)
    else:
        print(f"  [OK] {assign_result.text}")
        on_step("assign", "success", assign_result.text)

    # ── STEP 3: IN PROGRESS — move ticket out of the backlog ──────
    on_step("in_progress", "in-progress")
    step("3/8  IN PROGRESS — Moving ticket to in-progress")

    ip_result = ticket(TicketAction(command="update_status", ticket_key=resolved_key, target_status=cfg.status_in_progress))
    if ip_result.is_error:
        print(f"  !! {ip_result.text}")
        on_step("in_progress", "failed", ip_result.text)
    else:
        print(f"  [OK] {ip_result.text}")
        on_step("in_progress", "success", ip_result.text)

    # ── STEP 4: CLARITY GATE ───────────────────────────────────────
    on_step("gate", "in-progress")
    step("4/8  CLARITY GATE — Checking ticket detail")

    word_count = len(description.split())
    if word_count < cfg.gate_min_words:
        msg = f"Ticket description is too short ({word_count} words). Please provide more detail including specific packages, versions, and repository to modify."
        print(f"  !! {msg}")
        on_step("gate", "human", msg)
        post_comment(msg, "gate", "human")
        set_ticket_status(cfg.status_failed, "gate", "human")
        raise PipelineHalt("human", msg)

    from openhands.sdk import LLM
    from openhands.sdk.llm import Message, TextContent
    from pydantic import SecretStr

    gate_llm = LLM(
        model=cfg.azure_model,
        api_key=SecretStr(cfg.azure_key),
        base_url=cfg.azure_url,
    )

    gate_prompt = (
        "You are validating a ticket for an automation bot. "
        f"The bot will work in repo {target_repo} on branch '{target_branch}'.\n\n"
        "Does the following description have enough detail about WHAT to change "
        "(specific packages, versions, files) to implement without guessing?\n"
        "Ignore missing repo/branch/location details — those are handled by the bot configuration.\n\n"
        f"Summary: {summary}\nDescription: {description}\n\n"
        "Answer YES or NO followed by a one-sentence reason."
    )

    try:
        gate_messages = [Message(role="user", content=[TextContent(text=gate_prompt)])]
        gate_response = gate_llm.completion(messages=gate_messages)
        gate_text = ""
        if gate_response.message.content:
            first = gate_response.message.content[0]
            if isinstance(first, TextContent):
                gate_text = first.text.strip()
    except Exception as e:
        print(f"  !!! Gate LLM call failed: {e}")
        msg = f"Clarity check failed to run (technical error): {e}"
        on_step("gate", "failed", msg)
        post_comment("Clarity check failed to run (technical error) — please retry or check the bot logs", "gate", "failed")
        set_ticket_status(cfg.status_failed, "gate", "failed")
        raise PipelineHalt("failed", msg)

    gate_ok = gate_text.strip().upper().startswith("YES")
    print(f"  Gate response: {gate_text.strip()[:200]}")
    print(f"  → {'[OK] PASS' if gate_ok else '!! FAIL'}")

    if not gate_ok:
        on_step("gate", "human", gate_text.strip()[:200])
        post_comment(f"Ticket needs more detail: {gate_text.strip()}", "gate", "human")
        set_ticket_status(cfg.status_failed, "gate", "human")
        raise PipelineHalt("human", gate_text.strip())

    on_step("gate", "success", gate_text.strip()[:200])

    # ── STEP 3: AGENT RUN ─────────────────────────────────────────
    on_step("agent", "in-progress")
    step("5/8  AGENT RUN — Cloning repo and running agent")

    repo_url = f"https://{cfg.github_token}@github.com/{target_repo}.git"
    print(f"  Git clone URL: https://<token>@github.com/{target_repo}.git")
    rc, out, err = run_cmd(
        ["git", "clone", "--branch", target_branch, repo_url, str(repo_dir)],
        cwd=workspace_dir,
        silent=True,
    )
    if rc != 0:
        msg = f"Failed to clone repo: {err[:300]}"
        print(f"!! {msg}")
        on_step("agent", "failed", msg)
        post_comment(msg, "agent", "failed")
        raise PipelineHalt("failed", msg)

    # Snapshot HEAD so the verify step can detect changes the agent commits
    # itself, not just changes left uncommitted in the working tree.
    _, base_commit, _ = run_cmd(["git", "rev-parse", "HEAD"], repo_dir)
    base_commit = base_commit.strip()

    # Point 11: install the repo's dependencies first, so the agent can run
    # the tests itself while working, and the pipeline can run them after.
    test_timeout_sec = cfg.test_timeout_min * 60
    if cfg.verify_mode == "command" and cfg.test_setup_command:
        on_step("agent", "in-progress", f"[TESTS] Setting up: {cfg.test_setup_command}")
        rc_setup, setup_out = run_shell(cfg.test_setup_command, repo_dir, test_timeout_sec)
        if rc_setup != 0:
            why = "timed out" if rc_setup is None else f"exited with code {rc_setup}"
            msg = f"Test setup `{cfg.test_setup_command}` {why} — the repository's dependencies couldn't be installed."
            on_step("agent", "failed", f"[TESTS-OUTPUT]\n{_tail(setup_out)}")
            on_step("agent", "failed", msg)
            post_comment(msg, "agent", "failed")
            raise PipelineHalt("failed", msg)

    # Persona skill content, rendered with this run's variables (see
    # skills/README.md for the {{VAR}} contract) — sets the agent's operating
    # persona before it sees the specific ticket.
    persona_content = skills_store.render_skill(
        persona,
        TICKET_KEY=resolved_key,
        TICKET_SUMMARY=summary,
        TARGET_REPO=target_repo,
        TARGET_BRANCH=target_branch,
        PERSONA=persona,
    )

    # Build task from ticket
    task_prompt = (
        f"--- PERSONA: {persona} ---\n"
        f"{persona_content}\n"
        f"--- END PERSONA ---\n\n"
        f"Ticket {resolved_key}: {summary}\n\n"
        f"Description: {description}\n\n"
        f"You are working in repo: {target_repo} (branch: {target_branch})\n"
        f"The workspace is at: {repo_dir}\n\n"
        "CRITICAL: Do NOT ask me clarifying questions — just use your best judgment to interpret packages.\n"
        "Correct likely typos (e.g. 'pydentic' -> 'pydantic', 'trasformers' -> 'transformers', 'langraph' -> 'langgraph').\n"
        "IMPORTANT: Do NOT run 'pip install' or any package installation — it is too slow and unnecessary.\n"
        "IMPORTANT: Do NOT start a local dev server, HTTP server, or any other long-running/blocking "
        "process to manually test your change (e.g. 'npm run dev', 'python -m http.server') — there is "
        "no browser here to view it with, and interrupting a blocking command can crash your terminal "
        "session. Verify purely by reading the code and running 'git diff'.\n"
        "Just edit the file, verify with 'git diff', then call finish."
    )
    if cfg.verify_mode == "command":
        task_prompt += (
            f"\nTESTS: this repository is tested with `{cfg.test_command}` (dependencies are already installed). "
            "Run it after your change and fix any failures your change introduces before you finish."
        )
    if cfg.protected_paths:
        task_prompt += (
            "\nPOLICY: you must NOT create, modify, or delete any file matching these protected paths: "
            f"{', '.join(cfg.protected_paths)}. If the ticket can't be done without touching them, "
            "make no change and explain why."
        )

    from openhands.sdk import Agent, Conversation, Tool
    from openhands.tools.terminal import TerminalTool
    from openhands.tools.file_editor import FileEditorTool
    from openhands.tools.task_tracker import TaskTrackerTool

    agent_llm = LLM(
        model=cfg.azure_model,
        api_key=SecretStr(cfg.azure_key),
        base_url=cfg.azure_url,
    )

    agent = Agent(
        llm=agent_llm,
        tools=[
            Tool(name=TerminalTool.name),
            Tool(name=FileEditorTool.name),
            Tool(name=TaskTrackerTool.name),
        ],
    )

    conversation = Conversation(
        agent=agent,
        workspace=str(repo_dir),
        callbacks=[_make_agent_event_logger(on_step)],
    )

    print(f"  Agent workspace: {repo_dir}")
    print(f"  Task: {task_prompt[:200]}...")
    os.environ["GIT_TERMINAL_PROMPT"] = "0"

    try:
        conversation.send_message(task_prompt)
        conversation.run()
        print("  [OK] Agent run completed")
    except Exception as e:
        msg = f"Agent run failed: {str(e)[:300]}"
        on_step("agent", "human", msg)
        post_comment(msg, "agent", "human")
        set_ticket_status(cfg.status_failed, "agent", "human")
        raise PipelineHalt("human", msg)

    # ── STEP 4: VERIFY SUCCESS ─────────────────────────────────────
    step("6/8  VERIFY — Checking agent's work")

    # Combine three sources of change, each queried with a dedicated
    # `--name-only`-style command so every path comes back unprefixed —
    # no manual slicing of `git status --porcelain`'s status-code columns,
    # which is fragile (rename/staged/unstaged lines don't all use the same
    # column width) and previously corrupted paths (e.g. "src/app.py" -> "rc/app.py"):
    # (1) commits the agent made itself on top of base_commit, (2) tracked
    # files modified/staged but not committed, (3) brand-new untracked files.
    modified_files = []
    _, committed_stdout, _ = run_cmd(["git", "diff", "--name-only", base_commit, "HEAD"], repo_dir)
    _, unstaged_stdout, _ = run_cmd(["git", "diff", "--name-only"], repo_dir)
    _, staged_stdout, _ = run_cmd(["git", "diff", "--name-only", "--cached"], repo_dir)
    _, untracked_stdout, _ = run_cmd(["git", "ls-files", "--others", "--exclude-standard"], repo_dir)

    for stdout in (committed_stdout, unstaged_stdout, staged_stdout, untracked_stdout):
        for path in stdout.strip().split("\n"):
            path = path.strip()
            if path and path not in modified_files:
                modified_files.append(path)

    if not modified_files:
        msg = "Agent made no changes to the repository."
        print(f"  !! {msg}")
        on_step("agent", "human", msg)
        post_comment(msg, "agent", "human")
        set_ticket_status(cfg.status_failed, "agent", "human")
        raise PipelineHalt("human", msg)

    print(f"  [OK] Modified files: {modified_files}")

    # Guardrail policy — enforced on what actually changed, not just asked for
    # in the prompt. Nothing has been committed or pushed at this point.
    violations = protected_path_violations(modified_files, cfg.protected_paths)
    if violations:
        msg = (
            f"Guardrail blocked this change — the agent modified protected path(s): {', '.join(violations)} "
            f"(policy for {cfg.app_name}: {', '.join(cfg.protected_paths)}). Nothing was pushed."
        )
        print(f"  !! {msg}")
        on_step("agent", "human", f"[GUARDRAIL] {msg}")
        post_comment(msg, "agent", "human")
        set_ticket_status(cfg.status_failed, "agent", "human")
        raise PipelineHalt("human", msg)

    # Point 11: the app's own test command is the real gate — the agent
    # saying "done" isn't enough.
    verification = "No automated tests are configured for this application — review the diff."
    if cfg.verify_mode == "command":
        on_step("agent", "in-progress", f"[TESTS] Running {cfg.test_command}")
        t0 = time.monotonic()
        rc_tests, test_out = run_shell(cfg.test_command, repo_dir, test_timeout_sec)
        took = f"{time.monotonic() - t0:.0f}s"
        if rc_tests != 0:
            why = f"timed out after {cfg.test_timeout_min} min" if rc_tests is None else f"failed (exit code {rc_tests})"
            msg = f"Tests {why}: `{cfg.test_command}`. Nothing was pushed."
            print(f"  !! {msg}")
            on_step("agent", "human", f"[TESTS-OUTPUT]\n{_tail(test_out)}")
            on_step("agent", "human", f"[TESTS] ✗ {msg}")
            post_comment(f"{msg}\n\nLast lines of output:\n{_tail(test_out, 15)}", "agent", "human")
            set_ticket_status(cfg.status_failed, "agent", "human")
            raise PipelineHalt("human", msg)
        on_step("agent", "in-progress", f"[TESTS] ✓ {cfg.test_command} passed ({took})")
        verification = f"`{cfg.test_command}` passed ({took})."
    elif cfg.verify_mode == "github_actions":
        verification = "GitHub Actions results are reported on this PR."

    # Validate requirements.txt — only check the new packages the ticket asked for,
    # not the entire file (pre-existing packages like nvidia-cufile may not resolve locally).
    new_packages = [l.strip() for l in description.split("\n") if ">=" in l or "==" in l]
    if new_packages:
        failed = []
        for pkg_spec in new_packages:
            rc2, out2, err2 = run_cmd(
                [sys.executable, "-m", "pip", "install", pkg_spec, "--dry-run"],
                cwd=repo_dir,
            )
            if rc2 != 0:
                failed.append(pkg_spec)
        if failed:
            msg = f"Agent modified requirements.txt but some new packages failed pip validation: {', '.join(failed)}"
            print(f"  !! {msg}")
            on_step("agent", "human", msg)
            post_comment(msg, "agent", "human")
            set_ticket_status(cfg.status_failed, "agent", "human")
            raise PipelineHalt("human", msg)

    print("  [OK] Work verified successfully")
    on_step("agent", "success", f"Modified: {', '.join(modified_files)}")

    # ── APPROVAL GATE (per app) ────────────────────────────────────
    if cfg.require_pr_approval:
        run_cmd(["git", "add", "-A", "--intent-to-add"], repo_dir, silent=True)  # show new files in the stat
        _, diffstat, _ = run_cmd(["git", "diff", "--stat", base_commit], repo_dir, silent=True)
        summary_line = diffstat.strip().splitlines()[-1] if diffstat.strip() else f"{len(modified_files)} file(s) changed"
        on_step("pr", "human", f"[APPROVAL] Waiting for approval before opening a PR — {summary_line}: {', '.join(modified_files)}")
        if wait_for_approval is None:
            msg = f"{cfg.app_name} requires approval before a PR opens, and this run has no approver (CLI run)."
            on_step("pr", "human", msg)
            raise PipelineHalt("human", msg)
        approved, actor, note = wait_for_approval({"modified_files": modified_files, "diffstat": diffstat})
        if not approved:
            if approved is None:
                msg = "Approval request timed out — no one approved the change, so no PR was opened."
            else:
                msg = f"PR rejected by @{actor}" + (f": {note}" if note else "") + " — no PR was opened."
            print(f"  !! {msg}")
            on_step("pr", "human", msg)
            post_comment(msg, "pr", "human")
            set_ticket_status(cfg.status_failed, "pr", "human")
            raise PipelineHalt("human", msg)
        on_step("pr", "in-progress", f"Approved by @{actor}" + (f": {note}" if note else ""))
        print(f"  [OK] Approved by @{actor}")

    # ── STEP 5: PR CREATION ─────────────────────────────────────────
    on_step("pr", "in-progress")
    step("7/8  PR — Creating branch, committing, and opening PR")

    # Collapse any run of non-alphanumeric characters to a single hyphen (not
    # just spaces/periods) — a ticket title routinely has ':', '/', '(', etc.
    # (e.g. "Frontend: show live signup count...") which are invalid in a git
    # ref name and would otherwise break branch creation.
    short_desc = re.sub(r"[^a-z0-9]+", "-", summary.lower()).strip("-")[:40].strip("-")
    # Unique per run so re-running the same ticket never collides with a branch
    # (and PR) an earlier run already pushed.
    branch_name = f"{cfg.branch_prefix}{resolved_key}-{short_desc}-{uuid.uuid4().hex[:6]}"

    rc, _, err = run_cmd(["git", "checkout", "-b", branch_name], repo_dir)
    if rc != 0:
        # Fail fast here with the real reason, rather than silently continuing
        # to commit on whatever branch happens to be checked out and only
        # discovering the problem at push time ("src refspec ... does not
        # match any") — which used to be the only symptom of this failing.
        msg = f"Failed to create branch {branch_name}: {err[:300]}"
        print(f"  !! {msg}")
        on_step("pr", "failed", msg)
        post_comment(msg, "pr", "failed")
        raise PipelineHalt("failed", msg)
    run_cmd(["git", "add", "-A"], repo_dir)
    rc, _, _ = run_cmd(
        ["git", "commit", "-m", f"{resolved_key}: {summary}\n\nCloses {resolved_key}\n\nCo-authored-by: openhands <openhands@all-hands.dev>"],
        repo_dir,
    )
    if rc != 0:
        # The agent may have already committed its own changes mid-run (verified
        # above via base_commit..HEAD), in which case there's nothing left to
        # stage here - that's fine, not an error. Only halt if HEAD truly never
        # moved past base_commit.
        _, head_now, _ = run_cmd(["git", "rev-parse", "HEAD"], repo_dir)
        if head_now.strip() == base_commit:
            msg = "Nothing to commit (already up to date)"
            print(f"  ⚠️  {msg}")
            on_step("pr", "human", msg)
            post_comment(msg, "pr", "human")
            raise PipelineHalt("human", msg)
        print("  [OK] Agent already committed its changes; nothing further to stage")

    push_url = f"https://{cfg.github_token}@github.com/{target_repo}.git"
    print(f"  Git push to: https://<token>@github.com/{target_repo}.git {branch_name}")
    rc, out, err = run_cmd(["git", "push", push_url, branch_name], repo_dir)
    if rc != 0:
        msg = f"Push failed: {err[:300]}"
        print(f"  !! {msg}")
        on_step("pr", "failed", msg)
        post_comment(msg, "pr", "failed")
        raise PipelineHalt("failed", msg)

    print(f"  [OK] Pushed branch: {branch_name}")

    pr_title = f"{resolved_key}: {summary}"
    pr_body = (
        f"## Summary\n"
        f"Implements changes for ticket **{resolved_key}**.\n\n"
        f"**Ticket**: [{resolved_key}]({ticket_url})\n\n"
        f"**Changes**:\n"
        f"- Modified files: {', '.join(modified_files)}\n\n"
        f"**Verification**:\n"
        f"- {verification}\n"
        f"- Ready for human review.\n"
    )

    pr_data = gh_api(cfg.github_token, "POST", f"/repos/{target_repo}/pulls", {
        "title": pr_title,
        "head": branch_name,
        "base": target_branch,
        "body": pr_body,
    })
    pr_url = pr_data.get("html_url", "")
    print(f"  [OK] PR created: {pr_url}")

    if cfg.verify_mode == "github_actions":
        on_step("pr", "in-progress", pr_url)
        _, head_sha, _ = run_cmd(["git", "rev-parse", "HEAD"], repo_dir, silent=True)
        head_sha = head_sha.strip()
        if cfg.ci_workflow:
            try:
                gh_api(cfg.github_token, "POST", f"/repos/{target_repo}/actions/workflows/{cfg.ci_workflow}/dispatches", {"ref": branch_name})
                on_step("pr", "in-progress", f"[CI] Triggered workflow {cfg.ci_workflow} on {branch_name}")
            except RuntimeError as e:
                on_step("pr", "in-progress", f"[CI] Couldn't trigger {cfg.ci_workflow} ({e}) — waiting for push/PR checks instead")
        on_step("pr", "in-progress", f"[CI] Waiting for GitHub Actions on {head_sha[:7]}…")
        outcome, checks = wait_for_ci(
            cfg.github_token, target_repo, head_sha, test_timeout_sec,
            lambda line: on_step("pr", "in-progress", line),
        )
        if outcome != "passed":
            failed = [c["name"] for c in checks if c["state"] == "failure"]
            msg = {
                "failed": f"CI failed on the PR: {', '.join(failed)}",
                "none": "No CI checks ran for this PR — check the repository's GitHub Actions triggers",
                "timeout": f"CI didn't finish within {cfg.test_timeout_min} min",
            }[outcome]
            print(f"  !! {msg}")
            on_step("pr", "human", f"[CI] ✗ {msg}")
            post_comment(f"{msg}.\n\n**Pull Request**: {pr_url}", "pr", "human")
            set_ticket_status(cfg.status_failed, "pr", "human")
            raise PipelineHalt("human", msg, pr_url=pr_url)
        verification = f"GitHub Actions passed ({len(checks)} check{'s' if len(checks) != 1 else ''})."
        on_step("pr", "in-progress", f"[CI] ✓ {verification}")

    on_step("pr", "success", pr_url)

    # ── STEP 6: REPORT BACK ───────────────────────────────────────
    on_step("jira", "in-progress")
    step("8/8  REPORT — Updating ticket")

    completion_comment = (
        f"Agent completed work on this ticket.\n\n"
        f"**Pull Request**: {pr_url}\n\n"
        f"**Files modified**: {', '.join(modified_files)}\n"
        f"**Verification**: {verification}"
    )

    on_step("jira", "success", f"{resolved_key} -> {cfg.status_done}")
    post_comment(completion_comment, "jira", "success")
    set_ticket_status(cfg.status_done, "jira", "success")

    print(f"\n{'=' * 60}")
    print(f"  [OK] DONE — {resolved_key} is now {cfg.status_done}")
    print(f"  PR: {pr_url}")
    print(f"{'=' * 60}")

    return {"status": "success", "ticket_key": resolved_key, "pr_url": pr_url, "modified_files": modified_files}


def main():
    try:
        run_pipeline()
    except PipelineHalt as e:
        print(f"  !! {e.status.upper()}: {e.message}")
        sys.exit(0)


if __name__ == "__main__":
    main()
