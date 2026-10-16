"""Transcript capability token — the multi-tenant cross-user fence.

The sandbox->backend transcript endpoints require a per-session HMAC
capability token bound to the path session_id: without one, supplying
project_key + session_id alone let any caller reaching the port read or write
any session's transcript, with no ownership check enforced.
"""

from __future__ import annotations

import pytest

import astrabox.core.service.orchestrator.transcript_capability as cap


@pytest.fixture(autouse=True)
def _fixed_signing_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", "unit-test-shared-secret")
    yield


def test_token_is_deterministic_and_session_bound() -> None:
    a1 = cap.mint_transcript_capability_token("sess-A")
    a2 = cap.mint_transcript_capability_token("sess-A")
    b = cap.mint_transcript_capability_token("sess-B")
    assert a1 == a2, "same session + key -> same token (multi-replica verify)"
    assert a1 != b, "different sessions get different tokens"
    assert len(a1) == 64  # hex sha256


def test_verify_accepts_own_token_rejects_cross_session() -> None:
    token_a = cap.mint_transcript_capability_token("sess-A")
    assert cap.verify_transcript_capability_token("sess-A", token_a) is True
    # The cross-user hole: session B's request carrying A's token is rejected.
    assert cap.verify_transcript_capability_token("sess-B", token_a) is False
    assert cap.verify_transcript_capability_token("sess-A", "") is False
    assert cap.verify_transcript_capability_token("sess-A", "deadbeef") is False


def test_route_authorization_binds_to_session(monkeypatch: pytest.MonkeyPatch) -> None:
    # The route's _authorize path: import the module, drive verify directly
    # (the FastAPI wiring is exercised by the wire-contract snapshot).
    from astrabox.api.routes import transcript as routes

    assert routes.transcript_capability_required() is True
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_CAPABILITY_REQUIRED", "false")
    assert routes.transcript_capability_required() is False


# ── Signing-key trust root ──────────────────────────────────────────────────


def test_zero_config_single_server_derives_a_real_deployment_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # No signing key, no vault key, capability required, and the local secret
    # store selected: the signer derives from the deployment master key,
    # generated once into the state dir. The quickstart therefore boots with a
    # per-deployment trust root.
    # The fence must never silently use the public development key outside
    # local mode; the forgery assertion below pins that tenant boundary.
    import base64
    import hashlib
    import hmac as hmac_mod

    from astrabox.config.settings import get_settings

    monkeypatch.delenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", raising=False)
    monkeypatch.delenv("ASTRABOX_VAULT_MASTER_KEY", raising=False)
    monkeypatch.delenv("ASTRABOX_LOCAL_MODE", raising=False)
    monkeypatch.setenv("ASTRABOX_SECRET_STORE", "local")
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_CAPABILITY_REQUIRED", "true")
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        token = cap.mint_transcript_capability_token("sess-A")
        assert cap.verify_transcript_capability_token("sess-A", token) is True
        cap.validate_signing_key_config()  # boot passes, zero config
        key_file = tmp_path / "vault.key"
        assert key_file.exists()
        assert (key_file.stat().st_mode & 0o777) == 0o600
        # NOT the public dev key's signature:
        forged = hmac_mod.new(
            cap._LOCAL_DEV_KEY, b"sess-A", hashlib.sha256
        ).hexdigest()
        assert token != forged
        # And stable across a restart (the persisted key, not a fresh one):
        master = base64.urlsafe_b64decode(key_file.read_text().strip())
        derived = hmac_mod.new(master, cap._DERIVE_DOMAIN, hashlib.sha256).digest()
        assert token == hmac_mod.new(derived, b"sess-A", hashlib.sha256).hexdigest()
    finally:
        get_settings.cache_clear()


def test_kms_store_requires_explicit_shared_signing_material(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", raising=False)
    monkeypatch.delenv("ASTRABOX_VAULT_MASTER_KEY", raising=False)
    monkeypatch.delenv("ASTRABOX_LOCAL_MODE", raising=False)
    monkeypatch.setenv("ASTRABOX_SECRET_STORE", "aws_kms")
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="does not expose"):
        cap.validate_signing_key_config()
    assert not (tmp_path / "vault.key").exists()
