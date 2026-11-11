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
lists it at the top of the left rail. Creation also attempts to prepare its
workspace so the first conversation can attach to it. If that preparation
fails, the saved Assistant remains and its first conversation attempts
preparation again. A user whose
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

Saving a changed definition attempts to refresh a ready workspace in place.
If that refresh fails, the next conversation prepares the current revision.
An existing ready conversation checks the revision before using the profile.
Creating or editing a record alone is not proof that its workspace is ready.

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
    Python dependencies are prepared by Hermes' package manager together with
    Hermes' own dependency graph; installation failure stops preparation.
    With a Limited network, PyPI (`pypi.org`, `files.pythonhosted.org`) and
    Hermes' pinned GitHub dependencies (`github.com`,
    `release-assets.githubusercontent.com`) are admitted within the same
    author boundary as a package's Git origin.
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

## Assistants that work from a panel: dsh-superagent {#panel-runtimes}

Some Agent programs do not hold conversations with their owner.
[dsh-superagent](https://github.com/deepseek-ai/dsh-superagent-prototype)
runs on DeepSeek Harness, takes requests from its Sources (a GitHub
repository, or a ticket or to-do service your own Source plugin reads), turns
each into a Ticket, and has one of its owner's Agents work it. People talk to it where
they already work; the owner supervises it in the bundle's own panel. An Assistant on a
RuntimeProfile with the `dsh_superagent` engine therefore has no
conversations: opening it shows the panel, on its Superagent page, inside the
console, and the Assistant keeps running between visits so it can answer
its Sources.

The deployment needs:

- **OpenSandbox with Secure Access and wildcard routes** (`gatewayRouteMode:
  wildcard` on the lifecycle server). The console opens the panel through a
  short-lived signed address of the form
  `<sandbox>-<port>-<expiry>-<signature>.<gateway domain>`; a header-routed
  gateway cannot be opened by a browser and the Assistant shows why. The
  address lives `ASTRABOX_SANDBOX_ENDPOINT_URL_TTL_SECONDS` (15 minutes by
  default); the console fetches the next one before it expires, which reloads
  the panel.
- **The gateway domain on the console's site.** The harness keeps its login in
  a `SameSite=Strict` cookie, which a browser sends inside the console's frame
  only when both share a registrable domain and a scheme: for example a
  console at `https://astrabox.example.com` with a gateway at
  `*.sbx.example.com`, both HTTPS. A wildcard certificate for the gateway
  domain has to be issued with a DNS challenge.
- **A GitHub account for the agent, to work from GitHub.** Set the deployment secret
  `astrabox-dsh-superagent-github-token` (through the environment that is
  `ASTRABOX_DSH_SUPERAGENT_GITHUB_TOKEN`) to a token of one bot account, and
  every dsh-superagent Assistant uses it. An owner who wants the work done
  under another account sets **GitHub agent account token** on the
  Assistant's settings page; it is stored encrypted and replaces the shared one for that Assistant
  only. Without either, the agent has no GitHub account: the GitHub Source
  stays unset and its other Sources run. The account must receive mentions on GitHub itself: in its
  notification settings, **Participating, @mentions and custom** must include
  **On GitHub**. The token is a classic personal access token with the `repo`
  and `notifications` scopes.

To put an Assistant to work, invite the bot account to the repository, then
in the panel's **Settings** set the GitHub Source's repository, the accounts
whose requests it may act on, and the command that prepares a Ticket's
checkout, for example:

```bash
git clone --quiet "https://github.com/$DSH_SUPERAGENT_GITHUB_REPOSITORY.git" "$DSH_SUPERAGENT_DIR" \
  && git -C "$DSH_SUPERAGENT_DIR" checkout -q -b "agent/$DSH_SUPERAGENT_NAME"
```

A mention of the bot in an issue from an allowed account then becomes a
Ticket; the bot answers on the issue, and a code change arrives as a pull
request for a person to merge. The bundle's own model calls use the
Assistant's model through the deployment's model gateway.

The work itself is done by one of the owner's Agents. For each Ticket, the
bundle's main Session picks the Agent whose name and description fit the
request, and the Ticket becomes a conversation of that Agent, started over
[A2A](a2a.md) as the Assistant's owner. The owner sees it in the console like
any other conversation, and **Open conversation** on the Ticket's page opens
it; the request's follow-ups, such as new comments on the issue, go to the
same conversation. The Agent works in its own sandbox with its own program and
model, and the prepare command above runs in its workspace, so the panel's
execution settings leave out the bundle's own worker model, execution
directory and blocked commands. When the Agent's work on a message completes,
its reply is the Ticket's result, which the Assistant answers the request with
on its thread; a pull request the reply names from the Ticket's branch becomes
the Ticket's to follow. A question the Agent asks, or an approval it waits
for, shows on the Ticket and is answered in the Agent's conversation. Stopping
a Ticket cancels its conversation's work ([cancellation](a2a.md)). The Agents
the Assistant can choose are the ones its owner can use when its runtime
starts; an Agent added later is offered after the next start.

Give the Agents that do code work access to GitHub as the bot. The bundle
follows only pull requests the bot opened from a Ticket's branch, so an Agent
that works as another account opens pull requests its Ticket does not follow.
An administrator puts the bot's token in a Vault twice, and assigns the Vault
to those Agents
([Authenticate with Vaults](credentials.md#2-add-a-credential)):

- as an **HTTP Basic (Git HTTPS)** credential for the repository's HTTPS URL,
  for example `https://github.com/your-org/your-repo.git`, with the bot's
  login as the username, which `git push` uses; and
- as an **Environment variable** credential named `GH_TOKEN` for
  `api.github.com`, which `gh pr create` uses.

The Agents' sandbox image needs `git` and `gh`; the Claude Code image has both.

The bundle keeps its Tickets, threads and decisions in its own database. The
platform saves that database, with the harness's Session logs and settings,
in the platform database as the box works, and restores them into a new box
after the box is released or lost; Ticket checkouts are prepared again.

Limits:

- The bot reads the notifications of the repository each Assistant names and
  never marks them read, so several Assistants can share one bot as long as
  each watches its own repository. Nothing stops two Assistants from naming
  the same repository, and an Assistant can be pointed at any repository the
  bot was invited to.
- The Skills, MCP servers, packages and Agent-program configuration in the
  [Assistant defaults](#defaults) are written for the program of the
  RuntimeProfile the defaults choose. An Assistant on another program does
  not inherit them. The defaults' model still applies; dsh-superagent has no
  place for the organization instructions.
- Feishu, the bundle's second Source, is not configured by the platform.
- A process holds the environment it started with: an environment credential
  added to an assigned Vault reaches the bundle when the Assistant's runtime
  next starts, while a rotated or archived one applies to the running one
  ([Protect credentials](egress-credential-injection.md#add-an-outbound-credential)).

### Your own Sources {#own-sources}

A Source is a dsh-superagent plugin that reads a service's events and replies
on it ([the bundle's cookbook](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/docs/008-cookbook.md#add-a-source)).
AstraBox's image patches the bundle so that GitHub is optional and a Source can
live in its own package, importing the bundle's Source interface from
`dsh-superagent/sources` (`containers/sandbox-dsh-superagent/upstream-changes.patch`,
offered upstream). An organization adds its ticket or to-do service by building
its own image on AstraBox's:

```dockerfile
FROM ghcr.io/colton-z/astrabox-sandbox-dsh-superagent:<version>
# The plugin package, with its own dependencies; `dsh-superagent` resolves to the installed bundle.
COPY --chmod=u=rwX,go=rX my-tickets-source /opt/astrabox/dsh-plugins/my-tickets-source
# Its loader layer: inserted after AstraBox's, in name order.
COPY --chmod=0444 50-my-tickets.yml /opt/astrabox/superagent/patches.d/50-my-tickets.yml
# Boot the composition once and require the Source kind to be installed.
RUN /usr/local/bin/astrabox-dsh-superagent-boot-check --source my-tickets
```

with a loader layer such as:

```yaml
- insert:
    - id: my-tickets-source
      name: file:///opt/astrabox/dsh-plugins/my-tickets-source/lib/index.js
```

The check boots the harness as an unprivileged account, as a box runs it under
the Assistant's own account, so a plugin file that account cannot read fails the
build instead of leaving the Source silently uninstalled; keep the plugin's files
readable by everyone, as `--chmod=u=rwX,go=rX` does. Point a RuntimeProfile on
the `dsh_superagent` engine at that image. The
Source's settings appear in the panel's **Settings**. The plugin reaches its
service with a credential from a Credential Vault assigned to the Assistant:
an `environment_variable` credential bound to the service's host gives the
plugin a placeholder in its environment, which the sandbox's outbound proxy
replaces in matching requests ([Protect credentials](egress-credential-injection.md#add-an-outbound-credential)).
The RuntimeProfile's network access must admit the service's host.

An owner can also add a Source to their own Assistant, with no image of their
own: package it as a dsh plugin Bundle whose own `cordis.patch.yml` inserts the
plugin, and list it under **Profile package.json** on the Assistant's **Skills,
plugins and configuration** card, for example
`{"dependencies": {"my-tickets-source": "1.2.0"}}` (npm versions, git specs or
tarball URLs). Saving prepares the Assistant again on its box: the harness's own
`dsh plugin` installs the Bundle and the harness restarts; a failed install
stops the start with pnpm's own message. Every new box installs the Bundles
again from the configuration; they are not part of the saved state. The
[Assistant defaults](#defaults) can list Bundles for every Assistant on that
program. With limited network access, a public registry or git host is admitted
for the Assistant; a private one, such as an internal registry, only once an
administrator names it in the RuntimeProfile's allowed hosts.

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
a reply is in progress, that reply ends and Hermes does not run that message
again: a clean shutdown stops it and says so, and a gateway that died without
settling it ends it with an error saying its outcome is unknown. Messages
already sent behind it are answered in order once the conversation reconnects.

A conversation that cannot be prepared ends and shows why, for example when
the Assistant names an MCP server but its RuntimeProfile does not allow MCP
servers. Fix the cause, then start a new conversation.

## Start and continue work

Select the Assistant in the rail. It opens the Assistant's **main
conversation**, the same one every time, starting it when the Assistant has
none; AstraBox prepares or restores the workspace when needed. Its routines
and watches report there, and the Assistant's row in the rail is
marked unread when a reply arrives while the owner is elsewhere. The main
conversation is that row, so it is not listed again under Sessions.

Before the first message, the conversation lists what the Assistant can use:
the apps connected for it, its Skills and tools, its own and inherited, and the
apps the owner could connect. **Assistant** in the conversation header opens
its profile: name, timezone, the owner's instructions, connected apps switched
on or off for it, what it inherits, and deletion. See [Routines and watches](#routines-and-watches).

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

## Reach it from messaging apps {#triggers}

An Assistant can have its own triggers, the same Deployments an Agent has:
a messaging channel such as Telegram or Slack, or a webhook. Its schedules are
its owner's personal schedules instead; see
[Routines and watches](#routines-and-watches).
Open **Management console → Deployments**, select **New trigger**, and pick
the Assistant under **Agent or Assistant**. Only the Assistant's owner can
create or change its triggers, and every conversation they start runs as the
owner. A trigger is the only way a messaging app reaches the Assistant: a bot
token among the RuntimeProfile's variables reaches the agent's tools, but
Hermes' own gateway is kept off every messaging platform.

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
result, a finished background task, or a reply Hermes starts itself.

## Routines and watches {#routines-and-watches}

An Assistant does two kinds of work without being asked each time, and the
owner manages them in different places.

**Routines** are work at a time the owner names, such as "every weekday at 8,
summarize my unread email". The Assistant creates one with its
`create_schedule` tool, as a personal schedule run by the platform's scheduler
as its owner. Each run is a message in the Assistant's main conversation,
marked as sent by a schedule, and the reply arrives there; if the owner is
mid-conversation, the run waits its turn. The owner lists, edits, disables,
runs and deletes routines in the account menu under **Personal schedules**,
beside the schedules the Assistant hands to an Agent
([Personal schedules](a2a.md#personal-agent-schedules)). A routine does not
wait for the workspace to be up: the platform's scheduler fires it, and the
main conversation prepares the workspace as it does for any message.

**Watches** are the Assistant keeping an eye on something and speaking up only
when it matters, such as "tell me when an email from my manager mentions a
deadline". Hermes runs these as jobs in its own scheduler, inside the
Assistant's workspace, and AstraBox reports each run back into the
conversation that created the job: a channel chat receives it right away, and
a console conversation shows it unread in the list, with a browser
notification when notifications are on. A run that answers `[SILENT]`, which
a watch does when nothing meets its bar, is kept but not delivered. The jobs
are kept in the platform database with the rest of Hermes' profile state, so
they survive replacing the workspace's sandbox.

Ask the Assistant to change or remove a watch through Hermes' native job tools.

Every Assistant also has AstraBox's `astrabox-proactive` Skill, shipped in the
Hermes image. It teaches the Assistant to offer routines and watches when it
notices recurring work (a morning briefing, an important-mail watch, meeting
prep, a weekly review), to tell the two apart by whether the owner named a
time, to create one only after the owner agrees, to write each one so it runs
on its own with the owner's connected apps, and to keep watches quiet until
something is worth reporting.

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
Assistant's Hermes gateway once no reply is running, as for any MCP change.

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
model's context window. It does so during a reply, once the context passes its
own threshold, and that reply waits for the summary. Hermes decides the model's
context window itself, from what the model endpoint reports or, when it reports
nothing, from the model's name.

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

Credential delivery is separate from ownership. Assigned MCP, environment and
HTTP Basic credentials require sandbox credential protection. The sandbox provider attaches the
credential only to matching outbound requests, keeping its stored value outside
the Assistant's sandbox. An authenticated managed MCP binding is rejected when
that protection is disabled.

Administrators can assign a Credential Vault to an Assistant from
**Management console → Credentials**. The Assistant can then use every assigned
credential without exposing its stored value to the user or Agent program: MCP
credentials for its remote MCP servers, and environment and HTTP Basic
credentials for the programs in its sandbox, such as a dsh-superagent Source
plugin calling its own ticket service. They belong to the Assistant's one
sandbox, which all its conversations share. See [Credentials](credentials.md) for supported credential types and
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

AstraBox drives Hermes' gateway over its canonical protocol, `hermes-gateway-v1`.
Before opening a native conversation, the core reads the gateway's
`runtime.describe` and refuses an image that does not advertise durable
admission, event replay and the per-conversation tool environment on
`session.create`. Deploy the core and its corresponding sandbox image together;
an application version alone does not identify an overlay image's protocol.

Each attachment first waits for the image's gateway bridge to answer its
readiness path, `/healthz` on port 9118. The bridge answers it 200 when the
gateway is ready and 503 while it is not, and the core waits through 503 and a
refused connection for up to 300 seconds. Any other answer means another
program serves that port, such as a server from an older sandbox image. The
reader stops at once with `runtime_protocol_incompatible`, without waiting the
remaining time. The error names the status it received, and the conversation's
waiting messages stay queued under their ids. Update the sandbox image, then
recover the conversation; the messages are delivered once.

An unreadable execution event stops that Session's reader with
`runtime_protocol_incompatible`. The page shows recovery required and the
specific protocol error, and refuses new input. Reader sweeps do not retry it.
The readable prefix, native response and input facts, and last readable cursor
remain intact. The platform does not mark a live box unavailable, guess a tool's
outcome, skip the event, or replay an admitted task.

Correct the runtime protocol before explicitly recovering the Session. If the
same gateway instance still holds the conversation's event ring, recovery reads
from the stored cursor. Restarting the gateway or replacing the box does not
retain that ring; this mechanism alone does not recover previously unread
completed tool events from SessionDB.

A conversation blocked by a runtime protocol fault can still be ended or
archived. These operations end its recorded native conversation on the existing
workspace: its queued messages are withdrawn, a running reply is stopped, and
the conversation leaves Hermes' list of live sessions. They do not release the
shared sandbox. A native cleanup failure returns an error and leaves the
conversation open for a retry. Ordinary working conversations remain protected
from end or archive.
