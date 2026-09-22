"""An unset ``ASTRABOX_MCP_PROXY_BASE_URL`` must not 500 the first session.

The README quickstart runs the server in a container that publishes its port to
the HOST loopback only and spawns sandbox containers on the same default bridge.
The lifecycle worker builds a sandbox-callback URL pre-provision and fails loud
(500) on an empty base — so with the var unset the very first session-create
500s. Fix: when unset, derive the server container's OWN bridge address
(self-detected container IP + internal ASTRABOX_PORT), which is the address a
sibling sandbox actually reaches. Derived ONLY inside a container; on the host
"" is preserved (operator sets it explicitly, as dev.sh / the e2e scripts do).
"""

from __future__ import annotations

import pytest

import astrabox.common.utils.settings as settings


# ── the derivation ───────────────────────────────────────────────────────────

def test_derives_bridge_url_inside_container(monkeypatch) -> None:
    monkeypatch.setattr(settings, "_running_in_container", lambda: True)
    monkeypatch.setattr(settings, "_detect_own_container_ip", lambda: "172.17.0.7")
    monkeypatch.setenv("ASTRABOX_PORT", "8000")
    assert settings._default_mcp_proxy_base_url() == "http://172.17.0.7:8000"


def test_derivation_follows_astrabox_port_override(monkeypatch) -> None:
    monkeypatch.setattr(settings, "_running_in_container", lambda: True)
    monkeypatch.setattr(settings, "_detect_own_container_ip", lambda: "172.17.0.7")
    monkeypatch.setenv("ASTRABOX_PORT", "9000")
    assert settings._default_mcp_proxy_base_url() == "http://172.17.0.7:9000"


def test_no_default_on_the_host(monkeypatch) -> None:
    # Not in a container -> preserve the existing fail-loud (empty base).
    monkeypatch.setattr(settings, "_running_in_container", lambda: False)
    monkeypatch.setattr(settings, "_detect_own_container_ip", lambda: "172.17.0.7")
    assert settings._default_mcp_proxy_base_url() == ""


def test_no_default_when_ip_undetectable(monkeypatch) -> None:
    # In a container but the bridge IP can't be resolved -> fail loud, not a
    # broken guess.
    monkeypatch.setattr(settings, "_running_in_container", lambda: True)
    monkeypatch.setattr(settings, "_detect_own_container_ip", lambda: "")
    monkeypatch.setenv("ASTRABOX_PORT", "8000")
    assert settings._default_mcp_proxy_base_url() == ""


# ── the self-IP detector rejects loopback (never a reachable peer address) ───

def test_detect_ip_rejects_loopback(monkeypatch) -> None:
    class _Sock:
        def connect(self, addr):  # noqa: ANN001
            pass

        def getsockname(self):
            return ("127.0.0.1", 12345)

        def close(self):
            pass

    monkeypatch.setattr(settings.socket, "socket", lambda *a, **k: _Sock())
    assert settings._detect_own_container_ip() == ""


def test_detect_ip_returns_bridge_ip(monkeypatch) -> None:
    class _Sock:
        def connect(self, addr):  # noqa: ANN001
            pass

        def getsockname(self):
            return ("172.17.0.4", 12345)

        def close(self):
            pass

    monkeypatch.setattr(settings.socket, "socket", lambda *a, **k: _Sock())
    assert settings._detect_own_container_ip() == "172.17.0.4"


# ── end-to-end through load_astrabox_settings() ──────────────────────────────

def test_settings_uses_explicit_env_over_derivation(monkeypatch) -> None:
    monkeypatch.setenv("ASTRABOX_MCP_PROXY_BASE_URL", "http://explicit:1234")
    # Even inside a container, an explicit value wins (no derivation).
    monkeypatch.setattr(settings, "_running_in_container", lambda: True)
    monkeypatch.setattr(settings, "_detect_own_container_ip", lambda: "172.17.0.7")
    assert load_mcp_proxy_base_url(monkeypatch) == "http://explicit:1234"


def test_settings_derives_when_unset_in_container(monkeypatch) -> None:
    monkeypatch.delenv("ASTRABOX_MCP_PROXY_BASE_URL", raising=False)
    monkeypatch.setattr(settings, "_running_in_container", lambda: True)
    monkeypatch.setattr(settings, "_detect_own_container_ip", lambda: "172.17.0.7")
    monkeypatch.setenv("ASTRABOX_PORT", "8000")
    assert load_mcp_proxy_base_url(monkeypatch) == "http://172.17.0.7:8000"


def load_mcp_proxy_base_url(monkeypatch) -> str:
    # Isolate from any on-disk config: every config lookup returns its literal
    # default (safe for the str + int fields load_astrabox_settings builds).
    monkeypatch.setattr(settings, "_safe_get", lambda key, default: default)
    return settings.load_astrabox_settings().mcp_proxy_base_url


# ── session-create's callback build with the var unset ──────────────────────

def test_callback_url_builds_in_container_without_the_var(monkeypatch) -> None:
    # This is the exact function that 500'd the README quickstart's first session.
    from astrabox.core.service.orchestrator import sandbox_lifecycle as sl

    monkeypatch.delenv("ASTRABOX_MCP_PROXY_BASE_URL", raising=False)
    monkeypatch.setenv("ASTRABOX_PORT", "8000")
    monkeypatch.setattr(settings, "_running_in_container", lambda: True)
    monkeypatch.setattr(settings, "_detect_own_container_ip", lambda: "172.17.0.2")

    url = sl.build_sandbox_callback_url(
        subject_type="session", subject_id="s1", generation="g1", token="t1"
    )
    # Points at the server container's OWN bridge address:port (the address a
    # sibling sandbox reaches) — verified reachable (HTTP 200) in the topology test.
    assert url == "http://172.17.0.2:8000/api/v1/sandbox-callback/session/s1/g1/t1"


def test_callback_url_still_fails_loud_on_the_host(monkeypatch) -> None:
    from astrabox.common.utils.errors import APIError
    from astrabox.core.service.orchestrator import sandbox_lifecycle as sl

    monkeypatch.delenv("ASTRABOX_MCP_PROXY_BASE_URL", raising=False)
    monkeypatch.setattr(settings, "_running_in_container", lambda: False)
    with pytest.raises(APIError):
        sl.build_sandbox_callback_url(
            subject_type="session", subject_id="s1", generation="g1", token="t1"
        )
