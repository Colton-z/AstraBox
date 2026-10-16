# Artifacts

An artifact is a standalone page, app, diagram, document, or source-code view
published by an Agent or Assistant. Its content, versions, data, and share
settings are stored in AstraBox's database. You can reopen it after the Session
ends or its sandbox is destroyed; persistent workspace storage is not required.

Artifacts are private personal resources. The user the creating conversation
runs for owns them. Administrators do not gain access to another user's artifacts.

## Create an artifact

Ask an Agent or Assistant to publish substantial, reusable output as an artifact:

> Create an HTML artifact titled “Project checklist”, with a checklist I can
> use in the browser. Publish it with the artifacts tool.

The built-in `artifacts` tools are available to Agent and Assistant conversations
unless the Agent's **Artifacts** switch is off or the Session was started by a
messaging channel.
`create_artifact` accepts a title, kind, and either content or a workspace
`file_path` that the platform copies. Reading a workspace file during publication
does not make subsequent viewing depend on that file or sandbox.

To keep an Agent from publishing artifacts, turn off **Artifacts** in the
Agent's **MCP, Skills and Plugins** settings, or send `"artifacts_enabled": false`
when creating or updating it. An Agent created without the field has the tools.
The next conversation starts without them, including one that takes a prepared
sandbox, and calls from the Agent's existing conversations are refused.
Artifacts the Agent already published stay viewable and keep their share links.

| Kind | What opens |
| --- | --- |
| `html` | An HTML page with scripts |
| `react` | A React component with a default export; JSX and TypeScript are supported |
| `svg` | A vector graphic |
| `markdown` | A document rendered by the console, without page scripts |
| `mermaid` | A diagram |
| `code` | Source code with the supplied `language`, without execution |

React pages can import `react`, `react-dom`, `lucide-react`, `recharts`,
`lodash`, `d3`, `papaparse`, `mathjs`, `three`, `xlsx`, and the supported
`@/components/ui/*` controls: button, card, input, label, textarea, select,
checkbox, switch, tabs, badge, alert, separator, progress, slider, and dialog.
Tailwind is available. These runtime libraries are served by AstraBox; unknown
imports display an error with the available modules.

## Open and use the panel

A published artifact appears as a card in the conversation, outside folded tool
work. Its title, kind, and version identify the page. A new artifact opens the
**Artifacts** tab beside the conversation; use **Open** on a card to return to it.
Agent panels list artifacts published by that Session, while Assistant panels
list the Assistant's artifacts.

Use **Full page** for more space, **Copy source** to copy the viewed version,
or **Download source** to save it. The **Artifacts** navigation entry opens your
gallery at `/artifacts`; a full page has the address `/artifacts/<id>`.
The full page links to its origin conversation or Assistant and offers deletion.
Deleting an artifact removes its versions, data, and share link.

Page errors appear over the viewer. In the conversation panel, **Ask the Agent
to fix** prepares a message containing the artifact id, version, and error.
Review and send that message to request a repair.

## Versions and later updates

Every content publication creates a new immutable version. Ask the Agent to
rewrite the content or make exact string replacements with `update_artifact`.
An ambiguous or missing replacement fails without publishing; a stale
`base_version` is a conflict. Use **Version history** when more than one version
exists.

Open viewers following the latest version refresh when a new version is
published. Viewing a historical version grants no runtime capabilities. Restoring
a version through the API creates a new version; it does not alter history.
Artifact data is separate from versioned content and survives content updates.

A later conversation of the same user and the same Agent or Assistant can use
`list_artifacts`, `get_artifact`, and `update_artifact` to find and refresh the
same page. A scheduled Deployment of that Agent can do the same. These tools
cannot reach artifacts from another Agent or Assistant. Schedule, webhook and
manual runs retain the tools unless the Agent's **Artifacts** switch is off;
messaging-channel Sessions do not.

## Sharing

Open **Share artifact** and choose the link's access, audience, version, and
optional expiry, then **Save**. Copy the displayed link. **Revoke link** closes
it; revoked, expired, and deleted artifacts cannot be opened through that link.

| Setting | Meaning |
| --- | --- |
| **View** | Read content and use capabilities permitted at View level by the current manifest |
| **Interact** | Signed-in viewers may write data and send room events where the manifest permits it |
| **Anyone with the link** | Anonymous visitors are admitted with View access, even when the link says Interact |
| **Signed-in viewers only** | Visitors must sign in before using the link |
| **Follow the latest version** | The shared page follows subsequent publications |
| **Pin version** | The link serves selected page content; capabilities and live data rules follow the current version, and republishing is unavailable |

Only the owner can publish versions, change sharing, or delete an artifact.
An Interact link does not transfer ownership. Shared pages show **Content is
user-generated and unverified**. Anonymous visitors see no author identity.

The current version's `sample` or `agent` declaration requires signed-in
viewers, including on a pinned link. **Anyone with the link** is refused with
`ARTIFACT_SHARE_AUDIENCE_RESTRICTED`. Adding either capability to an existing
shared artifact requires anonymous visitors to sign in on subsequent runtime
requests (`ARTIFACT_SIGN_IN_REQUIRED`). The share dialog uses the current
policy even while you view history.

Model requests and Agent runs also require authorization to use the origin
Agent, including for the artifact owner. For Assistant artifacts, both are
restricted to the owner. `sample` permits signed-in View viewers with that
authorization; `agent` requires Interact or Owner access.

## Live page capabilities

Declare capabilities in `create_artifact` or `update_artifact`, for example
`{"db": {}, "user": {}, "room": {"topics": {"ping": "interact"}}}`.
Omitting `capabilities` on an update preserves them; `{}` clears them.
Inside a scripted page, `await window.astrabox.use(name)` resolves to a namespace
or `null`. Check for `null` before use. A declaration alone does not grant access:
the viewer's level and the current manifest govern each call. Pins keep selected
content while capability declarations, data rules and room topics use current
policy. Historical views have no runtime capabilities.
Reload a pinned page to obtain newly declared capabilities; server operations
revalidate current policy while an existing frame stays open.

The examples below belong inside an async page script and assume the named
capability is granted. `permissions` is built in and needs no declaration.

### Republish: `artifact`

Declare `{"artifact": {}}`. Only the owner can publish. The viewed version is
the base version, so simultaneous edits can return `conflict`. Pinned share
views do not grant this capability, including when the owner opens the link.

```js
const artifact = await window.astrabox.use('artifact');
if (artifact) await artifact.publish('<html><body>Updated page</body></html>');
```

### Store data: `db`

Declare `{"db": {}}`. Documents use even-segment paths such as `live/status`;
collections use odd-segment paths such as `notes`. The API supports `doc`,
`collection`, `get`, `set`, `update`, `delete`, `add`, `where`, `orderBy`,
`limit`, and live `onSnapshot` subscriptions.

```js
const db = await window.astrabox.use('db');
if (db) {
  db.collection('notes').onSnapshot(snapshot => {
    document.querySelector('#notes').textContent =
      snapshot.docs.map(doc => doc.data().text).join('\n');
  });
  await db.collection('notes').add({text: 'A shared note'});
}
```

Default access is View for reads and Interact for writes. A manifest may provide
`rules` with `path`, `read`, and `write` levels. Deeper prefix rules override
inherited levels; write must be at least read. The `data/users/{self}` subtree
is private to that viewer by default, including against the owner;
`{self}` expands to their opaque per-artifact id.
Store a document at `data/users/{self}/profile`, for example. Anonymous viewers
have no private `{self}` path and cannot write. Explicit `{self}` rules also
create private subtrees. A rule at a private prefix such as `data/users` can
open it by setting both `read` and `write`. There can be at most 64 rules;
`{self}` is allowed only as the final segment of a rule path.
A rule with `write: 'view'` permits signed-in View viewers to write; anonymous
viewers cannot write at any rule level.

### Ask the model: `sample`

Declare `{"sample": {}}`. The shell asks for consent once per page load.
Requests use the origin Agent's or Assistant's configured model. Review the
page before allowing requests, which consume model usage.
Requests use the configured server model credential. The bundled model gateway
receives attribution headers identifying the viewer and artifact.

```js
const model = await window.astrabox.use('sample');
if (model) {
  const answer = await model.sample('Suggest one checklist item.', {maxTokens: 100});
  document.querySelector('#answer').textContent = answer.text;
}
```

Options include streamed `onText` updates, page-provided tools, and caching.
`sample.json` parses a JSON answer. `modelTier` is accepted but does not choose
another model.

Each server request has a 60-second deadline, a 256 KiB JSON limit, and at most
8,192 output tokens (default 4,096). Each viewer can have two concurrent requests
and start 30 requests per minute across artifacts and backend replicas.
Page-provided tools can use up to eight model rounds; each round counts toward
these limits. Optional caching lasts five minutes within the page load.

### Identify viewers: `user`

Declare `{"user": {}}`. Ids are opaque and scoped to this artifact; they are
not platform account ids. Guests are identified by `me().guest`; their ids are
scoped to the share link and page load.

```js
const user = await window.astrabox.use('user');
if (user) document.querySelector('#role').textContent =
  user.isOwner() ? 'Owner' : (user.canEdit() ? 'Interactor' : 'Viewer');
```

`id`, `me`, `can`, and `profiles` provide viewer state and permitted profiles.
Profiles contain opaque ids and display names; guests receive empty display
names for signed-in users. Account ids and email addresses are not exposed.

### Presence and events: `room`

Declare `{"room": {"topics": {"ping": "interact"}}}`. Presence is available
to all viewers. Sending an event requires Owner unless the topic declares
another level. `join(name)` selects a named room.

```js
const room = await window.astrabox.use('room');
if (room) {
  room.onPeers(peers => { document.querySelector('#count').textContent = peers.length; });
  room.on('ping', data => { document.querySelector('#message').textContent = data.text; });
  await room.presence({viewing: true});
  await room.emit('ping', {text: 'Hello'});
}
```

Room events are visible for ten minutes. Presence expires 45 seconds after the
last heartbeat; the page runtime sends heartbeats while present. Use `leave()`
to leave explicitly. Events and presence share a per-viewer write budget of
10/second with a burst of 20 across artifacts and backend replicas.

### Save a file: `downloads`

Declare `{"downloads": true}`. The shell confirms every save; page-initiated
browser downloads are blocked.

```js
const downloads = await window.astrabox.use('downloads');
if (downloads) await downloads.save({filename: 'notes.txt', data: 'A shared note'});
```

### Refresh through the Agent: `agent`

Declare `{"agent": {}, "db": {}}` to run the origin Agent and observe its data
updates. The shell displays the page's exact prompt and asks for consent on
every call. A run starts a new conversation owned by the viewer, with the
viewer's artifact access and data rules. It uses the Agent's configured MCP
servers, credentials, and network policy.

The run's artifacts tools reach only this artifact, even if the viewer owns
others, and cannot create another artifact. Only the artifact owner can publish
versions. A shared viewer's tool access is revalidated against the link's
enabled state, token, expiry, audience, current access and served version;
revocation or expiry removes access, and a downgrade reduces the acting level.
Owner runs require current ownership and do not depend on a share link.

```js
const agent = await window.astrabox.use('agent');
if (agent) {
  const run = await agent.run({prompt:
    'Use artifact_data to write the latest project status to live/status on this artifact.'});
  agent.watch(run.runId, status => {
    document.querySelector('#state').textContent = status.state;
  });
}
```

`run` returns `runId`, `sessionId`, and `url`; open the conversation when the
Agent needs input. When the origin Agent's **Artifacts** switch is off, `run`
fails with `unavailable` (`ARTIFACT_AGENT_UNAVAILABLE`) because the run could
not reach the artifact. `status` and `watch` report `running`, `waiting_input`,
`completed`, or `failed`. Data written by the run arrives through the page's
`db` subscriptions. There is one active run per viewer per artifact and a
limit of 10 runs per viewer per hour across artifacts and backend replicas.
Waiting for input counts as active.

### Manage grants: `permissions`

Grants apply to the current page load. A viewer can remove and restore eligible
capabilities, but cannot gain access beyond the manifest or their level.

```js
const permissions = await window.astrabox.use('permissions');
if (permissions) await permissions.manage();
```

`state(name)` inspects a grant and `request(names)` asks for eligible grants.

## Security and limits

Scripted content runs in an opaque-origin sandboxed iframe. It cannot read
console cookies, storage, or `parent.document`. It has no direct network data
access; server-backed runtime calls use the shell's message bridge and are
checked again by the server. The shell also validates paths and confines
requests to this artifact's runtime routes. External images and nested frames
are blocked. Inline scripts and styles, bundled runtime libraries, and the
supported script/font CDN hosts are allowed. Do not put secrets into page source
or shared data.

The runtime supplies per-artifact, per-viewer `localStorage`, limited to 5 MiB;
share-token views also isolate storage by token. `sessionStorage` lasts for
the page load. These stores do not expose the console's storage.

Artifact tool results in conversation history contain the artifact marker and
URL; `get_artifact` adds content, description, manifest and the acting viewer's
opaque identity. Data operations return their data result and marker.
Tool metadata contains no owner id, origin or share state. Sharing is managed
through the owner console and API.

| Limit | Maximum | API error |
| --- | --- | --- |
| Content per version | 4 MiB of UTF-8 | `ARTIFACT_CONTENT_TOO_LARGE` |
| Versions per artifact | 1,000 | `ARTIFACT_VERSION_LIMIT` |
| Title | 200 characters | `ARTIFACT_TITLE_LIMIT` |
| Data document | 256 KiB of JSON | `ARTIFACT_DATA_LIMIT` |
| Documents per artifact | 10,000 | `ARTIFACT_DATA_COUNT_LIMIT` |
| Room payload | 16 KiB of JSON | `ARTIFACT_ROOM_LIMIT` |
| Sample request JSON, including raw HTTP bytes | 256 KiB | `ARTIFACT_SAMPLE_TOO_LARGE` |
| Sample output tokens per request | 8,192 (default 4,096) | `ARTIFACT_SAMPLE_TOKEN_LIMIT` |
| Concurrent sample requests per viewer | 2 across artifacts and replicas | `ARTIFACT_SAMPLE_RATE_LIMITED` |
| Sample starts per viewer | 30/minute across artifacts and replicas | `ARTIFACT_SAMPLE_RATE_LIMITED` |
| Room/presence writes per viewer | 10/second, burst 20 across artifacts and replicas | `ARTIFACT_LIVE_RATE_LIMITED` |
| Live streams per viewer per backend replica | 8 across artifacts | `ARTIFACT_LIVE_RATE_LIMITED` |
| Active Agent runs per viewer per artifact | 1, including waiting for input | `ARTIFACT_AGENT_RUN_LIMITED` |
| Agent-run starts per viewer | 10/hour across artifacts and replicas | `ARTIFACT_AGENT_RUN_LIMITED` |

Sandbox file access (`files`) is unavailable because artifacts must remain
usable without a sandbox. Direct viewer connectors (`mcp`) are unavailable;
use `agent` so calls follow the Agent's credentials and network policy.
`assets` and `comments` resolve to `null`: blob storage and comment threads are
not supported. Templates, remixing, email invitations, and an editor role are
not supported. Artifacts are platform-hosted output, not a general application
hosting service with arbitrary network access or package installation.

### Runtime errors

Page calls reject with `code` and `message`: `not_granted`, `not_writer`,
`conflict`, `invalid_argument`, `not_found`, `rate_limited`, `unavailable`,
`too_large`, or `cancelled`. Handle failures visibly; model failures may include
partial `text`. Runtime endpoints map artifact API errors to
`data.runtime_code`; live and sample SSE error events use runtime codes.

For example, stale `base_version` returns `ARTIFACT_VERSION_CONFLICT`
(`conflict`), denied data reads return `ARTIFACT_DATA_DENIED` (`not_granted`),
and requests to an undeclared capability return
`ARTIFACT_CAPABILITY_NOT_DECLARED` (`not_granted`). Missing or foreign owner
resources return `ARTIFACT_NOT_FOUND`; invalid share links return
`ARTIFACT_SHARE_NOT_FOUND` (both `not_found`).
