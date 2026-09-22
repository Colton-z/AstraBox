"""Secret resolution from the process environment.

Secrets are read directly from environment variables — there is no external
secret manager. :class:`SecretProvider.get_secret(secret_name, default=None)`
maps a logical secret name to an environment-variable key
(``secret_name.upper().replace("-", "_")``) and returns its value, or ``default``
when the name is empty or the variable is unset.

A missing secret returns ``default``; the caller decides whether that is fatal.
"""

from __future__ import annotations

import os

__all__ = ["SecretProvider", "secret_env_key"]


def secret_env_key(secret_name: str) -> str:
    """Map a logical secret name to its environment-variable key.

    ``"api-key"`` -> ``"API_KEY"``: uppercased with ``-`` normalized to
    ``_``, so configuration can refer to secrets by their human name while the
    environment variable stays conventional.
    """
    return secret_name.upper().replace("-", "_")


class SecretProvider:
    """Resolve named secrets from the process environment (env-var only)."""

    @staticmethod
    def get_secret(secret_name: str, default: str | None = None) -> str | None:
        """Return the secret value for ``secret_name`` from the environment.

        Looks up :func:`secret_env_key`; returns ``default`` when the name is
        empty or the variable is unset/blank. Never raises and never consults a
        secret manager — secrets live in the environment.
        """
        if not secret_name:
            return default
        value = os.getenv(secret_env_key(secret_name))
        if value:
            return value
        return default
