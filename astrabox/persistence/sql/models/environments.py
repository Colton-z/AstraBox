"""Environments: the runtime and network policy an Agent or Assistant runs in."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Text, func, true
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from astrabox.persistence.sql.base import Base


class Environment(Base):
    """An Environment, keyed by its name: what Agents and Assistants reference.

    One column per field of the administrator's form
    (``environment_schema.ENV_FIELD_SCHEMA``); the nested ``networking``,
    ``provider_access`` and ``tracing`` objects are flattened into columns with
    their prefix. The two credentials (``provider_access.api_key`` and
    ``tracing.auth_token``) are never stored here: the row holds the key each
    value has in the secret store, under the Environment's own scope
    (``environment_repository.secret_scope``).
    """

    __tablename__ = "environments"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    engine_kind: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(server_default=true())
    #: Empty means the deployment's default backend.
    sandbox_backend: Mapped[str | None] = mapped_column(Text)
    #: An image pin; empty means the engine adapter's deployment default.
    runtime_template_name: Mapped[str | None] = mapped_column(Text)
    network_type: Mapped[str] = mapped_column(Text)
    #: Set for ``limited`` networking only.
    network_allowed_hosts: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    network_allow_mcp_servers: Mapped[bool | None]
    idle_action: Mapped[str] = mapped_column(Text)
    sandbox_tenancy: Mapped[str | None] = mapped_column(Text)
    sandbox_permission_level: Mapped[str | None] = mapped_column(Text)
    endpoint_provider: Mapped[str | None] = mapped_column(Text)
    provider_base_url: Mapped[str | None] = mapped_column(Text)
    provider_api_key_ref: Mapped[str | None] = mapped_column(Text)
    provider_api_key_secret_name: Mapped[str | None] = mapped_column(Text)
    tracing_enabled: Mapped[bool | None]
    tracing_endpoint: Mapped[str | None] = mapped_column(Text)
    #: Header names to values, sent to the collector as given.
    tracing_headers: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    tracing_auth_token_ref: Mapped[str | None] = mapped_column(Text)
    tracing_auth_token_secret_name: Mapped[str | None] = mapped_column(Text)
    tracing_environment: Mapped[str | None] = mapped_column(Text)
    tracing_signals: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    tracing_log_user_prompt: Mapped[bool | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_by: Mapped[str | None] = mapped_column(Text)
