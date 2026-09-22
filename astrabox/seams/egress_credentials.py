"""Egress credential substitution — THE semantics, as importable code.

``environment_variable`` vault credentials promise structural secrecy: the
sandbox only ever sees an opaque placeholder in its environment; the REAL
value is substituted into outbound requests at the backend's egress boundary
(``SandboxProvider.supports_egress_credential_injection``). This module makes
that substitution contract precise and testable, so every backend implements
the SAME semantics instead of each proxy author re-inventing the
security-sensitive part. It is import-light (stdlib only), like every seam.

The contract, normatively:

* **Placeholder** — ``mint_placeholder()`` returns
  ``ASTRABOX-VAULT-CRED::<credential_id>::<nonce>``. Minted fresh per
  session attach (the nonce dies with the session); injected into the
  sandbox env under the credential's ``secret_name``. The placeholder is the
  ONLY thing that may enter the sandbox, model context, logs, or transcripts.
* **Substitution points** — outbound request HEADERS (when the credential's
  ``injection_location.header``) and BODY (when ``.body``). Each placeholder
  occurrence is replaced with the secret value; responses are never touched.
* **Host gating** — substitution happens only when the request's host matches
  the credential's ``networking`` allowlist: ``unrestricted``, or ``limited``
  with entries matching case-insensitively — exact hostname, or any-depth
  subdomain via a ``*.example.com`` entry (the apex needs its own entry).
  An entry carrying ``:port`` must match the request port; a port-less entry
  matches any port.
* **TLS gating** — substitution requires ``scheme == "https"``. A plaintext
  ``http://`` request to an allowed host is REFUSED unless the credential
  explicitly opted in (``allow_insecure_http=True`` — internal cleartext
  targets only); injecting a real secret onto the wire unencrypted would
  defeat structural secrecy at the last hop.
* **FAIL-CLOSED** — a request containing ANY placeholder that may not be
  substituted (host not allowed, scheme not allowed, or the placeholder sits
  in a location its credential disabled) is REFUSED
  (:class:`EgressSubstitutionRefused`), never forwarded with the placeholder
  inside. Forwarding would leak call intent to an unapproved host and
  silently break the agent's request anyway.
* **Whole-body contract** — :meth:`EgressCredentialMap.apply` scans a
  COMPLETE header set + body. A proxy handling streamed request bodies must
  either buffer the body or scan with a rolling window of at least
  :attr:`EgressCredentialMap.max_placeholder_length` - 1 carried bytes across
  chunk boundaries; feeding it per-chunk fragments silently defeats both
  substitution and the fail-closed rule (a placeholder split across chunks
  matches nothing).

A backend's egress proxy builds one :class:`EgressCredentialMap` per session
from the vault entries, exports each ``secret_name=placeholder`` into the
sandbox env, and calls :meth:`EgressCredentialMap.apply` on every outbound
request. What remains backend-specific is only transport plumbing: how the
proxy sits on the egress path (see ``docs/egress-credential-injection.md``).
"""

from __future__ import annotations

import hashlib
import secrets as _secrets
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Iterable, Mapping
from urllib.parse import urlparse

__all__ = [
    "PLACEHOLDER_PREFIX",
    "EgressCredential",
    "ModelEgressCredential",
    "ModelEgressCredentialSubstitution",
    "MCPHeaderEgressCredential",
    "HTTPBasicEgressCredential",
    "HTTPBasicEgressCredentialSet",
    "SandboxEgressCredentialPlan",
    "EgressCredentialMap",
    "EgressSubstitutionRefused",
    "MCPOutboundCredential",
    "MCPOutboundCredentialResolution",
    "SubstitutionResult",
    "EGRESS_HELD_PLACEHOLDER",
    "credential_request_path",
    "merge_credential_plans",
    "mint_placeholder",
    "workload_placeholder_context",
    "workload_credential_name",
    "workload_model_placeholder",
    "host_matches",
]

PLACEHOLDER_PREFIX = "ASTRABOX-VAULT-CRED::"

#: Public inert value an engine receives instead of a model credential. This is
#: an AstraBox contract, not a provider token: any credential-injection backend
#: must make outbound authentication succeed while this remains the only value
#: visible inside the sandbox.
EGRESS_HELD_PLACEHOLDER = "astrabox-credential-held-by-egress-sidecar"


@dataclass(frozen=True)
class ModelEgressCredentialSubstitution:
    """One prepared runtime's bearer mapped through a model binding."""

    name: str
    secret_value: str = field(repr=False)
    placeholder: str


@dataclass(frozen=True)
class ModelEgressCredential:
    """Provider-neutral protected model authentication for one endpoint."""

    name: str
    secret_value: str = field(repr=False)
    credential_header: str
    base_url: str
    request_methods: tuple[str, ...]
    request_paths: tuple[str, ...]
    substitutions: tuple[ModelEgressCredentialSubstitution, ...] = ()


@dataclass(frozen=True)
class MCPHeaderEgressCredential:
    """Provider-neutral protected request headers for one direct MCP URL."""

    name: str
    server_url: str
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    header_sources: Mapping[str, str] = field(default_factory=dict)
    request_methods: tuple[str, ...] = ("GET", "POST", "DELETE")
    transports: tuple[str, ...] = ()


def credential_request_path(base_url: str, suffix: str) -> str:
    """Build the absolute request path an engine adds to an endpoint URL."""

    parsed = urlparse(str(base_url or "").strip())
    base_path = str(parsed.path or "").strip().rstrip("/")
    request_suffix = str(suffix or "").strip().lstrip("/")
    if not request_suffix:
        raise ValueError("a protected credential request path cannot be empty")
    return f"{base_path}/{request_suffix}" if base_path else f"/{request_suffix}"


def mint_placeholder(credential_id: str, *, context: str | None = None) -> str:
    """Mint one opaque placeholder for a credential.

    Without ``context`` every call gets a fresh nonce.  A caller that must
    resolve the same live sandbox more than once may supply its private,
    per-sandbox generation token: that keeps the placeholder stable for that
    generation while changing it after recovery creates the next box.
    """
    cred = str(credential_id or "").strip()
    if not cred:
        raise ValueError("credential_id must be non-empty")
    scope = str(context or "").strip()
    nonce = (
        hashlib.sha256(f"{scope}\0{cred}".encode()).hexdigest()[:32]
        if scope
        else _secrets.token_hex(16)
    )
    return f"{PLACEHOLDER_PREFIX}{cred}::{nonce}"


def workload_placeholder_context(workload_id: str) -> str:
    """Placeholder scope for one workload — one egress identity.

    A workload is a running engine whose outbound calls must be attributable.
    Deliberately not "a sandbox, or an isolated session": defining it as a
    union of resource shapes means a backend with a third shape forces every
    call site to learn a new member, and nothing in this module may branch on
    which shape a workload has. Its id is a platform one the backend treats as
    opaque — the conversation id once the workload has an owner, the prepared
    slot id before that.

    Unique per workload on purpose: a placeholder is the bearer the egress
    boundary substitutes, so two workloads — even of one Agent, even in one
    box — must never share one. The scope is derived, not stored, so the side
    that claims a prepared workload reaches the same placeholder as the side
    that prepared it, and a recovery that discards it mints a fresh one.
    """
    normalized = str(workload_id or "").strip()
    if not normalized:
        raise ValueError("a workload placeholder context requires a workload id")
    return f"prepared-slot:{normalized}"


def workload_model_placeholder(workload_id: str) -> str:
    """The model gateway bearer one workload is spawned with.

    Worthless like :data:`EGRESS_HELD_PLACEHOLDER`, and unique per workload so
    the egress boundary can substitute each sibling's traffic with that
    workload's own credential. Fail-closed by construction: a bearer the
    boundary does not know is forwarded verbatim and refused upstream, never
    silently rewritten into someone else's identity.

    The literal shape is what boxes already in flight carry, so it is fixed
    here rather than restyled; what moved is who defines it. A backend that
    minted its own naming made the platform learn that backend's vocabulary,
    which is how five separate writers came to compose the same substitution
    table and one of them forgot the rule.
    """
    normalized = str(workload_id or "").strip()
    if not normalized:
        raise ValueError("a workload model placeholder requires a workload id")
    return f"astrabox-slot-credential-{normalized}"


def workload_credential_name(workload_id: str) -> str:
    """The protected model credential backing one workload.

    The sibling of :func:`workload_model_placeholder`: that one names what the
    workload is spawned holding, this one names what the egress boundary
    substitutes in its place. Both are per workload for the same reason, and
    both keep the literal shape boxes already in flight carry.
    """

    normalized = str(workload_id or "").strip()
    if not normalized:
        raise ValueError("a workload credential name requires a workload id")
    return f"astrabox-slot-{normalized}"


def host_matches(request_host: str, request_port: int | None, allowed: str) -> bool:
    """One allowlist entry vs one request host[:port] (see module contract)."""
    entry = str(allowed or "").strip().lower()
    host = str(request_host or "").strip().lower().rstrip(".")
    if not entry or not host:
        return False
    entry_port: int | None = None
    if ":" in entry and not entry.endswith("]"):  # tolerate bare IPv6 without port
        entry_host, _, port_text = entry.rpartition(":")
        if port_text.isdigit():
            entry, entry_port = entry_host, int(port_text)
    if entry_port is not None and entry_port != (request_port if request_port is not None else 443):
        return False
    entry = entry.rstrip(".")
    if entry.startswith("*."):
        suffix = entry[1:]  # ".example.com"
        return host.endswith(suffix) and host != suffix.lstrip(".")
    return host == entry


@dataclass(frozen=True)
class EgressCredential:
    """One resolved ``environment_variable`` credential, egress-side view."""

    credential_id: str
    secret_name: str
    #: The real secret. ``repr=False``: an accidentally logged credential or
    #: map must never print the value (the placeholder is the loggable name).
    secret_value: str = field(repr=False)
    placeholder: str = ""
    #: ``{"type": "unrestricted"}`` or ``{"type": "limited", "allowed_hosts": [...]}``
    networking: Mapping[str, object] = field(default_factory=dict)
    #: ``{"header": bool, "body": bool}``
    injection_location: Mapping[str, bool] = field(default_factory=dict)
    #: Substitution onto plaintext ``http://`` is refused unless this is True
    #: (explicit per-credential opt-in for internal cleartext targets).
    allow_insecure_http: bool = False
    #: Optional HTTP request limits. Empty means the host and scheme are the
    #: complete destination rule. When present, ``methods`` contains normalized
    #: HTTP methods and ``paths`` contains absolute glob patterns such as
    #: ``/repos/acme/private/*``.
    allowed_requests: Mapping[str, object] = field(default_factory=dict)

    def allows_host(self, host: str, port: int | None) -> bool:
        kind = str(self.networking.get("type") or "").strip()
        if kind == "unrestricted":
            return True
        hosts = self.networking.get("allowed_hosts")
        if not isinstance(hosts, (list, tuple)):
            return False
        return any(host_matches(host, port, str(h)) for h in hosts)

    def allows_method(self, method: str) -> bool:
        methods = self.allowed_requests.get("methods")
        if not isinstance(methods, (list, tuple)) or not methods:
            return True
        candidate = str(method or "").strip().upper()
        return candidate in {str(value).strip().upper() for value in methods}

    def allows_path(self, path: str) -> bool:
        paths = self.allowed_requests.get("paths")
        if not isinstance(paths, (list, tuple)) or not paths:
            return True
        candidate = str(path or "").strip()
        return any(fnmatchcase(candidate, str(pattern)) for pattern in paths)


@dataclass(frozen=True)
class HTTPBasicEgressCredential:
    """HTTP Basic authentication for one HTTPS destination and its children.

    Providers encode the username/password and inject the authentication header
    at egress. Neither value is delivered through the workload environment.
    """

    credential_id: str
    url: str
    username: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class HTTPBasicEgressCredentialSet:
    """Complete Basic credential selection for one managed Vault binding.

    An empty selection revokes that scope's old bindings on the next apply.
    Omitting the set leaves it untouched during unrelated credential writes.
    """

    scope_id: str
    credentials: tuple[HTTPBasicEgressCredential, ...] = ()


@dataclass(frozen=True)
class SandboxEgressCredentialPlan:
    """Protected-credential intent carried across the sandbox provider seam.

    These are application-level requests, not one provider's Vault models.
    Providers translate each category into their own egress mechanism or fail
    before allocating a sandbox. An Environment network mode is intentionally
    absent; the request-bound destinations are themselves the exact additional
    reachability authorized by attaching this plan.
    """

    model: tuple[ModelEgressCredential, ...] = ()
    environment: tuple[EgressCredential, ...] = ()
    mcp: tuple[MCPHeaderEgressCredential, ...] = ()
    http_basic: tuple[HTTPBasicEgressCredentialSet, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.model or self.environment or self.mcp or self.http_basic)


def merge_credential_plans(
    *plans: SandboxEgressCredentialPlan | None,
) -> SandboxEgressCredentialPlan | None:
    """Combine independent credential sources without provider translation."""

    present = [plan for plan in plans if plan is not None and not plan.is_empty]
    if not present:
        return None
    return SandboxEgressCredentialPlan(
        model=tuple(item for plan in present for item in plan.model),
        environment=tuple(item for plan in present for item in plan.environment),
        mcp=tuple(item for plan in present for item in plan.mcp),
        http_basic=tuple(item for plan in present for item in plan.http_basic),
    )


@dataclass(frozen=True)
class MCPOutboundCredential:
    """Resolved headers for one MCP destination, still outside the sandbox.

    ``target_url`` is the immutable Vault lookup key. The runtime may send the
    request to a provider gateway instead; the engine adapter maps these
    headers onto that actual outbound URL when it writes the egress sidecar.
    """

    credential_id: str
    target_url: str
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class MCPOutboundCredentialResolution:
    """One Session's immutable Vault scope and its current MCP matches.

    ``scope_id`` fingerprints the ordered Vault binding captured when the
    Session was created. It lets an Agent-shared sandbox distinguish two
    conversations that are both unauthenticated *now* but could resolve
    differently after a credential is added or archived. The opaque digest is
    safe to carry in sidecar entry names; raw Vault ids never leave the host.
    """

    scope_id: str
    credentials: tuple[MCPOutboundCredential, ...] = ()


class EgressSubstitutionRefused(Exception):
    """Fail-closed refusal: the request must NOT be forwarded (see reason)."""

    def __init__(self, reason: str, *, credential_id: str, location: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.credential_id = credential_id
        self.location = location


@dataclass
class SubstitutionResult:
    #: headers/body carry the SUBSTITUTED real secret after a successful call —
    #: repr=False so an accidental log/repr never prints them (only the
    #: substituted credential ids, which are safe, show).
    headers: dict[str, str] = field(repr=False)
    body: str = field(repr=False)
    substituted_credential_ids: list[str] = field(default_factory=list)


class EgressCredentialMap:
    """The per-session placeholder→credential map an egress proxy holds."""

    def __init__(self, credentials: Iterable[EgressCredential]) -> None:
        self._by_placeholder: dict[str, EgressCredential] = {}
        for cred in credentials:
            if not cred.placeholder.startswith(PLACEHOLDER_PREFIX):
                raise ValueError(
                    f"credential {cred.credential_id!r} placeholder was not minted "
                    "by mint_placeholder()"
                )
            if cred.placeholder in self._by_placeholder:
                raise ValueError(f"duplicate placeholder for {cred.credential_id!r}")
            self._by_placeholder[cred.placeholder] = cred

    def sandbox_env(self) -> dict[str, str]:
        """What the sandbox environment gets: ``secret_name -> placeholder``."""
        return {c.secret_name: c.placeholder for c in self._by_placeholder.values()}

    @property
    def max_placeholder_length(self) -> int:
        """Longest placeholder in this map — the streamed-body scan window.

        A proxy scanning a chunked body must carry at least this many minus
        one bytes across chunk boundaries (see the whole-body contract in the
        module docstring)."""
        return max((len(p) for p in self._by_placeholder), default=0)

    def apply(
        self,
        *,
        scheme: str,
        host: str,
        port: int | None,
        method: str = "",
        path: str = "",
        headers: Mapping[str, str],
        body: str = "",
    ) -> SubstitutionResult:
        """Substitute every allowed placeholder; refuse fail-closed otherwise.

        ``scheme`` is the outbound request's URL scheme and is REQUIRED:
        substitution onto anything but ``https`` is refused unless the
        credential set ``allow_insecure_http``. Raises
        :class:`EgressSubstitutionRefused` when any placeholder in the
        request may not be substituted — the proxy must return an error to the
        sandbox, never forward the request.

        SINGLE-TARGET CONTRACT: the returned :class:`SubstitutionResult` is
        valid ONLY for the exact ``(scheme, host, port)`` this call checked —
        the allowlist decision is baked into the substituted bytes. A proxy
        that follows an HTTP redirect (or retries against a different
        endpoint) MUST NOT forward the substituted headers/body to the new
        target: that would deliver the real secret to a host the allowlist
        never approved (an upstream 3xx becomes credential exfiltration).
        Re-run ``apply()`` against the redirect target's ORIGINAL
        placeholder-bearing request instead — it re-checks scheme and host and
        refuses fail-closed — or simply surface the 3xx to the sandbox and let
        the client re-issue.
        """
        normalized_scheme = str(scheme or "").strip().lower()
        out_headers = dict(headers)
        out_body = body
        substituted: list[str] = []
        for placeholder, cred in self._by_placeholder.items():
            in_headers = any(placeholder in v for v in out_headers.values())
            in_body = placeholder in out_body
            if not in_headers and not in_body:
                continue
            if normalized_scheme != "https" and not cred.allow_insecure_http:
                raise EgressSubstitutionRefused(
                    f"credential {cred.credential_id!r} ({cred.secret_name}) "
                    f"refuses substitution onto scheme {normalized_scheme!r} — "
                    "injecting a secret over cleartext defeats structural "
                    "secrecy (set allow_insecure_http on the credential only "
                    "for internal cleartext targets)",
                    credential_id=cred.credential_id,
                    location="scheme",
                )
            if not cred.allows_host(host, port):
                raise EgressSubstitutionRefused(
                    f"credential {cred.credential_id!r} ({cred.secret_name}) is not "
                    f"allowed for host {host!r} (networking allowlist)",
                    credential_id=cred.credential_id,
                    location="host",
                )
            if not cred.allows_method(method):
                raise EgressSubstitutionRefused(
                    f"credential {cred.credential_id!r} ({cred.secret_name}) is not "
                    f"allowed for HTTP method {str(method or '').strip().upper()!r}",
                    credential_id=cred.credential_id,
                    location="method",
                )
            if not cred.allows_path(path):
                raise EgressSubstitutionRefused(
                    f"credential {cred.credential_id!r} ({cred.secret_name}) is not "
                    f"allowed for request path {str(path or '').strip()!r}",
                    credential_id=cred.credential_id,
                    location="path",
                )
            if in_headers and not bool(cred.injection_location.get("header")):
                raise EgressSubstitutionRefused(
                    f"credential {cred.credential_id!r} placeholder appeared in a "
                    "request HEADER but header injection is disabled for it",
                    credential_id=cred.credential_id,
                    location="header",
                )
            if in_body and not bool(cred.injection_location.get("body")):
                raise EgressSubstitutionRefused(
                    f"credential {cred.credential_id!r} placeholder appeared in the "
                    "request BODY but body injection is disabled for it",
                    credential_id=cred.credential_id,
                    location="body",
                )
            if in_headers:
                out_headers = {
                    k: v.replace(placeholder, cred.secret_value)
                    for k, v in out_headers.items()
                }
            if in_body:
                out_body = out_body.replace(placeholder, cred.secret_value)
            substituted.append(cred.credential_id)
        return SubstitutionResult(
            headers=out_headers, body=out_body, substituted_credential_ids=substituted
        )
