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

A new task's message may name earlier tasks in `referenceTaskIds`, at most ten,
each a task the same caller sent to any Agent it can still use; another id is
refused with `InvalidParamsError`. The Agent receives, below the message, each
referenced task as it stands: its id, Agent, state, status message, the start
of its result text and the code references the
[task outcome extension](#task-outcome-v1) gives it. A message that answers a
task's interaction takes no references.

## Task outcomes

A conversation that an A2A context created has one more platform tool, on the
`a2a` MCP server: `report_outcome`. The Agent calls it before its final reply to
say what became of the request, whatever its engine (Claude Code, Codex, Pi or
the DeepSeek Harness):

| `state` | Meaning | The task |
|---|---|---|
| `TASK_STATE_COMPLETED` | It did what was asked; `message` says what it did | `TASK_STATE_COMPLETED` |
| `TASK_STATE_REJECTED` | It did not and will not; `message` says why | `TASK_STATE_REJECTED` |

With `TASK_STATE_REJECTED` it may add `handback: true`: the request belongs to
another agent or must be split, and `message` says what should happen instead.
When its work is in a git repository it names `repository`, `branch`, the last
`commit` it pushed and the `pull_request` it opened. The platform records the
call with the response; when the response ends `completed`, the last report of
that response decides the task's state, and its `message` is the task's
`TaskStatus.message`, ahead of the conversation link. A response that ends
failed or cancelled keeps that state. A response without a report is a
completed task, as for a conversation without the tool.

The task's state and status message are A2A 1.0 core: any client reads a
declined request as `TASK_STATE_REJECTED` and why. The hand-back and the code
references have no core element; they travel in the task outcome extension
below, for a client that activates it.

The tool exists only in conversations an A2A context created, not in a console
conversation that `send_agent_message` reaches. Because a prepared Agent
runtime fixed its platform tools before any conversation claimed it, a
conversation an A2A context created starts its own runtime rather than claiming
a prepared one.

## Task outcome extension (v1) {#task-outcome-v1}

URI: `https://www.astrabox.ai/docs/a2a#task-outcome-v1`

A profile extension, in the sense of the
[A2A extensions guide](https://a2a-protocol.org/v1.0.0/topics/extensions/): it
adds data to core objects and changes no method. The Agent Card lists it in
`capabilities.extensions` with `required: false`. A client activates it per
request with the `A2A-Extensions` header, as in
`A2A-Extensions: https://www.astrabox.ai/docs/a2a#task-outcome-v1`; the
response's `A2A-Extensions` header then names it, and every task the response
carries has the data below. Without activation a task carries none of it.

The data sits in the core objects' `metadata`, under the extension's URI as
the key, and the object's `extensions` list names the URI:

- On a `TASK_STATE_REJECTED` task's `status.message`, when the Agent handed the
  request back: `{"handback": true}`. The request belongs to another agent or
  must be split; the status message says what should happen instead. A
  rejected task without it is a plain decline.
- On the task's result artifact (`artifactId` `<task id>:result`), when the
  Agent named them: `repository`, `branch`, `commit` (lowercase hexadecimal)
  and `pull_request` (an http(s) URL), each a string and each optional.

```json
{
  "status": {
    "state": "TASK_STATE_REJECTED",
    "message": {
      "role": "ROLE_AGENT",
      "parts": [{"text": "Agent declined the task.\n\nThis is a billing change; the billing agent owns it.\n\n[View Agent session](/sessions/…)"}],
      "extensions": ["https://www.astrabox.ai/docs/a2a#task-outcome-v1"],
      "metadata": {"https://www.astrabox.ai/docs/a2a#task-outcome-v1": {"handback": true}}
    }
  }
}
```

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

`CancelTask` stops the work a task's message started:

- A message no runtime has received yet is withdrawn. The task reads
  `TASK_STATE_CANCELED`, and the conversation shows the message as cancelled by
  its sender.
- A message a running response answers stops with that response, which ends
  `cancelled`.
- A message the runtime queued behind other work goes with the response in
  progress and everything else queued, because runtimes drop queued messages
  only all together. Codex and Pi can drop them. Claude Code, the DeepSeek
  Harness and Hermes run a queued message once the work ahead of it ends, so
  such a task cannot be canceled.

A cancellation may stop the caller's other tasks in the same `contextId`,
because one response answers several of its messages or they were queued
together. It never stops another sender's message, such as one the
conversation's owner typed in the console. When it would, the task is refused
with `TaskNotCancelableError`, which says why. An already-ended task is refused
the same way.

`CancelTask` returns once the runtime has stopped. If the conversation's
runtime is still being connected, it returns the task as it stands, and the
work stops before the runtime takes the message.

A pending platform approval requires the explicit interaction id and answer; a
new text message is not approval.

The server accepts text tasks, with `referenceTaskIds`, and exposes results and
platform interaction data.
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

## Personal schedules {#personal-agent-schedules}

The Assistant's built-in schedule tools manage `/api/v1/me/schedules`. These
reuse the platform scheduler, Deployment and Run records. The source Assistant
records where the plan was created. A plan with an `agent_id` is performed by
that Agent in a conversation of its own; a plan without one is performed by the
source Assistant itself, each run a message in its main conversation
([Routines and watches](assistants.md#routines-and-watches)).

A personal schedule always executes as its authenticated creator. Creating,
enabling or firing a plan checks target access: the creator's access to the
Agent, or ownership of the Assistant. Revoking access blocks new work while the
owner can still inspect, disable or remove the plan. Another user, including
the target Agent's owner, cannot manage that personal plan through the Agent's
deployment administration. A2A communication does not create a second
scheduler.

Open the account settings menu and choose **Personal schedules** to inspect
these plans. Select a plan in the dialog to edit its name, prompt, cron and
timezone, enable or disable it, run it now, replay a previous Run, or delete it.
Each Run with a Session links to its conversation. Create a plan by asking the
Assistant what should be done and when, and whether an Agent should do it.
