# Persona skills

One `SKILL.md` per SDLC role, in the [AgentSkills](https://agentskills.io/specification)
format the OpenHands SDK natively understands
(`openhands-sdk/openhands/sdk/skills/skill.py`). These aren't bespoke to this
bot — drop any of these directories into another OpenHands-based app's
`.agents/skills/` (or `.openhands/skills/`) and the SDK's own loader picks
them up, keyword-triggers and all, with zero code changes.

```
skills/personas/<slug>/SKILL.md
```

## Frontmatter contract

```yaml
---
name: backend-engineer          # must match the parent directory name
description: >
  One paragraph: what this persona is for and when to use it.
license: MIT
metadata:
  sdlc_stage: implementation    # discovery/design/implementation/verification/release/operate
  variables: "TICKET_KEY,TICKET_SUMMARY,TARGET_REPO,TARGET_BRANCH,PERSONA"
triggers:
  - api
  - endpoint
  - service
---
```

- `name` — lowercase, hyphenated, ≤64 chars, must equal the directory name
  (enforced by the SDK's `validate_skill_name`).
- `triggers` — keywords used two ways: (1) natively, if you load this file
  into a live interactive OpenHands agent via `AgentContext(skills=[...])`,
  the SDK auto-injects the skill's content when a keyword appears in a user
  message; (2) in *this* app, `backend/skills_store.select_persona()` scores
  a ticket's summary/description against these same keywords to pick which
  persona handles a given ticket — one keyword list, two consumers.
- `metadata.variables` — self-documents which `{{VAR}}` placeholders this
  skill's body uses (see below), so an editor knows what to fill in without
  reading the whole file.

## Dynamic variables

The SDK's own trigger/injection system doesn't do template substitution —
that's this app's addition, kept deliberately simple so it stays portable:
any `{{VAR}}` token in the markdown *body* (never inside the frontmatter) is
replaced with a plain string before the content is handed to the agent.
Supported by every persona skill in this directory:

| Variable | Meaning |
|---|---|
| `{{TICKET_KEY}}` | Resolved ticket ID (Jira key or ADO work item ID) |
| `{{TICKET_SUMMARY}}` | Ticket title/summary |
| `{{TARGET_REPO}}` | `owner/repo` the agent is working in |
| `{{TARGET_BRANCH}}` | Branch the agent branches off of |
| `{{PERSONA}}` | The slug of the persona itself (useful for self-referential logging) |

A different app wiring these files in only needs to implement the same
`content.replace("{{VAR}}", value)` pass — no SDK internals required.

## Adding a persona

1. `mkdir skills/personas/<slug>` and add a `SKILL.md` following the
   frontmatter contract above.
2. Keep the body generic — no paths, tool names, or conventions specific to
   *this* repo. A persona skill should read the same whether the agent is
   pointed at a Python service or a TypeScript frontend.
3. Use the Skills tab in the dashboard, or `PUT /api/skills/<slug>`, to edit
   in place — both validate frontmatter via `Skill.load()` before saving, so
   a malformed file can't silently break the next pipeline run.
