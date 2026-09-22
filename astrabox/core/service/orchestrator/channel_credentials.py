"""Encrypted, write-only credentials for channel bindings.

Deployment rows hold routing and policy. Platform tokens live behind the
existing SecretStore seam so the local install gets AES-256-GCM at rest and an
enterprise distribution can select its own secret manager without changing the
channel providers. Hydration is deliberately late: only a source connection or
an outbound delivery receives the values, and no durable work item does.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from astrabox.common.utils.errors import APIError
from astrabox.seams.channel import ChannelProvider
from astrabox.seams.secrets import SecretStore, secret_store_for_name

_CREDENTIAL_KEY = "channel_credentials"


class ChannelCredentialService:
    """Validate, seal, rotate, and hydrate one binding's credentials."""

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
    def _scope(deployment_id: str) -> str:
        return f"deployment/{deployment_id}"

    @staticmethod
    def normalize(
        provider: ChannelProvider, value: Any, *, required: bool
    ) -> dict[str, Any]:
        if value is None:
            raw: dict[str, Any] = {}
        elif isinstance(value, Mapping):
            raw = dict(value)
        else:
            raise APIError(
                code="CHANNEL_CREDENTIALS_INVALID",
                message="credentials must be an object",
                status_code=400,
            )
        try:
            normalized = provider.normalize_credentials(raw)
        except APIError:
            raise
        except (TypeError, ValueError) as exc:
            raise APIError(
                code="CHANNEL_CREDENTIALS_INVALID",
                message=str(exc),
                status_code=400,
            ) from exc
        if not isinstance(normalized, dict):
            raise RuntimeError(
                f"channel provider {provider.name!r} normalize_credentials "
                "must return a dict"
            )
        if required and not normalized:
            raise APIError(
                code="CHANNEL_CREDENTIALS_INVALID",
                message=f"credentials are required for channel {provider.name!r}",
                status_code=400,
            )
        return normalized

    async def put(self, deployment_id: str, credentials: Mapping[str, Any]) -> None:
        encoded = json.dumps(
            dict(credentials), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        await self._store().put(
            scope=self._scope(deployment_id), key=_CREDENTIAL_KEY, value=encoded
        )

    async def get(self, deployment_id: str) -> dict[str, Any] | None:
        encoded = await self._store().get(
            scope=self._scope(deployment_id), key=_CREDENTIAL_KEY
        )
        if encoded is None:
            return None
        try:
            value = json.loads(encoded)
        except Exception as exc:
            raise RuntimeError(
                f"channel credentials for deployment {deployment_id!r} are corrupt"
            ) from exc
        if not isinstance(value, dict):
            raise RuntimeError(
                f"channel credentials for deployment {deployment_id!r} are not an object"
            )
        return value

    async def purge(self, deployment_id: str) -> None:
        await self._store().purge_scope(scope=self._scope(deployment_id))

    async def hydrate(
        self, binding: Mapping[str, Any], provider: ChannelProvider
    ) -> dict[str, Any]:
        """Return an ephemeral binding carrying validated credentials."""

        descriptor = provider.describe()
        if not descriptor.credential_fields:
            return dict(binding)
        deployment_id = str(binding.get("deployment_id") or "").strip()
        if not deployment_id:
            raise RuntimeError("channel credential hydration requires deployment_id")
        stored = await self.get(deployment_id)
        if stored is None:
            raise APIError(
                code="CHANNEL_CREDENTIALS_UNAVAILABLE",
                message=(
                    f"channel {provider.name!r} has no stored platform credentials; "
                    "replace them from the Deployment page"
                ),
                status_code=503,
            )
        normalized = self.normalize(provider, stored, required=True)
        return {**dict(binding), "channel_credentials": normalized}


__all__ = ["ChannelCredentialService"]
