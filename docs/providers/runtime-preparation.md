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

One provider answers every point at which a deployment prepares an Agent or a
Session. Each method has a default, so implement only the points you need:

| Method | When the platform calls it |
| --- | --- |
| `plan` | Whenever it plans an Agent's runtime; synchronous, no I/O |
| `synchronize`, `recover` | Once per prepared box, before it becomes waiting inventory |
| `prepare_session` | Before the engine initializes: cold start, claim, reattachment, sandbox replacement |
| `prepare_turn` | Before every engine write, under the turn guard |
| `read_user_actions`, `perform_user_action` | When the Session's owner reads or acts on its prerequisites |

The platform owns ordering, placement, fencing, file delivery and failure
handling. The provider owns external identities, declaration formats, credential
policy and prerequisites. It never starts a consent flow by itself, registers an
Agent per Session, or dispatches work.

## Declare when a Session is required

`plan(*, config) -> RuntimePreparationPlan` is synchronous and performs no I/O.
Its detached resolved Agent configuration contains no requesting Session.
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
prewarming and protected Vault credential substitution retain their existing path.
Session-bound resource prewarming also works without Credential Vault; it leaves
model and environment credentials out of unclaimed inventory and resolves them
at claim. It does not implement unavailable Vault capabilities.

This controls initialization performed by the engine adapter. Required system
services and wrappers must already be baked into the selected image. The provider
cannot retrofit a wrapper into a box or defer an image's own entrypoint.

## Synchronize Agent resources

`async synchronize(*, context: AgentPreparationContext) -> None` runs after the
actual Plugin and Skills caches exist, before waiting inventory is published.
It runs once for each prepared box, so an Agent without prepared capacity is
never synchronized; claim, cold start, reattachment and input refresh do not
call it. Per-conversation declarations belong in `prepare_turn`.

| Context | Meaning |
| --- | --- |
| `operation.agent_id` | Complete stable Agent ID; the external identity key |
| `operation.operation_id` | Durable invocation identity for idempotency and recovery |
| `creator_user_id` | Persisted Agent creator |
| `config_version`, `preparation_id`, `runtime_generation` | Authored version, pool lifetime and resolved recipe |
| `config` | Detached resolved Agent configuration |
| `sandbox` | Standard `SandboxHandle`, including its file interface |
| `packages` | Ordered source declarations and actual cache checkout paths |
| `skills_path` | Actual prepared Skills directory, or `None` |

Read actual prepared files and merge the deployment's declarations. Keep the
external identity keyed by Agent ID across versions and pool members. Skip
unchanged external writes in the provider. Do not log trusted configuration.

The existing Agent repository's compare-and-set serializes external updates
across replicas, members and generations. The host checks the version, refresh
revision, pool lifetime and generations before and after synchronization and
before publishing inventory. A superseded result is refused.

An exception, timeout, cancellation or host loss leaves an unresolved operation.
Its deadline permits `async recover(*, operation: AgentPreparationOperation)`;
it does not permit a new uncertain write. Return `True` only after that exact
operation can never write again, including delayed requests. Recovery must be
idempotent and must not fence newer operations. The default returns `False`.
A successful `synchronize` must leave no outstanding external writes. An expired
lease or absent remote result alone does not prove quiescence.

## Deliver Session materials

`async prepare_session(*, context: SessionPreparationContext) -> RuntimeMaterials`
runs before engine activation on cold start, claim, reattachment and sandbox
replacement. Its `principal_id` comes from the persisted Session owner. The context
includes Session/Agent IDs, resolved configuration, runtime generation, sandbox ID
and workload identity. No fake Session is supplied during Agent prewarming.

Return `RuntimeFile(name, content: bytes)` entries and an environment mapping.
Use `context.file_path(name)` when an environment value names a returned file.
The platform writes each file into the workload's private home with ownership
and mode `0600`, outside the task workspace. File names must be safe basenames;
paths remain stable for that Session and sandbox. Material bytes and environment
values are excluded from their dataclass representations and public errors. A
Session-bound provider must return material.

The environment stays fixed for the life of the runtime it starts. An engine
adapter must declare `supports_startup_environment()` and deliver
`EngineStartupContext.runtime_env` before native initialization. The built-in
Claude Code adapter does so through its SDK options; other built-in adapters
refuse Session environment rather than ignore it. Platform-owned environment
names cannot be overwritten. A refusal blocks activation with
`RUNTIME_PREPARATION_FAILED`; the exception text is not exposed.

The Claude Agent SDK probes the CLI with `claude -v` before it starts it, without
the options environment. An image wrapper that gates the CLI on Session material
should pass `-v` and `--version` through unchanged.

## Prepare each engine write

`async prepare_turn(*, context: TurnPreparationContext) -> RuntimeMaterials` runs
immediately before every engine write, after the platform reloads the Session,
confirms that the turn still holds its slot and claims a durable per-Session
guard. It must be idempotent for `context.dispatch_attempt_id`, which identifies
one write attempt including reconnect retries. Return refreshed files only; the
platform writes them to the same path `prepare_session` used, so
`context.file_path(name)` names the same file. A non-empty environment fails the
write. Provider-owned state may also be renewed here through
`context.sandbox_handle`.

The hook runs under a host-clock deadline. A timeout, exception or self-cancel
refuses the write with `TURN_PREPARATION_FAILED` and the input never reaches the
engine. Set `turn_validity_seconds` when prepared state expires: the host calls
`prepare_turn` again for the same attempt before the validity lapses while the
write is live, and a failed refresh quarantines the guard. Turn preparation
requires a sandbox backend that declares `sandbox_is_profile_exclusive` and
implements `owns_sandbox`, so that the live handle is proven to belong to the
persisted binding.

## Require a user action

Raise `PreparationUserActionRequired(message=..., url=...)` from `prepare_session`
or `prepare_turn` when the user must act first. The host validates the message,
status and optional HTTP(S) URL and returns a non-retryable
`TURN_PREPARATION_USER_ACTION_REQUIRED` (409) with that payload. Any other
exception stays generic, because provider text can carry secrets. The console
keeps the refused input and its images for an explicit retry; nothing is
resent automatically.

`read_user_actions(context=...)` returns `SessionUserActionState`, which the
console reads through `GET /api/v1/sessions/{session_id}/user-action`.
`perform_user_action(context=..., action_id=...)` runs an operation the user
chose from the state's `actions` through `POST` at the same path. The host
checks that the operation is currently offered; operations must be idempotent
for their current pending requirement across replicas. The context carries the
persisted Session ID, its authenticated owner and Agent ID. Reads require read
scope, operations write scope. Provider errors fail the request with
`SESSION_USER_ACTION_FAILED` rather than grant readiness, and the console blocks
sending while a prerequisite is unknown or pending. A status read is advisory:
`prepare_turn` remains the authority for every caller, including those that do
not use the console.

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
