# Add an Agent program

An Agent program is the software that runs the agent loop, such as a coding
agent CLI or server. To run a new Agent program in AstraBox, connect its
supported protocol through an adapter and provide a sandbox image that contains
the program.

AstraBox runs Agent programs as 24/7 cloud Agents. The Agent program keeps its
own agent loop, tools, event names, permission modes, and conversation format.
AstraBox manages sandbox lifecycle, message delivery, streaming, saved Session
state, and access through the API, web console, and channels.

## Adapter

The adapter connects the Agent program's supported protocol to AstraBox. It
starts or reconnects the program, delivers input, streams output, stops active
work, and preserves the program's native conversation identity.

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
| Owns | Protocol requests, native events, conversation identity, and reconnect behavior | Executable, system packages, accounts, startup command, and resident services |
| Changes when | The Agent program's protocol or supported capabilities change | Its runtime dependencies or operating-system requirements change |
| Loaded by | Python entry point `astrabox.providers.engine` | The Environment's runtime image |
| Verified by | Adapter and client conformance suites | A real sandbox startup and conversation |

Build both parts for a new Agent program. An adapter without its image cannot
start the program; an image without its adapter cannot participate in the
AstraBox conversation lifecycle.

## Implement the adapter

### Start from the installed interfaces

Use the interfaces installed with the AstraBox version you support:

- `astrabox/core/service/orchestrator/engine/base.py` defines
  `EngineAdapter`, `EngineClient`, and the optional protocols;
- `astrabox/core/service/orchestrator/engine/capabilities.py` defines
  `EngineRuntimeCapabilities`;
- `astrabox/core/service/orchestrator/engine/provisioning.py` defines the
  sandbox request and provisioning result;
- `astrabox/testing/engine_conformance.py` defines the shared conformance checks;
- the Agent program's official protocol types and documentation define the
  meaning of its events and options.

Keep the Agent program's names and values unchanged. For example, if its API
calls a setting `approval_policy`, expose and return `approval_policy`; do not
translate it into an AstraBox-specific synonym.

### Declare the runtime

Return `EngineRuntimeCapabilities` from the adapter. The declaration includes:

- a stable `engine_kind`;
- supported AstraBox Session kinds;
- the engine's workload facts (`EngineWorkloadDeclaration`: its configuration
  directory name, the environment variable naming it, and the commands its own
  path needs in the image);
- its conversation placement (`per_conversation_account` when the engine's
  process runs as the conversation's own account, `box_account` while the
  integration still drives one box-scoped service);
- the default runtime image, if the Environment may omit one;
- permission modes and defaults exposed by the engine;
- an `engine_options_schema` of native JSON object blocks, with labels and help
  identifying each native target and override rules; `protected_keys` names
  top-level fields actually managed by the platform, not a vendor-option whitelist;
- `session_log` when AstraBox must preserve opaque conversation files outside
  a replaceable sandbox;
- `configuration_inputs`, the optional AstraBox configuration the adapter
  actually consumes: MCP servers, Skills, Plugins, or tracing.

Registration validates the declaration. When an Agent is saved, any configured
value outside the adapter's `configuration_inputs` is rejected instead of being
stored as an inert setting.
Tracing is checked when saving the Environment: enabled tracing requires an
adapter that declares support. A valid disabled tracing configuration can remain
stored when switching to an adapter without that capability.

### Implement the client

Sandbox tenancy is not part of the declaration. How many conversations a box
carries is an Environment choice, and the runtime identity it implies —
account naming, home placement, the account-assembly commands — is composed by
the platform per `(sandbox_tenancy, session_kind)` from one set of rules for
every engine. Shared tenancy requires the sandbox's isolated-session capability
and the adapter's `per_conversation_account` placement. Implement
`shared_conversation_service_launch()` when the engine needs a service in that
isolated session. It must start the image-installed service in the background;
the platform waits for its declared port before activation.

## 2. Translate engine events

`EngineClient` represents one native conversation. It must:

1. use `bind_conversation()` to bind an AstraBox Session to the exact native
   conversation identifier;
2. use `deliver()` to accept each durable input idempotently;
3. emit `data-input-consumed` for the exact input before its response frames;
4. use `iter_turn_events()` to stream output until the Agent program finishes,
   requests input, or the transport detaches;
5. cancel or interrupt active work;
6. report the capabilities of the connected program;
7. close its own connections and processes.

Classify native output once, inside the adapter, as public UI output, a control
fact, a terminal result, or a private diagnostic. Vendor event names,
identifiers, finish reasons, and payload meanings remain adapter-owned; core
orchestration must not infer them.

If the Agent program supports approvals, questions, child-run control,
permission modes, server information, live-turn reconnect, or transcript
recovery, implement the corresponding optional protocol from `base.py`. Do not
advertise an optional capability until the connected client implements it.

Optional means optional: a program without child agents remains a valid Agent
program. Do not manufacture children, permission modes, or background execution
to satisfy a common interface. Select live acceptance cases by the capabilities
actually declared, and report an unsupported capability as not applicable, not
as a passing test. A declared capability must pass its user-visible path,
including streaming and saved-history reads where that capability supports them.

Keep failure scope native too. A failed child or a failed inspection request is
not evidence that the parent program stopped. End the parent output stream only
on the program's lifecycle signal or an actual transport failure; retain local
request failures as diagnostics without inventing a successful result.

### Report a tool call

A tool call reaches the browser as three frames on the UI stream, and the
console builds one card from them:

```python
from astrabox.core.service.orchestrator.engine.emissions import PublicUIFrame

yield PublicUIFrame({"type": "tool-input-start", "toolCallId": call_id,
                     "toolName": name})
yield PublicUIFrame({"type": "tool-input-available", "toolCallId": call_id,
                     "toolName": name, "input": arguments})
yield PublicUIFrame({"type": "tool-output-available", "toolCallId": call_id,
                     "output": result})
```

`toolName` and `input` are the program's own, carried verbatim: a console that
had to recognise a tool by name would work for one Agent program and show
nothing for the next.

`PublicUIFrame` adds `dynamic: True` to input frames through the shared
`public_ui_frame()` helper; dictionary-producing translators use the same
helper. Adapters do not maintain a second copy of this presentation rule.
The browser's AI SDK decides
the part's type from it — set, it builds a `dynamic-tool` part; omitted, it
builds a typed `tool-<name>` part, which every console reader ignores. The
output frame does not carry it: the SDK matches that one by `toolCallId` to the
part the input frames already created.

Omitting it fails silently. The turn streams, the text renders, and nothing
errors; only the tool card, the diff panel and the approval-to-card binding go
missing, because the part they read was never built. The common boundary owns
the flag, while real tool-card acceptance checks live input/result matching,
visible completion, an ordinary subsequent answer, and saved-history reload.

For completed native file changes, use `engine/file_changes.py` to emit
`data-file-changes`. The adapter supplies the tool-call identity, changed paths
and native content pairs, unified patches, structured hunks or isolated
excerpts. Pass `diff=None` when the supplier confirms a write but provides no
comparison. Do not construct an applied diff from requested tool inputs.
The existing public data lane persists these results and replays them as
`ui_data`; no new journal or filesystem read is needed for cold display.

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
that restore, but the resulting client must expose the same native conversation
key. Implement
`EngineLiveTurnReconnect` only when the program can continue an unfinished
output stream. Otherwise, settle the interrupted turn explicitly and resume
the same native conversation for the next input; never silently bind a saved
Session to a new or empty supplier conversation.

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
against; an incompatible pinned version causes startup to fail with the mismatch.
Duplicate names and incomplete adapters also fail during startup.

## Verify the integration

Complete these checks in order:

1. Build event-translation tests from recordings produced by the supported
   Agent program release.
2. Cover input delivery, continuation, streaming, cancellation, reconnect, and
   every optional capability the adapter advertises.
3. Run `EngineAdapterContractSuite` and `EngineClientContractSuite` from
   `astrabox.testing.engine_conformance`.
4. Start the image through a real OpenSandbox lifecycle service and complete
   two inputs in the same conversation.
5. Restart the AstraBox service and confirm that the same native conversation
   continues or that an unfinished turn receives a clear terminal result.

Update the recordings, adapter, and image together whenever the pinned Agent
program release changes its protocol or runtime requirements.

## Related documents

- [Architecture](./architecture.md) — AstraBox responsibilities and extension interfaces
- [OpenSandbox provider](./providers/opensandbox.md) — sandbox lifecycle and
  runtime behavior
- [Environments](./environments.md) — images, resources, and network access
