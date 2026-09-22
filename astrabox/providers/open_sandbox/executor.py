"""OpenSandbox atomic create, credential, readiness, and disposal helpers."""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import timedelta
from typing import Any, Protocol

from opensandbox import Sandbox
from opensandbox.models.sandboxes import CredentialProxyConfig
from opensandbox.models.filesystem import WriteEntry

from astrabox.core.service.orchestrator.runtime.pty_terminal import (
    EXECD_PORT,
)
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.providers.open_sandbox import _config
from astrabox.providers.open_sandbox._metadata import (
    assignment_metadata_value,
    session_metadata_value,
)
from astrabox.providers.open_sandbox.credential_vault import (
    carry_workload_substitutions,
)
from astrabox.providers.open_sandbox.sandbox import (
    OpenSandboxDataPlane,
    OpenSandboxHandle,
    OpenSandboxSandboxProvider,
)
from astrabox.providers.sandbox_image import (
    AIO_IMAGE_ENTRYPOINT,
    IN_BOX_SIDECAR_PORT,
)
from astrabox.seams.sandbox import (
    SANDBOX_ASSIGNMENT_ID_METADATA_KEY,
    SANDBOX_MANAGED_BY_METADATA_KEY,
    SANDBOX_MANAGED_BY_METADATA_VALUE,
    SANDBOX_PERMISSION_LEVEL_ADVANCED,
    SANDBOX_PERMISSION_LEVEL_DEFAULT,
    SANDBOX_PERMISSION_LEVEL_PRIVILEGED,
    SANDBOX_PERMISSION_LEVELS,
    SANDBOX_SESSION_ID_METADATA_KEY,
    SandboxDestruction,
)

logger = get_logger(__name__)

#: How often a conversation re-asks whether its Agent's box exists yet.
#: Short enough that the join costs a fraction of the box create it is
#: waiting on, long enough that a dry pool is not polled hundreds of times.
_AGENT_BOX_WAIT_POLL_SECONDS = 3.0


class SandboxRuntimeOptions(Protocol):
    """Engine-neutral process inputs the sandbox executor consumes.

    An engine may carry a much richer vendor options object, but the sandbox
    provider reads only the working directory needed at box creation. Keeping
    that structural shape here prevents a provider from depending on any
    engine SDK type or copying per-turn process environment onto the box.
    """

    cwd: Any

IN_BOX_SERVER_LOG = "/tmp/server.log"

#: Strong references to give-backs still running after the caller that started
#: them was cancelled. Without this the event loop is the only owner of those
#: tasks and may collect them mid-flight.
_RELEASES_IN_FLIGHT: set[asyncio.Task[Any]] = set()

# How long to wait for the in-box server's /health to come up before failing loud.
_INBOX_SERVER_READY_TIMEOUT_SECONDS = 30.0
_INBOX_SERVER_READY_POLL_SECONDS = 0.5

# Per-attempt host→box reachability budget. A pool can publish a sandbox after
# the in-box health check passes but before the host route has converged, so the
# readiness gate retries connection failures within its existing overall
# timeout. An HTTP response is definitive and is never retried.
_HOST_REACH_TIMEOUT_SECONDS = 10.0

# Budget for the "is a control server ALREADY answering in there?" probe that
# runs before the execd launch on boxes this executor did not create. Shorter
# than the reachability preflight above because a negative answer is a normal,
# expected outcome here (the server died and must be relaunched), not a failure
# — so it must cost a session little, while still outlasting a healthy server
# that happens to be busy.
_INBOX_SERVER_PROBE_TIMEOUT_SECONDS = 5.0

# ``Sandbox.create`` waits for execd's non-streaming ``GET /ping``. OpenSandbox
# command output uses a separate SSE path, which can still be converging after
# the ping turns green. Confirm that path with a bounded no-op command before a
# caller starts using it; user commands are never retried.
_EXECD_COMMAND_READY_TIMEOUT_SECONDS = 10.0
_EXECD_COMMAND_READY_POLL_SECONDS = 0.2
_EXECD_COMMAND_TRANSIENT_ERRORS = (
    "empty sse stream",
    "incomplete chunked read",
    # A probe that ran out of ITS OWN two seconds is the readiness question
    # answering "not yet", not a broken box. Treated as permanent, one slow
    # first exec — a box seconds old on a node running a dozen of them —
    # failed the whole runtime start and spent the ten-second budget below on
    # nothing (p187: `sh -lc :` did not complete within 2s, raised, done).
    "did not complete within",
)

# OpenSandbox server 0.2.3 consumes this exact extension on both its Docker and
# Kubernetes direct-create paths. It adds the namespace/mount privileges and
# tmpfs that execd's isolation capability measures after the box starts.
_EXECD_ISOLATION_EXTENSION_KEY = "bootstrap.execd.isolation"

# Probes /health from INSIDE the box (urllib, no extra deps): readiness is
# "the server bound its in-box port", independent of the host→box route the
# turn transport uses afterwards.
_HEALTH_PROBE = (
    "import urllib.request,sys\n"
    "try:\n"
    f"    r=urllib.request.urlopen('http://localhost:{IN_BOX_SIDECAR_PORT}/health',timeout=2)\n"
    "    sys.exit(0 if (getattr(r,'status',r.getcode())==200 and r.read()==b'OK') else 1)\n"
    "except Exception:\n"
    "    sys.exit(1)\n"
)


def _runner_ws_uri(base: str, *, session_id: str, sandbox_id: str) -> str:
    """Full http(s) endpoint → the runner's ws URI (scheme swap, PATH KEPT).

    A directly-reached endpoint is a bare ``http://host:port``; a
    server-proxied one carries a relay path
    (``http://server/sandboxes/<id>/proxy/<port>``) which is exactly what the
    relay forwards — dropping it would dial the lifecycle server's own root.
    """
    base = str(base).rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :]
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :]
    # The endpoint contract (provider get_endpoint) always emits a full URL;
    # anything else is a wiring bug, not a recoverable state.
    raise RuntimeError(
        f"open_sandbox endpoint is not a full http(s) URL: {base!r} "
        f"(session={session_id} sandbox={sandbox_id})"
    )


def _is_vault_already_exists_error(exc: BaseException) -> bool:
    """The egress sidecar's create answers 409 when the vault is already there.

    Matched structurally where the SDK carries the status, with the sidecar's
    own error text as the tiebreak — a 409 from anything OTHER than an
    existing vault must keep failing the adopt.
    """
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    if "already exists" in text:
        return True
    return status == 409 and "credential vault" in text


def _is_vault_conflict_error(exc: BaseException) -> bool:
    """Whether an atomic Vault mutation lost a revision/name race."""
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    return status == 409 or "http 409" in text or "conflict" in text


def _vault_item_name(item: Any) -> str:
    raw = item.get("name") if isinstance(item, dict) else getattr(item, "name", None)
    name = str(raw or "").strip()
    if not name:
        raise ValueError("credential vault entries must have a non-empty name")
    return name


def _vault_mutations(
    desired: list[Any],
    existing: list[Any],
    *,
    delete_names: set[str] | None = None,
) -> dict[str, object] | None:
    """Split desired entries into the add/replace verbs OpenSandbox requires."""
    existing_names = {_vault_item_name(item) for item in existing}
    desired_names = {_vault_item_name(item) for item in desired}
    additions = [item for item in desired if _vault_item_name(item) not in existing_names]
    replacements = [item for item in desired if _vault_item_name(item) in existing_names]
    mutations: dict[str, object] = {}
    if additions:
        mutations["add"] = additions
    if replacements:
        mutations["replace"] = replacements
    deletions = sorted(((delete_names or set()) & existing_names) - desired_names)
    if deletions:
        mutations["delete"] = deletions
    return mutations or None


def _mcp_credential_prefixes(bindings: list[Any]) -> tuple[str, ...]:
    return tuple(
        f"{name}-h-"
        for item in bindings
        if (name := _vault_item_name(item)).startswith("astrabox-mcp-")
    )


def _mcp_credential_names(items: list[Any]) -> set[str]:
    return {
        name
        for item in items
        if (name := _vault_item_name(item)).startswith("astrabox-mcp-")
        and "-h-" in name
        and "-s-" in name
    }


def _mcp_binding_names(items: list[Any]) -> set[str]:
    return {
        name
        for item in items
        if (name := _vault_item_name(item)).startswith("astrabox-mcp-")
        and "-v-" in name
        and "-i-" in name
    }


def _mcp_binding_destination_prefix(name: str) -> str:
    head, marker, _scope = str(name or "").partition("-v-")
    return f"{head}{marker}" if head.startswith("astrabox-mcp-") and marker else ""


def _mcp_binding_scope_prefix(name: str) -> str:
    head, marker, _identity = str(name or "").rpartition("-i-")
    return (
        f"{head}{marker}"
        if head.startswith("astrabox-mcp-") and "-v-" in head and marker
        else ""
    )


def _vault_inline_secret_values(
    vault_write: tuple[list[Any], list[Any]] | None,
) -> tuple[str, ...]:
    """Read inline values only long enough to scrub a sidecar failure."""

    credentials = vault_write[0] if vault_write is not None else []
    values: set[str] = set()
    for credential in credentials:
        source = (
            credential.get("source")
            if isinstance(credential, dict)
            else getattr(credential, "source", None)
        )
        value = (
            source.get("value")
            if isinstance(source, dict)
            else getattr(source, "value", None)
        )
        if isinstance(value, str) and value:
            values.add(value)
    return tuple(sorted(values, key=len, reverse=True))


def _vault_unusable_message(
    exc: BaseException,
    *,
    vault_write: tuple[list[Any], list[Any]] | None = None,
) -> str:
    message = _config.scrub_secret(
        "the platform could not apply protected credentials to this box "
        f"({type(exc).__name__}: {exc}). The error in parentheses identifies "
        "the failure; common causes include an unreachable egress "
        "sidecar, stale or mismatched OpenSandbox endpoint authentication, an "
        "egress policy that excludes "
        "a credential binding target, or an incompatible existing vault",
        secret=_config.resolve_api_key(load_astrabox_settings()),
    )
    for secret in _vault_inline_secret_values(vault_write):
        message = _config.scrub_secret(message, secret=secret)
    return message


async def _wait_for_execd_command_stream(
    handle: OpenSandboxHandle,
    *,
    session_id: str,
) -> None:
    """Wait for execd's SSE command path with a no-op readiness probe.

    The SDK already proved ``GET /ping``. An empty or incomplete stream, or the
    host deadline expiring for one bounded probe attempt, means this separate
    route is still converging. Every other error is a real failure and leaves
    immediately. Retrying this probe cannot duplicate user work because the
    command is the shell no-op ``:``.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _EXECD_COMMAND_READY_TIMEOUT_SECONDS
    attempts = 0
    while True:
        attempts += 1
        try:
            code, _stdout, stderr = await handle.exec_collect(
                ["sh", "-lc", ":"],
                timeout=min(2.0, max(0.1, deadline - loop.time())),
            )
        except Exception as exc:
            detail = _config.scrub_secret(
                str(exc),
                secret=_config.resolve_api_key(load_astrabox_settings()),
            )
            transient = isinstance(exc, TimeoutError) or any(
                marker in detail.lower() for marker in _EXECD_COMMAND_TRANSIENT_ERRORS
            )
            remaining = deadline - loop.time()
            if not transient:
                raise RuntimeError(
                    "open_sandbox execd command readiness probe failed "
                    f"(session={session_id} sandbox={handle.sandbox_id}): {detail}"
                ) from exc
            if remaining <= 0:
                raise RuntimeError(
                    "open_sandbox execd command stream did not become ready within "
                    f"{_EXECD_COMMAND_READY_TIMEOUT_SECONDS:g}s "
                    f"(session={session_id} sandbox={handle.sandbox_id}): {detail}"
                ) from exc
            await asyncio.sleep(min(_EXECD_COMMAND_READY_POLL_SECONDS, remaining))
            continue
        if code != 0:
            raise RuntimeError(
                "open_sandbox execd command readiness probe returned non-zero "
                f"(session={session_id} sandbox={handle.sandbox_id} exit={code} "
                f"stderr={stderr.decode('utf-8', 'replace')[-300:]!r})"
            )
        if attempts > 1:
            logger.info(
                "open_sandbox execd command stream became ready: session=%s "
                "sandbox=%s attempts=%d",
                session_id,
                handle.sandbox_id,
                attempts,
            )
        return


async def apply_open_sandbox_vault(
    handle: OpenSandboxHandle,
    *,
    vault_write: tuple[list[Any], list[Any]],
    session_id: str,
    managed_credential_names: tuple[str, ...] = (),
    managed_binding_names: tuple[str, ...] = (),
    managed_basic_scopes: tuple[str, ...] = (),
    create_if_missing: bool = True,
) -> None:
    """Create the Vault, or reconcile named entries in an existing revision.

    OpenSandbox deliberately gives ``add`` and ``replace`` different meanings:
    replacing a name that is absent is an error. An existing Vault may be empty
    or populated. Read the sanitized state and issue both mutation verbs in
    one atomic patch, guarded by its revision.  A shared Agent sandbox can have
    two conversations arrive together, so a lost revision race is re-read and
    retried rather than surfacing an incidental 409 to one of them.

    A binding is replaced WHOLE, and its substitution list is the one part of a
    vault the read API never returns. A caller that composed only its own
    workload's identity therefore stripped every sibling's, silently, and their
    placeholders reached the gateway verbatim. So the substitutions are not
    taken from the caller at all: they are derived here, inside the guarded
    window, from the credential names this same read returned plus the ones
    this write adds. The vault is its own authority, no caller needs to know
    which siblings exist, and there is no second store to fall behind it.
    """
    credentials, bindings = vault_write
    desired_mcp_names = _mcp_credential_names(credentials)
    managed_mcp_names = {
        str(name or "").strip()
        for name in managed_credential_names
        if str(name or "").strip()
    }
    desired_mcp_bindings = _mcp_binding_names(bindings)
    managed_mcp_bindings = {
        str(name or "").strip()
        for name in managed_binding_names
        if _mcp_binding_destination_prefix(str(name or "").strip())
    }
    destination_prefixes = {
        prefix
        for name in desired_mcp_bindings
        if (prefix := _mcp_binding_destination_prefix(name))
    }
    scope_prefixes = {
        prefix
        for name in desired_mcp_bindings
        if (prefix := _mcp_binding_scope_prefix(name))
    }
    mcp_prefixes = _mcp_credential_prefixes(bindings)
    vault = handle.sidecar_faces.credential_vault
    if create_if_missing:
        try:
            await vault.create(credentials=credentials, bindings=bindings)
        except Exception as exc:
            if not _is_vault_already_exists_error(exc):
                raise RuntimeError(
                    _vault_unusable_message(exc, vault_write=vault_write)
                ) from None
        else:
            logger.info(
                "open_sandbox: session=%s created protected credentials in sandbox=%s",
                session_id,
                handle.sandbox_id,
            )
            return

    for attempt in range(3):
        try:
            state = await vault.get()
            existing_credentials = list(getattr(state, "credentials", []) or [])
            existing_bindings = list(getattr(state, "bindings", []) or [])
            existing_mcp_names = _mcp_credential_names(existing_credentials)
            related_existing = {
                name
                for name in existing_mcp_names
                if any(
                    name.startswith(prefix)
                    for prefix in (*destination_prefixes, *mcp_prefixes)
                )
            }
            same_scope_credentials = {
                name
                for name in related_existing
                if any(name.startswith(prefix) for prefix in scope_prefixes)
            }
            effective_managed_credentials = (
                managed_mcp_names | same_scope_credentials
            )
            foreign_credentials = (
                related_existing
                - desired_mcp_names
                - effective_managed_credentials
            )
            existing_mcp_bindings = _mcp_binding_names(existing_bindings)
            related_bindings = {
                name
                for name in existing_mcp_bindings
                if any(name.startswith(prefix) for prefix in destination_prefixes)
            }
            same_scope_bindings = {
                name
                for name in related_bindings
                if any(name.startswith(prefix) for prefix in scope_prefixes)
            }
            effective_managed_bindings = managed_mcp_bindings | same_scope_bindings
            foreign_bindings = (
                related_bindings
                - desired_mcp_bindings
                - effective_managed_bindings
            )
            if foreign_credentials or foreign_bindings:
                raise APIError(
                    code="VAULT_CREDENTIAL_CONFLICT",
                    message=(
                        "the running sandbox already holds a different MCP "
                        "credential scope for this destination; use a sandbox "
                        "whose Sessions share the same managed Vault binding"
                    ),
                    status_code=409,
                )
            # Basic auth matches clean URLs without a workload placeholder.
            # Reconcile the complete managed selection, including an empty one,
            # so an archived credential cannot keep authenticating that URL.
            basic_names = {
                _vault_item_name(item)
                for item in [*existing_credentials, *existing_bindings]
                if _vault_item_name(item).startswith("astrabox-basic-")
            }
            owned_basic_names = {
                name for name in basic_names
                if any(f"-v-{scope}-i-" in name for scope in managed_basic_scopes)
            }
            desired_basic_names = {
                _vault_item_name(item) for item in bindings
                if _vault_item_name(item).startswith("astrabox-basic-")
            }
            basic_destinations = {
                name.partition("-v-")[0] + "-v-" for name in desired_basic_names
            }
            if any(
                name not in owned_basic_names and name not in desired_basic_names
                and any(name.startswith(prefix) for prefix in basic_destinations)
                for name in basic_names
            ):
                raise APIError(
                    code="VAULT_CREDENTIAL_CONFLICT",
                    message=(
                        "the running sandbox already holds a different HTTP Basic "
                        "credential scope for this destination; use a sandbox "
                        "whose Sessions share the same managed Vault binding"
                    ),
                    status_code=409,
                )
            credential_mutations = _vault_mutations(
                credentials,
                existing_credentials,
                delete_names=effective_managed_credentials | owned_basic_names,
            )
            binding_mutations = _vault_mutations(
                carry_workload_substitutions(
                    bindings, [*existing_credentials, *credentials]
                ),
                existing_bindings,
                delete_names=effective_managed_bindings | owned_basic_names,
            )
            if credential_mutations is None and binding_mutations is None:
                return
            await vault.patch(
                expected_revision=int(getattr(state, "revision")),
                credentials=credential_mutations,
                bindings=binding_mutations,
            )
        except APIError:
            raise
        except Exception as patch_exc:
            if attempt < 2 and _is_vault_conflict_error(patch_exc):
                continue
            raise RuntimeError(
                _vault_unusable_message(patch_exc, vault_write=vault_write)
            ) from None
        logger.info(
            "open_sandbox: session=%s reconciled protected credentials in "
            "sandbox=%s's existing vault",
            session_id,
            handle.sandbox_id,
        )
        return


async def destroy_open_sandbox_box(provider: Any, sandbox_id: str) -> SandboxDestruction:
    """Destroy one box through the provider's judgement, surviving cancellation.

    Every create guard in this package ends here. Two properties it has that a
    direct ``await provider.confirm_destroyed(...)`` does not:

    * the destroy runs in a task of its own behind ``asyncio.shield``. Guards
      run inside ``except BaseException`` blocks, which is where a shutdown's
      SECOND cancellation lands: without the shield the DELETE is cut in half
      and the box survives with nobody having decided that. The shield lets the
      cancellation reach the caller (who is being cancelled and should be)
      while the destroy runs to its own end;
    * the task is held in :data:`_RELEASES_IN_FLIGHT` while it runs, because
      once the caller lets go the event loop is the only thing referring to it
      and a task nothing holds may be collected mid-flight.

    Returns the verdict rather than a bool, so a caller that could not destroy
    the box is handed the id it must keep instead of a False it can ignore.
    """
    target = str(sandbox_id or "").strip()
    if not target:
        return SandboxDestruction.nothing_named(
            detail="the open_sandbox create guard had no sandbox id to destroy"
        )
    task = asyncio.create_task(provider.confirm_destroyed(target))
    _RELEASES_IN_FLIGHT.add(task)
    task.add_done_callback(_RELEASES_IN_FLIGHT.discard)
    try:
        destruction = await asyncio.shield(task)
    except asyncio.CancelledError:
        logger.error(
            "open_sandbox create guard: cancelled while destroying sandbox %s; the "
            "destroy itself keeps running, but nothing here can report its outcome",
            target,
        )
        raise
    if not destruction.confirmed:
        logger.error(
            "open_sandbox create guard: sandbox %s was not confirmed destroyed and "
            "may still be running: %s",
            target,
            destruction.detail,
        )
    return destruction


def _volumes_from_specs(specs: list[dict[str, Any]] | None) -> list[Any]:
    """Build the SDK's `Volume` objects from the provider's plain specs.

    The models are imported here rather than at module scope for the reason the
    file already gives its other lazy imports: a lifecycle-only caller pays for
    what it uses.
    """

    from opensandbox.models.sandboxes import Volume

    volumes: list[Any] = []
    for spec in specs or []:
        pvc = dict(spec.get("pvc") or {})
        if not pvc:
            raise RuntimeError(
                "a workspace volume must name a backend; only pvc is built here"
            )
        volumes.append(
            Volume.model_validate(
                {
                    "name": str(spec["name"]),
                    # By alias throughout: these models declare their fields
                    # under the wire names and do not populate by field name, so
                    # the alias IS the key. Validating the whole nested payload
                    # also lets the model build the backend struct itself.
                    "pvc": {
                        "claimName": str(pvc["claimName"]),
                        "createIfNotExists": bool(pvc.get("createIfNotExists", True)),
                        "deleteOnSandboxTermination": bool(
                            pvc.get("deleteOnSandboxTermination", False)
                        ),
                    },
                    "mountPath": str(spec["mountPath"]),
                    "subPath": str(spec["subPath"]) if spec.get("subPath") else None,
                    "readOnly": bool(spec.get("readOnly", False)),
                }
            )
        )
    return volumes


async def verify_host_reach(
    handle: OpenSandboxHandle,
    *,
    session_id: str,
    port: int = EXECD_PORT,
    path: str = "/astrabox-route-probe",
    require_ok: bool = False,
) -> None:
    """Wait until the HOST can reach the box's control port.

    The readiness poll above runs INSIDE the box (``exec_collect`` of a
    loopback probe), so it answers only "the server bound its port" — it
    says nothing about the host→box route the turn transport needs. Without
    this check the first real host→box touch is the engine client's ws dial
    onto the returned runner URI, so a misconfigured network (AstraBox and the sandbox on
    different docker networks, a server-proxy reach that is off, a firewalled
    endpoint) surfaces as a bare ``CLIConnectionError`` with no hint of the
    cause — and only once a turn is already under way.

    The SDK client pool may expose a newly healthy box before its host route has
    converged. Connection failures therefore remain a readiness state for
    the same bounded interval as the in-box probe.

    The probe target is EXECD, not any engine's own service: execd is the
    daemon OpenSandbox injects on every standard create, including boxes made by
    the SDK client-pool creator. It uses the same host→box route every other port
    shares, so an HTTP answer from it proves the route for whichever service the
    engine starts next. Probing an engine service here would make generic box
    preparation Claude-shaped: DeepSeek Harness does not expose Claude's
    ``:8000 /health`` contract, so that probe can exhaust the preparation
    window even when the route is healthy. Engine-level readiness stays where
    it belongs: the Claude flow's own control-server gate calls this with its port,
    ``/health`` and ``require_ok=True`` — there a non-200 means a
    stranger answers the engine's endpoint, which is not reachability.
    On the default route probe any HTTP status passes — a 404 from
    execd is still execd answering over the route.
    """
    plane = OpenSandboxDataPlane(handle=handle, port=port)
    deadline = time.monotonic() + _INBOX_SERVER_READY_TIMEOUT_SECONDS
    attempts = 0
    last_connection_error: ConnectionError | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            detail = (
                str(last_connection_error)
                if last_connection_error is not None
                else "the host-route readiness deadline expired"
            )
            raise RuntimeError(
                host_reach_error(handle, session_id=session_id, detail=detail)
            ) from last_connection_error
        attempts += 1
        try:
            response = await plane.request(
                "GET",
                path,
                timeout=min(_HOST_REACH_TIMEOUT_SECONDS, remaining),
            )
        except ConnectionError as exc:
            last_connection_error = exc
            remaining = deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(_INBOX_SERVER_READY_POLL_SECONDS, remaining))
            continue
        except Exception as exc:
            raise RuntimeError(
                host_reach_error(handle, session_id=session_id, detail=str(exc))
            ) from exc
        if require_ok and response.status_code != 200:
            raise RuntimeError(
                host_reach_error(
                    handle,
                    session_id=session_id,
                    detail=f"{path} answered status={response.status_code} "
                    f"body={response.text[:200]!r} (something other than the "
                    "in-box control server is answering this endpoint)",
                )
            )
        if attempts > 1:
            logger.info(
                "open_sandbox host route became ready: session=%s sandbox=%s attempts=%d",
                session_id,
                handle.sandbox_id,
                attempts,
            )
        return

def host_reach_error(handle: OpenSandboxHandle, *, session_id: str, detail: str) -> str:
    # The detail is foreign text (a transport error, a stranger's response
    # body) and this message reaches the runtime's start-failure log line
    # and its 502 — scrubbed like every other lifecycle-face exception exit.
    detail = _config.scrub_secret(
        detail, secret=_config.resolve_api_key(load_astrabox_settings())
    )
    return (
        "the in-box control server is healthy INSIDE the sandbox but the "
        "host cannot reach it over TCP "
        f"(session={session_id} sandbox={handle.sandbox_id} "
        f"port={IN_BOX_SIDECAR_PORT}): {detail}. This is a network "
        "configuration problem, not a sandbox failure: AstraBox and the "
        "sandbox must be on the same docker network for the endpoint the "
        "lifecycle API hands out to be routable — otherwise set "
        "ASTRABOX_SANDBOX_ENDPOINT_VIA_SERVER_PROXY=1 so endpoints are "
        "reached through the opensandbox server instead."
    )

# ── in-box runner (launch_sandbox_runner) ────────────────────────────────


#: How long a conversation that lost the first-borrow race waits for the winner
#: to publish the agent's box. Sized to a Pool member's start rather than to a
#: turn: past this, the answer is that the agent really has no box.
_AGENT_BOX_RACE_WAIT_SECONDS = 45.0
_AGENT_BOX_RACE_POLL_SECONDS = 1.0


def _direct_create_extensions(permission_level: str) -> dict[str, str]:
    """Map one platform level onto OpenSandbox's direct-create contract."""

    level = str(permission_level or SANDBOX_PERMISSION_LEVEL_DEFAULT).strip().lower()
    if level not in SANDBOX_PERMISSION_LEVELS:
        raise APIError(
            code="UNSUPPORTED_SANDBOX_PERMISSION_LEVEL",
            message=(
                f"open_sandbox cannot create sandbox_permission_level={level!r}; "
                f"expected one of {', '.join(repr(item) for item in SANDBOX_PERMISSION_LEVELS)}"
            ),
            status_code=409,
            data={"sandbox_permission_level": level},
        )
    if level == SANDBOX_PERMISSION_LEVEL_DEFAULT:
        return {}
    if level == SANDBOX_PERMISSION_LEVEL_ADVANCED:
        return {_EXECD_ISOLATION_EXTENSION_KEY: "enable"}
    raise APIError(
        code="UNSUPPORTED_SANDBOX_PERMISSION_LEVEL",
        message=(
            "open_sandbox cannot grant sandbox_permission_level='privileged' "
            "because neither its direct-create nor Pool lifecycle contract "
            "exposes a machine-readable privileged attestation"
        ),
        status_code=409,
        data={"sandbox_permission_level": SANDBOX_PERMISSION_LEVEL_PRIVILEGED},
    )


async def require_open_sandbox_permission_level(
    handle: OpenSandboxHandle,
    permission_level: str,
) -> None:
    """Prove a running box can enforce the requested in-box isolation level."""

    level = str(permission_level or SANDBOX_PERMISSION_LEVEL_DEFAULT).strip().lower()
    if level not in SANDBOX_PERMISSION_LEVELS:
        raise APIError(
            code="UNSUPPORTED_SANDBOX_PERMISSION_LEVEL",
            message=(
                f"open_sandbox cannot prove sandbox_permission_level={level!r}; "
                f"expected one of {', '.join(repr(item) for item in SANDBOX_PERMISSION_LEVELS)}"
            ),
            status_code=409,
            data={
                "sandbox_id": handle.sandbox_id,
                "sandbox_permission_level": level,
            },
        )
    if level == SANDBOX_PERMISSION_LEVEL_DEFAULT:
        return
    if level == SANDBOX_PERMISSION_LEVEL_PRIVILEGED:
        raise APIError(
            code="UNSUPPORTED_SANDBOX_PERMISSION_LEVEL",
            message=(
                "open_sandbox cannot prove sandbox_permission_level='privileged': "
                "the lifecycle API does not expose a Pool's pod template or a "
                "running sandbox's privileged-container status"
            ),
            status_code=409,
            data={
                "sandbox_id": handle.sandbox_id,
                "sandbox_permission_level": level,
            },
        )
    reported = await handle.sidecar_faces.isolation.capabilities()
    if bool(getattr(reported, "available", False)):
        return
    raise APIError(
        code="SANDBOX_ISOLATION_UNSUPPORTED",
        message=(
            f"open_sandbox sandbox {handle.sandbox_id!r} does not provide the "
            f"requested {level!r} permission level: "
            f"{getattr(reported, 'message', None) or 'isolation capability unavailable'}"
        ),
        status_code=409,
        data={
            "sandbox_id": handle.sandbox_id,
            "sandbox_permission_level": level,
        },
    )


async def create_open_sandbox_box(
    *,
    session_id: str,
    assignment_id: str,
    image: str,
    env: dict[str, str],
    resource_limits: dict[str, str],
    resource_requests: dict[str, str],
    metadata: dict[str, str] | None = None,
    cwd: str | None,
    require_execd_command_stream: bool = False,
    entrypoint: tuple[str, ...] | None = None,
    transport: Any = None,
    network_policy: Any = None,
    permission_level: str = SANDBOX_PERMISSION_LEVEL_DEFAULT,
    credential_proxy_enabled: bool = False,
    vault_write: tuple[list[Any], list[Any]] | None = None,
    volumes: list[dict[str, Any]] | None = None,
) -> OpenSandboxHandle:
    """Create ONE OpenSandbox sandbox and return its handle. No agent started.

    The backend's box-create primitive, called by the provider's
    ``create_sandbox`` seam and by the Agent client pool, so the boot contract
    (explicit image entrypoint, lease-sized TTL, reverse-lookup metadata) and
    the post-create kill guard exist once. Everything that provisions a box for
    a CONVERSATION goes through the seam rather than here, because the seam is
    where the platform's create-time plan is honoured.

    ``session_id`` carries the logical owner of the physical runtime: the
    Session for conversation tenancy or the Agent runtime for shared tenancy.
    The provider projects that identity onto OpenSandbox's metadata wire.
    ``assignment_id`` is the durable identity of this create attempt. Before
    creating, the provider asks for a box already carrying that identity; a
    replay after a worker crash therefore converges the same box. ``env`` is
    the box's boot environment. ``entrypoint`` declares the image's
    own boot command; when omitted, the shared agent image contract is used.
    ``resource_limits`` and ``resource_requests`` stay separate all the way to
    the SDK so its request-equals-limit fallback never selects node capacity.
    An empty tuple is rejected rather than allowing the SDK to replace it with
    ``tail -f /dev/null``. Reserved ``OPENSANDBOX_EGRESS_*``
    names are rejected from that untrusted map; when the host configured its
    private DNS upstream this function adds that one value for the lifecycle
    server to route to the sidecar. ``network_policy``,
    when given, is the typed egress policy the box is created under: upstream
    attaches its egress sidecar to a sandbox precisely because one was asked for,
    so passing it is what turns the sidecar on for that box.
    ``permission_level`` is mapped to OpenSandbox's namespaced execd-isolation
    extension for ``advanced`` and then verified against the running box;
    ``privileged`` has no direct-create mapping and is refused before the API is
    called. ``credential_proxy_enabled`` creates the credential proxy without writing a
    vault yet; this is the safe Agent-prewarm state. A non-empty ``vault_write``
    also enables the proxy and immediately writes its contents. ``cwd``, when
    given, is pre-created inside the box so a later process spawn cannot race
    the directory into existence. ``require_execd_command_stream`` proves the
    SSE command path with a no-op before returning; callers that do not use
    execd leave it false.

    The returned handle owns its own SDK object and connection pool, so it
    outlives whatever built it and stays valid until closed; its ``sandbox_id``
    is what a later :meth:`OpenSandboxSandboxProvider.connect` re-binds by.

    Any failure AFTER the create kills the just-created box before re-raising:
    the lifecycle API has no create idempotency key, so an orphan nobody holds
    an id for would otherwise live out its full lease.
    """
    assignment = str(assignment_id or "").strip()
    if not assignment:
        raise APIError(
            code="SANDBOX_ASSIGNMENT_INVALID",
            message="open_sandbox create requires a non-empty assignment_id",
            status_code=500,
        )
    assignment_metadata = assignment_metadata_value(assignment)
    session_metadata = session_metadata_value(session_id)
    caller_metadata = {
        str(key).strip(): str(value).strip()
        for key, value in dict(metadata or {}).items()
    }
    reserved_metadata = {
        SANDBOX_SESSION_ID_METADATA_KEY,
        SANDBOX_MANAGED_BY_METADATA_KEY,
        SANDBOX_ASSIGNMENT_ID_METADATA_KEY,
    }
    collisions = sorted(reserved_metadata & set(caller_metadata))
    if collisions:
        raise APIError(
            code="SANDBOX_CONFIG_INVALID",
            message=(
                "sandbox create metadata cannot replace platform ownership keys: "
                + ", ".join(collisions)
            ),
            status_code=400,
        )
    settings = load_astrabox_settings()
    connection_config = _config.sdk_connection_config(
        settings,
        transport=transport,
        request_timeout_seconds=_config.create_request_timeout_seconds(settings),
    )
    # Key hygiene (the invariant the provider's _api_error enforces): every call
    # below is a lifecycle-face request carrying the API key header, and a
    # misbehaving server can echo it back in an error body that lands verbatim in
    # the SDK exception text — which the runtime's start-failure path then logs
    # and folds into its 502 message. Scrub every exception exit on this face.
    secret = _config.resolve_api_key(settings)
    # Optional hardening requested by the platform, collected before the call so
    # each value is either passed or absent — never passed as None, which the SDK
    # treats differently from unset.
    hardening: dict[str, Any] = {}
    if network_policy is not None:
        hardening["network_policy"] = network_policy
    if credential_proxy_enabled or vault_write is not None:
        hardening["credential_proxy"] = CredentialProxyConfig(enabled=True)
    if bool(getattr(settings, "sandbox_secure_access_enabled", False)):
        # OpenSandbox enforces this only on Kubernetes ingress-gateway
        # deployments. A mismatched Docker/direct deployment returns 400; that
        # is the intended fail-loud result for a security control it cannot honor.
        hardening["secure_access"] = True
    create_extensions = _direct_create_extensions(permission_level)
    if create_extensions:
        hardening["extensions"] = create_extensions
    create_env = dict(env)
    reserved = sorted(key for key in create_env if str(key).startswith("OPENSANDBOX_EGRESS_"))
    if reserved:
        raise APIError(
            code="SANDBOX_CONFIG_INVALID",
            message=(
                "sandbox environment variables under OPENSANDBOX_EGRESS_* are "
                "reserved for the host; remove: " + ", ".join(reserved)
            ),
            status_code=400,
        )
    resolved_entrypoint = tuple(entrypoint) if entrypoint is not None else AIO_IMAGE_ENTRYPOINT
    if not resolved_entrypoint or any(not str(part).strip() for part in resolved_entrypoint):
        raise APIError(
            code="SANDBOX_CONFIG_INVALID",
            message="sandbox entrypoint must contain at least one non-empty command part",
            status_code=400,
        )
    dns_upstream = str(getattr(settings, "sandbox_egress_dns_upstream", "") or "").strip()
    if dns_upstream and network_policy is not None:
        create_env["OPENSANDBOX_EGRESS_DNS_UPSTREAM"] = dns_upstream
    provider = OpenSandboxSandboxProvider(transport=transport)
    recovered = await provider.find_sandbox_by_assignment(assignment)
    if recovered is not None and recovered.session_id != session_metadata:
        raise APIError(
            code="SANDBOX_ASSIGNMENT_CONFLICT",
            message=(
                f"open_sandbox assignment {assignment!r} belongs to runtime owner "
                f"{recovered.session_id!r}, not {str(session_id)!r}"
            ),
            status_code=409,
        )
    if recovered is not None:
        try:
            recovered_handle = await provider.connect(recovered.sandbox_id)
            sdk_sandbox = recovered_handle.sidecar_faces
            logger.info(
                "open_sandbox recovered create assignment: assignment=%s "
                "owner=%s sandbox=%s",
                assignment,
                session_id,
                recovered.sandbox_id,
            )
        except BaseException as exc:
            destruction = await destroy_open_sandbox_box(
                provider,
                recovered.sandbox_id,
            )
            leaked = destruction.leaked_sandbox_id
            if leaked is None or not isinstance(exc, Exception):
                if leaked is not None:
                    logger.error(
                        "open_sandbox correlated-create recovery was cancelled "
                        "and the unusable sandbox survives: assignment=%s "
                        "sandbox=%s (%s)",
                        assignment,
                        leaked,
                        destruction.detail,
                    )
                raise
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=_config.scrub_secret(
                    "open_sandbox could not reconnect create assignment "
                    f"{assignment!r} for runtime owner {str(session_id)!r} ({exc}), "
                    "and the correlated sandbox could not be destroyed: "
                    f"{destruction.detail}",
                    secret=secret,
                ),
                status_code=502,
                data={"sandbox_id": leaked, "leaked_sandbox_id": leaked},
            ) from exc
    else:
        try:
            sdk_sandbox = await Sandbox.create(
                image=image,
                # The SDK client substitutes ["tail","-f","/dev/null"] for ANY
                # falsy entrypoint, which would strip the AIO image's real init;
                # the boot contract must be passed explicitly.
                entrypoint=list(resolved_entrypoint),
                env=create_env,
                resource=dict(resource_limits),
                resource_requests=dict(resource_requests),
                # TTL = the deployment's conversation lease, never the SDK's
                # 600 s default; the activity-renew loop extends it via
                # provider.renew.
                timeout=timedelta(seconds=int(settings.sandbox_lease_seconds)),
                ready_timeout=timedelta(
                    seconds=int(settings.sandbox_ready_timeout_seconds)
                ),
                **hardening,
                metadata={
                    **caller_metadata,
                    # The seam owns these names. Runtime owner + deployment
                    # establish ownership; assignment names this create attempt.
                    SANDBOX_SESSION_ID_METADATA_KEY: session_metadata,
                    SANDBOX_MANAGED_BY_METADATA_KEY: SANDBOX_MANAGED_BY_METADATA_VALUE,
                    SANDBOX_ASSIGNMENT_ID_METADATA_KEY: assignment_metadata,
                },
                connection_config=connection_config,
                # Durable storage, when the caller planned any. Passed at CREATE
                # because that is the only moment a mount can be established:
                # this backend offers no in-box remount, and a pooled box is
                # created before the conversation that borrows it exists.
                volumes=_volumes_from_specs(volumes) if volumes else None,
            )
        except Exception as exc:
            raise RuntimeError(
                _config.scrub_secret(
                    "open_sandbox sandbox create failed for runtime owner "
                    f"{str(session_id)!r}: {exc}",
                    secret=secret,
                )
            ) from exc
    handle = OpenSandboxHandle(sdk_sandbox)
    try:
        await require_open_sandbox_permission_level(handle, permission_level)
        if require_execd_command_stream:
            await _wait_for_execd_command_stream(handle, session_id=str(session_id))
        if vault_write is not None:
            # BEFORE anything else uses the box. The CLI is launched with a
            # placeholder, so until this lands the sandbox holds a credential
            # that authenticates nothing — and the guard below destroys the box
            # rather than handing back one whose every turn would fail with an
            # upstream 401 three layers from the cause.
            await apply_open_sandbox_vault(
                handle,
                vault_write=vault_write,
                session_id=str(session_id),
            )
        if cwd:
            # mode matches the SDK's own default vocabulary (integer-spelled
            # permissions). Idempotent.
            await sdk_sandbox.files.create_directories([WriteEntry(path=cwd, mode=755)])
    except BaseException as exc:
        # The claim needs no asking: this function created the box on the line
        # above and has handed it to nobody. Preserve the sandbox id when
        # cleanup cannot confirm destruction so the caller can recover it.
        sandbox_id = str(getattr(sdk_sandbox, "id", "") or "").strip()
        with contextlib.suppress(Exception):
            await sdk_sandbox.close()
        destruction = await destroy_open_sandbox_box(
            provider, sandbox_id
        )
        leaked = destruction.leaked_sandbox_id
        if leaked is None or not isinstance(exc, Exception):
            if leaked is not None:
                logger.error(
                    "open_sandbox create guard: cancelled after create and the "
                    "sandbox survives: sandbox=%s (%s)",
                    leaked,
                    destruction.detail,
                )
            raise
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=_config.scrub_secret(
                f"open_sandbox sandbox setup failed for session {str(session_id)!r} "
                f"({exc}), and the sandbox created for it could not be destroyed: "
                f"{destruction.detail}",
                secret=secret,
            ),
            status_code=502,
            data={"sandbox_id": leaked, "leaked_sandbox_id": leaked},
        ) from exc
    return handle


__all__ = [
    "create_open_sandbox_box",
    "require_open_sandbox_permission_level",
]
