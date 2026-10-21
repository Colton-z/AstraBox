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
from typing import Any

import pytest

from astrabox.deploy import onebox
from astrabox.identity.session_signing import reset_session_signing_cache
from astrabox.providers.litellm_shared_auth import (
    SANDBOX_INFERENCE_KEY_TYPE,
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
