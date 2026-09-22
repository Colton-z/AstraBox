# Sessions

> Create, run, inspect, and archive Agent Sessions.

A Session is one stateful Agent execution. It uses an Agent and Environment and
preserves its messages, Events, and current state. You send messages to the
Session, and it returns a stream of Events.

## Session Status Lifecycle

A Session is a state machine with the following core statuses:

| Status | Description | Transitions to |
| --- | --- | --- |
| `CREATING` | Preparing or assigning the runtime. | `READY`, `TERMINATED` |
| `READY` | Ready for a user message. | `BUSY`, `TERMINATED`, `RECOVERY_REQUIRED`, `DELETED` |
| `BACKGROUND_RUNNING` | Ready for foreground input while child tasks remain active. | `READY`, `BUSY`, `TERMINATED`, `RECOVERY_REQUIRED`, `DELETED` |
| `BUSY` | The Agent is processing the foreground turn. | `INTERRUPTING`, `READY`, `RECOVERY_REQUIRED`, `TERMINATED` |
| `INTERRUPTING` | Interruption requested; waiting for the running turn to stop. | `READY`, `RECOVERY_REQUIRED` |
| `RECOVERY_REQUIRED` | Stored history is available, but the runtime requires recovery. | `READY`, `CREATING`, `TERMINATED`, `DELETED` |
| `TERMINATED` | The runtime is offline. | `CREATING`, `DELETED` |
| `DELETED` | Deleted. | — (terminal state) |

Two additional lifecycle markers are surfaced alongside `state`:

- **Archived**: recorded separately from `state`. The Session remains readable
  but leaves the active conversation list.
- **Deleted**: represented by the terminal `DELETED` state. A deleted Session
  cannot be restored.

1. <b>Created → CREATING</b> A new Session enters `CREATING` while AstraBox
   prepares or assigns its runtime.
2. <b>CREATING → READY</b> The Session is ready for input.
3. <b>READY → BUSY</b> Sending a message starts a foreground turn.
4. <b>BUSY → READY</b> When the foreground turn completes, the Session becomes
   ready for the next turn. It can report `BACKGROUND_RUNNING` while child
   tasks remain active.
5. <b>BUSY → INTERRUPTING → READY</b> Interrupting a foreground turn moves the
   Session through `INTERRUPTING`. The Session remains reusable.
6. <b>RECOVERY_REQUIRED or TERMINATED</b> The conversation remains stored, but
   its runtime must be recovered or recreated before more work can run.
7. <b>DELETED (terminal)</b> Deleting the Session ends its lifecycle.

Conversation recovery does not require a persistent workspace volume. AstraBox
stores the Agent program's native session data in the platform database and
restores it for that program's own resume operation when compute is replaced.
Workspace files have a separate lifetime: preserving them after sandbox loss or
release requires optional persistent workspace storage. A lost connection alone
does not prove the sandbox has failed; a confirmed sandbox failure ends the
affected turn, and the next message can start recovery without replaying that
failed task automatically.

## Automatic model request retries

A retry performed by the selected Agent program or model gateway remains part
of the active turn. AstraBox keeps the Session in `BUSY`; it does not expose a
separate `rescheduling` state. Keep the stream open and do not resend the same
message while the turn is active.

If the stream reports an error, use the recorded turn failure and subsequent
Session state as the source of truth. The selected Agent program or model
gateway owns any retry classification; AstraBox does not manufacture a second
retry status.

## Interrupt Semantics

- **Interrupt on `READY`**: an idempotent no-op; the Session stays ready.
- **Interrupt with an active turn**: AstraBox records the request, moves the
  Session through `INTERRUPTING`, and returns `status: "accepted"`.
- **After interrupt**: the Session remains reusable after the turn settles.

```bash
# Interrupt the current turn
curl --silent --show-error --request POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/interrupt" \
  --header "Authorization: Bearer $ACCESS_TOKEN"
```

Only `DELETED` is final. Interrupting a foreground turn does not delete the
Session or its saved history.

## Sending Messages While a Turn Is Active (409 Error)

`POST /api/v1/sessions/{session_id}/ai-stream` returns `HTTP 409 SESSION_BUSY`
when the Session already has an active foreground turn or is still settling an
interrupt. Wait for `READY` or `BACKGROUND_RUNNING`, or interrupt the current
turn before sending the next foreground message.

```json
{
  "code": "SESSION_BUSY",
  "message": "session already has an active turn",
  "data": null,
  "error": {
    "code": "SESSION_BUSY",
    "status_code": 409,
    "category": "state",
    "retryable": true,
    "owner": "session",
    "user_message": "session already has an active turn"
  }
}
```

Wait for the current turn's terminal stream frame before sending another
message. A pending interaction must instead be answered through the interaction
response endpoint.

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
| `current_turn_id` | string/null | Active turn ID. |
| `last_turn_status` | string/null | Status of the most recent turn. |
| `created_at` | string/null | Creation time. |
| `updated_at` | string/null | Last update time. |

Session list responses contain a summary; `GET
/api/v1/sessions/{session_id}` returns the complete current details. When the
Agent program reports token usage or model cost for a completed turn, the
console shows it with the turn result. Billing and quotas remain with the model
service configured for the deployment.

AstraBox does not accept an Agent object, Agent version, or Environment in the
creation body. The request path identifies the Agent, and the Agent already
selects the Environment. The Session records that Agent identity; it does not
snapshot or lock an Agent version:

- AstraBox resolves the current Agent and Environment when it first prepares a
  runtime for the Session.
- Saving an Agent or Environment does not reconfigure an active turn or running
  runtime in place.
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

The request body for `POST /sessions/{id}/ai-stream` contains one user message
and returns that turn as an AI SDK UI Message Stream v1 event stream.

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `content` | string | Yes | The user message. |
| `client_message_id` | string | No | A caller-generated idempotency key for the message. |
| `permission_mode` | string | No | A permission mode supported by the Session's Agent program. |

```bash
# Send a message and stream the response
curl --no-buffer --silent --show-error --request POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/ai-stream" \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{
    "content": "Analyze the code complexity of all Python files in the current directory.",
    "client_message_id": "analysis-1"
  }'
```

Sending the message changes a `READY` Session to `BUSY`; a Session in
`BACKGROUND_RUNNING` can also accept a new foreground turn. When processing
completes, the Session returns to `READY`, or reports `BACKGROUND_RUNNING` if
child tasks remain active. The stream ends with `data: [DONE]` after the
foreground turn reaches a terminal result.

## Read Events

The `ai-stream` response uses the AI SDK UI Message Stream protocol. Open a
Session-follow connection to wait for one complete turn without sending input:

```text
GET /api/v1/sessions/{session_id}/ai-stream?follow=session
```

A `data-resume-cursor` frame carries the latest safe integer in
`data.frameSeq`. Save that value and reconnect with `after_seq`; AstraBox does
not use the SSE `Last-Event-ID` header:

```text
GET /api/v1/sessions/{session_id}/ai-stream?follow=session&after_seq=42
```

For durable conversation history, call `GET
/api/v1/sessions/{session_id}/messages` and page backwards with `before`. See
[SSE Event Stream](events-stream.md) for frame shapes and reconnection.

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
defines its accepted values.

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

Ending or deleting a Session is different from interrupting its current turn.
Use the lifecycle operation that matches the intended retention behavior.

## Multi-Turn Conversation Workflow

Sessions support multi-turn conversations. The recommended pattern is:

1. Send the user message through `POST /sessions/{id}/ai-stream`.
2. Consume the AI SDK UI Message Stream response.
3. Wait for the terminal frame and for the Session to report `READY` or
   `BACKGROUND_RUNNING`.
4. Send the next message to the same Session.

```bash
BASE_URL="$SERVICE_URL/api/v1"

# Turn 1: Make the initial request
curl --no-buffer --silent --show-error --request POST \
  "$BASE_URL/sessions/$SESSION_ID/ai-stream" \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{"content":"Create a Python Flask project scaffold.","client_message_id":"turn-1"}'

# Turn 2: Add a follow-up requirement
curl --no-buffer --silent --show-error --request POST \
  "$BASE_URL/sessions/$SESSION_ID/ai-stream" \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{"content":"Add unit tests and a CI configuration to the project.","client_message_id":"turn-2"}'
```

> Wait for the foreground turn to finish before sending the next message.
> Sending while the Session is still `BUSY` or `INTERRUPTING` returns HTTP 409.

## Share a Session

Open the Session and select **Share** to create a read-only link. Choose an
expiry time and whether viewers may download workspace files. The link itself
grants read access, so send it only to intended viewers and revoke it when it
is no longer needed. Viewers cannot send messages or change the Session.

## Best practices

1. **Treat Agent configuration as live** — A Session does not pin an Agent
   version. Saving an Agent does not alter a running turn, but a recreated
   runtime uses the current saved Agent and Environment configuration.
2. **Use stable request identifiers** — Give each Session creation request a
   stable `Idempotency-Key` and each message a stable `client_message_id` for
   duplicate prevention and traceability.
3. **Interrupt and archive intentionally** — Interrupt a foreground turn that
   should stop; archive the Session only when it should leave the active list
   and release its runtime.

## FAQ

**Q: What happens if I send a message to a `BUSY` Session?**

A: `POST /sessions/{id}/ai-stream` returns `HTTP 409 SESSION_BUSY`. Wait for the
foreground turn to settle or interrupt it. Integrations that submit input and
consume output separately can use `POST /sessions/{id}/turn-inputs`; whether
input can join an active turn follows the selected Agent program.

**Q: Can I still use a Session after interrupting?**

A: Yes. After the active turn settles, send the next message to continue.
Interrupting a turn does not delete the Session or its history.

**Q: How do I get the complete conversation history?**

A: Call `GET /sessions/{id}/messages` and page backwards with `before`. During
an active turn, the first page also includes messages that are still being
generated.

**Q: How do I reconnect after an SSE disconnect?**

A: Save `data.frameSeq` from the latest `data-resume-cursor` frame, then
reconnect with the `after_seq` query parameter. AstraBox does not consume
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
