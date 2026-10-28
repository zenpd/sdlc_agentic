# Agent SDLC

Jira automation bot powered by OpenHands SDK. Picks up tickets, runs an AI coding agent, creates PRs, and reports back — all the way from Jira to merged.

---

## Architecture

```
jira_bot.py             → pipeline execution and provider-neutral coordination
workflow.py             → canonical stage definitions for the current workflow
stages/ticket.py         → ticket parsing and routing normalization
jira_tool.py / ado_tool.py → ticket-provider adapters
ui/                     → Next.js dashboard
openhands-sdk/          → vendored SDK workspace package
openhands-tools/        → vendored tools workspace package
```

### Pipeline Flow

| Step | Name | What happens |
|------|------|-------------|
| 1 | **Fetch** | Reads a Jira or Azure DevOps ticket and normalizes its details |
| 2 | **Assign** | Ensures the ticket is assigned to the configured target user |
| 3 | **Clarity Gate** | Automatically analyzes requirements; asks specific human clarification questions if needed, then rechecks |
| 4 | **In progress** | After the gate passes, moves the ticket to its active state before implementation |
| 5 | **Implementation** | OpenHands edits the target repository |
| 6 | **Verification** | Independently reruns configured checks and reviews the approved diff against ticket requirements |
| 7 | **Pull request** | Pushes the branch, opens a PR, and optionally waits for CI |
| 8 | **Report** | Comments on the ticket and updates its final status |

The current flow remains sequential and preserves its existing behavior. Stage
metadata is centralized in `backend/workflow.py`, and ticket parsing/routing
normalization is isolated in `backend/stages/ticket.py`.

### Run modes

Users can select a mode when starting a run:

- **Automatic** runs every stage in the current sequence. Configured approval
  and existing error/safety conditions can still pause or stop it.
- **Assisted** creates one persisted human task at a time for stages needing
  human input, while automatically running configured integrations and checks.
  Ticket assignment follows Automatic mode: it uses the configured
  target user, or the integration account when no target is configured. After
  the clarity gate passes, the ticket is moved to its active state automatically before implementation.
  The clarity gate analyzes the ticket automatically. If it needs clarification,
  it pauses with specific questions and suggested summary/description; the human
  can edit those fields, add relevant context, and provide answers. The gate runs
  again and decides whether work can continue—there is no manual pass override.
  When clear, the coding agent loads the approved ticket summary and description,
  clones the configured target branch, and follows repository coding instructions
  before proposing code. The human can review and edit each proposed file and
  apply edits to the implementation workspace, request another coding-agent
  revision, or accept the code. Acceptance runs the configured
  setup and test commands against the final human-approved files, checks diff
  hygiene and protected paths, and saves the branch, local commit, changed files,
  final diff, and test result as Verification-stage context. Verification then
  independently reruns configured checks in an isolated Git worktree and uses a
  read-only reviewer to compare the final diff with the approved requirements.
  Its report displays check outcomes and output, requirement coverage, and
  findings and explains why the outcome passed or failed. A human can accept a
  successful report, submit evidence from their own verification when they
  disagree with or need to supplement the automated report, request fixes
  (returning to the coding agent), or flag a concern and stop the run. Human
  verification decisions are recorded with reviewer and evidence; protected
  paths remain enforced. If maintainer approval is configured, it is requested
  before any branch is published or PR is created. After approval, the configured
  GitHub integration publishes the reviewed branch and creates (or reuses) an
  open pull request to the configured target branch. The UI displays its URL,
  number, source and target branches, and creation evidence. The service verifies
  the PR is open and matches the approved commit, repository, branches, and
  changed files, then automatically continues to the final ticket update. On
  creation failure, the task preserves the error and offers retry or a manual
  URL fallback that receives the same validation.
  The final ticket stage automatically composes a comment from
  the approved ticket requirements, implementation summary and changed files,
  verification outcome, and PR URL. It checks the ticket's current status,
  performs the configured final transition only if needed, writes the comment,
  and re-reads the ticket and its comments to confirm the result. The generated
  status, PR link, comment, and evidence are recorded automatically; provider
  confirmation completes the run without another human click. Failed updates
  remain retryable, and a run-specific marker prevents duplicate completion
  comments on retry.
  Configure linting and type checks as part of the application's test command
  when they should run as required verification checks.
  If the backend restarts during agent generation, validation, verification, or
  PR creation or ticket update, the interrupted work is surfaced as a retryable
  human task instead of remaining stuck.

Automatic mode remains the default for API and webhook-started runs. Configured
per-app approval before PR publication remains available in either mode.
Arbitrary per-stage checkpoint configuration and durable recovery of automated
runs are future work. Active Automatic runs can be stopped from the Run page.
Cancellation is cooperative: an in-flight operation is allowed to finish, but
no later stage will start. The active run can be restored on the Run page after
navigating away.

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
# Optional per-stage deployment overrides (blank/unset uses AZURE_OPENAI_DEPLOYMENT)
# AZURE_OPENAI_CLARITY_GATE_DEPLOYMENT="gpt-4.1-mini"
# AZURE_OPENAI_CODING_DEPLOYMENT="your-coding-deployment"
# AZURE_OPENAI_VERIFICATION_DEPLOYMENT="your-review-deployment"

# Jira
JIRA_BASE_URL=https://<your-domain>.atlassian.net
JIRA_USER_EMAIL=<your-email>
JIRA_API_TOKEN=<your-token>
TARGET_ASSIGNEE=<Jira account ID or email>

# GitHub
GITHUB_TOKEN=<github-pat>
```

`TARGET_ASSIGNEE` is the user who should own tickets during a run. Azure DevOps
accepts an identity such as an email address; Jira accepts a unique account ID
or email. If it is unset, the integration assigns to its own account, as in
Automatic mode. Assignment is skipped when the ticket already has that user.
The workflow moves the ticket to the configured in-progress status only after
the clarity gate passes, immediately before implementation starts.

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
- `POST /api/runs/{id}/cancel` — request cancellation of an active Automatic run
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
Usage is attributed to the month it is reported, not the month the run began.
The dashboard shows that monthly estimate; the Logs page shows the usage for
the selected run.

Costs are labeled as estimated because provider/model pricing can change. If
LiteLLM cannot calculate a cost for every completion, the run is marked
partial; if no completion has a known price, cost is unavailable rather than
reported as zero. Token usage and any available partial estimate are still
retained. Existing run-level totals are preserved and assigned to the run's
start month, the best available attribution for usage without stage timestamps.

The Manual implementation score is a separate, evidence-based quality gate,
not the coding agent's self-rating. It awards up to 100 points: ticket
requirement coverage and correctness (25), language-aware syntax validation
(15), static quality (10), scope and safety (10), implementation robustness
(20), independent regression review (10), and security assessment (10). The
independent reviewer considers whether each ticket requirement is implemented
correctly based on visible code evidence, in addition to checking static
quality, scope/safety, robustness, regression risk, and security across the
changed languages. Python changes also receive built-in AST quality/security
checks.

The robustness review assesses implementation logic (8 points), edge-case
handling (6), and integration quality (6). Each area receives its full
allocation when strong, half when partial, and zero when weak. Scope/safety
assesses whether the change stays within the approved ticket and avoids
unsupported destructive behavior, data loss, access-control weakening, or
unsafe operational side effects. No findings earns 10 points, minor findings
earn 8, major findings earn 5 and require human review, and critical findings
earn zero and block review skipping. This replaces the previous automated-test
category; scoring no longer depends on a repository-specific test command.
Validation still runs separately when the user accepts the implementation.

Syntax validation uses safe, non-executing parsers/checkers when available:
Python, JavaScript/JSX, TypeScript/TSX, Java, Go, Rust, Ruby, PHP, shell,
JSON, and TOML. Several checkers require their corresponding toolchain (for
example Node.js plus the repository's TypeScript package, a JDK, Go's `gofmt`,
Rust's `rustfmt`, Ruby, PHP, or Bash). If a changed source file has no compatible
checker or a required tool is unavailable, the syntax category earns zero and
the evaluation cannot skip human code review. Documentation-only changes do
not require a source syntax checker. Other source languages are reported as
unsupported rather than passed.

The score always uses the fixed 100-point scale: checks that cannot run or
return valid evidence earn zero and are never removed from the denominator. The
UI separately shows how much check weight produced evidence. Security checks
are evidence-based heuristics, not a full security audit. Static checks run in
a temporary Git worktree and do not run project build scripts or execute the
changed application code. Skipping code review requires complete evidence for
all six categories, a score of at least 85, and no critical failures. A syntax
failure or critical security finding blocks skipping code review regardless of
score.

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
