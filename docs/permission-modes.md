# Permission Modes

> Control how the selected Agent program handles tool calls and approval.

Permission modes control what the Agent can access and whether it **needs
human approval** before performing a tool action, letting you balance Agent
autonomy with human oversight.

Installed Agent programs do not use one common permission model. AstraBox keeps
each program's own mode names and behavior instead of translating them into a
platform-owned `allow / ask / deny` policy.

## Runtime behavior

Open an Agent conversation and use the permission selector below the message
box. The selector appears only when the Agent program supports changing modes.

The selector lists only the modes declared by the Agent program installed in
the Agent's Environment. Their names and descriptions come from that program.
The program also declares the default when no mode is selected. AstraBox does
not parse a command to override that decision or reinterpret the program's
approval request.
Some programs also expose a separate approval setting in the Agent's
**Advanced** section; it remains separate from the permission selector.

A permission mode does not override the Environment's network policy or the
sandbox's isolation settings.

## Configure a mode for a Session

Open the Agent, start or resume a conversation, and choose a permission mode
below the message box. You can change it while the Agent is idle; your next
message runs under the new mode without editing the Agent or creating another
conversation.

If the selected Agent program exposes an additional approval setting, configure
it while creating or editing the Agent under **Advanced**. The console keeps
that setting distinct from the conversation's permission mode.

## Pending action flow

When the Agent needs approval before an action:

1. The Agent emits an approval request.
2. The Agent's response pauses and waits for human input.
3. The web console shows the action, its arguments, and the available choices.
4. The user approves or rejects the action.
5. The Agent continues the same response with that decision.

Closing the browser does not approve or reject an action. Reopen the same
conversation to continue while the request is still pending.

## Confirm or reject an action

The web console handles approval requests directly. An API client can answer
the same request with:

```text
POST /api/v1/sessions/{session_id}/interactions/{interaction_id}/answer
```

Use the `interaction_id` of the pending request.
`GET /api/v1/sessions/{session_id}` returns the request in
`pending_interaction`, and the Session stream carries it in a
`data-interaction` part.

The request's `presentation` decides the shape of `answer`:

| `presentation` | `answer` |
|---|---|
| `tool_approval` | `{"decision": "approve"}` or `{"decision": "reject"}` |
| `decision` | `{"decision": "<id>"}`, where `<id>` is the `id` of one of the request's `options` |
| `form` | `{"answers": [...]}` with one entry for each of the request's `questions`, or `{"decline": true}` |

Each `answers` entry names its `question_id` and carries `option_label`,
`option_labels` for a multi-select question, or `free_text` where the question
allows it.

Request body to approve a `tool_approval` request:

```json
{
  "answer": {
    "decision": "approve"
  }
}
```

Or to reject it:

```json
{
  "answer": {
    "decision": "reject",
    "comment": "This operation is outside the scope of the current task."
  }
}
```

When you reject, include a `comment` when the Agent can use it to adjust its
plan and try again.

## FAQ

**Q: Can one response require multiple answers?**

A: Yes, but AstraBox exposes one active interaction at a time. After it is
resolved, the Agent program can request another answer in the same response.

**Q: Do pending actions time out?**

A: The browser does not set the timeout. The Agent program may end its own
wait, and a lost runtime also ends the request. If that happens, answering the
old request returns `409 INTERACTION_NOT_OPEN`; send the next message to
continue the conversation.

**Q: Can a client-side custom tool use a permission mode?**

A: AstraBox does not define a platform client-side custom-tool interface. If the
selected Agent program needs client input, AstraBox carries its engine-native
interaction and returns the answer through the interaction's `answer` endpoint;
the program defines how that answer affects its tool execution.

## Next steps

- [Agent tools and extensions](adding-tools.md) — Configure the capabilities
  available to an Agent.
- [SSE Event Stream](events-stream.md) — Receive output and pending
  interactions.
- [Sessions](sessions.md) — Start and continue Agent work.
- [Defining an Agent](authoring-agents.md) — Review Agent configuration.
