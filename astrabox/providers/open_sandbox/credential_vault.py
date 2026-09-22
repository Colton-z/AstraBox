"""Keep model credentials in the egress sidecar, outside the sandbox.

The sandbox receives :data:`EGRESS_HELD_PLACEHOLDER`. The sidecar
terminates outbound HTTPS and substitutes the real credential from its
write-only vault, so sandbox processes cannot read the secret.

Vault mode requires an egress image and the provider's protected-injection
capability. Environment networking is a separate sandbox input. The provider
combines it with the exact hosts authorized by the attached bindings when it
builds the effective OpenSandbox policy; neither configuration duplicates the
other.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
from typing import Any, Iterable, Literal
from urllib.parse import urlparse

# The SDK's PUBLIC models. The generated ones under ``api.egress.models`` share
# their names and are NOT what the vault adapter validates against.
from opensandbox.models.sandboxes import (
    Credential,
    CredentialAuth,
    CredentialBinding,
    CredentialMatch,
    CredentialSubstitution,
    CustomHeaderEntry,
    InlineCredentialSource,
)

from astrabox.common.utils.errors import APIError
from astrabox.seams.egress_credentials import (
    EGRESS_HELD_PLACEHOLDER,
    EgressCredential,
    SandboxEgressCredentialPlan,
    mint_placeholder,
    workload_placeholder_context,
    workload_model_placeholder,
)

#: One credential, one binding, one name. The binding refers to the credential by
#: this name rather than by value, which is the whole shape of the mechanism.
_CREDENTIAL_NAME = "astrabox-model-gateway"

#: Every per-workload credential carries this prefix, which is what makes
#: the live set recoverable from the names the vault does return.
_SLOT_CREDENTIAL_PREFIX = "astrabox-slot-"

#: Environment credential names keep the provider-neutral credential id after
#: this provider-owned prefix. Their bindings stay singular per destination;
#: sibling workloads are represented by substitutions inside that one binding.
_ENV_CREDENTIAL_PREFIX = "astrabox-vault-"

_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_RESERVED_CREDENTIAL_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "content-type",
        "forwarded",
        "host",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
    }
)


def _is_credential_fqdn(host: str) -> bool:
    """Match OpenSandbox's Credential Vault host grammar locally."""

    candidate = str(host or "").strip().lower().rstrip(".")
    if candidate.startswith("*."):
        candidate = candidate[2:]
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        pass
    else:
        return False
    return (
        "." in candidate
        and len(candidate) <= 253
        and all(_HOST_LABEL.fullmatch(label) for label in candidate.split("."))
    )


def require_vault_preconditions(*, egress_image: str, egress_mode: str) -> None:
    """Refuse a half-configured vault, naming what is missing.

    The credential proxy is the component that holds and injects protected
    values. ``egress_mode`` is an OpenSandbox component precondition, not an
    Environment reachability decision; no Environment host or mode is read
    here.
    """
    missing: list[str] = []
    if not str(egress_image or "").strip():
        missing.append(
            "ASTRABOX_SANDBOX_EGRESS_IMAGE (no sidecar means nothing holds the credential)"
        )
    if str(egress_mode or "").strip().lower() != "dns+nft":
        missing.append(
            "ASTRABOX_SANDBOX_EGRESS_MODE=dns+nft "
            "(OpenSandbox's credential component refuses any other mode)"
        )
    if missing:
        raise APIError(
            code="SANDBOX_CONFIG_INVALID",
            message=(
                "ASTRABOX_SANDBOX_CREDENTIAL_VAULT is on but the deployment cannot "
                "hold the credential outside the sandbox; missing: " + "; ".join(missing)
            ),
            status_code=400,
        )


def gateway_host(base_url: str) -> str:
    """The host a binding must match, from the endpoint the credential is scoped to.

    A binding that matched everything would attach the credential to any
    request the sandbox made. The binding itself names where this secret may
    go; Environment networking independently decides whether that request can
    leave the box.
    """
    parsed = urlparse(str(base_url or "").strip())
    host = (parsed.hostname or "").strip()
    if not host:
        raise APIError(
            code="SANDBOX_CONFIG_INVALID",
            message=(
                "the credential vault needs the model endpoint's host, and "
                f"{base_url!r} has none — a credential must be scoped to where it may be sent"
            ),
            status_code=400,
        )
    return host


def _gateway_match(base_url: str) -> tuple[str, Literal["https", "http"]]:
    """Return the exact host/scheme pair OpenSandbox can bind.

    CredentialMatch contains no port field. OpenSandbox derives the port from
    the scheme (HTTP=80, HTTPS=443), so any other port would produce a binding
    that cannot match the model request.
    """

    raw = str(base_url or "").strip()
    parsed = urlparse(raw)
    host = (parsed.hostname or "").strip()
    scheme = str(parsed.scheme or "").strip().lower()
    if not host or scheme not in {"http", "https"}:
        raise APIError(
            code="SANDBOX_CONFIG_INVALID",
            message=(
                "the credential vault needs an http:// or https:// model endpoint "
                f"with a host; got {base_url!r}"
            ),
            status_code=400,
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise APIError(
            code="SANDBOX_CONFIG_INVALID",
            message=f"the model endpoint has an invalid port: {base_url!r}",
            status_code=400,
        ) from exc
    expected_port = 80 if scheme == "http" else 443
    if port is not None and port != expected_port:
        raise APIError(
            code="SANDBOX_CONFIG_INVALID",
            message=(
                "Credential Vault can bind a model gateway only on standard "
                f"ports 80 or 443; got {base_url!r}. Put the gateway behind a "
                "standard-port proxy, or turn protected delivery off explicitly."
            ),
            status_code=400,
        )
    if not _is_credential_fqdn(host):
        raise APIError(
            code="SANDBOX_CONFIG_INVALID",
            message=(
                "Credential Vault requires the model gateway host to be a "
                f"fully qualified domain name, not an IP address or single-label "
                f"name; got {host!r}. Configure a DNS name the sandbox can "
                "resolve, or turn protected delivery off explicitly."
            ),
            status_code=400,
        )
    return host, scheme  # type: ignore[return-value]


def build_vault_write(
    *,
    credential: str,
    credential_header: str,
    base_url: str,
    request_methods: Iterable[str] = ("GET", "POST"),
    request_paths: Iterable[str] = ("/v1/*", "/chat/completions"),
    name: str = _CREDENTIAL_NAME,
    extra_substitutions: Iterable[tuple[str, str]] = (),
) -> tuple[list[Credential], list[CredentialBinding]]:
    """``(credentials, bindings)`` for one gateway this sandbox may reach.

    ``credential_header`` decides how the sidecar attaches it, and it is the same
    decision the CLI's own environment variable name follows: a gateway Bearer
    token is substituted for the placeholder the workload sends in its
    Authorization header, a vendor key becomes an ``x-api-key`` rewrite.
    Getting this wrong does not leak anything — it produces a request the
    upstream rejects — but it produces it on every turn, so it is derived from
    the credential's own source rather than assumed.

    ``extra_substitutions`` adds further ``(placeholder, credential_name)``
    pairs to the gateway binding — one per prepared slot — so sibling
    workloads in a shared box spend distinct identities through the one
    binding the destination may have. Callers own supplying the COMPLETE
    current set: the vault's read API never returns substitution entries, so
    the composed binding is always written whole.
    """
    secret = str(credential or "").strip()
    if not secret:
        raise APIError(
            code="SANDBOX_CONFIG_INVALID",
            message="the credential vault was asked to store an empty credential",
            status_code=400,
        )

    host, scheme = _gateway_match(base_url)
    header = str(credential_header or "").strip().lower()
    methods = list(
        dict.fromkeys(
            str(method or "").strip().upper()
            for method in request_methods
            if str(method or "").strip()
        )
    )
    paths = list(
        dict.fromkeys(
            str(path or "").strip()
            for path in request_paths
            if str(path or "").strip()
        )
    )
    if not methods or not paths or any(not path.startswith("/") for path in paths):
        raise APIError(
            code="SANDBOX_CONFIG_INVALID",
            message=(
                "Credential Vault model bindings require at least one HTTP "
                "method and one absolute request path"
            ),
            status_code=400,
        )

    credentials = [
        Credential(
            name=name,
            source=InlineCredentialSource(type="inline", value=secret),
        )
    ]

    if header in ("x-api-key", "api-key"):
        auth = CredentialAuth(type="apiKey", name="x-api-key", credential=name)
    else:
        # Substitution instead of a bearer rewrite, deliberately: a bearer
        # binding overwrites the Authorization header box-wide, so every
        # workload in a shared box spends the same identity. Substitution maps
        # each placeholder to its own credential inside one binding, which is
        # what lets a prepared slot's child carry a per-slot placeholder and
        # spend a Session-scoped key after claim. It is also fail-closed: a
        # token the binding does not know passes through to the gateway and is
        # refused there, loudly, instead of being silently rewritten.
        auth = CredentialAuth(
            type="passthrough",
            substitutions=[
                CredentialSubstitution.model_validate(
                    {
                        "credential": cred_name,
                        "placeholder": placeholder,
                        "in": ["header"],
                    }
                )
                for placeholder, cred_name in (
                    (EGRESS_HELD_PLACEHOLDER, name),
                    *tuple(extra_substitutions or ()),
                )
            ],
        )

    bindings = [
        CredentialBinding(
            name=name,
            match=CredentialMatch(
                hosts=[host],
                schemes=[scheme],
                methods=methods,
                paths=paths,
            ),
            auth=auth,
        )
    ]
    return credentials, bindings


def workload_credential_name(workload_id: str) -> str:
    """The vault credential one workload's placeholder substitutes to."""
    normalized = str(workload_id or "").strip()
    if not normalized:
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message="a workload credential name requires a workload id",
            status_code=500,
        )
    return f"{_SLOT_CREDENTIAL_PREFIX}{normalized}"


def workload_of_credential_name(name: str) -> str | None:
    """The workload a vault credential belongs to, or None if it is not one.

    The inverse of :func:`workload_credential_name`, and the reason no shadow of
    the vault is needed: a workload's credential NAME and its placeholder are
    two derivations of one workload id, and names are the one part of a vault
    the read API does return.
    """
    candidate = str(name or "").strip()
    if not candidate.startswith(_SLOT_CREDENTIAL_PREFIX):
        return None
    return candidate[len(_SLOT_CREDENTIAL_PREFIX) :] or None


def workload_substitutions(existing_credentials: Iterable[Any]) -> list[tuple[str, str]]:
    """Every live workload's ``(placeholder, credential name)``, from the vault.

    The gateway binding is replaced whole and its substitution list is
    write-only, so a writer that composed only its own identity silently
    stripped its siblings' — and nothing could notice, because what was lost
    cannot be read back. Five writers shared that rule and one did not carry
    it: a claimed slot's placeholder reached the gateway verbatim and the
    conversation spent 174 seconds retrying `401 Virtual Key expected`.

    Deriving the set from the vault's own credential names instead of from a
    ledger beside it removes the class rather than guarding it. There is no
    local copy to go stale, no window between two stores, and no writer needs
    to know which siblings exist — every one of them reads the same authority
    under the same revision it writes.
    """
    substitutions: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in existing_credentials or ():
        raw = item.get("name") if isinstance(item, dict) else getattr(item, "name", None)
        workload = workload_of_credential_name(str(raw or ""))
        if workload is None or workload in seen:
            continue
        seen.add(workload)
        substitutions.append((workload_model_placeholder(workload), str(raw).strip()))
    return substitutions


def carry_workload_substitutions(
    bindings: Iterable[Any], credentials: Iterable[Any]
) -> list[Any]:
    """Complete shared bindings with every live workload's placeholder.

    The model credential names in sanitized Vault state are the recoverable
    workload identities whose substitutions this box must retain. Model
    substitutions route each workload to its own gateway credential.
    Environment substitutions route each workload-specific placeholder to the
    one Agent-bound credential; they remain inside that credential's existing
    binding so OpenSandbox never sees overlapping request bindings.

    The caller's own substitutions are kept and the derived ones added, so a
    writer that already knows its identity is not second-guessed and a writer
    that does not cannot leave a sibling out.
    """
    derived = workload_substitutions(credentials)
    workloads = [
        workload
        for _placeholder, credential in derived
        if (workload := workload_of_credential_name(credential)) is not None
    ]
    completed: list[Any] = []
    for binding in bindings:
        auth = getattr(binding, "auth", None)
        current = list(getattr(auth, "substitutions", None) or [])
        binding_name = str(getattr(binding, "name", "") or "").strip()
        if auth is None or not current:
            completed.append(binding)
            continue
        seen = {str(getattr(item, "placeholder", "") or "") for item in current}
        if binding_name == _CREDENTIAL_NAME:
            additions = [
                CredentialSubstitution.model_validate(
                    {
                        "credential": credential,
                        "placeholder": placeholder,
                        "in": ["header"],
                    }
                )
                for placeholder, credential in derived
                if placeholder not in seen
            ]
        else:
            seed = current[0]
            credential = str(getattr(seed, "credential", "") or "").strip()
            surfaces = list(getattr(seed, "in_", None) or [])
            is_environment_binding = (
                credential.startswith(_ENV_CREDENTIAL_PREFIX)
                and binding_name in {credential, f"{credential}-http"}
                and bool(surfaces)
            )
            if not is_environment_binding:
                completed.append(binding)
                continue
            credential_id = credential.removeprefix(_ENV_CREDENTIAL_PREFIX)
            additions = []
            for workload in workloads:
                placeholder = mint_placeholder(
                    credential_id,
                    context=workload_placeholder_context(workload),
                )
                if placeholder in seen:
                    continue
                additions.append(
                    CredentialSubstitution.model_validate(
                        {
                            "credential": credential,
                            "placeholder": placeholder,
                            "in": surfaces,
                        }
                    )
                )
        if not additions:
            completed.append(binding)
            continue
        completed.append(
            binding.model_copy(
                update={
                    "auth": auth.model_copy(
                        update={"substitutions": [*current, *additions]}
                    )
                }
            )
        )
    return completed

def build_mcp_header_vault_write(
    *,
    server_url: str,
    headers: dict[str, str],
    header_sources: dict[str, str],
    name: str,
    request_methods: Iterable[str] = ("GET", "POST", "DELETE"),
) -> tuple[list[Credential], list[CredentialBinding]]:
    """Bind an MCP server's complete header set to its exact outbound path.

    A single ``customHeaders`` binding is deliberate. Separate bindings for a
    provider gateway key and a saved upstream key would both match the same
    request, which OpenSandbox rejects as ambiguous. An empty header map emits
    a stable passthrough binding: refreshing after a credential is archived
    replaces the old injecting binding instead of leaving its secret active in
    the process-local sidecar Vault.
    """

    binding_name = str(name or "").strip()
    if not binding_name:
        raise APIError(
            code="SANDBOX_CONFIG_INVALID",
            message="an MCP Credential Vault binding needs a stable name",
            status_code=400,
        )
    host, scheme = _gateway_match(server_url)
    parsed = urlparse(str(server_url or "").strip())
    if parsed.query and headers:
        raise APIError(
            code="SANDBOX_CONFIG_INVALID",
            message=(
                "Credential Vault cannot scope MCP header injection to a URL "
                f"query string; got {server_url!r}. Use a query-free MCP endpoint."
            ),
            status_code=400,
        )
    methods = list(
        dict.fromkeys(
            str(method or "").strip().upper()
            for method in request_methods
            if str(method or "").strip()
        )
    )
    if not methods:
        raise APIError(
            code="SANDBOX_CONFIG_INVALID",
            message="an MCP Credential Vault binding needs an HTTP method",
            status_code=400,
        )

    credentials: list[Credential] = []
    header_entries: list[CustomHeaderEntry] = []
    seen_headers: set[str] = set()
    normalized_sources = {
        str(header or "").strip().lower(): str(source or "").strip()
        for header, source in header_sources.items()
    }
    for header_name, raw_value in sorted(
        headers.items(), key=lambda item: item[0].lower()
    ):
        header = str(header_name or "").strip()
        key = header.lower()
        value = str(raw_value)
        if (
            not _HEADER_NAME.fullmatch(header)
            or key in _RESERVED_CREDENTIAL_HEADERS
        ):
            raise APIError(
                code="SANDBOX_CONFIG_INVALID",
                message=f"MCP credential uses unsupported request header {header!r}",
                status_code=400,
            )
        if key in seen_headers:
            raise APIError(
                code="VAULT_CREDENTIAL_CONFLICT",
                message=f"MCP credentials claim request header {header!r} twice",
                status_code=409,
            )
        if not value:
            raise APIError(
                code="SANDBOX_CONFIG_INVALID",
                message=f"MCP credential header {header!r} has an empty value",
                status_code=400,
            )
        source = normalized_sources.get(key, "")
        if not source:
            raise APIError(
                code="SANDBOX_CONFIG_INVALID",
                message=(
                    f"MCP credential header {header!r} has no stable source identity"
                ),
                status_code=500,
            )
        seen_headers.add(key)
        credential_name = (
            f"{binding_name}-h-"
            f"{hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]}-s-"
            f"{hashlib.sha256(source.encode('utf-8')).hexdigest()[:12]}"
        )
        credentials.append(
            Credential(
                name=credential_name,
                source=InlineCredentialSource(type="inline", value=value),
            )
        )
        header_entries.append(
            CustomHeaderEntry(name=header, credential=credential_name)
        )

    auth = (
        CredentialAuth(type="customHeaders", headers=header_entries)
        if header_entries
        else CredentialAuth(type="passthrough")
    )
    binding = CredentialBinding(
        name=binding_name,
        match=CredentialMatch(
            hosts=[host],
            schemes=[scheme],
            methods=methods,
            paths=[parsed.path or "/"],
        ),
        auth=auth,
    )
    return credentials, [binding]


def _host_and_scheme(
    entry: str, *, credential_id: str, allow_insecure_http: bool
) -> tuple[str, Literal["https", "http"]]:
    """One seam allowlist entry → the ``(host, scheme)`` upstream can match on.

    Upstream matches an exact FQDN or a leftmost-label wildcard (``*.example.com``
    matches ``a.b.example.com`` but not the apex), and it derives the port from
    the scheme — 443 for https, 80 for http, nothing else, because those are the
    only ports the egress sidecar redirects into its intercepting proxy.

    Every entry this cannot express is REFUSED, naming the entry. The tempting
    alternative — drop the port, widen the wildcard, keep going — writes a
    binding LOOSER than the one its owner asked for, which is the single failure
    this mechanism exists to prevent.
    """

    def _refuse(reason: str) -> APIError:
        return APIError(
            code="SANDBOX_CONFIG_INVALID",
            message=(
                f"credential {credential_id!r} allows host {entry!r}, which the "
                f"sandbox backend's egress vault cannot bind: {reason}"
            ),
            status_code=400,
        )

    text = str(entry or "").strip().lower().rstrip(".")
    if not text:
        raise _refuse("the entry is empty")

    host: str = text
    scheme: Literal["https", "http"] = "https"
    if ":" in text and not text.endswith("]"):  # tolerate bare IPv6 without a port
        head, _, port_text = text.rpartition(":")
        if port_text.isdigit():
            port = int(port_text)
            if port == 443:
                host, scheme = head, "https"
            elif port == 80:
                host, scheme = head, "http"
            else:
                raise _refuse(
                    f"port {port} is not intercepted (the sidecar redirects only "
                    "80 and 443, so a credential bound here would never be injected)"
                )
    if not host:
        raise _refuse("the entry names a port but no host")
    if scheme == "http" and not allow_insecure_http:
        raise _refuse(
            "it resolves to cleartext http and the credential did not set "
            "allow_insecure_http (injecting a secret unencrypted defeats the "
            "structural secrecy the placeholder exists for)"
        )
    if host.startswith("*."):
        rest = host[2:]
        if not rest or "*" in rest:
            raise _refuse("only a leftmost-label wildcard such as *.example.com is valid")
    elif "*" in host:
        raise _refuse("only a leftmost-label wildcard such as *.example.com is valid")
    if not _is_credential_fqdn(host):
        raise _refuse(
            "the host must be a fully qualified domain name, not an IP address "
            "or single-label name"
        )
    return host, scheme


def build_env_credential_vault_write(
    credentials: Iterable[EgressCredential],
) -> tuple[list[Credential], list[CredentialBinding]]:
    """``(credentials, bindings)`` for the vault's ``environment_variable`` entries.

    The sandbox gets each credential's placeholder under its own ``secret_name``;
    upstream replaces that literal on the way OUT, on the surfaces the credential
    enabled and only for the hosts its allowlist named. This is the same promise
    :mod:`astrabox.seams.egress_credentials` states — expressed in the upstream
    vault's match language instead of executed in this process, because the
    substitution happens in the egress sidecar, which is the only place that sees
    the request after the box encrypted it.

    ``passthrough`` is the auth type for exactly this: no typed header is
    attached, the binding carries nothing but the substitution rules. A model
    gateway credential is the other shape and stays on :func:`build_vault_write`.

    Refuses, rather than narrows, anything upstream cannot express — see
    :func:`_host_and_scheme`, and ``networking.unrestricted`` below.
    """
    out_credentials: list[Credential] = []
    out_bindings: list[CredentialBinding] = []

    for cred in credentials:
        name = f"astrabox-vault-{cred.credential_id}"
        kind = str(cred.networking.get("type") or "").strip()
        if kind == "unrestricted":
            raise APIError(
                code="SANDBOX_CONFIG_INVALID",
                message=(
                    f"credential {cred.credential_id!r} is networking.unrestricted, "
                    "which this backend's egress vault cannot express: upstream "
                    "requires every binding to name its hosts, and a credential "
                    "that may go anywhere cannot be bound to somewhere. Give it "
                    'networking {"type": "limited", "allowed_hosts": [...]}.'
                ),
                status_code=400,
            )
        raw_hosts = cred.networking.get("allowed_hosts")
        if not isinstance(raw_hosts, (list, tuple)) or not raw_hosts:
            raise APIError(
                code="SANDBOX_CONFIG_INVALID",
                message=(
                    f"credential {cred.credential_id!r} names no allowed hosts, so "
                    "there is nowhere its secret may be sent"
                ),
                status_code=400,
            )

        surfaces = [
            surface
            for surface, enabled in (
                ("header", bool(cred.injection_location.get("header"))),
                ("body", bool(cred.injection_location.get("body"))),
            )
            if enabled
        ]
        if not surfaces:
            raise APIError(
                code="SANDBOX_CONFIG_INVALID",
                message=(
                    f"credential {cred.credential_id!r} enables neither header nor "
                    "body injection, so its placeholder would never be replaced"
                ),
                status_code=400,
            )

        # One binding per scheme: two bindings that differ in scheme can never
        # both match one request, so this cannot create the ambiguous-match case
        # upstream fails closed on.
        by_scheme: dict[Literal["https", "http"], list[str]] = {}
        for entry in raw_hosts:
            host, scheme = _host_and_scheme(
                str(entry),
                credential_id=cred.credential_id,
                allow_insecure_http=cred.allow_insecure_http,
            )
            hosts = by_scheme.setdefault(scheme, [])
            if host not in hosts:
                hosts.append(host)

        out_credentials.append(
            Credential(
                name=name,
                source=InlineCredentialSource(type="inline", value=cred.secret_value),
            )
        )
        for scheme, hosts in sorted(by_scheme.items()):
            allowed_requests = dict(cred.allowed_requests or {})
            match_kwargs: dict[str, object] = {
                "hosts": hosts,
                "schemes": [scheme],
            }
            methods = allowed_requests.get("methods")
            paths = allowed_requests.get("paths")
            if isinstance(methods, (list, tuple)) and methods:
                match_kwargs["methods"] = list(methods)
            if isinstance(paths, (list, tuple)) and paths:
                match_kwargs["paths"] = list(paths)
            out_bindings.append(
                CredentialBinding(
                    name=name if scheme == "https" else f"{name}-{scheme}",
                    match=CredentialMatch.model_validate(match_kwargs),
                    auth=CredentialAuth(
                        type="passthrough",
                        # Through the alias: ``in`` is a Python keyword, so the
                        # model spells the field ``in_`` and only the mapping
                        # form carries the name upstream actually reads.
                        substitutions=[
                            CredentialSubstitution.model_validate(
                                {
                                    "credential": name,
                                    "placeholder": cred.placeholder,
                                    "in": list(surfaces),
                                }
                            )
                        ],
                    ),
                )
            )

    return out_credentials, out_bindings


def open_sandbox_vault_write(
    plan: SandboxEgressCredentialPlan | None,
) -> tuple[list[Credential], list[CredentialBinding]] | None:
    """Translate AstraBox credential intent at the OpenSandbox boundary."""

    if plan is None:
        return None
    credentials: list[Credential] = []
    bindings: list[CredentialBinding] = []

    for model_item in plan.model:
        item_credentials, item_bindings = build_vault_write(
            credential=model_item.secret_value,
            credential_header=model_item.credential_header,
            base_url=model_item.base_url,
            request_methods=model_item.request_methods,
            request_paths=model_item.request_paths,
            name=model_item.name,
            extra_substitutions=(
                (item.placeholder, item.name)
                for item in model_item.substitutions
            ),
        )
        item_credentials.extend(
            Credential(
                name=item.name,
                source=InlineCredentialSource(
                    type="inline", value=item.secret_value
                ),
            )
            for item in model_item.substitutions
        )
        credentials.extend(item_credentials)
        bindings.extend(item_bindings)

    if plan.environment:
        item_credentials, item_bindings = build_env_credential_vault_write(
            plan.environment
        )
        credentials.extend(item_credentials)
        bindings.extend(item_bindings)

    for mcp_item in plan.mcp:
        try:
            if mcp_item.headers and "sse" in mcp_item.transports:
                raise APIError(
                    code="SANDBOX_CONFIG_INVALID",
                    message=(
                        "OpenSandbox cannot safely bind authenticated SSE MCP "
                        f"server {mcp_item.server_url!r}: its POST target is "
                        "supplied dynamically and cannot be scoped before the "
                        "connection"
                    ),
                    status_code=400,
                )
            item_credentials, item_bindings = build_mcp_header_vault_write(
                server_url=mcp_item.server_url,
                headers=dict(mcp_item.headers),
                header_sources=dict(mcp_item.header_sources),
                name=mcp_item.name,
                request_methods=mcp_item.request_methods,
            )
        except APIError:
            # An anonymous direct endpoint needs no provider binding. Keep its
            # neutral plan so a later credential refresh reaches this adapter,
            # but do not make an OpenSandbox-only match limitation block the
            # unauthenticated connection.
            if mcp_item.headers:
                raise
            continue
        credentials.extend(item_credentials)
        bindings.extend(item_bindings)

    for group in plan.http_basic:
        for item in group.credentials:
            host, scheme = _gateway_match(item.url)
            parsed = urlparse(item.url)
            path = parsed.path.rstrip("/")
            if scheme != "https" or not path or parsed.query or parsed.fragment or parsed.username:
                raise APIError(
                    code="SANDBOX_CONFIG_INVALID",
                    message="HTTP Basic credentials require a clean HTTPS destination path",
                    status_code=400,
                )
            destination = hashlib.sha256(item.url.encode()).hexdigest()[:24]
            name = f"astrabox-basic-{destination}-v-{group.scope_id}-i-{item.credential_id}"
            credentials.append(
                Credential(
                    name=name,
                    source=InlineCredentialSource(
                        type="inline",
                        value=base64.b64encode(f"{item.username}:{item.password}".encode()).decode("ascii"),
                    ),
                )
            )
            bindings.append(
                CredentialBinding(
                    name=name,
                    match=CredentialMatch(
                        hosts=[host], schemes=[scheme],
                        paths=[path, f"{path}/*"], methods=["GET", "HEAD", "POST"],
                    ),
                    auth=CredentialAuth(type="basic", credential=name),
                )
            )

    return credentials, bindings


__all__ = [
    "build_env_credential_vault_write",
    "build_mcp_header_vault_write",
    "build_vault_write",
    "gateway_host",
    "open_sandbox_vault_write",
    "require_vault_preconditions",
    "carry_workload_substitutions",
    "workload_of_credential_name",
    "workload_substitutions",
]
