# Agent-to-Agent communication

An Assistant uses Hermes' bundled A2A tools to discover and call other Agents.
AstraBox supplies the Assistant owner's accessible Agents as peers, with
credentials scoped to its workspace and each target. The target executes in a
normal AstraBox Session, owned by the caller, through its configured engine.

Hermes is pinned to 0.21.5, commit
`f97608f178d1ffeca59860195ab7da295f7c8e5f`. Its
[A2A plugin](https://github.com/NousResearch/hermes-agent/blob/f97608f178d1ffeca59860195ab7da295f7c8e5f/website/docs/user-guide/messaging/a2a.md)
provides `a2a_discover`, `a2a_call`, `a2a_list`, `a2a_history` and
`a2a_orchestrate`. AstraBox does not start Hermes' inbound messaging gateway
to execute managed Agents: that gateway owns a different execution lifecycle.

## Calling a managed Agent

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
context continue it. A Task identifies a logical input, including when the
existing input queue carries it into a later engine turn. The Session journal
remains the execution authority. Task metadata includes the platform Session
id, and text results include its console link.

Repeating an identical `messageId` reuses its input. Reusing it with different
message content is refused. Hermes generates its own message ids, so asking
the Assistant to call again is a new request, not an idempotent retry.

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
use the same public projection. No Hermes plugin replacement or separate Agent
execution path is needed: the MCP bridge supplies the structured operation the
pinned text-only tool cannot encode.

Task-scoped cancellation is refused: the current
platform interrupt targets a physical turn, which can carry multiple queued
inputs, and cannot atomically fence cancellation to one A2A Task. A pending platform approval requires
the explicit interaction id and answer; a new text message is not approval.

The server accepts text tasks and exposes results and platform interaction data.
It does not accept uploaded file parts or push-notification configurations.
`SendMessage`, `SendStreamingMessage`, `GetTask`, `ListTasks` and
`SubscribeToTask` use the same Session journal. A nonblocking new task is
admitted before its runtime is ready; the existing Session worker owns delivery.
An extra input sent while that Session is still being created is refused rather
than creating a second delivery worker.

Streaming clients may disconnect without cancelling accepted execution. A
subscription starts with the current Task, then emits ordered journal updates;
concurrent subscribers covering the same events receive the same order.
Binary tool approvals use `AUTH_REQUIRED`; forms and decisions with
adapter-declared options use `INPUT_REQUIRED`, including Codex's approval choices.
Both keep the stream open. A structured answer resumes the original Task;
the stream closes when that task reaches a terminal outcome. Subscribing to an
already terminal Task or sending it a new answer is refused. Use `GetTask` to
read its result.

`historyLength` limits the returned Task history. `acceptedOutputModes` can
select text, JSON, or both; a single format converts the other result parts into
that format, retaining their content. Unrestricted Task reads retain the native
text and public transcript blocks, using the same public projection as the
Session API. Runtime locators and raw diagnostic envelopes remain server-side.
The protocol definition is the
[A2A 1.0 specification](https://a2a-protocol.org/v1.0.0/specification/);
the SDK handles its transport, while AstraBox projects its task semantics from
the platform journal.
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
