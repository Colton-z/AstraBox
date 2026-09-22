"""``/api/v1/admin/sandboxes/*`` — the sandbox resource's read-only ops face.

This module exposes sandbox inventory, detail, and diagnostics through the
provider seam so operators can inspect what a backend is running.

**Read-only, and deliberately so.** Every mutation a sandbox has (kill, renew)
already belongs to a session's lifecycle and stays there; a second, sessionless
way to destroy a box would be a second authority over the same object.

**Under ``/api/v1/admin`` because that prefix is the gate.**
:func:`astrabox.web.identity_middleware._is_admin_path` hard-gates exactly this
prefix — an asserted identity needs the admin role, and anonymous never passes
once a real resolver is configured. Mounting an ops surface anywhere else would
mean either shipping it ungated or inventing a second gate; it lives here so it
inherits the one the console admin family already uses.

Everything below dispatches through the sandbox seam
(:meth:`~astrabox.seams.sandbox.SandboxProvider.list_sandboxes` /
``describe_sandbox`` / ``read_diagnostics``), whose defaults refuse loud with a
501 naming the backend. That refusal is passed through verbatim: a backend that
cannot enumerate says so here, and this surface never converts "cannot ask" into
an empty list.

**Every caller string that becomes part of a backend URL is checked here, before
it is handed to a provider.** A backend's control plane is addressed by paths
built from these values (for example,
``/sandboxes/{id}/diagnostics/{scope}``), and an HTTP client resolves ``..`` in a
path the way a browser would — so an unchecked id or pool name does not merely
name the wrong object, it re-points the request at a different endpoint of that
control plane and this surface then echoes the answer back. Read-only makes that
no smaller: the control plane is reachable from inside this process and, on the
usual deployment, from nowhere else, which is exactly the boundary a proxy
crosses. :func:`_url_path_segment` is the one place that decides; every route
below goes through it, and the grammars are refusals, not repairs — nothing is
stripped, escaped or normalised into something acceptable.

Registered directly on the passed ``app``; ``register_sandbox_routes(app) ->
None`` is idempotent by ``id(app)``.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from fastapi import Request
from pydantic import BaseModel, ConfigDict

from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.bootstrap import SANDBOX_IDLE_ACTIONS
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.seams.sandbox import (
    SANDBOX_DIAGNOSTIC_SCOPES,
    SandboxDescriptor,
    SandboxProvider,
    default_sandbox_backend,
    registered_sandbox_names,
    sandbox_for_name,
)
from astrabox.api.routes._shared import _resolve_user, _svc

_registered_on: int | None = None

#: Hard ceiling on one page, so a caller cannot ask the backend for its whole
#: inventory in one request and call it pagination.
_MAX_PAGE_SIZE = 200
_DEFAULT_PAGE_SIZE = 50

#: The widest shape a sandbox id may have to be usable as one path segment.
#:
#: A sandbox id is the backend's own opaque string and this surface invents no
#: meaning for it — but "opaque to this surface" is not "arbitrary bytes",
#: because the id goes into a URL path this surface then fetches. The grammar
#: below is the set of characters that cannot end a segment, start a query,
#: or address a parent:
#: no ``/``, no ``%``, no ``?``/``#``, no whitespace, no control characters, and
#: a leading alphanumeric so ``.`` and ``..`` are not names.
#:
#: It admits every id shape the backends in play mint — UUIDs, container hex
#: digests, Kubernetes resource names — and a backend whose ids fall outside it
#: gets a 400 naming the id rather than a request sent somewhere else. Widening
#: it is a deliberate edit here, so the decision stays in one place instead of
#: being re-derived per route.
_SANDBOX_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: A bound no legitimate id needs to cross, so an unbounded string never
#: reaches a URL builder.
_SANDBOX_ID_MAX_LENGTH = 253


def _url_path_segment(
    raw: str | None,
    *,
    field: str,
    pattern: re.Pattern[str],
    max_length: int,
) -> str:
    """The caller's string, only if it is spellable as one URL path segment.

    The single gate every caller-supplied name passes before a provider can
    build a URL out of it. It answers one question — *may this become a path
    segment* — and answers it by refusing anything that is not already one.
    There is no sanitising branch: a value that needed repairing to be safe is
    a value the caller did not mean, and quietly repairing it would hide which
    object the answer is actually about.

    """
    value = str(raw or "").strip()
    if not value:
        raise APIError(
            code="SANDBOX_NAME_INVALID",
            message=f"{field} is required",
            status_code=400,
        )
    if len(value) > max_length or not pattern.fullmatch(value):
        raise APIError(
            code="SANDBOX_NAME_INVALID",
            message=(
                f"{field} {value!r} is not a usable name: it must match "
                f"{pattern.pattern} and be at most {max_length} characters"
            ),
            status_code=400,
        )
    return value


def _sandbox_id(raw: str) -> str:
    return _url_path_segment(
        raw,
        field="sandbox_id",
        pattern=_SANDBOX_ID_RE,
        max_length=_SANDBOX_ID_MAX_LENGTH,
    )


def _diagnostic_scope(raw: str) -> str:
    """One of the four known scopes, or a 400 naming them.

    The vocabulary is closed, so this is a membership test rather than a
    grammar — and the check lives here as well as in the backend because this
    route, not the backend, is what an unauthenticated-by-accident deployment
    would expose. The code matches the backend's own refusal so a caller sees
    one error for one mistake.
    """
    scope = str(raw or "").strip().lower()
    if scope not in SANDBOX_DIAGNOSTIC_SCOPES:
        raise APIError(
            code="SANDBOX_DIAGNOSTIC_SCOPE_INVALID",
            message=(
                f"unknown diagnostic scope {raw!r}; expected one of "
                f"{', '.join(SANDBOX_DIAGNOSTIC_SCOPES)}"
            ),
            status_code=400,
        )
    return scope


def _resolve_backend(raw: str | None) -> tuple[str, SandboxProvider]:
    """The named backend, or the deployment default; never a guess.

    An unconfigured default with several backends registered is a real
    ambiguity, not something to resolve by picking one — it fails with the
    registered names so the caller can name one.

    This one caller string needs no segment grammar: it is resolved by lookup in
    the process-wide registry and never reaches a URL. A name that is not a
    registered key is a 404, so the only values that survive are ones a provider
    registered under.
    """
    name = str(raw or "").strip().lower() or default_sandbox_backend()
    if not name:
        raise APIError(
            code="SANDBOX_BACKEND_UNRESOLVED",
            message=(
                "no sandbox backend named and no deployment default is "
                f"configured (registered: {registered_sandbox_names()})"
            ),
            status_code=400,
        )
    try:
        return name, sandbox_for_name(name)
    except RuntimeError as exc:
        raise APIError(
            code="SANDBOX_BACKEND_UNKNOWN",
            message=str(exc),
            status_code=404,
        ) from exc


def _positive_int(raw: str | None, *, field: str, default: int, maximum: int) -> int:
    value_text = str(raw or "").strip()
    if not value_text:
        return default
    try:
        value = int(value_text)
    except ValueError as exc:
        raise APIError(
            code="SANDBOX_QUERY_INVALID",
            message=f"{field} must be an integer; got {value_text!r}",
            status_code=400,
        ) from exc
    if value < 1 or value > maximum:
        raise APIError(
            code="SANDBOX_QUERY_INVALID",
            message=f"{field} must be between 1 and {maximum}; got {value}",
            status_code=400,
        )
    return value


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _descriptor_payload(
    descriptor: SandboxDescriptor, *, backend: str
) -> dict[str, Any]:
    """The wire shape of one sandbox. Values are the backend's own.

    ``session_id`` is present only when the box actually carries the create
    metadata; there is no fallback that derives it from the id or from create
    order, because a wrong session attribution is worse than none.
    """
    return {
        "sandbox_id": descriptor.sandbox_id,
        "backend": backend,
        "state": descriptor.state,
        "created_at": _iso(descriptor.created_at),
        "expires_at": _iso(descriptor.expires_at),
        "image": descriptor.image,
        "entrypoint": list(descriptor.entrypoint),
        "metadata": dict(descriptor.metadata),
        "session_id": descriptor.session_id,
        "endpoint": descriptor.endpoint,
    }


# ── Response payloads ────────────────────────────────────────────────
#
# One model per route, declared as ``ApiEnvelope[...]`` on the decorator so the
# generated OpenAPI client sees the body instead of an untyped object; the
# obligations that declaration carries are stated in
# :mod:`astrabox.api.routes.response_envelope`.
#
# Every field below is one the handler above always writes, and every value in
# it is the backend's own. Whatever a backend reports beyond these fields still
# reaches the caller: ``extra="allow"`` on each model is what keeps a response
# model from deleting it.


class AdminSandboxSummary(BaseModel):
    """One sandbox, as :func:`_descriptor_payload` renders it."""

    model_config = ConfigDict(extra="allow")

    sandbox_id: str
    backend: str
    state: str
    created_at: str | None
    expires_at: str | None
    image: str | None
    entrypoint: list[str]
    metadata: dict[str, str]
    session_id: str | None
    endpoint: str | None


class AdminSandboxPagination(BaseModel):
    """The backend's own paging counters, passed through."""

    model_config = ConfigDict(extra="allow")

    page: int
    page_size: int
    total_items: int
    total_pages: int
    has_next_page: bool


class AdminSandboxPage(BaseModel):
    """One page of a backend's inventory."""

    model_config = ConfigDict(extra="allow")

    backend: str
    items: list[AdminSandboxSummary]
    pagination: AdminSandboxPagination


class AdminSandboxIdleAction(BaseModel):
    """Which idle actions a backend can carry out, and the installation default."""

    model_config = ConfigDict(extra="allow")

    backend: str
    actions: list[str]
    supported_actions: list[str]
    default_action: str | None
    detail: str | None


class AdminSandboxIdleSweep(BaseModel):
    """What one forced run of the idle/expiry sweep did.

    ``summary`` holds the tick's own counters
    (:meth:`~astrabox.core.service.orchestrator.expiration_watcher.ExpirationWatcher.scan_once`);
    an empty object means neither sweep found a candidate.
    """

    model_config = ConfigDict(extra="allow")

    summary: dict[str, int]


class AdminSandboxEgressRule(BaseModel):
    """One egress rule as the box's sidecar reports it."""

    model_config = ConfigDict(extra="allow")

    action: str
    target: str


class AdminSandboxSecurity(BaseModel):
    """What one sandbox reports about its own containment."""

    model_config = ConfigDict(extra="allow")

    sandbox_id: str
    available: bool
    default_action: str | None
    egress_rules: list[AdminSandboxEgressRule]
    credential_names: list[str]
    binding_names: list[str]
    detail: str | None


class AdminSandboxDiagnostics(BaseModel):
    """One plain-text diagnostic report about one sandbox.

    ``text`` carries no schema — it is the backend's prose, transported inside
    JSON. ``known_scopes`` is the accepted scope vocabulary, not a list of the
    reports this sandbox can produce.
    """

    model_config = ConfigDict(extra="allow")

    sandbox_id: str
    backend: str
    scope: str
    content_type: str
    text: str
    truncated: bool
    known_scopes: list[str]


def register_sandbox_routes(app: Any) -> None:
    global _registered_on
    if _registered_on == id(app):
        return
    _registered_on = id(app)

    @app.get(
        "/api/v1/admin/sandboxes",
        response_model=ApiEnvelope[AdminSandboxPage],
        response_model_exclude_unset=True,
    )
    async def list_sandboxes(request: Request):
        """One page of the backend's sandbox inventory.

        Paged at the source: ``pagination`` carries the backend's own counters,
        so the console advances by ``page`` while ``has_next_page`` holds rather
        than assuming one response is the whole inventory.
        """
        await _resolve_user(request)
        backend, provider = _resolve_backend(request.query_params.get("backend"))
        page = _positive_int(
            request.query_params.get("page"),
            field="page",
            default=1,
            maximum=1_000_000,
        )
        page_size = _positive_int(
            request.query_params.get("page_size"),
            field="page_size",
            default=_DEFAULT_PAGE_SIZE,
            maximum=_MAX_PAGE_SIZE,
        )
        result = await provider.list_sandboxes(page=page, page_size=page_size)
        return success_response(
            {
                "backend": backend,
                "items": [
                    _descriptor_payload(item, backend=backend)
                    for item in result.items
                ],
                "pagination": {
                    "page": result.page,
                    "page_size": result.page_size,
                    "total_items": result.total_items,
                    "total_pages": result.total_pages,
                    "has_next_page": result.has_next_page,
                },
            }
        )

    @app.get(
        "/api/v1/admin/sandbox-idle-action",
        response_model=ApiEnvelope[AdminSandboxIdleAction],
        response_model_exclude_unset=True,
    )
    async def describe_sandbox_idle_action(request: Request):
        """Which idle actions a backend can carry out, and which one is in force.

        The question a form has to ask before offering an environment the choice:
        an ``idle_action`` the backend cannot honor is refused at write time, and a
        control that offers it anyway makes the operator discover that by being
        rejected.

        Whether a backend implements pausing is a property of the code, declared
        on the seam, so there is nothing to be unreachable and ``supported`` is
        a plain boolean.

        What it therefore does not answer: whether this cluster is arranged for
        snapshots. Pausing additionally needs a containerd at the standard socket,
        a CRI that pulls locally, and a registry both the node and a Pod can
        reach (docs/providers/opensandbox.md). A backend that declares pausing on
        a cluster missing one of those pauses nothing — the commit fails, the box
        is left running, and the sweeper says so. So this reports capability, and
        the form's copy carries the cluster requirement; neither pretends to be
        the other.

        ``default_action`` is the installation's own setting. It seeds the field
        when an environment is created and is not consulted again: a stored
        environment always carries its own action, so a sweep reads that and
        never this.
        """
        await _resolve_user(request)
        backend, provider = _resolve_backend(request.query_params.get("backend"))
        return success_response(
            {
                "backend": backend,
                "actions": list(SANDBOX_IDLE_ACTIONS),
                "supported_actions": [
                    action
                    for action in SANDBOX_IDLE_ACTIONS
                    if action != "pause" or provider.supports_pause
                ],
                "default_action": str(
                    load_astrabox_settings().sandbox_idle_action or ""
                ).strip().lower()
                or None,
                "detail": (
                    None
                    if provider.supports_pause
                    else (
                        f"the {provider.name!r} sandbox backend cannot snapshot a "
                        f"sandbox, so an idle box would go on being destroyed with "
                        f"its workspace"
                    )
                ),
            }
        )

    @app.post(
        "/api/v1/admin/sandbox-idle-sweep",
        response_model=ApiEnvelope[AdminSandboxIdleSweep],
        response_model_exclude_unset=True,
    )
    async def run_sandbox_idle_sweep(request: Request):
        """Run the idle/expiry sweep once, now, and answer with what it did.

        The expiration watcher normally invokes this sweep every 300 seconds. This
        endpoint invokes the same scan immediately, so checking idle parking or
        dead-binding convergence does not add the watcher interval to any cluster
        operation.

        Read-only in the sense that matters: it takes no arguments and can decide
        nothing the timer would not have decided on its next tick. It is a
        `POST` because it acts.

        The summary is the tick's own — `idle_candidates` / `idle_parked` /
        `idle_active` / `idle_failed`, plus the dead-binding counters — so a caller
        sees why nothing happened, which is usually the question. An empty object
        means neither sweep found a candidate.
        """
        await _resolve_user(request)
        summary = await _svc()._expiration_watcher.scan_once()
        return success_response({"summary": summary})

    @app.get(
        "/api/v1/admin/sandboxes/{sandbox_id}",
        response_model=ApiEnvelope[AdminSandboxSummary],
        response_model_exclude_unset=True,
    )
    async def describe_sandbox(sandbox_id: str, request: Request):
        """One sandbox as its backend's control plane describes it."""
        await _resolve_user(request)
        backend, provider = _resolve_backend(request.query_params.get("backend"))
        descriptor = await provider.describe_sandbox(_sandbox_id(sandbox_id))
        return success_response(_descriptor_payload(descriptor, backend=backend))

    @app.get(
        "/api/v1/admin/sandboxes/{sandbox_id}/security",
        response_model=ApiEnvelope[AdminSandboxSecurity],
        response_model_exclude_unset=True,
    )
    async def read_sandbox_security(sandbox_id: str, request: Request):
        """What this box reports about its own containment.

        Asked of the sandbox, not of the control plane: an operator opening
        this panel wants to know whether the egress policy and the credential
        vault their deployment configured actually took effect, and an answer
        derived from that configuration cannot tell them. So every field is the
        box's answer.

        ``available: false`` is a finding, not a failure. It means no egress
        sidecar answered — the box has no egress policy and no vault, whatever
        was intended — and it carries the reason. A backend that cannot ask a
        box at all says so the same way, which an operator can tell apart from
        an uncontained box because the detail names the backend.

        Credentials appear by name only. The vault is write-only by
        construction; no stored value can be read back, here or anywhere.
        """
        await _resolve_user(request)
        _, provider = _resolve_backend(request.query_params.get("backend"))
        posture = await provider.read_security_posture(_sandbox_id(sandbox_id))
        return success_response(
            {
                "sandbox_id": posture.sandbox_id,
                "available": posture.available,
                "default_action": posture.default_action,
                "egress_rules": [
                    {"action": action, "target": target}
                    for action, target in posture.egress_rules
                ],
                "credential_names": list(posture.credential_names),
                "binding_names": list(posture.binding_names),
                "detail": posture.detail,
            }
        )

    @app.get(
        "/api/v1/admin/sandboxes/{sandbox_id}/diagnostics/{scope}",
        response_model=ApiEnvelope[AdminSandboxDiagnostics],
        response_model_exclude_unset=True,
    )
    async def read_sandbox_diagnostics(
        sandbox_id: str,
        scope: str,
        request: Request,
    ):
        """One plain-text diagnostic report, passed through as text.

        ``text`` is prose for a human to read — no schema, no promised fields,
        no stability across backends or backend versions. It is returned inside
        the JSON envelope purely as transport; render it verbatim and do not
        parse it. ``truncated`` marks a report the provider had to cap.

        ``***`` in the text is a value the backend withheld, and a run of them
        is normal rather than a fault: a backend that cannot prove where a
        withheld value ended keeps masking to the end of the report. When a
        report reads that way, the later sections are readable as their own
        scopes.

        A backend or server that does not produce the report answers 501 with
        its own reason, so the caller learns the report cannot be had and why,
        instead of receiving empty text that reads as a healthy, silent
        sandbox.

        ``known_scopes`` is the scope vocabulary this route accepts
        (:data:`~astrabox.seams.sandbox.SANDBOX_DIAGNOSTIC_SCOPES`) — a constant,
        and named as the constant it is. It is deliberately not called
        "available": support is per backend and, for a control plane fronting
        several runtimes, potentially per runtime, so which of these four this
        sandbox can actually produce is knowable only by asking for each one.
        A field that promised availability would be advertising reports the same
        backend goes on to refuse.
        """
        await _resolve_user(request)
        backend, provider = _resolve_backend(request.query_params.get("backend"))
        report = await provider.read_diagnostics(
            _sandbox_id(sandbox_id), scope=_diagnostic_scope(scope)
        )
        return success_response(
            {
                "sandbox_id": report.sandbox_id,
                "backend": backend,
                "scope": report.scope,
                "content_type": report.content_type,
                "text": report.text,
                "truncated": report.truncated,
                "known_scopes": list(SANDBOX_DIAGNOSTIC_SCOPES),
            }
        )
