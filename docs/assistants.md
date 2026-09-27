# Assistants

An Assistant is a personal, long-lived cloud workspace owned by one user. Its
conversations share a workspace, and AstraBox saves the Agent program's native
state in the platform database. Keeping workspace files after the sandbox is
released or lost requires the deployment's optional persistent workspace volume.

Use an Assistant for work that grows over time, such as maintaining notes,
analyzing a changing set of files, or returning to the same project over several
days. Use an [Agent](authoring-agents.md) for a reusable cloud Agent that people
and other systems can reach through the console, API, Deployments, or remote MCP.

## Agent and Assistant

| | Agent | Assistant |
|---|---|---|
| Who can use it | Accounts that pass the Agent's authorization rules | Its owner |
| Typical work | Repeatable tasks, team use, and automation | One person's work that continues over time |
| Workspace | Each conversation has its own workspace | The owner's conversations use the same workspace |
| How work starts | Console, API, Deployment, or remote MCP | Console or API |

An Assistant does not merge its conversations. Each conversation has its own
Session record and history, while all of those Sessions work in the Assistant's
shared workspace.

## Create an Assistant

An Assistant uses an **Environment**: the saved runtime setup that selects the
Agent program, sandbox settings, model connection, and network access. The
Assistant form shows enabled Environments whose Agent program supports
Assistants.

1. Open **Management console → Assistants** and select **New assistant**.
2. Enter a name and select an **Environment**.
3. Optionally add a description and a [system prompt](#system-prompt), and
   choose the default [permission mode](permission-modes.md).
4. Select **Create**.

The signed-in user becomes the owner. The Environment also determines the Agent
program, and the console keeps both fixed after creation. The name, description,
icon, system prompt, and default permission mode remain editable.

## System prompt

The system prompt defines the Assistant's role, style, and rules. It is the
same setting an [Agent](authoring-agents.md) has, and the Agent program applies
it through its own mechanism.

Hermes, the bundled Assistant program, uses it as its `SOUL.md`: the identity at
the start of its system prompt. The system prompt replaces Hermes' built-in
"You are Hermes Agent" identity; the rest of Hermes' own prompt, such as its
tool guidance, stays. Hermes checks this text the way it checks any
`SOUL.md`: it withholds content that matches its prompt-injection patterns and
truncates content longer than its context-file limit, which is at least 20,000
characters.

Leave the field empty to keep Hermes' own default identity. Clearing a system
prompt that was set restores that default. The exception is a `SOUL.md` that
changed after AstraBox wrote it, such as an edit made through Hermes; that
version is kept. While a system prompt is set, AstraBox writes it again
whenever Hermes starts or the Assistant's settings change, replacing such
edits.

## When a change takes effect

Saving a change never ends a reply in progress. When the change reaches a
conversation depends on when Hermes reads the setting:

- **System prompt.** Hermes builds a conversation's prompt when the
  conversation starts. AstraBox writes the new prompt into Hermes' profile:
  conversations started after the change use it, and a conversation that is
  already open keeps the prompt it started with.
- **Model.** Hermes checks its configured model at the start of every reply.
  AstraBox writes the new model into Hermes' profile: new conversations use it,
  and a conversation that is already open switches to it from its next
  message. A reply in progress finishes on the model it started with.
- **MCP servers, and model credentials that the sandbox holds itself.** Hermes
  reads these only when its program starts, so AstraBox restarts it. A restart
  would end every reply in progress in the Assistant's other conversations, so
  AstraBox first waits until none is running, including replies waiting for an
  approval or an answer and background delegations. The new conversation shows
  as preparing until then. If work is still running after 30 minutes, the new
  conversation fails with an error that names it, and nothing is restarted.
  A new MCP server's host is also added to what the workspace can reach.

After a restart, the Assistant's other conversations continue with their
history on their next message. If Hermes restarts for any other reason while
a reply is in progress, that reply ends with an error saying the backend lost
it, and the conversation accepts the next message.

A conversation that cannot be prepared ends and shows why, for example when
the Assistant names an MCP server but its Environment does not allow MCP
servers. Fix the cause, then start a new conversation.

## Start and continue work

Open **Assistants** in the user console, choose an Assistant, and select
**Start conversation**. AstraBox prepares or restores the workspace when needed
and opens a new Session. Starting another conversation later creates another
Session attached to the same workspace.

The Agent program defines its native state, which AstraBox saves in the
platform database for restoration. Each conversation also has its own Session
history. Ending one conversation does not end the Assistant or clear the shared
workspace; file retention across sandbox replacement depends on persistent
workspace storage.

Messages, approvals, questions, files, and share links use the common Session
interface described in [Sessions](sessions.md).

## Pause and resume the workspace

Select **Pause workspace** on the Assistant's management page when it does not
need active compute. AstraBox stops native state writers, confirms that the
Agent program's state is saved in the platform database, then releases the
sandbox. This operation works without a persistent workspace volume. To keep
workspace files across it, configure that volume or export the files first.

Select **Resume workspace**, or simply start another conversation, to restore
the saved native state into a new sandbox. With persistent workspace storage,
the same Assistant files are mounted there; without it, the replacement has a
fresh filesystem. This Assistant operation releases and recreates compute; it
does not use OpenSandbox's sandbox pause/resume operation.

The management page reports whether the workspace is not started, starting,
ready, paused, or needs attention. The pause operation reports success only
after AstraBox confirms both the save and the sandbox release. If either step
cannot be confirmed, the Assistant remains available for a retry.

## Authorization and credentials

Only the owner can list, open, update, pause, resume, or delete an Assistant.
Requests from other accounts receive the same not-found response as requests for
an unknown Assistant.

Credential delivery is separate from ownership. Assigned MCP credentials
require sandbox credential protection. The sandbox provider attaches the
credential only to matching outbound requests, keeping its stored value outside
the Assistant's sandbox. An authenticated managed MCP binding is rejected when
that protection is disabled.

Administrators can assign a Credential Vault to an Assistant from
**Management console → Credentials**. The Assistant can then use the assigned
MCP credentials without exposing their stored values to the user or Agent
program. See [Credentials](credentials.md) for supported credential types and
assignment rules.

## Delete an Assistant

Export files that need to be kept elsewhere before deleting the Assistant from
its management page. AstraBox removes the Assistant record only after it
confirms that the current sandbox has been destroyed. If destruction cannot be
confirmed, the record remains available so deletion can be retried.

## API access

Assistant creation, conversations, workspace lifecycle, and deletion are also
available through the HTTP API. The system prompt is the Assistant's `system`
field, as it is an Agent's. Each AstraBox instance publishes the exact
Assistant endpoints in the interactive API reference at `/docs` and the OpenAPI
document at `/openapi.json`. See [API overview](api.md) for authentication and
request conventions.

## Related guides

- [Environments](environments.md)
- [Sessions](sessions.md)
- [Credentials](credentials.md)
- [Connect model services](models.md)
