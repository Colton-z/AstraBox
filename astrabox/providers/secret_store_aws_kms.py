"""AWS KMS-backed credential storage using the AWS Encryption SDK.

The provider keeps ciphertext in AstraBox's normal persistence collection and
uses an AWS KMS keyring for envelope encryption. AWS KMS protects a fresh data
key for each value; plaintext data keys and secret values are never persisted.
The SDK message carries the encrypted data key, so stateless replicas need only
the same KMS key permission and database.

Install ``astrabox[aws-kms]``, set ``ASTRABOX_SECRET_STORE=aws_kms``, and provide
an immutable symmetric KMS key ARN in ``ASTRABOX_AWS_KMS_KEY_ARN``. The official
server image includes this extra. The execution role needs ``kms:GenerateDataKey``
and ``kms:Decrypt`` for that key.
"""

from __future__ import annotations

import base64
import os
import threading
from typing import Any

from anyio import to_thread

from astrabox.providers.secret_store import EncryptedCollectionSecretStore
from astrabox.seams.secrets import register_secret_store

AWS_KMS_KEY_ARN_ENV = "ASTRABOX_AWS_KMS_KEY_ARN"
_LOCAL_MASTER_KEY_ENV = "ASTRABOX_VAULT_MASTER_KEY"
_PLATFORM_SIGNING_KEY_ENV = "ASTRABOX_TRANSCRIPT_SIGNING_KEY"
_PURPOSE = "astrabox-credential-store"


def _key_region(key_arn: str) -> str:
    parts = key_arn.split(":", 5)
    if (
        len(parts) != 6
        or parts[0] != "arn"
        or not parts[1]
        or parts[2] != "kms"
        or not parts[3]
        or not parts[4]
        or not parts[5].startswith("key/")
    ):
        raise RuntimeError(
            f"{AWS_KMS_KEY_ARN_ENV} must be an immutable AWS KMS key ARN "
            "(arn:<partition>:kms:<region>:<account>:key/<id>)"
        )
    return parts[3]


def _build_encryption_runtime(key_arn: str) -> tuple[Any, Any]:
    """Construct the official SDK client and KMS keyring without a KMS call."""
    try:
        import aws_encryption_sdk
        import boto3
        from aws_cryptographic_material_providers.mpl import (
            AwsCryptographicMaterialProviders,
        )
        from aws_cryptographic_material_providers.mpl.config import (
            MaterialProvidersConfig,
        )
        from aws_cryptographic_material_providers.mpl.models import (
            CreateAwsKmsKeyringInput,
        )
        from aws_encryption_sdk import CommitmentPolicy
    except ImportError as exc:
        raise RuntimeError(
            "the aws_kms secret store requires the 'aws-kms' extra; "
            "install astrabox[aws-kms] or use the official server image"
        ) from exc

    kms_client = boto3.client("kms", region_name=_key_region(key_arn))
    material_provider = AwsCryptographicMaterialProviders(config=MaterialProvidersConfig())
    keyring = material_provider.create_aws_kms_keyring(
        input=CreateAwsKmsKeyringInput(
            kms_key_id=key_arn,
            kms_client=kms_client,
        )
    )
    client = aws_encryption_sdk.EncryptionSDKClient(
        commitment_policy=CommitmentPolicy.REQUIRE_ENCRYPT_REQUIRE_DECRYPT
    )
    return client, keyring


class AwsKmsSecretStore(EncryptedCollectionSecretStore):
    """Envelope-encrypted values shared by stateless AstraBox replicas."""

    name = "aws_kms"

    def __init__(self) -> None:
        self._runtime: tuple[Any, Any] | None = None
        self._runtime_lock = threading.Lock()

    @staticmethod
    def _configured_key_arn() -> str:
        key_arn = str(os.getenv(AWS_KMS_KEY_ARN_ENV, "") or "").strip()
        if not key_arn:
            raise RuntimeError(
                f"{AWS_KMS_KEY_ARN_ENV} is required when ASTRABOX_SECRET_STORE=aws_kms"
            )
        _key_region(key_arn)
        return key_arn

    def _encryption_runtime(self) -> tuple[Any, Any]:
        if self._runtime is None:
            with self._runtime_lock:
                if self._runtime is None:
                    self._runtime = _build_encryption_runtime(self._configured_key_arn())
        return self._runtime

    @staticmethod
    def _encryption_context(scope: str, key: str) -> dict[str, str]:
        # AWS records encryption context in CloudTrail, so these fields contain
        # only record identity and purpose; no secret value is ever included.
        return {
            "astrabox-purpose": _PURPOSE,
            "astrabox-scope": scope,
            "astrabox-key": key,
        }

    def validate_configuration(self) -> None:
        self._configured_key_arn()
        if str(os.getenv(_LOCAL_MASTER_KEY_ENV, "") or "").strip():
            raise RuntimeError(
                f"{_LOCAL_MASTER_KEY_ENV} is only consumed by the local secret store; "
                "remove it when ASTRABOX_SECRET_STORE=aws_kms"
            )
        if not str(os.getenv(_PLATFORM_SIGNING_KEY_ENV, "") or "").strip():
            raise RuntimeError(
                f"{_PLATFORM_SIGNING_KEY_ENV} is required when "
                "ASTRABOX_SECRET_STORE=aws_kms because KMS does not expose raw "
                "key material for platform signing"
            )
        self._encryption_runtime()

    def _seal_sync(self, scope: str, key: str, value: str) -> str:
        client, keyring = self._encryption_runtime()
        try:
            ciphertext, _header = client.encrypt(
                source=value.encode("utf-8"),
                keyring=keyring,
                encryption_context=self._encryption_context(scope, key),
            )
        except Exception as exc:
            raise RuntimeError(
                f"AWS KMS secret encryption failed (scope={scope!r} key={key!r})"
            ) from exc
        return base64.b64encode(ciphertext).decode("ascii")

    def _unseal_sync(self, scope: str, key: str, sealed: str) -> str:
        try:
            ciphertext = base64.b64decode(sealed.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError) as exc:
            raise RuntimeError(
                f"invalid AWS KMS ciphertext encoding (scope={scope!r} key={key!r})"
            ) from exc

        client, keyring = self._encryption_runtime()
        try:
            plaintext, _header = client.decrypt(
                source=ciphertext,
                keyring=keyring,
                encryption_context=self._encryption_context(scope, key),
            )
        except Exception as exc:
            raise RuntimeError(
                f"AWS KMS secret decryption failed (scope={scope!r} key={key!r})"
            ) from exc
        return plaintext.decode("utf-8")

    async def _seal_value(self, *, scope: str, key: str, value: str) -> str:
        return await to_thread.run_sync(self._seal_sync, scope, key, value)

    async def _unseal_value(self, *, scope: str, key: str, sealed: str) -> str:
        return await to_thread.run_sync(self._unseal_sync, scope, key, sealed)


register_secret_store(AwsKmsSecretStore())


__all__ = ["AWS_KMS_KEY_ARN_ENV", "AwsKmsSecretStore"]
