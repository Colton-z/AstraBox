"""OpenSandbox sandbox provider: the by-id lifecycle over the OpenSandbox
lifecycle API, the live-sandbox handle, and the in-box HTTP data plane.

The runtime treats a "sandbox" as an opaque handle. For OpenSandbox that handle
(:class:`OpenSandboxHandle`) wraps the SDK ``Sandbox`` object (identity +
``commands``/``files`` capabilities + endpoint resolution). Lifecycle
(connect/kill/probe/renew/expiry/endpoint-by-id) lives on the provider, one SDK
object per operation, closed in ``finally`` — no long-lived pool a caller must
remember to release.

Error taxonomy (single mapping, applied at every lifecycle edge):

* SDK 404 (``SandboxApiException.status_code == 404``) → probe ``NOT_FOUND`` /
  ``kill`` returns ``True`` (idempotent — the server keeps no tombstone) /
  everything else ``APIError(SANDBOX_NOT_FOUND, 404)``;
* any other SDK/transport failure → ``APIError(AGENT_RUNTIME_ERROR, 502)``;
* ``probe`` never raises (the runtime's probe wrapper only bounds timeouts);
* the API key never enters an exception text, log line, or repr — every message
  built from SDK error text is scrubbed first.
"""

from __future__ import annotations

import asyncio
import dataclasses
import contextlib
import time
import re
import shlex
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx
from opensandbox import Sandbox, SandboxManager
from opensandbox.adapters.factory import AdapterFactory
from opensandbox.exceptions import SandboxApiException
from opensandbox.models.filesystem import SearchEntry
from opensandbox.models.sandboxes import NetworkRule, SandboxEndpoint, SandboxFilter, SnapshotFilter

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.providers.open_sandbox import _config
from astrabox.providers.open_sandbox._metadata import (
    assignment_metadata_value,
    is_client_pool_identity,
    session_metadata_value,
)
from astrabox.providers.open_sandbox.credential_vault import (
    open_sandbox_vault_write,
    require_vault_preconditions,
)
from astrabox.providers.open_sandbox.networking import (
    open_sandbox_network_policy,
    with_vault_binding_allows,
)
from astrabox.providers.sandbox_image import (
    IN_BOX_CLI,
    IN_BOX_SIDECAR_PORT,
    resolve_agent_image,
)
from astrabox.seams.egress_credentials import SandboxEgressCredentialPlan
from astrabox.seams.sandbox import (
    SANDBOX_ASSIGNMENT_ID_METADATA_KEY,
    SANDBOX_DIAGNOSTIC_SCOPES,
    SANDBOX_LIFECYCLE_PROBE_FAILED,
    SANDBOX_LIFECYCLE_PROBE_NOT_FOUND,
    SANDBOX_LIFECYCLE_PROBE_OK,
    SANDBOX_MANAGED_BY_METADATA_KEY,
    SANDBOX_MANAGED_BY_METADATA_VALUE,
    SANDBOX_PERMISSION_LEVEL_ADVANCED,
    SANDBOX_PERMISSION_LEVEL_DEFAULT,
    SANDBOX_SESSION_ID_METADATA_KEY,
    SandboxClaim,
    SandboxBrowserEndpoint,
    SandboxClientPoolCreator,
    SandboxClientPoolPreparer,
    SandboxClientPoolSpec,
    SandboxClientPoolStatus,
    SandboxCreateSpec,
    SandboxDataPlane,
    SandboxDescriptor,
    SandboxDiagnostics,
    SandboxHttpResponse,
    SandboxLifecycleProbeResult,
    SandboxPage,
    SandboxIsolatedSession,
    SandboxIsolationCapability,
    SandboxSecurityPosture,
    SandboxProvider,
    SandboxRuntimeDefaults,
    claim_from_metadata,
    register_sandbox,
)

logger = get_logger(__name__)


def _open_sandbox_vault_write(
    plan: SandboxEgressCredentialPlan | None,
) -> tuple[list[Any], list[Any]] | None:
    """Validate and translate credential intent before sandbox allocation."""

    if plan is None:
        return None
    from astrabox.deploy.sandbox_server import egress as sandbox_egress_config

    egress_config = sandbox_egress_config()
    require_vault_preconditions(
        egress_image=str(egress_config.get("image") or ""),
        egress_mode=str(egress_config.get("mode") or ""),
    )
    return open_sandbox_vault_write(plan)

#: This backend's registry key. Named once so a sibling module can ask the
#: registry for THIS provider (the pool asks it for its disposal capabilities)
#: without spelling the string a second time.
OPEN_SANDBOX_BACKEND_NAME = "open_sandbox"

_TRAILING_INT_RE = re.compile(r"(-?\d+)\s*$")

# Pause and resume are asynchronous on this control plane: the call returns
# while a commit Job still runs. These bound the wait for the state to settle.
# The timeout is generous because the commit is an image push whose duration is
# the box's filesystem, not a fixed cost — and the caller's fallback for "not
# settled" is simply to leave the sandbox alone, which is cheap.
_PAUSE_SETTLE_TIMEOUT_SECONDS = 300.0
_PAUSE_SETTLE_POLL_SECONDS = 3.0
# Upstream's phase vocabulary, upper-cased at the comparison. `Succeed` is what
# a box that finished pausing reports once its snapshot Job completed.
_PAUSED_STATES = frozenset({"PAUSED", "SUCCEED"})
_RUNNING_STATES = frozenset({"RUNNING", "READY"})
_SETTLE_LOST_STATES = frozenset({"FAILED", "ERROR", "TERMINATED"})
"""States a pause or a resume can never leave for the one it was aiming at.

The control plane reaching one of these is the answer, not a step towards it:
its own conditions say the transition is over and did not happen. Polling on
past it spends the whole settle budget to return the same verdict, and a caller
that is an HTTP request spends it in front of the user.
"""

#: execd's stdout/stderr line-event model used by ``exec_collect``. Pinned execd
#: v1.1.0 (:data:`EXECD_LINE_EVENT_MODEL_VERSION`) emits this shape, although
#: execd publishes no normative contract for it:
#:
#: * one event carries one output LINE with its terminating newline removed;
#: * an event whose text is EXACTLY ``"\n"`` carries an EMPTY line.
#:
#: Reassembly therefore normalizes the empty-line event to ``""`` and re-joins on
#: ``"\n"``. Joining the raw texts (either with ``""`` or with ``"\n"``) is wrong:
#: the first drops every line boundary, the second doubles the boundary around an
#: empty line, which already arrives carrying its own ``"\n"``.
EXECD_LINE_EVENT_MODEL_VERSION = "1.1.0"

#: The measurement, in a shape a test can assert:
#: ``(in-box command, execd events, bytes exec_collect reassembles)``.
#: Deliberately a tripwire — if execd changes the semantics, the table test goes
#: red instead of the file panel silently mis-splitting a body from the status
#: code ``curl -w '\n%{http_code}'`` appends.
EXECD_LINE_EVENT_MODEL: tuple[tuple[str, tuple[str, ...], bytes], ...] = (
    # Faithful: interior line boundaries survive.
    (r"printf 'hello\n200'", ("hello", "200"), b"hello\n200"),
    # Faithful: an empty interior line arrives as the "\n" event.
    (r"printf 'a\n\n404'", ("a", "\n", "404"), b"a\n\n404"),
    # NOT faithful: execd emits no event for a TRAILING newline, so it cannot be
    # recovered …
    (r"printf 'only\n'", ("only",), b"only"),
    # … which makes the newline-terminated output above indistinguishable from
    # this one. Both reassemble identically; that ambiguity is inherent to a
    # line-event stream, not something a smarter reader could resolve.
    (r"printf 'no-newline'", ("no-newline",), b"no-newline"),
    # NOT faithful: a CR is consumed with the LF that follows it, so CRLF
    # output arrives as LF.
    (r"printf 'a\r\nb\n200'", ("a", "b", "200"), b"a\nb\n200"),
)

#: The event text that stands for an empty line (see the model above).
_EXECD_EMPTY_LINE_EVENT = "\n"


def collect_execd_stream(messages: Any) -> bytes:
    """Reassemble one execd output stream's line events into UTF-8 bytes.

    Applies :data:`EXECD_LINE_EVENT_MODEL` exactly: each event is a line, an
    event whose text is exactly ``"\\n"`` is an empty line, and the lines are
    re-joined with ``"\\n"``. An empty event list reassembles to ``b""``.
    """
    lines = ["" if str(msg.text) == _EXECD_EMPTY_LINE_EVENT else str(msg.text) for msg in messages]
    return "\n".join(lines).encode("utf-8")


#: Host-side deadline for one execd command round trip. The SDK's execd SSE
#: client deliberately disables its read timeout (a streaming face), so without
#: this bound a command that never terminates — or a half-open host→box TCP
#: connection — hangs the await forever. Every other in-box face is already
#: deadline-bounded (the resident ws transport via bounded_sidecar_operation,
#: the dataplane per request); this closes the execd face. Generous: every
#: in-tree ``exec_collect`` consumer is a short probe/scan, and callers with a
#: tighter budget pass their own.
_EXECD_ROUNDTRIP_TIMEOUT_SECONDS = 60.0

#: Ceiling on one diagnostic report's characters. A ``logs`` report is an
#: unbounded container log; without a cap one operator page-load could pull a
#: multi-gigabyte body through the API process. What is KEPT depends on the
#: scope — see :data:`_DIAGNOSTIC_TAIL_SCOPES` — and a capped report always
#: comes back with ``truncated=True`` so nothing reads a fragment as the whole.
#:
#: The cap is applied WHILE READING (:func:`_read_capped_report`), not to a body
#: already in memory: buffering the whole report and then slicing it would spend
#: exactly the memory the cap exists to refuse.
_DIAGNOSTICS_MAX_CHARS = 256 * 1024

#: Scopes whose report is an append-only STREAM, where the newest lines are the
#: ones an operator is looking for, so a capped report keeps its TAIL. The other
#: scopes (``summary``/``inspect``) are rendered reports that read top-down, so
#: those keep their HEAD.
_DIAGNOSTIC_TAIL_SCOPES = frozenset({"events", "logs"})

#: One line of a rendered report that assigns a value to a name.
#:
#: The name half accepts ANY run of non-space, non-``=`` characters, and that
#: width is the whole point. A pattern that spelled out which characters a name
#: may contain would decide "this is not a variable" for a name it merely did
#: not recognise — ``2FA_TOKEN=…``, ``agent[0].token=…``, a key with a colon in
#: it — and print the value in full, which is the one outcome this pass exists to
#: prevent. Recognition is the allowlists' job below; the pattern's job is only
#: to find the boundary between a name and a value, and it must never be the
#: thing that lets a line through.
#:
#: Still ANCHORED, and the name may hold no whitespace, so prose that happens to
#: contain an ``=`` mid-line (``  Ready: True (reason=N/A)``, ``Requests: {'cpu':
#: '1'}``) is not an assignment and stays readable. The name may hold no ``=``
#: either, so a ``====`` banner is prose rather than an assignment to ``=``.
_ENV_ASSIGNMENT_RE = re.compile(r"^(?P<lead>\s*)(?P<key>[^\s=]+)=(?P<value>.*)$")

#: The ONLY environment variables whose values survive into a diagnostic report.
#:
#: An allowlist, not a denylist, and that choice is the whole point. The
#: OpenSandbox docker runtime renders the container's full ``Environment:``
#: block into its ``inspect`` report (and therefore into ``summary``, which is
#: inspect + events + logs), masking only names containing SECRET / TOKEN /
#: PASSWORD / KEY. That rule is a reasonable default for a general-purpose
#: sandbox server, but it does not know AstraBox's vocabulary, and two of the
#: values the platform injects are secrets whose names contain none of those words:
#:
#: * ``_ASTRABOX_TRANSCRIPT_BACKEND_BASE_URL`` — its path carries a transcript
#:   CAPABILITY token, which grants read/write on that session's transcript.
#: * ``ANTHROPIC_CUSTOM_HEADERS`` — the Langfuse correlation pair, which names
#:   the conversation and the AstraBox user.
#:
#: Listing only known secrets would miss new sensitive variables. The rule is
#: inverted: explicitly safe values remain visible and every other value is
#: returned as ``***``.
#:
#: The Kubernetes runtime's ``inspect`` renders no environment block at all, so
#: on that runtime this pass simply finds nothing to do. It is applied to every
#: scope regardless, because ``logs`` is whatever the process wrote to stdout.
_DIAGNOSTIC_ENV_ALLOWLIST = frozenset(
    {
        # Deployment-wide facts about running inside an AstraBox sandbox at all,
        # with no session in them. The platform includes both on every standard
        # create, including those issued by the SDK client-pool creator.
        "IS_SANDBOX",
        "DISABLE_BROWSER",
        # The model endpoint and model name. Not credentials, and between them
        # they answer the most common question a diagnostics reader has ("what
        # was this box actually talking to?"). The endpoint is a deployment-level
        # setting the operator configured on the Environment and can already read
        # back in /manage.
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        # Ordinary process/image environment.
        "HOME",
        "HOSTNAME",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "PATH",
        "PWD",
        "SHELL",
        "SHLVL",
        "TERM",
        "TZ",
        "USER",
        "DEBIAN_FRONTEND",
        "NODE_ENV",
        "NODE_VERSION",
        "PYTHON_VERSION",
        "PYTHONUNBUFFERED",
    }
)

#: The metadata keys AstraBox writes on every box it creates, which the docker
#: runtime renders as a ``Labels:`` block of ``key=value`` lines.
#:
#: Named here for the same reason the env names above are: the assignment
#: pattern matches them (it matches every ``name=value`` line, by design), so
#: without this they would be redacted along with everything else unrecognised.
#: Both are deployment/session facts an operator already holds — the session id
#: is in the URL they asked this report for — and both are what makes a report
#: identifiable at a glance. A label key AstraBox does not write is not on this
#: list and is redacted like any other unknown name.
_DIAGNOSTIC_LABEL_ALLOWLIST = frozenset(
    {
        # executor.py / pool.py stamp this on create.
        "astrabox.managed-by",
        SANDBOX_ASSIGNMENT_ID_METADATA_KEY,
        SANDBOX_SESSION_ID_METADATA_KEY,
    }
)

#: What a redacted value is replaced with. Matches the marker the OpenSandbox
#: server already uses for the names it masks itself, so a report does not
#: appear to have two different kinds of hidden value.
_REDACTED_VALUE = "***"


def _redact_env_values(text: str) -> str:
    """Keep the value of an allowlisted assignment; blank everything else.

    Diagnostic reports are unstructured text whose format varies by runtime.
    Assignment names remain visible, allowlisted values remain intact, and all
    other assignment values become ``***``.

    Values may contain newlines, and report text provides no trustworthy marker
    for where such a value ends. After a redacted assignment, non-assignment
    prose therefore stays masked through the end of the report. This can hide
    later sections of a combined Docker ``summary``; callers can request the
    separate ``events`` and ``logs`` scopes when they need those sections.

    Each assignment-shaped continuation line is evaluated independently, but
    it never restores prose output. This bounds visible content to names and
    explicitly allowlisted values.
    """
    if not text or "=" not in text:
        return text
    out: list[str] = []
    redacting = False
    for line in text.split("\n"):
        match = _ENV_ASSIGNMENT_RE.match(line)
        if match is not None:
            key = match.group("key")
            if key in _DIAGNOSTIC_ENV_ALLOWLIST or key in _DIAGNOSTIC_LABEL_ALLOWLIST:
                out.append(line)
            else:
                out.append(f"{match.group('lead')}{key}={_REDACTED_VALUE}")
                # Latched, never cleared: the only thing that could clear it is
                # evidence from the text, and the text is what is in doubt.
                redacting = True
            continue
        if not line.strip():
            out.append(line)
            continue
        out.append(_REDACTED_VALUE if redacting else line)
    return "\n".join(out)


#: Host-side deadline for one diagnostics round trip. Larger than the lifecycle
#: request timeout because a report is rendered on demand (a log read, an event
#: query) rather than served from the control plane's own state.
_DIAGNOSTICS_TIMEOUT_SECONDS = 30.0

#: Default page size for the inventory face when the caller names none.
_DEFAULT_LIST_PAGE_SIZE = 50

#: Budget for ``SandboxCreateSpec.wait_for_inbox_service_port``: how long an
#: in-box service gets to bind its port after the box reports ready. Generous
#: because the services this gate covers (an agent gateway, a dashboard) do
#: real startup work — an interpreter, a config load — after the box itself is
#: up, and the gate's whole purpose is to absorb exactly that.
_INBOX_SERVICE_READY_TIMEOUT_SECONDS = 90


async def _read_capped_report(
    response: httpx.Response, *, keep_tail: bool, limit: int
) -> tuple[str, bool]:
    """Read a STREAMED report, holding at most ``limit`` (+ one chunk) characters.

    Returns ``(text, truncated)``. The cap is what keeps an unbounded ``logs``
    body out of this process's memory, so it has to bind on the way IN — reading
    the whole response and then slicing it would already have paid the cost the
    cap is there to refuse.

    Which end survives is the caller's, and each end stops early differently:

    * HEAD (rendered reports, read top-down) — chunks accumulate until the limit
      is passed, and then the read STOPS. The rest of the body is never
      transferred; closing the response mid-stream is exactly what tells the
      server so.
    * TAIL (append-only streams, where the newest lines are the point) — the
      whole body must be seen, because the end is not known until it arrives, but
      only the last ``limit`` characters are kept: the buffer is trimmed on every
      chunk rather than grown.

    A tail that dropped anything is re-aligned to its first NEWLINE, so the
    report starts at a whole line. That is a safety property, not tidiness: the
    redaction pass that runs afterwards reads line by line, and a leading
    fragment cut out of the middle of a redacted value would arrive with the
    ``name=`` half missing and read as ordinary prose. Residue, stated: a kept
    region with no newline in it at all cannot be re-aligned, and is handled by
    the redaction pass as the single unterminated line it is.
    """
    if keep_tail:
        buffer = ""
        dropped = False
        async for chunk in response.aiter_text():
            buffer += chunk
            if len(buffer) > limit:
                dropped = True
                buffer = buffer[-limit:]
        if dropped and "\n" in buffer:
            buffer = buffer.split("\n", 1)[1]
        return buffer, dropped
    parts: list[str] = []
    size = 0
    async for chunk in response.aiter_text():
        parts.append(chunk)
        size += len(chunk)
        if size > limit:
            break
    text = "".join(parts)
    if len(text) > limit:
        return text[:limit], True
    return text, False


def _is_not_found(exc: SandboxApiException) -> bool:
    return exc.status_code == 404


class ReportedExitStatus:
    """The exit status execd named, kept out of the way of what followed it.

    execd reports a process's own exit code as an error's value ("9" for
    `exit 9`) and reports transport trouble the same way with a message. The
    SDK's dispatcher assigns ``execution.error`` on every error event, so a
    drain complaining about the pipe a finished shell closed replaces the exit
    status and the run ends up with no code at all.

    Every event still reaches ``on_error``. A process exits once, so the FIRST
    numeric value is its outcome; anything after it describes the plumbing.
    """

    __slots__ = ("code",)

    def __init__(self) -> None:
        self.code: int | None = None

    def record(self, error: Any) -> None:
        if self.code is not None:
            return
        try:
            self.code = int(str(getattr(error, "value", "")))
        except (TypeError, ValueError):
            return


class OpenSandboxEndpoint:
    """``get_endpoint`` return shape: a full-URL endpoint plus the headers every
    request targeting it must carry (empty for a direct-reach deployment).
    Exposes ``.endpoint`` so it satisfies the seam's ``SandboxEndpointRef``."""

    __slots__ = ("endpoint", "headers")

    def __init__(self, endpoint: str, headers: dict[str, str] | None = None) -> None:
        self.endpoint = str(endpoint)
        self.headers = dict(headers or {})


def _snapshot_items(listing: object) -> list:
    """The snapshots in whatever shape the SDK's list call answered with."""
    if isinstance(listing, dict):
        for key in ("items", "snapshots", "data"):
            value = listing.get(key)
            if isinstance(value, list):
                return value
        return []
    for attr in ("items", "snapshots", "data"):
        value = getattr(listing, attr, None)
        if isinstance(value, list):
            return value
    return list(listing) if isinstance(listing, list) else []


class OpenSandboxHandle:
    """A handle to one OpenSandbox sandbox, wrapping the SDK ``Sandbox``.

    Deliberately does NOT expose a ``.sandbox`` attribute: the runtime's
    ``get_underlying_sandbox`` probes that name to unwrap composite handles, and
    this handle IS the object every consumer should hold — unwrapping must
    return it unchanged. Identity is exposed under both ``sandbox_id`` and
    ``id`` so ``extract_sandbox_id`` resolves without re-guessing.
    """

    __slots__ = ("_sdk", "_endpoints")

    def __init__(self, sdk_sandbox: Sandbox) -> None:
        self._sdk = sdk_sandbox
        # get_endpoint memoization, keyed by in-box port: endpoint routing is
        # stable for a live sandbox, and the consumers (dataplane builds, panel
        # reads) re-resolve per call otherwise.
        self._endpoints: dict[int, OpenSandboxEndpoint] = {}

    @property
    def sidecar_faces(self) -> Sandbox:
        """The SDK object, for the faces this handle deliberately does not wrap.

        The handle exists to keep the SDK's surface out of the rest of the
        codebase, and everything a session needs is wrapped. The egress policy
        and credential vault are used only by the provider's containment face
        (the read-only posture plus gated E2E rule mutation); wrapping those SDK
        methods on the value handle would add a second adapter for one provider
        caller, while reaching into the private slot would hide the dependency.
        This property names it.
        """
        return self._sdk

    @property
    def sandbox_id(self) -> str:
        return str(self._sdk.id)

    @property
    def id(self) -> str:
        """Alias of :attr:`sandbox_id` for the extractor's ``id`` probe."""
        return str(self._sdk.id)

    @property
    def commands(self) -> Any:
        """The SDK ``Commands`` capability, passed through untouched.

        Core command readers dispatch on the SDK's exact signature
        (``run(command, *, opts=..., handlers=...)`` and
        ``interrupt(execution_id)``), so the adapter is the seam.
        """
        return self._sdk.commands

    @property
    def files(self) -> Any:
        """The SDK ``Filesystem`` capability, passed through untouched."""
        return self._sdk.files

    async def get_filesystem(
        self, port: int, *, headers: Mapping[str, str] | None = None
    ) -> Any:
        """Bind the SDK file capability to an already-running conversation service.

        Reuse the sandbox's owned transport. Closing this handle closes that
        transport; the consumer must not close a second copy of the SDK sandbox.
        """
        endpoint = await self.get_endpoint(port)
        address = urlsplit(endpoint.endpoint)
        if address.scheme not in {"http", "https"} or not address.netloc:
            raise ValueError("sandbox filesystem endpoint requires an HTTP(S) URL")
        connection = self._sdk.connection_config.model_copy(
            update={"protocol": address.scheme}
        )
        return AdapterFactory(connection).create_filesystem_service(
            SandboxEndpoint(
                endpoint=address.netloc + address.path.rstrip("/"),
                headers={**endpoint.headers, **dict(headers or {})},
            )
        )

    async def search_file_paths(
        self, path: str, pattern: str, *, port: int,
        headers: Mapping[str, str] | None = None,
    ) -> list[str]:
        """Keep the SDK's search request and result types inside the provider."""
        filesystem = await self.get_filesystem(port, headers=headers)
        entries = await filesystem.search(SearchEntry(path=path, pattern=pattern))
        return [entry.path for entry in entries]

    def _endpoint_error(
        self,
        exc: Exception,
        *,
        operation: str,
        port: int,
        settings: Any,
    ) -> APIError:
        if isinstance(exc, SandboxApiException) and _is_not_found(exc):
            return APIError(
                code="SANDBOX_NOT_FOUND",
                message=(
                    f"open_sandbox {operation}: sandbox {self.sandbox_id!r} not found"
                ),
                status_code=404,
            )
        return APIError(
            code="AGENT_RUNTIME_ERROR",
            message=_config.scrub_secret(
                f"open_sandbox {operation} failed for sandbox "
                f"{self.sandbox_id!r} port {int(port)}: {exc}",
                secret=_config.resolve_api_key(settings),
            ),
            status_code=502,
        )

    async def get_endpoint(self, port: int = IN_BOX_SIDECAR_PORT) -> OpenSandboxEndpoint:
        """Resolve the full-URL endpoint for an in-box port (memoized per port).

        The SDK returns a bare ``host:port``; the scheme comes from the
        deployment's lifecycle base URL (``_config.endpoint_url``). Required
        routing headers are preserved on the returned value.
        """
        key = int(port)
        cached = self._endpoints.get(key)
        if cached is not None:
            return cached
        settings = load_astrabox_settings()
        try:
            raw = await self._sdk.get_endpoint(key)
        except Exception as exc:
            raise self._endpoint_error(
                exc,
                operation="endpoint resolution",
                port=key,
                settings=settings,
            ) from None
        resolved = OpenSandboxEndpoint(
            endpoint=_config.endpoint_url(raw.endpoint, settings=settings),
            headers=dict(raw.headers or {}),
        )
        self._endpoints[key] = resolved
        return resolved

    async def get_signed_endpoint(
        self,
        port: int,
        *,
        expires_at: datetime,
    ) -> OpenSandboxEndpoint:
        """Mint a fresh OSEP-0011 browser URL for one in-box port.

        Signed endpoints are deliberately not cached: their expiry is part of
        the credential and every refresh request must receive a newly minted
        URL with the caller's requested lifetime.
        """
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        settings = load_astrabox_settings()
        try:
            raw = await self._sdk.get_signed_endpoint(
                int(port), int(expires_at.timestamp())
            )
        except Exception as exc:
            raise self._endpoint_error(
                exc,
                operation="signed endpoint resolution",
                port=int(port),
                settings=settings,
            ) from None
        return OpenSandboxEndpoint(
            endpoint=_config.endpoint_url(raw.endpoint, settings=settings),
            headers=dict(raw.headers or {}),
        )

    async def exec_collect(
        self,
        cmd: list[str],
        *,
        max_output: int | None = None,
        timeout: float | None = _EXECD_ROUNDTRIP_TIMEOUT_SECONDS,
    ) -> tuple[int, bytes, bytes]:
        """Run ``cmd`` in the box over execd; return ``(exit_code, stdout, stderr)``.

        TEXT-SAFE ONLY, and specifically LINE-shaped. execd streams output as
        decoded per-LINE text events (:data:`EXECD_LINE_EVENT_MODEL`), so what
        the streams reassemble to is:

        * FAITHFUL for interior line boundaries, empty lines included — the
          reassembly rule is what makes ``curl -w '\\n%{http_code}'`` splittable
          by its last newline;
        * NOT faithful for a TRAILING newline (execd emits no event for it, so
          ``foo\\n`` and ``foo`` reassemble identically) or for ``\\r`` before a
          newline (CRLF arrives as LF);
        * NOT faithful for raw binary — the bytes are a UTF-8 re-encoding of
          decoded text.

        Anything needing exact bytes uses the ``files`` API instead
        (:meth:`put_bytes` to write, ``files.read_bytes`` to read), or stages
        through a file and base64s it back over this face — the contract the
        session file service already follows for uploads and downloads.

        The argv is joined with ``shlex.join`` so the execd shell sees exactly
        one command with exact quoting. A missing exit code is inferred from the
        execd error payload (its ``value`` carries the numeric code for a plain
        non-zero exit); an execd-level error with no numeric code maps to 1 with
        the error text appended to stderr. ``max_output`` (when set) keeps only
        each stream's trailing bytes so a runaway command cannot blow up host
        memory.

        ``timeout`` is a HOST-side wall-clock deadline on the whole round trip
        (the SDK's execd streaming client has no read timeout of its own, so a
        non-terminating in-box command or a dead host→box route would otherwise
        hang forever); ``None`` opts out for a deliberately long-running call.
        Expiry raises ``TimeoutError`` — loud, never a silent stall.
        """
        command = shlex.join(str(part) for part in cmd)
        try:
            async with asyncio.timeout(timeout):
                execution = await self._sdk.commands.run(command)
        except TimeoutError:
            if timeout is None:
                raise
            raise TimeoutError(
                f"open_sandbox execd command did not complete within "
                f"{timeout:g}s: {command[:200]!r}"
            ) from None
        stdout = collect_execd_stream(execution.logs.stdout)
        stderr = collect_execd_stream(execution.logs.stderr)
        code = execution.exit_code
        if code is None:
            error = execution.error
            if error is None:
                code = 0
            else:
                match = _TRAILING_INT_RE.search(str(error.value or ""))
                if match is not None:
                    code = int(match.group(1))
                else:
                    code = 1
                    stderr += f"{error.name}: {error.value}".encode("utf-8")
        if max_output is not None:
            stdout = stdout[-max_output:]
            stderr = stderr[-max_output:]
        return int(code), stdout, stderr

    async def put_bytes(self, path: str, data: bytes) -> None:
        """Write ``data`` verbatim to ``path`` inside the box (binary-safe)."""
        await self._sdk.files.write_file(path, bytes(data))

    async def close(self) -> None:
        """Release the SDK's local HTTP resources (never the remote sandbox)."""
        await self._sdk.close()


class OpenSandboxDataPlane(SandboxDataPlane):
    """Reach one sandbox's in-box HTTP services (the resident control server on
    :8000) directly over host→box TCP.

    Two construction modes mirror the seam's two reach modes:

    * a live :class:`OpenSandboxHandle` — the endpoint is resolved LAZILY on the
      first request (``build_dataplane`` is a synchronous seam, and endpoint
      resolution is an async SDK call; deferring it keeps the seam shape without
      an event-loop bridge) and cached with its routing headers;
    * a full-URL endpoint string — the multi-replica reach; no headers are
      available on this face (v1 scope, enforced loud in ``resolve_endpoint``).

    Transport-level failures raise ``ConnectionError`` (the retryable shape the
    probe/interrupt callers expect); an HTTP error status is DATA, returned as
    the response.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        handle: OpenSandboxHandle | None = None,
        port: int = IN_BOX_SIDECAR_PORT,
    ) -> None:
        if (base_url is None) == (handle is None):
            raise RuntimeError("OpenSandboxDataPlane needs exactly one of base_url / handle")
        self._base_url = str(base_url).rstrip("/") if base_url is not None else None
        self._handle = handle
        self._port = int(port)
        self._headers: dict[str, str] = {}

    async def _resolve(self) -> tuple[str, dict[str, str]]:
        if self._base_url is None:
            assert self._handle is not None  # enforced by __init__
            endpoint = await self._handle.get_endpoint(self._port)
            self._base_url = endpoint.endpoint.rstrip("/")
            self._headers = dict(endpoint.headers)
        return self._base_url, dict(self._headers)

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> SandboxHttpResponse:
        base_url, endpoint_headers = await self._resolve()
        # Per-request headers win on conflict; the endpoint's required routing
        # headers fill the gaps (same precedence as the execd auth transport).
        merged = {**endpoint_headers, **dict(headers or {})}
        url = f"{base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=float(timeout)) as client:
                response = await client.request(
                    str(method or "GET").upper(),
                    url,
                    json=json,
                    headers=merged or None,
                )
        except httpx.HTTPError as exc:
            raise ConnectionError(
                f"open_sandbox in-box HTTP request failed (path={path}): {exc}"
            ) from exc
        return SandboxHttpResponse(response.status_code, response.text)


def workspace_claim_name() -> str:
    """The platform volume every subject's durable workspace is a subPath of.

    One claim for the deployment rather than one per subject, because a pooled
    box is created before the conversation that borrows it: a per-conversation
    claim has no name to use at create time, while a shared claim plus the
    subject's subPath does. Which medium answers for the claim is the
    deployment's choice — the server maps it to a PersistentVolumeClaim on
    Kubernetes and a named volume on Docker.

    Empty means this deployment configured no durable workspace storage. That is
    a real deployment shape (a disposable single-node install), and it is
    answered by refusing to plan mounts rather than by creating a box whose
    files quietly live on the container's overlay — see the caller.
    """

    from astrabox.common.utils.settings import load_astrabox_settings

    return str(
        getattr(load_astrabox_settings(), "sandbox_workspace_volume", "") or ""
    ).strip()


def volume_specs_from_planned_mounts(
    planned: list[tuple[str, str]], *, claim_name: str, create_if_not_exists: bool = True
) -> list[dict[str, Any]]:
    """Express planned mounts in the sandbox backend's create-time vocabulary.

    The one piece with no original to recover: the deleted code emitted the
    previous backend's ``{local_dir, remote_dir, permission}`` specs, and
    OpenSandbox states the same idea as a `Volume` — one backend (`host`, `pvc`
    or `ossfs`), a `mountPath`, and a `subPath` under it. The platform plans
    ``(box_path, storage_subpath)`` and this maps it, which is why the planner
    did not change shape to move backends and why the translation lives here:
    the seam document rules that no medium type appears above a provider.

    `pvc` is the backend chosen here because it is the runtime-neutral one: the
    server maps a claim to a PersistentVolumeClaim on Kubernetes and to a named
    volume on Docker, so one deployment-configured claim serves both without the
    orchestrator learning which it is. Every subject shares that claim and is
    separated by `subPath`, which is what lets a warm box carry it: a pool
    creates boxes before any conversation is known, so a per-conversation claim
    cannot be named at create time, while a shared claim plus the subject's
    subPath can.

    Volume names are Kubernetes DNS labels (`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`,
    ≤63 characters) and a storage subpath is neither, so the name is derived
    from the mount's position rather than from the path: the path itself travels
    in `subPath`, where it is not constrained.
    """

    specs: list[dict[str, Any]] = []
    for index, (box_path, storage_subpath) in enumerate(planned):
        specs.append(
            {
                "name": f"astrabox-workspace-{index}",
                "pvc": {
                    "claimName": claim_name,
                    "createIfNotExists": create_if_not_exists,
                    # The claim outlives every box that mounts it — that is the
                    # whole point of moving these files off the container's
                    # overlay. A box's termination must never take it.
                    "deleteOnSandboxTermination": False,
                },
                "mountPath": box_path,
                "subPath": storage_subpath.lstrip("/"),
                "readOnly": False,
            }
        )
    return specs

def box_is_unreachable(exc: BaseException) -> bool:
    """Whether this failure means the host could not reach the box at all.

    Walks the cause chain for a transport error rather than reading the
    message. The SDK reports a dead route as `SandboxInternalException`, the
    same class it uses for a genuine internal fault, and puts the original
    error in `__cause__` — so the class says nothing and the text is not a
    contract.

    The distinction decides what the caller does. "Your isolated session is not
    there" is a 409 about a session; "this box does not answer" is the box
    being gone, and only the second licenses giving the conversation a new one.
    """

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, httpx.TransportError):
            return True
        current = current.__cause__ or current.__context__
    return False


#: Strong references to give-backs still running after the caller that started
#: them was cancelled. Without this the event loop is the only owner of those
#: tasks and may collect them mid-flight.
class OpenSandboxSandboxProvider(SandboxProvider):
    """OpenSandbox lifecycle-API sandbox provider (the second backend).

    Capability posture (frozen by the flag table test):

    * ``conversation_bootstrap_transport = "sandbox_command_script"`` — the
      in-box runner serves the envelope protocol, not a bootstrap HTTP route,
      so conversation bootstrap goes over the box's command channel.
    * ``uses_create_oss_mounts = True`` — a remote box cannot see host NAS
      paths; this short-circuits every host-mount step in session create.

    All three optional inventory reads are implemented (:meth:`list_sandboxes`,
    :meth:`describe_sandbox`, :meth:`read_diagnostics`): the OpenSandbox control
    plane pages its sandbox list and renders per-sandbox plain-text reports.
    Diagnostics availability is the SERVER's to decide per deployment, so that
    method relays a server refusal rather than deciding here.

    ``transport`` is a test-only injection point (an ``httpx.MockTransport``
    carried into every SDK connection config); production constructs with none.
    """

    name = OPEN_SANDBOX_BACKEND_NAME

    # --- capability flags -------------------------------------------------
    requires_sandbox_object_for_ws = False
    uses_create_oss_mounts = True
    connection_secret_uses_legacy_sandbox_api_key = False
    conversation_bootstrap_transport = "sandbox_command_script"
    requires_https_git = False
    # Endpoints resolved here carry no embedded sandbox identity (host-mode
    # deployments hand out bare host:port), so the endpoint string is never
    # authoritative over the persisted sandbox id.
    endpoint_is_authoritative = False
    assistant_workspace_root_preprovisioned = False
    supports_correlated_create = True
    # The substitution runs in the egress sidecar — the only place that sees a
    # request after the box encrypted it — declared to it as
    # `CredentialSubstitution` under `passthrough` auth.
    # `credential_vault.build_env_credential_vault_write` expresses a seam
    # credential in that match language and REFUSES every allowlist form
    # upstream cannot match exactly (unrestricted, uninterceptable ports,
    # non-leftmost wildcards) rather than binding a looser one, because a
    # binding wider than its owner asked for still works and still passes a
    # smoke test.
    supports_egress_credential_injection = True
    # One create = one box with its own network namespace and fixed in-box
    # ports; a sandbox is never reassigned across profiles.
    sandbox_is_profile_exclusive = True
    # Every create this backend makes carries `timeout` = the deployment's
    # sandbox lease (`create_open_sandbox_box`, and the pool lend alongside it),
    # and both OpenSandbox runtimes terminate a box whose lease lapses. That is
    # what makes a create whose ANSWER is lost a bounded leak rather than an
    # unbounded one, and the pool checks this flag before making such a create
    # at all — the promise is declared where a caller can read it instead of
    # being assumed.
    created_sandboxes_self_expire = True
    supports_create_network_policy = True
    supports_pause = True
    supports_client_pool = True
    # Isolated sessions (OSEP-0013) are an execd feature this backend reaches
    # through the SDK's `sandbox.isolation` adapter. Declaring it says the
    # BACKEND can; whether a given box can is `read_isolation_capability`, and
    # under the per-conversation privilege level the honest answer is no.
    supports_isolated_sessions = True
    supports_isolated_session_recovery = True
    # The pinned create contract maps `advanced` to execd isolation and the
    # running box proves it through `/v1/isolated/capabilities`. Neither the
    # direct-create nor Pool APIs expose evidence for a privileged container.
    supported_permission_levels = (
        SANDBOX_PERMISSION_LEVEL_DEFAULT,
        SANDBOX_PERMISSION_LEVEL_ADVANCED,
    )
    # The unfenced pair (registration validates them together). Hard product
    # decision: this backend never adopts the generation fence.

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        # asyncio pool schedulers are bound to their event loop; Redis is the
        # shared authority across loops and server replicas.
        self._client_pool_registries: dict[int, Any] = {}
        self._client_pool_registries_lock = threading.Lock()

    def _client_pool_registry(self) -> Any:
        loop = asyncio.get_running_loop()
        key = id(loop)
        with self._client_pool_registries_lock:
            registry = self._client_pool_registries.get(key)
            if registry is None:
                from astrabox.providers.open_sandbox.agent_pool import (
                    OpenSandboxClientPoolRegistry,
                )

                registry = OpenSandboxClientPoolRegistry(transport=self._transport)
                self._client_pool_registries[key] = registry
            return registry

    async def shutdown_current_loop_resources(self) -> None:
        """Stop this loop's pool scheduler and finish by-id releases."""
        from astrabox.providers.open_sandbox import executor as executor_module

        loop = asyncio.get_running_loop()
        key = id(loop)
        with self._client_pool_registries_lock:
            registry = self._client_pool_registries.pop(key, None)
        if registry is not None:
            await registry.shutdown()
        releases = tuple(
            task
            for task in executor_module._RELEASES_IN_FLIGHT
            if task.get_loop() is loop
        )
        await asyncio.gather(*releases, return_exceptions=True)

    async def ensure_client_pool(
        self,
        spec: SandboxClientPoolSpec,
        *,
        creator: SandboxClientPoolCreator,
        preparer: SandboxClientPoolPreparer,
    ) -> None:
        await self._client_pool_registry().ensure(
            spec,
            creator=creator,
            preparer=preparer,
        )

    async def acquire_client_pool(
        self,
        spec: SandboxClientPoolSpec,
    ) -> OpenSandboxHandle | None:
        acquired = await self._client_pool_registry().acquire(spec)
        if acquired is None:
            return None
        if not isinstance(acquired, OpenSandboxHandle):
            raise TypeError("OpenSandbox client pool returned a foreign handle")
        return acquired

    async def describe_client_pool(
        self,
        pool_name: str,
    ) -> SandboxClientPoolStatus:
        return await self._client_pool_registry().describe(pool_name)

    async def retire_client_pool(self, pool_name: str) -> None:
        await self._client_pool_registry().retire(pool_name)

    def owns_unclaimed_sandbox(self, descriptor: SandboxDescriptor) -> bool:
        metadata = descriptor.metadata
        return (
            metadata.get(SANDBOX_MANAGED_BY_METADATA_KEY)
            == SANDBOX_MANAGED_BY_METADATA_VALUE
            and is_client_pool_identity(
                str(metadata.get(SANDBOX_SESSION_ID_METADATA_KEY) or ""),
                str(metadata.get(SANDBOX_ASSIGNMENT_ID_METADATA_KEY) or ""),
            )
        )

    # --- SDK plumbing (one object per op, closed in finally) ---------------
    def _sdk_connection_config(
        self,
        settings: Any,
        *,
        use_server_proxy_override: bool | None = None,
    ) -> Any:
        return _config.sdk_connection_config(
            settings,
            transport=self._transport,
            use_server_proxy_override=use_server_proxy_override,
        )

    @staticmethod
    def _settings() -> Any:
        # The by-id lifecycle seam passes no settings; resolve the deployment
        # configuration the way the runtime does (uncached, env-driven).
        return load_astrabox_settings()

    def _api_error(
        self,
        exc: Exception,
        *,
        operation: str,
        sandbox_id: str,
        secret: str | None,
    ) -> APIError:
        if isinstance(exc, SandboxApiException) and _is_not_found(exc):
            # A not-found on CONNECT is the definitive "this instance is gone",
            # and it has to say so in the one word the turn path classifies on.
            # `is_sandbox_gone_error` keys on SANDBOX_GONE; it is what arms the
            # same-send re-borrow, so a generic SANDBOX_NOT_FOUND here silently
            # demotes an out-of-band death to the lapsed-lease backstop — the
            # user's message fails and only the NEXT one gets a box.
            #
            # The typed code is the whole mechanism: nothing downstream
            # re-derives "gone" from the message text, so a provider that
            # returns the generic code disables both gates at once and leaves
            # no trace of having done so.
            #
            # Every other operation keeps the generic 404: an admin describing
            # a bogus id, or a sweep renewing a box that is already reaped, is
            # not a turn discovering its sandbox died.
            if operation == "connect":
                return APIError(
                    code="SANDBOX_GONE",
                    message=(f"open_sandbox {operation}: sandbox {sandbox_id!r} no longer exists"),
                    status_code=404,
                )
            return APIError(
                code="SANDBOX_NOT_FOUND",
                message=(f"open_sandbox {operation}: sandbox {sandbox_id!r} not found"),
                status_code=404,
            )
        return APIError(
            code="AGENT_RUNTIME_ERROR",
            message=_config.scrub_secret(
                f"open_sandbox {operation} failed for sandbox {sandbox_id!r}: {exc}",
                secret=secret,
            ),
            status_code=502,
        )

    async def _get_sandbox_info(
        self,
        sandbox_id: str,
        *,
        settings: Any,
        operation: str,
        secret: str | None,
    ) -> Any:
        manager = await SandboxManager.create(
            connection_config=self._sdk_connection_config(settings)
        )
        try:
            return await manager.get_sandbox_info(str(sandbox_id))
        except APIError:
            raise
        except Exception as exc:
            raise self._api_error(
                exc, operation=operation, sandbox_id=sandbox_id, secret=secret
            ) from exc
        finally:
            await manager.close()

    @staticmethod
    def _require_running(info: Any, *, sandbox_id: str, operation: str) -> None:
        state = str(getattr(info.status, "state", "") or "").strip()
        if state.lower() != "running":
            if operation == "connect":
                # Not running is gone, for a per-session box: identity IS
                # addressing, so nothing restarts this instance in place. A
                # stopped-but-present box and a removed one are the same fact
                # to every caller, so they carry the same code.
                raise APIError(
                    code="SANDBOX_GONE",
                    message=(
                        f"open_sandbox {operation}: sandbox {sandbox_id!r} is not "
                        f"RUNNING (state={state!r}) and is never restarted in place"
                    ),
                    status_code=404,
                )
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    f"open_sandbox {operation}: sandbox {sandbox_id!r} is not "
                    f"RUNNING (state={state!r})"
                ),
                status_code=502,
            )

    # --- backend-wide create/connect decisions ----------------------------
    def connection_config(
        self,
        *,
        connection_config_cls: Any,
        settings: Any,
        request_timeout_seconds: int | None = None,
        secret_material: str | None = None,
    ) -> Any | None:
        # Self-contained: this package imports the SDK itself, so the caller's
        # class handle and legacy secret_material (which stays None — see
        # connection_secret_uses_legacy_sandbox_api_key=False) are not needed.
        _ = (connection_config_cls, request_timeout_seconds, secret_material)
        return self._sdk_connection_config(settings)

    def secret_material(self, *, settings: Any) -> str:
        # Empty string is legal: an authless local OpenSandbox server has no
        # key, and the base return type is ``str``, not ``str | None``.
        return _config.resolve_api_key(settings) or ""

    def owns_sandbox(self, sandbox: Any) -> bool:
        # Lets ``sandbox_for_sandbox`` reverse-map a live handle to this
        # provider. True exactly for the ``OpenSandboxHandle`` type.
        return isinstance(sandbox, OpenSandboxHandle)

    async def create_sandbox(self, spec: SandboxCreateSpec) -> OpenSandboxHandle:
        """Provision one sandbox from the neutral spec; no agent started.

        Field-by-field, and why:

        * ``session_id`` — written as create metadata
          (``astrabox.session-id``), the reverse-lookup key the control plane
          already reads back from a box.
        * ``assignment_id`` — written as immutable create metadata and queried
          with OpenSandbox's server-side metadata filter before allocation, so
          replay of one durable startup command reattaches its existing box.
        * ``metadata`` — opaque platform facts carried through the lifecycle
          service without provider interpretation. Ownership keys remain
          reserved and are composed by this adapter.
        * ``image`` — the create's image, falling back to the shared image
          contract (:func:`resolve_agent_image`) when the spec names none.
        * ``entrypoint`` — the image's declared boot command, falling back to
          the shared agent-image contract when absent. This is not inferred
          from the image name: runtimes with a different image contract state
          it in the create spec.
        * ``cwd`` — pre-created inside the box at create time, so whatever the
          caller spawns next cannot race the directory into existence.
        * ``env`` — written VERBATIM as the boot environment. Nothing is added:
          the seam defines this field as the boot environment, and a provider
          that quietly injected its own variables would make the neutral spec
          a lie.
        * ``resource_limits`` / ``resource_requests`` — passed independently to
          the SDK. Limits remain the runtime ceiling and requests remain the
          scheduler reservation; collapsing them changes how many boxes a node
          can admit.
        * ``publish_ports`` — NO-OP here, and it is not a gap. Publishing a
          host port is a local-container concept; OpenSandbox resolves an
          address for ANY in-box port on demand
          (``GET /v1/sandboxes/{id}/endpoints/{port}`` behind
          :meth:`OpenSandboxHandle.get_endpoint` / :meth:`resolve_endpoint`),
          and ``CreateSandboxRequest`` has no port field to carry it. Every
          in-tree consumer reaches an in-box service that way — for example,
          the Hermes adapter resolves OpenSandbox's injected execd port before
          opening its PTY — so nothing depends on a host publish.
        * ``wait_for_inbox_service_port`` — honored: the create blocks until
          something inside the box accepts a TCP connection on that port
          (:meth:`_await_inbox_service_ready`), and a timeout kills the box
          rather than returning a handle to a box that will never serve.
        * ``death_callback_url`` — IGNORED, as the seam permits for a provider
          with no such mechanism. The pinned OpenSandbox create contract has
          no callback/webhook or termination-hook field, and
          ``SandboxLifecycle`` only exposes pre-start and periodic commands.
          Neither ordinary create nor the SDK client-pool creator installs a
          termination hook. Recovery is pull-based: the expiration watcher probes eligible
          lapsed or suspect bindings and converges only confirmed terminal
          sandboxes. The callback receivers and in-box script remain available
          for a future or external emitter; their presence is not a delivery
          guarantee (``docs/maintainers/sandbox-death-notification.md``).

        The returned handle owns its own SDK object, so it survives this
        provider instance; a later caller re-binds by id with :meth:`connect`.
        """
        # Lazy import: the executor module pulls the engine SDK + websockets,
        # which a lifecycle-only caller never needs. The box-create itself is
        # agent-free — no agent options, no CLI.
        from astrabox.providers.open_sandbox.executor import create_open_sandbox_box

        translated_vault = _open_sandbox_vault_write(spec.vault_write)
        translated_network = with_vault_binding_allows(
            open_sandbox_network_policy(spec.network_policy), translated_vault
        )
        handle = await create_open_sandbox_box(
            session_id=spec.session_id,
            assignment_id=spec.assignment_id,
            image=str(spec.image or "").strip() or resolve_agent_image(),
            env=dict(spec.env),
            resource_limits=dict(spec.resource_limits),
            resource_requests=dict(spec.resource_requests),
            metadata=dict(spec.metadata),
            cwd=str(spec.cwd or "").strip() or None,
            require_execd_command_stream=bool(spec.requires_command_channel),
            entrypoint=spec.entrypoint,
            transport=self._transport,
            network_policy=translated_network,
            permission_level=spec.permission_level,
            credential_proxy_enabled=bool(spec.credential_proxy_enabled),
            vault_write=translated_vault,
            volumes=volume_specs_from_planned_mounts(
                list(spec.workspace_mounts),
                claim_name=spec.workspace_volume or workspace_claim_name(),
                create_if_not_exists=spec.workspace_volume_create_if_missing,
            )
            if spec.workspace_mounts
            else None,
        )
        if spec.wait_for_inbox_service_port is None:
            return handle
        sandbox_id = handle.sandbox_id
        try:
            await self._await_inbox_service_ready(
                handle, port=int(spec.wait_for_inbox_service_port)
            )
        except BaseException as exc:
            # The claim is MINE by construction and does not need asking: this
            # method created the box moments ago and the caller has not been
            # handed the handle, so nothing else can be holding it.
            #
            # `except BaseException` includes CancelledError — a shutdown lands
            # here inside an ALREADY cancelled task, where the next await is
            # cancelled again and a delete run inline would be cut in half.
            # `destroy_open_sandbox_box` runs the destroy in a shielded task of
            # its own so cancellation cannot interrupt it. When the destroy fails
            # on an ordinary error the raised APIError CARRIES the id, so the
            # box's surviving name is what the startup failure path persists;
            # under cancellation there is no such path left and the id goes to
            # the log instead (see the branch below).
            from astrabox.providers.open_sandbox.executor import (
                destroy_open_sandbox_box,
            )

            with contextlib.suppress(Exception):
                await handle.close()
            destruction = await destroy_open_sandbox_box(self, sandbox_id)
            leaked = destruction.leaked_sandbox_id
            if leaked is None:
                raise
            if not isinstance(exc, Exception):
                # A cancellation stays a cancellation — converting it would tell
                # the shutdown path a request failed. The id goes to the log,
                # which during shutdown is the only reader left.
                logger.error(
                    "open_sandbox create_sandbox: cancelled during readiness and "
                    "the sandbox created for it survives: sandbox=%s (%s)",
                    leaked,
                    destruction.detail,
                )
                raise
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    f"open_sandbox create_sandbox: the in-box service on port "
                    f"{int(spec.wait_for_inbox_service_port)} never became ready "
                    f"({exc}), and the sandbox created for it could not be "
                    f"destroyed: {destruction.detail}"
                ),
                status_code=502,
                data={"sandbox_id": leaked, "leaked_sandbox_id": leaked},
            ) from exc
        return handle

    async def apply_credential_vault(
        self,
        sandbox: Any,
        *,
        vault_write: SandboxEgressCredentialPlan,
        managed_credential_names: tuple[str, ...] = (),
        managed_binding_names: tuple[str, ...] = (),
        create_if_missing: bool = True,
    ) -> None:
        """Create or refresh the egress-side vault on an existing box."""
        if not isinstance(sandbox, OpenSandboxHandle):
            raise APIError(
                code="SANDBOX_CONFIG_INVALID",
                message=(
                    "open_sandbox credential delivery needs an "
                    "OpenSandboxHandle for an existing sandbox"
                ),
                status_code=500,
            )
        from astrabox.providers.open_sandbox.executor import (
            apply_open_sandbox_vault,
        )

        await apply_open_sandbox_vault(
            sandbox,
            vault_write=_open_sandbox_vault_write(vault_write) or ([], []),
            session_id=sandbox.sandbox_id,
            managed_credential_names=managed_credential_names,
            managed_binding_names=managed_binding_names,
            managed_basic_scopes=tuple(item.scope_id for item in vault_write.http_basic),
            create_if_missing=create_if_missing,
        )

    async def adopt_sandbox_identity(
        self,
        sandbox: Any,
        *,
        session_id: str,
        assignment_id: str,
    ) -> None:
        """Move a prepared OpenSandbox box to its platform runtime owner."""

        if not isinstance(sandbox, OpenSandboxHandle):
            raise APIError(
                code="SANDBOX_CONFIG_INVALID",
                message=(
                    "open_sandbox identity adoption needs an "
                    "OpenSandboxHandle for an existing sandbox"
                ),
                status_code=500,
            )
        session = str(session_id or "").strip()
        assignment = str(assignment_id or "").strip()
        if not session or not assignment:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    "prepared sandbox adoption requires both runtime owner and "
                    "durable assignment identity"
                ),
                status_code=500,
            )
        await sandbox.sidecar_faces.patch_metadata(
            {
                SANDBOX_SESSION_ID_METADATA_KEY: session_metadata_value(session),
                SANDBOX_MANAGED_BY_METADATA_KEY: SANDBOX_MANAGED_BY_METADATA_VALUE,
                SANDBOX_ASSIGNMENT_ID_METADATA_KEY: assignment_metadata_value(
                    assignment
                ),
            }
        )

    async def _await_inbox_service_ready(
        self,
        handle: OpenSandboxHandle,
        *,
        port: int,
        timeout_seconds: int = _INBOX_SERVICE_READY_TIMEOUT_SECONDS,
    ) -> None:
        """Block until something in the box accepts TCP on ``port``.

        Readiness is spelled as "the in-box service has BOUND its port", which
        is the only claim that holds for an arbitrary port on an arbitrary
        image — this seam field names a port, not a protocol, so probing an
        HTTP route here would bake one image's contract into the provider.

        Polled from INSIDE the box in ONE execd round trip (a loop in the
        in-box interpreter, not a host-side retry loop): the host-to-box route
        is a separate question from in-box readiness, and one round trip is
        also what keeps a ~90 s gate off the execd face for its whole duration.
        Fails loud on timeout; the caller kills the box.
        """
        probe = (
            "import socket,sys,time\n"
            f"deadline=time.monotonic()+{int(timeout_seconds)}\n"
            "while time.monotonic()<deadline:\n"
            "    try:\n"
            f"        with socket.create_connection(('127.0.0.1',{int(port)}),timeout=2):\n"
            "            sys.exit(0)\n"
            "    except OSError:\n"
            "        time.sleep(0.5)\n"
            "sys.exit(7)\n"
        )
        try:
            exit_code, _stdout, stderr = await handle.exec_collect(
                ["python3", "-c", probe],
                # The in-box loop owns the deadline; the host bound only has to
                # outlast it so a healthy slow poll is never cut off mid-probe.
                timeout=float(timeout_seconds) + _EXECD_ROUNDTRIP_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    f"open_sandbox in-box service on port {int(port)} did not "
                    f"answer the readiness probe (the probe itself did not "
                    f"return): {exc}"
                ),
                status_code=502,
            ) from exc
        if exit_code != 0:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    f"open_sandbox in-box service on port {int(port)} was not "
                    f"listening within {int(timeout_seconds)}s "
                    f"(probe exit={exit_code}, stderr={stderr[-200:]!r})"
                ),
                status_code=502,
            )

    def runtime_defaults(self) -> SandboxRuntimeDefaults:
        return SandboxRuntimeDefaults(
            runtime_image=resolve_agent_image(),
            agent_command=IN_BOX_CLI,
        )

    # --- in-box transport (data plane) ------------------------------------
    def build_dataplane(
        self,
        *,
        sandbox: Any = None,
        endpoint: str | None = None,
        port: int = IN_BOX_SIDECAR_PORT,
    ) -> SandboxDataPlane:
        """Build a plane from a live handle or a persisted endpoint string.

        A live handle wins (it carries the routing headers a bare endpoint
        string cannot); a bare ``host:port`` endpoint is normalized to a full
        URL with the deployment scheme. Fails loud when neither yields an
        address — there is no fallback.
        """
        if isinstance(sandbox, OpenSandboxHandle):
            return OpenSandboxDataPlane(handle=sandbox, port=int(port))
        ep = str(endpoint or "").strip()
        if ep:
            return OpenSandboxDataPlane(base_url=_config.endpoint_url(ep), port=int(port))
        raise RuntimeError(
            "open_sandbox cannot build a dataplane: need an OpenSandboxHandle "
            f"or an endpoint string (sandbox="
            f"{type(sandbox).__name__ if sandbox is not None else None} "
            f"endpoint={endpoint!r})"
        )

    # --- by-id lifecycle ---------------------------------------------------
    async def connect(self, sandbox_id: str) -> OpenSandboxHandle:
        """Bind to an existing RUNNING sandbox by id; return a value handle.

        The info precheck fails fast on a dead box: ``Sandbox.connect`` is
        called with ``skip_health_check=True`` because the SDK's default health
        wait burns the full connect timeout against a terminated sandbox before
        failing — the lifecycle state already answers the question.
        """
        return await self._connect_handle(sandbox_id)

    async def _connect_handle(
        self,
        sandbox_id: str,
        *,
        use_server_proxy_override: bool | None = None,
    ) -> OpenSandboxHandle:
        """Connect with the normal server reach mode or an explicit override."""
        settings = self._settings()
        secret = _config.resolve_api_key(settings)
        info = await self._get_sandbox_info(
            sandbox_id, settings=settings, operation="connect", secret=secret
        )
        self._require_running(info, sandbox_id=sandbox_id, operation="connect")
        try:
            sdk_sandbox = await Sandbox.connect(
                str(sandbox_id),
                connection_config=self._sdk_connection_config(
                    settings,
                    use_server_proxy_override=use_server_proxy_override,
                ),
                skip_health_check=True,
            )
        except Exception as exc:
            raise self._api_error(
                exc, operation="connect", sandbox_id=sandbox_id, secret=secret
            ) from exc
        return OpenSandboxHandle(sdk_sandbox)

    async def discard_snapshots(self, sandbox_id: str) -> int:
        """Delete the snapshots a parked box left behind. Returns how many went.

        Called BEFORE the box is destroyed, and the order is the whole reason this
        exists: deleting a sandbox drops its snapshot RECORDS with it, so once the
        box is gone there is no id left to ask about — while the images those
        records pointed at stay in the registry. Thirty such orphans accumulated in
        a day of pausing, and together with build cache they filled the node's disk
        until kubelet reported DiskPressure, evicted Pods, and every sandbox create
        failed with a network error three layers from the cause.

        Best-effort by contract, not by accident: a snapshot that cannot be listed
        or deleted must not stop a box from being reclaimed — leaking an image is a
        disk-space problem, leaking a running box is a money-and-privacy one. The
        count is what a caller logs; failures are logged here.

        NOTE the registry blobs are a separate matter. Deleting the snapshot through
        the control plane removes its record and the platform's reference to it; the
        image data in the deployment's registry is reclaimed by that registry's own
        garbage collection, which nothing here can drive. That is stated in
        docs/providers/opensandbox.md as the deployment's responsibility.
        """
        target = str(sandbox_id or "").strip()
        if not target:
            return 0
        settings = self._settings()
        secret = _config.resolve_api_key(settings)
        manager = await SandboxManager.create(
            connection_config=self._sdk_connection_config(settings)
        )
        removed = 0
        try:
            try:
                # Filtered by the CONTROL PLANE, not here: the filter face takes the
                # sandbox id, so asking for one box's snapshots is one request whose
                # answer needs no sifting, and a paging bug in local sifting cannot
                # delete a snapshot belonging to another box.
                listing = await manager.list_snapshots(SnapshotFilter(sandbox_id=target))
            except Exception as exc:
                logger.warning(
                    "open_sandbox discard_snapshots: cannot list snapshots for sandbox %s: %s",
                    target,
                    _config.scrub_secret(str(exc), secret=secret),
                )
                return 0
            for snapshot in _snapshot_items(listing):
                snapshot_id = str(
                    getattr(snapshot, "id", "")
                    or getattr(snapshot, "snapshot_id", "")
                    or (snapshot.get("id") if isinstance(snapshot, dict) else "")
                    or ""
                ).strip()
                owner = str(
                    getattr(snapshot, "sandbox_id", "")
                    or (snapshot.get("sandboxId") if isinstance(snapshot, dict) else "")
                    or (snapshot.get("sandbox_id") if isinstance(snapshot, dict) else "")
                    or ""
                ).strip()
                if not snapshot_id or owner != target:
                    continue
                try:
                    await manager.delete_snapshot(snapshot_id)
                    removed += 1
                except Exception as exc:
                    logger.warning(
                        "open_sandbox discard_snapshots: snapshot %s of sandbox %s "
                        "could not be deleted and its image stays in the registry: %s",
                        snapshot_id,
                        target,
                        _config.scrub_secret(str(exc), secret=secret),
                    )
        finally:
            await manager.close()
        if removed:
            logger.info(
                "open_sandbox discard_snapshots: removed %d snapshot(s) of sandbox %s",
                removed,
                target,
            )
        return removed

    async def kill(self, sandbox_id: str) -> bool:
        """Terminate the sandbox; True only when this box is PROVEN gone.

        The server keeps no tombstone — a killed sandbox immediately 404s — so
        a 404 is the shape "already destroyed" arrives in. It is not the ONLY
        thing that shape means: a base URL pointing at the wrong deployment, a
        route prefix that moved, a reverse proxy answering for a path it does
        not have, or an SDK aimed at another server all produce a 404 for a
        request that never reached this sandbox's control plane at all. Read as
        success, those become a released pointer upstream and a live box with
        no name left.

        So a 404 is not the answer here, it is the trigger for the SECOND
        observation: :meth:`probe` asks the control plane whether it still
        knows this sandbox. ``NOT_FOUND`` confirms (the same 404, now as an
        answer to "does it exist" rather than to "delete it"); ``OK`` means the
        box is still there and the delete's 404 came from somewhere else, which
        is False; a probe that could not be answered is no evidence either way
        and raises rather than inventing one.

        Any other delete failure raises.
        """
        settings = self._settings()
        secret = _config.resolve_api_key(settings)
        manager = await SandboxManager.create(
            connection_config=self._sdk_connection_config(settings)
        )
        try:
            await manager.kill_sandbox(str(sandbox_id))
            return True
        except SandboxApiException as exc:
            if not _is_not_found(exc):
                raise self._api_error(
                    exc, operation="kill", sandbox_id=sandbox_id, secret=secret
                ) from exc
            not_found_exc: SandboxApiException = exc
        except Exception as exc:
            raise self._api_error(
                exc, operation="kill", sandbox_id=sandbox_id, secret=secret
            ) from exc
        finally:
            await manager.close()
        return await self._confirm_404_means_gone(
            sandbox_id, not_found_exc=not_found_exc, secret=secret
        )

    async def pause(self, sandbox_id: str) -> bool:
        """Commit the box's filesystem, free its compute; True when PAUSED.

        The control plane does the work asynchronously — the pause call returns
        while a commit Job is still running — so acknowledgement is not evidence.
        This polls until the state settles, because the one caller that matters
        stops renewing the lease on a True and a box that never actually paused
        would then be reclaimed with its workspace.

        A commit that fails leaves the sandbox RUNNING, which is the safe
        outcome and is reported as False rather than raised: the box is still
        usable, the deployment merely did not get to reclaim it.
        """
        settings = self._settings()
        secret = _config.resolve_api_key(settings)
        manager = await SandboxManager.create(
            connection_config=self._sdk_connection_config(settings)
        )
        try:
            await manager.pause_sandbox(str(sandbox_id))
        except Exception as exc:
            raise self._api_error(
                exc, operation="pause", sandbox_id=sandbox_id, secret=secret
            ) from exc
        finally:
            await manager.close()
        return await self._settles_on(sandbox_id, wanted=_PAUSED_STATES, operation="pause")

    async def resume(self, sandbox_id: str) -> bool:
        """Restore a paused box from its snapshot, under the SAME id.

        The resumed Pod boots from the committed image rather than the agent
        image, which is what makes the workspace survive; everything the box was
        RUNNING does not, so callers must treat this as a fresh boot on old
        files.
        """
        settings = self._settings()
        secret = _config.resolve_api_key(settings)
        manager = await SandboxManager.create(
            connection_config=self._sdk_connection_config(settings)
        )
        try:
            await manager.resume_sandbox(str(sandbox_id))
        except Exception as exc:
            raise self._api_error(
                exc, operation="resume", sandbox_id=sandbox_id, secret=secret
            ) from exc
        finally:
            await manager.close()
        return await self._settles_on(sandbox_id, wanted=_RUNNING_STATES, operation="resume")

    async def _settles_on(self, sandbox_id: str, *, wanted: frozenset[str], operation: str) -> bool:
        """Poll until the control plane reports one of ``wanted``, or give up.

        Returns False on the timeout rather than raising: a transition that has
        not finished is not the same as an error, and every caller of pause and
        resume has a correct thing to do with "not yet" (leave the box alone).
        """
        deadline = time.monotonic() + _PAUSE_SETTLE_TIMEOUT_SECONDS
        state = ""
        while time.monotonic() < deadline:
            probe = await self.probe(sandbox_id)
            state = str(probe.sandbox_state or "").upper()
            if state in wanted:
                return True
            if state in _SETTLE_LOST_STATES:
                logger.warning(
                    "open_sandbox %s: sandbox %s settled on %s, which it cannot "
                    "leave for %s — the transition is over and did not happen",
                    operation,
                    sandbox_id,
                    state,
                    "/".join(sorted(wanted)),
                )
                return False
            await asyncio.sleep(_PAUSE_SETTLE_POLL_SECONDS)
        logger.warning(
            "open_sandbox %s: sandbox %s did not settle within %ss (last state=%s)",
            operation,
            sandbox_id,
            _PAUSE_SETTLE_TIMEOUT_SECONDS,
            state or "unknown",
        )
        return False

    async def _confirm_404_means_gone(
        self,
        sandbox_id: str,
        *,
        not_found_exc: SandboxApiException,
        secret: str | None,
    ) -> bool:
        """Second observation behind a delete's 404 (see :meth:`kill`)."""
        probe = await self.probe(sandbox_id)
        if probe.probe_status == SANDBOX_LIFECYCLE_PROBE_NOT_FOUND:
            return True
        if probe.probe_status == SANDBOX_LIFECYCLE_PROBE_OK:
            logger.error(
                "open_sandbox kill: the delete of sandbox %s answered 404 but the "
                "control plane still reports it (state=%s) — the 404 did not come "
                "from this sandbox being gone",
                sandbox_id,
                probe.sandbox_state,
            )
            return False
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=_config.scrub_secret(
                f"open_sandbox kill: the delete of sandbox {sandbox_id!r} answered "
                f"404 ({not_found_exc}), and the confirming probe could not say "
                f"whether the sandbox is gone: {probe.error_text or 'no detail'}",
                secret=secret,
            ),
            status_code=502,
        )

    async def claim_of(
        self, sandbox_id: str, *, expected_session_id: str | None = None
    ) -> SandboxClaim:
        """Whose sandbox is this, read off the ownership metadata the create wrote.

        The create writes two keys (:data:`SANDBOX_MANAGED_BY_METADATA_KEY` and
        :data:`SANDBOX_SESSION_ID_METADATA_KEY`) and the control plane hands
        them back on ``GET /v1/sandboxes/{id}``; the judgement over them lives
        in the seam, not here, so every backend that can answer answers the
        same way.

        A control plane that could not be asked yields ``UNKNOWN``, not
        ``UNCLAIMED``: an unreachable server is not a sandbox without an owner.
        A 404 is the one case where ownership genuinely does not apply —
        there is no box left to own — and is reported as ``UNCLAIMED`` with
        that as its stated reason.
        """
        target = str(sandbox_id or "").strip()
        if not target:
            return SandboxClaim.unknown(
                target, detail="open_sandbox was asked whose an empty sandbox id is"
            )
        settings = self._settings()
        secret = _config.resolve_api_key(settings)
        try:
            info = await self._get_sandbox_info(
                target, settings=settings, operation="claim_of", secret=secret
            )
        except APIError as exc:
            if exc.status_code == 404:
                return SandboxClaim.unclaimed(
                    target,
                    detail=(
                        "this control plane has no such sandbox, so there is "
                        "nothing left to attribute"
                    ),
                )
            return SandboxClaim.unknown(
                target,
                detail=(f"the control plane could not be asked about this sandbox: {exc.message}"),
            )
        except Exception as exc:  # noqa: BLE001 - an unanswerable question is UNKNOWN
            return SandboxClaim.unknown(
                target,
                detail=_config.scrub_secret(
                    "the control plane could not be asked about this sandbox: "
                    f"{type(exc).__name__}: {exc}",
                    secret=secret,
                ),
            )
        expected = str(expected_session_id or "").strip()
        claim = claim_from_metadata(
            sandbox_id=target,
            metadata=dict(getattr(info, "metadata", None) or {}),
            expected_session_id=(
                session_metadata_value(expected) if expected else None
            ),
            session_id_key=SANDBOX_SESSION_ID_METADATA_KEY,
            managed_by_key=SANDBOX_MANAGED_BY_METADATA_KEY,
            managed_by_value=SANDBOX_MANAGED_BY_METADATA_VALUE,
        )
        if claim.may_destroy and expected:
            return dataclasses.replace(claim, session_id=expected)
        return claim

    async def renew(self, sandbox_id: str, ttl_seconds: int) -> datetime | None:
        """Extend the TTL to now+ttl (absolute), monotonically; return the expiry.

        ``expires_at >= now+ttl`` short-circuits — renew never SHORTENS a lease
        (the activity-renew loop calls this on every lapse check). Otherwise the
        public ``renew_sandbox`` API sets exactly now+ttl and its response is
        the authoritative new expiry.
        """
        settings = self._settings()
        secret = _config.resolve_api_key(settings)
        info = await self._get_sandbox_info(
            sandbox_id, settings=settings, operation="renew", secret=secret
        )
        ttl = timedelta(seconds=int(ttl_seconds))
        target = datetime.now(timezone.utc) + ttl
        current = info.expires_at
        # Manual-cleanup sandboxes have no scheduled expiry to extend.
        if current is None:
            return None
        if isinstance(current, datetime):
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            if current >= target:
                return current
        manager = await SandboxManager.create(
            connection_config=self._sdk_connection_config(settings)
        )
        try:
            response = await manager.renew_sandbox(str(sandbox_id), ttl)
        except Exception as exc:
            raise self._api_error(
                exc, operation="renew", sandbox_id=sandbox_id, secret=secret
            ) from exc
        finally:
            await manager.close()
        return response.expires_at

    async def expires_at(self, sandbox_id: str) -> datetime | None:
        """The current expiry from the control plane; None only when the server
        reports no scheduled termination. A missing sandbox raises (404 →
        ``SANDBOX_NOT_FOUND``) — never swallowed into a None the expiration
        watcher would misread as "no lease"."""
        settings = self._settings()
        secret = _config.resolve_api_key(settings)
        info = await self._get_sandbox_info(
            sandbox_id, settings=settings, operation="expires_at", secret=secret
        )
        value = info.expires_at
        return value if isinstance(value, datetime) else None

    async def probe(self, sandbox_id: str) -> SandboxLifecycleProbeResult:
        """Control-plane state → one of the three probe constants; never raises.

        A successfully read state maps to OK with the lowercased state string —
        the interaction broker's terminal-state set speaks exactly this
        vocabulary (``terminated``/``paused``/…), so a dead-but-present box
        converges through the state, not through a probe failure. 404 →
        NOT_FOUND; any transport/config failure → PROBE_FAILED with NO state
        (transient — never terminal).
        """
        secret: str | None = None
        try:
            settings = self._settings()
            secret = _config.resolve_api_key(settings)
            manager = await SandboxManager.create(
                connection_config=self._sdk_connection_config(settings)
            )
            try:
                info = await manager.get_sandbox_info(str(sandbox_id))
            finally:
                await manager.close()
        except SandboxApiException as exc:
            if _is_not_found(exc):
                return SandboxLifecycleProbeResult(
                    probe_status=SANDBOX_LIFECYCLE_PROBE_NOT_FOUND,
                )
            return SandboxLifecycleProbeResult(
                probe_status=SANDBOX_LIFECYCLE_PROBE_FAILED,
                error_text=_config.scrub_secret(str(exc), secret=secret),
            )
        except Exception as exc:
            # Same scrub as the branch above: the SDK wraps transport and
            # parsing failures in non-API exception types whose text can carry
            # the same echoed request headers — the invariant does not care
            # which exception class delivered the body.
            return SandboxLifecycleProbeResult(
                probe_status=SANDBOX_LIFECYCLE_PROBE_FAILED,
                error_text=_config.scrub_secret(str(exc), secret=secret),
            )
        state = str(getattr(info.status, "state", "") or "").strip().lower()
        if not state:
            return SandboxLifecycleProbeResult(
                probe_status=SANDBOX_LIFECYCLE_PROBE_FAILED,
                error_text=f"sandbox {sandbox_id!r} returned an empty lifecycle state",
            )
        return SandboxLifecycleProbeResult(
            probe_status=SANDBOX_LIFECYCLE_PROBE_OK,
            sandbox_state=state,
        )

    async def resolve_endpoint(self, sandbox_id: str, port: int) -> str | None:
        """Full in-box port URL by id (the multi-replica / post-restart reach).

        Connects transiently (info precheck + ``skip_health_check``), resolves
        the endpoint, and closes. An endpoint that REQUIRES routing headers
        cannot be represented on this string-only face — that is a loud error,
        not a silently header-stripped URL (v1 scope cut).
        """
        handle = await self.connect(sandbox_id)
        try:
            endpoint = await handle.get_endpoint(int(port))
        except APIError:
            raise
        except Exception as exc:
            settings = self._settings()
            raise self._api_error(
                exc,
                operation="resolve_endpoint",
                sandbox_id=sandbox_id,
                secret=_config.resolve_api_key(settings),
            ) from exc
        finally:
            await handle.close()
        if endpoint.headers:
            raise APIError(
                code="SANDBOX_ENDPOINT_HEADERS_UNSUPPORTED",
                message=(
                    f"open_sandbox endpoint for sandbox {sandbox_id!r} port "
                    f"{int(port)} requires routing headers, which the "
                    "endpoint-string face cannot carry (v1 supports "
                    "direct-reach deployments only)"
                ),
                status_code=502,
            )
        return endpoint.endpoint

    async def resolve_browser_endpoint(
        self,
        sandbox_id: str,
        port: int,
        *,
        expires_at: datetime | None = None,
    ) -> SandboxBrowserEndpoint | None:
        """Resolve OpenSandbox's native execd/gateway URL by sandbox id.

        Docker returns execd's ``/proxy/{port}`` URL. Kubernetes Secure Access
        uses the SDK's signed-endpoint operation, which produces an OSEP-0011
        URI or wildcard route suitable for direct browser navigation.
        """
        # The service process may use the lifecycle relay for commands and
        # files, but that address can be private to the AstraBox container. Ask
        # OpenSandbox for its public execd/ingress route for browser navigation.
        # Signed routes also reject use_server_proxy at the upstream API.
        handle = await self._connect_handle(
            sandbox_id,
            use_server_proxy_override=False,
        )
        try:
            if expires_at is None:
                endpoint = await handle.get_endpoint(int(port))
            else:
                endpoint = await handle.get_signed_endpoint(int(port), expires_at=expires_at)
        except APIError:
            raise
        except Exception as exc:
            settings = self._settings()
            raise self._api_error(
                exc,
                operation="resolve_browser_endpoint",
                sandbox_id=sandbox_id,
                secret=_config.resolve_api_key(settings),
            ) from exc
        finally:
            await handle.close()
        return SandboxBrowserEndpoint(
            endpoint=endpoint.endpoint,
            headers=dict(endpoint.headers),
            expires_at=expires_at,
            signed=expires_at is not None,
        )

    # --- control-plane inventory (the read-only ops face) ------------------
    @staticmethod
    def _descriptor(info: Any) -> SandboxDescriptor:
        """One control-plane ``SandboxInfo`` → the seam's neutral descriptor.

        A pure rename of fields. Nothing is inferred and nothing is filled in:
        whatever the control plane reports for a sandbox's image, entrypoint or
        state is what an operator sees, because the whole point of this face is
        to show the backend's own answer rather than a reinterpretation of it.
        """
        metadata = dict(getattr(info, "metadata", None) or {})
        image_spec = getattr(info, "image", None)
        image = str(getattr(image_spec, "image", "") or "").strip() or None
        session_id = str(metadata.get(SANDBOX_SESSION_ID_METADATA_KEY) or "").strip() or None
        return SandboxDescriptor(
            sandbox_id=str(info.id),
            state=str(getattr(info.status, "state", "") or ""),
            created_at=getattr(info, "created_at", None),
            expires_at=getattr(info, "expires_at", None),
            image=image,
            entrypoint=tuple(str(part) for part in (getattr(info, "entrypoint", None) or ())),
            metadata=metadata,
            session_id=session_id,
        )

    async def list_sandboxes(
        self, *, page: int = 1, page_size: int = _DEFAULT_LIST_PAGE_SIZE
    ) -> SandboxPage:
        """One page of the control plane's sandbox inventory.

        Paged at the source (``GET /v1/sandboxes`` answers ``items`` +
        ``pagination``); the server's own counters ride back on the page, so a
        caller advances by asking for the next ``page`` instead of guessing
        whether it has seen everything. Unfiltered on purpose: this is "what is
        this backend running", which includes boxes this deployment never
        created — the ones with no ``astrabox.session-id`` metadata.
        """
        settings = self._settings()
        secret = _config.resolve_api_key(settings)
        manager = await SandboxManager.create(
            connection_config=self._sdk_connection_config(settings)
        )
        try:
            paged = await manager.list_sandbox_infos(
                SandboxFilter(page=int(page), page_size=int(page_size))
            )
        except Exception as exc:
            # Not routed through _api_error: that mapping is per-sandbox (its
            # 404 branch means "this sandbox is gone"), and a listing has no
            # sandbox to be missing.
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=_config.scrub_secret(
                    f"open_sandbox list_sandboxes failed: {exc}", secret=secret
                ),
                status_code=502,
            ) from exc
        finally:
            await manager.close()
        pagination = paged.pagination
        return SandboxPage(
            items=tuple(self._descriptor(info) for info in paged.sandbox_infos),
            page=int(pagination.page),
            page_size=int(pagination.page_size),
            total_items=int(pagination.total_items),
            total_pages=int(pagination.total_pages),
            has_next_page=bool(pagination.has_next_page),
        )

    async def find_sandbox_by_assignment(
        self,
        assignment_id: str,
    ) -> SandboxDescriptor | None:
        """Resolve one create attempt through OpenSandbox's metadata filter.

        Two results are corruption, not a choice: an assignment identifies one
        physical create attempt, so selecting either duplicate would make a
        later cleanup nondeterministic. The deployment marker is included in
        the server-side filter and the returned metadata is checked again;
        provider filtering narrows candidates but is never ownership proof.
        """

        assignment = str(assignment_id or "").strip()
        if not assignment:
            raise ValueError("sandbox assignment_id must be non-empty")
        target = assignment_metadata_value(assignment)
        settings = self._settings()
        secret = _config.resolve_api_key(settings)
        manager = await SandboxManager.create(
            connection_config=self._sdk_connection_config(settings)
        )
        try:
            paged = await manager.list_sandbox_infos(
                SandboxFilter(
                    metadata={
                        SANDBOX_ASSIGNMENT_ID_METADATA_KEY: target,
                        SANDBOX_MANAGED_BY_METADATA_KEY: SANDBOX_MANAGED_BY_METADATA_VALUE,
                    },
                    page=1,
                    page_size=2,
                )
            )
        except Exception as exc:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=_config.scrub_secret(
                    "open_sandbox correlated-create lookup failed for "
                    f"assignment {assignment!r}: {exc}",
                    secret=secret,
                ),
                status_code=502,
            ) from exc
        finally:
            await manager.close()

        matches = [
            self._descriptor(info)
            for info in paged.sandbox_infos
            if str(
                (getattr(info, "metadata", None) or {}).get(
                    SANDBOX_ASSIGNMENT_ID_METADATA_KEY
                )
                or ""
            ).strip()
            == target
            and str(
                (getattr(info, "metadata", None) or {}).get(
                    SANDBOX_MANAGED_BY_METADATA_KEY
                )
                or ""
            ).strip()
            == SANDBOX_MANAGED_BY_METADATA_VALUE
        ]
        if not matches:
            return None
        if len(matches) != 1 or int(paged.pagination.total_items) != 1:
            raise APIError(
                code="SANDBOX_ASSIGNMENT_AMBIGUOUS",
                message=(
                    "OpenSandbox reports more than one sandbox for create "
                    f"assignment {assignment!r}; refusing to choose"
                ),
                status_code=409,
            )
        return matches[0]

    async def describe_sandbox(self, sandbox_id: str) -> SandboxDescriptor:
        """The control plane's description of one sandbox (404 → SANDBOX_NOT_FOUND)."""
        settings = self._settings()
        info = await self._get_sandbox_info(
            sandbox_id,
            settings=settings,
            operation="describe_sandbox",
            secret=_config.resolve_api_key(settings),
        )
        descriptor = self._descriptor(info)
        # Best-effort, and never fatal: the detail read must still answer when
        # the box is gone or the control plane will not resolve it. Absent beats
        # guessed — see SandboxDescriptor.endpoint.
        try:
            handle = await self.connect(sandbox_id)
        except Exception:
            return descriptor
        try:
            resolved = await handle.get_endpoint()
        except Exception:
            return descriptor
        endpoint = str(getattr(resolved, "endpoint", "") or "").strip()
        return (
            dataclasses.replace(descriptor, endpoint=endpoint or None) if endpoint else descriptor
        )

    async def read_security_posture(self, sandbox_id: str) -> SandboxSecurityPosture:
        """Ask the BOX what it is contained by — the sidecar's own policy and vault.

        Two reads against the box's egress sidecar, not against the control
        plane: the lifecycle API's ``SandboxInfo`` reports no containment at all,
        and an answer derived from what AstraBox asked for could not tell an
        operator whether the request took effect, which is the only question
        worth asking.

        No sidecar is not an error. A box created without a network policy has
        none, and the honest report of that is ``available=False`` carrying the
        reason — an operator reading "this box has no egress sidecar" learns
        something; one reading an empty panel learns nothing and may assume the
        better of the two possibilities.

        Credentials are reported by NAME. The vault never returns a stored value
        — write-only by construction — and this would not pass one on if it did.
        """
        settings = self._settings()
        secret = _config.resolve_api_key(settings)
        try:
            handle = await self.connect(sandbox_id)
        except APIError:
            raise
        except Exception as exc:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=_config.scrub_secret(
                    f"open_sandbox could not reach sandbox {sandbox_id!r}: {exc}",
                    secret=secret,
                ),
                status_code=502,
            ) from exc

        try:
            underlying: Any = getattr(handle, "sidecar_faces", None) or handle
            try:
                policy = await underlying.get_egress_policy()
            except Exception as exc:
                return SandboxSecurityPosture(
                    sandbox_id=sandbox_id,
                    available=False,
                    detail=_config.scrub_secret(
                        f"no egress sidecar answered for this sandbox: {exc}", secret=secret
                    ),
                )

            rules = tuple(
                (str(getattr(rule, "action", "") or ""), str(getattr(rule, "target", "") or ""))
                for rule in (getattr(policy, "egress", None) or ())
            )
            credentials: tuple[str, ...] = ()
            bindings: tuple[str, ...] = ()
            with contextlib.suppress(Exception):
                credentials = tuple(
                    str(getattr(item, "name", "") or "")
                    for item in await underlying.credential_vault.list_credentials()
                )
            with contextlib.suppress(Exception):
                bindings = tuple(
                    str(getattr(item, "name", "") or "")
                    for item in await underlying.credential_vault.list_bindings()
                )
            return SandboxSecurityPosture(
                sandbox_id=sandbox_id,
                available=True,
                default_action=str(getattr(policy, "default_action", "") or "") or None,
                egress_rules=rules,
                credential_names=credentials,
                binding_names=bindings,
            )
        finally:
            with contextlib.suppress(Exception):
                await handle.close()

    async def patch_egress_rules(
        self,
        sandbox_id: str,
        *,
        rules: tuple[tuple[str, str], ...],
    ) -> None:
        """Merge rules through the connected box's OpenSandbox egress face."""
        handle = await self.connect(sandbox_id)
        try:
            underlying: Any = getattr(handle, "sidecar_faces", None) or handle
            await underlying.patch_egress_rules(
                [
                    NetworkRule.model_validate({"action": action, "target": target})
                    for action, target in rules
                ]
            )
        except APIError:
            raise
        except Exception as exc:
            settings = self._settings()
            raise self._api_error(
                exc,
                operation="patch_egress_rules",
                sandbox_id=sandbox_id,
                secret=_config.resolve_api_key(settings),
            ) from exc
        finally:
            with contextlib.suppress(Exception):
                await handle.close()

    async def delete_egress_rules(
        self,
        sandbox_id: str,
        *,
        targets: tuple[str, ...],
    ) -> None:
        """Delete targets through the connected box's OpenSandbox egress face."""
        handle = await self.connect(sandbox_id)
        try:
            underlying: Any = getattr(handle, "sidecar_faces", None) or handle
            await underlying.delete_egress_rules(list(targets))
        except APIError:
            raise
        except Exception as exc:
            settings = self._settings()
            raise self._api_error(
                exc,
                operation="delete_egress_rules",
                sandbox_id=sandbox_id,
                secret=_config.resolve_api_key(settings),
            ) from exc
        finally:
            with contextlib.suppress(Exception):
                await handle.close()

    async def read_isolation_capability(self, sandbox_id: str) -> SandboxIsolationCapability:
        """Ask the BOX whether it can host isolated sessions (OSEP-0013).

        Never derived from configuration. Whether bwrap can create a
        namespace depends on what the Pod template granted the container: the
        same image answers differently under two templates. A default one
        reports ``available=False`` with "Creating new namespace failed:
        Operation not permitted", and one granting BOTH ``CAP_SYS_ADMIN`` and
        an unconfined AppArmor profile reports ``available=True``. Either
        grant alone still fails, at ``bwrap: Failed to make / slave``.

        A no is an answer, not an error — it is what a box in the
        per-conversation privilege level correctly says about itself.
        """
        settings = self._settings()
        secret = _config.resolve_api_key(settings)
        try:
            handle = await self.connect(sandbox_id)
        except APIError:
            raise
        except Exception as exc:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=_config.scrub_secret(
                    f"open_sandbox could not reach sandbox {sandbox_id!r}: {exc}",
                    secret=secret,
                ),
                status_code=502,
            ) from exc
        try:
            underlying: Any = getattr(handle, "sidecar_faces", None) or handle
            try:
                reported = await underlying.isolation.capabilities()
            except Exception as exc:
                return SandboxIsolationCapability(
                    sandbox_id=sandbox_id,
                    available=False,
                    detail=_config.scrub_secret(
                        f"this box did not answer the isolation capability probe: {exc}",
                        secret=secret,
                    ),
                )
            return SandboxIsolationCapability(
                sandbox_id=sandbox_id,
                available=bool(getattr(reported, "available", False)),
                isolator=str(getattr(reported, "isolator", "") or "") or None,
                version=str(getattr(reported, "version", "") or "") or None,
                setpriv_available=bool(getattr(reported, "setpriv_available", False)),
                userns_available=bool(getattr(reported, "userns_available", False)),
                commit_supported=bool(getattr(reported, "commit_supported", False)),
                diff_supported=bool(getattr(reported, "diff_supported", False)),
                detail=str(getattr(reported, "message", "") or "") or None,
            )
        finally:
            with contextlib.suppress(Exception):
                await handle.close()

    async def read_memory_headroom(self, sandbox_id: str) -> tuple[int, int]:
        """The box's own memory limit and current use, in bytes.

        Asked of the BOX for the same reason its isolation capability is: the
        limit belongs to the Pod template a deployment wrote, and a second copy
        of the number here would be a second source of truth for a fact the
        kernel already holds.

        `(0, 0)` when the box will not answer. A caller deciding admission on
        that reads it as "no headroom proven", which is the safe direction: the
        cost of refusing a conversation is one refusal, and the cost of admitting
        one too many is the whole container.
        """

        try:
            handle = await self.connect(sandbox_id)
        except Exception as exc:  # noqa: BLE001 — an unreachable box has no room
            logger.info(
                "open_sandbox: box %s did not answer the memory probe: %s",
                sandbox_id,
                exc,
            )
            return (0, 0)
        try:
            # Through `exec_collect`, like every other in-box probe here. The
            # SDK's `commands.run` does not return the output on the result: it
            # returns an execution whose bytes are behind `logs.stdout`, and
            # `exec_collect` is where this module already knows to drain it.
            # Reading `result.stdout` / `result.output` instead yields None from
            # both, so the box "answered" with the empty string — reported as no
            # headroom, so the mistake showed up as "never packs" rather than as
            # an error, on every box, for the whole run.
            _code, stdout, _stderr = await handle.exec_collect(
                ["cat", "/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current"]
            )
            text = stdout.decode("utf-8", "replace")
            # `memory.max` is the literal `max` when the template set no limit.
            # Not a number, so this reads as no headroom proven and the box is
            # not packed — deliberate: without a limit the container cannot be
            # reasoned about, and the node, not the container, would pay.
            numbers = [
                int(line.strip())
                for line in text.splitlines()
                if line.strip().isdigit()
            ]
            if len(numbers) < 2:
                logger.info(
                    "open_sandbox: box %s answered the memory probe with %r",
                    sandbox_id,
                    text[:200],
                )
                return (0, 0)
            return (numbers[0], numbers[1])
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "open_sandbox: box %s memory probe failed: %s", sandbox_id, exc
            )
            return (0, 0)
        finally:
            with contextlib.suppress(Exception):
                await handle.close()

    async def read_listening_ports(self, sandbox_id: str) -> set[int]:
        """The TCP ports something in this box is already listening on.

        Asked of the box for the same reason its memory limit is: what is
        bound inside it is the kernel's fact, not a number the platform can
        keep a second copy of. Read from ``/proc/net/tcp`` rather than a tool
        (``ss``/``netstat`` are not in every engine image), where state ``0A``
        is LISTEN and the local address is ``HEX_IP:HEX_PORT``.

        Connection and probe failures return an empty set. The caller then
        proceeds without a collision precheck; this result does not prove that
        the sandbox has no listeners.
        """

        try:
            handle = await self.connect(sandbox_id)
        except Exception as exc:  # noqa: BLE001 — probe failure permits placement
            logger.info(
                "open_sandbox: box %s did not answer the listen probe: %s",
                sandbox_id,
                exc,
            )
            return set()
        try:
            _code, stdout, _stderr = await handle.exec_collect(
                ["cat", "/proc/net/tcp", "/proc/net/tcp6"]
            )
            ports: set[int] = set()
            for line in stdout.decode("utf-8", "replace").splitlines():
                fields = line.split()
                if len(fields) < 4 or fields[3] != "0A":
                    continue
                local = fields[1].rsplit(":", 1)
                if len(local) != 2:
                    continue
                try:
                    ports.add(int(local[1], 16))
                except ValueError:
                    continue
            return ports
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "open_sandbox: box %s listen probe failed: %s", sandbox_id, exc
            )
            return set()
        finally:
            with contextlib.suppress(Exception):
                await handle.close()

    async def count_live_isolated_sessions(self, sandbox_id: str) -> int:
        """How many isolated sessions the box itself still holds.

        The last word before a box is destroyed. Every platform-side lens on
        occupancy is a ledger written at some point in a placement, so each
        one has a window where a conversation is real and not yet visible
        there; the box's own session table has no such window, because a
        session exists exactly when execd holds it.

        ``-1`` when the box cannot be asked, which a caller reads as "no
        answer" and must not treat as empty: the whole point is that this is
        the check standing between a live conversation and a deleted box.
        """

        try:
            handle = await self.connect(sandbox_id)
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "open_sandbox: box %s did not answer the session census: %s",
                sandbox_id,
                exc,
            )
            return -1
        try:
            underlying: Any = getattr(handle, "sidecar_faces", None) or handle
            sessions = await underlying.isolation.list()
            return sum(
                1
                for item in sessions or []
                if str(getattr(item, "status", "") or "").strip().lower() == "active"
            )
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "open_sandbox: box %s session census failed: %s", sandbox_id, exc
            )
            return -1
        finally:
            with contextlib.suppress(Exception):
                await handle.close()

    async def find_isolated_sessions_by_workspace(
        self,
        sandbox_id: str,
        *,
        workspace_source_dir: str,
    ) -> tuple[SandboxIsolatedSession, ...]:
        """Recover active execd sessions from their platform workspace path."""

        target = str(workspace_source_dir or "").strip().rstrip("/")
        if not target.startswith("/"):
            raise APIError(
                code="CONVERSATION_IDENTITY_INVALID",
                message="isolated-session recovery requires an absolute workspace source",
                status_code=500,
            )
        settings = self._settings()
        secret = _config.resolve_api_key(settings)
        handle = await self.connect(sandbox_id)
        try:
            underlying: Any = getattr(handle, "sidecar_faces", None) or handle
            summaries = await underlying.isolation.list()
            found: list[SandboxIsolatedSession] = []
            for summary in summaries or []:
                if str(getattr(summary, "status", "") or "").strip().lower() != "active":
                    continue
                session_id = str(
                    getattr(summary, "session_id", "") or ""
                ).strip()
                if not session_id:
                    continue
                try:
                    session = await underlying.isolation.attach(session_id)
                except SandboxApiException as exc:
                    if _is_not_found(exc):
                        continue
                    raise self._api_error(
                        exc,
                        operation="recover isolated session",
                        sandbox_id=sandbox_id,
                        secret=secret,
                    ) from exc
                info = getattr(session, "info", None)
                workspace = getattr(info, "workspace", None)
                source = str(getattr(workspace, "path", "") or "").strip().rstrip(
                    "/"
                )
                if not source:
                    raise APIError(
                        code="SANDBOX_ISOLATION_UNSUPPORTED",
                        message=(
                            f"sandbox {sandbox_id!r} has an active isolated session "
                            f"{session_id!r} whose workspace identity was not echoed; "
                            "crash recovery cannot distinguish its owner"
                        ),
                        status_code=501,
                    )
                if source != target:
                    continue
                workspace_dir = source
                for bind in getattr(info, "binds", None) or []:
                    if str(getattr(bind, "source", "") or "").rstrip("/") == target:
                        workspace_dir = str(
                            getattr(bind, "dest", "") or source
                        ).rstrip("/")
                        break
                found.append(
                    SandboxIsolatedSession(
                        sandbox_id=str(sandbox_id),
                        session_id=session_id,
                        uid=getattr(info, "uid", None),
                        gid=getattr(info, "gid", None),
                        workspace_dir=workspace_dir,
                        workspace_source_dir=source,
                    )
                )
            return tuple(found)
        except APIError:
            raise
        except Exception as exc:
            raise self._api_error(
                exc,
                operation="list isolated sessions",
                sandbox_id=sandbox_id,
                secret=secret,
            ) from exc
        finally:
            with contextlib.suppress(Exception):
                await handle.close()

    async def data_plane_unreachable(self, sandbox_id: str) -> bool:
        """Is the box's data plane dead while its control-plane record lives?

        The false-Ready shape: a Pod killed out-of-band leaves its sandbox
        record answering GET (still `running`) while nothing behind the
        endpoint exists any more. The control-plane probe cannot see that, so
        an engine whose transport times out against such a box would retry
        against it forever. One short HTTP touch of execd — the daemon every
        engine image carries, on the same host→box route every port shares —
        answers the question: any HTTP status is a live box, a connection
        error is a dead one. A vendor not-found on connect is already the
        definitive gone.
        """

        from astrabox.providers.open_sandbox.executor import EXECD_PORT

        target = str(sandbox_id or "").strip()
        if not target:
            return False
        try:
            handle = await self.connect(target)
        except APIError as exc:
            return str(getattr(exc, "code", "")) in (
                "SANDBOX_GONE",
                "SANDBOX_NOT_FOUND",
            )
        try:
            plane = OpenSandboxDataPlane(handle=handle, port=EXECD_PORT)
            try:
                await plane.request(
                    "GET", "/astrabox-route-probe", timeout=5.0
                )
            except ConnectionError:
                return True
            except Exception:
                # A non-connection failure proves nothing about liveness.
                return False
            return False
        finally:
            close = getattr(handle, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    await close()

    async def open_isolated_session(
        self,
        sandbox_id: str,
        *,
        workspace_dir: str,
        workspace_source_dir: str,
        uid: int | None = None,
        gid: int | None = None,
        share_net: bool = True,
        extra_writable: list[str] | None = None,
        extra_binds: list[tuple[str, str]] | None = None,
    ) -> SandboxIsolatedSession:
        """Open one isolated session in a live box, under its own POSIX owner.

        ``mode="rw"`` and not ``overlay``, which is the default upstream
        documents. Overlay is what would give a private layer over a shared
        base, and it does not start here: every overlay create dies at
        ``start bwrap: bwrap process exited immediately after start``,
        including onto a dedicated volume. Asking for a mode the
        box cannot deliver would fail every conversation rather than isolate
        them, so this asks for the one that works and the layered workspace
        stays a separate, honest gap.

        ``idle_timeout_seconds=0`` disables execd's idle reaper. A conversation
        is idle between turns BY DEFINITION, and a session collected while its
        user was reading would take the resident runner down with it. The
        session ends when the platform closes it, or when the box does.

        ``workspace_source_dir`` MUST ALREADY BE OWNED BY ``uid``. execd
        auto-creates the path when it is absent (OSEP-0013) but creates it as root, and
        nothing upstream chowns it — so a session opened against a directory it
        does not own gets ``Permission denied`` on its first write. The caller
        must create the directory and assign it to ``uid`` before opening the
        isolated session.

        ``0700`` on that directory, not the default ``0755``: execd writes files
        ``0644``, so a sibling conversation in the same box can otherwise READ
        another's work even though it cannot write it.

        This method opens a session; it does not promise the session has
        anywhere to work.
        """
        from opensandbox.models.isolated import (
            BindMount,
            CreateIsolatedSessionRequest,
            IsolatedWorkspaceSpec,
        )

        settings = self._settings()
        secret = _config.resolve_api_key(settings)
        workspace = str(workspace_dir or "").strip()
        workspace_source = str(workspace_source_dir or "").strip()
        if not workspace.startswith("/") or not workspace_source.startswith("/"):
            raise APIError(
                code="INVALID_REQUEST",
                message=(
                    "an isolated session needs absolute workspace and source "
                    f"directories; got workspace={workspace!r} "
                    f"source={workspace_source!r}"
                ),
                status_code=400,
            )
        handle = await self.connect(sandbox_id)
        underlying: Any = getattr(handle, "sidecar_faces", None) or handle
        request = CreateIsolatedSessionRequest(
            workspace=IsolatedWorkspaceSpec(path=workspace_source, mode="rw"),
            binds=(
                (
                    [BindMount(source=workspace_source, dest=workspace, readonly=False)]
                    if workspace_source != workspace
                    else []
                )
                + [
                    BindMount(source=source, dest=dest, readonly=False)
                    for source, dest in (extra_binds or [])
                    if str(source).startswith("/") and str(dest).startswith("/")
                ]
            )
            or None,
            extra_writable=[
                str(path).strip()
                for path in (extra_writable or [])
                if str(path or "").strip()
            ]
            or None,
            uid=uid,
            gid=gid,
            share_net=share_net,
            idle_timeout_seconds=0,
        )
        try:
            session = await underlying.isolation.create(request)
        except Exception as exc:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=_config.scrub_secret(
                    f"open_sandbox could not open an isolated session in {sandbox_id!r}: {exc}",
                    secret=secret,
                ),
                status_code=502,
            ) from exc
        session_id = str(getattr(session, "session_id", "") or "").strip()
        if not session_id:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    f"open_sandbox opened an isolated session in {sandbox_id!r} "
                    "that reported no session id; it cannot be addressed or closed"
                ),
                status_code=502,
            )
        return SandboxIsolatedSession(
            sandbox_id=sandbox_id,
            session_id=session_id,
            uid=uid,
            gid=gid,
            workspace_dir=workspace,
            workspace_source_dir=workspace_source,
        )

    async def prepare_isolated_workspace(
        self, sandbox_id: str, *, workspace_dir: str, uid: int, gid: int
    ) -> None:
        """Create the workspace, hand it to ``uid``, and close it to the rest.

        One command as the box's root over the ordinary execd face — the same
        face every other in-box operation uses. ``mkdir -p`` is idempotent and
        the chown/chmod re-assert rather than toggle, so re-preparing an
        existing workspace (a reattach, a retry) is a no-op that still
        guarantees the invariant.
        """
        path = str(workspace_dir or "").strip()
        if not path.startswith("/"):
            raise APIError(
                code="INVALID_REQUEST",
                message=(f"an isolated workspace needs an absolute path; got {workspace_dir!r}"),
                status_code=400,
            )
        owner = f"{int(uid)}:{int(gid)}"
        quoted = shlex.quote(path)
        handle = await self.connect(sandbox_id)
        exit_code, _stdout, stderr = await handle.exec_collect(
            [
                "sh",
                "-c",
                f"mkdir -p {quoted} && chown -R {owner} {quoted} && chmod 0700 {quoted}",
            ]
        )
        if exit_code != 0:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    f"could not prepare isolated workspace {path!r} for {owner} in "
                    f"sandbox {sandbox_id!r}: {stderr.decode('utf-8', 'replace').strip()}"
                ),
                status_code=502,
            )

    async def run_in_isolated_session(
        self,
        sandbox_id: str,
        session_id: str,
        *,
        code: str,
        envs: Mapping[str, str] | None = None,
        timeout_s: float | None = 60.0,
    ) -> tuple[int, str, str]:
        """Run ``code`` in one isolated session over execd.

        The host-side deadline is this call's own, not the box's: ``timeout_s`` bounds the
        whole round trip because a non-terminating command or a dead host→box
        route would otherwise hang forever. The box gets its own bound too, so
        a command that outlives the deadline is stopped there rather than left
        running with nobody reading it.
        """
        settings = self._settings()
        secret = _config.resolve_api_key(settings)
        target = str(session_id or "").strip()
        if not target:
            raise APIError(
                code="INVALID_REQUEST",
                message="cannot run in an isolated session without a session id",
                status_code=400,
            )
        handle = await self.connect(sandbox_id)
        underlying: Any = getattr(handle, "sidecar_faces", None) or handle
        try:
            session = await underlying.isolation.attach(target)
        except Exception as exc:
            if box_is_unreachable(exc):
                # The typed code IS the mechanism here: the turn path classifies
                # on SANDBOX_GONE and nothing downstream re-derives it from the
                # text. Reported as a 409 about the session, a destroyed box
                # left the conversation retrying an attach to something that
                # cannot come back, instead of being given a new box.
                raise APIError(
                    code="SANDBOX_GONE",
                    message=_config.scrub_secret(
                        f"open_sandbox isolated attach: sandbox {sandbox_id!r} "
                        f"does not answer, so isolated session {target!r} "
                        f"cannot be reached: {exc}",
                        secret=secret,
                    ),
                    status_code=404,
                ) from exc
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=_config.scrub_secret(
                    f"isolated session {target!r} is not there to run in: {exc}",
                    secret=secret,
                ),
                status_code=409,
            ) from exc
        try:
            async with asyncio.timeout(timeout_s):
                if envs:
                    from opensandbox.models.isolated import IsolatedRunOpts

                    execution = await session.run(
                        code,
                        opts=IsolatedRunOpts(
                            envs={str(key): str(value) for key, value in envs.items()},
                            timeout_seconds=(
                                max(0, int(timeout_s))
                                if timeout_s is not None
                                else None
                            ),
                        ),
                    )
                else:
                    execution = await session.run(code)
        except TimeoutError:
            if timeout_s is None:
                raise
            raise TimeoutError(
                f"open_sandbox isolated run did not complete within {timeout_s:g}s: {code[:200]!r}"
            ) from None
        stdout = collect_execd_stream(execution.logs.stdout).decode("utf-8", "replace")
        stderr = collect_execd_stream(execution.logs.stderr).decode("utf-8", "replace")
        exit_code = execution.exit_code
        if exit_code is None:
            error = execution.error
            if error is None:
                exit_code = 0
            else:
                match = _TRAILING_INT_RE.search(str(error.value or ""))
                exit_code = int(match.group(1)) if match is not None else 1
                stderr += f"{error.name}: {error.value}"
        return int(exit_code), stdout, stderr

    async def stream_in_isolated_session(
        self,
        sandbox_id: str,
        session_id: str,
        *,
        code: str,
        envs: Mapping[str, str] | None = None,
        timeout_s: float | None = 300.0,
    ):
        """Stream OpenSandbox's native isolated ``/run`` SSE response.

        The SDK invokes handlers while it consumes the response, so a small
        queue turns those callbacks into the provider seam's async iterator.
        Cancelling this iterator cancels the SDK request and closes the SSE
        connection. OpenSandbox v1.1.0 does not reliably stop the active run
        on disconnect, so callers that need interruption must run disposable
        work in a sibling isolated session and delete that session afterward.

        Execd currently merges the isolated shell's stderr into stdout before
        scanning it, so ordinary command output arrives as ``stdout``. Its
        structured execution error is reported as ``stderr`` instead of being
        mistaken for user output.
        """
        from opensandbox.models.execd import ExecutionHandlers
        from opensandbox.models.isolated import IsolatedRunOpts

        settings = self._settings()
        secret = _config.resolve_api_key(settings)
        target = str(session_id or "").strip()
        if not target:
            raise APIError(
                code="INVALID_REQUEST",
                message="cannot stream in an isolated session without a session id",
                status_code=400,
            )

        handle = await self.connect(sandbox_id)
        underlying: Any = getattr(handle, "sidecar_faces", None) or handle
        try:
            session = await underlying.isolation.attach(target)
        except Exception as exc:
            with contextlib.suppress(Exception):
                await handle.close()
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=_config.scrub_secret(
                    f"isolated session {target!r} is not there to stream in: {exc}",
                    secret=secret,
                ),
                status_code=409,
            ) from exc

        queue: asyncio.Queue[Any] = asyncio.Queue()
        finished = object()
        outcome: dict[str, Any] = {}

        async def on_stdout(message: Any) -> None:
            text = str(getattr(message, "text", "") or "")
            # Execd's isolated stream is line-shaped and removes the line
            # terminator. Put it back for the terminal display; its sentinel is
            # parsed before anything is exposed to the caller.
            await queue.put({"type": "stdout", "text": text + "\n"})

        reported_exit_status = ReportedExitStatus()

        async def on_error(error: Any) -> None:
            reported_exit_status.record(error)

        async def drive() -> None:
            try:
                opts = IsolatedRunOpts(
                    envs={str(key): str(value) for key, value in (envs or {}).items()},
                    timeout_seconds=(
                        max(0, int(timeout_s)) if timeout_s is not None else None
                    )
                )
                execution = await session.run(
                    code,
                    opts=opts,
                    handlers=ExecutionHandlers(
                        on_stdout=on_stdout,
                        on_error=on_error,
                        skip_accumulation=True,
                    ),
                )
                outcome["execution"] = execution
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                outcome["exception"] = exc
            finally:
                await queue.put(finished)

        task = asyncio.create_task(drive())
        try:
            while True:
                event = await queue.get()
                if event is finished:
                    break
                yield event
            await task

            run_error = outcome.get("exception")
            if run_error is not None:
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message=_config.scrub_secret(
                        f"open_sandbox isolated terminal run failed: {run_error}",
                        secret=secret,
                    ),
                    status_code=502,
                ) from run_error

            execution = outcome.get("execution")
            error = getattr(execution, "error", None)
            if error is not None:
                # Structured, not pre-rendered: the vendor's run error is a
                # FACT (for a shell-killing command it is the expected shape —
                # execd's end marker died with bash and its reader returns
                # before capturing any exit code), and only the caller knows
                # whether it also holds the true exit status from another
                # channel. Text would force every reader to parse prose.
                yield {
                    "type": "__error__",
                    "name": str(getattr(error, "name", "") or "RuntimeError"),
                    "value": str(
                        getattr(error, "value", "") or "isolated run failed"
                    ),
                }
            exit_code = getattr(execution, "exit_code", None)
            if exit_code is None:
                # `execution.error` is whichever error arrived LAST — the SDK's
                # dispatcher overwrites it per event — so a transport error
                # raised while draining a shell that has already exited hides
                # the exit status behind it. Every error event also reached
                # `on_error`, where the status was kept: execd reports a
                # process's exit code as the error's value, and only a numeric
                # value is one.
                exit_code = reported_exit_status.code
            if exit_code is None:
                exit_code = 1 if error is not None else 0
            yield {"type": "__done__", "exit_code": int(exit_code)}
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
            with contextlib.suppress(Exception):
                await handle.close()

    async def close_isolated_session(self, sandbox_id: str, session_id: str) -> None:
        """Destroy the session, and with it every process running inside.

        Idempotent by intent, not by hope: the caller wants the session not
        running, and a session that is already gone satisfies that. Reaching
        the box at all can still fail loudly — "I could not ask" is a different
        fact from "it is not there", and only the second one is success.
        """
        target = str(session_id or "").strip()
        if not target:
            return
        settings = self._settings()
        secret = _config.resolve_api_key(settings)
        try:
            handle = await self.connect(sandbox_id)
        except APIError as exc:
            if str(exc.code or "") == "SANDBOX_GONE":
                return
            raise
        underlying: Any = getattr(handle, "sidecar_faces", None) or handle
        try:
            try:
                session = await underlying.isolation.attach(target)
            except Exception:
                # Attach fails for a session that does not exist, which is the
                # state the caller asked for.
                return
            try:
                await session.delete()
            except Exception as exc:
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message=_config.scrub_secret(
                        f"open_sandbox could not close isolated session {target!r} "
                        f"in {sandbox_id!r}: {exc}",
                        secret=secret,
                    ),
                    status_code=502,
                ) from exc
        finally:
            with contextlib.suppress(Exception):
                await handle.close()

    async def read_diagnostics(self, sandbox_id: str, *, scope: str) -> SandboxDiagnostics:
        """One PLAIN-TEXT diagnostic report from ``GET /v1/sandboxes/{id}/diagnostics/{scope}``.

        What comes back is prose an operator reads, not data a program parses,
        and this method deliberately keeps it that way: the report is carried as
        text with the server's own content type and no attempt to structure it.
        The SDK is not on this path — its diagnostics client speaks the
        ``scope``-parameterised variant, which is a different operation from
        these four fixed reports, and it covers only two of them.

        Availability is the SERVER's answer, per deployment. A server that does
        not implement a report says so, and this method relays that refusal as a
        501 naming the server's own reason — never as an empty report, which an
        operator would read as "the sandbox has nothing to say".

        Long reports are capped at :data:`_DIAGNOSTICS_MAX_CHARS` with
        ``truncated=True``; the kept end follows :data:`_DIAGNOSTIC_TAIL_SCOPES`.
        The body is STREAMED and the cap applied as it arrives
        (:func:`_read_capped_report`), so a multi-gigabyte ``logs`` never sits in
        this process at all — which is what the cap was always for.
        """
        wanted = str(scope or "").strip().lower()
        if wanted not in SANDBOX_DIAGNOSTIC_SCOPES:
            raise APIError(
                code="SANDBOX_DIAGNOSTIC_SCOPE_INVALID",
                message=(
                    f"unknown diagnostic scope {scope!r}; expected one of "
                    f"{', '.join(SANDBOX_DIAGNOSTIC_SCOPES)}"
                ),
                status_code=400,
            )
        settings = self._settings()
        secret = _config.resolve_api_key(settings)
        connection = self._sdk_connection_config(settings)
        headers = _config.lifecycle_headers(connection, secret=secret, accept="text/plain, */*")
        url = f"{connection.get_base_url()}/sandboxes/{sandbox_id}/diagnostics/{wanted}"
        try:
            async with httpx.AsyncClient(
                timeout=_DIAGNOSTICS_TIMEOUT_SECONDS, transport=self._transport
            ) as client:
                async with client.stream("GET", url, headers=headers) as response:
                    status_code = response.status_code
                    content_type = str(response.headers.get("content-type") or "text/plain")
                    raw, truncated = await _read_capped_report(
                        response,
                        keep_tail=wanted in _DIAGNOSTIC_TAIL_SCOPES,
                        limit=_DIAGNOSTICS_MAX_CHARS,
                    )
        except httpx.HTTPError as exc:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=_config.scrub_secret(
                    f"open_sandbox diagnostics {wanted!r} failed for sandbox {sandbox_id!r}: {exc}",
                    secret=secret,
                ),
                status_code=502,
            ) from exc
        # Two passes, and both are needed. scrub_secret removes the ONE value
        # this backend knows by heart (the OpenSandbox API key, which a
        # misbehaving server can echo back). _redact_env_values removes
        # everything the report may be quoting out of the container's own
        # environment — session-scoped capability tokens included. Applied here,
        # before `body` is used, so the 501/5xx branches below quote a redacted
        # excerpt too rather than only the success path being safe.
        body = _redact_env_values(_config.scrub_secret(raw, secret=secret))
        if status_code == 404:
            raise APIError(
                code="SANDBOX_NOT_FOUND",
                message=(f"open_sandbox diagnostics {wanted!r}: sandbox {sandbox_id!r} not found"),
                status_code=404,
            )
        if status_code == 501:
            raise APIError(
                code="SANDBOX_DIAGNOSTICS_NOT_IMPLEMENTED",
                message=(
                    f"the OpenSandbox server at {_config.lifecycle_base_url(settings)} "
                    f"does not implement the {wanted!r} diagnostic report for "
                    f"sandbox {sandbox_id!r}; it answered: {body[:500]}"
                ),
                status_code=501,
            )
        if status_code != 200:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    f"open_sandbox diagnostics {wanted!r} for sandbox "
                    f"{sandbox_id!r} returned HTTP {status_code}: "
                    f"{body[:500]}"
                ),
                status_code=502,
            )
        return SandboxDiagnostics(
            sandbox_id=str(sandbox_id),
            scope=wanted,
            content_type=content_type,
            text=body,
            truncated=truncated,
        )

# Consumer-driven registration (importing performs the side effect; invoked by
# astrabox.providers.register_builtin_providers()).
register_sandbox(OpenSandboxSandboxProvider())


__all__ = [
    "EXECD_LINE_EVENT_MODEL",
    "EXECD_LINE_EVENT_MODEL_VERSION",
    "OpenSandboxDataPlane",
    "OpenSandboxEndpoint",
    "OpenSandboxHandle",
    "OpenSandboxSandboxProvider",
    "collect_execd_stream",
]
