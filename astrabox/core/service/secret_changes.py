"""The secret-store side of a metadata change, committed with the metadata.

A service that keeps a record's metadata in the platform database and its
secret values in the :class:`astrabox.seams.secrets.SecretStore` prepares one
:class:`SecretStoreChange` per change and hands :meth:`SecretStoreChange.apply`
to the repository, which runs it inside the metadata change's transaction after
its statements have succeeded. A store that keeps its values in the platform
database then commits with the metadata or not at all.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.providers.secret_store import EncryptedCollectionSecretStore
from astrabox.seams.secrets import SecretStore


@dataclass(frozen=True)
class SecretStoreChange:
    """The secret-store writes of one metadata change; built by :func:`prepare_secret_change`.

    ``put`` holds sealed values when the store keeps its values in the platform
    database and plain values otherwise.
    """

    store: SecretStore
    scope: str
    put: Mapping[str, str]
    remove: tuple[str, ...]
    purge: bool

    async def apply(self, conn: AsyncConnection) -> None:
        if isinstance(self.store, EncryptedCollectionSecretStore):
            await self.store.apply(
                conn, scope=self.scope, put=self.put, remove=self.remove, purge=self.purge
            )
            return
        if self.purge:
            await self.store.purge_scope(scope=self.scope)
        for key in self.remove:
            await self.store.delete(scope=self.scope, key=key)
        for key, value in self.put.items():
            await self.store.put(scope=self.scope, key=key, value=value)

    async def discard(self) -> None:
        """Remove what :meth:`apply` stored for a change whose transaction did not commit.

        Only a store outside the database needs it: there the values were
        written before the commit failed. Callers use it for new keys only,
        which held nothing before the change.
        """
        if isinstance(self.store, EncryptedCollectionSecretStore):
            return
        for key in self.put:
            await self.store.delete(scope=self.scope, key=key)


async def prepare_secret_change(
    store: SecretStore,
    scope: str,
    *,
    put: Mapping[str, str] | None = None,
    remove: Sequence[str] = (),
    purge: bool = False,
) -> SecretStoreChange:
    """The secret-store writes that go with one metadata change of the record ``scope`` names.

    A store that keeps its values in the platform database writes them on the
    metadata transaction's connection, so values and metadata commit together
    or not at all. The values are sealed here, before that transaction opens,
    because sealing may call a key service.

    A store outside the database cannot join the transaction. Its writes run
    inside it after the metadata statements have succeeded and before the
    commit: a change the database refuses never reaches the store, and a store
    write that fails rolls the metadata back. Only a commit that fails after
    the store writes leaves the two apart; a create then discards its values
    (:meth:`SecretStoreChange.discard`), and a removal can be repeated.
    """
    values = dict(put or {})
    if isinstance(store, EncryptedCollectionSecretStore):
        values = {
            key: await store.seal(scope=scope, key=key, value=value)
            for key, value in values.items()
        }
    return SecretStoreChange(
        store=store, scope=scope, put=values, remove=tuple(remove), purge=purge
    )


__all__ = ["SecretStoreChange", "prepare_secret_change"]
