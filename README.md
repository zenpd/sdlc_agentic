# Agent SDLC

Jira/Azure DevOps automation bot — picks up tickets, runs an AI coding agent
against a persona-specific skill, creates PRs, reports back.

Built on the OpenHands SDK with a Next.js dashboard UI.

---

## Project Structure

```
backend/              → FastAPI app + pipeline
  jira_bot.py            main pipeline (6-step flow)
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

---

See [SETUP.md](SETUP.md) for detailed documentation, architecture, pipeline
flow, ticket-provider configuration, persona skills, and development notes.
