# Add an Agent program

An Agent program is the software that runs the agent loop, such as a coding
agent CLI or server. To run a new Agent program in AstraBox, connect its
supported protocol through an adapter and provide a sandbox image that contains
the program.

AstraBox runs Agent programs as 24/7 cloud Agents. The Agent program keeps its
own agent loop, tools, event names, permission modes, and conversation state.
AstraBox manages sandbox lifecycle, message custody, streaming, saved Session
records, and access through the API, web console, and channels.

## Adapter

The adapter connects the Agent program's supported protocol to AstraBox. It has
two parts:

- the **adapter class** declares what the program needs in a sandbox and starts
  or reconnects the program in a box AstraBox has prepared;
- the **runtime client** is one live connection to one running conversation.
  It hands the program inputs, reads its events, reports its state, and passes
  on answers and interruptions, through one contract that AstraBox uses for
  every Agent program.

The program's runtime owns its conversation. Whether it is working, which
inputs it has taken, where each response starts and ends, and which approvals
it waits on are what the runtime client reports; AstraBox records those
reports and never infers them.

The code interface is named `EngineAdapter`, and `engine_kind` is the stored
identifier that selects an installed adapter. These names belong to the
Python extension API; the product concept is an Agent program.

## Sandbox image

The sandbox image contains the Agent program, its operating-system packages,
its control service, and every dependency required before a conversation
starts. A prewarmed sandbox can be created before a Session claims it, so the
Agent program must already be installed in the image and resident services must
be running when the sandbox reports ready. Workspace contents and credentials
stay outside the image and are supplied through the AstraBox sandbox lifecycle.

## Adapter vs sandbox image

| Dimension | Adapter | Sandbox image |
| --- | --- | --- |
| Purpose | Connects the Agent program's protocol to AstraBox | Provides the environment in which the Agent program runs |
| Owns | Protocol requests, native events, conversation identity, the resume cursor, and reconnect behavior | Executable, system packages, accounts, startup command, and resident services |
| Changes when | The Agent program's protocol or supported capabilities change | Its runtime dependencies or operating-system requirements change |
| Loaded by | Python entry point `astrabox.providers.engine` | The RuntimeProfile's runtime image |
| Verified by | Adapter and runtime-client conformance suites, and live conversations | A real sandbox startup and conversation |

Build both parts for a new Agent program. An adapter without its image cannot
start the program; an image without its adapter cannot participate in the
AstraBox conversation lifecycle.

## Implement the adapter

### Start from the installed interfaces

Use the interfaces installed with the AstraBox version you support:

- `astrabox/core/service/orchestrator/engine/base.py` defines `EngineAdapter`,
  `EngineStartupContext`, `EngineStartupMaterialRequest`, and the optional
  protocols;
- `astrabox/core/service/orchestrator/engine/runtime.py` defines the runtime
  contract: `RuntimeClient`, `RuntimeInput`, `RuntimeCursor`, `RuntimeState`,
  the runtime events, and `initialize_runtime_client`;
- `astrabox/core/service/orchestrator/engine/capabilities.py` defines
  `EngineRuntimeCapabilities`, and `engine/extensions.py` defines
  `EngineExtensions`;
- `astrabox/core/service/orchestrator/engine/provisioning.py` defines
  `EngineSandboxRequest`;
- `astrabox/core/service/orchestrator/runtime/models.py` defines
  `SessionRuntime`, the value `activate_runtime()` returns;
- `astrabox/testing/engine_conformance.py` defines the shared conformance
  checks;
- the Agent program's official protocol types and documentation define the
  meaning of its events and options.

The five built-in runtime clients are worked examples. Each module's docstring
explains its cursor, its response boundaries, and the vendor record that makes
an input admitted: `engine/claude_runtime_client.py`,
`engine/codex_runtime_client.py`, `engine/deepseek_harness_runtime_client.py`,
`engine/hermes_runtime_client.py`, and `engine/pi_runtime_client.py`.

Keep the Agent program's names and values unchanged. For example, if its API
calls a setting `approval_policy`, expose and return `approval_policy`; do not
translate it into an AstraBox-specific synonym.

### Declare the runtime

Return `EngineRuntimeCapabilities` from the adapter's `capabilities` property.
The declaration includes:

- a stable `engine_kind`;
- `supported_session_kinds`, the AstraBox Session kinds it drives
  (`agent_chat`, `assistant_chat`);
- the engine's workload facts (`EngineWorkloadDeclaration`: its configuration
  directory name, the environment variable naming it, and the commands its own
  path needs in the image);
- its conversation placement (`per_conversation_account` when the engine's
  process runs as the conversation's own account, `box_account` while the
  integration still drives one box-scoped service);
- `default_runtime_image`, if the RuntimeProfile may omit one;
- `permission_modes` and `permission_mode_defaults`, in the engine's own
  vocabulary;
- `extensions` (`EngineExtensions`): the author MCP transports it accepts, where
  it discovers Skills, its native package kinds, its native configuration
  documents (object blocks whose `protected_keys` name the top-level fields the
  platform manages), and whether it sends telemetry to a RuntimeProfile's tracing
  endpoint;
- `session_log` when AstraBox must preserve opaque conversation files outside
  a replaceable sandbox;
- `requires_process_sessions` when, in a conversation's own box, the engine
  runs as one of the box's long-lived processes driven over its stdin and
  stdout (`SandboxHandle.processes`); a RuntimeProfile that pairs the engine
  with conversation tenancy is then refused on a sandbox backend that does not
  declare `supports_process_sessions`.

Registration validates the declaration. When an Agent is saved, any configured
value the adapter's `extensions` do not accept is rejected instead of being
stored as an inert setting. Tracing is checked when saving the RuntimeProfile:
enabled tracing requires an adapter that declares support. A valid disabled
tracing configuration can remain stored when switching to an adapter without
that capability.

Declare the runtime client class as `engine_client_type`. Registration checks
that the class serves the whole runtime contract before the engine appears in
any product catalog.

### Place conversations in a box

Sandbox tenancy is not part of the declaration. How many conversations a box
carries is a RuntimeProfile choice, and the runtime identity it implies —
account naming, home placement, the account-assembly commands — is composed by
the platform per `(sandbox_tenancy, session_kind)` from one set of rules for
every engine. Shared tenancy requires the sandbox's isolated-session capability
and the adapter's `per_conversation_account` placement. Implement
`shared_conversation_service_launch()` when the engine needs a service in that
isolated session. It must start the image-installed service in the background;
the platform waits for its declared port before activation.

For verified text publication, use `install_verified_text_script` from
`runtime/sandbox_script_writer.py` with the context's `runtime_identity` and a
runtime-visible path. The platform maps workspace paths to their backing
location before writing, for both cold and prepared placements. Do not convert
workspace paths in the adapter. A `None` identity explicitly addresses the box
filesystem; an incomplete supplied identity is rejected before any write.

## Implement the runtime client

AstraBox runs one reader per Session: exactly one process holds the Session's
reader lease and the only connection to its runtime. That process hands the
runtime inputs, answers and interruptions, reads its events, and records them.
Any replica may accept a person's request; a database notice wakes the reader,
which passes it on over its connection. Write the client for one reader at a
time; a new reader opens a new client.

| Member | What the platform expects |
| --- | --- |
| `engine_kind` | The adapter's `engine_kind`. |
| `is_live` (property) | Whether the connection to the runtime is open. |
| `engine_session_key` (property) | The vendor's conversation id. A replacement runtime resumes this conversation. |
| `submit(input)` | Hand the runtime one `RuntimeInput`; return once the runtime has received it. Idempotent by `input_id`. Raise `RuntimeInputRefused(code, message, status_code=)` when the runtime declines it. |
| `events(after=cursor)` | An async generator of runtime events after the cursor (from the start for `None`), for as long as the connection lives. Raise `RuntimeCursorExpired` when the cursor cannot be resumed. |
| `state()` | The runtime's own account of itself now (`RuntimeState`). |
| `answer(interaction_id, response)` | Pass a person's answer to an open interaction. A redelivery may return normally when the runtime retains proof it accepted that answer. Otherwise raise `InteractionNotOpen` when it holds no such open interaction. |
| `interrupt(cancel_queued=False)` | Stop the response in progress; with `cancel_queued`, also drop the inputs queued behind it. |
| `committed(cursor)` | AstraBox durably recorded every event through the cursor. A runtime that keeps history only until its reader has it may release what precedes the cursor. |
| `capabilities()` | The connected runtime's `EngineCapabilityManifest`: permission modes, input content types, and controls. |
| `close()` | Close the connection. The runtime itself keeps running. |

Two optional members are used when present: `set_permission_mode(mode)`, which
the reader calls with the Session's stored mode when it connects and whenever
the owner changes it, and `stop_child_run(control_ref)`, which stops one
sub-agent by the control reference your child facts carry.

An engine declaring `supports_server_info` may return `runtime_versions` in
`get_server_info()`: `runtime_generation` is its observed incarnation id, and
`components` lists `{component, version, source, reason}` records. Component
names and version strings belong to the adapter. A known version has a
nonempty string; an unknown one has `version: null` and a nonempty reason.
Every component names its observation source. Deployment labels and host
package versions are not observations of an in-box runtime.

Startup, recovery and reader connection save this observation with its sandbox
id and read time. The admin Session list and detail read that saved snapshot;
they neither query nor wake compute. The snapshot describes the last connected
instance, not continuous health or a version audit log. Its generation and
`matches_bound_sandbox` distinguish it from another instance; absent information
is explicitly unknown. Engines without this optional observation remain usable.
The Claude runner records its loaded source SHA256, installed SDK distribution
and the output of its selected native CLI's `--version` once per runner boot.

### Report the runtime's events

The events are a small closed set. Each keeps the vendor's own words:

| Event | Report it when |
| --- | --- |
| `InputAdmitted(input_id)` | The runtime durably holds the input: a restart of the runtime's process would still answer it. |
| `ResponseStarted(response_id, origin, input_ids)` | The runtime began a response. `origin` is `input` when it answers submitted inputs, or `engine` when the runtime started it on its own. |
| `ResponseOutput(response_id, frame)` | One browser part: an AI SDK UI message chunk translated from the vendor's output. `response_id` is `None` only for output that belongs to no response, such as a child run's status. |
| `InteractionOpened(response_id, interaction_id, contract)` | A response is paused on a person. |
| `InteractionClosed(interaction_id, reason)` | The runtime no longer waits on the interaction: `answered`, `withdrawn`, or `expired`. |
| `ResponseEnded(response_id, outcome, native_reason, usage, error)` | The runtime ended the response. `outcome` is your declared reading of the vendor's ending (`completed`, `failed`, or `cancelled`); `native_reason` is the vendor's own word for it, unchanged. |
| `StatusChanged(status)` | The runtime's own status changed: `working`, `waiting`, or `idle`. |
| `RuntimeDiagnostic(event_type, raw)` | Raw vendor evidence for operators; it never reaches a browser. |

Read each fact from the vendor's own signal. A response's id must be stable for
the life of the response, including across a new reader: derive it from the
vendor's ids. Report `working` for background work the runtime started itself,
and `waiting` while a response is paused on a person.

### Admit inputs

`submit()` must be idempotent by `input_id`. Where the vendor deduplicates by a
message id, use the input id (or a value derived from it) as that id; where it
does not, check the vendor's history, queue, or your own submissions before
sending the input again. A runtime that is working may receive another input
without having admitted it yet. Its adapter must keep those boundaries
distinct; how the vendor consumes and answers the input is engine-specific.

The Claude adapter hands inputs in delivery order to the same persistent
`ClaudeSDKClient.query()`, including while a response is running. Claude's
native queue decides when to consume them and which response answers them;
the adapter does not wait for `ResultMessage` before delivering another input.
A delivery receipt therefore does not mean Claude has consumed that input.
Multiple inputs consumed before the same Result belong to the same response.
When another input joins, the existing message's metadata carries the complete
`input_ids` through the same output log used by live readers and history.
Other engines report their own admission and response boundaries through the
same contract.

Recovery coverage includes inputs handed to the native queue but not consumed,
completed earlier responses, and the currently active response with all inputs
the engine has consumed. Compare native consumption and Result boundaries with
the platform's input and response records; do not require one response per input.

Report `InputAdmitted` only at the vendor record that survives a restart of the
runtime's process, such as the user message written to the vendor's transcript.
The answer to the submit request is not admission when the vendor can still
discard the input. Until an input is admitted, AstraBox keeps it and hands it to
a replacement runtime if this one is lost; after admission, AstraBox never sends
it again. `RuntimeState.admitted_input_ids` lets a new reader close custody of
inputs whose `InputAdmitted` it missed.

### Make the cursor deterministic

Every event carries the `RuntimeCursor` to resume after it. `generation` names
the runtime incarnation the position belongs to (a runner process, a backend
epoch, a vendor session); `position` is your own resume state, which AstraBox
stores as JSON and never interprets. AstraBox records the cursor together with
the parts it has relayed, and a reader that takes over resumes from it.

Reading `events(after=cursor)` again must yield the same events after that
cursor, in the same order. The reader numbers the browser parts it records, so
a follower that saw a part live and later reads it from the record sees it
once. Base the translation on what the vendor retains (its journal, its log,
or its history API) and put any translation state that record cannot rebuild
into the position.

Raise `RuntimeCursorExpired` for a cursor from another generation or for a
position the vendor no longer retains. The reader then takes back the inputs
the runtime received but never admitted, asks `state()` for the runtime's own
account (open responses, pending and closed interactions, queued and admitted
inputs, and the cursor to continue from), and reconciles: a response the runtime no longer
reports ends as failed (`runtime_lost`), and an interaction it no longer waits
on closes with the runtime's retained closure reason, or as expired when it has
no closure receipt. Adapters report an accepted answer through a replayable
`InteractionClosed(..., "answered")` event and retain its receipt in
`RuntimeState.closed_interactions` for reconciliation. An answer saved in the
platform database does not establish acceptance by the runtime.

### Shape a response's browser parts

A response's parts start with a `start` part whose `messageId` is the response
id and whose `messageMetadata` carries `response_id`, `input_ids`, and `origin`.
They end with a `finish` part; a response that fails sends an `error` part
before its `finish`.

An adapter may withdraw an unconfirmed text or reasoning preview without
removing completed steps. Put its chunk id in
`providerMetadata.astrabox.part_id` on the opening chunk, then publish a
`message-metadata` chunk with the cumulative `retracted_part_ids` for that
response. Live messages and stored history omit those parts before display
and process-block grouping. The output log retains the original chunks and
the withdrawal for deterministic replay. The adapter, not the platform,
decides whether native output is provisional or has been superseded.

When a response pauses on an interaction, report `InteractionOpened` with the
contract, then the parts that close the browser's message segment: a
`tool-approval-request` part for a tool approval
(`build_tool_approval_request_frame`), a `data-interaction` part whose `data` is
the interaction record, and a `finish` part with `finishReason: "tool-calls"`.
Report `StatusChanged("waiting")`. Before any further part of the response,
its continued output or another interaction request alike, open the same
message again with a `start` part under the same `messageId`. Build the
contract with the helpers in `engine/interaction_contract.py` and check it with
`validate_interaction_contract`: it declares how a person can answer (a form, a
decision among declared options, or a tool approval) and carries the vendor's
identity and meaning unchanged.

### Report a tool call

A tool call reaches the browser as three parts on the UI stream, and the
console builds one card from them:

```python
from astrabox.core.service.orchestrator.engine.runtime import ResponseOutput

yield ResponseOutput(cursor, response_id, {"type": "tool-input-start",
                     "toolCallId": call_id, "toolName": name})
yield ResponseOutput(cursor, response_id, {"type": "tool-input-available",
                     "toolCallId": call_id, "toolName": name, "input": arguments})
yield ResponseOutput(cursor, response_id, {"type": "tool-output-available",
                     "toolCallId": call_id, "output": result})
```

`toolName` and `input` are the program's own, carried verbatim: a console that
had to recognise a tool by name would work for one Agent program and show
nothing for the next.

`ResponseOutput` adds `dynamic: True` to tool input parts through the shared
`public_ui_frame()` helper, so adapters do not maintain a second copy of this
presentation rule. The browser's AI SDK decides the part's type from it — set,
it builds a `dynamic-tool` part; omitted, it builds a typed `tool-<name>` part,
which every console reader ignores. The output part does not carry it: the SDK
matches that one by `toolCallId` to the part the input parts already created.

For completed native file changes, use `file_changes_frame` from
`engine/file_changes.py` to emit `data-file-changes`. The adapter supplies the
tool-call identity, changed paths and native content pairs, unified patches,
structured hunks or isolated excerpts. Pass `diff=None` when the supplier
confirms a write but provides no comparison. Do not construct an applied diff
from requested tool inputs. AstraBox records the part with the response that
made it, and the Diff panel reads it from there.

### Report child runs

If the Agent program delegates work to child agents, report each child's
lifecycle and messages as `data-subagent` parts whose data follows
`engine/child_runs.py`. Choose one stable native reference per child; AstraBox
mints the public child-run ID from it and keeps the native reference private.
Report child parts with `response_id=None` when they belong to no response.

Optional means optional: a program without child agents remains a valid Agent
program. Do not manufacture children, permission modes, or background execution
to satisfy a common interface. Select live acceptance cases by the capabilities
actually declared, and report an unsupported capability as not applicable, not
as a passing test. A declared capability must pass its user-visible path,
including streaming and saved-history reads where that capability supports them.

Keep failure scope native too. A failed child or a failed inspection request is
not evidence that the parent program stopped. End a response only on the
program's own signal; keep local request failures as diagnostics without
inventing a successful result.

### Connect and reconnect

Use the protocol supported by the Agent program, such as its HTTP API,
bidirectional RPC interface, SDK, or documented headless process protocol. The
adapter owns that protocol's request and event vocabulary.

Implement `activate_runtime()` against the platform-prepared
`EngineStartupContext`. On a new Session it starts or connects the Agent
program. When `context.attach_mode` is present, it restores that Session's
exact native conversation using the supplied resume key and material; AstraBox
has already re-adopted the sandbox and refreshed its workspace and credentials.
The supplier may recreate its process and in-memory session object as part of
that restore, but the resulting client must expose the same native
conversation key. Never bind a saved Session to a new or empty supplier
conversation.

Before returning, pass the client to `initialize_runtime_client(client,
engine_kind=..., resume_session_key=context.resume_session_key)`. It checks that
the client is live, resumed the requested conversation, and declares the
permission modes the adapter registered, and returns the capability manifest.
Return a `SessionRuntime` whose `engine_client` is the client and whose
`engine_manifest` is that manifest.

### Declare sandbox requirements

Return an `EngineSandboxRequest` from `sandbox_request()`. The request supplies
the image entry point, model-credential destination, working-directory
variable, environment values, published ports, and an optional readiness port.

Use `startup_material_request()` to return an `EngineStartupMaterialRequest`
for platform transcript storage, native runtime-state storage, sandbox-death
notifications, or named platform secrets. AstraBox resolves the secrets and
scoped callback credentials, then supplies them through `EngineStartupContext`.
The adapter consumes this material rather than issuing platform credentials.

AstraBox alone chooses whether to claim, create, reconnect, or replace a
sandbox. The platform applies the selected backend, workspace and runtime
identity, model and MCP credentials, network policy, and lifecycle tracking,
then passes the prepared context to `activate_runtime()`. An adapter declares
what its Agent program needs and performs the vendor handshake; it does not
call the platform provisioning flow or make sandbox-allocation decisions.

## Build the sandbox image

### Runtime and operating system

Choose the operating system and architecture supported by the Agent program.
Install the Agent program at a pinned release and use its documented unattended
interface. Do not depend on software that is absent from the image.

### Tools in the image

Install every command declared by the adapter's runtime profile. Keep optional
developer tools out of the required-command list; a missing required command
causes startup to fail before a Session accepts input.

### Working directory

The runtime profile defines the user home, workspace, source directory, file
root, cache, and temporary directory. Use those resolved paths instead of
assuming `/root`, `/home`, or a fixed UID.

### Installing extra software

Install system packages, language runtimes, the Agent program, and supervised
services in the image build. Do not install a system capability when a Session
starts: a prepared sandbox already exists before per-Session input is available.

### Resources and timeouts

Available CPU, memory, disk, and execution timeouts depend on the sandbox
backend and deployment configuration. Validate the Agent program's minimum
requirements on the target backend. A readiness service must fail startup
clearly when the program cannot serve requests.

### File persistence

Local files remain available while the same sandbox exists. An optional
persistent workspace preserves working files across sandbox replacement.
Native conversation state is stored separately in AstraBox's database and does
not require that workspace volume.

For a native JSONL conversation log, declare `session_log`. AstraBox mirrors
its JSON entries and restores them into a replacement sandbox before reconnect.
It preserves the entries and their order without interpreting vendor semantics;
JSONL whitespace need not be byte-identical. An engine with another native state
format can request `runtime_state_store` and supply its save/restore integration.
Declare `runtime_state_scope="session"` when that native profile belongs to one
Session's isolated account; the default `"workspace"` binds it to the Agent or
Assistant workspace. The platform issues the owner and capability. Engines
forward that owner unchanged, and publish references only after required native
bytes reach database custody. DSH extends its supplier's local attachment
backend for this acknowledgement, retaining native normalization, immutable
publication, and verified reads. Its restore barrier runs before the Session
opens on replacement compute.

An Assistant profile's startup records its native-state release obligation
before activation. A failed activation must stop native writers and confirm
the store and profile save, or raise `RuntimeStateSaveRequired` with the exact
sandbox and runtime identity. The platform retains that allocation for
recovery. Wake completes the save barrier before releasing its compute; a
periodic mirror or a saved profile archive alone is not that confirmation.

Hermes also recovers a same-owner local SessionDB left before its first native
snapshot was published. Cold bootstrap preserves a complete supplier backup,
profile archive and previous receipt in the private profile home. It publishes
those immutable bytes through the existing snapshot API with the current
materializer, allocation and generation validated in the save transaction.
Foreign receipts and superseded generations are refused. A partial publication
resends the same backed-up bytes through the snapshot predecessor check; the
restore receipt changes only after both archives are acknowledged.

### Execution user and environment variables

Create every required account and writable directory in the image. The runtime
may run as a non-root user, and the resolved workspace must be writable before
the program reports ready. Credentials are provided through the provisioning
interface; never bake them into the image or print them in logs.

## Register the adapter

Publish the adapter class through the package's entry points:

```toml
[project.entry-points."astrabox.providers.engine"]
example = "example_package.adapter:ExampleAdapter"
```

The entry-point name must match `ExampleAdapter.engine_kind`. The class may set
`seams_api_version` to the AstraBox extension-interface version it was built
against (currently `7`); an incompatible pinned version causes startup to fail
with the mismatch. Duplicate names, incomplete adapters, and runtime clients
that do not serve the whole runtime contract also fail during startup.

## Verify the integration

Complete these checks in order:

1. Build event-translation tests from recordings produced by the supported
   Agent program release, never from hand-written payloads.
2. Check cursor determinism: reading the recorded events again from any cursor
   yields the same events after it.
3. Run `EngineAdapterContractSuite` and `RuntimeClientContractSuite` from
   `astrabox.testing.engine_conformance`. Both are structural; driving a real
   conversation belongs to your end-to-end tests:

   ```python
   from astrabox.testing.engine_conformance import (
       EngineAdapterContractSuite,
       RuntimeClientContractSuite,
   )

   class TestExampleAdapter(EngineAdapterContractSuite):
       def make_adapter(self):
           return ExampleAdapter()

   class TestExampleRuntimeClient(RuntimeClientContractSuite):
       def client_type(self):
           return ExampleRuntimeClient
   ```

4. Start the image through a real OpenSandbox lifecycle service and complete
   two inputs in the same conversation, including one sent while the program is
   still working.
5. Cover interruption and every optional capability the adapter advertises,
   including an approval answered after the reader reconnects.
6. Restart the AstraBox service while a response streams and confirm that the
   response continues from the recorded cursor without repeated or missing
   parts.
7. Remove the sandbox while a response runs and confirm that the response ends
   as failed and the next message resumes the same native conversation on a new
   sandbox.

Update the recordings, adapter, and image together whenever the pinned Agent
program release changes its protocol or runtime requirements.

## Related documents

- [Architecture](./architecture.md) — AstraBox responsibilities and extension interfaces
- [OpenSandbox provider](./providers/opensandbox.md) — sandbox lifecycle and
  runtime behavior
- [RuntimeProfiles](./runtime-profiles.md) — images, resources, and network access
