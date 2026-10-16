# Preserve records through an offline upgrade

Convert existing data to AstraBox **0.1.9's PostgreSQL schema, Alembic revision
`0002`**, before starting the upgraded installation. This guide defines the
record relationships and custody to preserve. The operator owns conversion,
transactional loading and its validation; AstraBox does not provide an
installation-wide import command or automatically convert 0.1.8 records.
Other databases and private extension formats are outside this specification.

The examples below were reviewed against the current models and API; they were
not executed as a migration acceptance. Pin both the installed release and its
schema revision. Do not load old collection documents into the new tables.

## Prepare and load in this order

1. Stop source and destination writers, scheduling and runtime readers at a
   consistent cut. Finish native-state persistence; resolve uncertain input
   consumption before exporting. Back up the source and its keys. Preserve
   workspace files through their storage system separately.
2. Prepare the destination's PostgreSQL schema with the release's offline
   `astrabox.persistence.sql.migrate.upgrade_to_head()` operation. This applies
   schema revisions, not old-data conversion. Do not start onebox or the API
   to prepare an import: startup creates installation identity, reconciles
   compute, seeds defaults and starts readers before extension lifespan hooks.
3. In an operator-controlled database transaction, load installation identity
   and sealed credentials, Environments, then Agents and Assistants with their
   authorization. Load Sessions before setting Assistant main-Session links.
   Include the ordered Vault/MCP/connector bindings the selected records use.
4. Load the selected history and, if native continuation is required, complete
   native state. Validate the graph and ciphertext before committing. The
   existing HTTP history API described below is a separate transaction after
   startup, suitable only before the Session has its own messages.
5. Start the installation only after the offline transaction commits and the
   original identity/key configuration is in place. Verify ownership and
   history readback before sending the first intentional new input.

A nonempty destination can contain matching references or unrelated records.
The operator's loader must compare normalized values: the same ID and content
is unchanged; the same ID with another owner or configuration is a conflict
that rolls back the load. Do not use blind upserts, rename business IDs to
avoid conflicts, or retry an uncertain commit without inspecting its outcome.
These are loader requirements, not a built-in graph-import API.

## Example identity and column map

These synthetic values show the links to preserve. They are a column map, not
an executable SQL script or a new input format. Carry all required configuration,
valid lifecycle fields and timestamps from the converted records; use the
linked release declarations for defaults and nullability.

| Table | Example columns |
|---|---|
| `installation` | `singleton=true`, `installation_id=<original installation ID>` |
| `secrets` | `scope='environment/agent-env'`, `key='provider_access.api_key/ref-01'`, `sealed=<original authenticated ciphertext>` |
| `environments` | `name='agent-env'`, `engine_kind='claude_code'`, `network_type='unrestricted'`, `idle_action='pause'`, `provider_api_key_ref='provider_access.api_key/ref-01'`; a separate `assistant-env` uses the installed Assistant-capable `hermes` engine |
| `agents` | `agent_id='agent-01'`, `name='Example'`, `name_key='example'`, `environment_name='agent-env'`, `model=<configured model>`, `created_by='user-01'`, `org_id='org-01'`, `visibility='allowlist'`, `admins=['user-01']`, `allowed_user_ids=['user-01']`, `version=1`, `state='ACTIVE'` |
| `assistants` | `assistant_id='assistant-01'`, `owner_id='user-01'`, `org_id='org-01'`, `engine_kind='hermes'`, `environment_name='assistant-env'`, `display_name='Workspace'`, `is_primary=true`, `workspace_id=<preserved workspace ID>`, initially `main_session_id=NULL` |
| `sessions` — Agent | `session_id='00000000-0000-4000-8000-000000000001'`, `user_id='user-01'`, `org_id='org-01'`, `session_kind='agent_chat'`, `workspace_kind='agent'`, `agent_id='agent-01'`, `assistant_id=NULL` |
| `sessions` — Assistant | A distinct stable Session ID ending `002`, the same user/org, `session_kind='assistant_chat'`, `workspace_kind='assistant'`, `assistant_id='assistant-01'`, `agent_id=NULL` |
| main-Session link | After Session002 exists, set `assistants.main_session_id` to it; verify that its owner and Assistant ID agree |

The exact release models are
[Environment](https://github.com/Colton-z/AstraBox/blob/v0.1.9/astrabox/persistence/sql/models/environments.py),
[Agent](https://github.com/Colton-z/AstraBox/blob/v0.1.9/astrabox/persistence/sql/models/agents.py),
[Assistant](https://github.com/Colton-z/AstraBox/blob/v0.1.9/astrabox/persistence/sql/models/assistants.py),
[Session and native state](https://github.com/Colton-z/AstraBox/blob/v0.1.9/astrabox/persistence/sql/models/sessions.py),
and [Vault/Secret](https://github.com/Colton-z/AstraBox/blob/v0.1.9/astrabox/persistence/sql/models/vaults.py).
Their constraints do not check every semantic link: validate Session kind,
engine compatibility, same-Session response/input links and owner-only
Assistant access as well. Internal identity columns need correct allocator
restoration or reference-preserving remapping; public output cursors and
business IDs must remain stable.

Normal Agent creation generates an ID and schedules reconciliation. It is not
an identity-preserving replacement for the offline load. Keep deleted-primary
Assistant records where present: they participate in primary identity history.
Before startup, also account for default seeding: any live Agent suppresses it;
an Assistant-only database does not. With no live Agent, the seed can replace
an Environment named `claude-code`. Do not fabricate an Agent to bypass this
behavior or assume that an Assistant-only load suppresses it.

## Import earlier messages through the existing API

An authenticated administrator can add display history to Session001 before it
has messages of its own. Use its actual preserved Session ID:

```http
POST /api/v1/admin/sessions/00000000-0000-4000-8000-000000000001/history-import
Content-Type: application/json
Authorization: Bearer <administrator access token>

{"messages":[
  {"source_id":"old-user-1","role":"user","created_at":"2026-10-01T10:00:00Z","blocks":[{"type":"text","text":"Earlier question."}]},
  {"source_id":"old-reply-1","role":"assistant","created_at":"2026-10-01T10:01:00Z","parent_source_id":"old-user-1","completion":"complete","blocks":[{"type":"text","text":"Earlier answer."}]}
]}
```

The first import returns `imported: 2, unchanged: 0`; an identical repeat returns
`imported: 0, unchanged: 2` inside the normal response envelope. Changing the
reply under `old-reply-1` produces `HISTORY_IMPORT_CONFLICT`. A valid request
that reorders already-imported messages also conflicts. A Session with its own
messages refuses with `HISTORY_IMPORT_AFTER_CONVERSATION`.

The API also preserves supported image, thinking, tool-use and tool-result
blocks. Use `completion: "unknown"` when the source recorded no reply ending;
do not manufacture completion. It assigns new imported input/response IDs and
preserves source provenance. It does not execute imported inputs or restore
native SessionStore. See [Import earlier history](sessions.md#import-earlier-history).

## Preserve identity, keys and native custody

Platform user IDs follow the identity provider's stable subject; display-name
rows do not register accounts or authorize access. Preserve the configured
identity provider, user/org IDs, Agent authorization and Assistant ownership.
An external agent account, delegation and filesystem mapping remain the
extension owner's responsibility: supply their stable IDs, credential/refresh
references, workspace paths and ACLs. Do not assume platform user IDs recreate
an external account, or require new authorization to cover a missing mapping.

Copy ciphertext only with the original Secret Store format, scope/key and
master key or KMS decryption permissions/context. Authenticate every referenced
value privately; masks, missing keys and undecryptable values are errors.
Changing stores requires explicit resealing through the target store. Retain
platform signing material as well. Expired or revoked external authorization
is not repaired by copying records.

Preserve an installation ID only when replacing that same stopped installation.
Do not overwrite another installation's ID to adopt its sandboxes. Retire old
compute through its owning lifecycle, or explicitly recreate it after cutover;
do not transplant live reader/terminal leases, pending control generations or
prewarm claims. Workspace identity/files are preserved independently.

For native continuation, retain `engine_session_key`, every transcript scope
and native child transcript, and both native SessionStore and profile-state
archives that the engine uses. Validate owner tuples, published heads,
predecessors, chunk count/order, byte size and SHA256. Preserve output parts,
interactions and saved answers as well as text. Without that custody, describe
the result as readable history with new native execution, not resumed context.
Refuse uncertain input-consumption or incompatible cursor mappings: consumed
work must never become pending again or be replayed to rebuild missing history.
