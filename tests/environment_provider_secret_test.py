"""The environment model api_key is a write-only secret.

`provider_access.api_key` is a plaintext credential. It must NEVER be shipped to
the client — a UI mask over a leaked key protects nothing. So the config service
redacts it to a sentinel on every client-facing read, and on write treats the
sentinel as "unchanged, keep the stored key". Resolution keeps the raw key
(`get_environment`), so the sandbox still gets the real value.
"""

from __future__ import annotations

from typing import Any

import pytest

import astrabox.core.service.orchestrator.engine.claude_code  # noqa: F401 — registers the claude_code engine (engine_kind enum validation)
import astrabox.providers.open_sandbox.sandbox  # noqa: F401 — registers the sandbox provider (Environment capability validation)
from astrabox.core.service.orchestrator.agent_config_service import (
    AgentConfigService,
    ENV_API_KEY_SENTINEL,
)
from astrabox.common.utils.user_context import UserContext


class _FakeEnvRepo:
    def __init__(self, docs: dict[str, dict[str, Any]]) -> None:
        self.docs = docs
        self.upserts: list[tuple[str, dict[str, Any]]] = []

    async def list_all(self) -> list[dict[str, Any]]:
        return list(self.docs.values())

    async def get_any_by_name(self, name: str) -> dict[str, Any] | None:
        return self.docs.get(name)

    async def upsert(self, name: str, doc: dict[str, Any]) -> dict[str, Any]:
        self.upserts.append((name, dict(doc)))
        self.docs[name] = {**self.docs.get(name, {}), **doc}
        return self.docs[name]


def _svc(env_repo: _FakeEnvRepo) -> AgentConfigService:
    return AgentConfigService(
        agent_repo=object(),  # type: ignore[arg-type]  # unused on the env path
        environment_repo=env_repo,  # type: ignore[arg-type]
        assistant_repo=object(),  # type: ignore[arg-type]  # unused
    )


_USER = UserContext(user_id="admin")


async def test_list_redacts_a_set_api_key_and_leaves_an_unset_one() -> None:
    env_repo = _FakeEnvRepo({
        "with-key": {"name": "with-key", "provider_access": {"base_url": "http://x", "api_key": "sk-REAL"}},
        "no-key": {"name": "no-key", "provider_access": {"base_url": "http://y"}},
        "no-provider": {"name": "no-provider"},
    })
    out = {d["name"]: d for d in await _svc(env_repo).list_environment_configs(_USER)}
    # The real key is never in the client-facing payload.
    assert out["with-key"]["provider_access"]["api_key"] == ENV_API_KEY_SENTINEL
    assert "sk-REAL" not in str(out)
    # An unset key stays unset; base_url is untouched.
    assert out["with-key"]["provider_access"]["base_url"] == "http://x"
    assert out["no-key"]["provider_access"].get("api_key") in (None, "")
    assert out["no-provider"].get("provider_access") is None


async def test_write_with_the_mask_untouched_preserves_the_stored_key() -> None:
    env_repo = _FakeEnvRepo({
        "e": {"name": "e", "provider_access": {"base_url": "http://x", "api_key": "sk-REAL"}},
    })
    # Client loaded the redacted env and changed only base_url, sending the mask back.
    await _svc(env_repo).upsert_environment_config(
        _USER, "e", {"engine_kind": "claude_code", "provider_access": {"base_url": "http://new", "api_key": ENV_API_KEY_SENTINEL}}
    )
    written = env_repo.upserts[-1][1]
    assert written["provider_access"]["api_key"] == "sk-REAL"  # preserved, not the mask
    assert written["provider_access"]["base_url"] == "http://new"


async def test_write_with_a_new_key_stores_it_and_the_return_is_redacted() -> None:
    env_repo = _FakeEnvRepo({"e": {"name": "e", "provider_access": {"api_key": "sk-OLD"}}})
    result = await _svc(env_repo).upsert_environment_config(
        _USER, "e", {"engine_kind": "claude_code", "provider_access": {"api_key": "sk-NEW"}}
    )
    # Stored: the new plaintext key.
    assert env_repo.docs["e"]["provider_access"]["api_key"] == "sk-NEW"
    # Returned to the client: redacted, never the plaintext.
    assert result["provider_access"]["api_key"] == ENV_API_KEY_SENTINEL
    assert "sk-NEW" not in str(result)


async def test_resolution_path_keeps_the_raw_key() -> None:
    # get_environment is the internal/resolution read — it must return the REAL key.
    env_repo = _FakeEnvRepo({"e": {"name": "e", "provider_access": {"api_key": "sk-REAL"}}})
    env = await _svc(env_repo).get_environment("e")
    assert env is not None and env["provider_access"]["api_key"] == "sk-REAL"


# ── the collector credential is the same kind of secret ─────────────────────
#
# `tracing.auth_token` reaches an OTLP collector rather than a model gateway, and
# is as reusable as the model key. It was returned in the clear by every admin
# read, including the response to the PUT that stored it, until it joined
# `_ENV_SECRET_FIELDS`. These cases exist because masking one half is worse than
# masking neither: a form that loads the mask and saves an unrelated field would
# write the mask over the credential.

_TRACING = {
    "enabled": True,
    "endpoint": "https://collector.example.com/api/public/otel",
    "signals": ["traces", "metrics"],
}


async def test_list_redacts_the_tracing_credential_too() -> None:
    env_repo = _FakeEnvRepo({
        "e": {"name": "e", "tracing": {**_TRACING, "auth_token": "Basic REAL"}},
        "no-token": {"name": "no-token", "tracing": dict(_TRACING)},
    })
    out = {d["name"]: d for d in await _svc(env_repo).list_environment_configs(_USER)}
    assert out["e"]["tracing"]["auth_token"] == ENV_API_KEY_SENTINEL
    assert "Basic REAL" not in str(out)
    # The rest of the block is untouched, and an unset token stays unset.
    assert out["e"]["tracing"]["endpoint"] == _TRACING["endpoint"]
    assert out["no-token"]["tracing"].get("auth_token") in (None, "")


async def test_write_with_the_tracing_mask_untouched_preserves_the_stored_token() -> None:
    env_repo = _FakeEnvRepo({
        "e": {"name": "e", "tracing": {**_TRACING, "auth_token": "Basic REAL"}},
    })
    # The client loaded the redacted environment and named a signal, sending the
    # mask back with everything else.
    await _svc(env_repo).upsert_environment_config(
        _USER,
        "e",
        {
            "engine_kind": "claude_code",
            "tracing": {
                **_TRACING,
                "signals": ["traces"],
                "auth_token": ENV_API_KEY_SENTINEL,
            },
        },
    )
    written = env_repo.upserts[-1][1]
    assert written["tracing"]["auth_token"] == "Basic REAL"  # restored, not the mask
    assert written["tracing"]["signals"] == ["traces"]


async def test_both_secrets_are_redacted_and_preserved_in_one_pass() -> None:
    """One declaration, so a document carrying both cannot have one half leak."""

    env_repo = _FakeEnvRepo({
        "e": {
            "name": "e",
            "provider_access": {"base_url": "http://gw", "api_key": "sk-REAL"},
            "tracing": {**_TRACING, "auth_token": "Basic REAL"},
        },
    })
    svc = _svc(env_repo)
    read = (await svc.list_environment_configs(_USER))[0]
    assert read["provider_access"]["api_key"] == ENV_API_KEY_SENTINEL
    assert read["tracing"]["auth_token"] == ENV_API_KEY_SENTINEL

    await svc.upsert_environment_config(
        _USER,
        "e",
        {
            "engine_kind": "claude_code",
            "provider_access": read["provider_access"],
            "tracing": read["tracing"],
        },
    )
    written = env_repo.upserts[-1][1]
    assert written["provider_access"]["api_key"] == "sk-REAL"
    assert written["tracing"]["auth_token"] == "Basic REAL"
