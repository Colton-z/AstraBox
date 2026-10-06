"""The six engine-independent artifact tools and their maintained authoring guide."""

from __future__ import annotations

import json
import re
from typing import Any

from .limits import KINDS


def has_artifact_marker(output: Any) -> bool:
    """Find markers within 12 levels, 4096 values and 8,388,608 JSON text characters."""
    visited: dict[int, Any] = {}
    remaining_values = 4096
    remaining_text = 8 * 1024 * 1024

    def read(value: Any, depth: int) -> bool:
        nonlocal remaining_values, remaining_text
        if depth > 12 or remaining_values <= 0:
            return False
        remaining_values -= 1
        if isinstance(value, str):
            if len(value) > remaining_text:
                return False
            remaining_text -= len(value)
            if "astrabox_artifact" not in value:
                return False
            try:
                parsed = json.loads(value)
            except (ValueError, RecursionError):
                return False
            return isinstance(parsed, (dict, list)) and read(parsed, depth + 1)
        if not isinstance(value, (dict, list)) or id(value) in visited:
            return False
        visited[id(value)] = value
        if isinstance(value, dict):
            marker = value.get("astrabox_artifact")
            if isinstance(marker, dict):
                version = marker.get("version")
                if (
                    isinstance(marker.get("id"), str)
                    and re.fullmatch(r"art_[0-9a-f]{24}", marker["id"])
                    and isinstance(version, int)
                    and not isinstance(version, bool)
                    and version > 0
                    and isinstance(marker.get("title"), str)
                    and isinstance(marker.get("kind"), str)
                    and marker["kind"] in KINDS
                ):
                    return True
        for item in value.values() if isinstance(value, dict) else value:
            if remaining_values <= 0:
                break
            if read(item, depth + 1):
                return True
        return False

    return read(output, 0)


GUIDE = r"""# Authoring AstraBox artifacts

Create an artifact for substantial standalone content a reader will reuse: an
interactive dashboard, calculator, diagram, document or reusable source file.
Keep short answers in the conversation. Pages are private by default, hosted in
the platform database, and survive the conversation and sandbox. Later runs can
find the same artifact with list_artifacts, get it, update it or refresh its db,
only within the same origin Agent or Assistant and for the binding's user.
Another Agent or Assistant cannot access these artifacts. Messaging-channel
Sessions have no artifacts tools; schedule, webhook and manual runs retain them.
A grant-bearing run reaches only its granted artifact and cannot create another.

## Tools
create_artifact({title, kind, content | file_path, language?, description?, capabilities?})
update_artifact({artifact_id, content | file_path | edits, title?, description?, capabilities?, base_version?})
get_artifact({artifact_id, version?}); list_artifacts({query?, limit?})
get_artifact also returns viewer {id,level,guest} for choosing private db paths.
artifact_data({artifact_id, action, path?, collection?, data?, where?, order_by?, limit?, writes?})
artifact_guide({})

Kinds: html (complete HTML), react (default-exported component, JSX/TypeScript),
svg, markdown, mermaid, code (requires language). Content is UTF-8, at most
4 MiB. file_path is copied from the current live workspace with the file API's
path guards; it is never a URL or a sandbox dependency of the published page.
Titles are at most 200 characters. Updates are immutable versions (at most
1000); capabilities omitted carry forward, {} clears them. Edits are ordered
{old_string,new_string,replace_all?}: absent or ambiguous matches reject the
whole operation. Pass base_version from get_artifact to avoid overwriting a
concurrent publish: use its astrabox_artifact.version field. Create and update
return only astrabox_artifact {id,version,title,kind} and url. list_artifacts
returns items in the same form. get_artifact adds content, description, manifest
and the acting viewer's opaque identity. Results never include owner identity,
share tokens or share state. Sharing is managed through the console.

## Frame and libraries
Scripts run in an opaque-origin frame. Network requests, external images and
page-initiated downloads are blocked. Inline scripts/styles, approved script
CDNs, embedded data/blob images and bundled libraries work. Use runtime db,
sample or agent for platform data; use downloads for saving a file.
Pages may use localStorage; the shell supplies it per artifact per viewer
(maximum 5 MiB). sessionStorage lasts for this page load. Neither is shared
between viewers; use db for durable collaborative data.

React imports: react, react-dom, lucide-react, recharts, lodash, d3, papaparse,
mathjs, three, xlsx, and @/components/ui/{button,card,input,label,textarea,
select,checkbox,switch,tabs,badge,alert,separator,progress,slider,dialog}.
Tailwind classes are supported through @tailwindcss/browser. Export one default
component. Unknown imports display an error; there are no arbitrary packages.

## Declaration and entry point
capabilities = {artifact:{}, db:{rules:[]}, sample:{}, agent:{}, user:{},
                room:{topics:{chat:'interact'}}, downloads:true}
Declare only the capabilities the page needs. permissions is built in and must
not be declared. Unknown names are refused. use('assets'), use('comments') and
other unsupported names return null. Historical versions have no runtime;
pinned shares fix content only and use the current manifest, db rules and room
topics, but cannot publish.

```ts
const db = await window.astrabox.use('db'); // namespace or null
// use(name): Promise<Readonly<Namespace> | null>, null after 10s without a shell
```
Always handle null. Namespaces are frozen and limited by the manifest, viewer
level and supported contract. Levels: none < view < interact < owner. Only the
owner publishes or manages the artifact; administrators have no implicit access.

## artifact
```ts
artifact.publish(html: string): Promise<{astrabox_artifact, url}>
```
Publishes a version with base_version equal to the viewed version. Open latest
views refresh. Others receive not_writer, a race receives conflict. Pinned
shares cannot publish, including when the owner follows their pinned link.

## db
Firestore-shaped paths: odd segments name collections, even name documents.
Documents are JSON objects, at most 256 KiB; at most 10000 documents per page.
```ts
db.doc(path: string): DocumentRef
db.collection(path: string): CollectionRef
ref.get(): Promise<DocumentSnapshot | QuerySnapshot>
doc.set(data: object): Promise<void>
doc.update(patch: object): Promise<void> // shallow merge; missing rejects
doc.delete(): Promise<void>
collection.add(data: object): Promise<DocumentRef>
collection.where(field: string, op: '=='|'!='|'<'|'<='|'>'|'>='|'in'|'array-contains', value: unknown): Query
query.orderBy(field: string, direction?: 'asc'|'desc'): Query
query.limit(n: number): Query
ref.onSnapshot(fn: (snapshot) => void): () => void
// DocumentSnapshot: {exists, id, data()}; QuerySnapshot: {docs, size, empty, docChanges()}
```
Subscriptions refetch when a visible data-path event arrives; document contents
are never broadcast in live events. HTTP/tool queries use where entries
{field,op,value}, order_by entries {field,direction}, and limit.
artifact_data actions: get, list/query (collection), set/update/delete (path),
batch (writes [{action:'set'|'update'|'delete',path,data?}]). Batch is atomic.

Default read=view, write=interact. Deeper prefix rules override inherited levels:
{path:'scores',read:'view',write:'owner'}. At most 64 rules; write must be at
least read. {self} is allowed only as a final rule segment, substituted with
this viewer's opaque id. data/users/{self} is private even to other owners;
a rule at data/users that sets BOTH read and write explicitly opens that
prefix. The same applies to explicit {self} subtrees. Guests have no {self}
and never write. Example: db.doc('data/users/' + user.id() + '/profile').

## sample
```ts
const model = await window.astrabox.use('sample');
model.sample(input: string | Array<{role:'user'|'assistant'|'tool',content:string|null,
  tool_calls?:unknown[],tool_call_id?:string}>, options?: {
  tools?:Array<{name:string,description?:string,input_schema?:object,execute(input):unknown}>,
  maxTokens?:number, modelTier?:string, cache?:boolean,
  onText?:(text:string)=>void, signal?:AbortSignal
}): Promise<{text:string,tool_calls:unknown[],truncated:boolean}>
model.json(input, options): Promise<unknown>
```
The shell asks for consent once per page load. One model round per call, using
the origin's model. The page runtime executes only the page-provided tools and
can send tool results in another round. The server fixes the system message;
pages cannot set it. modelTier is accepted and ignored (one model).
HTTP requests use input or messages arrays, max_tokens and tool definitions
without execute. SSE events are delta {text}, done {text,tool_calls,truncated}
and error {code,message,text}; tool_calls use the native function shape.
Requires a signed-in viewer allowed to use its origin Agent, including owners;
Assistant origins require ownership. Deleted origins return unavailable.
Limits: 2 concurrent requests and 30 per minute per viewer across replicas,
8192 max_tokens (default 4096), and 256 KiB request JSON.

## agent
```ts
agent.run({prompt:string}): Promise<{runId:string,sessionId:string,url:string}>
agent.status(runId:string): Promise<{state:'running'|'waiting_input'|'completed'|'failed',sessionId:string,error?:string}>
agent.watch(runId:string, fn:(status)=>void): () => void
```
Use agent for live data from the origin Agent's tools, connectors, network and
credentials. The shell displays the exact prompt and asks for consent EVERY
run. Each run creates a normal conversation owned by the viewer and sends a
platform preamble plus this prompt. Its artifacts tools get a grant for ONLY
this artifact, act as the viewer under db rules, and use the viewer's {self}.
The share grant is revalidated on each call, including revocation, expiry,
current access and served content pin. A non-owner can read and write permitted data, but cannot publish versions or
read any other artifacts, including the viewer's own. The origin Agent runs in its ordinary
sandbox with its own MCP and egress policies. Watch db snapshots for results.
Requires a signed-in owner or interact viewer allowed to use the origin Agent;
Assistant origins allow owner only. One active run per viewer/artifact, ten
per viewer/hour. waiting_input means the Agent asked a question; link to url.
A Deployment schedule can periodically refresh the same artifact through tools.

## user
```ts
user.isOwner(): boolean; user.canEdit(): boolean
user.can(capability:string): boolean
user.id(): string|null; user.me(): Readonly<{id:string|null,guest:boolean,level:string}>
user.profiles(ids:string[]): Promise<Array<{id:string,display_name:string}>>
```
Ids are opaque and scoped to the artifact; guests have a per-page-load guest
id derived by a share-scoped HMAC. Profiles never reveal the platform user id
or email; guests get empty display_name for signed-in users.

## room
```ts
room.join(name:string): Room
room.emit(topic:string,data:unknown): Promise<{seq:number}>
room.on(topic:string,fn:(data)=>void): () => void
room.presence(state:unknown|null): Promise<{seq:number}>
room.onPeers(fn:(peers:Array<{id:string,state:unknown,guest:boolean}>)=>void): () => void
room.leave(): Promise<{seq:number}>
```
Presence is available to all viewers and expires after 45s without a heartbeat.
The runtime sends heartbeats while present; null leaves. Messages need owner unless a
manifest topic opens them to view/interact. Payloads are at most 16 KiB;
room events are retained for ten minutes. Emit/presence share a per-viewer
token bucket (10/s, burst 20); live streams are capped at 8 per viewer/replica. HTTP journal messages contain room/topic/data,
from (opaque id), from_kind ('user'|'guest'); presence contains room/viewer_id,
state or null, guest. Page on() callbacks receive data; onPeers() receives the
current peer list. Subscriptions return unsubscribe functions.

## downloads and permissions
```ts
downloads.save({filename:string,data:string|Blob|ArrayBuffer}): Promise<null>
permissions.state(name:string): Promise<{name:string,granted:boolean}>
permissions.state(): Promise<{granted:string[]}>
permissions.request(names?:string[]): Promise<{granted:string[]}>
permissions.manage(): Promise<{granted:string[]}>
```
The shell confirms EVERY download. Permissions manage per-load grants and never
expand a manifest or the server's authorization.

## Sharing and errors
Sharing is owner-managed in the console: access=view|interact,
audience=anyone|signed_in, version=null (latest) or a pinned integer, and an
optional expiry. Anonymous link visitors are view-only, admitted only for an
anyone audience whose CURRENT version declares neither sample nor agent.
Enabling anyone is refused when the CURRENT manifest declares either model
capability. Later model-capability updates also require anonymous visitors to
sign in. A pinned share keeps its version and cannot publish. Revocation and
expiry fail closed. Anonymous visitors see no author identity.

Runtime errors: not_granted, not_writer, conflict, invalid_argument, not_found,
rate_limited, unavailable, too_large, cancelled. Handle errors visibly and
preserve partial sample text on failure. A page error offers Ask the Agent to
fix; the viewer sends the artifact id, version and error to its conversation.
"""


def artifact_tools() -> list[dict[str, Any]]:
    string = {"type": "string"}
    properties: dict[str, dict[str, Any]] = {
        "create_artifact": {
            "title": string,
            "kind": {
                "type": "string",
                "enum": ["html", "react", "svg", "markdown", "mermaid", "code"],
            },
            "content": string,
            "file_path": string,
            "language": string,
            "description": string,
            "capabilities": {"type": "object"},
        },
        "update_artifact": {
            "artifact_id": string,
            "content": string,
            "file_path": string,
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": string,
                        "new_string": string,
                        "replace_all": {"type": "boolean"},
                    },
                    "required": ["old_string", "new_string"],
                    "additionalProperties": False,
                },
            },
            "title": string,
            "description": string,
            "capabilities": {"type": "object"},
            "base_version": {"type": "integer", "minimum": 1},
        },
        "get_artifact": {"artifact_id": string, "version": {"type": "integer", "minimum": 1}},
        "list_artifacts": {
            "query": string,
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "artifact_data": {
            "artifact_id": string,
            "action": {
                "type": "string",
                "enum": ["get", "list", "query", "set", "update", "delete", "batch"],
            },
            "path": string,
            "collection": string,
            "data": {"type": "object"},
            "where": {"type": "array", "items": {"type": "object"}},
            "order_by": {"type": "array", "items": {"type": "object"}},
            "limit": {"type": "integer", "minimum": 1},
            "writes": {"type": "array", "items": {"type": "object"}},
        },
        "artifact_guide": {},
    }
    required = {
        "create_artifact": ["title", "kind"],
        "update_artifact": ["artifact_id"],
        "get_artifact": ["artifact_id"],
        "artifact_data": ["artifact_id", "action"],
    }
    descriptions = {
        "create_artifact": "Publish substantial standalone content the reader will reuse as a durable page. Not for short answers. Read artifact_guide for kinds, libraries and the runtime API.",
        "update_artifact": "Publish a new version of a reusable artifact; rewrite, copy a file, or apply exact edits. Read artifact_guide for runtime capabilities and conflicts.",
        "get_artifact": "Read an artifact's metadata and content before editing it or using its db. Read artifact_guide for the page runtime API.",
        "list_artifacts": "Find the user's reusable artifacts, newest first, including ones from earlier conversations.",
        "artifact_data": "Read or refresh the artifact's durable db under the acting viewer's rules. Read artifact_guide for paths, queries and atomic batches.",
        "artifact_guide": "Read the complete authoring and window.astrabox.use runtime guide before writing an interactive artifact.",
    }
    return [
        {
            "name": name,
            "description": descriptions[name],
            "inputSchema": {
                "type": "object",
                "properties": props,
                "required": required.get(name, []),
                "additionalProperties": False,
            },
        }
        for name, props in properties.items()
    ]
