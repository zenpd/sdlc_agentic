Next Things — making this a generic, multi-tenant SDLC agent platform
Condensed from the full writeup — one 2-line bullet per idea, each with a concrete example grounded in this codebase.
1. Application onboarding & multi-tenant config
Application entity — a first-class "app" record (name, team, provider choices), not just one implicit global target. eg: onboarding "Checkout Service" creates one row instead of editing the one shared .env.
Per-app config store — config_store.py becomes keyed by app_id instead of one ENV_PATH for the whole deployment. eg: get_config("checkout-svc") and get_config("billing-svc") return different Jira/ADO settings.
De-globalize jira_bot.py — config becomes a PipelineConfig object passed in, not module-level globals re-derived by _reload_config(). eg: two apps' pipelines running at once today would silently stomp each other's TICKET_PROVIDER.
Real secrets backend — move off flat-file secrets to Vault/AWS Secrets Manager/Azure Key Vault once there are N apps' worth of tokens. eg: Checkout Service's ADO PAT and Billing Service's GitHub token stop sharing one .env blast radius.
Per-app defaults — persona, branch convention, gate strictness, status names, all overridable per onboarded app, not just globally. eg: Checkout Service defaults to security-reviewer; an internal tools app defaults to backend-engineer.
2. Real authentication & persona-based login
Real auth — replace the hardcoded admin/admin check with real credentials or SSO/OIDC. eg: sign in via Entra ID the same way the onboarded Jira/ADO instances already require.
Separate User from agent Persona — a human's login identity and the coding agent's SDLC-role label are different concepts; don't conflate them. eg: user "Priya" (Maintainer role) triggers a run that uses the frontend-engineer agent persona.
RBAC — Admin / Maintainer / Runner / Viewer, gating onboarding, config edits, skill edits, and run triggers separately. eg: a Viewer can watch a run's progress but can't click "Run Pipeline" or see the ADO PAT is set.
Session tokens — real JWT/server session instead of client-only state that vanishes on refresh. eg: refreshing /dashboard today silently "logs you out" since nothing was ever actually persisted.
Audit log — attribute onboarding, config changes, and skill edits to a user, which nothing does today. eg: "who changed TARGET_BRANCH to main last Tuesday" is currently unanswerable.
3. Is an onboarded app user-specific, or shared?
Default to team-scoped apps — SDLC automation against a shared repo is a team activity, not a personal one. eg: "Checkout Service" is visible to the whole payments team, not just whoever onboarded it.
Personal preferences layered on top — default app, pinned apps, notification settings per user, without forking the app itself per user. eg: Priya's default landing app is Checkout Service; her teammate's is Billing Service — same app list underneath.
RBAC governs access, not ownership — a user without access just doesn't see the app, rather than apps being per-user silos. eg: removing someone from the payments team hides Checkout Service from their app list immediately.
Multi-team membership — a user can belong to more than one team, with an active-team switcher scoping the app list. eg: a platform engineer supporting three product teams switches context instead of juggling three logins.
4. Session & personalization persistence
Server-side preferences — persist more than the current dark-mode toggle (today's only persisted setting). eg: default app, default provider view, and table page size survive a new device/browser, not just localStorage.
Recently viewed/run — per-user history of apps and tickets touched. eg: "Recently run: ADO #616, JIRA-204" shown on login instead of starting from a blank dashboard.
Per-user notification preferences — independent of the app-level notification config. eg: Priya wants Slack DMs on failure only; a teammate wants email on every run.
Session management — active-sessions list, sign-out-everywhere. eg: revoke a stale laptop's session without rotating every credential it had access to.
5. Finer-grained, composable skills
Split persona from language/framework — a composable modifier, not N×M separate files per combination. eg: backend-engineer + an auto-detected Go modifier (from go.mod), instead of a standalone backend-engineer-go.md.
Actually use the sdlc_stage tag — today it's a display label on the Skills tab card, not something that gates which stage uses which skill. eg: a verification-tagged skill only loads for a future Verify-stage agent, not the whole run.
Per-app skill overrides — an onboarded app adds its own skill without touching the shared global library. eg: Checkout Service adds a skill for its internal payment-retry framework; no other app sees it.
Skill versioning — recover a prior version after a bad edit. eg: someone fat-fingers backend-engineer/SKILL.md and needs yesterday's version back.
Surface the SDK's skill marketplace — browse/install public skills, not only hand-author every one. eg: load_public_skills() (already in the vendored SDK) powers a "browse marketplace" button on the Skills tab.
6. Stage-specific decoupled agents
Decompose run_pipeline() — independent stage handlers (Fetch, Gate, Code, Verify, PR, Report) instead of one linear function. eg: re-run just the Verify stage after a flaky CI failure, without re-fetching the ticket or re-coding.
Per-stage model choice — cheap/fast model for the gate check, the strongest available model for the Code stage. eg: GPT-4.1-mini for the clarity gate, a frontier model only for the actual coding step.
Per-stage skill pull — each stage loads only skills tagged for its own sdlc_stage, not one persona's content for the whole run. eg: a dedicated Review stage pulls verification-tagged skills; the Code stage pulls implementation-tagged ones.
Per-app stage skipping — make stages individually enabled/disabled per onboarded app. eg: an app that always wants a human-reviewed patch (no PR) just disables the PR stage.
Pluggable stage registry — add a wholly new stage (a dedicated Test- Writing agent) without editing the core pipeline function. eg: insert a Test-Writing Agent between Code and Verify for apps that opt in, leaving others unaffected.
7. Git/repo provider abstraction
RepoExecutor/RepoAction abstraction — mirrors the existing Jira/ADO ticket-provider split, for the git-hosting side. eg: clone/push_branch/open_pr implemented once each for GitHub, GitLab, Bitbucket, Azure Repos.
Per-app repo provider config — an app declares its git host the same way it already declares its ticket provider. eg: Checkout Service is on GitLab; Billing Service is on GitHub — both run the same pipeline code.
Self-hosted instance support — configurable base URL per app instead of a hardcoded api.github.com. eg: an internal GitHub Enterprise or self-managed GitLab instance works without code changes.
8. Scale the data layer
Postgres instead of SQLite — runs_db.py's single-file, single-lock design doesn't hold up under concurrent multi-app writes. eg: two apps' pipelines finishing at the same second today both lock the same runs.db file.
Real task queue — replace the bare threading.Thread(daemon=True) per run with Celery/RQ/Arq/Temporal. eg: a backend crash mid-run today silently loses that run; a real queue would retry or recover it.
app_id-partitioned run history — index runs per app rather than one global, unscoped table. eg: "show Checkout Service's last 50 runs" becomes an indexed query instead of a full-table filter.
9. Observability, cost, and notifications
Structured logging — replace print()-into-a-text-blob with queryable structured logs per stage/app/user. eg: "all failed Gate stages for Checkout Service this week" becomes a real query, not a text grep.
Token/cost tracking per run — LiteLLM already reports usage; nothing captures it today. eg: "Checkout Service's automation cost $14 this month" surfaced on its dashboard card.
Outbound notifications — Slack/Teams/email on completion or failure, configurable per app and per user. eg: a failed run pings the owning team's Slack channel instead of only showing up if someone checks the dashboard.
Inbound webhooks — a ticket-provider webhook triggers a run automatically instead of requiring a manual click. eg: tagging a Jira ticket "Agent-ready" fires the pipeline immediately, using the JQL that already exists but is never scheduled.
10. Approval & governance workflows
Configurable human-approval gate before PR creation/merge, more fine-grained than today's single pass/fail clarity gate. eg: Checkout Service requires a human sign-off before any PR opens; an internal tools app doesn't.
Per-app guardrail policy — enforced, not just prompt text asking the agent nicely to behave. eg: "never touch */ci/* or IaC files" enforced as a policy check, not just a line in the task prompt.
11. Real CI/test integration
Wire Verify into the app's actual CI — replace the generic pip install --dry-run heuristic with the real test suite. eg: the Verify stage triggers Checkout Service's actual GitHub Actions run and waits for its result.
Per-app test/CI command config — "how do I verify this" is inherently app-specific. eg: pytest for one onboarded app, npm test for another, a full CI pipeline trigger for a third.