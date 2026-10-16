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
import re
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, Field, ValidationInfo, field_validator, model_validator
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


_CPU_QUANTITY = re.compile(r"(\d+(?:\.\d+)?)(m?)")
_MEMORY_QUANTITY = re.compile(r"(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E)?")
_MEMORY_UNITS = {
    None: 1,
    "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15, "E": 10**18,
    "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60,
}


def cpu_quantity(value: str) -> float:
    """Cores in a Kubernetes CPU quantity such as ``2``, ``0.5`` or ``500m``."""
    match = _CPU_QUANTITY.fullmatch(value)
    if match is None or float(match.group(1)) <= 0:
        raise ValueError(f"{value!r} is not a CPU quantity such as 2, 0.5 or 500m")
    cores = float(match.group(1))
    return cores / 1000 if match.group(2) else cores


def memory_quantity(value: str) -> float:
    """Bytes in a Kubernetes memory quantity such as ``2Gi`` or ``768Mi``."""
    match = _MEMORY_QUANTITY.fullmatch(value)
    if match is None or float(match.group(1)) <= 0:
        raise ValueError(f"{value!r} is not a memory quantity such as 2Gi or 768Mi")
    return float(match.group(1)) * _MEMORY_UNITS[match.group(2)]


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
    * **Database** (``db_url``) — PostgreSQL via SQLAlchemy+asyncpg, the only
      store. Maintained launchers inject a URL built from their generated
      database credential; another launch path must set one.
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

    sandbox_backends: str = Field(
        default="",
        description=(
            "Comma-separated names of the sandbox backends this deployment enables; "
            "empty enables only sandbox_backend."
        ),
    )

    @property
    def enabled_sandbox_backends(self) -> list[str]:
        """``sandbox_backends`` split into names; empty when it is unset."""
        return [
            part.strip().lower()
            for part in str(self.sandbox_backends or "").split(",")
            if part.strip()
        ]

    # --- sandbox resource envelopes ----------------------------------------
    # A conversation's own box, an Agent's shared box and an Assistant's
    # workspace box each have one envelope, for every box of that kind:
    # created on demand, prepared in a pool, or replacing a lost one. Limits are the ceiling; requests are the
    # Kubernetes scheduler's reservation. Memory requests equal the limits, so a
    # node never promises memory it lacks; CPU requests are lower, because a
    # busy CPU only slows a box down. A shared box carries its base runtime once
    # for every conversation it holds, and admits another while its free memory
    # allows. An Assistant's box stays running between messages and is idle
    # most of the time, so it reserves little CPU.
    sandbox_cpu_limit: str = Field(default="1", description="CPU limit of a conversation's own box.")
    sandbox_memory_limit: str = Field(default="1Gi", description="Memory limit of a conversation's own box.")
    sandbox_cpu_request: str = Field(
        default="250m", description="CPU request of a conversation's own box."
    )
    sandbox_memory_request: str = Field(
        default="1Gi", description="Memory request of a conversation's own box."
    )
    shared_sandbox_cpu_limit: str = Field(default="4", description="CPU limit of an Agent's shared box.")
    shared_sandbox_memory_limit: str = Field(
        default="4Gi", description="Memory limit of an Agent's shared box."
    )
    shared_sandbox_cpu_request: str = Field(
        default="1", description="CPU request of an Agent's shared box."
    )
    shared_sandbox_memory_request: str = Field(
        default="4Gi", description="Memory request of an Agent's shared box."
    )
    assistant_sandbox_cpu_limit: str = Field(
        default="1", description="CPU limit of an Assistant's workspace box."
    )
    assistant_sandbox_memory_limit: str = Field(
        default="1Gi", description="Memory limit of an Assistant's workspace box."
    )
    assistant_sandbox_cpu_request: str = Field(
        default="100m", description="CPU request of an Assistant's workspace box."
    )
    assistant_sandbox_memory_request: str = Field(
        default="1Gi", description="Memory request of an Assistant's workspace box."
    )

    @field_validator(
        "sandbox_cpu_limit",
        "sandbox_memory_limit",
        "sandbox_cpu_request",
        "sandbox_memory_request",
        "shared_sandbox_cpu_limit",
        "shared_sandbox_memory_limit",
        "shared_sandbox_cpu_request",
        "shared_sandbox_memory_request",
        "assistant_sandbox_cpu_limit",
        "assistant_sandbox_memory_limit",
        "assistant_sandbox_cpu_request",
        "assistant_sandbox_memory_request",
        mode="before",
    )
    @classmethod
    def _unset_sandbox_resource(cls, value: Any, info: ValidationInfo) -> Any:
        # Compose forwards a variable the operator left unset as "".
        if value == "":
            return cls.model_fields[str(info.field_name)].default
        return value

    @field_validator(
        "sandbox_cpu_limit",
        "sandbox_cpu_request",
        "shared_sandbox_cpu_limit",
        "shared_sandbox_cpu_request",
        "assistant_sandbox_cpu_limit",
        "assistant_sandbox_cpu_request",
        mode="after",
    )
    @classmethod
    def _validate_sandbox_cpu(cls, value: str) -> str:
        cpu_quantity(value)
        return value

    @field_validator(
        "sandbox_memory_limit",
        "sandbox_memory_request",
        "shared_sandbox_memory_limit",
        "shared_sandbox_memory_request",
        "assistant_sandbox_memory_limit",
        "assistant_sandbox_memory_request",
        mode="after",
    )
    @classmethod
    def _validate_sandbox_memory(cls, value: str) -> str:
        memory_quantity(value)
        return value

    # --- sandbox quota -----------------------------------------------------
    # Ceilings on everything one sandbox backend runs for this deployment,
    # whatever created it. Empty leaves a ceiling to its default (memory on a
    # bundled Docker server: the host's memory less 4Gi; otherwise none);
    # "unlimited" states there is none.
    sandbox_quota_boxes: str = Field(default="", description="Most sandboxes a backend runs.")
    sandbox_quota_memory: str = Field(
        default="", description="Most memory, in box limits, a backend's sandboxes may hold."
    )
    sandbox_quota_cpu: str = Field(
        default="", description="Most CPU, in box limits, a backend's sandboxes may hold."
    )
    sandbox_quota_concurrent_creates: str = Field(
        default="", description="Most sandboxes a backend creates at once."
    )

    @field_validator("sandbox_quota_boxes", "sandbox_quota_concurrent_creates", mode="after")
    @classmethod
    def _validate_quota_count(cls, value: str) -> str:
        if value not in ("", "unlimited") and not (value.isdigit() and int(value) > 0):
            raise ValueError(f"{value!r} is not a positive whole number or 'unlimited'")
        return value

    @field_validator("sandbox_quota_memory", mode="after")
    @classmethod
    def _validate_quota_memory(cls, value: str) -> str:
        if value not in ("", "unlimited"):
            memory_quantity(value)
        return value

    @field_validator("sandbox_quota_cpu", mode="after")
    @classmethod
    def _validate_quota_cpu(cls, value: str) -> str:
        if value not in ("", "unlimited"):
            cpu_quantity(value)
        return value

    @model_validator(mode="after")
    def _sandbox_requests_within_limits(self) -> AstraBoxSettings:
        for box, prefix in (("conversation", ""), ("shared", "shared_"), ("assistant", "assistant_")):
            for kind, parse in (("cpu", cpu_quantity), ("memory", memory_quantity)):
                request = getattr(self, f"{prefix}sandbox_{kind}_request")
                limit = getattr(self, f"{prefix}sandbox_{kind}_limit")
                if parse(request) > parse(limit):
                    raise ValueError(
                        f"{box} box {kind} request {request} exceeds its limit {limit}; "
                        "a box cannot reserve more than it may use"
                    )
        return self

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
    scheduler_provider: str = Field(
        default="dbos",
        description=(
            "Name of the scheduler provider that runs scheduled Deployments; "
            "'none' disables them."
        ),
    )
    db_url: str | None = Field(
        default=None,
        description=(
            "PostgreSQL URL. The maintained local launchers supply one from "
            "generated service-scoped credentials; there is no fallback password."
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
        """The configured PostgreSQL URL."""
        if self.db_url:
            return self.db_url
        raise ValueError(
            "ASTRABOX_DB_URL is required. Use the maintained Compose/dev launcher "
            "to generate local database credentials, or set a URL for an external "
            "PostgreSQL service."
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
