"""A sandbox's model credential is a scoped LiteLLM key, not the admin key.

The gateway maps ``LITELLM_MASTER_KEY`` to ``PROXY_ADMIN``. The model binding
the egress sidecar writes admits ``/v1/*``, and the gateway's management
surface — ``/v1/mcp/server``, ``/v1/mcp/toolset``, ``/v1/mcp/network/*`` — lives
under that same prefix. A workload never has to read a credential to spend it:
the sidecar attaches whatever the binding names to whatever request the workload
makes. So the credential that binding names must not be an administrator's.
"""

from __future__ import annotations

import json
import os
import urllib.error
from typing import Any

import pytest

from astrabox.deploy import onebox
from astrabox.identity.session_signing import reset_session_signing_cache
from astrabox.providers.litellm_shared_auth import (
    SANDBOX_INFERENCE_KEY_ALIAS,
    SANDBOX_INFERENCE_KEY_TYPE,
    CAPABILITY_PREFIX,
    looks_like_access_token,
    sandbox_inference_key,
)
from astrabox.providers.model import LiteLLMModelEndpointProvider

_SECRET = "b" * 64


@pytest.fixture(autouse=True)
def _isolated_signing_cache() -> Any:
    """The signing secret is cached in a module global for the process.

    Under the full suite an earlier test primes that cache, so the credential
    the code derives stops matching the one a test computed from its own
    secret — and the file passes alone while failing in the suite.
    """

    reset_session_signing_cache()
    yield
    reset_session_signing_cache()


@pytest.fixture
def signing_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("ASTRABOX_AUTH_SESSION_SECRET", _SECRET)
    monkeypatch.delenv("ASTRABOX_LITELLM_API_KEY", raising=False)
    return _SECRET


def test_a_sandbox_carries_a_litellm_key_not_the_master_key(
    signing_secret: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    master = "sk-master-do-not-hand-out"
    monkeypatch.setenv("LITELLM_MASTER_KEY", master)

    credential = LiteLLMModelEndpointProvider._sandbox_inference_credential()

    assert credential, "a sandbox must be given some model credential"
    assert credential != master, "a sandbox must not carry the gateway's admin key"
    assert credential == sandbox_inference_key(signing_secret)


def test_every_replica_derives_the_same_key_and_a_rotation_changes_it() -> None:
    """Derivation is what lets replicas agree with nothing stored between them.

    LiteLLM returns a key's plaintext only at creation, so a replica that
    generated its own would hold a value no other replica could name.
    """

    assert sandbox_inference_key(_SECRET) == sandbox_inference_key(_SECRET)
    assert sandbox_inference_key(_SECRET) != sandbox_inference_key("c" * 64)


def test_an_operator_supplied_key_still_wins(
    signing_secret: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gateway with its own key system is still reachable.

    The escape hatch exists because not every deployment runs this adapter; it
    is explicit configuration, which is why it is not what an embedded
    deployment silently falls back to.
    """

    monkeypatch.setenv("ASTRABOX_LITELLM_API_KEY", "sk-operator-scoped")

    assert (
        LiteLLMModelEndpointProvider._sandbox_inference_credential()
        == "sk-operator-scoped"
    )


def test_the_embedded_deployment_no_longer_aliases_the_master_key() -> None:
    """onebox must not copy LITELLM_MASTER_KEY into the inference variable.

    Aliasing the two makes the embedded and external topologies disagree about
    one variable, and puts an administrator credential on every sandbox's model
    traffic.
    """

    source = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "astrabox", "deploy", "onebox.py"
    )
    with open(source, encoding="utf-8") as handle:
        body = handle.read()

    assert "os.environ[LITELLM_API_KEY_ENV_NAME] = key" not in body


def test_a_litellm_key_is_not_mistaken_for_an_access_token(signing_secret: str) -> None:
    """The adapter must decline a LiteLLM key rather than reject it.

    Under ``custom_auth_settings.mode: auto`` the adapter is asked first about
    every credential, including keys LiteLLM itself issued. Treating "not a
    capability" as "must be an access token" sends a virtual key to the OIDC
    provider, which rejects it, and the request ends before LiteLLM's own key
    authentication ever runs.
    """

    assert looks_like_access_token("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhIn0.signature")
    assert not looks_like_access_token(sandbox_inference_key(signing_secret))
    assert not looks_like_access_token("sk-anything-litellm-issued")
    assert not looks_like_access_token(CAPABILITY_PREFIX + "a.b.c")
    assert not looks_like_access_token("")


class _Response:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body.encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _gateway(
    monkeypatch: pytest.MonkeyPatch, responses: list[tuple[int, str]]
) -> list[dict[str, Any]]:
    """Answer the proxy's key endpoints in order, recording what was asked."""

    import urllib.request

    seen: list[dict[str, Any]] = []
    queue = list(responses)

    def _urlopen(request: Any, timeout: float = 0) -> _Response:
        payload = json.loads(request.data.decode("utf-8")) if request.data else None
        seen.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "json": payload,
                "authorization": request.headers.get("Authorization", ""),
            }
        )
        status, body = queue.pop(0)
        if status >= 400:
            raise urllib.error.HTTPError(
                request.full_url, status, body, {}, None  # type: ignore[arg-type]
            )
        return _Response(status, body)

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    return seen


@pytest.fixture
def provisioning(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("ASTRABOX_AUTH_SESSION_SECRET", _SECRET)
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-master-do-not-hand-out")
    return sandbox_inference_key(_SECRET)


def test_the_key_is_created_with_litellm_s_inference_only_type(
    provisioning: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``llm_api`` is LiteLLM's own "may infer, may not manage" key type.

    Asserting the type rather than a route list: LiteLLM resolves it to the
    ``llm_api_routes`` preset it maintains, so an inference route LiteLLM adds
    is covered without a change here — and the management surface stays out
    even though it sits under the same ``/v1`` prefix.
    """

    seen = _gateway(monkeypatch, [(404, "not found"), (200, "{}"), (200, "{}")])

    assert onebox.ensure_sandbox_inference_key() == provisioning

    assert seen[1]["url"].endswith("/key/generate")
    assert seen[1]["json"]["key"] == provisioning, (
        "the value must be supplied, or LiteLLM returns one nothing else can name"
    )
    assert seen[1]["json"]["key_type"] == SANDBOX_INFERENCE_KEY_TYPE
    assert seen[1]["json"]["key_alias"] == SANDBOX_INFERENCE_KEY_ALIAS


def test_external_gateway_provisioning_uses_the_server_side_origin(
    provisioning: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The external topology must not keep dialing embedded loopback."""

    monkeypatch.setenv(
        "ASTRABOX_LITELLM_SERVER_BASE_URL",
        "https://gateway.example.test/root/",
    )
    seen = _gateway(monkeypatch, [(404, "not found"), (200, "{}"), (200, "{}")])

    onebox.ensure_sandbox_inference_key()

    assert all(
        entry["url"].startswith("https://gateway.example.test/root/")
        for entry in seen
    )


def test_the_key_is_spent_once_before_the_deployment_serves(
    provisioning: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Creating a key does not prove a request carrying it is accepted.

    The adapter declines credentials that are not AstraBox's, and LiteLLM only
    falls back to its own key authentication because ``custom_auth_settings.mode``
    is ``auto``. Lose that and key creation still succeeds while every sandbox's
    first model call fails, so the check has to be a real request with the real
    credential — carrying the sandbox key, not the master key that made it.
    """

    seen = _gateway(monkeypatch, [(404, "no"), (200, "{}"), (200, "{}")])

    onebox.ensure_sandbox_inference_key()

    spend = seen[-1]
    assert spend["url"].endswith("/v1/models")
    assert spend["authorization"] == f"Bearer {provisioning}"
    assert "sk-master-do-not-hand-out" not in spend["authorization"]


def test_an_existing_key_is_not_created_again(
    provisioning: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every restart runs this. Re-creating would fail on the second boot."""

    seen = _gateway(monkeypatch, [(200, "{}"), (200, "{}")])

    assert onebox.ensure_sandbox_inference_key() == provisioning
    assert not any(entry["url"].endswith("/key/generate") for entry in seen)


def test_an_existing_key_the_gateway_rejects_stops_the_deployment(
    provisioning: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This guards the restart path, where the key already exists.

    A deployment whose key table is already populated and whose fallback setting
    is absent passes every creation check. Verifying only when the key is
    created never looks at such a deployment again.
    """

    _gateway(monkeypatch, [(200, "{}"), (401, "Invalid proxy server token")])

    with pytest.raises(RuntimeError, match="does not accept the key"):
        onebox.ensure_sandbox_inference_key()


def test_a_key_that_could_not_be_created_stops_the_deployment(
    provisioning: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Serving without it means every sandbox 401s on its first model call.

    Coming up anyway turns one loud startup failure into a turn failure per
    conversation, reported as the model being unavailable.
    """

    _gateway(monkeypatch, [(404, "no"), (500, "boom"), (404, "still no")])

    with pytest.raises(RuntimeError, match="could not provision"):
        onebox.ensure_sandbox_inference_key()


def test_a_key_another_replica_created_concurrently_is_accepted(
    provisioning: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two replicas booting together both try; the loser must not crash."""

    _gateway(
        monkeypatch, [(404, "no"), (400, "already exists"), (200, "{}"), (200, "{}")]
    )

    assert onebox.ensure_sandbox_inference_key() == provisioning
