---
name: astrabox-proactive
description: "Offer and set up recurring briefings and quiet monitors for the user — morning briefings, important-mail watches, meeting prep, weekly reviews — that run on a schedule and report back into this conversation."
version: 0.1.0
author: AstraBox
license: Apache-2.0
metadata:
  hermes:
    tags: [Automation, Cron, Briefing, Monitor, Proactive]
    related_skills: [email-inbox-triage]
---

# Proactive briefings and monitors

You run inside AstraBox as the user's long-lived assistant. You can do work for
them on a schedule, without being asked each time. Use this skill to notice
when that would help, offer it, and set it up so it keeps working on its own.

## When to use

- The user describes something recurring: "every morning", "each Friday",
  "before my meetings", "keep an eye on".
- The user asks you to tell them when something happens: an email from
  someone, a price, a deadline, a reply.
- You just finished a task the user is likely to need again (a summary of
  today's mail, an agenda for today) — offer once to do it on a schedule.

Do not use it to schedule one-off work the user wants now; do that now.

## What AstraBox gives you

- **Scheduled jobs.** `cronjob_manage` with `action="create"` creates a job.
  Each run reports into the conversation that created it, and to the
  messaging chat that conversation belongs to, so the user receives it
  wherever they talk to you. The console marks the conversation unread and can
  notify them.
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

### 2. Write a self-contained job prompt

A run starts with no memory of this conversation. Its prompt must name the
tools to use, the time window, the criteria, and the shape of the report.
Keep the report short and scannable.

### 3. Make monitors quiet

A monitor runs often and usually has nothing to say. End its prompt with:
"If nothing meets the bar, respond with exactly [SILENT] and nothing else."
A run answering `[SILENT]` is kept but not delivered.

### 4. Create it and confirm

Call `cronjob_manage` with `action="create"`, a short `name`, the `schedule`,
and the prompt. Then tell the user what will happen and when the first run
is. To change or stop it later, use `cronjob_manage` with `action="list"`,
`"pause"`, `"resume"` or `"remove"`.

## Recipes

| Automation | Schedule | Prompt essentials |
|---|---|---|
| Morning briefing | `0 8 * * 1-5` | Today's calendar events (time, title, who), conflicts, mail from the last day that needs a reply or mentions a deadline, one line per item. |
| Important-mail monitor | `*/30 * * * *` | New mail since the last run matching the user's criteria (sender, deadline, request); otherwise `[SILENT]`. |
| Meeting prep | `0 7 * * 1-5` | For each of today's meetings with others: attendees, the latest related mail thread, open questions. |
| Weekly review | `0 17 * * 5` | Meetings held, threads still awaiting a reply, next week's calendar. |

## Done when

The user agreed, the job exists in `cronjob_manage` `action="list"`, and you
told them the schedule, the first run time and how to stop it.
