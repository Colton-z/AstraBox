"""Secret resolution for the persistence/runtime layer — env-var only.

Secrets come from the process environment; there is no external secret
manager, governance gate, or silent recovery. A missing secret returns
``default`` and the caller decides whether that is fatal.

This is the import path the runtime/persistence call sites use
(``from astrabox.common.utils.secrets import SecretProvider``). It re-exports
the canonical :class:`~astrabox.secrets.SecretProvider`; the public surface
(``SecretProvider.get_secret(secret_name, default=None) -> str | None`` and
``secret_env_key``) matches the top-level module exactly.
"""

from __future__ import annotations

from astrabox.secrets import SecretProvider, secret_env_key

__all__ = ["SecretProvider", "secret_env_key"]
