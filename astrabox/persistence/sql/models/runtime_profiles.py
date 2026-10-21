"""RuntimeProfiles: the runtime and network policy an Agent or Assistant runs in."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, true
from sqlalchemy.orm import Mapped, mapped_column

from astrabox.persistence.sql.base import Base
from astrabox.persistence.sql.schema import MYSQL_TABLE_OPTIONS, boolean_checks, string_list_check
from astrabox.persistence.sql.types import KeyText, StringList, payload_json, payload_text


class RuntimeProfile(Base):
    """A RuntimeProfile, keyed by its name: what Agents and Assistants reference.

    One column per field of the administrator's form
    (``runtime_profile_schema.RUNTIME_PROFILE_FIELD_SCHEMA``); the nested ``networking``,
    ``provider_access`` and ``tracing`` objects are flattened into columns with
    their prefix. The two credentials (``provider_access.api_key`` and
    ``tracing.auth_token``) are never stored here: the row holds the key each
    value has in the secret store, under the RuntimeProfile's own scope
    (``runtime_profile_repository.secret_scope``).
    """

    __tablename__ = "runtime_profiles"
    __table_args__ = (
        string_list_check("network_allowed_hosts"),
        string_list_check("tracing_signals"),
        *boolean_checks("enabled", "network_allow_mcp_servers", "tracing_enabled", "tracing_log_user_prompt"),
        MYSQL_TABLE_OPTIONS,
    )

    name: Mapped[str] = mapped_column(KeyText(512), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(payload_text())
    description: Mapped[str | None] = mapped_column(payload_text())
    engine_kind: Mapped[str] = mapped_column(payload_text())
    enabled: Mapped[bool] = mapped_column(server_default=true())
    #: Empty means the deployment's default backend.
    sandbox_backend: Mapped[str | None] = mapped_column(payload_text())
    #: An image pin; empty means the engine adapter's deployment default.
    runtime_template_name: Mapped[str | None] = mapped_column(payload_text())
    network_type: Mapped[str] = mapped_column(payload_text())
    #: Set for ``limited`` networking only.
    network_allowed_hosts: Mapped[list[str] | None] = mapped_column(StringList())
    network_allow_mcp_servers: Mapped[bool | None]
    idle_action: Mapped[str] = mapped_column(payload_text())
    sandbox_tenancy: Mapped[str | None] = mapped_column(payload_text())
    sandbox_permission_level: Mapped[str | None] = mapped_column(payload_text())
    endpoint_provider: Mapped[str | None] = mapped_column(payload_text())
    provider_base_url: Mapped[str | None] = mapped_column(payload_text())
    provider_api_key_ref: Mapped[str | None] = mapped_column(KeyText(512))
    provider_api_key_secret_name: Mapped[str | None] = mapped_column(payload_text())
    tracing_enabled: Mapped[bool | None]
    tracing_endpoint: Mapped[str | None] = mapped_column(payload_text())
    #: Header names to values, sent to the collector as given.
    tracing_headers: Mapped[dict[str, Any] | None] = mapped_column(payload_json())
    tracing_auth_token_ref: Mapped[str | None] = mapped_column(KeyText(512))
    tracing_auth_token_secret_name: Mapped[str | None] = mapped_column(payload_text())
    tracing_environment: Mapped[str | None] = mapped_column(payload_text())
    tracing_signals: Mapped[list[str] | None] = mapped_column(StringList())
    tracing_log_user_prompt: Mapped[bool | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_by: Mapped[str | None] = mapped_column(payload_text())
