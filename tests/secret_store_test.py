"""The credential secret store: no local master key under KMS, and KMS envelopes bound
to their record.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest

from astrabox.config.settings import get_settings
from astrabox.providers.secret_store import load_master_key
from astrabox.providers.secret_store_aws_kms import AwsKmsSecretStore


def test_non_local_store_never_generates_a_local_master_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ASTRABOX_SECRET_STORE", "aws_kms")
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ASTRABOX_VAULT_MASTER_KEY", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="does not expose"):
            load_master_key()
    finally:
        get_settings.cache_clear()
    assert not (tmp_path / "vault.key").exists()


class _FakeEncryptionClient:
    def __init__(self) -> None:
        self.context_by_ciphertext: dict[bytes, dict[str, str]] = {}

    def encrypt(
        self, *, source: bytes, keyring: object, encryption_context: dict[str, str]
    ) -> tuple[bytes, object]:
        assert keyring == "kms-keyring"
        ciphertext = b"encrypted:" + source
        self.context_by_ciphertext[ciphertext] = dict(encryption_context)
        return ciphertext, object()

    def decrypt(
        self, *, source: bytes, keyring: object, encryption_context: dict[str, str]
    ) -> tuple[bytes, object]:
        assert keyring == "kms-keyring"
        if self.context_by_ciphertext.get(source) != encryption_context:
            raise ValueError("encryption context mismatch")
        return source.removeprefix(b"encrypted:"), object()


def test_kms_ciphertext_crosses_replicas_and_binds_record_identity() -> None:
    async def scenario() -> None:
        client = _FakeEncryptionClient()
        writer = AwsKmsSecretStore()
        reader = AwsKmsSecretStore()
        writer._runtime = (client, "kms-keyring")
        reader._runtime = (client, "kms-keyring")

        sealed = await writer._seal_value(
            scope="vault/v1", key="credential/token", value="channel-secret"
        )
        assert "channel-secret" not in sealed
        assert (
            await reader._unseal_value(scope="vault/v1", key="credential/token", sealed=sealed)
            == "channel-secret"
        )

        ciphertext = base64.b64decode(sealed)
        context = client.context_by_ciphertext[ciphertext]
        assert context == {
            "astrabox-purpose": "astrabox-credential-store",
            "astrabox-scope": "vault/v1",
            "astrabox-key": "credential/token",
        }
        with pytest.raises(RuntimeError, match="decryption failed"):
            await reader._unseal_value(scope="vault/v2", key="credential/token", sealed=sealed)

    asyncio.run(scenario())


def test_kms_store_requires_explicit_platform_signing_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ASTRABOX_AWS_KMS_KEY_ARN",
        "arn:aws:kms:us-west-2:123456789012:key/test-key",
    )
    monkeypatch.delenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", raising=False)
    monkeypatch.delenv("ASTRABOX_VAULT_MASTER_KEY", raising=False)

    with pytest.raises(RuntimeError, match="ASTRABOX_TRANSCRIPT_SIGNING_KEY is required"):
        AwsKmsSecretStore().validate_configuration()
