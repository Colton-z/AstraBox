"""Where a RuntimeProfile's engine sends its spans, and what reaches the CLI.

The credential reaches the exporter by reference without being written back to
storage, an operator's value cannot split the exporter's header or attribute
list, and prompt recording is the only content capture a RuntimeProfile turns on.
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.claude_code_config import (
    _claude_tracing_env,
)
from astrabox.seams.tracing import (
    parse_tracing_config,
    runtime_tracing_spec,
    tracing_egress_hosts,
)

_ENDPOINT = "https://collector.example.com/api/public/otel"


def _doc(**over: object) -> dict[str, object]:
    return {"enabled": True, "endpoint": _ENDPOINT, **over}


# ── not configured, and configured-but-off ──────────────────────────────────


@pytest.mark.parametrize("token", ["Basic cGs6c2s=", "Bearer otel-reference-token"])
def test_secret_reference_reaches_native_authorization_without_changing_storage(
    monkeypatch: pytest.MonkeyPatch, token: str,
) -> None:
    monkeypatch.setenv("TRACING_COLLECTOR_AUTH", token)
    stored = _doc(auth_token_secret_name="tracing-collector-auth")
    original = deepcopy(stored)
    template = SimpleNamespace(runtime_profile_name="traced", tracing=stored)

    spec = runtime_tracing_spec(template)

    assert spec is not None and spec.active
    assert _claude_tracing_env(spec)["OTEL_EXPORTER_OTLP_HEADERS"] == f"Authorization={token}"
    assert tracing_egress_hosts(template) == ["collector.example.com"]
    assert stored == original


# ── the checks that refuse ──────────────────────────────────────────────────


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


# ── the vendor translation ──────────────────────────────────────────────────


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
