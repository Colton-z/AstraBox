# SSE Event Stream

AstraBox streams public Session output over **Server-Sent Events (SSE)** using
the [AI SDK UI Message Stream
protocol](https://ai-sdk.dev/docs/ai-sdk-ui/stream-protocol). A single
connection delivers every frame as it happens — no polling required.

## Connection URL

```text
GET /api/v1/sessions/{session_id}/ai-stream?follow=session
```

Request headers:

```text
Authorization: Bearer $ACCESS_TOKEN
Accept: text/event-stream
```

The `follow=session` connection may open while the Session is idle. It waits
for output, delivers one assistant reply, and then closes. Open the next
connection with the last `after_seq` cursor to wait for the following reply.
Several queued inputs can produce separate replies within one platform turn;
closing a reply's connection does not stop that turn or its engine.

You can also send a message and receive that turn on one request:

```text
POST /api/v1/sessions/{session_id}/ai-stream
```

The response declares `x-vercel-ai-ui-message-stream: v1`. Replayable semantic
frames are persisted while output streams, so a client can reconnect after a
network interruption. Live delta chunks may be coalesced in durable history.
AstraBox does not use the SSE `Last-Event-ID` header; resume with `after_seq` as
described below.

## SSE Format

Each frame is a JSON object in the SSE `data` field:

```text
data: {"type":"start","messageId":"RESPONSE_MESSAGE_ID","messageMetadata":{"turn_id":"TURN_ID"}}

data: {"type":"text-start","id":"text-1"}

data: {"type":"text-delta","id":"text-1","delta":"Hello"}

data: {"type":"text-end","id":"text-1"}

data: {"type":"finish","finishReason":"stop"}

data: {"type":"data-resume-cursor","transient":true,"data":{"frameSeq":42,"turnId":"TURN_ID"}}

data: [DONE]
```

| Field | Description |
| --- | --- |
| `type` | AI SDK message-part type; it determines the rest of the JSON shape. |
| `id` or `toolCallId` | Identifier shared by the related start, delta, and end frames. |
| `data` | Payload of an AstraBox `data-*` part. |

Heartbeat comments may be sent to keep the connection alive.

## Message Deltas

Incremental text output begins with `text-start`, followed by one or more
`text-delta` frames and `text-end`:

```text
data: {"type":"text-start","id":"text-1"}

data: {"type":"text-delta","id":"text-1","delta":"Hello"}

data: {"type":"text-end","id":"text-1"}
```

Reasoning output, when the selected Agent program provides it, follows the
same pattern:

```text
data: {"type":"reasoning-start","id":"reasoning-1"}

data: {"type":"reasoning-delta","id":"reasoning-1","delta":"Inspecting the repository"}

data: {"type":"reasoning-end","id":"reasoning-1"}
```

The `id` is identical for all frames in the same text or reasoning block.
Tool input uses a corresponding `tool-input-start`, `tool-input-delta`, and
`tool-input-available` sequence linked by `toolCallId`.

## Reconnecting During Message Deltas

Use the last `frameSeq` received in a `data-resume-cursor` part as `after_seq`
when reconnecting. Behavior depends on the cursor and the message structure:

1. **The cursor is within a reply.** The stream replays that reply from its
   input-consumption boundary, including its stable message ID and all text
   and tool parts, then continues with new deltas. The AI SDK creates fresh
   parser state on resume; a cursor alone cannot reconstruct earlier parts.
2. **The cursor is at a completed reply boundary.** That reply stays outside
   the next response. The stream waits for or replays the following reply.
3. **The turn has completed but its tail was not received.** The stream rebuilds
   the last unfinished reply through its terminal frame and cursor, then
   closes with `[DONE]`.

Text and reasoning resumes synthesize a missing `*-start` frame when necessary
so the next delta remains valid AI SDK input. Clients should still process
parts by their IDs and treat replayed parts idempotently.

Reply-content replay does not repeat a `data-session-store-reload` notification
at or before the requested `after_seq`. That cursor acknowledges the notification
even when the content reader rewinds to reconstruct the SDK message. A newer
reload notification still requires a SessionStore rebuild before continuation.

## Event Catalog

The stream follows the AI SDK protocol rather than defining an AstraBox event
vocabulary. These are the frame families an integration normally handles:

| Frame family | Meaning | Examples |
| --- | --- | --- |
| Message and step lifecycle | An assistant message or model step started or finished. | `start`, `start-step`, `finish-step`, `finish` |
| Text and reasoning | Incremental Agent output. | `text-*`, `reasoning-*` |
| Tool calls | Tool input, approval, and result. | `tool-input-*`, `tool-approval-*`, `tool-output-*` |
| Interaction | A question, decision, or approval that requires a response. | `data-interaction` |
| Turn correlation | Associates accepted input and result data with the turn. | `data-turn-accepted`, `data-input-consumed`, `data-result`, `data-turn-failure` |
| Child-run change | Signals that the Session's child-run view should be refreshed. | `data-child-runs-changed` |
| Resume | Provides a safe durable cursor. | `data-resume-cursor` |
| Error | Reports a stream failure. | `error` |

Standard AI SDK parts retain their documented shapes. `data-*` parts carry an
object in `data`; treat fields not documented by your integration as additive.

## Common Event Flow

```text
data-turn-accepted
start
data-input-consumed
start-step
reasoning-start / reasoning-delta / reasoning-end  (optional)
text-start / text-delta / text-end
tool-input-* / tool-output-*                       (optional)
finish-step
data-result
finish
data-resume-cursor
[DONE]
```

Not every turn contains every frame. An Agent program can emit multiple model
steps and tool calls. Child tasks can also produce a transient
`data-child-runs-changed` notification; read the Session child-run endpoint for
their current state.

### Model results

`data-result` contains the usage or cost values reported by the Agent program
for the completed turn. Available fields differ by Agent program and model
service. For example:

```json
{
  "type": "data-result",
  "data": {
    "total_cost_usd": 0.0137,
    "usage": {
      "input_tokens": 1204,
      "output_tokens": 88
    }
  }
}
```

Treat the `data` object as additive and do not assume every Agent program
reports every metric.

## Connection lifecycle

- A `finish` frame with `finishReason: "stop"` ends the current reply. When its
  `messageMetadata.response_boundary` is `true`, the platform turn continues
  across a FIFO response boundary. Use Session state to judge turn completion.
  The response includes a durable resume cursor and `[DONE]` before closing.
- An `error` frame terminates the current response. Save any resume cursor sent
  before it and inspect the Session before deciding whether to retry.
- A `finish` frame with `finishReason: "tool-calls"` can mark an interaction
  point inside the same turn; after the interaction is answered, that turn
  continues on the Session stream.
- For network drops, reconnect with `after_seq`. A deleted or inaccessible
  Session is reported as an HTTP error when the next connection is opened.

## Tool call pairs

Every completed tool invocation is linked by `toolCallId`.

Key tool-input frames:

```json
{
  "type": "tool-input-available",
  "toolCallId": "tool-call-1",
  "toolName": "Bash",
  "input": {"command": "pwd"}
}
```

The matching output uses the same `toolCallId`:

```json
{
  "type": "tool-output-available",
  "toolCallId": "tool-call-1",
  "output": "/workspace"
}
```

Failed or rejected calls use `tool-output-error` or `tool-output-denied`.
Process frames in stream order; do not sort tool calls by their identifier.

## Tool Responses

When the stream emits a `data-interaction` part or `tool-approval-request`,
send the response to `POST
/api/v1/sessions/{session_id}/interaction-respond` with the interaction ID:

```json
{
  "interaction_id": "INTERACTION_ID",
  "answer": {
    "decision": "approve"
  }
}
```

The response shape depends on the interaction's `presentation`: a form carries
question answers, a decision uses one of the declared option IDs, and a tool
approval accepts `approve` or `reject`. See [Permission
Modes](permission-modes.md) for complete examples.

## `finish` full schema

The terminal `finish` part names the AI SDK finish reason:

```json
{
  "type": "finish",
  "finishReason": "stop"
}
```

On a `follow=session` connection, AstraBox follows it with a transient durable
cursor:

```json
{
  "type": "data-resume-cursor",
  "transient": true,
  "data": {
    "frameSeq": 42,
    "turnId": "TURN_ID"
  }
}
```

The transport then sends `data: [DONE]` and closes the response. Save
`frameSeq`; do not derive a cursor from a message, block, or tool identifier.

## Reconnect with `after_seq`

A new Session-follow connection replays durable frames from the beginning by
default. To continue from a received cursor, pass `after_seq`:

```text
GET /api/v1/sessions/{session_id}/ai-stream?follow=session&after_seq=42
```

AstraBox emits `data-resume-cursor` only at a structurally safe replay point.
The server also rewinds within an active reply to include its identity and
complete content. Replace that reply by message ID; do not append its replay
as another assistant message.

> For long-running connections, update the saved cursor whenever a
> `data-resume-cursor` part arrives and use its `frameSeq` for the next request.

## Frame delivery cadence

AstraBox does not expose `delta_flush_interval_ms`. Text, reasoning, and tool
input frames are forwarded as the selected Agent program produces them. The
transport may send keepalive comments while no output is available.

## Event History

Use the messages endpoint for durable conversation history and pagination:

```bash
curl --silent --show-error \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/messages?limit=20" \
  --header "Authorization: Bearer $ACCESS_TOKEN"
```

The response contains saved messages rather than raw transport frames. Use
`before` to page backwards. To resume raw streamed output, use the
`data-resume-cursor` and `after_seq` flow above.

## curl Examples

```bash
curl --no-buffer --silent --show-error \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Accept: text/event-stream' \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/ai-stream?follow=session"
```

Resuming with `after_seq`:

```bash
curl --no-buffer --silent --show-error \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Accept: text/event-stream' \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/ai-stream?follow=session&after_seq=42"
```

## JavaScript EventSource Example

The browser's native `EventSource` uses the signed-in same-origin cookie. It
cannot add an `Authorization` header; bearer-token clients should use `fetch`
with an SSE parser instead.

```javascript
let afterSeq = -1;

function followSession(sessionId) {
  const url = new URL(`/api/v1/sessions/${sessionId}/ai-stream`, window.location.origin);
  url.searchParams.set('follow', 'session');
  if (afterSeq >= 0) url.searchParams.set('after_seq', String(afterSeq));

  const stream = new EventSource(url, {withCredentials: true});

  stream.onmessage = (event) => {
    if (event.data === '[DONE]') {
      stream.close();
      followSession(sessionId);
      return;
    }

    const part = JSON.parse(event.data);
    if (part.type === 'text-delta') {
      console.log('Agent:', part.delta);
    }
    if (part.type === 'data-resume-cursor') {
      afterSeq = part.data.frameSeq;
    }
    if (part.type === 'error') {
      console.error('Error:', part.errorText);
    }
  };

  stream.onerror = () => {
    stream.close();
    followSession(sessionId);
  };
}
```

## Client Implementation Tips

1. <b>Track the last safe cursor</b> — save `data-resume-cursor.data.frameSeq`
   and use it as `after_seq` after disconnects.
2. <b>Use an AI SDK parser</b> — preserve the start/delta/end order and IDs for
   text, reasoning, and tool parts.
3. <b>Watch reply completion</b> — after clean response EOF, open a new
   `follow=session` response for the next reply. Inspect Session state on error.
4. <b>Reconnect on transport loss</b> — reopen from the last safe cursor; do not
   guess from the last text delta you rendered.
5. <b>Handle documented data parts</b> — process the `data-*` types your client
   needs and ignore additive fields it does not understand.

## FAQ

<b>Q: I'm getting a flood of historical events on connect. How do I avoid this?</b>

A: Pass `after_seq` set to the last `frameSeq` from a `data-resume-cursor` part.
Completed replies before that cursor are omitted. An active reply is replayed
with its full content and stable identity so the SDK can replace it.

<b>Q: Will the SSE connection close on its own?</b>

A: A `follow=session` response closes at one assistant reply's boundary.
Open the next response with `after_seq` to wait for the following reply. While
the Session is idle, the current response stays open and receives keepalives.

<b>Q: Can I use WebSockets instead?</b>

A: The Session message stream uses SSE. SSE fits the one-way output stream and
supports reconnection through AstraBox's durable cursor.

<b>Q: Can I set `delta_flush_interval_ms`?</b>

A: No. AstraBox forwards frames at the cadence produced by the selected Agent
program and does not expose a separate flush interval.
