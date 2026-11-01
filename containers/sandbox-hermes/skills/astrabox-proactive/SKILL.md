---
name: astrabox-proactive
description: "Offer and set up work the user wants done without asking each time: routines at a time they name (morning briefings, weekly reviews, meeting prep) and quiet watches that speak up only when something matters (important mail, a reply, a deadline)."
version: 0.2.0
author: AstraBox
license: Apache-2.0
metadata:
  hermes:
    tags: [Automation, Schedule, Cron, Briefing, Monitor, Proactive]
    related_skills: [email-inbox-triage]
---

# Routines and watches

You run inside AstraBox as the user's long-lived assistant. You can do work for
them without being asked each time. Use this skill to notice when that would
help, offer it, and set it up so it keeps working on its own.

## When to use

- The user names a time or a frequency: "every morning", "each Friday at 5",
  "before my meetings" — a **routine**.
- The user asks you to tell them when something happens: an email from
  someone, a price, a deadline, a reply, "keep an eye on" — a **watch**.
- You just finished a task the user is likely to need again (a summary of
  today's mail, an agenda for today) — offer once to make it a routine.

Do not use it to schedule one-off work the user wants now; do that now.

## Two kinds, two tools

The kind decides the tool. The user manages the two in different places, so a
routine set up as a watch is one they cannot find.

| | Routine | Watch |
|---|---|---|
| The user said | a time or a frequency | "tell me when", "keep an eye on" |
| Tool | `create_schedule` | `cronjob_manage` with `action="create"` |
| Each run | always replies | replies only when something meets the bar |
| Where it arrives | your main conversation | the conversation that created it |
| The user manages it | Personal schedules, in the account menu | the Proactive switch in your profile, or by asking you |

- **Routines.** `create_schedule` without `agent_id` makes you run it; each
  run is a message in your main conversation, and the reply goes there and to
  the messaging chat that conversation belongs to. With `agent_id`, that Agent
  runs it in a conversation of its own. `list_schedules`, `update_schedule`
  (pause with `{"enabled": false}`, resume with `true`) and `delete_schedule`
  change it later.
- **Watches.** A job you schedule for yourself. It reports into the
  conversation that created it. When the owner switches Proactive off in your
  profile, none of these jobs runs until they switch it on again.
- **The user's clock.** Schedules are read in the user's timezone, the one
  named in the "Conversation started" line of your system prompt. "8 in the
  morning" is `0 8 * * *`.
- **The user's apps.** Work apps the user connected (Gmail, Google Calendar,
  Google Drive, Notion, Linear, Jira, Confluence, GitHub, Slack) are MCP
  tools named after the app, acting as the user. `list_connections` says which
  are connected; `request_connection` shows the user a card to connect one.
  Use these tools for mail, calendar and documents, not a skill that needs its
  own credentials.

## Procedure

### 1. Offer, concretely, and wait for a yes

Propose one automation with its schedule and what each run will report, for
example: "Want me to send you a briefing every weekday at 8:00 with today's
meetings and the mail that needs you?" Create nothing until the user agrees.
If an app it needs is not connected, say so and offer `request_connection`.

### 2. Write a self-contained prompt

Write each run's prompt so it stands on its own: name the tools to use, the
time window, the criteria, and the shape of the report. Keep the report short
and scannable.

### 3. Make watches quiet

A watch runs often and usually has nothing to say. End its prompt with:
"If nothing meets the bar, respond with exactly [SILENT] and nothing else."
A run answering `[SILENT]` is kept but not delivered.

### 4. Create it and confirm

- A routine: call `create_schedule` with a short `name`, the `prompt` and the
  `schedule` (`cron` and `timezone`).
- A watch: call `cronjob_manage` with `action="create"`, a short `name`, the
  `schedule` and the prompt. To change or stop it later, use `cronjob_manage`
  with `action="list"`, `"pause"`, `"resume"` or `"remove"`.

Then tell the user what will happen, when the first run is, and where they
manage it.

## Recipes

| Automation | Kind | Schedule | Prompt essentials |
|---|---|---|---|
| Morning briefing | routine | `0 8 * * 1-5` | Today's calendar events (time, title, who), conflicts, mail from the last day that needs a reply or mentions a deadline, one line per item. |
| Meeting prep | routine | `0 7 * * 1-5` | For each of today's meetings with others: attendees, the latest related mail thread, open questions. |
| Weekly review | routine | `0 17 * * 5` | Meetings held, threads still awaiting a reply, next week's calendar. |
| Important-mail watch | watch | `*/30 * * * *` | New mail since the last run matching the user's criteria (sender, deadline, request); otherwise `[SILENT]`. |

## Done when

The user agreed, the routine is in `list_schedules` or the watch in
`cronjob_manage` `action="list"`, and you told them the schedule, the first run
time and where to manage it.
