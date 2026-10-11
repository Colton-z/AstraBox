"""Deployment management and invocation through the Session kernel.

Deployments are trigger bindings on an Agent or an Assistant. Built-in
schedules create durable Runs through the deployment's selected scheduler
provider (:mod:`astrabox.seams.scheduler`); external systems can instead call
an authenticated webhook or a registered channel provider. Every path starts a
normal Session on the binding's target through
:class:`~astrabox.core.service.orchestrator.conversation_targets.ConversationTargets`
and uses the existing turn authority.

Identity: a binding's conversations run as its target's owner (an Agent's
creator, an Assistant's owner). Channel bindings on an Agent can delegate to an
administrator-configured execution account; an Assistant is owner-only, so its
channel bindings admit only the senders listed in ``allowed_participants``. Auth on the inbound endpoint is the
webhook signature itself (HMAC scene: HMAC-SHA256(secret, timestamp); scheduler:
the deployment's issued secret), not a login session.

**Channel scenes** (``scene = "channel:<provider>"``) share the binding rows,
CRUD, and trigger endpoint, but their spine lives in
:mod:`~astrabox.core.service.orchestrator.channel_ingress_service`: this
service is only the HTTP adapter — it authenticates the callback through the
registered :class:`~astrabox.seams.channel.ChannelProvider` and hands the
typed inbound to ``ChannelIngressService.ingest`` (the single post-auth
ingress every channel source converges on; docs/channel-spine.md).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrabox.persistence.repository import (
    DeploymentRepository,
    SessionEventRepository,
    SessionSnapshotRepository,
)
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.agent_access import is_platform_admin
from astrabox.core.service.orchestrator.channel_ingress_service import (
    VALID_ATTENTION_POLICIES,
    ChannelIngressService,
    binding_execution_mode,
)
from astrabox.core.service.orchestrator.channel_credentials import (
    ChannelCredentialService,
)
from astrabox.core.service.orchestrator.conversation_targets import (
    TARGET_AGENT,
    ConversationTarget,
    ConversationTargets,
    target_fields,
    target_ref,
)
from astrabox.core.service.orchestrator.deployment_scheduling import (
    run_payload,
    schedule_definition,
)
from astrabox.seams.scheduler import (
    InvalidSchedule,
    RunPermanentError,
    RunRecord,
    ScheduleCapacityExceeded,
    SchedulerNotStarted,
    SchedulerProvider,
    SchedulerUnavailable,
    configured_scheduler,
)
from astrabox.seams.channel import (
    EXECUTION_ACCOUNT,
    EXECUTION_MODES,
    EXECUTION_OWNER,
    ChannelCallbackResponse,
    channel_if_registered,
    channel_scene_name,
    get_channel,
)

logger = get_logger(__name__)

SCENE_HMAC = "hmac"
SCENE_SCHEDULER = "scheduler"
SCENE_SCHEDULE = "schedule"
_VALID_SCENES = (SCENE_HMAC, SCENE_SCHEDULER, SCENE_SCHEDULE)


def _hmac_window_seconds() -> int:
    """Freshness window for the HMAC scene (default 300s = 5 min)."""
    try:
        return max(1, int(os.getenv("ASTRABOX_WEBHOOK_HMAC_WINDOW_SECONDS", "300")))
    except ValueError:
        return 300


def _hmac_signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    """HMAC webhook signature: Base64(HMAC-SHA256(secret, "timestamp.sha256(body)")).

    The signature binds both the request timestamp and the exact body, so a
    captured (timestamp, signature) pair cannot be replayed with a different
    body — critical here because the webhook body becomes the prompt fed to the
    agent (replay + prompt-injection). Combined with the freshness-window check
    in :meth:`DeploymentService._verify_signature`, a captured pair is unusable
    once the window lapses.
    """
    body_digest = hashlib.sha256(raw_body or b"").hexdigest()
    signed = f"{timestamp}.{body_digest}"
    mac = hmac.new(secret.encode("utf-8"), signed.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


# How long to wait for a freshly-created session's sandbox to become READY
# before sending the first message. Webhook is non-interactive: unlike the MCP
# flow (create_conversation then a later send_message), there is no external
# client to retry, so this waits for readiness here before sending.
_SESSION_READY_TIMEOUT_SECONDS = 120
_SESSION_READY_POLL_SECONDS = 2.0

#: ``scheduler=`` default: resolve the deployment's selected provider per call.
_SELECTED_SCHEDULER: Any = object()


class DeploymentService:
    def __init__(
        self,
        *,
        deployment_repo: DeploymentRepository,
        agent_repo: Any,
        agent_service_getter: Any,
        stream_message_events_ds: Any,
        dispatch_turn_input: Any,
        sessions_repo: Any,
        spawn_background_task: Any,
        agent_config: Any,
        channel_ingress: ChannelIngressService,
        channel_credentials: ChannelCredentialService | None = None,
        scheduler: SchedulerProvider | None | Any = _SELECTED_SCHEDULER,
        session_snapshots_repo: Any | None = None,
        session_events_repo: Any | None = None,
        assistant_service_getter: Any = None,
    ) -> None:
        self._deployment_repo = deployment_repo
        self._agent_repo = agent_repo
        self._agent_service_getter = agent_service_getter
        self._targets = ConversationTargets(
            agent_repo=agent_repo,
            agent_service_getter=agent_service_getter,
            assistant_service_getter=assistant_service_getter,
        )
        self._agent_config = agent_config
        # platform_service.stream_message_events_ds (bound method)
        self._stream_message_events_ds = stream_message_events_ds
        # platform_service.dispatch_turn_input (bound method). Scheduled Runs
        # need the durable admission receipt, not the turn's output stream.
        self._dispatch_turn_input = dispatch_turn_input
        self._sessions_repo = sessions_repo
        # Tests pass a provider (or ``None`` for disabled scheduling); the
        # server resolves the deployment's selection on each call.
        self._scheduler_override = scheduler
        self._session_snapshots_repo = (
            session_snapshots_repo
            if session_snapshots_repo is not None
            else SessionSnapshotRepository()
        )
        self._session_events_repo = (
            session_events_repo
            if session_events_repo is not None
            else SessionEventRepository()
        )
        # The channel spine (durable work items, dispatch, delivery).
        self._channel_ingress = channel_ingress
        self._channel_credentials = channel_credentials or ChannelCredentialService()
        # platform_service._spawn_background_task: detaches from the request task
        # scope (fresh contextvars + strong ref) so the deferred turn survives
        # after the webhook's HTTP response returns. Raw asyncio.ensure_future
        # would be torn down with the request task.
        self._spawn_background_task = spawn_background_task

    # ── scheduler provider access ────────────────────────────────────────────

    def _scheduler(self) -> SchedulerProvider | None:
        """The deployment's scheduler provider; ``None`` when scheduling is disabled."""

        if self._scheduler_override is not _SELECTED_SCHEDULER:
            return self._scheduler_override
        try:
            return configured_scheduler()
        except SchedulerNotStarted as exc:
            raise APIError(
                code="SCHEDULE_UNAVAILABLE", message=str(exc), status_code=409
            ) from exc

    def _require_scheduler(self) -> SchedulerProvider:
        provider = self._scheduler()
        if provider is None:
            raise APIError(
                code="SCHEDULE_UNAVAILABLE",
                message=(
                    "scheduled Deployments are disabled in this deployment "
                    "(ASTRABOX_SCHEDULER_PROVIDER=none)"
                ),
                status_code=409,
            )
        return provider

    @staticmethod
    @contextlib.asynccontextmanager
    async def _scheduling() -> AsyncIterator[None]:
        """Answer a scheduler provider's refusal with the public schedule errors.

        The provider's own vocabulary and error text stay out of the response.
        """

        try:
            yield
        except InvalidSchedule as exc:
            raise APIError(
                code="INVALID_SCHEDULE",
                message="schedule cron or timezone is invalid",
                status_code=400,
            ) from exc
        except ScheduleCapacityExceeded as exc:
            raise APIError(
                code="SCHEDULE_CAPACITY_EXCEEDED",
                message=(
                    f"this deployment's scheduler supports at most {exc.limit} "
                    "active schedules"
                ),
                status_code=409,
            ) from exc
        except SchedulerNotStarted as exc:
            raise APIError(
                code="SCHEDULE_UNAVAILABLE", message=str(exc), status_code=409
            ) from exc
        except SchedulerUnavailable as exc:
            raise APIError(
                code="SCHEDULE_EXECUTION_UNAVAILABLE",
                message="the schedule execution store is unavailable",
                status_code=503,
            ) from exc

    async def _apply_schedule(self, deployment: dict[str, Any]) -> None:
        """Project a Deployment's schedule into the scheduler provider.

        With scheduling disabled a disabled schedule needs no projection, so it
        can still be edited; enabling one is refused.
        """

        definition = schedule_definition(deployment)
        scheduler = self._scheduler()
        if scheduler is None:
            if definition.enabled:
                self._require_scheduler()
            return
        async with self._scheduling():
            await scheduler.apply(definition)

    async def _delete_schedule(self, deployment_id: str) -> None:
        scheduler = self._scheduler()
        if scheduler is None:
            return
        async with self._scheduling():
            await scheduler.delete(deployment_id)

    # ── Management (called by authenticated admin routes) ────────────────────

    @staticmethod
    def _can_manage_agent(user: UserContext, agent: dict[str, Any] | None) -> bool:
        owner_id = str((agent or {}).get("user_id") or "").strip()
        return bool(agent) and (
            is_platform_admin(getattr(user, "roles", []))
            or bool(owner_id and owner_id == user.user_id)
        )

    async def assert_can_manage_agent(self, user: UserContext, agent_id: str) -> dict[str, Any]:
        """Resolve an agent and assert the caller may manage its bindings.

        An agent is an Owner resource (domain-model.md §1): its trigger
        bindings are managed by the agent's owner, or by an instance admin.
        Authorization is anchored on the Agent's own owner rather than on any
        referenced runtime configuration.
        Missing and foreign agents share one non-disclosing 404.
        """
        agent = await self._agent_repo.get_agent(str(agent_id or "").strip())
        if not self._can_manage_agent(user, agent):
            raise APIError(code="NOT_FOUND", message="agent not found", status_code=404)
        return agent

    async def list_for_agent(self, agent_id: str) -> list[dict[str, Any]]:
        rows = await self._deployment_repo.list_by_agent(agent_id)
        return [self._sanitize(row) for row in rows]

    async def assert_can_manage_assistant(
        self, user: UserContext, assistant_id: str
    ) -> ConversationTarget:
        """Resolve an Assistant whose bindings the caller may manage.

        An Assistant is owner-only (domain-model.md §2): only its owner manages
        its bindings, administrators included. Missing and foreign Assistants
        share one non-disclosing 404.
        """
        target = await self._targets.resolve(
            {"assistant_id": str(assistant_id or "").strip()}
        )
        if target is None or not target.owner_user_id or target.owner_user_id != user.user_id:
            raise APIError(code="NOT_FOUND", message="assistant not found", status_code=404)
        return target

    async def list_for_assistant(self, assistant_id: str) -> list[dict[str, Any]]:
        rows = await self._deployment_repo.list_by_assistant(assistant_id)
        return [self._sanitize(row) for row in rows]

    async def delete_for_assistant(self, assistant_id: str) -> None:
        """Delete every binding of a deleted Assistant, schedules included."""
        for row in await self._deployment_repo.list_by_assistant(assistant_id):
            with contextlib.suppress(APIError):
                await self.delete(
                    str(row.get("deployment_id") or ""), assistant_id=assistant_id
                )

    @staticmethod
    def _validate_allowed_participants(
        value: Any, *, is_channel: bool, required: bool
    ) -> list[str]:
        """Normalize the external sender identities a channel binding admits."""
        if value is None:
            value = []
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise APIError(
                code="INVALID_REQUEST",
                message="allowed_participants must be a list of strings",
                status_code=400,
            )
        participants = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if participants and not is_channel:
            raise APIError(
                code="INVALID_REQUEST",
                message="allowed_participants applies only to channel scenes",
                status_code=400,
            )
        if not all(item.isprintable() for item in participants):
            raise APIError(
                code="INVALID_REQUEST",
                message="allowed_participants contains control characters",
                status_code=400,
            )
        if required and not participants:
            raise APIError(
                code="INVALID_REQUEST",
                message=(
                    "a channel binding on an Assistant must list its owner's "
                    "account in allowed_participants"
                ),
                status_code=400,
            )
        return participants

    @staticmethod
    def _validate_execution_identity(
        mode: Any,
        user_id: Any,
        *,
        provider: Any,
        scene: str,
        on_assistant: bool,
        actor: UserContext | None,
        previous: tuple[str, str] = (EXECUTION_OWNER, ""),
    ) -> tuple[str, str]:
        """Who a channel binding's messages run as: ``(execution_mode, execution_user_id)``.

        ``owner`` runs them as the target's owner, ``account`` as the one
        account in ``execution_user_id``, and ``linked`` as each
        participant's linked account. The mode must be one the provider
        declares, so a binding of a provider whose work belongs to a person
        cannot run as the owner. Only an administrator may choose anything
        but the owner or change the choice, and an Assistant's conversations
        always run as its owner.
        """
        if mode is not None and not isinstance(mode, str):
            raise APIError(
                code="INVALID_REQUEST", message="execution_mode must be a string",
                status_code=400,
            )
        if user_id is not None and not isinstance(user_id, str):
            raise APIError(
                code="INVALID_REQUEST", message="execution_user_id must be a string",
                status_code=400,
            )
        user = str(user_id or "").strip()
        if user and not user.isprintable():
            raise APIError(
                code="INVALID_REQUEST", message="execution_user_id contains control characters",
                status_code=400,
            )
        chosen = str(mode or "").strip() or EXECUTION_OWNER
        if chosen not in EXECUTION_MODES:
            raise APIError(
                code="INVALID_REQUEST",
                message=f"execution_mode must be one of {list(EXECUTION_MODES)}",
                status_code=400,
            )
        supported = provider.describe().execution_modes
        if chosen not in supported:
            raise APIError(
                code="INVALID_REQUEST",
                message=f"{scene!r} runs its messages only as {list(supported)}",
                status_code=400,
            )
        if on_assistant and chosen != EXECUTION_OWNER:
            raise APIError(
                code="INVALID_REQUEST",
                message="an Assistant's conversations always run as its owner",
                status_code=400,
            )
        if chosen == EXECUTION_ACCOUNT and not user:
            raise APIError(
                code="INVALID_REQUEST",
                message="execution_mode 'account' runs as execution_user_id, which is empty",
                status_code=400,
            )
        if chosen != EXECUTION_ACCOUNT and user:
            raise APIError(
                code="INVALID_REQUEST",
                message="execution_user_id applies only to execution_mode 'account'",
                status_code=400,
            )
        if (chosen, user) != previous and not is_platform_admin(getattr(actor, "roles", [])):
            raise APIError(
                code="FORBIDDEN",
                message="only an administrator may change who a channel's messages run as",
                status_code=403,
            )
        return chosen, user

    async def list_manageable(self, user: UserContext) -> list[dict[str, Any]]:
        """List the caller's Deployments without one request per Agent."""

        rows = await self._deployment_repo.list_active()
        agent_ids = [
            str(row.get("agent_id") or "").strip()
            for row in rows
            if str(row.get("agent_id") or "").strip()
        ]
        agents = await self._agent_repo.list_agents_by_ids(agent_ids)
        assistants: dict[str, ConversationTarget | None] = {}
        visible: list[dict[str, Any]] = []
        for row in rows:
            ref = target_ref(row)
            if ref is None:
                continue
            kind, target_id = ref
            rendered = self._sanitize(row)
            if kind == TARGET_AGENT:
                agent = agents.get(target_id)
                if not self._can_manage_agent(user, agent):
                    continue
                rendered["agent_name"] = str((agent or {}).get("name") or target_id)
            else:
                if target_id not in assistants:
                    assistants[target_id] = await self._targets.resolve(row)
                assistant = assistants[target_id]
                if assistant is None or assistant.owner_user_id != user.user_id:
                    continue
                rendered["assistant_name"] = assistant.name
            visible.append(rendered)
        return visible

    async def create(
        self,
        *,
        agent_id: str = "",
        assistant_id: str = "",
        creator_user_id: str,
        scene: str,
        name: str = "",
        prompt_prefix: str = "",
        secret: str | None = None,
        attention_policy: str | None = None,
        channel_config: Any = None,
        credentials: Any = None,
        callback_base_url: str | None = None,
        schedule: Any = None,
        execution_user_id: Any = None,
        execution_mode: Any = None,
        allowed_participants: Any = None,
        actor: UserContext | None = None,
    ) -> dict[str, Any]:
        target = target_ref({"agent_id": agent_id, "assistant_id": assistant_id})
        if target is None:
            raise APIError(
                code="INVALID_REQUEST",
                message="a Deployment names exactly one Agent or Assistant",
                status_code=400,
            )
        on_assistant = target[0] != TARGET_AGENT
        scene = str(scene or "").strip()
        channel_name = channel_scene_name(scene)
        channel_provider = None
        if channel_name is not None:
            # Capability-gate the API surface (mirrors the vault env-credential
            # gate): a channel binding whose provider is not installed could
            # never trigger — fail at write time with the reason, rather than
            # at callback time.
            channel_provider = channel_if_registered(channel_name)
            if channel_provider is None:
                raise APIError(
                    code="CHANNEL_PROVIDER_UNKNOWN",
                    message=(
                        f"no channel provider named {channel_name!r} is registered; "
                        f"install its distribution (entry-point group "
                        f"'astrabox.providers.channel') before binding scene={scene!r}"
                    ),
                    status_code=400,
                )
        elif scene not in _VALID_SCENES:
            raise APIError(
                code="INVALID_REQUEST",
                message=(
                    f"scene must be one of {list(_VALID_SCENES)} or 'channel:<provider>'"
                ),
                status_code=400,
            )
        elif channel_config is not None:
            raise APIError(
                code="INVALID_REQUEST",
                message="channel_config applies only to channel scenes",
                status_code=400,
            )
        elif credentials is not None:
            raise APIError(
                code="INVALID_REQUEST",
                message="credentials applies only to channel scenes",
                status_code=400,
            )
        resolved_policy = self._validate_attention_policy(
            attention_policy, is_channel=channel_name is not None
        )
        if channel_provider is not None:
            mode, execution_user = self._validate_execution_identity(
                execution_mode, execution_user_id, provider=channel_provider, scene=scene,
                on_assistant=on_assistant, actor=actor,
            )
        elif execution_mode is not None or execution_user_id is not None:
            raise APIError(
                code="INVALID_REQUEST",
                message="execution_mode and execution_user_id apply only to channel scenes",
                status_code=400,
            )
        participants = self._validate_allowed_participants(
            allowed_participants,
            is_channel=channel_name is not None,
            required=on_assistant and channel_name is not None,
        )
        deployment_id = uuid.uuid4().hex
        doc = {
            "deployment_id": deployment_id,
            **target_fields(*target),
            "created_by": str(creator_user_id or "").strip(),
            "enabled": True,
            "deleted": False,
            "scene": scene,
            "prompt_prefix": str(prompt_prefix or ""),
            "created_at": utcnow_iso(),
            "updated_at": utcnow_iso(),
        }
        normalized_credentials: dict[str, Any] | None = None
        if channel_provider is not None:
            doc["execution_mode"] = mode
            doc["execution_user_id"] = execution_user
            doc["allowed_participants"] = participants
            descriptor = channel_provider.describe()
            if descriptor.credential_fields and str(secret or "").strip():
                raise APIError(
                    code="CHANNEL_CREDENTIALS_INVALID",
                    message=(
                        f"channel {channel_name!r} uses named platform credentials, "
                        "not a trigger secret"
                    ),
                    status_code=400,
                )
            normalized_config, validated_credentials = (
                await self._validate_channel_configuration(
                    channel_provider,
                    config=channel_config,
                    credentials=credentials,
                )
            )
            doc["channel_config"] = normalized_config
            if descriptor.credential_fields:
                normalized_credentials = validated_credentials
                doc["credentials_configured"] = True
            if descriptor.callback_path:
                callback_origin = str(callback_base_url or "").strip().rstrip("/")
                if not callback_origin:
                    raise APIError(
                        code="CHANNEL_CONFIG_INVALID",
                        message=(
                            f"channel {channel_name!r} requires the public AstraBox "
                            "request origin to construct its callback URL"
                        ),
                        status_code=400,
                    )
                doc["callback_base_url"] = (
                    f"{callback_origin}/api/v1/deployments/{deployment_id}/callback"
                )
        if scene == SCENE_SCHEDULE:
            schedule_name = str(name or "").strip()
            if not schedule_name:
                raise APIError(
                    code="INVALID_SCHEDULE",
                    message="scheduled Deployment name is required",
                    status_code=400,
                )
            if not str(doc["prompt_prefix"]).strip():
                raise APIError(
                    code="INVALID_SCHEDULE",
                    message="scheduled Deployment prompt_prefix is required",
                    status_code=400,
                )
            if str(secret or "").strip():
                raise APIError(
                    code="INVALID_SCHEDULE",
                    message="scheduled Deployments do not use a secret",
                    status_code=400,
                )
            doc["name"] = schedule_name
            doc["schedule"] = self._normalize_schedule(schedule)
            # Refuse before any write when this deployment runs no scheduler.
            self._require_scheduler()
            # The repository is the desired-state authority. Write it first so
            # any invocation created during projection sees committed config;
            # startup reconciliation repairs a crash between these two writes.
            stored = await self._deployment_repo.upsert(deployment_id, doc)
            try:
                await self._apply_schedule(doc)
                return self._sanitize(stored)
            except Exception:
                # The provider restores its prior schedule before raising. A
                # fresh random ID therefore has no schedule to delete; only the
                # desired-state write needs to be rolled back here.
                cleanup_error: Exception | None = None
                try:
                    await self._deployment_repo.soft_delete_for_deployment(
                        deployment_id, str(agent_id).strip(),
                        assistant_id=str(assistant_id).strip(),
                    )
                except Exception as exc:
                    cleanup_error = cleanup_error or exc
                    logger.exception(
                        "could not remove rejected scheduled Deployment %s",
                        deployment_id,
                    )
                if cleanup_error is not None:
                    raise APIError(
                        code="SCHEDULE_EXECUTION_UNAVAILABLE",
                        message="the rejected schedule could not be rolled back",
                        status_code=503,
                    ) from cleanup_error
                raise
        if str(name or "").strip():
            raise APIError(
                code="INVALID_REQUEST",
                message="name applies only to scene='schedule'",
                status_code=400,
            )
        if schedule is not None:
            raise APIError(
                code="INVALID_SCHEDULE",
                message="schedule applies only to scene='schedule'",
                status_code=400,
            )
        if channel_provider is not None:
            if channel_provider.uses_trigger_secret:
                doc["secret"] = str(secret or "").strip() or uuid.uuid4().hex[:16]
        else:
            doc["secret"] = str(secret or "").strip() or uuid.uuid4().hex[:16]
        if resolved_policy is not None:
            doc["attention_policy"] = resolved_policy
        if normalized_credentials is not None:
            await self._channel_credentials.put(deployment_id, normalized_credentials)
        try:
            stored = await self._deployment_repo.upsert(deployment_id, doc)
        except Exception:
            if normalized_credentials is not None:
                with contextlib.suppress(Exception):
                    await self._channel_credentials.purge(deployment_id)
            raise
        return self._sanitize(stored, include_secret=True)

    async def update(
        self,
        deployment_id: str,
        *,
        agent_id: str = "",
        assistant_id: str = "",
        patch: dict[str, Any],
        actor: UserContext | None = None,
    ) -> dict[str, Any]:
        # Resolve visibility before validating the patch so missing, deleted and
        # foreign ids remain indistinguishable.  The repository repeats the
        # same scope/deleted predicates on the eventual atomic write, closing
        # the read -> mutation race.
        existing = await self._get_for_deployment(
            deployment_id, agent_id, assistant_id=assistant_id
        )
        allowed = {
            "enabled",
            "prompt_prefix",
            "scene",
            "attention_policy",
            "channel_config",
            "credentials",
            "name",
            "schedule",
            "execution_user_id",
            "execution_mode",
            "allowed_participants",
        }
        updates = {k: v for k, v in patch.items() if k in allowed}
        existing_scene = str(existing.get("scene") or "")
        if "allowed_participants" in updates:
            updates["allowed_participants"] = self._validate_allowed_participants(
                updates["allowed_participants"],
                is_channel=channel_scene_name(existing_scene) is not None,
                required=bool(assistant_id)
                and channel_scene_name(existing_scene) is not None,
            )
        if "execution_mode" in updates or "execution_user_id" in updates:
            existing_channel = channel_scene_name(existing_scene)
            if existing_channel is None:
                raise APIError(
                    code="INVALID_REQUEST",
                    message="execution_mode and execution_user_id apply only to channel scenes",
                    status_code=400,
                )
            for key in ("execution_mode", "execution_user_id"):
                if key in updates and not isinstance(updates[key], str):
                    raise APIError(
                        code="INVALID_REQUEST", message=f"{key} must be a string",
                        status_code=400,
                    )
            previous_mode = binding_execution_mode(existing)
            previous_user = (
                str(existing.get("execution_user_id") or "").strip()
                if previous_mode == EXECUTION_ACCOUNT else ""
            )
            provider = channel_if_registered(existing_channel)
            if provider is None:
                raise APIError(
                    code="CHANNEL_PROVIDER_UNKNOWN",
                    message=f"no channel provider named {existing_channel!r} is registered",
                    status_code=400,
                )
            mode_value = updates.get("execution_mode", previous_mode)
            mode, user = self._validate_execution_identity(
                mode_value,
                updates.get(
                    "execution_user_id",
                    previous_user if mode_value == EXECUTION_ACCOUNT else "",
                ),
                provider=provider, scene=existing_scene,
                on_assistant=bool(assistant_id), actor=actor,
                previous=(previous_mode, previous_user),
            )
            updates["execution_mode"] = mode
            updates["execution_user_id"] = user
        if "scene" in updates:
            next_scene = str(updates["scene"] or "").strip()
            if next_scene != existing_scene and SCENE_SCHEDULE in {
                existing_scene,
                next_scene,
            }:
                raise APIError(
                    code="INVALID_REQUEST",
                    message="a Deployment cannot change to or from a schedule",
                    status_code=400,
                )
            existing_channel = channel_scene_name(existing_scene)
            next_channel = channel_scene_name(next_scene)
            if next_scene != existing_scene and (
                existing_channel is not None or next_channel is not None
            ):
                raise APIError(
                    code="INVALID_REQUEST",
                    message="a Deployment cannot change to or from a channel scene",
                    status_code=400,
                )
            if next_channel is not None:
                if channel_if_registered(next_channel) is None:
                    raise APIError(
                        code="CHANNEL_PROVIDER_UNKNOWN",
                        message=f"no channel provider named {next_channel!r} is registered",
                        status_code=400,
                    )
            elif next_scene not in _VALID_SCENES:
                raise APIError(
                    code="INVALID_REQUEST",
                    message=f"scene must be one of {list(_VALID_SCENES)}",
                    status_code=400,
                )
            updates["scene"] = next_scene
        if "attention_policy" in updates:
            is_channel = channel_scene_name(str(existing.get("scene") or "")) is not None
            resolved_policy = self._validate_attention_policy(
                updates["attention_policy"], is_channel=is_channel
            )
            if resolved_policy is None:
                updates.pop("attention_policy")
            else:
                updates["attention_policy"] = resolved_policy
        if existing_scene == SCENE_SCHEDULE:
            updates.pop("attention_policy", None)
            if "channel_config" in updates or "credentials" in updates:
                raise APIError(
                    code="INVALID_REQUEST",
                    message=(
                        "channel_config and credentials apply only to channel scenes"
                    ),
                    status_code=400,
                )
            name = str(updates.get("name", existing.get("name")) or "").strip()
            prompt = str(
                updates.get("prompt_prefix", existing.get("prompt_prefix")) or ""
            )
            if not name:
                raise APIError(
                    code="INVALID_SCHEDULE",
                    message="scheduled Deployment name is required",
                    status_code=400,
                )
            if not prompt.strip():
                raise APIError(
                    code="INVALID_SCHEDULE",
                    message="scheduled Deployment prompt_prefix is required",
                    status_code=400,
                )
            updates["name"] = name
            updates["prompt_prefix"] = prompt
            if "schedule" in updates:
                updates["schedule"] = self._normalize_schedule(updates["schedule"])
            candidate = {**existing, **updates, "updated_at": utcnow_iso()}
            # The scheduler provider validates and changes its schedule first.
            # If the guarded product write then fails or loses a delete race,
            # restore the old schedule; startup reconciliation is the
            # crash-safe backstop.
            await self._apply_schedule(candidate)
            try:
                updated = await self._deployment_repo.update_for_deployment(
                    deployment_id,
                    agent_id,
                    {**updates, "updated_at": candidate["updated_at"]},
                    assistant_id=assistant_id,
                )
            except Exception:
                await self._restore_schedule_projection(existing)
                raise
            if updated is None:
                # The row disappeared after the visibility read. Its desired
                # state is deletion, so leaving either old or candidate
                # schedule would create an orphan invocation source.
                await self._delete_schedule(deployment_id)
                self._raise_deployment_not_found()
            return self._sanitize(updated)
        updates.pop("name", None)
        if "schedule" in updates:
            raise APIError(
                code="INVALID_SCHEDULE",
                message="schedule applies only to scene='schedule'",
                status_code=400,
            )
        existing_channel = channel_scene_name(existing_scene)
        normalized_credentials: dict[str, Any] | None = None
        prior_credentials: dict[str, Any] | None = None
        if existing_channel is not None:
            provider = get_channel(existing_channel)
            descriptor = provider.describe()
            changes_config = "channel_config" in updates
            changes_credentials = "credentials" in updates
            if changes_config or changes_credentials:
                if descriptor.credential_fields:
                    prior_credentials = await self._channel_credentials.get(
                        deployment_id
                    )
                    if prior_credentials is None and not changes_credentials:
                        raise APIError(
                            code="CHANNEL_CREDENTIALS_UNAVAILABLE",
                            message=(
                                f"channel {existing_channel!r} has no stored platform "
                                "credentials; replace them before changing its config"
                            ),
                            status_code=503,
                        )
                raw_credentials = updates.pop(
                    "credentials", prior_credentials if prior_credentials is not None else {}
                )
                normalized_config, validated_credentials = (
                    await self._validate_channel_configuration(
                        provider,
                        config=updates.get(
                            "channel_config", existing.get("channel_config") or {}
                        ),
                        credentials=raw_credentials,
                    )
                )
                if changes_config:
                    updates["channel_config"] = normalized_config
                if changes_credentials and descriptor.credential_fields:
                    normalized_credentials = validated_credentials
                elif changes_credentials:
                    raise APIError(
                        code="CHANNEL_CREDENTIALS_INVALID",
                        message=f"channel {existing_channel!r} has no named credentials",
                        status_code=400,
                    )
            if normalized_credentials is not None:
                await self._channel_credentials.put(
                    deployment_id, normalized_credentials
                )
                updates["credentials_configured"] = True
        elif "channel_config" in updates or "credentials" in updates:
            raise APIError(
                code="INVALID_REQUEST",
                message="channel_config and credentials apply only to channel scenes",
                status_code=400,
            )
        try:
            updated = await self._deployment_repo.update_for_deployment(
                deployment_id,
                agent_id,
                {**updates, "updated_at": utcnow_iso()},
                assistant_id=assistant_id,
            )
        except Exception:
            if normalized_credentials is not None:
                await self._restore_channel_credentials(
                    deployment_id, prior_credentials
                )
            raise
        if updated is None:
            # It was deleted between the visibility read and guarded write.
            if normalized_credentials is not None:
                await self._restore_channel_credentials(
                    deployment_id, prior_credentials
                )
            self._raise_deployment_not_found()
        return self._sanitize(updated)

    @staticmethod
    def _normalize_schedule(schedule: Any) -> dict[str, str]:
        if not isinstance(schedule, dict):
            raise APIError(
                code="INVALID_SCHEDULE",
                message="schedule must be an object",
                status_code=400,
            )
        cron = " ".join(str(schedule.get("cron") or "").split())
        timezone_name = str(schedule.get("timezone") or "").strip()
        if len(cron.split()) != 5:
            raise APIError(
                code="INVALID_SCHEDULE",
                message="schedule cron must contain exactly five fields",
                status_code=400,
            )
        if not timezone_name:
            raise APIError(
                code="INVALID_SCHEDULE",
                message="schedule timezone is required",
                status_code=400,
            )
        # The timezone's validity is the product's rule, not one scheduler
        # engine's: every provider receives an IANA name it can resolve.
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise APIError(
                code="INVALID_SCHEDULE",
                message=f"schedule timezone {timezone_name!r} is not an IANA timezone",
                status_code=400,
            ) from exc
        return {"cron": cron, "timezone": timezone_name}

    @staticmethod
    def _validate_attention_policy(
        attention_policy: Any, *, is_channel: bool
    ) -> str | None:
        policy = str(attention_policy or "").strip().lower()
        if not policy:
            return None
        if not is_channel:
            raise APIError(
                code="INVALID_REQUEST",
                message="attention_policy applies only to channel scenes",
                status_code=400,
            )
        if policy not in VALID_ATTENTION_POLICIES:
            raise APIError(
                code="INVALID_REQUEST",
                message=(
                    f"attention_policy must be one of {list(VALID_ATTENTION_POLICIES)}"
                ),
                status_code=400,
            )
        return policy

    @staticmethod
    async def _validate_channel_configuration(
        provider: Any,
        *,
        config: Any,
        credentials: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if config is None:
            raw_config: dict[str, Any] = {}
        elif isinstance(config, dict):
            raw_config = dict(config)
        else:
            raise APIError(
                code="CHANNEL_CONFIG_INVALID",
                message="channel_config must be an object",
                status_code=400,
            )
        if credentials is None:
            raw_credentials: dict[str, Any] = {}
        elif isinstance(credentials, dict):
            raw_credentials = dict(credentials)
        else:
            raise APIError(
                code="CHANNEL_CREDENTIALS_INVALID",
                message="credentials must be an object",
                status_code=400,
            )
        try:
            result = await provider.validate_configuration(
                config=raw_config,
                credentials=raw_credentials,
            )
        except APIError:
            raise
        except (TypeError, ValueError) as exc:
            raise APIError(
                code="CHANNEL_CONFIG_INVALID",
                message=str(exc),
                status_code=400,
            ) from exc
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or not isinstance(result[0], dict)
            or not isinstance(result[1], dict)
        ):
            raise RuntimeError(
                f"channel provider {provider.name!r} validate_configuration must "
                "return (dict, dict)"
            )
        return result

    async def delete(
        self, deployment_id: str, *, agent_id: str = "", assistant_id: str = ""
    ) -> None:
        existing = await self._deployment_repo.get_for_deployment(
            deployment_id, agent_id, assistant_id=assistant_id
        )
        if not existing:
            # Repeat the guarded mutation even for an invisible id. Besides
            # preserving the one atomic repository boundary, this keeps a
            # missing/deleted/foreign id indistinguishable under a race.
            await self._deployment_repo.soft_delete_for_deployment(
                deployment_id, agent_id, assistant_id=assistant_id
            )
            self._raise_deployment_not_found()
        if str(existing.get("scene") or "") == SCENE_SCHEDULE:
            await self._delete_schedule(deployment_id)
        channel_name = channel_scene_name(str(existing.get("scene") or ""))
        prior_credentials: dict[str, Any] | None = None
        if channel_name is not None and get_channel(channel_name).describe().credential_fields:
            prior_credentials = await self._channel_credentials.get(deployment_id)
            await self._channel_credentials.purge(deployment_id)
        try:
            deleted = await self._deployment_repo.soft_delete_for_deployment(
                deployment_id, agent_id, assistant_id=assistant_id
            )
        except Exception:
            if str(existing.get("scene") or "") == SCENE_SCHEDULE:
                await self._restore_schedule_projection(existing)
            if prior_credentials is not None:
                await self._channel_credentials.put(
                    deployment_id, prior_credentials
                )
            raise
        if not deleted:
            if prior_credentials is not None:
                await self._channel_credentials.put(
                    deployment_id, prior_credentials
                )
            self._raise_deployment_not_found()

    async def _restore_channel_credentials(
        self,
        deployment_id: str,
        credentials: dict[str, Any] | None,
    ) -> None:
        if credentials is None:
            await self._channel_credentials.purge(deployment_id)
        else:
            await self._channel_credentials.put(deployment_id, credentials)

    async def _restore_schedule_projection(self, deployment: dict[str, Any]) -> None:
        """Restore a known committed definition after its product write fails."""

        try:
            await self._apply_schedule(deployment)
        except Exception:
            logger.exception(
                "could not restore scheduled Deployment projection %s",
                deployment.get("deployment_id"),
            )
            raise

    async def _get_for_deployment(
        self, deployment_id: str, agent_id: str, *, assistant_id: str = ""
    ) -> dict[str, Any]:
        existing = await self._deployment_repo.get_for_deployment(
            deployment_id, agent_id, assistant_id=assistant_id
        )
        if not existing:
            self._raise_deployment_not_found()
        return existing

    @staticmethod
    def _raise_deployment_not_found() -> None:
        """One non-disclosing management response for every invisible id."""

        raise APIError(code="NOT_FOUND", message="deployment not found", status_code=404)

    # ── Trigger (called by the unauthenticated inbound endpoint) ─────────────

    async def trigger(
        self, deployment_id: str, *, headers: dict[str, str], raw_body: bytes
    ) -> dict[str, Any]:
        deployment = await self._deployment_repo.get_by_id(deployment_id)
        if not deployment or deployment.get("deleted") or deployment.get("enabled") is False:
            raise APIError(code="NOT_FOUND", message="deployment not found or disabled", status_code=404)

        # Channel scenes: the provider authenticates the callback and maps the
        # payload; everything after that is the channel spine behind its single
        # typed ingress (docs/channel-spine.md invariant E).
        channel_name = channel_scene_name(str(deployment.get("scene") or ""))
        if channel_name is not None:
            provider = get_channel(channel_name)
            inbound = provider.verify_and_resolve(
                headers=headers, raw_body=raw_body, binding=deployment
            )
            receipt = await self._channel_ingress.ingest_resolved(deployment, inbound)
            response: dict[str, Any] = {
                "deployment_id": deployment_id,
                "session_id": receipt.session_id,
                "status": receipt.status,
            }
            response.update(receipt.ack_extra)
            return response

        if str(deployment.get("scene") or "") == SCENE_SCHEDULE:
            raise APIError(
                code="NOT_FOUND",
                message="scheduled Deployment has no public trigger endpoint",
                status_code=404,
            )

        # Built-in scenes: verify the webhook signature, forward the payload,
        # start a fresh conversation, drive one turn fire-and-forget.
        self._verify_signature(deployment, headers, raw_body)
        content = self._build_content(deployment, raw_body)
        target = await self._targets.resolve(deployment)
        if target is None:
            raise APIError(code="NOT_FOUND", message="bound agent not found", status_code=404)
        creator_user_id = target.owner_user_id
        if not creator_user_id:
            raise APIError(
                code="DEPLOYMENT_NO_OWNER",
                message="bound agent has no creator to run as",
                status_code=409,
            )
        user = UserContext(user_id=creator_user_id)
        started = await self._targets.start_conversation(target, user)
        session_id = str(started.get("session_id") or "")
        self._spawn_background_task(
            self._fire_when_ready(user, session_id, content, deployment_id),
            name=f"deployment-fire-{deployment_id}",
        )
        logger.warning(
            "deployment trigger accepted: deployment=%s %s=%s creator=%s session=%s scene=%s",
            deployment_id, target.kind, target.target_id, creator_user_id, session_id,
            str(deployment.get("scene") or ""),
        )
        return {
            "deployment_id": deployment_id,
            "session_id": session_id,
            "status": "accepted",
        }

    async def forward_channel_callback(
        self,
        deployment_id: str,
        *,
        method: str,
        path: str,
        query: str,
        headers: dict[str, str],
        raw_body: bytes,
    ) -> ChannelCallbackResponse:
        """Route one official-platform callback to its managed connector."""

        deployment = await self._deployment_repo.get_by_id(deployment_id)
        if (
            not deployment
            or deployment.get("deleted") is True
            or deployment.get("enabled") is False
        ):
            raise APIError(
                code="NOT_FOUND",
                message="deployment not found or disabled",
                status_code=404,
            )
        channel_name = channel_scene_name(str(deployment.get("scene") or ""))
        if channel_name is None:
            raise APIError(
                code="CHANNEL_CALLBACK_NOT_FOUND",
                message="deployment is not a channel callback binding",
                status_code=404,
            )
        provider = get_channel(channel_name)
        if provider.describe().callback_path is None:
            raise APIError(
                code="CHANNEL_CALLBACK_NOT_FOUND",
                message=f"channel {channel_name!r} does not use platform callbacks",
                status_code=404,
            )
        return await provider.forward_callback(
            deployment_id=deployment_id,
            method=method,
            path=path,
            query=query,
            headers=headers,
            raw_body=raw_body,
        )

    # ── Scheduled Deployment Runs ────────────────────────────────────────

    async def list_runs_for_deployment(
        self,
        deployment_id: str,
        *,
        agent_id: str = "",
        assistant_id: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        deployment = await self._get_for_deployment(
            deployment_id, agent_id, assistant_id=assistant_id
        )
        self._require_schedule(deployment)
        scheduler = self._require_scheduler()
        async with self._scheduling():
            records = list(await scheduler.list_runs(deployment_id, limit=limit))
        session_ids = [record.session_id for record in records if record.session_id]
        snapshots = await self._session_snapshots_repo.get_snapshots_batch(
            session_ids,
            projection={
                "session_id": 1,
                "current_turn_id": 1,
                "active_interaction_id": 1,
                "last_turn_id": 1,
                "last_turn_status": 1,
                "last_turn_error": 1,
            },
        )
        return [
            self._render_run(
                record,
                snapshot=snapshots.get(record.session_id or ""),
            )
            for record in records
        ]

    async def trigger_now(
        self, deployment_id: str, *, agent_id: str = "", assistant_id: str = ""
    ) -> dict[str, Any]:
        deployment = await self._get_for_deployment(
            deployment_id, agent_id, assistant_id=assistant_id
        )
        self._require_schedule(deployment)
        scheduler = self._require_scheduler()
        async with self._scheduling():
            record = await scheduler.enqueue(
                deployment_id=deployment_id,
                trigger="manual",
                payload=run_payload(deployment),
            )
        return self._render_run(record, snapshot=await self._snapshot_for_run(record))

    async def replay_run(
        self,
        run_id: str,
        *,
        deployment_id: str,
        agent_id: str = "",
        assistant_id: str = "",
    ) -> dict[str, Any]:
        """Start a new Run with the prompt a previous Run of this Deployment captured.

        The new Run uses the Deployment's current target; the source Run's id is
        recorded on it. A Run of another Deployment is the same 404 as a
        missing one.
        """

        deployment = await self._get_for_deployment(
            deployment_id, agent_id, assistant_id=assistant_id
        )
        self._require_schedule(deployment)
        scheduler = self._require_scheduler()
        async with self._scheduling():
            source = await scheduler.get_run(run_id)
        if source is None or source.invocation.deployment_id != str(deployment_id):
            raise APIError(
                code="NOT_FOUND", message="Deployment Run not found", status_code=404
            )
        captured = str(source.invocation.payload.get("input_text") or "")
        async with self._scheduling():
            record = await scheduler.enqueue(
                deployment_id=deployment_id,
                trigger="replay",
                payload=run_payload(deployment, input_text=captured),
                replayed_from_run_id=run_id,
            )
        return self._render_run(record, snapshot=await self._snapshot_for_run(record))

    async def _snapshot_for_run(self, run: RunRecord) -> dict[str, Any] | None:
        if not run.session_id:
            return None
        return await self._session_snapshots_repo.get_snapshot(run.session_id)

    async def start_run_session(
        self, context: dict[str, Any], *, run_id: str
    ) -> str:
        """Create (or find) one Run's Session; the scheduler provider calls this.

        The Session's idempotency key is the Run id, so a provider that calls
        again for the same Run gets the same Session.
        """

        deployment_id = str(context.get("deployment_id") or "").strip()
        deployment = await self._deployment_repo.get_by_id(deployment_id)
        if (
            not deployment
            or deployment.get("deleted") is True
            or str(deployment.get("scene") or "") != SCENE_SCHEDULE
            or target_ref(deployment) != target_ref(context)
        ):
            raise RunPermanentError("scheduled Deployment no longer exists")
        if (
            str(context.get("trigger") or "schedule") == "schedule"
            and deployment.get("enabled") is False
        ):
            raise RunPermanentError("scheduled Deployment is disabled")
        target = await self._targets.resolve(context)
        if target is None or not target.owner_user_id:
            raise RunPermanentError(
                "scheduled Deployment's Agent or owner is missing"
            )
        started = await self._targets.start_conversation(
            target,
            UserContext(user_id=target.owner_user_id),
            idempotency_key=f"deployment-run:{run_id}",
        )
        session_id = str((started or {}).get("session_id") or "").strip()
        if not session_id:
            raise RuntimeError("Deployment Run conversation start returned no session")
        return session_id

    async def drive_run_turn(
        self,
        context: dict[str, Any],
        *,
        run_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Admit (or find) one Run's turn; the scheduler provider calls this.

        The turn's ``client_message_id`` is derived from the Run id, so a
        provider that calls again for the same Run finds the admitted command
        instead of admitting a second one.
        """

        target = await self._targets.resolve(context)
        creator_user_id = target.owner_user_id if target is not None else ""
        if not creator_user_id:
            raise RunPermanentError(
                "Deployment Run's Agent or owner is missing"
            )
        if not await self._wait_session_ready(session_id):
            raise RuntimeError(
                f"session {session_id} did not become ready in "
                f"{_SESSION_READY_TIMEOUT_SECONDS}s"
            )
        client_message_id = f"deployment-run:{run_id}"
        command = await self._session_events_repo.find_command_by_client_message_id(
            session_id, client_message_id=client_message_id
        )
        if isinstance(command, dict):
            return self._run_turn_binding(command)

        receipt = await self._dispatch_turn_input(
            UserContext(user_id=creator_user_id),
            session_id,
            str(context.get("input_text") or ""),
            client_message_id=client_message_id,
        )
        command_id = str((receipt or {}).get("command_id") or "").strip()
        turn_id = str((receipt or {}).get("turn_id") or "").strip()
        if command_id and not turn_id:
            # SDK FIFO receipts intentionally expose the input id instead of
            # the platform turn. Its accepted command remains the authority.
            accepted = await self._session_events_repo.get_command_event(
                session_id, command_id=command_id
            )
            turn_id = str((accepted or {}).get("turn_id") or "").strip()
        if not command_id or not turn_id:
            raise RuntimeError("Deployment Run turn admission returned no command binding")
        return {"command_id": command_id, "turn_id": turn_id}

    @staticmethod
    def _run_turn_binding(command: dict[str, Any]) -> dict[str, str]:
        command_id = str(command.get("causation_id") or "").strip()
        turn_id = str(command.get("turn_id") or "").strip()
        if not command_id or not turn_id:
            raise RuntimeError("Deployment Run command has no turn binding")
        return {"command_id": command_id, "turn_id": turn_id}

    @staticmethod
    def _require_schedule(deployment: dict[str, Any]) -> None:
        if str(deployment.get("scene") or "") != SCENE_SCHEDULE:
            raise APIError(
                code="NOT_FOUND",
                message="scheduled Deployment not found",
                status_code=404,
            )

    #: The public status of a Run whose turn the bound Session does not yet
    #: describe, by the provider's dispatch state.
    _DISPATCH_STATUS = {
        "QUEUED": "QUEUED",
        "DISPATCHING": "RUNNING",
        "DISPATCHED": "COMPLETED",
        "FAILED": "FAILED",
        "CANCELLED": "CANCELLED",
    }

    @classmethod
    def _render_run(
        cls, run: RunRecord, *, snapshot: dict[str, Any] | None
    ) -> dict[str, Any]:
        """The public Run: the provider's dispatch state, then the bound Session's turn."""

        invocation = run.invocation
        session_id = run.session_id or ""
        turn_id = run.turn_id or ""
        if isinstance(snapshot, dict) and session_id and not turn_id:
            # A Run owns a fresh Session, so its in-flight turn is unambiguous
            # before the provider has recorded the turn id.
            turn_id = str(snapshot.get("current_turn_id") or "")
        status = cls._DISPATCH_STATUS.get(run.state, "UNKNOWN")
        error: str | None = None
        if isinstance(snapshot, dict) and turn_id:
            if str(snapshot.get("last_turn_id") or "") == turn_id:
                status = str(snapshot.get("last_turn_status") or "UNKNOWN")
                if snapshot.get("last_turn_error"):
                    error = str(snapshot["last_turn_error"])
            elif str(snapshot.get("current_turn_id") or "") == turn_id:
                status = (
                    "WAITING_INPUT"
                    if snapshot.get("active_interaction_id")
                    else "RUNNING"
                )
        if error is None and status == "FAILED" and run.failed:
            # The provider keeps its own failure detail for operators; it is
            # not part of the Run API.
            error = "Deployment Run execution failed"
        payload = invocation.payload
        return {
            "run_id": invocation.run_id,
            "deployment_id": invocation.deployment_id,
            "agent_id": str(payload.get("agent_id") or "") or None,
            "assistant_id": str(payload.get("assistant_id") or "") or None,
            "trigger": invocation.trigger,
            "status": status,
            "scheduled_for": (
                invocation.scheduled_for.isoformat()
                if invocation.scheduled_for is not None
                else None
            ),
            "replayed_from_run_id": invocation.replayed_from_run_id,
            "session_id": session_id or None,
            "turn_id": turn_id or None,
            "error": error,
            "created_at": run.created_at.isoformat() if run.created_at is not None else None,
        }

    # ── internals ────────────────────────────────────────────────────────────

    def _verify_signature(
        self, webhook: dict[str, Any], headers: dict[str, str], raw_body: bytes
    ) -> None:
        secret = str(webhook.get("secret") or "")
        scene = str(webhook.get("scene") or "")
        # headers are normalized to lower-case keys by the caller
        if scene == SCENE_HMAC:
            timestamp = str(headers.get("x-webhook-timestamp") or "")
            signature = str(headers.get("x-webhook-signature") or "")
            if not timestamp or not signature:
                raise APIError(
                    code="DEPLOYMENT_UNAUTHORIZED",
                    message="missing X-WEBHOOK-TIMESTAMP / X-WEBHOOK-SIGNATURE",
                    status_code=401,
                )
            # Freshness window: reject a stale timestamp before the signature
            # check, so a captured (timestamp, signature, body) triple is
            # unusable once the window lapses. Timestamp is unix epoch seconds.
            #
            # Residual, by design: within the window the exact same triple
            # replays (this scene keeps no nonce state — stateless verify
            # across replicas). Exactly-once here would need a shared
            # nonce/dedup store; channel scenes get precisely that via their
            # dedup_key inbound claims. An operator who needs replay-tight raw
            # hmac should front it with a channel scene (dedup_key) or narrow
            # ASTRABOX_WEBHOOK_HMAC_WINDOW_SECONDS to their sender's real skew.
            window = _hmac_window_seconds()
            try:
                ts = float(timestamp)
            except ValueError:
                raise APIError(
                    code="DEPLOYMENT_UNAUTHORIZED",
                    message="X-WEBHOOK-TIMESTAMP must be unix epoch seconds",
                    status_code=401,
                ) from None
            if abs(time.time() - ts) > window:
                raise APIError(
                    code="DEPLOYMENT_UNAUTHORIZED",
                    message=f"webhook timestamp outside the {window}s freshness window",
                    status_code=401,
                )
            expected = _hmac_signature(secret, timestamp, raw_body)
            if not hmac.compare_digest(expected, signature):
                raise APIError(
                    code="DEPLOYMENT_UNAUTHORIZED", message="invalid webhook signature", status_code=401
                )
        elif scene == SCENE_SCHEDULER:
            # Scheduler uses the deployment's issued secret directly as a bearer-style token.
            provided = str(
                headers.get("x-webhook-secret")
                or _strip_bearer(headers.get("authorization") or "")
            )
            if not provided or not hmac.compare_digest(secret, provided):
                raise APIError(
                    code="DEPLOYMENT_UNAUTHORIZED", message="invalid webhook secret", status_code=401
                )
        else:
            raise APIError(
                code="DEPLOYMENT_MISCONFIGURED",
                message=f"unknown webhook scene {scene!r}",
                status_code=500,
            )

    @staticmethod
    def _build_content(webhook: dict[str, Any], raw_body: bytes) -> str:
        prefix = str(webhook.get("prompt_prefix") or "").strip()
        raw_text = raw_body.decode("utf-8", errors="replace")
        return f"{prefix}\n\n{raw_text}" if prefix else raw_text

    async def _wait_session_ready(self, session_id: str) -> bool:
        """Poll until the session's sandbox is READY (bounded). Returns readiness."""
        deadline_polls = max(1, int(_SESSION_READY_TIMEOUT_SECONDS / _SESSION_READY_POLL_SECONDS))
        for _ in range(deadline_polls):
            session = await self._sessions_repo.get_session(session_id)
            state = str((session or {}).get("state") or "")
            if state in ("READY", "WAITING_INPUT", "BACKGROUND_RUNNING"):
                return True
            if state in ("TERMINATED", "DELETED", "RECOVERY_REQUIRED"):
                return False
            await asyncio.sleep(_SESSION_READY_POLL_SECONDS)
        return False

    async def _fire_when_ready(
        self,
        user: UserContext,
        session_id: str,
        content: str,
        deployment_id: str,
    ) -> None:
        """Background: wait for the session to be READY, then drive one turn.

        Non-interactive — there is no client to retry SESSION_BUSY, so this waits
        for provisioning here before sending. Errors are logged, never raised
        (this runs detached from the webhook's HTTP response). Built-in scenes
        are fire-and-forget by contract; the durable channel spine lives in
        ChannelIngressService.
        """
        try:
            if not session_id:
                logger.error("webhook %s: conversation start returned no session", deployment_id)
                return
            ready = await self._wait_session_ready(session_id)
            if not ready:
                logger.error(
                    "webhook %s: session %s did not become ready in %ss; turn not sent",
                    deployment_id, session_id, _SESSION_READY_TIMEOUT_SECONDS,
                )
                return
            agen = self._stream_message_events_ds(
                user,
                session_id,
                content,
                client_message_id=f"deployment:{deployment_id}:{session_id}",
            )
            await self._drain(agen)
        except Exception as exc:
            logger.error("webhook %s background fire failed: %s", deployment_id, exc)

    @staticmethod
    async def _drain(agen: Any) -> bool:
        """Drain the turn stream; return True only if it completed without error."""
        ok = True
        try:
            async for _ in agen:
                pass
        except Exception as exc:
            ok = False
            logger.error("webhook background drain failed: %s", exc)
        finally:
            with contextlib.suppress(Exception):
                await agen.aclose()
        return ok

    @staticmethod
    def _sanitize(
        doc: dict[str, Any] | None, *, include_secret: bool = False
    ) -> dict[str, Any]:
        if not doc:
            return {}
        clean = dict(doc)
        clean.pop("_id", None)
        clean.pop("source_cursor", None)
        if channel_scene_name(str(clean.get("scene") or "")) is not None:
            clean["execution_mode"] = binding_execution_mode(clean)
        if not include_secret:
            clean.pop("secret", None)
        return clean


def _strip_bearer(value: str) -> str:
    v = str(value or "").strip()
    if v.lower().startswith("bearer "):
        return v[7:].strip()
    return v
