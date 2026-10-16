"""Built-in ``local`` secret store and the database persistence of sealed values.

Values are sealed with AES-256-GCM under a deployment master key and stored in
the platform's PostgreSQL database (the ``secrets`` table), so the built-in store
provides a real at-rest guarantee without requiring an external secret service.

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
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Table, delete, select
from sqlalchemy.dialects.postgresql import Insert, insert
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.persistence.sql import execute, fetch_value
from astrabox.persistence.sql.models.vaults import Secret
from astrabox.seams.secrets import SecretStore, register_secret_store

MASTER_KEY_ENV = "ASTRABOX_VAULT_MASTER_KEY"
MASTER_KEY_FILENAME = "vault.key"
_AWS_KMS_KEY_ARN_ENV = "ASTRABOX_AWS_KMS_KEY_ARN"
_NONCE_BYTES = 12

_SECRETS = cast(Table, Secret.__table__)


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
    """Sealed values in the platform database, one ``secrets`` row per (scope, key).

    Subclasses provide the sealing. Because the rows sit beside the records
    they belong to, a caller can write them in the same transaction as those
    records: :meth:`seal` each value first, since sealing may call a key
    service and must not hold a transaction open, then pass the sealed values to
    :meth:`apply` on the transaction's connection.
    """

    @abstractmethod
    async def _seal_value(self, *, scope: str, key: str, value: str) -> str:
        """Return an authenticated encrypted representation of ``value``."""

    @abstractmethod
    async def _unseal_value(self, *, scope: str, key: str, sealed: str) -> str:
        """Authenticate and decrypt one provider-produced representation."""

    async def seal(self, *, scope: str, key: str, value: str) -> str:
        """``value`` sealed for ``(scope, key)``, ready for :meth:`apply`."""
        return await self._seal_value(scope=scope, key=key, value=value)

    async def unseal(self, *, scope: str, key: str, sealed: str) -> str:
        """Decrypt ciphertext read with its owner's record in one database snapshot."""
        return await self._unseal_value(scope=scope, key=key, sealed=sealed)

    async def apply(
        self,
        conn: AsyncConnection,
        *,
        scope: str,
        put: Mapping[str, str] | None = None,
        remove: Collection[str] = (),
        purge: bool = False,
    ) -> None:
        """Change ``scope``'s values inside the transaction ``conn`` belongs to.

        ``purge`` removes every value in the scope and ``remove`` the named
        keys; then ``put`` stores its values, which map each key to what
        :meth:`seal` returned for this scope and that key.
        """
        if purge:
            await conn.execute(delete(_SECRETS).where(_SECRETS.c.scope == scope))
        elif remove:
            await conn.execute(
                delete(_SECRETS).where(_SECRETS.c.scope == scope, _SECRETS.c.key.in_(remove))
            )
        if put:
            await conn.execute(
                _upsert([{"scope": scope, "key": key, "sealed": sealed} for key, sealed in put.items()])
            )

    async def put(self, *, scope: str, key: str, value: str) -> None:
        sealed = await self._seal_value(scope=scope, key=key, value=value)
        await execute(_upsert([{"scope": scope, "key": key, "sealed": sealed}]))

    async def get(self, *, scope: str, key: str) -> str | None:
        sealed = await fetch_value(
            select(_SECRETS.c.sealed).where(_SECRETS.c.scope == scope, _SECRETS.c.key == key)
        )
        if not sealed:
            return None
        return await self.unseal(scope=scope, key=key, sealed=str(sealed))

    async def delete(self, *, scope: str, key: str) -> None:
        await execute(delete(_SECRETS).where(_SECRETS.c.scope == scope, _SECRETS.c.key == key))

    async def purge_scope(self, *, scope: str) -> None:
        await execute(delete(_SECRETS).where(_SECRETS.c.scope == scope))


def _upsert(rows: list[dict[str, str]]) -> Insert:
    """Store ``rows``, replacing the sealed value of any (scope, key) already present.

    One statement, so concurrent first writes of one key leave one row.
    """
    statement = insert(_SECRETS).values(rows)
    return statement.on_conflict_do_update(
        index_elements=[_SECRETS.c.scope, _SECRETS.c.key],
        set_={"sealed": statement.excluded.sealed},
    )


class LocalEncryptedSecretStore(EncryptedCollectionSecretStore):
    """AES-GCM sealed values in the ``secrets`` table."""

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
