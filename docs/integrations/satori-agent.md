# External chat through Satori

AstraBox serves a fixed Agent at `/api/satori/agents/{agent_id}/v1`. An external
business backend can use Satori v1 HTTP and WebSocket connections here. This is
separate from the existing **Satori Protocol Server** channel configuration,
which makes AstraBox a client of an operator's external Satori server.

Employees stay in the business application's login flow. They do not need to
visit AstraBox's login page. The deployment's `WebIdentityResolver` verifies the
backend's delegated execution identity and returns the existing `UserContext`.
That context selects personal resources, Agent access and Session ownership. A
host can verify a delegated bearer or bind a trusted backend credential to a
verified employee assertion. The provider must validate that binding; AstraBox
never turns a plain employee field into an execution identity. A static backend
credential alone does not enumerate other employees' conversations. The bundled
single-user, no-auth resolver remains single-user and must not expose this API to
untrusted callers. The protocol does not supply an employee directory or consent
service.

## Resources and credentials

| Satori resource | AstraBox meaning |
| --- | --- |
| Login | The endpoint's Agent bot: platform `astrabox`, user id `{agent_id}` |
| Guild | Virtual namespace with id `{agent_id}` for the caller's conversations |
| Channel | An owned core Session id; each creation starts an independent conversation |
| User | The verified execution principal, or the Agent bot on assistant output |
| Message returned by `message.create` | Accepted core input, with its `client_message_id` |
| Assistant Message | Core `response_id`; updates include its accumulated text |

For HTTP, send `Authorization: Bearer <provider-verifiable credential>`,
`Satori-Platform: astrabox` and `Satori-User-ID: <agent_id>`. The last header selects
the Agent Login; it does **not** identify an employee. Trusted assertion headers,
if used, are interpreted only by the configured identity provider. Read methods
require `astrabox:read` when the provider supplies API scopes; writes require
`astrabox:write`. Agent visibility/use and Session ownership are checked on each
operation. API responses are Satori objects, without the console API envelope;
failures use their HTTP status and `{code, message, data}`.

## HTTP API

All methods below use POST with a JSON object, except `upload.create`, which
takes `multipart/form-data`. Unsupported APIs return 404.

| Path after `/v1/` | Body | Result |
| --- | --- | --- |
| `login.get` | `{}` | Login and supported standard API features |
| `channel.create` | `{guild_id: agent_id, data: {type: 0, name?: string}}` | New Channel |
| `channel.list` | `{guild_id: agent_id, next?: string}` | Caller-owned Channels, up to 100, and `next` |
| `channel.get` | `{channel_id}` | Owned Channel |
| `message.create` | `{channel_id, content}` | Array containing the accepted input Message |
| `upload.create` | multipart: one file part per file | `{part name: resource link}` |
| `message.get` | `{channel_id, message_id}` | Message text projection |
| `message.list` | `{channel_id, next?, direction?: "before", order?: "asc" or "desc", limit?: number}` | Native-ordered history page and `prev`/`next` |

`message.list` starts at the newest page, returns ascending native conversation
order by default and pages backwards. Limit defaults to 50 and is capped at 100.
Only `before` direction is supported; unsupported options are refused. Text is
Satori-escaped. Standard Message content is the message's text, followed by an
`<img>` or `<file>` element for each file it carried or wrote (see
[Attachments](#attachments)). Full output content, including reasoning, tools and
interactions, is available through the internal history and output interfaces
below.

Send a stable `Idempotency-Key` (1–128 characters) on `channel.create` and
`message.create` when retrying the same business request. Creation uses the core
conversation idempotency key; message delivery uses the core input key within
that Session. Repeating a message key returns the original accepted input and
does not execute another input. Use a new key for a different message. A successful
`message.create` is **acceptance**, not execution completion.

## Authorization and interactions

These are explicit [Satori internal APIs](https://satori.chat/zh-CN/advanced/internal.html),
not additional standard resource fields. Prefix their paths with `/v1/internal/`.

| Method | Body | Core result |
| --- | --- | --- |
| `astrabox.preparation.get` | `{}` | `{applicable, ready, user_action, actions}` |
| `astrabox.preparation.perform` | `{action_id}` | Updated prerequisite state |
| `astrabox.history` | `{channel_id, before?}` | Full public UI messages, native order, pagination and current interaction |
| `astrabox.conversation.get` | `{channel_id}` | Core Session detail, current/last responses, held inputs and pending interaction |
| `astrabox.interaction.answer` | `{channel_id, interaction_id, answer}` | Core answer receipt |
| `astrabox.response.stop` | `{channel_id}` | Core interrupt receipt |
| `astrabox.files.list` | `{channel_id, path?}` | The Channel's workspace directory, each file with a resource `url` |

Query preparation **before creating a Channel**. Reads do not start consent,
issue credentials or allocate a Session or sandbox. Display the provider's
`user_action` and offer only its current `actions`. An explicit perform request
invokes the host's operation; the platform does not grant consent itself.
Providers receive `SessionUserActionContext(session_id=None, principal_id=...,
subject=PreparationSubject("agent", agent_id))` at this stage. Personal delegation
belongs to principal plus stable subject identity, not to a Session or Agent
revision. Existing Session operations call the same provider with a Session id.
Channel creation and message admission recheck the requirement. Execution still
uses the ordinary preparation hooks, which must check actual grants, including
revocation after admission.

For a pending questionnaire or approval, preserve its core interaction id and
public contract. Submit the answer object described by that contract to
`astrabox.interaction.answer`. This calls the existing core answer path; an
answer receipt is not a completed response. Stop likewise requests interruption;
read the resulting response outcome. Core response statuses (`running`,
`waiting`, `ended`) and outcomes (`completed`, `failed`, `cancelled`) are carried
verbatim. Input refusal remains a failed input, rather than a fabricated
successful assistant message. There is no separate agent execution loop here.

## Attachments

1. **Upload.** `POST /v1/upload.create` with the same headers and
   `multipart/form-data`: each part is one file with a unique `name`, a
   `filename` and a `Content-Type`. The response maps each part name to a link,
   `internal:astrabox/{agent_id}/_tmp/{id}`. The link belongs to the caller at
   this endpoint for 15 minutes or until a message sends it. Upload does not
   need a Channel or a running sandbox.
2. **Send.** Put the links in `message.create` content beside the text:
   `Compare these <img src="..."/><file src="..." title="report.csv"/>`.
   `<img>` sends the file to the model as an image; it must be PNG, JPEG, GIF or
   WebP. `<file>` sends any file. Each file is also written into the
   Channel's workspace at `uploads/{input_id}/{name}` before the engine reads the
   message, and the message tells the engine those paths. A message carries up
   to 10 files of up to 25 MiB each; an upload accepts up to 10 parts.
3. **Read back.** History and `message-updated` content list a person's files
   as `internal:astrabox/{agent_id}/{channel_id}/attachments/{id}`; the internal
   history also carries them in `metadata.attachments`. These links read the
   stored file for the Session's lifetime. A reply lists the workspace files its
   tools reported changing as
   `internal:astrabox/{agent_id}/{channel_id}/files/{path}`; files an agent
   produced another way (a script, a Bash command) are found with
   `astrabox.files.list`. Workspace links read the file while the Channel's
   workspace exists.
4. **Download.** `GET /v1/proxy/{link}` with the caller's `Authorization`
   header returns the bytes with the file's media type. A link is readable only
   by its uploader (`_tmp`) or by the Channel's owner (the others). This server
   proxies only its own `internal:` links and announces no `proxy_urls`.

| Code | Status | Meaning |
| --- | --- | --- |
| `INPUT_ATTACHMENT_UNSUPPORTED` | 400 | An element other than text, `img` or `file`; a link `upload.create` did not return; an `img` that is not a supported image |
| `INPUT_ATTACHMENT_TOO_LARGE` | 413 | A file over 25 MiB |
| `INPUT_ATTACHMENT_UNAVAILABLE` | 409 | The upload expired, another message sent it, or another caller or endpoint uploaded it; upload again |
| `ENGINE_CAPABILITY_UNAVAILABLE` | 409 | The Agent's engine does not take image input; send the file as `<file>` instead |

`ENGINE_CAPABILITY_UNAVAILABLE` is also an input's recorded failure when its
Session's runtime had not started at acceptance. `INPUT_ATTACHMENT_DELIVERY_FAILED`
is the recorded failure when the files could not be written into the workspace;
the engine did not read that message. Retrying `message.create` with the same
`Idempotency-Key` returns the original input and sends no file again.

## Events and reconnect

Connect WebSocket `/v1/events`; send `{op: 3, body: {token, sn?}}`. The token is
resolved through the same host identity seam as HTTP. READY (`op: 4`) contains
this Agent's Login. Send PING (`op: 1`) at least every ten seconds; PONG is `op: 2`.
Failure to identify or keep the connection alive closes it. Identity, Environment entry and Agent access are checked on connection.
HTTP operations resolve current identity and permissions on every request.

EVENT (`op: 0`) carries a Satori event with a per-principal/Agent `sn`:

- `message-created` opens an assistant response; `message-updated` projects its
  text when the core stream segment finishes. A finish can mean waiting for an
  interaction; it is not by itself a completed execution.
- `internal`, `_type: "astrabox.output"` carries `{session_id, response_id,
  first_seq, through_seq, parts}`. Parts are the existing public core UI chunks,
  including native message positions, errors and real interaction identifiers.
- `internal`, `_type: "astrabox.state"` carries the observed core Session state,
  runtime status and latest response status/outcome. Offline intermediate state
  observations coalesce. Read full history to inspect every earlier response.

Save `sn` only after processing an event. Reconnect with that value to replay
visible events with greater numbers. Omitting `sn` starts with future events;
use `sn: 0` to include retained earlier events. Cursors belong to this verified
principal and Agent and must not be transferred to another identity. An invalid
future cursor is refused. Clients should ignore an already processed `sn` if a
network failure occurred before they saved it. Deleting a Session removes its
replay entries, so event numbers can have gaps.

Replay uses the shared database on every replica. Each process captures a feed
once for all of its connections to that principal and Agent, about four times a
second while any is open, and a connection reads only the events it has not
sent. A capture reads without locks and writes nothing when nothing changed. It
commits new event numbers, output positions and event references together,
conditional on the feed counter it read, so captures on different replicas
never number an event twice. There is no per-process replay cache or execution
queue. References read the existing immutable output log.
Retention follows the Session's persisted records, and does not require a
persistent workspace volume. `sn` orders delivery across Channels; within one
conversation, use core native message order from full history and position
chunks, never input submission timestamps. Structured output is not represented
by standard text alone.

## Deployment and example

The endpoint is hosted by the existing AstraBox server; no additional Satori
service is required. Publish its HTTP and WebSocket paths through the same
reverse proxy and install the deployment's identity/preparation providers on
each replica. If `ASTRABOX_AUTH_EXEMPT_PREFIXES` is explicitly overridden, include
`/api/satori/`: this facade authenticates its own HTTP and IDENTIFY requests.
Replicas must use the same Environment database and ordinary
coordination configuration. Replicas sharing a Docker sandbox service must also
use identical `ASTRABOX_MCP_PROXY_BASE_URL`,
`ASTRABOX_SANDBOX_EGRESS_DNS_UPSTREAM` and
`ASTRABOX_SANDBOX_EGRESS_DENY_CIDRS`. These values bind sandbox callbacks, DNS and
network policy to the existing edges. A standalone web replica must receive the
same effective values that the managed launcher derives; a different network
fingerprint correctly refuses those sandboxes. Keep the backend's credentials server-side. The
external browser can continue to contact only its business backend.

Run `examples/satori-agent/client.py` after setting the URL, Agent id and a token
file accepted by your provider. It performs preflight, creates one Channel,
submits one keyed input and prints events until the core response ends. The
example deliberately reports pending authorization/interaction instead of
pretending that it is a successful text answer.

The facade uses Satori Python core 1.3.11 and the unmodified MIT-licensed
WebsocketConnection component from server 1.3.11; notices are in
`astrabox/vendor/`. It does not implement social/group administration,
webhooks, a consent UI or enterprise employee-system adapters. It does not
change the existing inbound-channel client or vendor agent runtimes.
