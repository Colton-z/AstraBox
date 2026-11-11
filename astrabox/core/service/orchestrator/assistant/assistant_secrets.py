"""Credentials an Assistant's owner supplies for its runtime, kept write-only.

The runtime declares what it needs (``assistant.owner_secrets``); the owner
sets each value once and may replace it. Values live behind the SecretStore
seam under the Assistant's own scope, so the local install keeps them
encrypted at rest and a distribution can select its own secret manager. They
are read only to hand to the adapter that activates the runtime, and never
returned through the API.
"""

from __future__ import annotations

from astrabox.seams.secrets import SecretStore, secret_store_for_name


class AssistantOwnerSecrets:
    """Seal, read for activation, and purge one Assistant's owner secrets."""

    def __init__(
        self,
        *,
        store: SecretStore | None = None,
        secret_store_name: str | None = None,
    ) -> None:
        self._explicit_store = store
        self._secret_store_name = secret_store_name

    def _store(self) -> SecretStore:
        return self._explicit_store or secret_store_for_name(self._secret_store_name)

    @staticmethod
    def _scope(assistant_id: str) -> str:
        return f"assistant/{assistant_id}"

    @staticmethod
    def _key(key: str) -> str:
        return f"owner_secret/{key}"

    async def put(self, assistant_id: str, key: str, value: str) -> None:
        await self._store().put(scope=self._scope(assistant_id), key=self._key(key), value=value)

    async def read(self, assistant_id: str, keys: tuple[str, ...]) -> dict[str, str]:
        """The values set among ``keys``; an unset key is absent."""

        found: dict[str, str] = {}
        for key in keys:
            value = await self._store().get(scope=self._scope(assistant_id), key=self._key(key))
            if value is not None:
                found[key] = value
        return found

    async def purge(self, assistant_id: str) -> None:
        await self._store().purge_scope(scope=self._scope(assistant_id))
