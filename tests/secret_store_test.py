"""Credential secret-store selection, key publication, and KMS envelope tests."""

from __future__ import annotations

import asyncio
import base64
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

import astrabox.providers.secret_store as local_store_module
import astrabox.providers.secret_store_aws_kms as aws_store_module
from astrabox.bootstrap import BootstrapConfigError, _assert_secret_store_is_configured
from astrabox.config.settings import get_settings
from astrabox.providers.secret_store import load_master_key
from astrabox.providers.secret_store_aws_kms import AwsKmsSecretStore
from astrabox.seams.secrets import secret_store_for_name


def test_default_and_configured_provider_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASTRABOX_SECRET_STORE", raising=False)
    assert secret_store_for_name(None).name == "local"

    monkeypatch.setenv("ASTRABOX_SECRET_STORE", "aws_kms")
    assert secret_store_for_name(None).name == "aws_kms"
    assert secret_store_for_name("local").name == "local"


def test_unknown_provider_fails_with_registered_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_SECRET_STORE", "typo")
    with pytest.raises(RuntimeError) as caught:
        secret_store_for_name(None)
    message = str(caught.value)
    assert "typo" in message
    assert "aws_kms" in message
    assert "local" in message


def test_local_key_publication_is_complete_under_concurrent_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Concurrent starters must converge on one complete key, never an empty file."""
    worker_count = 8
    barrier = threading.Barrier(worker_count)
    counter_lock = threading.Lock()
    counter = 0

    def competing_key(_size: int) -> bytes:
        nonlocal counter
        with counter_lock:
            counter += 1
            marker = counter
        barrier.wait(timeout=10)
        return bytes([marker]) * 32

    monkeypatch.setenv("ASTRABOX_SECRET_STORE", "local")
    monkeypatch.delenv("ASTRABOX_VAULT_MASTER_KEY", raising=False)
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(local_store_module._secrets, "token_bytes", competing_key)
    get_settings.cache_clear()
    try:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            keys = list(executor.map(lambda _index: load_master_key(), range(worker_count)))
    finally:
        get_settings.cache_clear()

    assert len(set(keys)) == 1
    key_path = tmp_path / "vault.key"
    assert base64.urlsafe_b64decode(key_path.read_bytes()) == keys[0]
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert list(tmp_path.glob(".vault.key.*")) == []


def test_corrupt_local_key_file_fails_loud(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ASTRABOX_SECRET_STORE", "local")
    monkeypatch.delenv("ASTRABOX_VAULT_MASTER_KEY", raising=False)
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    (tmp_path / "vault.key").write_text("not base64!", encoding="ascii")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="urlsafe base64"):
            load_master_key()
    finally:
        get_settings.cache_clear()


def test_local_store_rejects_an_inert_kms_key_arn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ASTRABOX_AWS_KMS_KEY_ARN",
        "arn:aws:kms:us-west-2:123456789012:key/test-key",
    )

    with pytest.raises(RuntimeError, match="only consumed by the aws_kms"):
        secret_store_for_name("local").validate_configuration()


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


def test_kms_configuration_fails_during_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_SECRET_STORE", "aws_kms")
    monkeypatch.delenv("ASTRABOX_AWS_KMS_KEY_ARN", raising=False)
    monkeypatch.delenv("ASTRABOX_VAULT_MASTER_KEY", raising=False)

    with pytest.raises(BootstrapConfigError) as caught:
        _assert_secret_store_is_configured()
    assert "ASTRABOX_AWS_KMS_KEY_ARN" in str(caught.value)


def test_kms_validation_builds_the_official_runtime_without_contacting_kms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_arn = "arn:aws:kms:us-west-2:123456789012:key/test-key"
    built: list[str] = []
    fake_client = _FakeEncryptionClient()
    monkeypatch.setenv("ASTRABOX_AWS_KMS_KEY_ARN", key_arn)
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", "shared-platform-key")
    monkeypatch.delenv("ASTRABOX_VAULT_MASTER_KEY", raising=False)
    monkeypatch.setattr(
        aws_store_module,
        "_build_encryption_runtime",
        lambda configured: (built.append(configured) or fake_client, "kms-keyring"),
    )

    store = AwsKmsSecretStore()
    store.validate_configuration()
    assert built == [key_arn]


def test_kms_store_rejects_an_inert_local_master_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ASTRABOX_AWS_KMS_KEY_ARN",
        "arn:aws:kms:us-west-2:123456789012:key/test-key",
    )
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", "shared-platform-key")
    monkeypatch.setenv("ASTRABOX_VAULT_MASTER_KEY", "configured-but-unused")

    with pytest.raises(RuntimeError, match="only consumed by the local"):
        AwsKmsSecretStore().validate_configuration()


def test_kms_store_requires_an_immutable_key_arn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ASTRABOX_AWS_KMS_KEY_ARN",
        "arn:aws:kms:us-west-2:123456789012:alias/astrabox",
    )
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", "shared-platform-key")
    monkeypatch.delenv("ASTRABOX_VAULT_MASTER_KEY", raising=False)

    with pytest.raises(RuntimeError, match="immutable AWS KMS key ARN"):
        AwsKmsSecretStore().validate_configuration()


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
