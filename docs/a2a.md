# Agent-to-Agent communication

An Assistant uses Hermes' bundled A2A tools to discover and call other Agents.
AstraBox supplies the Assistant owner's accessible Agents as peers, with
credentials scoped to its workspace and each target. The target executes in a
normal AstraBox Session, owned by the caller, through its configured engine.

Hermes is pinned to 0.22.4, commit
`4d04303818b61bf3aa09278af48356315515965b`. Its
[A2A plugin](https://github.com/NousResearch/hermes-agent/blob/4d04303818b61bf3aa09278af48356315515965b/website/docs/user-guide/messaging/a2a.md)
provides `a2a_discover`, `a2a_call`, `a2a_list`, `a2a_history` and
`a2a_orchestrate`. The Assistant's Hermes gateway serves only the Assistant's own
conversations: it is kept off every one of its messaging platforms, including
its inbound A2A platform, even when a platform's credential is among the
RuntimeProfile's variables, and its managed configuration refuses the
`platforms` and `gateway` keys. A managed Agent executes in an AstraBox Session,
not in that gateway.

For long managed-Agent tasks, load the built-in `astrabox-agent-task` skill.
It submits through the existing A2A API with `returnImmediately=true`, retains
the task reference, and subscribes to that task from a Hermes background
terminal process with native completion notification. The Assistant reads the
result through `get_agent_task` and reports it in the originating conversation.
An interrupted observation connection does not fail or resubmit the task.
Background notifications must be enabled for a reply after the current turn;
otherwise the Assistant must wait with its native process tools. After runtime
loss, the saved task reference permits another query; the observer process
itself does not survive sandbox loss. The synchronous native `a2a_call` remains
subject to its configured peer timeout.

## Calling a managed Agent

With Hermes' native tools, call `a2a_list` and copy the exact peer key into
`a2a_call.agent`, for example `astrabox/<agent_id>`. A bare Agent ID is not a
peer key. The configured key selects the existing credentials; the discovery
URL is not an interchangeable call name. Passing a direct HTTP(S) URL is an
independent, unauthenticated call. Agent Card failures stop the call before any
task is sent; an authentication error does not trigger a POST to the base URL.

An authenticated external client fetches the Agent Card at
`/api/v1/agents/{agent_id}/.well-known/agent-card.json` and uses the JSON-RPC
URL advertised there. These routes use the deployment's configured web identity.
The server transport is the official Python `a2a-sdk` 1.2.2.
The binding is A2A 1.0 JSON-RPC with SSE. RPC clients send
`A2A-Version: 1.0`; patch versions are ignored, while other major or minor
versions are refused. REST and gRPC bindings are not exposed.

Assistant runtimes use the corresponding `/api/v1/assistant-a2a/` capability
routes. Their bearer authorizes one workspace and target; it is not a general
API credential. The receiver resolves the current Assistant owner from platform
records and checks target access on every operation. Request content cannot
select another employee or acquire the Agent owner's identity.

The A2A context identifies a conversation within the caller, organization and
target Agent. A new context creates a platform Session; later messages in that
context continue it. A Task is one message in that Session, and its state is
the state of the response that answers the message, as the Agent's runtime
reports it. Task metadata includes the platform Session id and the message's
input id, and text results include the Session's console link.

Repeating an identical `messageId` reuses its input. Reusing it with different
message content is refused. Hermes generates its own message ids, so asking
the Assistant to call again is a new request, not an idempotent retry.

## Message an existing conversation

Agent administrators configure **Agent collaboration** on the Agent's management
page: **Disabled**, **All accessible Agents**, or **Selected Agents**. Selected
Agents are picked by name. The Agent configuration in the database is the
source of this scope; conversations do not store a separate copy. Enabling the
tools takes effect when a new runtime loads the Agent configuration.

The built-in `agents` MCP server exposes these tools to enabled Agent
conversations and to Assistants:

- `list_agent_conversations` searches Agent names and current conversation
  titles, including AI-generated titles. It returns
  Session IDs, A2A context IDs and console links. Use `next_cursor` to continue
  a page. Names and titles can repeat or change; route by the returned IDs.
- `send_agent_message` sends to an existing `agent_id` and `session_id`, using a
  stable `message_id` for an identical retry. It returns an A2A Task; read
  `get_agent_task` for its result. This does not create another conversation.

Discovery and messaging are limited to the caller's own Agent conversations in
this Environment. The Agent's configured scope does not grant access to another
user's history or bypass the target Agent's existing access control. Channel
conversations do not receive these personal tools.

Messages include the platform-resolved sender identity and conversation link,
and are labelled as messages from another conversation rather than direct user
instructions. The target continues its native Session context. Busy-input
admission, scheduling and response boundaries remain the target engine's;
AstraBox does not add a messaging queue. A pending Task does not mean the target
has already answered.

## Interaction boundaries

The pinned Hermes toolset sends text and returns text, context and state. It
does not expose standalone task cancellation, subscription, or structured
approval tools. Those operations cannot be assumed available merely because
they are A2A protocol methods. AstraBox supplements native text dispatch with
`get_agent_task(agent_id, task_id)` and
`answer_agent_task(agent_id, task_id, interaction_id, answer, message_id)` in
the Assistant's signed `agents` MCP server. Pending text includes the explicit
Task and interaction ids and declared decision options. Read the Task's public
interaction contract, obtain the user's explicit answer or authorization, then
submit that answer for those exact ids. Preserve the declared decision and its
scope: Codex may offer `accept` or `acceptForSession`, not a generic `approve`.
Reuse `message_id` only for an identical retry. The answer tool returns the
current Task without waiting for execution to finish; read `get_agent_task`
for subsequent progress.

These two tools call the same A2A service as native peers, with the current
Assistant owner resolved from its workspace binding on every call. Task access,
target access and interaction validity remain enforced by that service. Results
have the same public shape. No Hermes plugin replacement or separate Agent
execution path is needed: the MCP bridge supplies the structured operation the
pinned text-only tool cannot encode.

Task-scoped cancellation is refused: the platform interrupt stops the
runtime's response in progress, which can answer several queued messages, and
cannot fence cancellation to one A2A Task. A pending platform approval requires
the explicit interaction id and answer; a new text message is not approval.

The server accepts text tasks and exposes results and platform interaction data.
It does not accept uploaded file parts or push-notification configurations.
`SendMessage`, `SendStreamingMessage`, `GetTask`, `ListTasks` and
`SubscribeToTask` read the same recorded responses. A new task's message is
accepted while its Session is still starting; the platform hands it to the
Agent once the Session's runtime is ready.

Streaming clients may disconnect without cancelling accepted execution. A
subscription starts with the current Task, then emits an update each time the
response's recorded state or result changes.
Binary tool approvals use `AUTH_REQUIRED`; forms and decisions with
adapter-declared options use `INPUT_REQUIRED`, including Codex's approval choices.
Both keep the stream open. A structured answer resumes the original Task;
the stream closes when that task reaches a terminal outcome. Subscribing to an
already terminal Task or sending it a new answer is refused. Use `GetTask` to
read its result.

`historyLength` limits the returned Task history. `acceptedOutputModes` can
select text, JSON, or both; a single format converts the other result parts into
that format, retaining their content. Unrestricted Task reads retain the reply's
text and its message parts, the same parts the Session's history shows. Runtime locators and raw diagnostic envelopes remain server-side.
The protocol definition is the
[A2A 1.0 specification](https://a2a-protocol.org/v1.0.0/specification/);
the SDK handles its transport, while AstraBox reads its task semantics from the
responses the Agent's runtime reports.
Hermes' native A2A conversation files and audit file are included in the profile
state archive, outside the user's workspace.

## Personal Agent schedules

The Assistant's built-in schedule tools manage `/api/v1/me/schedules`. These
reuse the platform scheduler, Deployment and Run records. The source Assistant
records where the plan was created; the target Agent performs the work.

A personal schedule always executes as its authenticated creator. Creating,
enabling or firing a plan checks target access. Revoking access blocks new work
while the owner can still inspect, disable or remove the plan. Another user,
including the target Agent's owner, cannot manage that personal plan through
the Agent's deployment administration. A2A communication does not create a
second scheduler.

Open the account settings menu and choose **Personal schedules** to inspect
these plans. Select a plan in the dialog to edit its name, prompt, cron and
timezone, enable or disable it, run it now, replay a previous Run, or delete it.
Each Run with a Session links to its conversation. Create a plan by asking the
Assistant which Agent should perform the work and when it should run.
