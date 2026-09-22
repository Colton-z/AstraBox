"""Where an Environment's engine sends its spans, and what reaches the CLI.

The cases cover configurations that can store and read back while emitting no
spans. Each assertion exercises the consumer that can make tracing silent:

* a credential given a scheme it did not ask for (`Bearer Basic …` → 401);
* a signal enabled against a collector that does not serve it (404 per flush);
* a switch that reaches no code because its signal is off;
* a resource-attribute value that splits the list instead of failing;
* a stored document the rules later tightened, killing a live conversation.
"""

from __future__ import annotations

from copy import deepcopy
import logging
from types import SimpleNamespace

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.claude_code_config import (
    _claude_tracing_env,
)
from astrabox.seams.tracing import (
    DEFAULT_TRACING_SIGNALS,
    parse_tracing_config,
    runtime_tracing_spec,
    tracing_egress_hosts,
)
from astrabox.secrets import SecretProvider

_ENDPOINT = "https://collector.example.com/api/public/otel"


def _doc(**over: object) -> dict[str, object]:
    return {"enabled": True, "endpoint": _ENDPOINT, **over}


# ── not configured, and configured-but-off ──────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [None, {}, {"enabled": False, "endpoint": "", "auth_token": ""}],
    ids=["absent", "empty", "every-field-blank"],
)
def test_an_environment_that_never_opted_in_has_no_spec(raw: object) -> None:
    """A form renders "not configured" as blank fields, not as a failure."""

    assert parse_tracing_config(raw) is None


def test_switching_tracing_off_keeps_the_credential() -> None:
    """`enabled` is a switch, not a presence test.

    Turning tracing off by clearing the endpoint would discard the token with
    it, so turning it back on would mean entering the secret again. The spec
    survives; `active` is what every consumer reads.
    """

    spec = parse_tracing_config(_doc(enabled=False, auth_token="Basic abc"))
    assert spec is not None
    assert spec.active is False
    assert spec.auth_token == "Basic abc"
    # Inert everywhere while off — no host joins an egress policy for it.
    assert tracing_egress_hosts(SimpleNamespace(tracing=_doc(enabled=False))) == []


@pytest.mark.parametrize("token", ["Basic cGs6c2s=", "Bearer otel-reference-token"])
def test_secret_reference_reaches_native_authorization_without_changing_storage(
    monkeypatch: pytest.MonkeyPatch, token: str,
) -> None:
    monkeypatch.setenv("TRACING_COLLECTOR_AUTH", token)
    stored = _doc(auth_token_secret_name="tracing-collector-auth")
    original = deepcopy(stored)
    template = SimpleNamespace(environment_name="traced", tracing=stored)

    spec = runtime_tracing_spec(template)

    assert spec is not None and spec.active
    assert _claude_tracing_env(spec)["OTEL_EXPORTER_OTLP_HEADERS"] == f"Authorization={token}"
    assert tracing_egress_hosts(template) == ["collector.example.com"]
    assert stored == original


@pytest.mark.parametrize("value", [None, "", "  \t"])
def test_missing_secret_disables_tracing_with_an_environment_diagnostic(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str | None,
) -> None:
    monkeypatch.setattr(logging.getLogger("astrabox"), "propagate", True)
    if value is None:
        monkeypatch.delenv("TRACING_COLLECTOR_AUTH", raising=False)
    else:
        monkeypatch.setenv("TRACING_COLLECTOR_AUTH", value)
    stored = _doc(auth_token_secret_name="tracing-collector-auth")
    original = deepcopy(stored)
    template = SimpleNamespace(environment_name="missing-collector-secret", tracing=stored)

    assert runtime_tracing_spec(template) is None
    assert tracing_egress_hosts(template) == []
    assert stored == original
    assert "tracing disabled for environment 'missing-collector-secret'" in caplog.text
    assert "tracing-collector-auth" in caplog.text
    assert "missing or empty" in caplog.text


@pytest.mark.parametrize("enabled", [False, True])
def test_disabled_or_inline_tracing_does_not_read_a_secret(
    monkeypatch: pytest.MonkeyPatch, enabled: bool,
) -> None:
    def unexpected_lookup(*args: object, **kwargs: object) -> str:
        raise AssertionError("this tracing configuration must not read a secret")

    monkeypatch.setattr(SecretProvider, "get_secret", unexpected_lookup)
    stored = _doc(auth_token="Basic inline") if enabled else _doc(
        enabled=False, auth_token_secret_name="unused-secret",
    )
    template = SimpleNamespace(tracing=stored)

    spec = runtime_tracing_spec(template)

    assert spec is not None and spec.active is enabled
    if enabled:
        assert _claude_tracing_env(spec)["OTEL_EXPORTER_OTLP_HEADERS"] == "Authorization=Basic inline"
    else:
        assert tracing_egress_hosts(template) == []
        assert spec.auth_token_secret_name == "unused-secret"


# ── the checks that refuse ──────────────────────────────────────────────────


def test_a_destination_is_required_once_anything_else_is_set() -> None:
    """A token with nowhere to send spans records an intention nothing can act
    on — the same defect as prewarm switched on with no pool named."""

    with pytest.raises(APIError) as caught:
        parse_tracing_config({"auth_token": "Basic abc"})
    assert "endpoint is required" in str(caught.value.message)


@pytest.mark.parametrize(
    "endpoint",
    ["collector.example.com", "/api/public/otel", "ftp://collector/x", "https://"],
    ids=["schemeless", "relative", "wrong-scheme", "no-host"],
)
def test_an_endpoint_with_no_host_is_refused(endpoint: str) -> None:
    """The host is what an egress policy admits and what the vault gates a
    credential on; a value with none has neither."""

    with pytest.raises(APIError):
        parse_tracing_config(_doc(endpoint=endpoint))


def test_two_credential_sources_are_refused() -> None:
    """Accepting both would leave which one wins to whoever reads next."""

    with pytest.raises(APIError):
        parse_tracing_config(_doc(auth_token="Basic abc", auth_token_secret_name="lf"))


@pytest.mark.parametrize(
    "headers",
    [{"X-Tenant": "a,b"}, {"X-Ten,ant": "a"}, {"X=Tenant": "a"}],
    ids=["comma-in-value", "comma-in-name", "equals-in-name"],
)
def test_a_header_that_would_split_the_exporter_list_is_refused(headers: dict) -> None:
    """`OTEL_EXPORTER_OTLP_HEADERS` is one comma-separated string. A comma inside
    an entry does not fail — it silently becomes two headers, one malformed."""

    with pytest.raises(APIError):
        parse_tracing_config(_doc(headers=headers))


# ── signals ─────────────────────────────────────────────────────────────────


def test_signals_default_to_the_two_every_receiver_serves() -> None:
    """Langfuse — the backend this repository documents — answers 404 on
    `/v1/logs`. Enabling all three by default would mean a rejected request
    every flush interval against the documented collector."""

    assert DEFAULT_TRACING_SIGNALS == ("traces", "metrics")
    assert parse_tracing_config(_doc()).signals == ("traces", "metrics")
    # An empty list is a form's "nothing selected", not a request for silence.
    assert parse_tracing_config(_doc(signals=[])).signals == ("traces", "metrics")


def test_an_unknown_signal_is_refused_rather_than_dropped() -> None:
    with pytest.raises(APIError) as caught:
        parse_tracing_config(_doc(signals=["trace"]))
    assert "traces, metrics, logs" in str(caught.value.message)


def test_recording_prompts_requires_the_signal_that_carries_them() -> None:
    """The vendor puts prompt text on log EVENTS. Without that signal the switch
    reaches no code, and an operator who asked for prompts and got none would
    read it as broken — which it would be."""

    with pytest.raises(APIError) as caught:
        parse_tracing_config(_doc(log_user_prompt=True))
    assert "log_user_prompt" in str(caught.value.message)

    spec = parse_tracing_config(_doc(log_user_prompt=True, signals=["traces", "logs"]))
    assert spec is not None and spec.log_user_prompt is True


# ── two severities, one validator ───────────────────────────────────────────


def test_a_session_never_dies_of_a_tracing_document() -> None:
    """Refusing is right at the form and wrong at session start.

    Measured: a `log_user_prompt` stored before the `signals` rule existed
    terminated a live conversation at sandbox create, answering someone's
    question with a tracing message. Tracing is a side-channel; a bad document
    costs its spans.
    """

    bad = SimpleNamespace(
        environment_name="claude-code",
        tracing={"enabled": True, "endpoint": _ENDPOINT, "log_user_prompt": True},
    )
    # The write path still refuses the same document.
    with pytest.raises(APIError):
        parse_tracing_config(bad.tracing)
    # The runtime read does not raise, and asks for no egress host.
    assert runtime_tracing_spec(bad) is None
    assert tracing_egress_hosts(bad) == []


def test_the_collector_host_is_what_an_egress_policy_must_admit() -> None:
    spec_host = SimpleNamespace(tracing=_doc(endpoint="https://Collector.EXAMPLE.com:4318/v1"))
    assert tracing_egress_hosts(spec_host) == ["collector.example.com"]


# ── the vendor translation ──────────────────────────────────────────────────


def test_every_switch_the_vendor_needs_is_named() -> None:
    """An endpoint alone configures nothing: telemetry is off until
    `CLAUDE_CODE_ENABLE_TELEMETRY`, and traces need the beta flag on top."""

    env = _claude_tracing_env(parse_tracing_config(_doc()))
    assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] == "1"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == _ENDPOINT
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
    # Export failures are silent by default, which makes "the collector rejected
    # this" indistinguishable from "tracing was never on".
    assert env["CLAUDE_CODE_OTEL_DIAG_STDERR"] == "1"
    # These boxes are disposable, so the vendor's 60s metric interval is a minute
    # of telemetry that dies with the sandbox.
    assert env["OTEL_METRIC_EXPORT_INTERVAL"] == "1000"
    assert env["OTEL_TRACES_EXPORT_INTERVAL"] == "1000"
    assert env["OTEL_LOGS_EXPORT_INTERVAL"] == "1000"


def test_an_unselected_signal_is_switched_off_rather_than_left_unset() -> None:
    env = _claude_tracing_env(parse_tracing_config(_doc(signals=["traces"])))
    assert env["OTEL_TRACES_EXPORTER"] == "otlp"
    assert env["OTEL_METRICS_EXPORTER"] == "none"
    assert env["OTEL_LOGS_EXPORTER"] == "none"
    # Never `console`: that exporter writes to stdout, the runner's own channel.
    assert "console" not in set(env.values())


def test_the_credential_is_sent_with_no_scheme_invented_for_it() -> None:
    """Langfuse authenticates with `Basic base64(public:secret)`. Prefixing
    `Bearer ` produced `Bearer Basic …` and a 401 naming the public key, which
    said nothing about the word this code had added in front of it.
    """

    env = _claude_tracing_env(parse_tracing_config(_doc(auth_token="Basic cGs6c2s=")))
    assert env["OTEL_EXPORTER_OTLP_HEADERS"] == "Authorization=Basic cGs6c2s="
    assert "Bearer" not in env["OTEL_EXPORTER_OTLP_HEADERS"]


def test_a_backend_with_its_own_header_needs_no_authorization() -> None:
    """Honeycomb reads `x-honeycomb-team`; the headers map is how it is told."""

    env = _claude_tracing_env(
        parse_tracing_config(_doc(headers={"x-honeycomb-team": "hcaik"}))
    )
    assert env["OTEL_EXPORTER_OTLP_HEADERS"] == "x-honeycomb-team=hcaik"


def test_resource_attribute_values_are_encoded_and_carry_the_conversation() -> None:
    """Attributes travel as one `k=v,k=v` string, so an operator typing a comma
    into the environment field would split the list rather than be refused. The
    conversation id is namespaced and deliberately not `session.id`: the CLI
    already puts its own session on every span."""

    env = _claude_tracing_env(
        parse_tracing_config(_doc(environment="prod, west")),
        session_id="4a727cff-d64b-4ed7-8531-7f6939b826af",
    )
    attributes = env["OTEL_RESOURCE_ATTRIBUTES"].split(",")
    assert attributes == [
        "deployment.environment=prod%2C%20west",
        "astrabox.conversation.id=4a727cff-d64b-4ed7-8531-7f6939b826af",
    ]


def test_prompt_recording_is_the_only_content_switch_opted_into() -> None:
    """The vendor has three more (tool details, tool bodies, raw API bodies). An
    environment that asked to see prompts did not ask for those."""

    env = _claude_tracing_env(
        parse_tracing_config(_doc(log_user_prompt=True, signals=["traces", "logs"]))
    )
    assert env["OTEL_LOG_USER_PROMPTS"] == "1"
    for other in ("OTEL_LOG_TOOL_DETAILS", "OTEL_LOG_TOOL_CONTENT", "OTEL_LOG_RAW_API_BODIES"):
        assert other not in env
