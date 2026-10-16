# SSE Event Stream

AstraBox streams public Session output over **Server-Sent Events (SSE)** using
the [AI SDK UI Message Stream
protocol](https://ai-sdk.dev/docs/ai-sdk-ui/stream-protocol). A connection
delivers every part as the Agent program produces it — no polling required.

## Connection URL

```text
GET /api/v1/sessions/{session_id}/stream?after_seq=42
```

Request headers:

```text
Authorization: Bearer $ACCESS_TOKEN
Accept: text/event-stream
```

The stream first sends the Session's recorded parts after `after_seq`, then
waits and sends new parts live. Each part is sent once. One connection ends
after the part that finishes a response (`finish`, or `error`), so each
connection carries one assistant reply; open the next connection with the last
cursor to wait for the following reply. Closing a connection does not stop the
response or the Agent program.

`after_seq` defaults to `0`, the start of the Session's output. To wait for the
next reply rather than read the Session's first one, start from the
`output_seq` that `GET /api/v1/sessions/{session_id}` and the first page of
`GET /api/v1/sessions/{session_id}/messages` return.

Send messages separately, with `POST /api/v1/sessions/{session_id}/inputs`; see
[Sessions](sessions.md). The response declares
`x-vercel-ai-ui-message-stream: v1`. AstraBox records every part it streams, so
a client can reconnect after a network interruption without losing output.
AstraBox does not use the SSE `Last-Event-ID` header; resume with `after_seq` as
described below.

## SSE Format

Each part is a JSON object in the SSE `data` field:

```text
data: {"type":"start","messageId":"RESPONSE_ID","messageMetadata":{"response_id":"RESPONSE_ID","input_ids":["INPUT_ID"],"origin":"input"}}

data: {"type":"text-start","id":"text-1"}

data: {"type":"text-delta","id":"text-1","delta":"Hello"}

data: {"type":"text-end","id":"text-1"}

data: {"type":"finish","finishReason":"stop"}

data: {"type":"data-resume-cursor","transient":true,"data":{"seq":42}}

data: [DONE]
```

| Field | Description |
| --- | --- |
| `type` | AI SDK message-part type; it determines the rest of the JSON shape. |
| `id` or `toolCallId` | Identifier shared by the related start, delta, and end parts. |
| `data` | Payload of an AstraBox `data-*` part. |

Keep-alive comments are sent after 15 seconds without output.

## Responses

A response is the Agent program's reply from the moment it starts until it
ends, as the program reports both. Its parts begin with a `start` part whose
`messageId` is the response ID and whose `messageMetadata` carries:

| Field | Meaning |
| --- | --- |
| `response_id` | The response's ID. |
| `input_ids` | The messages this response answers. One response can answer several queued messages. |
| `origin` | `input` when the response answers messages; `engine` when the Agent program started it on its own, such as after a background task finished or a scheduled job ran. No user message precedes an `engine` response. |

The response ends with a `finish` part. A response that fails sends an `error`
part before its `finish`.

## Message Deltas

Incremental text output begins with `text-start`, followed by one or more
`text-delta` parts and `text-end`:

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

The `id` is identical for all parts in the same text or reasoning block.
Tool input uses a corresponding `tool-input-start`, `tool-input-delta`, and
`tool-input-available` sequence linked by `toolCallId`.

## Reconnecting During Message Deltas

Use the last `seq` received in a `data-resume-cursor` part as `after_seq` when
reconnecting. The stream continues with the first part after that position, so
a client that keeps its parser state for the open reply continues it exactly
where it stopped, without repeated or missing parts.

A client that has lost its parser state, such as a page that reloads, reads the
conversation from `GET /api/v1/sessions/{session_id}/messages` instead. The
first page folds every recorded part of an open reply into its message and
returns `output_seq`; following the stream from `output_seq` adds the rest of
that reply exactly once.

## Event Catalog

The stream follows the AI SDK protocol rather than defining an AstraBox event
vocabulary. These are the part families an integration normally handles:

| Part family | Meaning | Examples |
| --- | --- | --- |
| Message and step lifecycle | A response started or finished, or a model step started or finished. | `start`, `start-step`, `finish-step`, `finish` |
| Text and reasoning | Incremental Agent output. | `text-*`, `reasoning-*` |
| Tool calls | Tool input, approval, and result. | `tool-input-*`, `tool-approval-request`, `tool-output-*` |
| Interaction | A question, decision, or approval that requires a response. | `data-interaction` |
| Failed message | A message that will not be answered; it starts no response. | `data-input-failed` |
| Tool cards | A card a platform tool recorded for the conversation, such as an artifact, bound to that tool call. | `data-tool-card` |
| Session change | Transient: the Session record changed, such as its generated title; read the Session again. | `data-session-changed` |
| Child runs | A child run's lifecycle or messages; read the Session child-run endpoint for their current state. | `data-subagent` |
| File changes | The files a tool call changed. | `data-file-changes` |
| Results | Usage or cost an Agent program reports for a response, when its adapter reports them. | `data-result` |
| Resume | Provides the cursor to reconnect from. | `data-resume-cursor` |
| Error | Reports a failed response or a stream failure. | `error` |

Standard AI SDK parts retain their documented shapes. `data-*` parts carry an
object in `data`; treat fields not documented by your integration as additive.

## Common Event Flow

```text
start
start-step
reasoning-start / reasoning-delta / reasoning-end  (optional)
text-start / text-delta / text-end
tool-input-* / tool-output-*                       (optional)
finish-step
data-result                                        (optional)
finish
data-resume-cursor
[DONE]
```

Not every response contains every part. An Agent program can emit multiple
model steps and tool calls. Parts that belong to no response, such as a child
run's status, can arrive between responses; they do not end the connection.

### Model results

`data-result` contains the usage or cost values the Agent program reported for
the response. Available fields differ by Agent program and model service; the
Claude Code adapter reports them, for example:

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

### Failed messages

A message that will not be answered is reported on the stream instead of a
response: the Agent program or the deployment's preparation refused it, the
conversation ended before it was taken, or its runtime cannot start until the
person acts.

```json
{
  "type": "data-input-failed",
  "data": {
    "input_id": "INPUT_ID",
    "code": "TURN_PREPARATION_FAILED",
    "message": "sandbox turn preparation failed"
  }
}
```

`data` can also carry `status_code` and, when the user must act first
(`TURN_PREPARATION_USER_ACTION_REQUIRED`), `user_action` with a message and an
optional URL. A message that failed is not sent again; send its text as a new
message.

## Connection lifecycle

- A `finish` part ends the current reply and the connection, after a
  `data-resume-cursor` part and `[DONE]`.
- An `error` part reports a failed response and ends the connection. Its
  `data-resume-cursor` part arrives before the `error` part, because an AI SDK
  reader stops at an error. The response's own `finish` part follows the
  `error` part in the Session's output, so the next connection starts with it.
  A response lost with its sandbox ends this way, and its `finish` part carries
  `messageMetadata.outcome: "failed"`. Inspect the Session before deciding
  whether to send the message again.
- A `finish` part with `finishReason: "tool-calls"` after a
  `data-interaction` part marks an interaction point: the response is paused,
  not ended. After the interaction is answered, the response continues under
  the same `messageId`, starting with a new `start` part, on the next
  connection.
- For network drops, reconnect with `after_seq`. A deleted or inaccessible
  Session is reported as an HTTP error when the next connection is opened.

## Tool call pairs

Every completed tool invocation is linked by `toolCallId`.

Key tool-input parts:

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
Process parts in stream order; do not sort tool calls by their identifier.

## Tool Responses

When the stream emits a `data-interaction` part or `tool-approval-request`,
send the response to `POST
/api/v1/sessions/{session_id}/interactions/{interaction_id}/answer` with the
interaction ID from the part:

```json
{
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

AstraBox follows it with a transient cursor:

```json
{
  "type": "data-resume-cursor",
  "transient": true,
  "data": {
    "seq": 42
  }
}
```

The transport then sends `data: [DONE]` and closes the response. Save `seq`;
do not derive a cursor from a message, block, or tool identifier.

## Reconnect with `after_seq`

A new connection sends the Session's output from the beginning by default. To
continue from a received cursor, pass `after_seq`:

```text
GET /api/v1/sessions/{session_id}/stream?after_seq=42
```

AstraBox sends a `data-resume-cursor` part after each burst of parts. Each
cursor names the last part sent, so reconnecting with it continues with the
next part.

> For long-running connections, update the saved cursor whenever a
> `data-resume-cursor` part arrives and use its `seq` for the next request.

## Frame delivery cadence

AstraBox does not expose `delta_flush_interval_ms`. Text, reasoning, and tool
input parts are forwarded as the selected Agent program produces them. The
transport may send keepalive comments while no output is available.

## Event History

Use the messages endpoint for durable conversation history and pagination:

```bash
curl --silent --show-error \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/messages?limit=20" \
  --header "Authorization: Bearer $ACCESS_TOKEN"
```

The response contains AI SDK UI messages: one for each message sent to the
Session, and one for each response, folded from the same parts the stream
sends. Use `before` to page backwards. To continue streamed output, use the
`data-resume-cursor` and `after_seq` flow above.

## curl Examples

```bash
curl --no-buffer --silent --show-error \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Accept: text/event-stream' \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/stream"
```

Resuming with `after_seq`:

```bash
curl --no-buffer --silent --show-error \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Accept: text/event-stream' \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/stream?after_seq=42"
```

## JavaScript EventSource Example

The browser's native `EventSource` uses the signed-in same-origin cookie. It
cannot add an `Authorization` header; bearer-token clients should use `fetch`
with an SSE parser instead.

```javascript
let afterSeq = 0;

function followSession(sessionId) {
  const url = new URL(`/api/v1/sessions/${sessionId}/stream`, window.location.origin);
  url.searchParams.set('after_seq', String(afterSeq));

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
      afterSeq = part.data.seq;
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

1. <b>Track the last cursor</b> — save `data-resume-cursor.data.seq` and use it
   as `after_seq` after disconnects.
2. <b>Use an AI SDK parser</b> — preserve the start/delta/end order and IDs for
   text, reasoning, and tool parts.
3. <b>Watch reply completion</b> — after a clean end of the response, open a new
   connection for the next reply. Inspect the Session on error.
4. <b>Reconnect on transport loss</b> — reopen from the last cursor; do not
   guess from the last text delta you rendered.
5. <b>Handle documented data parts</b> — process the `data-*` types your client
   needs and ignore additive fields it does not understand.

## FAQ

<b>Q: I'm getting a flood of historical events on connect. How do I avoid this?</b>

A: Pass `after_seq` set to the last `seq` from a `data-resume-cursor` part, or
to the Session's `output_seq`. Parts at or before that position are not sent.

<b>Q: Will the SSE connection close on its own?</b>

A: A connection closes after one reply ends. Open the next connection with
`after_seq` to wait for the following reply. While the Session is idle, the
current connection stays open and receives keepalives.

<b>Q: Can I use WebSockets instead?</b>

A: The Session message stream uses SSE. SSE fits the one-way output stream and
supports reconnection through AstraBox's recorded cursor.

<b>Q: Can I set `delta_flush_interval_ms`?</b>

A: No. AstraBox forwards parts at the cadence produced by the selected Agent
program and does not expose a separate flush interval.
