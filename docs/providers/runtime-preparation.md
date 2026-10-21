# Runtime preparation integrations

Install one deployment-owned `RuntimePreparationProvider` through
`astrabox.providers.runtime_preparation`:

```toml
[project.entry-points."astrabox.providers.runtime_preparation"]
enterprise = "my_deployment.preparation:PreparationProvider"
```

The entry point targets a subclass of `astrabox.seams.RuntimePreparationProvider`
with a stable `name`. Conflicting providers fail startup. With no provider, the
platform prepares Agents and Sessions without callbacks.

One provider answers every point at which a deployment prepares an Agent, an
Assistant or a Session. Each method has a default, so implement only the points
you need:

| Method | When the platform calls it |
| --- | --- |
| `plan` | Asynchronously when resolving a configuration that admits work; reads persisted subject policy |
| `synchronize`, `recover` | Once per prepared Agent box, before it becomes waiting inventory; for an Assistant, whenever its workspace prepares a revision |
| `prepare_session` | Before engine activation: cold start, claim, reattachment, sandbox replacement |
| `prepare_turn` | Before each input is handed to the engine |
| `read_user_actions`, `perform_user_action` | When the Session's owner reads or acts on its prerequisites |

The platform owns ordering, placement, fencing, file delivery and failure
handling. The provider owns external identities, declaration formats, credential
policy and prerequisites. It never starts a consent flow by itself, registers an
Agent per Session, or dispatches work.

## Declare when a Session is required

`async plan(*, config) -> RuntimePreparationPlan` may read the integration's
persisted policy. Its detached resolved configuration contains no requesting
Session. It must not perform writes or depend on process-local caches. The
platform resolves it under a 30-second deadline into one runtime view; generation,
inventory preparation and claim use that view's declaration. Read-only history
and management projections do not call it. The extension interface version is
`8`; `plan` is an asynchronous method.
`session_bound=True` means the engine cannot be initialized through
`EngineAdapter.prepare_runtime` before a real Session's materials exist.

The platform still prepares the image, workspace account, Plugin/Skills caches,
control service and shared seats. Waiting inventory contains no personal Session
material. Its receipt records `engine_initialized=False`. Claiming that inventory
retains its placement, delivers the Session material and activates the engine
without treating it as an already initialized vendor runtime. Failures discard
exactly the claimed unit; they do not silently switch to a cold start.

The provider name and declaration participate in the runtime generation. Changes
invalidate waiting inventory. With `session_bound=False`, ordinary initialized
prewarming remains available. Model delivery defaults to `"platform"`, which
uses the configured protected Vault path for initialized inventory.
Session-bound resource prewarming also works without Credential Vault; it leaves
model and environment credentials out of unclaimed inventory and resolves them
at claim. It does not implement unavailable Vault capabilities.

An integration that protects model traffic itself declares
`model_credential_delivery="provider"`, independently of `session_bound` and the
platform Vault switch. The engine receives an inert workload-specific placeholder
instead of the configured model key. The integration must refuse unbound traffic
and bind only the persisted Session owner and subject in `prepare_session` before
returning. `context.model_credential_placeholder` names the actual value already
fixed in an initialized process. It remains stable on reattachment; a prepared
claim retains its inventory identity. Use the supported backend or external
gateway API to update that binding, rather than changing the child's environment.
Reattachment refuses a change of delivery owner for an existing native process;
replace its compute to change that frozen contract.
If the backend needs its credential proxy prepared at box creation, the sandbox
integration requests `SandboxCreateSpec.credential_proxy_enabled` independently
of the platform Vault. This delegation covers only model delivery: MCP and
environment credentials retain their own protection requirements.

This controls initialization performed by the engine adapter. Required system
services and wrappers must already be baked into the selected image. The provider
cannot retrofit a wrapper into a box or defer an image's own entrypoint.

## Synchronize prepared resources

`async synchronize(*, context: ResourcePreparationContext) -> None` runs after
the actual Plugin and Skills caches exist in a box and before any engine runs
from them. `context.operation.subject` says what the resources belong to:

- **An Agent** (`kind="agent"`): once for each prepared box, before it becomes
  waiting inventory. An Agent without prepared capacity is never synchronized;
  claim, cold start, reattachment and input refresh do not call it.
- **An Assistant** (`kind="assistant"`): whenever its workspace prepares a
  revision of the Assistant — when the workspace is materialized, and when a
  conversation or the platform prepares an edited Assistant on the running box —
  before Hermes runs that revision. A conversation that attaches to a workspace
  already prepared for the current revision does not call it.

Per-conversation declarations belong in `prepare_turn`.

| Context | Meaning |
| --- | --- |
| `operation.subject` | `PreparationSubject(kind, id)`: the Agent or Assistant ID, the external identity key |
| `operation.operation_id` | Durable invocation identity for idempotency and recovery |
| `owner_user_id` | The Agent's persisted creator, or the user whose Assistant workspace this is |
| `revision` | The Agent's authored version, or the Assistant's configuration revision |
| `preparation_id`, `runtime_generation` | The Agent's pool lifetime, or the Assistant's box; the resolved recipe |
| `config` | Detached resolved configuration |
| `sandbox` | Standard `SandboxHandle`, including its file interface |
| `packages` | Ordered source declarations and actual cache checkout paths |
| `skills_path` | Actual prepared Skills directory, or `None` |

Read actual prepared files and merge the deployment's declarations. Keep the
external identity keyed by the subject across revisions and boxes. Skip
unchanged external writes in the provider. Do not log trusted configuration.

A compare-and-set on the platform's stored operation serializes host-dispatched
`synchronize` invocations across replicas and boxes. For an
Agent the host checks the version, refresh revision, pool lifetime and
generations before and after synchronization and before publishing inventory;
for an Assistant it checks that the revision is still current. A superseded
result is refused with `AGENT_RUNTIME_GENERATION_CONFLICT` or
`ASSISTANT_REVISION_CONFLICT`. While another synchronization of the same subject
runs, an Agent's preparation fails with `RESOURCE_SYNC_BUSY` and retries; an
Assistant's waits for it.

An exception, timeout, cancellation or host loss leaves an unresolved operation
(`RESOURCE_SYNC_FAILED`, then `RESOURCE_SYNC_UNRESOLVED`).
Its deadline permits `async recover(*, operation: ResourcePreparationOperation)`;
it does not permit a new uncertain write. Return `True` only after that exact
operation can never write again, including delayed requests. Recovery must be
idempotent and must not fence newer operations. The default returns `False`.
A successful `synchronize` must leave no outstanding external writes. An expired
lease or absent remote result alone does not prove quiescence.

### Provider state and transaction boundaries

The context is a trusted snapshot supplied by the host, not a held database
transaction or a lock on the subject. Its owner, revision and operation identify
the preparation being attempted; they do not guarantee that those values remain
current while provider code runs. An Assistant's revision includes inherited
defaults and RuntimeProfile configuration, not just its authored fields. The host's
checks reject superseded preparation results; they cannot undo provider writes
that already committed or remote requests that already took effect.

Keep external bindings and operation journals in provider-owned storage. Key a
binding by `(subject.kind, subject.id)` so revisions and replacement boxes retain
the same external identity. Use `operation_id` to identify an invocation's durable
work and recovery record. Coordinate any provider-owned registration endpoint
with these same records, using uniqueness and conditional updates to prevent
competing registrations or stale writes. Host operation coordination does not
serialize writes initiated independently by that endpoint.

A provider can commit its own binding and journal together with authoritative
subject validation through the public `PreparationCommitGuard`. A host-dispatched
`synchronize` context supplies `context.commit_guard`, bound to its real operation
and prepared generation. Validate it on the **same connection and transaction**
as the provider-table writes. Reading platform state before and after a write
does not provide that guarantee. Do not update platform-owned definition fields
to store private provider state.

An Assistant's first synchronization runs before its workspace publishes
`READY`. Its guard validates the owning materializer Session, startup owner,
sandbox generation and recorded allocation while holding their database locks.
Synchronization on a ready workspace validates its published sandbox instead.
Both paths retain the operation, owner, revision and deadline checks.

For standalone provider-state writes, including registration, import
`issue_preparation_commit_guard` from `astrabox.runtime_preparation`.
Call it inside AstraBox's identity middleware, or
from an embedding application that has bound an identity verified by its identity
resolver. Never bind a user ID taken from request JSON. The factory requires an
explicit bound identity and Agent management permission or Assistant ownership;
machine identities also need `astrabox:write` or `astrabox:admin`. The default
local identity lookup is insufficient without a bound context. Issuance reads
the authoritative owner and revision, holds no locks after returning, creates no
preparation operation, and refuses an active synchronization. Validate again
inside the write transaction; authorization and operation absence are rechecked.

```python
from astrabox.persistence.provider import get_persistence
from astrabox.runtime_preparation import issue_preparation_commit_guard
from astrabox.seams import PreparationSubject

# Inside an authenticated provider handler; no remote call in this block.
guard = await issue_preparation_commit_guard(
    subject=PreparationSubject(kind="agent", id=agent_id),
)
async with get_persistence().get_engine().begin() as connection:
    await guard.validate(connection)
    # provider_write is a SQLAlchemy statement for the provider's own table.
    # Its constraints and operation journal remain the provider's responsibility.
    await connection.execute(provider_write)
```

Use the selected persistence engine's actual `AsyncConnection` and an active
root transaction at `READ COMMITTED` isolation. Other engines, autocommit and
savepoints are refused. Validate before locking provider-owned rows, use only one
subject per transaction, propagate refusals to roll back, and keep the transaction
short. Definition, RuntimeProfile, defaults and operation locks last until that
transaction commits or rolls back. An Agent must remain `ACTIVE`; a deleted
Assistant, stale owner/revision or superseded/expired callback operation is
refused with `PREPARATION_SUBJECT_CONFLICT`. Assistant revision validation uses
the effective inherited configuration, including the first insertion of defaults.
An Agent's `revision` is its authored `version`: changing its RuntimeProfile alone
does not advance that revision. Agent callbacks additionally check the recorded
prepared generation; this does not recompute an effective RuntimeProfile revision.
The RuntimeProfile lock prevents concurrent edits during the validated transaction,
but does not certify that an Agent's RuntimeProfile is unchanged since guard issuance
or since its callback configuration was captured. A disabled RuntimeProfile is
permitted: startup admission remains the startup path's responsibility. An
RuntimeProfile edit still changes an Assistant's effective revision, so a guard
issued before that edit is stale even if the subject retains its owner.
Standalone guards require no active operation even if a synchronization starts
after issuance. A successful validation is not a promise about a later transaction.

Keep remote calls outside database transactions. Record durable intent before
issuing a remote write, and use the remote service's supported idempotency and
fencing mechanisms where available. A separate provider database cannot share
AstraBox's local transaction, and the guard makes no guarantee for it. Neither
arrangement makes a remote effect
atomic with a database commit or guarantees exactly-once remote execution. An
ambiguous outcome must remain unresolved until `recover` can prove that the exact
old invocation and its delayed requests can no longer write; a journal entry or
lease expiry alone is not that proof. If you cannot establish that proof, return
`False` rather than allow a new uncertain write.

## Deliver Session materials

`async prepare_session(*, context: SessionPreparationContext) -> RuntimeMaterials`
runs before engine activation on cold start, claim, reattachment and sandbox
replacement. Its `principal_id` comes from the persisted Session owner. The context
includes the Session ID, its `subject` and `revision`, resolved configuration,
runtime generation, sandbox ID and workload identity. For an Assistant
conversation it runs after the Assistant's resources are synchronized. No Session
is supplied while an Agent's inventory or an Assistant's workspace is prepared
without a conversation.

`context.engine_initialized` reports whether activation will attach to an
existing native process. Such a callback can refresh files, external gateway
bindings and environment supported by the engine's native activation contract.
Consumers that read their environment only at process startup still require
`session_bound=True`. Binding or material preparation
failure blocks activation and discards the exact failed prepared claim; it never
silently creates a cold replacement.

Return `RuntimeFile(name, content: bytes)` entries and an environment mapping.
Use `context.file_path(name)` when an environment value names a returned file.
The platform writes each file into the workload's private home with ownership
and mode `0600`, outside the task workspace. File names must be safe basenames;
paths remain stable for that Session and sandbox. Material bytes and environment
values are excluded from their dataclass representations and public errors. A
Session-bound provider must return material.

An engine adapter declares `startup_environment_scope()`. `"process"` delivers
`EngineStartupContext.runtime_env` before native initialization; Claude Code
uses its SDK options. An adapter declaring
`supports_initialized_environment()` also accepts the mapping after native
initialization. Claude applies the native `apply_flag_settings` control with an
`env` overlay and verifies `get_settings` before releasing input, retaining the
same single-use prepared CLI process. Reattachment applies the same native
contract. The runner image must advertise both controls; an older image is
refused, never silently treated as updated. Hooks and tools started afterward
inherit the updated native environment. Already-running child processes and
startup-only consumers are not retroactively changed; user-bound MCP servers
retain the native reconnect barrier during activation.

`"session"` delivers
`EngineStartupContext.session_env` through a native Session context, without
changing a shared process environment. The Hermes image carries a pinned,
MIT-licensed extension to its typed `session.create` and `session.resume`
contracts for this mapping; its native context propagation delivers the values
to tool subprocesses. Each conversation receives its own mapping again on
reattachment or box replacement. Assistant workspace preparation receives none.
An image without this extension rejects the RPC parameter explicitly.

Adapters returning `None` refuse the environment. Platform-owned environment
names cannot be overwritten, and Hermes also refuses native identity and
credential names. An initialized process without a native environment activation
mechanism refuses new process-scoped environment. Platform preparation refusals block activation with
`RUNTIME_PREPARATION_FAILED`; callback exception text is not exposed. Native
Session environment validation blocks Hermes attachment with `AGENT_RUNTIME_ERROR`
and a message that contains no supplied environment values.

The Claude Agent SDK probes the CLI with `claude -v` before it starts it, without
the options environment. An image wrapper that gates the CLI on Session material
should pass `-v` and `--version` through unchanged.

## Prepare each engine write

`async prepare_turn(*, context: TurnPreparationContext) -> RuntimeMaterials` runs
immediately before each input is handed to the engine. The Session's reader
calls it: the one process that holds the Session's connection to its runtime,
so a Session's inputs are prepared one at a time, in the order they were
accepted. The reader reloads the Session first. `context.turn_id` and
`context.dispatch_attempt_id` both carry the input's id. An input handed over
again, because the runtime that received it was replaced before it admitted
the input, is prepared again with the same id, so the hook must be idempotent
for it. Return refreshed files only; the platform writes them to the same path
`prepare_session` used, so `context.file_path(name)` names the same file. A
non-empty environment fails the input. Provider-owned state may also be renewed
here through `context.sandbox_handle`.

The hook runs under a 30-second deadline. A timeout or an exception refuses the
input with `TURN_PREPARATION_FAILED`: it never reaches the engine, the input is
recorded as failed, and the Session's stream reports it as a
`data-input-failed` part. The platform calls the hook once for each handover
and does not call it again while the engine works on the input. Because the hook runs before every
input, reuse a credential that is still valid and return at once; call the
deployment's service only when the credential is close to expiry. Turn
preparation requires a sandbox backend that declares
`sandbox_is_profile_exclusive`.

## Require a user action

Raise `PreparationUserActionRequired(message=..., url=...)` from `prepare_session`
or `prepare_turn` when the user must act first. The host validates the message,
status and optional HTTP(S) URL; any other exception stays generic, because
provider text can carry secrets. Raised from `prepare_turn`, it refuses that
input with `TURN_PREPARATION_USER_ACTION_REQUIRED`: the input is recorded as
failed with the validated action, and the Session's stream reports it as a
`data-input-failed` part carrying `user_action`. Nothing is resent
automatically.

A user action raised while a conversation starts does not end it. The
conversation stays open (`READY`, with its runtime unavailable) and the
platform records the action. Before the platform starts the runtime again for
a message, it reads `read_user_actions`. While that reports the requirement
pending, the runtime is not started and no box is taken: the message fails with
`TURN_PREPARATION_USER_ACTION_REQUIRED` and the action, which the Session's
stream reports as a `data-input-failed` part carrying `user_action`. Once it
reports ready, the next message starts the runtime and is delivered after
`prepare_session` succeeds. An explicit recovery
(`POST /api/v1/sessions/{session_id}/recover`) is refused at once with
`TURN_PREPARATION_USER_ACTION_REQUIRED` (409) and the action while the
requirement is pending. An Agent conversation's box is released when its
startup stops, and the next start takes a box again. An Assistant's workspace
keeps running.

`read_user_actions(context=...)` returns `SessionUserActionState`, which the
console reads through `GET /api/v1/sessions/{session_id}/user-action`.
`perform_user_action(context=..., action_id=...)` runs an operation the user
chose from the state's `actions` through `POST` at the same path. The host
checks that the operation is currently offered; operations must be idempotent
for their current pending requirement across replicas. The context carries the
persisted Session ID, its authenticated owner and its `subject`. Reads require read
scope, operations write scope. Provider errors fail the request with
`SESSION_USER_ACTION_FAILED` rather than grant readiness, and the console blocks
sending while a prerequisite is unknown or pending. A status read is advisory:
`prepare_turn` remains the authority for every caller, including those that do
not use the console.

The HTTP view also carries platform-owned `applicable`. For an ended Session,
or one whose Agent is `DELETING` or `DELETED`, GET returns
`{"applicable": false, "ready": false, "user_action": null, "actions": []}`
without calling the provider. POST refuses with `409 SESSION_READ_ONLY` before
reading or performing a provider operation. Owner and API-scope checks still
apply. The console omits the prerequisite view and does not query it when it
already knows the conversation is read-only; late responses from its executable
state cannot restore an action. Executable conversations return
`applicable: true`, retaining the provider's readiness and failure semantics.

The console re-reads prerequisites when startup or runtime recovery changes
the Session binding, when the page regains focus or visibility, and when the
browser comes back online. Return reads use the console's shared throttling
and in-flight deduplication; there is no prerequisite polling timer. A stale
operation refused with `409 INVALID_REQUEST` triggers a status read, without
replaying the operation. These reads initiate no consent or registration.

```python
from astrabox.seams import SessionUserActionCommand, SessionUserActionState

pending = SessionUserActionState(
    ready=False,
    user_action={"status": "pending", "message": "Complete the requested consent.",
                 "url": "https://identity.example/consent"},
    actions=(SessionUserActionCommand("check-current-consent", "Check consent"),),
)
ready = SessionUserActionState(ready=True)
```

Do not dispatch input, initiate consent, re-register an Agent or launch background
writes from any of these callbacks. Return material from the deployment's existing
credential system. Cancellation must leave no outstanding provider writes;
acknowledged platform file delivery completes before the invocation unwinds.

## Validation scope

`tests/e2e/runtime_preparation_test.py` runs every callback on a real deployment
with a fixture provider and an image-baked gate in front of the real Claude CLI.
It is marked `deployment_shape`, so the live lanes exclude it; run it against a
deployment built with that fixture. Its token document is synthetic. This is
not acceptance of any particular credential service, image or sandbox backend.
