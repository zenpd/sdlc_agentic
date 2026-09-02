import os
import re
import subprocess
import sys
import tempfile
import uuid
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
# Every setting below is re-derived by _reload_config() rather than computed
# once — called at import time (so these names exist immediately) and again
# as the first thing run_pipeline() does on every call, so a config change
# written via PUT /api/config (backend/config_store.py, which calls
# load_dotenv(override=True)) takes effect on the very next run with no
# process restart needed.
TICKET_PROVIDER: str
TicketExecutor: type
TicketAction: type
JIRA_JQL: str
ADO_WIQL: str
TICKET_QUERY: str
DEFAULT_TARGET_REPO: str
DEFAULT_TARGET_BRANCH: str
GITHUB_TOKEN: Optional[str]
AZURE_MODEL: str
AZURE_KEY: Optional[str]
AZURE_URL: Optional[str]
STATUS_FAILED: str
STATUS_DONE: str
STATUS_IN_PROGRESS: str


def _reload_config() -> None:
    """(Re)derive every config global from the current environment."""
    global TICKET_PROVIDER, TicketExecutor, TicketAction
    global JIRA_JQL, ADO_WIQL, TICKET_QUERY
    global DEFAULT_TARGET_REPO, DEFAULT_TARGET_BRANCH, GITHUB_TOKEN
    global AZURE_MODEL, AZURE_KEY, AZURE_URL
    global STATUS_FAILED, STATUS_DONE, STATUS_IN_PROGRESS

    # Which ticket tracker to pull from — "jira" (default) or "ado" (Azure DevOps Boards).
    TICKET_PROVIDER = os.getenv("TICKET_PROVIDER", "jira").strip().lower()
    if TICKET_PROVIDER not in ("jira", "ado"):
        raise RuntimeError(f"TICKET_PROVIDER must be 'jira' or 'ado', got: {TICKET_PROVIDER!r}")

    # TicketExecutor/TicketAction alias to whichever provider is active — every call site
    # below stays provider-agnostic since JiraAction and ADOAction share the same field names.
    if TICKET_PROVIDER == "ado":
        TicketExecutor = ADOExecutor
        TicketAction = ADOAction
    else:
        TicketExecutor = JiraExecutor
        TicketAction = JiraAction

    JIRA_JQL = os.getenv("JIRA_JQL", 'labels = "Agent-ready" AND status = "Ready for Agent"')
    ADO_WIQL = os.getenv("ADO_WIQL", "")  # empty -> ado_tool.DEFAULT_WIQL (tag-based)
    TICKET_QUERY = ADO_WIQL if TICKET_PROVIDER == "ado" else JIRA_JQL
    # Fallbacks only — callers (the /run page, the API) normally pass target_repo/target_branch
    # per run so this bot isn't locked to a single hardcoded repo.
    DEFAULT_TARGET_REPO = os.getenv("TARGET_REPO", "nikhilbajaj12/Lighthouse-Pharos")
    DEFAULT_TARGET_BRANCH = os.getenv("TARGET_BRANCH", "dev")
    GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
    AZURE_MODEL = f"azure/{os.getenv('AZURE_OPENAI_DEPLOYMENT', 'gpt-4.1-mini')}"
    AZURE_KEY = os.getenv("AZURE_OPENAI_API_KEY")
    AZURE_URL = os.getenv("AZURE_OPENAI_ENDPOINT")
    # Status/state names — defaults match a stock Jira workflow or a stock ADO "Task" work
    # item type; override via env if your process template uses different names.
    if TICKET_PROVIDER == "ado":
        STATUS_FAILED = os.getenv("STATUS_FAILED", "New")
        STATUS_DONE = os.getenv("STATUS_DONE", "Closed")
        STATUS_IN_PROGRESS = os.getenv("STATUS_IN_PROGRESS", "Active")
    else:
        STATUS_FAILED = os.getenv("STATUS_FAILED", "Human In Loop")
        STATUS_DONE = os.getenv("STATUS_DONE", "Done")
        STATUS_IN_PROGRESS = os.getenv("STATUS_IN_PROGRESS", "In Progress")


_reload_config()

OnStep = Callable[[str, str, Optional[str]], None]


class PipelineHalt(Exception):
    """Raised to stop the pipeline early with a final run status."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


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


def gh_api(method: str, path: str, json_data: dict | None = None) -> dict | list:
    """Call GitHub REST API with the user's token."""
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    resp = requests.request(method, url, headers=headers, json=json_data, timeout=30)
    if resp.status_code >= 400:
        body = resp.json()
        raise RuntimeError(f"GitHub API {resp.status_code}: {body.get('message', resp.text[:300])}")
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
) -> dict:
    """Runs the 6-stage ticket -> PR pipeline for one ticket (Jira or Azure DevOps,
    per TICKET_PROVIDER).

    ticket_key: if given, targets that specific ticket/work item directly.
    Otherwise picks up the next ticket matching JIRA_JQL (Jira) or ADO_WIQL (ADO).
    target_repo / target_branch: "owner/repo" and branch to clone + PR against.
    Falls back to TARGET_REPO / TARGET_BRANCH env vars if not given.
    Returns {status: "success", ticket_key, pr_url, modified_files} on success.
    Raises PipelineHalt(status, message) on any early stop ("failed" or "human").
    """
    # Picks up any config saved via PUT /api/config since this process started
    # or since the last run — see _reload_config()'s docstring above.
    _reload_config()

    workspace_dir = Path(tempfile.mkdtemp(prefix="jira_bot_"))

    required = {
        "AZURE_OPENAI_API_KEY": AZURE_KEY,
        "AZURE_OPENAI_ENDPOINT": AZURE_URL,
        "AZURE_OPENAI_DEPLOYMENT": os.getenv("AZURE_OPENAI_DEPLOYMENT"),
        "GITHUB_TOKEN": GITHUB_TOKEN,
    }
    if TICKET_PROVIDER == "ado":
        required["ADO_ORG_URL"] = os.getenv("ADO_ORG_URL")
        required["ADO_PROJECT"] = os.getenv("ADO_PROJECT")
        required["ADO_PAT"] = os.getenv("ADO_PAT")
    else:
        required["JIRA_BASE_URL"] = os.getenv("JIRA_BASE_URL")
        required["JIRA_USER_EMAIL"] = os.getenv("JIRA_USER_EMAIL")
        required["JIRA_API_TOKEN"] = os.getenv("JIRA_API_TOKEN")
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise PipelineHalt("failed", f"Missing env vars: {', '.join(missing)}")

    ticket = TicketExecutor()

    # ── STEP 1: FETCH ───────────────────────────────────────────────
    on_step("fetch", "in-progress")
    step("1/8  FETCH — Searching for ticket")

    if TICKET_PROVIDER == "ado":
        query_desc = str(ticket_key) if ticket_key else (TICKET_QUERY or "tag 'Agent-ready', state 'New'")
        result = ticket(TicketAction(
            command="get_ticket",
            ticket_key=str(ticket_key) if ticket_key else "",
            jql_filter=TICKET_QUERY,
        ))
    else:
        query_desc = f'key = "{ticket_key}"' if ticket_key else TICKET_QUERY
        result = ticket(TicketAction(command="get_ticket", jql_filter=query_desc))

    if result.is_error or not result.result:
        msg = f"No ticket found matching: {query_desc}"
        on_step("fetch", "failed", msg)
        raise PipelineHalt("failed", msg)

    lines = result.text.strip().split("\n")
    resolved_key = result.result
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
    # Repo/branch fall back to explicit run_pipeline args, then TARGET_REPO/
    # TARGET_BRANCH env vars. Persona falls back to keyword-based selection
    # (skills_store.select_persona) when not named explicitly.
    repo_match = re.search(r"(?im)^\s*repo(?:sitory)?\s*:\s*(\S+)\s*$", description)
    branch_match = re.search(r"(?im)^\s*branch\s*:\s*(\S+)\s*$", description)
    persona_match = re.search(r"(?im)^\s*persona\s*:\s*(\S+)\s*$", description)
    if repo_match:
        target_repo = repo_match.group(1)
    if branch_match:
        target_branch = branch_match.group(1)
    target_repo = target_repo or DEFAULT_TARGET_REPO
    target_branch = target_branch or DEFAULT_TARGET_BRANCH
    # Strip those lines from the working description so they don't confuse the gate/agent prompts.
    description = re.sub(r"(?im)^\s*(repo(?:sitory)?|branch|persona)\s*:\s*\S+\s*$", "", description).strip()

    if persona_match and persona_match.group(1) in {
        s["slug"] for s in skills_store.list_skills() if "error" not in s
    }:
        persona = persona_match.group(1)
    else:
        persona = skills_store.select_persona(summary, description)

    repo_dir = workspace_dir / target_repo.split("/")[-1]
    print(f"  Target repo: {target_repo} ({target_branch})")

    if TICKET_PROVIDER == "ado":
        ticket_url = f"{os.getenv('ADO_ORG_URL', '').rstrip('/')}/{os.getenv('ADO_PROJECT', '')}/_workitems/edit/{resolved_key}"
    else:
        ticket_url = f"{os.getenv('JIRA_BASE_URL', '').rstrip('/')}/browse/{resolved_key}"

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

    ip_result = ticket(TicketAction(command="update_status", ticket_key=resolved_key, target_status=STATUS_IN_PROGRESS))
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
    if word_count < 5:
        msg = f"Ticket description is too short ({word_count} words). Please provide more detail including specific packages, versions, and repository to modify."
        print(f"  !! {msg}")
        on_step("gate", "human", msg)
        ticket(TicketAction(command="add_comment", ticket_key=resolved_key, comment_text=msg))
        ticket(TicketAction(command="update_status", ticket_key=resolved_key, target_status=STATUS_FAILED))
        raise PipelineHalt("human", msg)

    from openhands.sdk import LLM
    from openhands.sdk.llm import Message, TextContent
    from pydantic import SecretStr

    gate_llm = LLM(
        model=AZURE_MODEL,
        api_key=SecretStr(AZURE_KEY),
        base_url=AZURE_URL,
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
        ticket(TicketAction(
            command="add_comment", ticket_key=resolved_key,
            comment_text="Clarity check failed to run (technical error) — please retry or check the bot logs",
        ))
        ticket(TicketAction(command="update_status", ticket_key=resolved_key, target_status=STATUS_FAILED))
        msg = f"Clarity check failed to run (technical error): {e}"
        on_step("gate", "failed", msg)
        raise PipelineHalt("failed", msg)

    gate_ok = gate_text.strip().upper().startswith("YES")
    print(f"  Gate response: {gate_text.strip()[:200]}")
    print(f"  → {'[OK] PASS' if gate_ok else '!! FAIL'}")

    if not gate_ok:
        ticket(TicketAction(command="add_comment", ticket_key=resolved_key, comment_text=f"Ticket needs more detail: {gate_text.strip()}"))
        ticket(TicketAction(command="update_status", ticket_key=resolved_key, target_status=STATUS_FAILED))
        on_step("gate", "human", gate_text.strip()[:200])
        raise PipelineHalt("human", gate_text.strip())

    on_step("gate", "success", gate_text.strip()[:200])

    # ── STEP 3: AGENT RUN ─────────────────────────────────────────
    on_step("agent", "in-progress")
    step("5/8  AGENT RUN — Cloning repo and running agent")

    repo_url = f"https://{GITHUB_TOKEN}@github.com/{target_repo}.git"
    print(f"  Git clone URL: https://<token>@github.com/{target_repo}.git")
    rc, out, err = run_cmd(
        ["git", "clone", "--branch", target_branch, repo_url, str(repo_dir)],
        cwd=workspace_dir,
        silent=True,
    )
    if rc != 0:
        msg = f"Failed to clone repo: {err[:300]}"
        print(f"!! {msg}")
        ticket(TicketAction(command="add_comment", ticket_key=resolved_key, comment_text=msg))
        on_step("agent", "failed", msg)
        raise PipelineHalt("failed", msg)

    # Snapshot HEAD so the verify step can detect changes the agent commits
    # itself, not just changes left uncommitted in the working tree.
    _, base_commit, _ = run_cmd(["git", "rev-parse", "HEAD"], repo_dir)
    base_commit = base_commit.strip()

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

    from openhands.sdk import Agent, Conversation, Tool
    from openhands.tools.terminal import TerminalTool
    from openhands.tools.file_editor import FileEditorTool
    from openhands.tools.task_tracker import TaskTrackerTool

    agent_llm = LLM(
        model=AZURE_MODEL,
        api_key=SecretStr(AZURE_KEY),
        base_url=AZURE_URL,
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
        ticket(TicketAction(command="add_comment", ticket_key=resolved_key, comment_text=msg))
        ticket(TicketAction(command="update_status", ticket_key=resolved_key, target_status=STATUS_FAILED))
        on_step("agent", "human", msg)
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
        ticket(TicketAction(command="add_comment", ticket_key=resolved_key, comment_text=msg))
        ticket(TicketAction(command="update_status", ticket_key=resolved_key, target_status=STATUS_FAILED))
        on_step("agent", "human", msg)
        raise PipelineHalt("human", msg)

    print(f"  [OK] Modified files: {modified_files}")

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
            ticket(TicketAction(command="add_comment", ticket_key=resolved_key, comment_text=msg))
            ticket(TicketAction(command="update_status", ticket_key=resolved_key, target_status=STATUS_FAILED))
            on_step("agent", "human", msg)
            raise PipelineHalt("human", msg)

    print("  [OK] Work verified successfully")
    on_step("agent", "success", f"Modified: {', '.join(modified_files)}")

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
    branch_name = f"feat/{resolved_key}-{short_desc}-{uuid.uuid4().hex[:6]}"

    rc, _, err = run_cmd(["git", "checkout", "-b", branch_name], repo_dir)
    if rc != 0:
        # Fail fast here with the real reason, rather than silently continuing
        # to commit on whatever branch happens to be checked out and only
        # discovering the problem at push time ("src refspec ... does not
        # match any") — which used to be the only symptom of this failing.
        msg = f"Failed to create branch {branch_name}: {err[:300]}"
        print(f"  !! {msg}")
        ticket(TicketAction(command="add_comment", ticket_key=resolved_key, comment_text=msg))
        on_step("pr", "failed", msg)
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
            raise PipelineHalt("human", msg)
        print("  [OK] Agent already committed its changes; nothing further to stage")

    push_url = f"https://{GITHUB_TOKEN}@github.com/{target_repo}.git"
    print(f"  Git push to: https://<token>@github.com/{target_repo}.git {branch_name}")
    rc, out, err = run_cmd(["git", "push", push_url, branch_name], repo_dir)
    if rc != 0:
        msg = f"Push failed: {err[:300]}"
        print(f"  !! {msg}")
        ticket(TicketAction(command="add_comment", ticket_key=resolved_key, comment_text=msg))
        on_step("pr", "failed", msg)
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
        f"- Tests passing: [OK]\n"
        f"- Ready for human review.\n"
    )

    pr_data = gh_api("POST", f"/repos/{target_repo}/pulls", {
        "title": pr_title,
        "head": branch_name,
        "base": target_branch,
        "body": pr_body,
    })
    pr_url = pr_data.get("html_url", "")
    print(f"  [OK] PR created: {pr_url}")
    on_step("pr", "success", pr_url)

    # ── STEP 6: REPORT BACK ───────────────────────────────────────
    on_step("jira", "in-progress")
    step("8/8  REPORT — Updating ticket")

    comment = (
        f"Agent completed work on this ticket.\n\n"
        f"**Pull Request**: {pr_url}\n\n"
        f"**Files modified**: {', '.join(modified_files)}\n"
        f"Tests verified: [OK]"
    )

    ticket(TicketAction(command="add_comment", ticket_key=resolved_key, comment_text=comment))
    ticket(TicketAction(command="update_status", ticket_key=resolved_key, target_status=STATUS_DONE))
    on_step("jira", "success", f"{resolved_key} -> {STATUS_DONE}")

    print(f"\n{'=' * 60}")
    print(f"  [OK] DONE — {resolved_key} is now {STATUS_DONE}")
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
