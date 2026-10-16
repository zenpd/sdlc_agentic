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
- `metadata.sdlc_stage` — declares the kind of SDLC work this persona handles.
  It is not the pipeline step name; `skills/stages.json` maps pipeline steps
  to the SDLC tags they may load.
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

Supported SDLC tags are `discovery`, `design`, `implementation`,
`verification`, `release`, and `operate`. A skill with no tag remains visible
in the library but is not eligible for any mapped pipeline stage.

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

## Per-application skills

The shared persona library remains in `skills/personas/<slug>/SKILL.md`.
Application-specific personas and overrides live separately:

```text
skills/apps/<app_id>/personas/<slug>/SKILL.md
```

Each application sees the shared library plus its own skills. An app-local
skill with the same slug takes precedence only for that app; adding a new
slug makes that skill available only to that app. Editing a shared skill from
the Skills page saves an app-local override instead of changing the shared
file. The pipeline selects and renders skills from the run's application
scope, and skill version histories are stored separately for each app.
Deleting an application also removes its app-local skills and versions.

The Skills page uses the currently selected application. The API accepts
`app_id` on skill listing, reading, writing, creation, and version-history
routes, for example `GET /api/skills?app_id=checkout`. Omitting `app_id`
continues to address the shared library for integrations that use the API
directly.

## Skill version history

When a saved skill changes, its previous content is saved before the new
content is written. The 20 most recent prior versions are retained per skill;
shared-skill history and each app's history are stored separately. On the
Skills page, open a skill to preview an older version or restore it. A restore
is validated like any other save and first records the current content, so the
restore itself can be undone. History is available through
`GET /api/skills/<slug>/versions`; pass `app_id` to use that app's history.

## Stack modifiers (language / framework notes)

A persona says *what role* the agent plays; it should not also say *which
language* the repo uses - otherwise you would need one file per combination
(`backend-engineer-go`, `backend-engineer-python`, ...). Instead, short
**modifier** skills live in their own folder and are mixed in on top of
whichever persona was chosen:

```
skills/modifiers/<slug>/SKILL.md      e.g. python, typescript, go, java
```

After the repo is cloned, the pipeline looks for marker files and adds the
notes of every matching modifier to the agent's prompt (in a
`STACK NOTES` block, right after the persona). The markers are declared in
each modifier's own frontmatter:

```yaml
---
name: go                        # must match the directory name
description: One line about this stack.
license: MIT
metadata:
  detect_files: "go.mod,go.work"   # comma-separated file names that signal this stack
---
```

- A modifier applies if any `detect_files` name exists as a regular, non-symlink
  file at the repo root or one folder down (so `backend/pyproject.toml` in a
  monorepo counts). Dependency/build folders and hidden folders are not scanned.
- At most 3 modifiers apply per run (alphabetical), so a big monorepo cannot
  bloat the prompt. A repo that matches none gets no extra block at all.
- To support a new language: `mkdir skills/modifiers/<slug>` and add a
  `SKILL.md` like the ones there - no Python change needed.
- `GET /api/modifiers` lists what is installed.

## Pipeline-stage skill gating

There are two different stage names:

- A persona's `metadata.sdlc_stage` describes its work type, such as
  `implementation` or `verification`.
- A pipeline stage describes where an agent runs, such as `code` or `verify`.

`skills/stages.json` maps the pipeline stages to the persona tags they may
load. The shipped mapping is:

```json
{
  "code": ["implementation"],
  "verify": ["verification"]
}
```

The pipeline first selects and renders an implementation persona for the
coding agent. After the code agent finishes, a separate read-only verification
agent selects and renders an eligible verification persona, reviews the
complete diff against the ticket, and must return an explicit `VERDICT: PASS`
before the pipeline can create a pull request. The verifier receives no
repository tools, so it cannot change the implementation. Missing, malformed,
oversized, or failed reviews stop the run for human attention. The
`git diff --check` result is also required to pass. If that check reports only
an extra blank line at the end of a changed file, the pipeline removes the
extra trailing blank line and reruns the check; other whitespace or
conflict-marker errors still stop the run for human attention. The verifier
reviews tests but does not execute them; the PR and ticket report that
limitation rather than claiming tests passed.

Automatic persona selection, stage-filtered skill listing, and rendering all
use the same mapping. The configured app default and any `Persona:` ticket
override are honored only if eligible for `code`; otherwise the pipeline logs
that it is skipping the incompatible persona and chooses an eligible one. The
Verify persona is selected from skills eligible for `verify`. Untagged skills
are not eligible for a mapped stage. Invalid mapping files fail clearly rather
than silently disabling the gate.

- `GET /api/stages` returns the active pipeline-to-SDLC mapping.
- `GET /api/skills?stage=code` lists skills eligible for that pipeline stage.
- The Skills page shows which pipeline stages are allowed to load each skill.

## Public skill marketplace

The Skills page can browse AgentSkills-format entries from the public
marketplace exposed by the OpenHands SDK's `load_public_skills()` API. Select a
skill to preview its source, then import it into the currently selected
application and assign an SDLC tag enabled by `skills/stages.json`. Imports
are app-local; they do not overwrite a shared persona or install anything into
the server user's home directory. An existing skill with the same name must be
edited or removed before importing that marketplace entry.

The current pipeline consumes persona instructions as Markdown text, so the
import copies and validates `SKILL.md` only. Auxiliary marketplace resources
such as scripts and reference files are not installed or exposed to the agent.
Review public instructions before importing them.