# Agent SDLC

Jira automation bot powered by OpenHands SDK. Picks up tickets, runs an AI coding agent, creates PRs, and reports back — all the way from Jira to merged.

---

## Architecture

```
jira_bot.py          → main pipeline (6-step flow)
jira_tool.py         → Jira API wrapper
ui/                  → Next.js frontend (React + Tailwind)
openhands-sdk/       → SDK package (installed dependency)
openhands-tools/     → Tool package (installed dependency)
```

### Pipeline Flow

| Step | Name | What happens |
|------|------|-------------|
| 1 | **Fetch** | JQL query picks up an `Agent-ready` ticket |
| 2 | **Clarity Gate** | LLM validates the description has enough detail |
| 3 | **Agent Run** | Clones repo, AI agent edits code, runs verification |
| 4 | **Verify** | Checks `git diff` for modifications + pip dry-run |
| 5 | **PR** | Creates branch, commits, pushes, opens GitHub PR |
| 6 | **Report** | Comments on Jira, transitions status to Done |

---

## Setup

### Prerequisites

- Python 3.12
- Azure OpenAI deployment (GPT-4.1-mini)
- Jira account with API token
- GitHub PAT with `Contents: Write`

### Environment Variables

Create `.env` in the project root:

```env
# Azure OpenAI
AZURE_OPENAI_ENDPOINT="https://<your-endpoint>.cognitiveservices.azure.com/"
AZURE_OPENAI_API_KEY=<your-key>
AZURE_OPENAI_DEPLOYMENT="gpt-4.1-mini"

# Jira
JIRA_BASE_URL=https://<your-domain>.atlassian.net
JIRA_USER_EMAIL=<your-email>
JIRA_API_TOKEN=<your-token>

# GitHub
GITHUB_TOKEN=<github-pat>
```

### Install

```bash
uv sync --dev
```

### Run Bot

```bash
uv run python jira_bot.py
```

---

## Frontend (Next.js)

A dashboard UI to visualize and trigger pipelines.

### Pages

| Route | Description |
|-------|-------------|
| `/` | Login page — pull the lamp chain, enter `admin`/`admin` |
| `/dashboard` | Stats cards + recent runs table |
| `/run` | Input a Jira ticket key, run pipeline, watch progress |
| `/logs` | Search/filter past runs, expand for step-by-step logs |

### Run Dev Server

```bash
cd ui
npm install
npm run dev
```

### Design

Two distinct design systems:

- **Login page** — dark brass/warm theme with animated SVG lamp, chain pull, dust particles (from `loginpage.html`)
- **All other pages** — light purple/indigo theme with grid background, orbs, step cards (from `dasboard.html`)

---

## Key Decisions

- **SDK over full OpenHands app** — lighter, no React/Docker overhead, fits a headless bot
- **Clarity gate** — LLM validates ticket has ≥5 words and sufficient detail
- **Gate fail-loudly** — on LLM error, ticket is NOT sent to agent; posts distinct Jira comment
- **pip dry-run** — checks only new packages from the ticket, not entire `requirements.txt`
- **Two design systems** — login page is standalone, rest of UI uses separate theme
- **GitHub PAT** — fine-grained token with `Contents: Write` on target repo

---

## Development Notes

### Run API

- `POST /api/run-ticket` — start a pipeline run
- `GET /api/run-status/{id}` — poll run progress
- `GET /api/stats` — aggregate run stats
- `GET /api/runs` — list all runs
- `GET /api/events` — query structured run events
- `GET /api/events/summary` — summarize final stage outcomes

### Structured Run Events

Pipeline stage updates and meaningful pipeline progress messages are written
as individual rows in the same SQLite database as run history. Each event
includes the run, application, optional user, stage, state, severity, message,
and timestamp. Routine console text and raw subprocess output are not persisted;
this avoids mixing unstructured output or credential-bearing command details
into the queryable event stream. Existing run-status responses remain unchanged.
Query events through:

- `GET /api/events` — newest events first; filter by `app_id`, `user_id`,
  `stage`, `state`, `level`, `run_id`, `ticket_key`, `since`, and `until`.
  Filters such as `stage` and `state` accept comma-separated values.
- `GET /api/events/summary` — counts of final stage outcomes, optionally
  filtered by application, user, or start time.

For example, `/api/events?app_id=checkout&stage=gate&state=failed,human&since=7d`
lists failed or human-needed Gate events for the Checkout application from the
last seven days. Use `include_trace=true` to include the agent's tool chatter;
trace messages are hidden by default. Limits default to 100 and can be set up
to 500.

When a run is started from a browser session connected through GitHub OAuth,
the GitHub login is recorded as `user_id`. Runs started without that session
have no user attribution.

### Per-run LLM usage and cost

LiteLLM/OpenHands usage metrics are saved to each run as prompt tokens,
completion tokens, total tokens, and estimated USD cost when the configured
model has pricing data. The run-history API includes these fields, and
`GET /api/stats` includes current-month totals for the selected application.
The dashboard shows that monthly estimate; the Logs page shows the usage for
the selected run.

Costs are labeled as estimated because provider/model pricing can change. If
LiteLLM cannot calculate a cost for every completion, the run is marked
partial; if no completion has a known price, cost is unavailable rather than
reported as zero. Token usage and any available partial estimate are still
retained. Existing runs are not backfilled.

### Outbound run notifications

Each application can send run completion and failure notifications to a shared
team destination. Configure these in **Integration Config → Outbound
notifications** for the selected application (or set server defaults for apps
that inherit them):

- Enable notifications separately for successful runs and failed/paused runs.
- Add a Slack incoming webhook URL, a Teams incoming webhook URL, and/or
  comma-separated email recipients.
- Email uses SMTP host, port (default `587`), sender, and optionally a username
  and password. STARTTLS is enabled by default.

Webhook URLs and the SMTP password are stored as secrets and are never returned
to the browser. A blank app-level field inherits the server default. Delivery
errors are logged with the app, run, and channel but do not change the pipeline
result. Notifications are opt-in and are not sent until an outcome and a
destination are configured.

### Inbound Jira webhooks

Jira can start a run automatically through `POST /api/webhooks/jira/{app_id}`.
The endpoint requires an `Authorization: Bearer <token>` header, accepts Jira
issue-created/updated payloads, and checks the issue against that application's
configured JQL before starting the pipeline. Replayed deliveries with the same
`X-Atlassian-Webhook-Identifier` are ignored, and a ticket already running for
the same app is not queued a second time.

Configure the app's **Integration Config → Inbound Jira webhook** bearer token.
In Jira Automation, create a rule with a label-change trigger, conditions for
the `Agent-ready` label and ready status, then add **Send web request**:

- Method: `POST`
- URL: `https://<public-backend-host>/api/webhooks/jira/<app_id>`
- Header: `Authorization: Bearer <the configured token>`
- Body:

  ```json
  {
    "webhookEvent": "jira:issue_updated",
    "issue": { "key": "{{issue.key}}" }
  }
  ```

Use the application's exact configured JQL as the final qualification check;
the Jira Automation conditions are an early filter, not a replacement. Jira's
cloud service cannot call a computer's `localhost` address. For local testing,
run a secured HTTPS tunnel to backend port 8040 and use its public HTTPS
forwarding URL in the rule. Stop the tunnel when testing is finished. A deployed
HTTPS backend is the recommended production arrangement.

The endpoint returns `202` when it queues a run, `200` when a valid event is
ignored (for example, the issue does not match the JQL or is a duplicate), and
an error status for invalid credentials or configuration. Webhook-triggered
runs have no signed-in dashboard user attribution.

### Inbound Azure DevOps webhooks

Azure DevOps can start a run through `POST /api/webhooks/ado/{app_id}`. The
endpoint accepts work-item-created and work-item-updated service-hook events,
authenticates them with HTTP Basic authentication, and checks the work item
against the application's configured WIQL (or the built-in `Agent-ready` tag
and `New` state query when no WIQL override is configured). Replayed event IDs
and tickets already running for the same application are ignored.

Set a **Service Hook password** in the app's **Integration Config → Inbound
Azure DevOps webhook** section. In Azure DevOps, open **Project settings →
Service hooks**, create a **Web Hooks** subscription for **Work item created**
and/or **Work item updated**, then configure:

- URL: `https://<public-backend-host>/api/webhooks/ado/<app_id>`
- Authentication: **Basic**, username `sdlc-agentic`, password the configured
  service-hook password
- Resource details: **All** or **Minimal** (the event must include
  `resource.id`)

Azure DevOps conditions can filter to the desired tag/state, but the
application's WIQL remains the final qualification check. Azure DevOps requires
a public HTTPS endpoint and cannot call `localhost`; for local testing, use a
secured HTTPS tunnel forwarding to backend port 8040 and use that public URL
when creating the service hook.

### Model

Uses `azure/gpt-4.1-mini` via LiteLLM. Cost per full pipeline run: ~$0.058.

### Git

```bash
git remote -v          # check remotes
git push origin main   # push to current repo
```
