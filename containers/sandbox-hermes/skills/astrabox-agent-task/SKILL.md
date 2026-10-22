---
name: astrabox-agent-task
description: "Find existing AstraBox Agent conversations by title, contact one, delegate work, and report a long task result, including after the current reply."
version: 0.1.0
author: AstraBox
license: Apache-2.0
metadata:
  hermes:
    tags: [A2A, Delegation, Background]
---

# Delegate an Agent task

Use this skill to find the owner's existing Agent conversations and contact an
Agent through AstraBox's A2A service. Keep the returned task and context
references in this conversation until the result has been reported.

## Find an existing conversation

When the user asks which Agent or A2A conversations they have or can continue,
use the `agents` MCP tool `list_agent_conversations`. It reads the user's platform
conversations, including ones started in the console without a previous A2A
exchange. Return their current Agent names and conversation titles. Follow
`next_cursor` when another page is requested; a page with `has_more=true` is not
the complete inventory. An empty result means no visible conversations matched
that query, not that no conversations exist anywhere.

Hermes `session_search` recalls Hermes session history. Native `a2a_list` discovers
configured peers and exchanges saved in this Hermes profile; `a2a_history` reads
one of those saved exchanges. Neither local history is the platform's Agent
conversation inventory, so a local zero cannot answer that platform question.
Use `a2a_list` to discover peer keys when starting new delegated work.

For an existing conversation, use `send_agent_message` only when the user asks
to send something, with the exact Agent and Session IDs returned by platform
discovery. Listing conversations sends no message and starts no replacement
business conversation. Retain the returned Task ID for `get_agent_task` or the
watcher below. A request to list or find conversations authorizes no message.

## Submit once

Run the adjacent `scripts/task.py` with the Hermes interpreter,
`"$HERMES_VENV/bin/python"`. Resolve the script relative to this skill's
location. Its `submit` action accepts these arguments:

```text
task.py submit --peer astrabox/<agent_id> --message-id <unique-stable-id> --message <instruction>
```

Use `terminal` in the foreground for this call. The script submits with
`returnImmediately=true` and prints the accepted task reference. An optional
`--context-id` continues a previous target conversation with a new task.
Choose a message ID before the call. If the submission response is lost, retry
only the identical peer, message, context and message ID. A fresh ID submits
new work; never repeat the task to retrieve its result.

Credentials come from the configured peer and stay inside the script. Do not
read or echo tokens, put them in commands, or copy them into this conversation.

## Wait using Hermes

For a task still `TASK_STATE_SUBMITTED` or `TASK_STATE_WORKING`, start:

```text
task.py watch --peer astrabox/<agent_id> --task-id <returned-task-id>
```

Use `terminal(background=true, notify=true)` in this same conversation. Set a
process timeout appropriate to the user's task. Keep the returned process
session ID. Hermes owns that process and its completion notification; after
acknowledging that work is underway, you can end the current reply. Do not
create a cron job or spend model turns repeatedly polling an unchanged task.

On completion, read the full result with the existing `agents` MCP tool
`get_agent_task(agent_id, task_id)`, then report it in this conversation once.
Read the task even if it completed during submission. Copy the target
conversation link when useful. A process transport error or timeout says
nothing about the remote task's outcome: query the same task, then start a
new watcher if it remains active. Do not resubmit it.

If the task needs input or authorization, read its public interaction contract
and ask the user. Use `answer_agent_task` for the exact task and interaction
with the user's explicit answer, then watch that same task. Report a failed,
canceled or rejected task using its returned state and explanation. Killing
a watcher stops observation; it does not cancel remote work. Task-scoped
cancellation is unavailable.

Native background processes are not durable execution. After runtime loss,
use the saved task reference to query the target again; do not claim that the
watcher itself survived. If notifications are disabled, wait with the native
`process` tool instead of promising a later automatic reply.
