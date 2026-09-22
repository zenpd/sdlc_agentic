# Agent SDLC — Live Demo Pitch

A speaking script for demoing the platform, structured the same way as our other product pitches: a problem statement with no clicking, then a screen-by-screen walkthrough with what to say at each step.

---

## 0. Problem Statement (2 minutes, no clicking — just talk)

"Before we start the demo, let me explain the problem we're addressing.

Every engineering team runs the same loop hundreds of times a week: a ticket gets filed in Jira or Azure DevOps, a developer picks it up, re-reads the description, figures out which files and repo it actually touches, writes the code, writes tests for it, and opens a pull request. Most of that loop isn't hard — it's just repetitive, and it's the single biggest source of cycle-time drag in a sprint.

It also doesn't scale evenly. Some tickets are perfectly clear and could be done in minutes. Others are vague, and a developer burns half a day just figuring out what was actually meant before writing a line of code. Today, both kinds of tickets sit in the same backlog, competing for the same developer attention.

That's where our platform comes in. It watches your ticket tracker, picks up tickets that are ready for automation, and takes them all the way from ticket to an opened, review-ready pull request — without a human touching the keyboard in between. And when a ticket genuinely isn't clear enough to act on safely, it says so, explains why, and hands it back to a human instead of guessing.

Let me walk you through it."

---

## 1. Login — "no manual tokens, ever"

*(Open the app — it lands on the Login screen.)*

"The very first thing a user does is sign in with their own GitHub account — that's it, one click, no manual token to generate or paste anywhere."

*(Click "Login with GitHub", approve the GitHub authorization screen.)*

"That's the whole onboarding step. No PAT to create, no scopes to figure out, no copy-pasting a secret into a config file. Once you're in, everything else in the app is unlocked."

---

## 2. Dashboard (Home)

*(Land on the home/dashboard screen after login.)*

"This is the home screen — the front door of the platform once you're signed in. From here you can launch a run directly, or head over to Logs to see everything that's already happened."

"Under the hood, every run this platform executes goes through the same seven-stage pipeline, fully automated:"

- **Fetch** — pulls the ticket's title, description, and status straight from Jira or Azure DevOps.
- **Assign** — takes ownership of the ticket on the tracker so it's clear it's being worked.
- **In Progress** — moves the ticket's status forward automatically.
- **Clarity Gate** — an LLM check that decides whether the ticket has *enough detail* to act on safely, before any code gets touched.
- **Agent** — an autonomous coding agent clones the target repo, writes the change, and verifies its own work.
- **PR** — opens a real, review-ready pull request with the diff.
- **Update Ticket** — reports back to the ticket: links the PR, and moves the ticket's status to done.

"Every one of those seven stages is visible live while it's running — that's the next thing I want to show you."

---

## 3. Applications — connect a repo without a token

*(Click "Applications" in the sidebar.)*

"This is where you tell the platform which codebase to work on. Since we're already logged in with GitHub, it lists every repository we have access to — no need to know or type a repo name."

*(Search/filter the repo list, click "Use this repo" on one.)*

"One click, and that's the active target. No manual token, no copy-pasting a repo path — the same GitHub session from login is reused to clone, push, and open the pull request."

---

## 4. Run — a single ticket, live, in full detail

*(Click "Run" in the sidebar, enter a ticket ID, click "Run Pipeline".)*

"Let's run one ticket live. Watch the top of the screen — that's the seven-stage pipeline I just described, animating in real time as each stage completes."

*(Point at the stage stepper as it progresses.)*

"And below it, this is the part most platforms hide from you entirely — the actual agent trace. As the agent works, we show which tool it's using, which file it's editing, and — this is the detail that matters — **exactly which lines it's changing**, with a real unified diff, live, as it happens. Not 'the agent is working,' but the actual code, line by line, as it's written."

*(Let it run to completion — PR link appears.)*

"And there's the pull request, opened automatically, ready for a human to review."

---

## 5. Human-in-the-Loop — when the platform says "not yet"

*(Trigger a second run against a ticket with a deliberately vague description.)*

"Now let's look at the other case — a ticket that isn't ready. This one has almost no detail in it. Watch what happens at the Clarity Gate stage."

*(Point at the amber "Human Review Required" banner as it appears.)*

"Instead of guessing and producing the wrong code, the platform stops itself right here, explains exactly why — in this case, the description doesn't say what to change — and posts that reason back as a comment directly on the ticket, so whoever owns it knows precisely what to fix."

*(Optionally switch to the actual Jira/ADO ticket to show the posted comment.)*

"This is the safety net that makes full automation trustworthy: it only ships code when it's actually confident, and it's transparent the moment it isn't."

---

## 6. Bulk Request — many tickets, truly in parallel

*(Click "Bulk Request" in the sidebar, add several ticket IDs, click "Run All".)*

"So far we've looked at one ticket at a time. In practice, a team has a whole backlog ready to go at once. This page lets you queue any number of tickets and run them **simultaneously** — not one after another. Each one gets its own isolated workspace, so five tickets touching five different files (or even the same file) never interfere with each other."

*(Point at multiple cards updating status concurrently.)*

"Every card here updates independently and in real time, and each one is a link straight into its own detailed trace."

---

## 7. Logs — the full history, at a glance and in depth

*(Click "Logs" in the sidebar.)*

"This is the operational view — for a lead or a manager, not just the person who triggered a run. At the top: total runs, how many succeeded, failed, or needed a human, and a seven-day trend of the team's throughput."

*(Scroll to the History section, click a row to expand it.)*

"And below that is the full history — every run the platform has ever executed. Click into any one of them, and you get the exact same live trace view we saw during the run — persona used, every tool call, every diff, after the fact. Nothing about a run is opaque or lost once it finishes."

---

## 8. Skills — how the platform knows *who* should do the work

"One thing worth calling out: the platform doesn't send every ticket to a generic coding agent. It routes each ticket to a specialized persona based on what the ticket is actually about — backend engineer, frontend engineer, QA engineer, security reviewer, or solution architect — each with its own scoped expertise and behavior."

*(Click "Skills" in the sidebar, open one persona.)*

"These personas are fully editable — a team can tune exactly how the 'security reviewer' persona thinks, or add an entirely new one, without touching any code."

---

## 9. Config — pluggable into any team's stack

*(Click "Config" in the sidebar.)*

"Last thing — this is what makes the platform pluggable into a real environment rather than a one-off demo. Ticket tracker: Jira or Azure DevOps, your choice, same behavior either way. Target repository and branch. And the LLM provider behind the agent, configurable per deployment."

"Nothing here is hardcoded to one company's stack — swap any one of these and the same seven-stage pipeline, the same live trace, the same human-in-the-loop safety net, all keep working exactly the same way."

---

## Closing

"So to recap: a ticket comes in, the platform decides whether it's clear enough to act on, and if it is, an autonomous agent writes the code, tests it, and opens a pull request — live, fully visible, down to the line. If it isn't clear enough, it says so, explains why, and hands it back — automatically, on the ticket itself.

No manual token setup, no black box, and it scales from one ticket to an entire backlog running in parallel.

That's the Agent SDLC platform. Thank you."
