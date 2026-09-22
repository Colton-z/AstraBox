"""AstraBox settings.

Typed application settings backed by stdlib + ``pydantic-settings``:

* :class:`AstraBoxSettings` — a typed ``BaseSettings`` reading ``ASTRABOX_*`` env
  vars and an optional TOML file. It covers the LLM endpoint/key/model the agent
  base talks to, the sandbox backend name, the on-disk state directory, and the
  database backend/URL.
* A ``.get(dotted_key, default=None)`` / ``.env`` shim over the typed tree, so
  dotted-key call sites (``astrabox.model.base_url`` etc.) can read the same
  object.

Precedence (highest first), matching pydantic-settings' default source order with
the TOML source spliced in just above file-less defaults:

    explicit kwargs  >  ASTRABOX_* env vars  >  .env file  >  TOML file  >  field defaults

LLM fields additionally accept the ``ANTHROPIC_*`` names that Anthropic's
``claude-agent-sdk`` itself reads (``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_AUTH_TOKEN`` /
``ANTHROPIC_MODEL``), so the same environment configures both AstraBox and the
delegated agent loop with one set of variables.

An unparseable TOML file or a malformed value fails loud at construction; an
absent TOML file is a normal no-op (the file is optional by design).
"""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

#: Default environment label, used when neither ``ASTRABOX_ENV`` nor ``SERVER_ENV``
#: is set. Overridable via either of those variables.
_DEFAULT_ENV = "community"

#: Default config-file location, overridable via ``ASTRABOX_CONFIG_FILE``. Kept as
#: a module constant (not a settings field) because it must be resolved *before*
#: the settings sources are built — it selects which TOML the TOML source reads.
_DEFAULT_CONFIG_FILE = "astrabox.toml"

_REMOVED_LOCAL_SANDBOX_BACKEND = "direct_docker"
_CURRENT_LOCAL_SANDBOX_BACKEND = "open_sandbox"


def normalize_configured_sandbox_backend(value: Any) -> str:
    """Normalize a deployment-level sandbox backend name.

    Values are stripped and lowercased. The value named by
    :data:`_REMOVED_LOCAL_SANDBOX_BACKEND` is accepted with a warning and
    resolves to ``open_sandbox``. Every other value, including a third-party
    provider name, passes through to provider resolution.
    """
    backend = str(value or "").strip().lower()
    if backend == _REMOVED_LOCAL_SANDBOX_BACKEND:
        from astrabox.common.logger.logger_factory import get_logger

        get_logger(__name__).warning(
            "ASTRABOX_SANDBOX_BACKEND=%s is no longer registered; using %s. "
            "Update the deployment setting to %s.",
            _REMOVED_LOCAL_SANDBOX_BACKEND,
            _CURRENT_LOCAL_SANDBOX_BACKEND,
            _CURRENT_LOCAL_SANDBOX_BACKEND,
        )
        return _CURRENT_LOCAL_SANDBOX_BACKEND
    return backend


def load_env_file_into_process_env() -> None:
    """Load the ``.env`` file into ``os.environ`` — never overriding real env.

    There are two config read paths: the typed fields on
    :class:`AstraBoxSettings` (whose pydantic ``env_file`` source reads ``.env``
    directly) and the wide ``os.getenv``-based operational surface
    (``common/utils/settings.py`` and friends). Without this loader, a value set
    only in ``.env`` reaches the first path but is silently ignored by the
    second — e.g. a pinned ``ASTRABOX_VAULT_MASTER_KEY`` in ``.env`` would be
    ignored and a per-node key generated instead. Loading the file into the
    process environment once, at process entry (``create_app()`` and the CLI),
    gives both paths one config source with one precedence rule:

        real environment  >  .env file  >  defaults

    Idempotent; a missing file is a normal no-op (the file is optional).
    ``ASTRABOX_ENV_FILE`` selects an alternative path, same as the pydantic
    source.
    """
    path = Path(os.environ.get("ASTRABOX_ENV_FILE") or ".env").expanduser()
    if not path.is_file():
        return
    # python-dotenv ships with pydantic-settings (its dotenv engine); parsing
    # with the same library keeps quoting/comment semantics identical between
    # the two read paths.
    from dotenv import dotenv_values

    for key, value in dotenv_values(path).items():
        if value is None:
            continue
        os.environ.setdefault(key, value)


def _resolve_config_path() -> Path | None:
    """Resolve the optional TOML config path, or ``None`` if none is present.

    ``ASTRABOX_CONFIG_FILE`` (if set and non-empty) wins and is *authoritative*:
    if it points at a missing file, this still returns the path so the TOML
    source raises a loud ``FileNotFoundError`` — an explicitly-requested config
    that isn't there is an error, not a silent skip. With no override, this
    probes the default filename in the current working directory and returns
    ``None`` when it is absent (the file is optional by design).
    """
    override = os.environ.get("ASTRABOX_CONFIG_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    candidate = Path.cwd() / _DEFAULT_CONFIG_FILE
    return candidate if candidate.is_file() else None


class _TomlConfigSource(PydanticBaseSettingsSource):
    """A pydantic-settings source that loads a flat-or-nested TOML mapping.

    The TOML is read once and its top-level keys are matched against the settings
    fields (by field name or declared alias). An ``[astrabox]`` table, if present,
    is merged in as the canonical section so a shared project ``pyproject``-style
    file can namespace AstraBox config; bare top-level keys are also honored.
    """

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data
        path = _resolve_config_path()
        if path is None:
            self._data = {}
            return self._data
        # A missing-but-requested file or malformed TOML fails loud here rather
        # than falling back to an empty dict.
        with path.open("rb") as fh:
            loaded = tomllib.load(fh)
        section = loaded.get("astrabox")
        merged: dict[str, Any] = {}
        merged.update({k: v for k, v in loaded.items() if not isinstance(v, dict) or k != "astrabox"})
        if isinstance(section, dict):
            merged.update(section)
        self._data = merged
        return self._data

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:  # noqa: D102 - pydantic hook
        data = self._load()
        if field_name in data:
            return data[field_name], field_name, False
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        data = self._load()
        result: dict[str, Any] = {}
        for field_name, field in self.settings_cls.model_fields.items():
            if field_name in data:
                result[field_name] = data[field_name]
        return result


class AstraBoxSettings(BaseSettings):
    """Typed settings covering the four core config surfaces.

    Field groups:

    * **LLM** (``llm_base_url`` / ``llm_auth_token`` / ``llm_model``) — what the
      ``claude-agent-sdk`` agent base talks to. Aliased to the ``ANTHROPIC_*``
      names the SDK reads natively so one env set drives both layers.
    * **Sandbox** (``sandbox_backend``) — the name-keyed backend selected at the
      runtime seam. Default is ``open_sandbox``; resolution fails loud on an
      unknown name.
    * **Storage** (``storage_provider``) — the name-keyed workspace medium
      selected at the storage seam. Independent of ``sandbox_backend``: where a
      workspace's files live is a durability decision, not a runtime one.
    * **State** (``state_dir``) — on-disk root for workspaces and local artifacts.
    * **Database** (``db_backend`` / ``db_url``) — PostgreSQL via
      SQLAlchemy+asyncpg. Maintained launchers inject a URL built from their
      generated database credential; another launch path must set one.
    """

    model_config = SettingsConfigDict(
        env_prefix="ASTRABOX_",
        env_file=os.environ.get("ASTRABOX_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # --- environment label -------------------------------------------------
    env: str = Field(
        default=_DEFAULT_ENV,
        validation_alias=AliasChoices("ASTRABOX_ENV", "SERVER_ENV"),
        description="Deployment environment label (diagnostic).",
    )

    # --- LLM / agent base --------------------------------------------------
    # Canonical inputs are the ANTHROPIC_* names so claude-agent-sdk and AstraBox
    # share one configuration; ASTRABOX_LLM_* aliases are accepted too.
    llm_base_url: str = Field(
        default="https://api.anthropic.com",
        validation_alias=AliasChoices(
            "ANTHROPIC_BASE_URL", "ASTRABOX_LLM_BASE_URL"
        ),
        description="Base URL of the Anthropic-compatible endpoint the agent loop calls.",
    )
    llm_auth_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ASTRABOX_LLM_AUTH_TOKEN"
        ),
        description="Bearer token / API key for the LLM endpoint. None => unset (the SDK errors on first call).",
    )
    llm_model: str = Field(
        default="claude-opus-4-8",
        validation_alias=AliasChoices("ANTHROPIC_MODEL", "ASTRABOX_LLM_MODEL"),
        description="Default model id passed to the agent loop.",
    )

    # --- sandbox runtime seam ---------------------------------------------
    sandbox_backend: str = Field(
        default="open_sandbox",
        description="Name of the registered sandbox backend (resolved at the runtime seam; fails loud if unknown).",
    )

    @field_validator("sandbox_backend", mode="before")
    @classmethod
    def _normalize_configured_sandbox_backend(cls, value: Any) -> str:
        return normalize_configured_sandbox_backend(value)

    # --- workspace storage seam --------------------------------------------
    storage_provider: str = Field(
        default="mounted_volume",
        description="Name of the registered storage provider (resolved at the storage seam; fails loud if unknown).",
    )
    efs_file_system_id: str = Field(
        default="",
        description="Existing EFS filesystem ID verified by the aws_efs storage provider against its CSI-backed PVC.",
    )

    # --- local state -------------------------------------------------------
    state_dir: Path = Field(
        default=Path("./.astrabox"),
        description="Root directory for local workspaces and artifacts.",
    )

    def resolved_state_dir(self) -> Path:
        """Return the state root with a configured home-directory alias expanded."""
        return self.state_dir.expanduser()

    # --- persistence -------------------------------------------------------
    db_backend: str = Field(
        default="postgresql",
        description="Persistence backend name (PostgreSQL by default).",
    )
    db_url: str | None = Field(
        default=None,
        description=(
            "Explicit database URL. The maintained local launchers supply one "
            "from generated service-scoped credentials; PostgreSQL never uses "
            "a repository-known fallback password. The optional SQLite "
            "compatibility backend derives a file under state_dir."
        ),
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
        # init > env > .env > TOML > defaults. The TOML source sits below the
        # dotenv source so explicit env/.env always wins over the file.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _TomlConfigSource(settings_cls),
        )

    @property
    def resolved_db_url(self) -> str:
        """Return the configured URL or the SQLite compatibility path."""
        if self.db_url:
            return self.db_url
        if self.db_backend in {"postgres", "postgresql"}:
            raise ValueError(
                "ASTRABOX_DB_URL is required for PostgreSQL. Use the maintained "
                "Compose/dev launcher to generate local database credentials, or "
                "set an explicit URL for an external PostgreSQL service."
            )
        if self.db_backend == "sqlite":
            db_path = self.resolved_state_dir() / "astrabox.sqlite"
            return f"sqlite+aiosqlite:///{db_path}"
        raise ValueError(
            f"db_url is required for db_backend={self.db_backend!r}; "
            "only the SQLite compatibility backend has an in-process local default."
        )

    # ---- dotted-key config shim --------------------------------------------
    # The settings reader (common/utils/settings.py::_safe_get) probes
    # config.get(dotted_key) / config.get_or_default(key, default) and
    # config.env; this class exposes that same three-method surface over the
    # typed tree.
    _DOTTED_ALIASES: dict[str, str] = {
        "astrabox.model.base_url": "llm_base_url",
        "astrabox.model.api_key": "llm_auth_token",
        "astrabox.model.model_name": "llm_model",
        "astrabox.sandbox_backend": "sandbox_backend",
        "astrabox.storage_provider": "storage_provider",
        "astrabox.state_dir": "state_dir",
        "astrabox.db.backend": "db_backend",
        "astrabox.db.url": "db_url",
    }

    def get(self, key: str, default: Any = None) -> Any:
        """Dotted/flat-key lookup (``.get`` shim).

        Resolves a known dotted alias to its typed field, else a bare field name,
        else returns ``default``. Returns ``default`` when the resolved value is
        ``None`` (call sites use ``config.get(k) or fallback``).
        """
        field = self._DOTTED_ALIASES.get(key, key)
        if field in type(self).model_fields:
            value = getattr(self, field)
            return default if value is None else value
        return default

    def get_or_default(self, key: str, default: Any) -> Any:
        """Explicit-default lookup (``.get_or_default`` shim)."""
        return self.get(key, default)


@lru_cache(maxsize=1)
def get_settings() -> AstraBoxSettings:
    """Return the process-wide settings singleton.

    Cached so every caller observes the same materialized config. Call
    ``get_settings.cache_clear()`` in tests that mutate the environment between
    cases.
    """
    return AstraBoxSettings()
