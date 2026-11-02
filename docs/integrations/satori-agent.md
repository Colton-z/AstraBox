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

All methods below use POST with a JSON object. Unsupported APIs return 404.

| Path after `/v1/` | Body | Result |
| --- | --- | --- |
| `login.get` | `{}` | Login and supported standard API features |
| `channel.create` | `{guild_id: agent_id, data: {type: 0, name?: string}}` | New Channel |
| `channel.list` | `{guild_id: agent_id, next?: string}` | Caller-owned Channels, up to 100, and `next` |
| `channel.get` | `{channel_id}` | Owned Channel |
| `message.create` | `{channel_id, content}` | Array containing the accepted input Message |
| `message.get` | `{channel_id, message_id}` | Message text projection |
| `message.list` | `{channel_id, next?, direction?: "before", order?: "asc" or "desc", limit?: number}` | Native-ordered history page and `prev`/`next` |

`message.list` starts at the newest page, returns ascending native conversation
order by default and pages backwards. Limit defaults to 50 and is capped at 100.
Only `before` direction is supported; unsupported options are refused. Text is
Satori-escaped. Input currently accepts text elements only and refuses other
markup; it does not silently discard attachments. Full output content, including
reasoning, tools, files and interactions, is available through the internal
history and output interfaces below. Standard Message content is its text view.

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

## Events and reconnect

Connect WebSocket `/v1/events`; send `{op: 3, body: {token, sn?}}`. The token is
resolved through the same host identity seam as HTTP. READY (`op: 4`) contains
this Agent's Login. Send PING (`op: 1`) at least every ten seconds; PONG is `op: 2`.
Failure to identify or keep the connection alive closes it. Identity and Agent
access are rechecked while the connection is open.

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

Replay uses the shared database on every replica. A locked feed counter, output
positions and event references commit together; there is no per-process replay
cache or execution queue. References read the existing immutable output log.
Retention follows the Session's persisted records, and does not require a
persistent workspace volume. `sn` orders delivery across Channels; within one
conversation, use core native message order from full history and position
chunks, never input submission timestamps. Structured output is not represented
by standard text alone.

## Deployment and example

The endpoint is hosted by the existing AstraBox server; no additional Satori
service is required. Publish its HTTP and WebSocket paths through the same
reverse proxy and install the deployment's identity/preparation providers on
each replica. Replicas must use the same Environment database and ordinary
coordination configuration. Keep the backend's credentials server-side. The
external browser can continue to contact only its business backend.

Run `examples/satori-agent/client.py` after setting the URL, Agent id and a token
file accepted by your provider. It performs preflight, creates one Channel,
submits one keyed input and prints events until the core response ends. The
example deliberately reports pending authorization/interaction instead of
pretending that it is a successful text answer.

The facade uses Satori Python core 1.3.11 and the unmodified MIT-licensed
WebsocketConnection component from server 1.3.11; notices are in
`astrabox/vendor/`. It does not implement social/group administration, uploads,
webhooks, a consent UI or enterprise employee-system adapters. It does not
change the existing inbound-channel client or vendor agent runtimes.
