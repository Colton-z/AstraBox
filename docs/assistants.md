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
3. Optionally add a description and choose the default
   [permission mode](permission-modes.md).
4. Select **Create**.

The signed-in user becomes the owner. The Environment also determines the Agent
program, and the console keeps both fixed after creation. The name, description,
icon, and default permission mode remain editable.

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
available through the HTTP API. Each AstraBox instance publishes the exact
Assistant endpoints in the interactive API reference at `/docs` and the OpenAPI
document at `/openapi.json`. See [API overview](api.md) for authentication and
request conventions.

## Related guides

- [Environments](environments.md)
- [Sessions](sessions.md)
- [Credentials](credentials.md)
- [Connect model services](models.md)
