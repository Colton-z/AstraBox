"""Deployments: the webhook, schedule and channel bindings of Agents and Assistants.

A scheduled Deployment's Runs are kept by the selected scheduler provider
(:mod:`astrabox.seams.scheduler`), not here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, func, text
from sqlalchemy.orm import Mapped, mapped_column

from astrabox.persistence.sql.base import Base
from astrabox.persistence.sql.schema import (
    MYSQL_TABLE_OPTIONS,
    TextDefault,
    boolean_checks,
    string_list_check,
)
from astrabox.persistence.sql.types import KeyText, StringList, payload_json, payload_text


class Deployment(Base):
    """A Deployment: a webhook, schedule or channel binding of an Agent or Assistant.

    A Deployment names exactly one target, ``agent_id`` or ``assistant_id``,
    and deleting that target deletes it. A personal schedule (``scope =
    'personal'``) is an Agent schedule an account created from one of its
    Assistants: it runs as that account and is deleted with that Assistant
    (``source_assistant_id``). Deleting a Deployment deletes every channel
    record of the binding (:mod:`astrabox.persistence.sql.models.channels`).

    ``updated_at`` changes on every configuration write and nowhere else, so
    it is the binding's revision: retained channel context and the channel
    source host key on it. Advancing ``source_cursor`` leaves it unchanged.
    """

    __tablename__ = "deployments"
    __table_args__ = (
        CheckConstraint(
            "(agent_id IS NULL) <> (assistant_id IS NULL)", name="one_target"
        ),
        CheckConstraint(
            "scope IS NULL OR (scope = 'personal' AND agent_id IS NOT NULL "
            "AND owner_user_id IS NOT NULL AND owner_org_id IS NOT NULL "
            "AND source_assistant_id IS NOT NULL)",
            name="personal_owner",
        ),
        CheckConstraint(
            "(schedule_cron IS NULL) = (schedule_timezone IS NULL)", name="schedule_pair"
        ),
        Index(None, "agent_id", "created_at"),
        Index(None, "assistant_id", "created_at"),
        Index(
            None,
            "source_assistant_id",
            postgresql_where=text("source_assistant_id IS NOT NULL"),
        ),
        Index(
            "ix_deployments_personal_owner",
            "owner_user_id",
            "owner_org_id",
            "created_at",
            postgresql_where=text("scope = 'personal'"),
        ),
        Index(
            "ix_deployments_schedules",
            "deployment_id",
            postgresql_where=text("scene = 'schedule'"),
        ),
        Index(
            "ix_deployments_channel_bindings",
            "deployment_id",
            postgresql_where=text("enabled AND scene LIKE 'channel:%'"),
        ),
        *boolean_checks("enabled", "credentials_configured"),
        string_list_check("allowed_participants"),
        MYSQL_TABLE_OPTIONS,
    )

    deployment_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    agent_id: Mapped[str | None] = mapped_column(
        KeyText(128), ForeignKey("agents.agent_id", ondelete="CASCADE")
    )
    assistant_id: Mapped[str | None] = mapped_column(
        KeyText(128), ForeignKey("assistants.assistant_id", ondelete="CASCADE")
    )
    created_by: Mapped[str] = mapped_column(payload_text())
    enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    #: ``hmac``, ``scheduler``, ``schedule`` or ``channel:<provider>``.
    scene: Mapped[str] = mapped_column(payload_text())
    prompt_prefix: Mapped[str] = mapped_column(payload_text(), server_default=TextDefault(""))
    scope: Mapped[str | None] = mapped_column(payload_text())
    owner_user_id: Mapped[str | None] = mapped_column(KeyText(512))
    owner_org_id: Mapped[str | None] = mapped_column(KeyText(512))
    source_assistant_id: Mapped[str | None] = mapped_column(
        KeyText(128), ForeignKey("assistants.assistant_id", ondelete="CASCADE")
    )
    #: A scheduled Deployment's name and its five-field cron in an IANA zone.
    name: Mapped[str | None] = mapped_column(payload_text())
    schedule_cron: Mapped[str | None] = mapped_column(payload_text())
    schedule_timezone: Mapped[str | None] = mapped_column(payload_text())
    #: The trigger secret a calling system signs or authenticates with.
    secret: Mapped[str | None] = mapped_column(payload_text())
    attention_policy: Mapped[str | None] = mapped_column(payload_text())
    response_instruction: Mapped[str | None] = mapped_column(payload_text())
    execution_mode: Mapped[str | None] = mapped_column(payload_text())
    execution_user_id: Mapped[str | None] = mapped_column(payload_text())
    allowed_participants: Mapped[list[str] | None] = mapped_column(StringList())
    #: The provider's own configuration, validated by the provider.
    channel_config: Mapped[dict[str, Any] | None] = mapped_column(payload_json())
    #: Set once platform credentials are sealed in the secret store under
    #: ``deployment/<deployment_id>``; the row never holds them.
    credentials_configured: Mapped[bool | None]
    callback_base_url: Mapped[str | None] = mapped_column(payload_text())
    #: The last source position a channel source acknowledged durably.
    source_cursor: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
