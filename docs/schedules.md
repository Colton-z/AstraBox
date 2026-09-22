# Manage schedules

> Run an Agent automatically on a recurring schedule.

In AstraBox, a Schedule is a trigger configuration that connects an Agent to a
calendar and a prompt. Each due time starts a fresh Agent Session, so no
existing Session needs to remain online.

Use the web console to create, list, update, disable, run, replay, and delete
Schedules.

## Prerequisites

- Create the Agent that should handle each invocation.
- Sign in as the Agent owner or an AstraBox administrator.
- The AstraBox installation must use PostgreSQL or SQLite. Schedules are
  unavailable with other database backends.

Open **Console → Triggers → New trigger**, then choose **Schedule**. The form
contains the complete Schedule definition:

| Field | Description |
| --- | --- |
| Agent | The Agent that handles every invocation. |
| Name | A short name that distinguishes this Schedule. |
| Prompt | What the Agent should do each time the Schedule fires. |
| Cron expression | A five-field cron expression: minute, hour, day of month, month, and day of week. |
| Timezone | A valid IANA timezone, such as `Asia/Shanghai`, `America/Los_Angeles`, or `UTC`. |

The form initially uses the browser's current IANA timezone when it can detect
one, and otherwise uses `UTC`. Confirm it before creating the Schedule.

## Create a schedule

In the form, specify:

- When to run, as a five-field cron expression.
- What the task should do.
- The required output in the prompt.
- The timezone.

### Recurring tasks

Every weekday at 9:00 AM in Shanghai:

```text
Cron expression: 0 9 * * 1-5
Timezone: Asia/Shanghai
Prompt: Summarize AI industry news from the past 24 hours. Select the five
most important stories and include source links.
```

Every Monday at 10:00 AM in Los Angeles:

```text
Cron expression: 0 10 * * 1
Timezone: America/Los_Angeles
Prompt: Summarize last week's project progress, risks, and action items.
Format the result as a Markdown table.
```

### One-time tasks

The built-in Schedule is recurring and does not have a one-time trigger. For a
one-time task, create an external-scheduler trigger and have that scheduler
call AstraBox at the required time. See [Automate Agent
runs](deployments.md#external-scheduler).

### Fixed-interval tasks

A five-field cron expression can represent minute, hourly, daily, weekly, and
monthly intervals. For example, run every 30 minutes:

```text
Cron expression: */30 * * * *
Timezone: UTC
```

For an interval or date range that cannot be represented by five-field cron,
use an external scheduler and its authenticated trigger.

After creation, the Schedule detail page shows its name, Agent, prompt, cron
expression, timezone, status, and Runs. Select **Disable** to stop future
scheduled invocations without deleting the definition.

## List schedules

Open **Console → Triggers** to list the trigger configurations for Agents you
can manage. Search by Agent, Schedule name, trigger type, or ID, and filter the
list by enabled or disabled status.

Select a Schedule to view or update its prompt and calendar, enable or disable
it, run it immediately, and inspect its Run history.

## Delete a schedule

Open the Schedule detail page and select **Delete**. Deleting it stops future
invocations and removes the trigger configuration. Sessions already created by
earlier Runs remain independent Session records.

If you may need the configuration or Run history again, select **Disable**
instead. A disabled Schedule no longer runs automatically and can be enabled
later.

## View execution results

- Each scheduled invocation creates a Run and a fresh Session.
- Select **Run now** to start a new Run without changing the calendar.
- Select **Replay** on a previous Run to create another Run with that Run's
  captured prompt.
- Open a Run with a `session_id` to inspect the Session's messages, files, and
  status.

The Runs table identifies whether each invocation came from the Schedule,
**Run now**, or **Replay**. Runs do not share conversation context with one
another.

## Limitations

- The built-in Schedule is recurring. It accepts exactly five cron fields and
  a valid IANA timezone; its minimum granularity is one minute.
- Schedules require PostgreSQL or SQLite, and one AstraBox installation
  supports at most 64 active Schedules.
- Times that pass while AstraBox is offline are skipped. A Schedule also cannot
  be changed to another trigger type; create a new trigger configuration
  instead.

## Related documentation

- [Automate Agent runs](deployments.md)
- [Run a Session](sessions.md)
- [Trigger an Agent with a webhook](webhooks.md)
- [Connect an Agent to a messaging platform](channels.md)
