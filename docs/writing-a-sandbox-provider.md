# Add a sandbox provider

A `SandboxProvider` gives AstraBox the sandboxes Agent programs run in. It
creates a sandbox, reaches it again by id, renews, pauses and destroys it, and
returns a handle to the sandbox's files, commands, long-lived processes, and
ports.

AstraBox includes one provider, `open_sandbox`, which calls an OpenSandbox
lifecycle service. Write another provider when a deployment's sandboxes come
from a different platform.

## What AstraBox and the provider each own

| AstraBox | The provider |
| --- | --- |
| Which sandbox a conversation uses, and when to create, pause, or replace one | Creating a sandbox from a `SandboxCreateSpec`, and connecting to it by id |
| Sessions, workspace planning, credentials, and starting the Agent program | Destroying a sandbox, and proving that it is gone |
| The native conversation state, kept in AstraBox's database | The sandbox's files, commands, long-lived processes, and port addresses |
| Refusing configuration the backend has not declared it can run | Declaring what the backend can run, and implementing what it declares |

The full contract, with every type, is in `astrabox/seams/sandbox.py` and
`astrabox/seams/sandbox_data.py`.

## Return a handle

`create_sandbox` and `connect` return a `SandboxHandle`. AstraBox uses these
members of it, and nothing else:

| Member | Contract |
| --- | --- |
| `sandbox_id` | The id the provider reaches the sandbox by again. |
| `files` | `SandboxFiles`: absolute paths taken literally, POSIX permission bits such as `0o640`, account names for owners, and `SandboxPathNotFound`, `SandboxPathRejected`, or `SandboxUnreachable` for a failure. |
| `commands` | `SandboxCommands`: one shell command line run as root with an empty stdin. A non-zero exit status is data in the `SandboxCommandResult`; a deadline passed raises `TimeoutError` and the sandbox stops the command. |
| `processes` | `SandboxProcesses`: long-lived processes, such as a terminal's shell or the Pi program, that any replica attaches to by id. One attachment holds a process; another may take it over. Output is numbered in bytes, stdout and stderr together, and replays from an offset the sandbox still holds or fails with `SandboxProcessReplayGap`. A process that exited runs again when the next attachment arrives. |
| `get_endpoint(port)` | A `SandboxEndpoint`: an absolute `http` or `https` URL that reaches the in-sandbox port, and the headers every request to it must carry. |
| `get_signed_endpoint(port, expires_at=...)` | A `SandboxEndpoint` whose URL itself carries a credential until `expires_at`, for a client that cannot send headers. A backend that cannot sign raises the 501 `SANDBOX_SIGNED_ENDPOINT_UNSUPPORTED`. |

A provider whose sandboxes cannot keep long-lived processes leaves
`supports_process_sessions` false and returns
`UnsupportedSandboxProcesses(name)` from `processes`; every operation on it
raises the 501 `SANDBOX_PROCESS_SESSIONS_UNSUPPORTED`. `close()`, when the
handle has one, releases local resources only and never destroys the sandbox.

## Reuse execd for files, commands, and processes

Every bundled sandbox image carries OpenSandbox's execd `v1.1.0` at
`/opt/astrabox/opensandbox/execd` (see the
[Container Reference](container-reference.md)). When the provider's sandboxes
run an execd on port `44772`, and `get_endpoint(44772)` returns a URL and
headers that reach it, build the handle's three data-plane faces from
`astrabox.providers.execd` instead of writing them:

```python
from astrabox.providers.execd import EXECD_PORT, ExecdCommands, ExecdFiles, ExecdProcesses


async def data_plane(sandbox_id, get_endpoint):
    execd = await get_endpoint(EXECD_PORT)
    return (
        ExecdFiles.at_endpoint(execd.url, execd.headers, sandbox_id=sandbox_id),
        ExecdCommands.at_endpoint(execd.url, execd.headers, sandbox_id=sandbox_id),
        ExecdProcesses.at_endpoint(execd.url, execd.headers, sandbox_id=sandbox_id),
    )
```

These implementations carry execd's wire rules: its mode format, its exit
status reporting, its replay frames, and its connection closing. With them,
the provider declares `supports_process_sessions = True`.

For expiring process endpoints, construct `ExecdProcesses` with an async
resolver that returns the complete `SandboxEndpoint`, including `expires_at`.
It resolves again before a new HTTP request or WebSocket attachment when the
cached endpoint expires; a naive expiry is interpreted as UTC. The resolver
must return a usable endpoint. `at_endpoint` is for fixed addresses. Resolving
another address preserves the process ID and replay cursor; it does not replace
an open attachment. The provider owns an existing connection's expiry policy.
HTTP permission errors do not trigger endpoint renewal or retries.

For process attachments through a WebSocket proxy, use the bundled execd image
and `ExecdProcesses` together. The client requests `errors=frame`, so an occupied
holder or missing process is rejected with the daemon's existing machine code
after WebSocket upgrade. The proxy must forward text and binary frames without
rewriting them. Unclassified proxy failures are errors, not evidence of an
occupied process; no status query or automatic takeover replaces the atomic
attachment decision. Existing boxes need the matching execd binary as well as
the updated client.

execd runs as root and executes whatever a request that reaches it asks for.
Put a per-sandbox credential in the headers `get_endpoint` returns, where the
sandbox's own workload cannot read it, or let only AstraBox reach the port.

A provider can instead implement the three protocols with its platform's own
APIs. It then has to keep the same contract, including writing a file's
permission bits and owner in the same operation, so verify it with the
conformance suite described below.

## Declare what the backend can do

Capability flags on the provider class say what the backend supports.
Registration refuses a flag declared true without the methods its row in
`SANDBOX_CAPABILITY_RULES` names, so a half-declared capability fails when the
provider is installed rather than at the first conversation that needs it.

| Flag | The provider also implements |
| --- | --- |
| `supports_create_network_policy` | `admit_runtime_egress`; creates each sandbox under the Environment's network access rules |
| `supports_isolated_sessions` | the isolated-session operations and `count_active_isolated_sessions`; `read_memory_headroom` and `read_listening_ports` have defaults that read the sandbox through `commands` |
| `supports_isolated_session_recovery` | `find_isolated_sessions_by_workspace` |
| `supports_pause` | `pause` and `resume` |
| `supports_client_pool` | the client-pool operations, `owns_unclaimed_sandbox`, and `client_pool_readiness` |
| `supports_correlated_create` | `find_sandbox_by_assignment` |
| `supports_egress_credential_injection` | `apply_credential_vault` |
| `supports_inventory_listing` | `list_sandboxes` and `describe_sandbox` |
| `supports_process_sessions` | the handle's `processes` |

AstraBox refuses configuration that needs a capability the selected backend
does not declare when that configuration is saved: an Environment on a backend
without `supports_create_network_policy`; sandbox sharing without
`supports_isolated_sessions`; the `Pause` idle behavior without
`supports_pause`; prepared capacity without a ready client pool; and, without
sandbox sharing, the Pi Agent program or an Agent's terminal panel without
`supports_process_sessions`.

`supports_correlated_create` decides what a platform restart in the middle of
a create can recover. With it, AstraBox asks `find_sandbox_by_assignment` for
the sandbox an interrupted start may have made, and continues with it. Without
it, AstraBox creates each sandbox once and never asks: a create that raises
fails the conversation's start with that error, and a start that a platform
restart interrupted is refused with `409 SANDBOX_CREATE_INTERRUPTED` instead of
making a second sandbox, so the person starts the conversation again. A sandbox
the backend made for that interrupted start is not known to AstraBox; the
backend's own expiry reclaims it. Retrying a request that failed in transit is
the provider's own client's job, for requests the backend accepts twice.

## Prove what happened to a sandbox

`kill(sandbox_id)` returns `True` only when the backend established that this
sandbox is gone; a delete it accepted but did not confirm returns `False`.
AstraBox never calls it directly. It calls `confirm_destroyed`, which turns
that answer, and a second observation from `probe`, into a verdict. A backend
that cannot probe raises `NotImplementedError` from `probe`; any other
exception counts as a probe that failed, and the destruction stays unconfirmed.

`endpoint_names_sandbox(endpoint)` returns the sandbox id an endpoint string
of this backend encodes, or `None` when the address does not say. AstraBox
uses a stored endpoint only when the backend confirms it names the expected
sandbox, and otherwise resolves it again from the sandbox id.

## Enable the backend

`ASTRABOX_SANDBOX_BACKENDS` lists the backends a deployment enables, and
`ASTRABOX_SANDBOX_BACKEND` names the default for an Environment that selects
none. A record that names an installed backend the deployment has not enabled
fails with `SANDBOX_BACKEND_DISABLED`. List the environment variables only the
provider reads in `owned_settings`: AstraBox warns at startup about each one
set while the provider is installed but not enabled.

A backend that mounts persistent workspace storage returns a `WorkspaceRoute`
from `workspace_route()`. Without one, a deployment that configures a
workspace volume with this backend is refused at startup.

## Package the provider

Declare the provider in its Python distribution:

```toml
[project.entry-points."astrabox.providers.sandbox"]
example = "astrabox_sandbox_example:ExampleSandboxProvider"
```

Set the class's `name` to the entry-point name. AstraBox instantiates and
registers the class when it starts. The class may also set
`seams_api_version`; an incompatible declared version fails at startup
instead of at the first conversation.

## Verify the provider

Bind the structural contract suite in the provider's unit tests:

```python
from astrabox.testing.provider_conformance import SandboxProviderContractSuite

from astrabox_sandbox_example import ExampleSandboxProvider


class TestExampleSandbox(SandboxProviderContractSuite):
    def make_provider(self):
        return ExampleSandboxProvider()
```

It checks that the abstract methods are implemented, the capability flags are
booleans, and every declared capability carries what its rule requires.

Then bind the data-plane suite in a live test against a real sandbox of the
provider, one case per clause:

```python
import pytest

from astrabox.testing.sandbox_data_conformance import (
    DATA_CONTRACT,
    PROCESSES_CONTRACT,
    DataContractBox,
    run_contract_check,
)


@pytest.mark.parametrize("check", DATA_CONTRACT + PROCESSES_CONTRACT, ids=lambda check: check.name)
async def test_data_contract(check, live_handle):
    await run_contract_check(check, DataContractBox(
        files=live_handle.files,
        commands=live_handle.commands,
        scratch="/tmp/data-contract",
        account="agent",
        processes=live_handle.processes,
    ))
```

Leave `PROCESSES_CONTRACT` out, and pass no `processes`, for a provider
without `supports_process_sessions`. `account` names an account, with a group
of the same name, that the sandbox image has and that the files face does not
itself run as.

Finally, follow a conversation through a real deployment that enables the
provider: start a conversation, read and write files in the Files panel, run a
command in the terminal and interrupt it, let the sandbox go idle, and send
another message.

## Related

- [Architecture](architecture.md) — where sandbox providers sit
- [OpenSandbox backend](providers/opensandbox.md) — the built-in provider
- [Container Reference](container-reference.md) — what a sandbox image carries
- [Embed AstraBox](embedding.md) — entry points and startup

Execution ownership is part of provider seam API9. Every `SandboxCreateSpec`
carries `execution_target`. Add `execution_target_metadata(spec.execution_target)`
to the provider-owned create metadata before allocating a resource, and preserve
that mapping through adoption and inventory readback. Its opaque value uses the
complete target digest in a label-safe representation; the readable target name
lives in the platform records. Do not replace or truncate it. Inventory-based
cleanup selects the installation and this token together, because a Session
binding can still be absent while creation is in progress.
