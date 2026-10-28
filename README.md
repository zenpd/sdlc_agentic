# Agent SDLC

Jira/Azure DevOps automation bot — picks up tickets, runs an AI coding agent
against a persona-specific skill, creates PRs, reports back.

Built on the OpenHands SDK with a Next.js dashboard UI.

---

## Project Structure

```
backend/              → FastAPI app + pipeline
  jira_bot.py            pipeline orchestration and execution
  workflow.py             canonical current-stage definitions
  stages/                 isolated stage-level operations
  jira_tool.py            Jira API wrapper
  ado_tool.py              Azure DevOps Boards API wrapper
  api_server.py            FastAPI routes
  runs_db.py                run-history persistence (SQLite)
  config_store.py           reads/writes .env-backed integration settings
  skills_store.py           persona skill discovery/selection/rendering
skills/personas/       → one SKILL.md per SDLC role (backend, frontend, QA, ...)
ui/                    → Next.js frontend (React + Tailwind)
openhands-sdk/          → vendored OpenHands SDK (workspace member)
openhands-tools/         → vendored OpenHands tools (workspace member)
SETUP.md               → full setup & architecture docs
```

## Quick Start

```bash
# install
uv sync --dev

# set env vars
# edit .env with your Azure, Jira/ADO, and GitHub credentials
# (or use the Config tab in the UI once it's running)

# run backend
uv run uvicorn backend.api_server:app --host 127.0.0.1 --port 8040

# run UI
cd ui && npm install && npm run dev
```

`ui/next.config.ts` proxies `/api/*` to `BACKEND_URL` (default
`http://localhost:8001` — pass `BACKEND_URL=http://127.0.0.1:8040 npm run dev`
if you started the backend on a different port).

## Run modes

- **Automatic** runs the existing pipeline in dependency order.
- **Assisted** runs ticket assignment, clarity analysis, verification, pull
  request creation, and the final ticket update automatically, while pausing
  for human clarification or implementation review when appropriate.
- Active Automatic runs can be stopped from the Run page. A stop
  request lets the current operation finish, prevents later stages from
  starting, and remains available after navigating away and returning.
- **Manual** keeps humans in control of stage decisions without making them
  write the implementation from scratch. Ticket assignment, clarity analysis,
  and the in-progress transition run automatically. If clarification is needed,
  the gate asks specific questions and reruns after the human responds. Once the
  ticket is clear, the coding agent loads the approved ticket, target branch,
  repository files, and repository coding instructions, then prepares a proposed
  change. A human can inspect and edit each proposed file, request an agent
  revision, or accept the code. Acceptance runs the configured checks against
  those final edited files, enforces protected paths, and saves the diff, changed
  files, branch, commit, and test result for the Verification stage. Verification
  independently reruns configured checks in an isolated worktree and asks a
  read-only reviewer to compare the committed diff with the approved ticket.
  The report shows check output, requirement coverage, and findings. A human can
  accept a successful report, submit evidence from their own verification when
  they disagree with or need to supplement the automated report, request
  implementation fixes (which returns the workflow to the coding agent), or flag
  a concern and stop. A human verification decision records the reviewer and
  evidence; protected-path rules remain enforced. Required maintainer approval is
  honored before the Manual PR stage starts. The configured GitHub integration
  publishes the reviewed branch, creates (or reuses) an open pull request, and
  confirms its URL, number, source/target branches, and changed files. A human
  then reviews the PR link and confirms before the workflow continues. If
  automatic creation fails, the task shows the error and offers retry or a
  manual URL fallback that is validated against the approved implementation.
  The final ticket stage automatically gathers the approved ticket details,
  implementation summary, verification outcome, and PR URL; moves the ticket to
  the configured final status when needed; and posts a completion comment. It
  confirms the status and comment with the ticket provider, displays generated
  evidence for human review, and only finishes after the human confirms.

---

See [SETUP.md](SETUP.md) for detailed documentation, architecture, pipeline
flow, ticket-provider configuration, persona skills, and development notes.
