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
| How work starts | Console, API, Deployment, or remote MCP | Console, API, or a Deployment its owner manages |

An Assistant does not merge its conversations. Each conversation has its own
Session record and history, while all of those Sessions work in the Assistant's
shared workspace.

## Your Assistant

Every user has a **primary Assistant** without setting one up. The first time a
user opens the console, AstraBox creates it from the deployment's
[Assistant defaults](#defaults), with the browser's [timezone](#timezone), and
lists it at the top of the left rail. Creating it writes only the record: the
workspace is prepared when its first conversation starts. A user whose
Assistants predate this has their earliest one marked primary instead.

The primary Assistant needs an **RuntimeProfile**: the saved runtime setup that
selects the Agent program, sandbox settings, model connection, and network
access. AstraBox uses the one the Assistant defaults name or, when they name
none, the only enabled RuntimeProfile whose Agent program supports Assistants.
With several such RuntimeProfiles and no choice in the defaults, or with none,
users get no Assistant and the rail shows no Assistants section; an
administrator sets the RuntimeProfile in the Assistant defaults.

Creating it does not require permission to author Agents and Assistants, so a
deployment that reserves authoring to administrators still gives every user
one. Apps the user already connected are turned on for it, as for any new
Assistant.

### More Assistants

1. Select **+** beside **Assistants** in the rail, or open
   **Management console → Assistants** and select **New assistant**.
2. Enter a name and select an **RuntimeProfile**.
3. Optionally add a description and a [system prompt](#system-prompt), and
   choose the default [permission mode](permission-modes.md). The
   [timezone](#timezone) starts as the browser's.
4. Select **Create**.

The signed-in user becomes the owner. The RuntimeProfile also determines the Agent
program, and the console keeps both fixed after creation. The name, description,
icon, timezone, system prompt, and default permission mode remain editable.

## Assistant defaults {#defaults}

What an Assistant can do is decided by whoever runs the deployment and by its
owner. Administrators set what every Assistant inherits in
**Management console → Assistant defaults** (or `/api/v1/admin/assistant-defaults`
and its `/extensions`):

- the **RuntimeProfile** new primary Assistants are created in, or automatic;
- the **model** Assistants use unless their own setting names one;
- **organization instructions**, given to every Assistant before its owner's
  own [system prompt](#system-prompt);
- **Skills, MCP servers, packages, and Agent-program configuration** every
  Assistant gets, written directly or, when the deployment runs one, selected
  from the [extension catalog](adding-tools.md#extension-catalog);
- **Credential Vaults** bound to every Assistant. A Vault the defaults bind
  is listed on its Credentials page and cannot be archived or deleted until
  the defaults release it.

They are checked the way an Assistant's own fields are, against what the
chosen RuntimeProfile's Agent program reads and may reach, so the extension
fields wait until a RuntimeProfile is chosen. Assistants read the defaults
together with their own settings, so a change takes effect as described in
[When a change takes effect](#when-a-change-takes-effect).

How they combine with an Assistant's own settings: instructions come first,
then the owner's; Skills, packages, and Vaults add up; MCP servers add up, and
an Assistant may not name one of its own like an inherited one; an
Agent-program configuration block the Assistant sets replaces the inherited
block of the same name. A RuntimeProfile the defaults name cannot be deleted
until the defaults point elsewhere.

The owner sees what is inherited in the Assistant's profile and on its first
conversation, but edits only their own settings.

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

## Timezone

An Assistant keeps time in its owner's timezone, an IANA name such as
`Europe/Berlin` (`timezone` in the API). Hermes names it in every
conversation's system prompt and reads the schedules of the jobs it creates in
it, so "every weekday at 8" runs at 8 in the owner's morning. Without one,
Hermes uses the sandbox's clock, which is UTC.

## Skills, plugins and Hermes configuration

An Assistant can add to its Agent program what that program accepts. The
Assistant's record page shows a **Skills, plugins and configuration** card with
the fields the RuntimeProfile's program declares; the API takes the same fields as
an [Agent](authoring-agents.md): `skills`, `mcp_servers`, `packages` and
`engine_options`. When the deployment runs an
[extension catalog](adding-tools.md#extension-catalog), the same card also
picks MCP servers and Skills from it, as an Agent's page does
(`GET`/`PUT /api/v1/assistants/{assistant_id}/extensions`); a catalog MCP
server whose upstream needs a credential reaches it through the Credential
Vault assigned to the Assistant. Without a catalog the card holds only the
Assistant's own fields. For Hermes:

- **Skills.** Git sources of `SKILL.md` Skills, written as
  `repository@ref#path`. AstraBox places them in the profile's
  `skills/astrabox` category, where Hermes lists them and loads them with
  `skill_view`. A Skill's name must not match another Skill in the profile:
  Hermes refuses to load a name it finds twice.
- **Packages.** Each entry names its `kind` and a Git source (`url`,
  `protocol`, a `branch` or a pinned `sha`, and the `paths` of the package
  directories in the repository). Hermes loads two kinds:
  - `hermes-plugin`: a directory with `plugin.yaml`, which can add tools,
    hooks, slash commands, Skills and system prompt sections. Its declared
    Python dependencies are installed from PyPI when it is placed; a
    dependency that cannot be installed stops the conversation's preparation
    with the plugin's name. With a Limited network, PyPI (`pypi.org`,
    `files.pythonhosted.org`) is admitted for an Assistant with a Hermes
    plugin, as a package's Git origin is.
  - `agent-plugin`: an Agent Plugins v1.0 package, a `plugin.json` with Skills
    and an `mcp.json`.

  A package directory becomes the plugin `astrabox/<directory>`, named after
  its path or, for a package at the repository root, the repository. AstraBox
  enables exactly the plugins it placed and removes them when they leave the
  configuration; plugins and Skills the Assistant installed itself stay.
- **Hermes configuration** (`engine_options.config`). Hermes `config.yaml` keys,
  such as `timezone` or `agent.max_turns`. AstraBox writes them to Hermes'
  managed scope, which wins over the profile's own `config.yaml` key by key and
  which the Assistant cannot change. The keys AstraBox sets itself are refused:
  `model`, `providers`, `custom_providers`, `mcp_servers`, `terminal`,
  `timezone` (set it as the Assistant's [timezone](#timezone)),
  `plugins.enabled` and `plugins.disabled`. `skills` is refused too, because
  Hermes does not read Skills settings from the managed scope.

You can ask your Assistant to create or edit its own Skills using Hermes'
native `skill_manage` tool, including customizing the defaults AstraBox ships.
These live in the Assistant's private profile, not its task workspace. Hermes
updates unmodified defaults and keeps your edits; the platform saves the Skill
files and Hermes' sync records in the database for restoration into a new box.
Skills supplied through the Assistant's `skills` configuration remain managed
by those sources: update the source to change what the configuration supplies.

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
- **Skills.** Hermes lists a conversation's Skills when the conversation
  starts: conversations started after the change have the new set.
- **Timezone, MCP servers, packages, Hermes configuration, and model
  credentials that the sandbox holds itself.** Hermes reads these only when its program starts,
  so AstraBox restarts it. A restart
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
the Assistant names an MCP server but its RuntimeProfile does not allow MCP
servers. Fix the cause, then start a new conversation.

## Start and continue work

Select the Assistant in the rail. It opens the Assistant's **main
conversation**, the same one every time, starting it when the Assistant has
none; AstraBox prepares or restores the workspace when needed. Scheduled jobs
created there report back there, and the Assistant's row in the rail is
marked unread when a reply arrives while the owner is elsewhere. The main
conversation is that row, so it is not listed again under Sessions.

Before the first message, the conversation lists what the Assistant can use:
the apps connected for it, its Skills and tools, its own and inherited, and the
apps the owner could connect. **Assistant** in the conversation header opens
its profile: name, timezone, the owner's instructions, connected apps switched
on or off for it, its scheduled jobs, what it inherits, and deletion. The
scheduled jobs are read from the Agent program's own job store while the
workspace runs (`GET /api/v1/assistants/{assistant_id}/scheduled-jobs`);
while it does not, the profile says so, and the jobs run once it is back.

Ending the main conversation is how an owner starts afresh: the next time the
Assistant is opened, a new main conversation starts. The API can still start
further conversations with `POST /api/v1/assistants/{assistant_id}/conversations`;
each is another Session attached to the same workspace.

The Agent program defines its native state, which AstraBox saves in the
platform database for restoration. Each conversation also has its own Session
history. Ending one conversation does not end the Assistant or clear the shared
workspace; file retention across sandbox replacement depends on persistent
workspace storage.

Messages, approvals, questions, files, and share links use the common Session
interface described in [Sessions](sessions.md).

## Reach it from messaging apps and on a schedule {#triggers}

An Assistant can have its own triggers, the same Deployments an Agent has:
a messaging channel such as Telegram or Slack, a schedule, or a webhook.
Open **Management console → Deployments**, select **New trigger**, and pick
the Assistant under **Agent or Assistant**. Only the Assistant's owner can
create or change its triggers, and every conversation they start runs as the
owner.

A channel bot accepts direct messages from anyone who finds it, so a channel
trigger on an Assistant must list the owner's own account under **Allowed
senders**, one per line, using the selected adapter's exact sender format and
example. For Satori's Telegram adapter, an example is `telegram:123456789`;
other adapters may use an unprefixed identity. See
[Allowed senders](channels.md#allowed-senders). Messages from any other account
are ignored and never reach the Assistant or its later context.

Each channel conversation is one Assistant conversation in the shared
workspace. Replies go back to the chat, and so does anything the Assistant
produces on its own afterwards in that conversation: a scheduled job's
result, a finished background task, or a reply Hermes starts itself. A
schedule starts a new Assistant conversation at each due time; see
[Schedules](schedules.md).

## Scheduled jobs it creates itself

Ask the Assistant to do something on a schedule, such as "every weekday at 8,
summarize my unread email", and Hermes creates a scheduled job in its own
scheduler. The job runs in the Assistant's workspace even when no
conversation is open, and AstraBox reports each run back into the
conversation that created the job: a channel chat receives it right away, and
a console conversation shows it unread in the list, with a browser
notification when notifications are on. A run that answers `[SILENT]`, which a
monitor does when nothing meets its bar, is kept but not delivered. The job is
kept in the platform database with the rest of Hermes' profile state, so it
survives replacing the workspace's sandbox.

Every Assistant also has AstraBox's `astrabox-proactive` Skill, shipped in the
Hermes image. It teaches the Assistant to offer such jobs when it notices
recurring work (a morning briefing, an important-mail monitor, meeting prep, a
weekly review), to create one only after the user agrees, to write each job so
it runs on its own with the user's connected apps, and to keep monitors quiet
until something is worth reporting.

## Connect work apps {#connectors}

The **Connectors** card on the Assistant's management page connects work apps:
Gmail, Google Calendar, Google Drive, Notion, Linear, Jira and Confluence,
GitHub, and Slack.

Select **Connect** and sign in at the vendor. The browser returns to the
**Connections** page, where each person's connections are listed, and the app
is turned on in each of the person's Assistants. A new Assistant starts with
the apps its owner has already connected. Turning an app off on one Assistant
keeps it off there when the owner connects it again; disconnecting an app turns
nothing off, so the agent can tell the person it needs connecting again.

Connections are held by the deployment's connector hub,
[ContextForge](https://github.com/IBM/mcp-context-forge), which runs beside
AstraBox. The hub stores each person's vendor token, refreshes it, and calls
the vendor with it. An agent reaches a connector only through the AstraBox
server, which relays each call to the hub as the person the conversation acts
for: an Assistant's owner, or whoever started the Agent conversation. No token
and not the hub's address enters a sandbox, so a connector needs no MCP network
access in the RuntimeProfile. A connection belongs to the account that made it;
each Assistant and Agent uses only the connectors turned on for it. When an
agent needs an app the person has not connected, it can show them a card to
connect it in the conversation. Turning a connector on or off restarts the
Assistant's Hermes backend once no reply is running, as for any MCP change.

**Disconnect** removes the token from the hub. It does not withdraw the access
at the vendor; to do that, remove the app in the vendor's account settings as
well. When the vendor stops accepting the connection, the card shows **Sign in
again**.

Notion, Linear, Jira and Confluence, GitHub, and Slack are served by the
vendor's own MCP server. Google runs its Gmail, Calendar and Drive MCP servers
only as a developer preview whose terms forbid offering them to users outside
your own organization, so these three are served by the deployment's own
[google_workspace_mcp](https://github.com/taylorwilsdon/google_workspace_mcp)
instances, which call Google's generally available APIs with each person's
token.

### Set up a connector

An administrator sets each app up once under **Management console →
Connectors**:

1. Where the vendor issues OAuth clients only to registered apps (Google,
   GitHub and Slack), register an OAuth app with the vendor using the redirect
   URI shown on the page (the hub's address followed by `/oauth/callback`),
   and enter its client ID and client secret. Notion, Linear and Atlassian
   register the hub automatically.
2. Select **Set up**.
3. Select **Connect your account** and sign in at the vendor. The hub can list
   an app's tools only with a signed-in account.
4. Select **Publish tools**. The app is then available to every account.

Saving an app's OAuth settings again removes its published tools, so publish
them again afterwards.

AstraBox asks Google for the access a ChatGPT app for the same product has:
reading, drafting and sending mail without permanent deletion for Gmail,
reading calendars and changing events for Calendar, and managing files for
Drive. It also asks for the account's email address, which google_workspace_mcp
uses to check each token with Google. Enable the Gmail API, Google Calendar
API and Google Drive API in the Google Cloud project that owns the OAuth
client: consent works without them, but every call to an app whose API is off
fails with "API is not enabled for your project".

Google treats Gmail and Drive access as restricted: an app used by accounts
outside your own Google Workspace organization needs Google's verification,
which includes a yearly third-party security assessment, before people other
than its test users can connect.

## Long conversations

When a conversation grows long, Hermes summarizes its older part so it fits the
model's context window. AstraBox asks Hermes to do this while the conversation
is quiet, shortly after a reply, once the context is 40% full, so the summary
is ready before a reply would need it. A message sent while the summary is
being made is answered at once, on the full conversation, and the summary is
made again at the next quiet moment. Hermes decides the model's context window
itself, from what the model endpoint reports or, when it reports nothing, from
the model's name.

## Pause and resume the workspace

Select **Pause workspace** on the Assistant's management page when it does not
need active compute. AstraBox stops native state writers, confirms that the
Agent program's state is saved in the platform database, then releases the
sandbox. This operation works without a persistent workspace volume. To keep
workspace files across it, configure that volume or export the files first.

Select **Resume workspace**, or simply start another conversation, to restore
the saved native state into a new sandbox. The saved state is Hermes'
conversation store and its profile: its memory, Skills (including edited
defaults and their native sync records), and its scheduled jobs. Hermes seeds
new defaults and updates unchanged ones without replacing user edits. Hermes
configuration is written again from the Assistant's settings.
With persistent workspace storage, the same Assistant files are mounted there;
without it, other files start fresh. This Assistant operation releases and
recreates compute; it does not use OpenSandbox's sandbox pause/resume
operation.

A workspace whose sandbox disappears without being paused, for example when
its node fails, is replaced within a few minutes without anyone opening the
Assistant: AstraBox restores the saved native state into a new sandbox, so its
conversations and channels answer again. A reply that was running when the
sandbox was lost ends with an error and is not retried. The replacement holds
what a resume restores: Hermes' saved state, and workspace files only with
persistent workspace storage. Changes Hermes made in the last few seconds
before the loss can be missing.

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
its profile or its management page. AstraBox removes the Assistant record only
after it confirms that the current sandbox has been destroyed. If destruction
cannot be confirmed, the record remains available so deletion can be retried.
Deleting the primary Assistant gives its owner a new one from the Assistant
defaults the next time they open the console.

## API access

Assistant creation, conversations, workspace lifecycle, and deletion are also
available through the HTTP API. The system prompt is the Assistant's `system`
field, as it is an Agent's. Each AstraBox instance publishes the exact
Assistant endpoints in the interactive API reference at `/docs` and the OpenAPI
document at `/openapi.json`. See [API overview](api.md) for authentication and
request conventions.

## Related guides

- [RuntimeProfiles](runtime-profiles.md)
- [Sessions](sessions.md)
- [Messaging channels](channels.md)
- [Schedules](schedules.md)
- [Credentials](credentials.md)
- [Connect model services](models.md)

## Runtime protocol compatibility

Hermes images advertise their executor completion classification through the
native `gateway.capabilities` build probe. A core requiring that classification
refuses an image that does not advertise it before opening a native conversation
or submitting a prompt. Deploy the core and its corresponding sandbox image
together; an application version alone does not identify an overlay image's protocol.

An unreadable execution event stops that Session's reader with
`runtime_protocol_incompatible`. The page shows recovery required and the
specific protocol error, and refuses new input. Reader sweeps do not retry it.
The readable prefix, native response and input facts, and last readable cursor
remain intact. The platform does not mark a live box unavailable, guess a tool's
outcome, skip the event, or replay an admitted task.

Correct the runtime protocol before explicitly recovering the Session. If the
same native epoch retains the event ring, recovery reads from the stored cursor.
Replacing the native process or box does not retain that ring; this mechanism
alone does not recover previously unread completed tool events from SessionDB.

A conversation blocked by a runtime protocol fault can still be ended or
archived. These operations close its recorded native conversation on the existing
workspace; they do not require execution protocol compatibility or release the
shared sandbox. A native cleanup failure returns an error and leaves the
conversation open for a retry. Ordinary working conversations remain protected
from end or archive.
