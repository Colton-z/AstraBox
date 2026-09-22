from __future__ import annotations

import asyncio
import builtins
import importlib.util
import re
import shutil
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import yaml


class _UserAPIKeyAuth:
    def __init__(self, **values: Any) -> None:
        self.values = values


class _ProxyException(Exception):
    def __init__(self, **values: Any) -> None:
        super().__init__(str(values.get("message") or "proxy error"))


class _Roles:
    INTERNAL_USER_VIEW_ONLY = "internal_user_view_only"
    PROXY_ADMIN = "proxy_admin"


def _load_adapter(tmp_path: Path, monkeypatch: Any, *, identity: str = "local") -> Any:
    """Load the adapter the way LiteLLM does: by path, with no astrabox package.

    LiteLLM reads the hook out of the config directory with
    ``spec_from_file_location``, which does not extend ``sys.path``, and its venv
    has no astrabox package at all. Both are reproduced here so a test cannot
    pass on an import the deployed adapter would not have.
    """

    root = Path(__file__).parents[1]
    sources = {
        "custom_auth.py": root / "containers/litellm/custom_auth.py",
        "astrabox_identity_session.py": root / "astrabox/identity/session_signing.py",
        "astrabox_litellm_auth.py": root / "astrabox/providers/litellm_shared_auth.py",
        "astrabox_oidc.py": root / "astrabox/identity/oidc.py",
    }
    for destination, source in sources.items():
        shutil.copy2(source, tmp_path / destination)

    proxy_types = ModuleType("litellm.proxy._types")
    proxy_types.LitellmUserRoles = _Roles
    proxy_types.ProxyException = _ProxyException
    proxy_types.UserAPIKeyAuth = _UserAPIKeyAuth
    proxy = ModuleType("litellm.proxy")
    proxy.__path__ = []  # type: ignore[attr-defined]
    litellm = ModuleType("litellm")
    litellm.__path__ = []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", litellm)
    monkeypatch.setitem(sys.modules, "litellm.proxy", proxy)
    monkeypatch.setitem(sys.modules, "litellm.proxy._types", proxy_types)
    for name in (
        "astrabox_identity_session",
        "astrabox_litellm_auth",
        "astrabox_oidc",
    ):
        monkeypatch.delitem(sys.modules, name, raising=False)

    monkeypatch.setenv("ASTRABOX_WEB_IDENTITY", identity)
    if identity == "oidc":
        monkeypatch.setenv("ASTRABOX_OIDC_ISSUER", "https://issuer.invalid")
        monkeypatch.setenv("ASTRABOX_OIDC_CLIENT_ID", "loader-test-client")
    monkeypatch.setenv("ASTRABOX_AUTH_SESSION_SECRET", "loader-test-secret")
    monkeypatch.setattr(sys, "path", [entry for entry in sys.path if entry != str(tmp_path)])
    real_import = builtins.__import__

    def isolated_import(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "astrabox" or name.startswith("astrabox."):
            raise ModuleNotFoundError(
                "the LiteLLM adapter distribution has no astrabox package"
            )
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", isolated_import)

    module_path = tmp_path / "custom_auth.py"
    spec = importlib.util.spec_from_file_location("custom_auth_loader_probe", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_custom_auth_loads_from_litellm_config_directory(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Mirror LiteLLM's spec loader, which does not extend ``sys.path``."""

    module = _load_adapter(tmp_path, monkeypatch)

    assert callable(module.user_api_key_auth)
    assert sys.path[0] == str(tmp_path)


@pytest.mark.parametrize("config_path", [
    "containers/litellm/config.yaml",
])
def test_the_proxy_config_asks_litellm_to_keep_its_own_authentication(config_path: str) -> None:
    """A custom_auth hook is the proxy's ONLY authenticator unless told otherwise.

    Without this setting LiteLLM never runs its own key authentication, so
    virtual keys, their budgets, rate limits and route scoping are all
    unreachable — an amputation of the gateway, when the intent was only to
    reuse AstraBox's OIDC identity.
    """

    root = Path(__file__).parents[1]
    config = yaml.safe_load((root / config_path).read_text())

    general = config["general_settings"]
    assert general["custom_auth"] == "custom_auth.user_api_key_auth"
    assert general["custom_auth_settings"]["mode"] == "auto"


def test_a_credential_that_is_not_ours_is_declined_not_rejected(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """LiteLLM falls back on any exception EXCEPT ``ProxyException``.

    ``enterprise_custom_auth`` re-raises ``ProxyException`` and swallows every
    other type to try its own key authentication. So the exception type is the
    whole mechanism: raising ``ProxyException`` for a key LiteLLM issued ends
    the request before LiteLLM is ever asked about it.
    """

    module = _load_adapter(tmp_path, monkeypatch)
    request = SimpleNamespace(url=SimpleNamespace(path="/v1/chat/completions"))

    for credential in ("sk-a-litellm-virtual-key", "Bearer sk-another", ""):
        with pytest.raises(Exception) as caught:  # noqa: PT011 - the type IS the assertion
            asyncio.run(module.user_api_key_auth(request, credential))
        assert not isinstance(caught.value, _ProxyException), (
            f"{credential!r} is not an AstraBox credential; declining it as a "
            "ProxyException stops LiteLLM from checking its own keys"
        )


class _ProviderConsulted(Exception):
    """Raised by the stubbed OIDC provider to record that it was reached."""


def _refuse_to_be_consulted(tmp_path: Path, monkeypatch: Any) -> Any:
    """Load the adapter in OIDC mode with a provider that reports being called.

    A gateway with no OIDC provider configured declines every unrecognised
    credential anyway, so the shape check only does work when OIDC is on. This
    is where a LiteLLM key would otherwise be posted to the identity provider.
    """

    module = _load_adapter(tmp_path, monkeypatch, identity="oidc")

    async def _consulted(*_: Any, **__: Any) -> Any:
        raise _ProviderConsulted("the OIDC provider was asked about this credential")

    monkeypatch.setattr(module, "principal_from_access_token", _consulted)
    return module


def test_a_litellm_key_never_reaches_the_identity_provider(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Shape decides, so a key LiteLLM issued is not posted to Casdoor.

    Without the shape check the provider rejects it and the adapter turns that
    into ``ProxyException`` — a hard 401 for a key the gateway itself issued,
    one round-trip to the identity provider per request.
    """

    module = _refuse_to_be_consulted(tmp_path, monkeypatch)
    request = SimpleNamespace(url=SimpleNamespace(path="/v1/chat/completions"))

    with pytest.raises(Exception) as caught:  # noqa: PT011 - the type IS the assertion
        asyncio.run(module.user_api_key_auth(request, "sk-a-litellm-virtual-key"))

    assert not isinstance(caught.value, _ProviderConsulted)
    assert not isinstance(caught.value, _ProxyException)


def test_an_access_token_still_reaches_the_identity_provider(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """The control for the test above: the shape check must not shut OIDC off.

    Without this, declining everything would pass that test and break every
    console login.
    """

    module = _refuse_to_be_consulted(tmp_path, monkeypatch)
    request = SimpleNamespace(url=SimpleNamespace(path="/v1/chat/completions"))

    with pytest.raises(_ProviderConsulted):
        asyncio.run(
            module.user_api_key_auth(
                request, "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhIn0.signature"
            )
        )


def test_an_astrabox_credential_that_fails_its_checks_is_rejected(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """A bad capability must not get a second hearing as a LiteLLM key."""

    module = _load_adapter(tmp_path, monkeypatch)
    request = SimpleNamespace(url=SimpleNamespace(path="/v1/chat/completions"))

    forged = "astrabox-litellm-cap-v1.not-a-real-jwt"
    with pytest.raises(_ProxyException):
        asyncio.run(module.user_api_key_auth(request, forged))


def test_bundled_litellm_mcp_server_names_use_supported_characters() -> None:
    root = Path(__file__).parents[1]
    config = yaml.safe_load((root / "containers/litellm/config.yaml").read_text())

    names = list(config["mcp_servers"])
    assert names == ["astrabox_agents", "bright_data"]
    assert all(re.fullmatch(r"[A-Za-z0-9_]+", name) for name in names)
    managed = config["mcp_servers"]["bright_data"]
    assert managed["url"] == "https://mcp.brightdata.com/mcp"
    # The gateway consumes x-mcp-bright_data-authorization itself; a verbatim
    # extra_headers forwarding entry would leak that client header upstream.
    assert "extra_headers" not in managed
    assert "static_headers" not in managed
    assert "credentials" not in managed
