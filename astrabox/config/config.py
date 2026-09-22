"""Config object — a dotted/flat accessor over ``app.yml`` + the environment.

Provides a small ``.get`` / ``.get_or_default`` / ``.env`` surface so call sites
can read configuration by dotted (``astrabox.model.base_url``) or flat
(``module_name``) key. Values are sourced natively by ``pydantic-settings``:

    ASTRABOX_* environment  >  ``app.yml``  >  the caller's ``default``

The single application config file is :data:`astrabox.config.app.yml`. There are
no per-environment overlays (``app-{env}.yml``): deployment-specific values come
from environment variables or a secret store. A malformed ``app.yml`` fails loud
at import; an absent file is a clean no-op.

The exported :data:`config` object:

* ``config.get(dotted_or_flat_key, default=None)`` — walks the loaded mapping by
  a flat top-level key or a dotted path, returning ``default`` when the key is
  absent or its value is ``None``.
* ``config.get_or_default(key, default)`` — explicit-default form of ``get``.
* ``config.env`` — the resolved environment string (``SERVER_ENV`` /
  ``ASTRABOX_ENV``, default ``"community"``), read for diagnostics.

Typed ``ASTRABOX_*`` settings (the LLM endpoint/key/model, sandbox backend, state
dir, database) live separately in :mod:`astrabox.config.settings`; the wide
operational surface is :mod:`astrabox.common.utils.settings`. Both, like this
module, resolve their values through native ``pydantic-settings`` sources.
"""

from __future__ import annotations

import os
from functools import lru_cache
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

#: The single application config file. Its keys are read through the native
#: ``YamlConfigSettingsSource`` below (shared with
#: :mod:`astrabox.common.utils.settings`, which reads the same file).
APP_YAML_PATH: Path = Path(__file__).resolve().parent / "app.yml"


@lru_cache(maxsize=1)
def _read_app_yaml() -> dict[str, Any]:
    """Parse ``app.yml`` once (a present-but-malformed file fails loud).

    Cached so the file is read a single time process-wide — both this module's
    :data:`config` singleton and every (uncached) ``load_astrabox_settings()``
    call resolve their YAML values from the one parse.
    """
    if not APP_YAML_PATH.is_file():
        return {}
    with APP_YAML_PATH.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise TypeError(
            f"config file {APP_YAML_PATH} must contain a top-level mapping, "
            f"got {type(loaded).__name__}"
        )
    return loaded


class AppYamlSource(YamlConfigSettingsSource):
    """``YamlConfigSettingsSource`` for ``app.yml``, backed by the cached parse.

    Overriding ``_read_file`` to return :func:`_read_app_yaml` keeps the single
    file read (the uncached ``load_astrabox_settings()`` builds a fresh settings
    object per call) without re-hitting disk each time.
    """

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(
            settings_cls,
            yaml_file=str(APP_YAML_PATH),
            yaml_file_encoding="utf-8",
        )

    def _read_file(self, file_path: Path | Traversable) -> dict[str, Any]:
        return _read_app_yaml()


def _resolve_env() -> str:
    """Resolve the active environment label (``SERVER_ENV`` / ``ASTRABOX_ENV``)."""
    for key in ("SERVER_ENV", "ASTRABOX_ENV"):
        value = str(os.environ.get(key, "")).strip()
        if value:
            return value
    return "community"


class _AppConfig(BaseSettings):
    """Config surface (``.get`` / ``.get_or_default`` / ``.env``).

    Holds the whole ``app.yml`` mapping as model extras (``extra="allow"``), so a
    flat or dotted key resolves by walking that tree. Values come from the native
    pydantic-settings sources (env then YAML); the caller's ``default`` is the
    floor.
    """

    model_config = SettingsConfigDict(
        env_ignore_empty=True,
        extra="allow",
    )

    @classmethod
    def settings_customise_sources(  # noqa: D102 - pydantic hook
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, env_settings, AppYamlSource(settings_cls))

    @property
    def env(self) -> str:
        """Resolved environment label (diagnostic)."""
        return _resolve_env()

    def get(self, key: str, default: Any = None) -> Any:
        """Resolve ``key`` (flat top-level or dotted path); ``default`` if unset."""
        tree: dict[str, Any] = self.model_extra or {}
        if key in tree:
            value = tree[key]
            return default if value is None else value
        node: Any = tree
        for segment in key.split("."):
            if not isinstance(node, dict) or segment not in node:
                return default
            node = node[segment]
        return default if node is None else node

    def get_or_default(self, key: str, default: Any) -> Any:
        """Explicit-default form of :meth:`get`."""
        return self.get(key, default)


#: The process-wide config object. Import as ``from astrabox.config.config import config``.
config = _AppConfig()
