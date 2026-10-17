# Sessions

> Create, run, inspect, and archive Agent Sessions.

A Session is one conversation with an Agent. It uses an Agent and Environment,
keeps the messages you send and the responses the Agent program produces, and
reports what the Agent program is doing now. You send messages to the Session,
and its responses arrive on the Session's stream.

The Agent program owns the conversation: whether it is working, which messages
it has taken, and where each response starts and ends are what the program
reports. AstraBox records those reports, shows them, and keeps your messages
until the program has taken them.

## Session Status Lifecycle

A Session reports one of these statuses in `state`:

| Status | Description | Transitions to |
| --- | --- | --- |
| `CREATING` | Preparing or assigning the runtime. | `READY`, `TERMINATED` |
| `READY` | Nothing is running; the Session accepts a message. | `PROCESSING`, `WAITING_INPUT`, `TERMINATED`, `DELETED` |
| `PROCESSING` | The Agent program is working, a response is open, or AstraBox still holds messages the program has not taken. | `READY`, `WAITING_INPUT`, `TERMINATED`, `DELETED` |
| `WAITING_INPUT` | A response is paused on an approval or a question. | `PROCESSING`, `READY`, `TERMINATED`, `DELETED` |
| `TERMINATED` | The runtime is offline. | `CREATING`, `DELETED` |
| `DELETED` | Deleted. | — (terminal state) |

`runtime_status` carries the Agent program's own report: `working`, `waiting`,
or `idle`. `PROCESSING` and `WAITING_INPUT` are derived from it and from the
responses, questions, and messages AstraBox has recorded for the Session.

Two additional lifecycle markers are surfaced alongside `state`:

- **Archived**: recorded separately from `state`. The Session remains readable
  but leaves the active conversation list.
- **Runtime unavailable**: `runtime_unavailable` is `true` while the Session's
  sandbox is gone or released. A `READY` Session keeps its history, and its
  next message starts a new sandbox.

1. <b>Created → CREATING</b> A new Session enters `CREATING` while AstraBox
   prepares or assigns its runtime.
2. <b>CREATING → READY</b> The Session is ready for input. A message sent while
   the Session is still `CREATING` is kept and reaches the Agent program once
   the runtime is ready.
3. <b>READY → PROCESSING</b> Sending a message hands it to the Agent program,
   which starts a response.
4. <b>PROCESSING → WAITING_INPUT → PROCESSING</b> A response that needs an
   approval or an answer pauses until someone responds, then continues.
5. <b>PROCESSING → READY</b> The response ends and the Agent program reports it
   is idle. A program that keeps working on its own, such as on a background
   task, stays `PROCESSING` until it reports idle.
6. <b>TERMINATED</b> The conversation remains stored, but its runtime must be
   recovered (`POST /api/v1/sessions/{session_id}/recover`) before more work can
   run.
7. <b>DELETED (terminal)</b> Deleting the Session ends its lifecycle.

Conversation recovery does not require a persistent workspace volume. AstraBox
stores the Agent program's native session data in the platform database and
restores it for that program's own resume operation when compute is replaced.
Workspace files have a separate lifetime: preserving them after sandbox loss or
release requires optional persistent workspace storage. A lost connection alone
does not prove the sandbox has failed; AstraBox reconnects to the program and
continues from the position it recorded. When the sandbox is confirmed lost,
the response in progress ends as failed. Messages the program had not taken yet
go to a new sandbox automatically, which resumes the conversation; a message
the program had taken is not sent again.

## Automatic model request retries

A retry performed by the selected Agent program or model gateway remains part
of the response in progress. The Session stays `PROCESSING`; AstraBox does not
expose a separate `rescheduling` state. Keep following the stream and do not
resend the same message while the response is open.

If the stream reports an error, use the response's recorded `outcome` and the
Session's subsequent state as the source of truth. The selected Agent program
or model gateway owns any retry classification; AstraBox does not manufacture
a second retry status.

## Interrupt Semantics

```bash
# Interrupt the response in progress
curl --silent --show-error --request POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/interrupt" \
  --header "Authorization: Bearer $ACCESS_TOKEN"
```

- **`status: "accepted"`**: AstraBox passes the request to the Agent program,
  which stops the response in progress and reports how it ended, for example
  `cancelled`.
- **`status: "idle"`**: no open response or held input was observed. This does
  not assert that the runtime is disconnected.
- **`409 PENDING_INPUTS_NOT_CANCELLED`**: no open response was observed, but the
  platform still holds accepted inputs. `data.input_ids` identifies those inputs
  at the time of the read. They have not been withdrawn and may still run.
  The console explains this refusal instead of presenting an idle success.
- **After interrupt**: the Session remains reusable; send the next message as
  usual.

Only `DELETED` is final. Interrupting a response does not delete the Session or
its saved history. A Session's web terminal has its own stop operation,
`POST /api/v1/sessions/{session_id}/terminal/interrupt`; the conversation
interrupt does not stop a terminal command.

Interrupt does not withdraw queued messages, including when it stops an active
response. Individual accepted inputs have no withdrawal operation. A `pending`
row means the delivery receipt has not arrived; the runtime may already have
received it. Do not delete or mark that row cancelled to implement withdrawal.

To take the entire conversation offline, use **Archive** in the conversation
list (`POST /api/v1/sessions/{session_id}/archive`). Successful archival prevents
further runtime connections while retaining saved history. Held inputs then
fail with `CONVERSATION_ENDED` when the reader reconciles the archived Session.
It is a separate lifecycle operation, not per-message cancellation and
not an undo of work the runtime already performed. Archival errors must be
resolved before treating the conversation as offline.

## Sending Messages While the Agent Is Working

A Session accepts a message at any status except `TERMINATED` and `DELETED`.
AstraBox hands messages to the Agent program in the order it accepted them. A
program that is already working queues a message in its own queue and answers
it when it is ready; whether it answers queued messages one at a time or
together is the program's choice. Each response lists the messages it answers
in `input_ids`.

Sending to a `TERMINATED` or deleted Session returns `HTTP 409
AGENT_RUNTIME_ERROR`. Recover a `TERMINATED` Session first. A pending approval
or question is answered through the interaction answer endpoint, not by
sending a message.

## Create a Session

A Session starts from an Agent. The Environment is already selected by that
Agent. From the console:

1. Open **Agents** in the AstraBox console.
2. Choose an Agent.
3. Select **Start conversation**.

The new Session opens when its runtime is ready. With Agent prewarming enabled,
it can claim a runtime prepared before the Session exists, including its Agent
configuration and configured extensions. When persistent workspace storage is
configured, AstraBox selects the new Session's workspace or the existing
Session's saved workspace before handing over the runtime. That assignment
stays fixed while the runtime is in use.

For an integration, send the Agent ID in the request path:

```bash
# Create a Session using an Agent ID
curl --silent --show-error --request POST \
  "$SERVICE_URL/api/v1/agents/$AGENT_ID/conversations" \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: create-code-review-session' \
  --data '{}'
```

A successful request returns the Session ID:

```json
{
  "code": "OK",
  "message": "success",
  "data": {
    "session_id": "SESSION_ID",
    "agent_id": "AGENT_ID",
    "deployment_name": "code-reviewer"
  }
}
```

The `Idempotency-Key` header is optional. Reusing the same key for the same
user and Agent returns the same Session instead of creating a duplicate.

The creation response identifies the Session, Agent, and deployment. A
subsequent `GET /api/v1/sessions/{session_id}` returns a Session object whose
commonly used fields are:

| Parameter | Type | Description |
| --- | --- | --- |
| `session_id` | string | System-generated Session ID. |
| `agent_id` | string | The Agent that started the Session. |
| `state` | string | Current Session status. |
| `title` | string/null | Session title. |
| `model_name` | string/null | Model reported for the current runtime. |
| `permission_mode` | string/null | Mode selected for the Agent program, when supported. |
| `runtime_status` | string/null | `idle`, `working` or `waiting`, as the Agent program last reported. |
| `current_responses` | array | Responses the Agent program is producing now. |
| `last_response` | object/null | The most recent response that ended, with its `outcome` (`completed`, `failed` or `cancelled`) and the program's own `native_reason`. |
| `pending_interaction` | object/null | The approval or question the Session is waiting on. |
| `held_inputs` | array | Messages accepted but not yet taken by the Agent program. |
| `output_seq` | integer | The output position to follow the Session's stream from. |
| `runtime_unavailable` | boolean | Whether the Session's sandbox is gone or released. |
| `created_at` | string/null | Creation time. |
| `updated_at` | string/null | Last update time. |

Session list responses contain a summary of each Session, with its `state`
from the last status the Agent program reported; `GET
/api/v1/sessions/{session_id}` returns the complete current details. When the
Agent program reports token usage or model cost for a response, the console
shows it with that response. Billing and quotas remain with the model service
configured for the deployment.

AstraBox does not accept an Agent object, Agent version, or Environment in the
creation body. The request path identifies the Agent, and the Agent already
selects the Environment. The Session records that Agent identity; it does not
snapshot or lock an Agent version:

- AstraBox resolves the current Agent and Environment when it first prepares a
  runtime for the Session.
- Saving an Agent or Environment does not reconfigure a running runtime in
  place.
- If AstraBox later recreates the runtime, the same Session can use the current saved
  Agent and Environment configuration.
- The Agent `version` field detects concurrent updates. It is not a selectable
  configuration version for a Session.

## Attach Resources After Creation

AstraBox does not accept a `resources` array in the Session creation request.
Configure the default GitHub repository and Vault assignments on the Agent,
then upload one-off files directly to the Session workspace after its runtime
is ready. See [Access GitHub](working-with-repos.md), [Authenticate with
Vaults](credentials.md), and [Attach and download files](files.md).

## Send Messages

When an administrator has configured a transcription model, the console's
microphone button records speech and appends its transcript to the editable
draft. Review the text before sending it. See [Voice input](voice-input.md)
for setup, recording limits, and retry behavior.

`POST /api/v1/sessions/{session_id}/inputs` accepts one message and returns a
receipt at once. The response to it arrives on the Session's stream.

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `content` | string | Yes | The user message. |
| `client_message_id` | string | No | A caller-generated idempotency key for the message. |

To change the permission mode the Agent program uses, update the Session's
permission mode (see [Read and Update Sessions](#read-and-update-sessions)).

```bash
# Send a message
curl --silent --show-error --request POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/inputs" \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{
    "content": "Analyze the code complexity of all Python files in the current directory.",
    "client_message_id": "analysis-1"
  }'
```

The receipt names the accepted message:

```json
{
  "code": "OK",
  "message": "success",
  "data": {
    "session_id": "SESSION_ID",
    "input_id": "INPUT_ID",
    "client_message_id": "analysis-1",
    "accepted_at": "2026-05-18T12:00:00Z",
    "state": "pending",
    "duplicate": false
  }
}
```

Sending the same `client_message_id` again returns the message accepted the
first time, with `duplicate: true`; the Agent program receives it once. A
deployment's admission policy can refuse a message with `HTTP 429
ADMISSION_DENIED`.

A message that the Agent program or the deployment's preparation refuses does
not start a response: the stream reports it as a `data-input-failed` part with
the reason, and the message keeps its `failure` in the Session history.

## Read the Session Stream

The stream uses the AI SDK UI Message Stream protocol and delivers each
response as one UI message, starting after the position you pass in
`after_seq`. To wait for the next response, start from the Session's
`output_seq`, which `GET /api/v1/sessions/{session_id}` and the first page of
messages return; a new Session's output starts at `0`:

```text
GET /api/v1/sessions/{session_id}/stream?after_seq=0
```

A `data-resume-cursor` part carries the latest position in `data.seq`. Save
that value and reconnect with `after_seq`; AstraBox does not use the SSE
`Last-Event-ID` header:

```text
GET /api/v1/sessions/{session_id}/stream?after_seq=42
```

One stream request ends after the part that finishes a response. Open the next
one with the saved `after_seq` to receive the following response. For durable
conversation history, call `GET /api/v1/sessions/{session_id}/messages` and page
backwards with `before`. See [SSE Event Stream](events-stream.md) for part
shapes and reconnection.

## Answer Approvals and Questions

When a response pauses on an approval or a question, the stream sends a
`data-interaction` part (and a `tool-approval-request` for a tool approval),
and the Session reports it as `pending_interaction`. Answer it with its
interaction ID:

```bash
curl --silent --show-error --request POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/interactions/$INTERACTION_ID/answer" \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{"answer": {"decision": "approve"}}'
```

The answer's shape depends on the interaction's `presentation`; see
[Permission Modes](permission-modes.md). An interaction that was already
answered, withdrawn, or expired returns `HTTP 409 INTERACTION_NOT_OPEN`. The
response continues on the Session stream.

## Read and Update Sessions

```bash
# Get one Session
curl --silent --show-error \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID" \
  --header "Authorization: Bearer $ACCESS_TOKEN"

# List Sessions with pagination
curl --silent --show-error \
  "$SERVICE_URL/api/v1/sessions?page=1&limit=10" \
  --header "Authorization: Bearer $ACCESS_TOKEN"

# Update the Agent program's permission mode for this Session
curl --silent --show-error --request POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/permission-mode" \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Content-Type: application/json' \
  --data "{\"permission_mode\":\"$PERMISSION_MODE\"}"
```

Example paginated response:

```json
{
  "code": "OK",
  "message": "success",
  "data": {
    "sessions": [
      {
        "session_id": "SESSION_ID",
        "agent_id": "AGENT_ID",
        "state": "READY",
        "runtime_status": "idle",
        "title": "code-reviewer",
        "created_at": "2026-05-18T12:00:00Z",
        "updated_at": "2026-05-18T12:30:00Z"
      }
    ],
    "has_more": false,
    "next_cursor": null
  }
}
```

Paged list responses use `has_more` and `next_cursor`. Without `page=1`, the
same list route returns the console's unpaged array. Permission mode is the only
Session configuration this route family updates; the selected Agent program
defines its accepted values. AstraBox stores the mode on the Session and
applies it to the running Agent program; a Session without a running program
receives it when its runtime starts.

### Conversation history and folded work

`GET /api/v1/sessions/{session_id}/messages` returns the recorded conversation
as UI messages. The console reads the same messages through
`GET /api/v1/sessions/{session_id}/history-blocks`, which replaces folded work
with `data-process-block` headers. Each header's `block_id` and `cursor` address
its parts at
`GET /api/v1/sessions/{session_id}/history-blocks/{block_id}?cursor=...`.
Opening the header fetches those parts at the recorded position.

A response that ended `failed` or `cancelled` can fold its finished work and
intermediate body text under one header. A successful response can do so when
it has text after its last tool call. Text after the last tool call stays
outside that fold. The native ending reason, such as `max_tokens`, does not
select a separate layout. The response metadata retains `outcome` and
`native_reason` independently of folding.

The console's display contract is:

- A failed response keeps its failure notice visible outside the fold. A
  cancelled response keeps its stopped notice visible there.
- On an abnormal ending, failed, denied and unfinished tool calls stay outside
  the whole-response fold. Interaction and result parts also stay outside it;
  a tool awaiting approval prevents that fold.
- Expanding a header restores the folded body text and process groups. Tool
  cards keep their own disclosures for input and output. Folding changes the
  initial presentation, without discarding the recorded steps.
- Reloading reads the same recorded response outcome and content through the
  history endpoints. A failed response does not become successful because its
  work has a generated label.

A process summary names operations; it is not the response's outcome. On the
process-summary endpoint, `status: completed` means label generation finished.
For a failed or cancelled response, `response_completed` is `false` even when
that label exists. Integrations use the response's recorded `outcome` to decide
whether it succeeded, and accept folded intermediate text when the failure
notice and all recorded steps remain accessible.

### Unread replies

A Session records when it last gained output (`last_output_at`), whether a
person's message produced it or the Agent program replied on its own, such as
an Assistant's scheduled job or heartbeat. Its owner's `read_at` records when
they last read it:

```bash
curl --silent --show-error --request POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/read" \
  --header "Authorization: Bearer $ACCESS_TOKEN"
```

A Session whose `last_output_at` is later than its `read_at` is unread. The
console marks it read while it is open in a visible tab and shows a dot beside
an unread conversation in the list. With notifications turned on (the bell
beside **Sessions** in the list), it shows a browser notification for each new
unread reply while a console tab is open, including a tab in the background.
A response that ends, including one the Agent program started on its own,
moves its conversation to the top of the list.

## Child Runs

An Agent program can delegate work to child runs. AstraBox exposes their tree,
engine-native status, messages, usage summary, and currently supported
operations at Session-scoped endpoints:

```text
GET /api/v1/sessions/{session_id}/child-runs
GET /api/v1/sessions/{session_id}/child-runs/{child_run_id}/messages
POST /api/v1/sessions/{session_id}/child-runs/{child_run_id}/stop
```

The **Agents** tab presents the same child-run information. See [Multiagent
orchestration](multi-agents.md) for delegation, shared workspace behavior, and
child-run controls.

## Lifecycle

Archive a Session when it should leave the active conversation list but remain
readable:

```text
POST /api/v1/sessions/{session_id}/archive
```

Delete a Session when its record and runtime should enter the terminal
`DELETED` state:

```text
DELETE /api/v1/sessions/{session_id}
```

Ending or deleting a Session is different from interrupting its current
response. Use the lifecycle operation that matches the intended retention
behavior.

Archiving, deleting, ending a conversation and terminating its sandbox return
`HTTP 409 SESSION_BUSY` while the Agent program reports that it is working.
Interrupt the work first, or wait for it to finish. A Session that is waiting
for an answer to an approval or a question is not working and can be archived
or deleted.

## Conversation Workflow

Sessions support multi-message conversations. The recommended pattern is:

1. Send the user message through `POST /sessions/{id}/inputs`.
2. Follow `GET /sessions/{id}/stream` from the last saved cursor (or the
   Session's `output_seq`), and read the response until its `finish` part.
3. Answer any approval or question the response pauses on.
4. Send the next message to the same Session. A message sent while the Agent
   program is still working waits in the program's queue.

```bash
BASE_URL="$SERVICE_URL/api/v1"

# Message 1: Make the initial request
curl --silent --show-error --request POST \
  "$BASE_URL/sessions/$SESSION_ID/inputs" \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{"content":"Create a Python Flask project scaffold.","client_message_id":"message-1"}'

# Read the response; a new Session's output starts at 0
curl --no-buffer --silent --show-error \
  "$BASE_URL/sessions/$SESSION_ID/stream?after_seq=0" \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Accept: text/event-stream'

# Message 2: Add a follow-up requirement
curl --silent --show-error --request POST \
  "$BASE_URL/sessions/$SESSION_ID/inputs" \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{"content":"Add unit tests and a CI configuration to the project.","client_message_id":"message-2"}'
```

## Share a Session

Open the Session and select **Share** to create a read-only link. Choose an
expiry time and whether viewers may download workspace files. The link itself
grants read access, so send it only to intended viewers and revoke it when it
is no longer needed. Viewers cannot send messages or change the Session.

## Import earlier history

When conversations move to AstraBox from another system, a platform
administrator can import each one's earlier messages so its Session shows
them. An import is for reading only: it starts no sandbox, runs nothing, and
changes no Session state. The Agent program does not read these messages; to
continue the conversation where it left off, restore the program's own session
history separately.

```bash
curl -X POST "$ASTRABOX/api/v1/admin/sessions/$SESSION_ID/history-import" \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"messages": [
    {"source_id": "m-1", "role": "user", "created_at": "2026-01-05T09:00:00Z",
     "blocks": [{"type": "text", "text": "Summarize the incident."}]},
    {"source_id": "m-2", "role": "assistant", "created_at": "2026-01-05T09:00:20Z",
     "completion": "unknown", "parent_source_id": "m-1",
     "blocks": [{"type": "text", "text": "The outage began when"}]}
  ]}'
```

- List messages in conversation order, up to 500 per call. `source_id` is the
  message's identity in the old system; `created_at` needs a timezone.
- A user message carries `text` and `image` blocks. A reply also carries
  `thinking`, `tool_use`, and the `tool_result` answering a call made earlier
  in the same reply.
- `completion` is required on a reply. Use `unknown` when the old system never
  recorded how the reply ended; the console shows it as imported without a
  recorded ending rather than as succeeded or failed.
- `parent_source_id` keeps which message a message replied to.
- Import before the Session's first message of its own. Once the conversation
  has started, an import is refused with `HISTORY_IMPORT_AFTER_CONVERSATION`,
  because history cannot be placed before messages already in it.
- Running the same import again changes nothing. To continue an import, send
  the messages already imported first, then the new ones. Other content under
  a used `source_id`, or a new message placed before imported ones, is refused
  with `HISTORY_IMPORT_CONFLICT`.

Imported messages read, page and share like any other message, and carry
`history_import` with their `source_id`, `completion`, and `parent_source_id`.

## Best practices

1. **Treat Agent configuration as live** — A Session does not pin an Agent
   version. Saving an Agent does not alter a running runtime, but a recreated
   runtime uses the current saved Agent and Environment configuration.
2. **Use stable request identifiers** — Give each Session creation request a
   stable `Idempotency-Key` and each message a stable `client_message_id` for
   duplicate prevention and traceability.
3. **Interrupt and archive intentionally** — Interrupt a response that should
   stop; archive the Session only when it should leave the active list and
   release its runtime.

## FAQ

**Q: What happens if I send a message while the Agent is working?**

A: AstraBox accepts it and hands it to the Agent program, which queues it and
answers it after the current work, possibly in the same response as other
queued messages. The response's `input_ids` name the messages it answers.

**Q: A message was refused. Can I send it again with the same `client_message_id`?**

A: Not to get a different answer. The same `client_message_id` always names
the same message, so sending it again returns the first receipt and the
message's recorded `failure`. Send the text again as a new message with a new
`client_message_id`.

**Q: Can I still use a Session after interrupting?**

A: Yes. Send the next message to continue. Interrupting a response does not
delete the Session or its history.

**Q: How do I get the complete conversation history?**

A: Call `GET /sessions/{id}/messages` and page backwards with the `before`
cursor each page returns. Messages are AI SDK UI messages: one per message you
sent and one per response. During an active response, the first page includes
the parts generated so far and `output_seq`, the position to follow the
Session's stream from for the rest.

**Q: How do I reconnect after an SSE disconnect?**

A: Save `data.seq` from the latest `data-resume-cursor` part, then reconnect
with the `after_seq` query parameter. AstraBox does not consume
`Last-Event-ID`.

**Q: Why is an Environment missing when I create or edit an Agent?**

A: The Agent form lists only enabled Environments whose Agent program supports
Agents. The **Environments** administration view also shows disabled entries;
check the Environment's enabled state and selected Agent program there.

## API Reference

- The deployed instance's `/docs` and `/openapi.json` references
- [SSE Event Stream](events-stream.md) — Consume output and reconnect safely.
- [Multiagent orchestration](multi-agents.md) — Inspect and control child runs.
- [Attach and download files](files.md) — Work with the Session workspace.
