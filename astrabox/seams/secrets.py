"""Secret-store seam — the ``SecretStore`` contract and registry.

One provider owns WHERE vault secret material lives and how it is protected at
rest. The vault feature (``vault_service``) stores only credential *metadata*
(names, scoping, injection rules) in the regular repository; every sensitive
value (tokens, secret values, refresh tokens, client secrets) is written to and
read from a :class:`SecretStore`, keyed by ``(scope, key)``:

* ``scope`` — the owning aggregate, e.g. ``vault/<vault_id>`` (purged as a unit
  when the vault is archived/deleted);
* ``key`` — the value's slot inside the scope, e.g.
  ``<credential_id>/secret_value``.

Values are write-mostly: the platform reads them back only at injection time
(the credential proxy attaching an ``Authorization`` header, an egress
substitution) — they are never returned by any API.

The ``local`` provider (:mod:`astrabox.providers.secret_store`) keeps values
encrypted at rest (AES-GCM) in the regular metadata store under a persistent
deployment key. The ``aws_kms`` provider uses the AWS Encryption SDK for
envelope encryption while keeping ciphertext in that same store. A deployment
selects one with ``ASTRABOX_SECRET_STORE``; an external provider can register at
the ``astrabox.providers.secrets`` entry-point group. Dispatch is name-keyed and
fails loud on an unknown or misconfigured provider.

This module imports only the standard library.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod

#: Entry-point group an external package registers a provider under.
ENTRY_POINT_GROUP = "astrabox.providers.secrets"
#: Deployment-wide provider selection. Empty keeps the zero-config local store.
SECRET_STORE_ENV = "ASTRABOX_SECRET_STORE"


class SecretStore(ABC):
    """One secret backend's storage of sensitive values, keyed by (scope, key)."""

    #: Registry key. Required, non-empty; lowercased when registered.
    name: str

    def validate_configuration(self) -> None:
        """Validate deployment configuration without reading secret material.

        Providers with no external prerequisite keep this no-op. A provider
        backed by a service should raise ``RuntimeError`` here when its package
        or required configuration is absent so application startup fails before
        the first credential operation.
        """

    @abstractmethod
    async def put(self, *, scope: str, key: str, value: str) -> None:
        """Store (create or replace) one value."""

    @abstractmethod
    async def get(self, *, scope: str, key: str) -> str | None:
        """Return the value, or ``None`` when absent (absence is a normal
        outcome — e.g. an archived credential whose payload was purged)."""

    @abstractmethod
    async def delete(self, *, scope: str, key: str) -> None:
        """Delete one value (idempotent)."""

    @abstractmethod
    async def purge_scope(self, *, scope: str) -> None:
        """Delete every value under ``scope`` (archive/delete of the owning
        aggregate; idempotent)."""


_STORES: dict[str, SecretStore] = {}


def register_secret_store(store: SecretStore) -> None:
    """Register a store under its ``name``. Last registration wins."""
    name = str(getattr(store, "name", "") or "").strip().lower()
    if not name:
        raise RuntimeError("secret store must have a non-empty name")
    _STORES[name] = store


def configured_secret_store_name(name: str | None = None) -> str:
    """Resolve an explicit provider name or the deployment-wide selection."""
    explicit = str(name or "").strip().lower()
    if explicit:
        return explicit
    return str(os.getenv(SECRET_STORE_ENV, "local") or "local").strip().lower() or "local"


def secret_store_for_name(name: str | None) -> SecretStore:
    """Resolve a store by name, or raise listing the registered names.

    An empty name resolves through ``ASTRABOX_SECRET_STORE``; its default is
    ``local`` so a single-server installation remains zero-configuration."""
    wanted = configured_secret_store_name(name)
    store = _STORES.get(wanted)
    if store is None:
        raise RuntimeError(
            f"no SecretStore registered for name={wanted!r} (registered: {sorted(_STORES)})"
        )
    return store


def registered_secret_store_names() -> list[str]:
    """The registered store names, sorted (for schemas/diagnostics)."""
    return sorted(_STORES)


__all__ = [
    "ENTRY_POINT_GROUP",
    "SECRET_STORE_ENV",
    "SecretStore",
    "configured_secret_store_name",
    "register_secret_store",
    "secret_store_for_name",
    "registered_secret_store_names",
]
