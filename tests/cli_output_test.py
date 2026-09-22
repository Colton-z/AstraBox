"""The failure contract: which exit code a caller gets, and what stdout holds.

An AI or a script driving this CLI branches on the exit code and parses stdout.
Both must therefore stay stable per kind of failure: a credential problem, an
address problem and a rejected request each need a different next action, and
`--output json` must never answer with prose.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from astrabox.cli import main
from astrabox.cli.client import ApiClient, Endpoint, resolve_endpoint
from astrabox.cli.output import (
    EXIT_AUTH,
    EXIT_CONFLICT,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_UNREACHABLE,
    EXIT_USAGE,
    CliError,
    emit_failure,
    render_table,
)

_CLI_ENV_VARS = (
    "ASTRABOX_ENDPOINT",
    "ASTRABOX_SERVER_HOST_PORT",
    "ASTRABOX_TOKEN",
    "ASTRABOX_CLIENT_ID",
    "ASTRABOX_CLIENT_SECRET",
    "ASTRABOX_TOKEN_URL",
    "ASTRABOX_SCOPE",
)


@pytest.fixture(autouse=True)
def _isolated_cli_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither the developer's shell nor a repository .env may steer these."""
    for name in _CLI_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "astrabox.config.settings.load_env_file_into_process_env", lambda *_a, **_k: None
    )


def _client_answering(status: int, body: Any) -> ApiClient:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return ApiClient(
        Endpoint(base_url="http://deployment.test", token=None),
        transport=httpx.MockTransport(handle),
    )


def _client_that_cannot_connect() -> ApiClient:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return ApiClient(
        Endpoint(base_url="http://deployment.test", token=None),
        transport=httpx.MockTransport(handle),
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, EXIT_AUTH),
        (403, EXIT_AUTH),
        (409, EXIT_CONFLICT),
        (400, EXIT_FAILED),
        (404, EXIT_FAILED),
        (500, EXIT_FAILED),
    ],
)
def test_http_status_maps_to_the_exit_code_that_says_what_to_fix(
    status: int, expected: int
) -> None:
    with _client_answering(status, {"code": "SOME_CODE", "message": "no", "data": None}) as client:
        with pytest.raises(CliError) as caught:
            client.get("/api/v1/agents")

    assert caught.value.exit_code == expected


def test_an_unreachable_endpoint_is_distinct_from_a_refused_request() -> None:
    """A caller retrying a wrong address forever is the failure this
    separation prevents: nothing about the request would change the outcome."""
    with _client_that_cannot_connect() as client:
        with pytest.raises(CliError) as caught:
            client.get("/api/v1/agents")

    assert caught.value.exit_code == EXIT_UNREACHABLE
    assert "deployment.test" in caught.value.message


def test_the_deployments_own_error_code_survives_untranslated() -> None:
    """The API's registered error codes are the vocabulary; a CLI-local
    rename would give a caller two names for one condition."""
    body = {"code": "AGENT_NOT_FOUND", "message": "agent not found", "data": None}
    with _client_answering(404, body) as client:
        with pytest.raises(CliError) as caught:
            client.get("/api/v1/agents/missing")

    assert caught.value.code == "AGENT_NOT_FOUND"
    assert "agent not found" in caught.value.message


def test_a_success_envelope_is_unwrapped_to_its_data() -> None:
    with _client_answering(200, {"code": "OK", "message": "ok", "data": [{"name": "a"}]}) as client:
        assert client.get("/api/v1/agents") == [{"name": "a"}]


def test_a_failure_code_inside_a_2xx_body_is_still_a_failure() -> None:
    """A handler that reports a problem inside a 200 envelope must not be read
    as success just because the status line was."""
    body = {"code": "INVALID_REQUEST", "message": "bad field", "data": None}
    with _client_answering(200, body) as client:
        with pytest.raises(CliError) as caught:
            client.get("/api/v1/agents")

    assert caught.value.code == "INVALID_REQUEST"


def test_json_output_stays_parseable_on_failure(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A caller parsing stdout must not have to handle 'sometimes JSON,
    sometimes a sentence'."""
    monkeypatch.setattr(
        "astrabox.cli.resources._client",
        lambda _args: _client_answering(
            403, {"code": "FORBIDDEN", "message": "admin role required", "data": None}
        ),
    )

    assert main(["get", "agents", "-o", "json"]) == EXIT_AUTH

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["code"] == "FORBIDDEN"


def test_table_output_puts_the_failure_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(
        "astrabox.cli.resources._client",
        lambda _args: _client_answering(
            500, {"code": "INTERNAL", "message": "boom", "data": None}
        ),
    )

    assert main(["get", "agents"]) == EXIT_FAILED

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "boom" in captured.err


def test_a_successful_list_exits_zero_and_prints_the_rows(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(
        "astrabox.cli.resources._client",
        lambda _args: _client_answering(
            200,
            {
                "code": "OK",
                "message": "ok",
                "data": [{"name": "researcher", "model": "claude-opus-5", "agent_id": "a1"}],
            },
        ),
    )

    assert main(["get", "agents", "-o", "json"]) == EXIT_OK

    assert json.loads(capsys.readouterr().out)[0]["name"] == "researcher"


def test_a_partial_client_credential_is_refused_not_downgraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Falling back to an unauthenticated request would surface as a confusing
    401 instead of naming the variable that is missing."""
    monkeypatch.setenv("ASTRABOX_CLIENT_ID", "cli")
    monkeypatch.setenv("ASTRABOX_TOKEN_URL", "https://idp.test/token")

    with pytest.raises(CliError) as caught:
        resolve_endpoint()

    assert caught.value.exit_code == EXIT_USAGE
    assert "ASTRABOX_CLIENT_SECRET" in caught.value.message


def test_no_credential_at_all_sends_no_authorization_header() -> None:
    """The default local identity mode expects exactly this; any other mode
    answers 401, which is the loud failure."""
    endpoint = resolve_endpoint()

    assert endpoint.token is None
    assert endpoint.headers() == {}


def test_the_token_flag_wins_over_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_TOKEN", "from-env")

    assert resolve_endpoint(token="from-flag").token == "from-flag"


def test_the_endpoint_flag_wins_over_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_ENDPOINT", "http://from-env:8000")

    assert resolve_endpoint(endpoint="http://from-flag:9000/").base_url == "http://from-flag:9000"


def test_the_default_endpoint_is_the_published_host_port() -> None:
    """containers/compose.yaml publishes 127.0.0.1:8088 -> container 8000, so a
    client on the host that defaulted to 8000 would reach nothing."""
    assert resolve_endpoint().base_url == "http://127.0.0.1:8088"


def test_moving_the_published_port_moves_the_default_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI and the Compose publish rule read one variable, so an operator
    who moves the port does not have to tell the CLI a second time."""
    monkeypatch.setenv("ASTRABOX_SERVER_HOST_PORT", "9099")

    assert resolve_endpoint().base_url == "http://127.0.0.1:9099"


def test_an_explicit_token_skips_the_client_credentials_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A complete client credential must not cause a token round-trip when the
    caller already supplied the token it would produce."""
    monkeypatch.setenv("ASTRABOX_CLIENT_ID", "cli")
    monkeypatch.setenv("ASTRABOX_CLIENT_SECRET", "secret")
    monkeypatch.setenv("ASTRABOX_TOKEN_URL", "https://idp.test/token")

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("no token exchange should be attempted")

    monkeypatch.setattr("httpx.post", refuse)

    assert resolve_endpoint(token="direct").token == "direct"


def test_an_empty_collection_still_renders_its_header() -> None:
    """An empty table and no output at all are different answers, and a caller
    reading stdout cannot tell them apart otherwise."""
    rendered = render_table([], ("name", "model"))

    assert rendered == "NAME  MODEL\n"


def test_a_table_cell_never_breaks_a_row_across_lines() -> None:
    rendered = render_table([{"name": "a\nb", "tags": ["x", "y"]}], ("name", "tags"))

    assert rendered.splitlines() == ["NAME  TAGS", "a b   x, y"]


def test_failure_details_are_printed_beside_the_message(capsys) -> None:
    emit_failure(
        CliError("ambiguous", details={"agent_ids": ["a", "b"]}), output="table"
    )

    assert "agent_ids: a, b" in capsys.readouterr().err
