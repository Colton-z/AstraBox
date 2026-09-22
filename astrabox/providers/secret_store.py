"""Built-in ``local`` secret store and shared encrypted collection persistence.

Values are sealed with AES-256-GCM under a deployment master key and stored in
the regular collection ingress (PostgreSQL by default, or whichever repository
backend the deployment selects), so the built-in store provides a real at-rest
guarantee without requiring an external secret service.

Master key resolution (fail-loud between the two, never a mixed state):

* ``ASTRABOX_VAULT_MASTER_KEY`` — urlsafe base64 of exactly 32 bytes; the
  operator-managed path (set it in every replica for multi-replica deploys).
* Otherwise a key is generated once and persisted to ``<state_dir>/vault.key``
  (mode 0600) — the zero-config local path. Losing the file means sealed values
  are unrecoverable (by design); back it up with the state dir.

Each value's ciphertext is bound to its ``(scope, key)`` via AEAD associated
data, so a sealed blob copied onto another credential row fails to open instead
of decrypting under the wrong identity.
"""

from __future__ import annotations

import base64
import os
import secrets as _secrets
import tempfile
from abc import abstractmethod
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from astrabox.persistence.repository.backend import (
    get_async_collection,
    run_mongo_with_retry,
)
from astrabox.seams.secrets import SecretStore, register_secret_store

MASTER_KEY_ENV = "ASTRABOX_VAULT_MASTER_KEY"
MASTER_KEY_FILENAME = "vault.key"
_AWS_KMS_KEY_ARN_ENV = "ASTRABOX_AWS_KMS_KEY_ARN"
_COLLECTION = "vault_secrets"
_NONCE_BYTES = 12


def _state_dir() -> Path:
    from astrabox.config.settings import get_settings

    return get_settings().resolved_state_dir()


def _decode_master_key(raw: str, *, source: str) -> bytes:
    try:
        key = base64.b64decode(raw.strip().encode("ascii"), altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise RuntimeError(f"{source} must be urlsafe base64 of exactly 32 bytes") from exc
    if len(key) != 32:
        raise RuntimeError(f"{source} must decode to exactly 32 bytes (got {len(key)})")
    return key


def _read_master_key_file(key_path: Path) -> bytes:
    try:
        raw = key_path.read_text(encoding="ascii")
    except OSError as exc:
        raise RuntimeError(f"cannot read vault master key file: {key_path}") from exc
    return _decode_master_key(raw, source=f"vault master key file {key_path}")


def _create_master_key_file(key_path: Path) -> bytes:
    """Atomically publish one complete deployment key and return the winner.

    The temporary file is fully written and synced before the hard link makes
    it visible at ``key_path``. Concurrent starters therefore either publish
    their complete key or read the complete key another starter published;
    none can observe the empty file produced by a create-then-write sequence.
    """
    key = _secrets.token_bytes(32)
    encoded = base64.urlsafe_b64encode(key)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{key_path.name}.", dir=key_path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as temporary_file:
            os.fchmod(temporary_file.fileno(), 0o600)
            temporary_file.write(encoded)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        try:
            os.link(temporary_path, key_path)
        except FileExistsError:
            return _read_master_key_file(key_path)

        directory_fd = os.open(key_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return key
    finally:
        temporary_path.unlink(missing_ok=True)


def load_master_key() -> bytes:
    """The 32-byte deployment master key: env override, else the persisted state-dir key.

    Public because it is the deployment's shared secret ROOT, not just the
    vault's: other subsystems derive their own domain-separated subkeys from it
    (the transcript capability signer does), so a deployment that can seal a
    vault value can also sign a fence token with zero extra configuration.
    Generation is once-per-deployment (0600, under the state dir); multi-replica
    deployments share ASTRABOX_VAULT_MASTER_KEY instead — one env var, every
    derived subsystem follows.
    """
    from astrabox.seams.secrets import configured_secret_store_name

    selected_store = configured_secret_store_name()
    if selected_store != "local":
        raise RuntimeError(
            "the selected secret store does not expose a deployment master key; "
            "set ASTRABOX_TRANSCRIPT_SIGNING_KEY for shared platform signing material"
        )

    raw = str(os.environ.get(MASTER_KEY_ENV) or "").strip()
    if raw:
        return _decode_master_key(raw, source=MASTER_KEY_ENV)

    key_path = _state_dir() / MASTER_KEY_FILENAME
    if key_path.exists():
        return _read_master_key_file(key_path)
    return _create_master_key_file(key_path)


class EncryptedCollectionSecretStore(SecretStore):
    """Shared collection persistence for providers that return sealed strings."""

    @abstractmethod
    async def _seal_value(self, *, scope: str, key: str, value: str) -> str:
        """Return an authenticated encrypted representation of ``value``."""

    @abstractmethod
    async def _unseal_value(self, *, scope: str, key: str, sealed: str) -> str:
        """Authenticate and decrypt one provider-produced representation."""

    async def put(self, *, scope: str, key: str, value: str) -> None:
        sealed = await self._seal_value(scope=scope, key=key, value=value)

        async def _put() -> None:
            collection = await get_async_collection(_COLLECTION)
            await collection.update_one(
                {"scope": scope, "key": key},
                {"$set": {"scope": scope, "key": key, "sealed": sealed}},
                upsert=True,
            )

        await run_mongo_with_retry("vault_secrets.put", _put)

    async def get(self, *, scope: str, key: str) -> str | None:
        async def _get() -> Any:
            collection = await get_async_collection(_COLLECTION)
            return await collection.find_one({"scope": scope, "key": key})

        doc = await run_mongo_with_retry("vault_secrets.get", _get)
        if not isinstance(doc, dict):
            return None
        sealed = str(doc.get("sealed") or "")
        if not sealed:
            return None
        return await self._unseal_value(scope=scope, key=key, sealed=sealed)

    async def delete(self, *, scope: str, key: str) -> None:
        async def _delete() -> None:
            collection = await get_async_collection(_COLLECTION)
            await collection.delete_many({"scope": scope, "key": key})

        await run_mongo_with_retry("vault_secrets.delete", _delete)

    async def purge_scope(self, *, scope: str) -> None:
        async def _purge() -> None:
            collection = await get_async_collection(_COLLECTION)
            await collection.delete_many({"scope": scope})

        await run_mongo_with_retry("vault_secrets.purge_scope", _purge)


class LocalEncryptedSecretStore(EncryptedCollectionSecretStore):
    """AES-GCM sealed values in the ``vault_secrets`` collection."""

    name = "local"

    def __init__(self) -> None:
        self._key: bytes | None = None  # resolved lazily, once

    def _aesgcm(self) -> AESGCM:
        if self._key is None:
            self._key = load_master_key()
        return AESGCM(self._key)

    def validate_configuration(self) -> None:
        if str(os.getenv(_AWS_KMS_KEY_ARN_ENV, "") or "").strip():
            raise RuntimeError(
                f"{_AWS_KMS_KEY_ARN_ENV} is only consumed by the aws_kms secret "
                "store; remove it or set ASTRABOX_SECRET_STORE=aws_kms"
            )
        load_master_key()

    @staticmethod
    def _aad(scope: str, key: str) -> bytes:
        return f"{scope}\x1f{key}".encode("utf-8")

    def _seal(self, *, scope: str, key: str, value: str) -> str:
        nonce = _secrets.token_bytes(_NONCE_BYTES)
        ct = self._aesgcm().encrypt(nonce, value.encode("utf-8"), self._aad(scope, key))
        return base64.urlsafe_b64encode(nonce + ct).decode("ascii")

    def _unseal(self, *, scope: str, key: str, sealed: str) -> str:
        blob = base64.urlsafe_b64decode(sealed.encode("ascii"))
        nonce, ct = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
        try:
            pt = self._aesgcm().decrypt(nonce, ct, self._aad(scope, key))
        except InvalidTag as exc:
            raise RuntimeError(
                f"vault secret failed authenticated decryption (scope={scope!r} "
                f"key={key!r}) — wrong master key or tampered/moved ciphertext"
            ) from exc
        return pt.decode("utf-8")

    async def _seal_value(self, *, scope: str, key: str, value: str) -> str:
        return self._seal(scope=scope, key=key, value=value)

    async def _unseal_value(self, *, scope: str, key: str, sealed: str) -> str:
        return self._unseal(scope=scope, key=key, sealed=sealed)


register_secret_store(LocalEncryptedSecretStore())
