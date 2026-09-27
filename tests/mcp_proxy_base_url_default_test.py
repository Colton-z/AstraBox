"""The sandbox callback base is configured, never derived from this server.

A sandbox may reach the callback host, so a base derived from the server
container's own address made every port the server listens on — the
unauthenticated API among them — an allowed egress target. The Compose stack
sets the base to the sandbox edge; every other shape sets it explicitly. An
unset base fails the first callback URL instead of being guessed.
"""

from __future__ import annotations

import os

import pytest

import astrabox.common.utils.settings as settings
from astrabox.common.utils.errors import APIError

_CONTAINER_MARKERS = {"/.dockerenv", "/run/.containerenv"}


@pytest.fixture
def inside_a_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make this process look like it runs in a container, as the server does."""

    real_exists = os.path.exists
    monkeypatch.setattr(
        os.path,
        "exists",
        lambda path: str(path) in _CONTAINER_MARKERS or real_exists(path),
    )


def _mcp_proxy_base_url(monkeypatch: pytest.MonkeyPatch) -> str:
    # Isolate from any on-disk config: every config lookup returns its default.
    monkeypatch.setattr(settings, "_safe_get", lambda key, default: default)
    return settings.load_astrabox_settings().mcp_proxy_base_url


def test_an_unset_base_is_not_derived_inside_a_container(
    monkeypatch: pytest.MonkeyPatch, inside_a_container: None
) -> None:
    monkeypatch.delenv("ASTRABOX_MCP_PROXY_BASE_URL", raising=False)
    monkeypatch.setenv("ASTRABOX_PORT", "8000")
    assert _mcp_proxy_base_url(monkeypatch) == ""


def test_an_explicit_base_is_used_as_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_MCP_PROXY_BASE_URL", "http://172.17.0.23:8000")
    assert _mcp_proxy_base_url(monkeypatch) == "http://172.17.0.23:8000"


def test_an_unset_base_fails_the_callback_url_naming_the_setting(
    monkeypatch: pytest.MonkeyPatch, inside_a_container: None
) -> None:
    from astrabox.core.service.orchestrator import sandbox_lifecycle as sl

    monkeypatch.delenv("ASTRABOX_MCP_PROXY_BASE_URL", raising=False)
    monkeypatch.setattr(settings, "_safe_get", lambda key, default: default)
    with pytest.raises(APIError) as caught:
        sl.build_sandbox_callback_url(
            subject_type="session", subject_id="s1", generation="g1", token="t1"
        )
    assert "ASTRABOX_MCP_PROXY_BASE_URL" in str(caught.value.message)
