"""Engine tracing — where an engine's own OpenTelemetry spans are sent.

Distinct from :mod:`astrabox.observability.tracing`, which traces THIS process:
FastAPI request spans for the AstraBox server, switched on by a deployment-wide
``OTEL_EXPORTER_OTLP_ENDPOINT``. This seam is about the OTHER end — the engine
running inside a sandbox, emitting its own spans about the work the agent did.
Two different subjects that may share one destination.

The Environment owns the collector destination and credential. An inline
``auth_token`` supplies the complete Authorization header; an
``auth_token_secret_name`` resolves through :class:`astrabox.secrets.SecretProvider`
from the server process environment when tracing is enabled.

Claude Code is the adapter that consumes this setting, translating it into
its native ``OTEL_*`` variables. The resolved credential enters that CLI's
process environment as plaintext; tracing does not use egress Vault
substitution. Other engines must declare and implement this configuration input
before an Environment can select it for them.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlparse

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.secrets import SecretProvider

logger = get_logger(__name__)

#: HTTP supports private collectors; HTTPS protects credentials in transit.
#: Tracing sends the configured header directly, without egress substitution.
_ALLOWED_SCHEMES = frozenset({"http", "https"})

#: OpenTelemetry's three signals, each with its own exporter switch on the
#: engine side.
TRACING_SIGNALS = ("traces", "metrics", "logs")

#: What an Environment gets when it does not choose. Traces and metrics, because
#: every OTLP receiver serves those two and the backend this repository documents
#: — Langfuse — answers 404 on `/v1/logs`. Turning all three on by default would
#: mean a rejected request every flush interval against the documented
#: collector, and the CLI drops export failures silently: the operator would see
#: partial data with nothing saying why.
DEFAULT_TRACING_SIGNALS = ("traces", "metrics")


@dataclass(frozen=True, slots=True)
class EngineTracingSpec:
    """One environment's answer to "where do the engine's spans go".

    ``auth_token`` and ``auth_token_secret_name`` are the same either/or the
    model provider's credential uses: an inline value (masked in read views) or
    a name resolved by SecretProvider. Neither is required — a trusted collector
    may want none.

    The token is the COMPLETE ``Authorization`` header value, scheme included:
    ``Basic <base64>`` for Langfuse, ``Bearer <token>`` elsewhere. Adapters send
    it as given and invent no scheme, because which one is right is the
    collector's answer. A backend that authenticates through some other header
    entirely uses ``headers``.

    ``enabled`` is a switch, not a presence test, for the reason prewarm has
    one: turning tracing off by clearing the endpoint would also discard the
    credential, so turning it back on would mean entering the secret again.
    Callers read :attr:`active` rather than testing the spec for truthiness.
    """

    endpoint: str
    headers: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    auth_token: str = ""
    auth_token_secret_name: str = ""
    environment: str = ""
    log_user_prompt: bool = False
    enabled: bool = False
    signals: tuple[str, ...] = DEFAULT_TRACING_SIGNALS

    @property
    def active(self) -> bool:
        """Whether this environment's engines should emit spans at all.

        The one question every consumer asks. A stored-but-off configuration is
        inert everywhere: no exporter is configured, no host is added to the
        egress allowlist, and no secret reference is resolved.
        """

        return self.enabled

    @property
    def endpoint_host(self) -> str:
        """The host an egress policy must admit for spans to leave the box."""

        parsed = urlparse(self.endpoint)
        return (parsed.hostname or "").lower()


def runtime_tracing_spec(template: Any) -> "EngineTracingSpec | None":
    """The spec a SESSION should act on, or None — never raising.

    `parse_tracing_config` is the one validator and it refuses a malformed or
    self-contradicting document, which is right at the form: the operator is
    looking at the field and can fix it.

    It is the wrong severity here. By session start nobody is looking at a form;
    a person is waiting for an answer, and this platform's own position on
    tracing is that it "is an observability side-channel, never load-bearing for
    a turn". A stored document the rules have since tightened — or one that
    reached the collection by another route — must therefore cost its spans, not
    the conversation. Measured: a `log_user_prompt` written before the `signals`
    rule existed terminated a real session at create, reported as a tracing
    message to someone who had asked a question.

    Loud, though, not silent: the refusal is logged with the environment named,
    because an operator who configured a collector and receives nothing needs the
    reason to exist somewhere.

    Enabled secret references are resolved into a new runtime spec, preserving
    the stored document. A missing reference disables tracing rather than
    exporting anonymously. Disabled tracing performs no secret lookup.
    """

    try:
        spec = parse_tracing_config(getattr(template, "tracing", None))
        if spec is None or not spec.active or not spec.auth_token_secret_name:
            return spec
        token = SecretProvider.get_secret(spec.auth_token_secret_name)
        if not token or not token.strip():
            raise _refuse(
                f"tracing.auth_token_secret_name {spec.auth_token_secret_name!r} "
                "is missing or empty in the server process environment"
            )
        return replace(spec, auth_token=token, auth_token_secret_name="")
    except APIError as exc:
        logger.error(
            "tracing disabled for environment %r: %s",
            getattr(template, "environment_name", None) or "<unnamed>",
            getattr(exc, "message", exc),
        )
        return None


def tracing_egress_hosts(template: Any) -> list[str]:
    """The collector host a sandbox of this Environment must be allowed to reach.

    ``runtime.config_resolver`` appends this host to a limited network policy.
    Credential delivery does not grant network access; an unrestricted policy
    requires no added host entry.

    Empty when tracing is absent or switched off, so a policy gains no host it
    has no use for.
    """

    spec = runtime_tracing_spec(template)
    if spec is None or not spec.active:
        return []
    host = spec.endpoint_host
    return [host] if host else []


def _refuse(message: str) -> APIError:
    return APIError(code="INVALID_REQUEST", message=message, status_code=400)


def parse_tracing_config(raw: Any) -> EngineTracingSpec | None:
    """Validate one environment's stored ``tracing`` document into a spec.

    Returns ``None`` for an absent or empty document — an environment that never
    opted in — and a spec otherwise. Raises ``APIError(INVALID_REQUEST, 400)`` on
    anything malformed. This runs both at admin WRITE time and when the runtime
    reads the document back, so the form and the
    session start can never disagree about what a valid configuration is.

    Nothing is defaulted or repaired. The checks are the ones that can be made
    without seeing the deployment:

    * an ``endpoint`` is required once anything else is set. A token, a header
      or a privacy switch with nowhere to send spans records an intention no
      session can act on — the same defect as prewarm switched on with no pool.
      ``enabled`` without one is the same defect stated outright;
    * the endpoint must be an absolute ``http``/``https`` URL with a host,
      because an egress policy needs the collector's host;
    * the two credential forms are mutually exclusive. Accepting both would
      leave which one wins to whoever reads the document next.

    Whether the collector exists, whether SecretProvider resolves the name, and
    whether the network policy admits the host are deliberately not checked
    here: the first two are answers this seam cannot see, and the third needs a
    sibling field, so it lives with the environment's other cross-field rules.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _refuse(f"tracing must be an object, got {type(raw).__name__}")
    if not any(str(value or "").strip() if isinstance(value, str) else value for value in raw.values()):
        # Every field empty is how a form renders "not configured"; it is the
        # same as the key being absent, not a configuration that failed.
        return None

    enabled_raw = raw.get("enabled")
    if enabled_raw is not None and not isinstance(enabled_raw, bool):
        raise _refuse("tracing.enabled must be a boolean")

    endpoint = str(raw.get("endpoint") or "").strip()
    if not endpoint:
        raise _refuse(
            "tracing.endpoint is required once any other tracing field is set; "
            "spans with no destination reach nothing"
        )
    parsed = urlparse(endpoint)
    if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.hostname:
        raise _refuse(
            "tracing.endpoint must be an absolute http:// or https:// URL naming "
            f"a host, got {endpoint!r}"
        )

    token = str(raw.get("auth_token") or "").strip()
    secret_name = str(raw.get("auth_token_secret_name") or "").strip()
    if token and secret_name:
        raise _refuse(
            "tracing carries both auth_token and auth_token_secret_name; name one "
            "source for the credential"
        )

    headers_raw = raw.get("headers") or {}
    if not isinstance(headers_raw, dict):
        raise _refuse(f"tracing.headers must be an object, got {type(headers_raw).__name__}")
    headers: dict[str, str] = {}
    for key, value in headers_raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise _refuse("tracing.headers must map strings to strings")
        name = key.strip()
        if not name:
            raise _refuse("tracing.headers carries an entry with an empty name")
        if "," in name or "," in value:
            # Every exporter this reaches renders the set as one
            # comma-separated string (`OTEL_EXPORTER_OTLP_HEADERS`), so a comma
            # inside an entry does not fail — it silently becomes two headers,
            # one of them malformed. Refused here, where the operator can see
            # which entry it was.
            raise _refuse(
                f"tracing.headers[{name!r}] contains a comma; the exporter's "
                "header list is comma-separated and would split it"
            )
        if "=" in name:
            # The same string splits name from value at the first `=`.
            raise _refuse(f"tracing.headers name {name!r} contains '='")
        headers[name] = value

    signals_raw = raw.get("signals")
    signals: tuple[str, ...]
    if signals_raw is None or signals_raw == []:
        signals = DEFAULT_TRACING_SIGNALS
    else:
        if not isinstance(signals_raw, list) or any(
            not isinstance(name, str) for name in signals_raw
        ):
            raise _refuse("tracing.signals must be a list of strings")
        seen: list[str] = []
        for name in signals_raw:
            key = name.strip().lower()
            if key not in TRACING_SIGNALS:
                raise _refuse(
                    f"tracing.signals contains {name!r}; expected any of "
                    f"{', '.join(TRACING_SIGNALS)}"
                )
            if key not in seen:
                seen.append(key)
        signals = tuple(seen)

    log_user_prompt = raw.get("log_user_prompt")
    if log_user_prompt is not None and not isinstance(log_user_prompt, bool):
        raise _refuse("tracing.log_user_prompt must be a boolean")
    if log_user_prompt and "logs" not in signals:
        # The vendor carries prompt text on log EVENTS, so this switch reaches
        # nothing without the logs signal. Refused rather than silently ignored:
        # an operator who asked to record prompts and got none would read that
        # as the setting not working, which is exactly what it would be.
        raise _refuse(
            "tracing.log_user_prompt needs 'logs' in tracing.signals — prompt "
            "text rides log events, so the switch reaches nothing without it"
        )

    return EngineTracingSpec(
        endpoint=endpoint,
        headers=MappingProxyType(headers),
        auth_token=token,
        auth_token_secret_name=secret_name,
        environment=str(raw.get("environment") or "").strip(),
        log_user_prompt=bool(log_user_prompt),
        enabled=bool(enabled_raw),
        signals=signals,
    )
