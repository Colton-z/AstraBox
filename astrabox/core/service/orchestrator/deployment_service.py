"""Deployment management and invocation through the Session kernel.

Deployments are trigger bindings on an Agent. Built-in schedules create durable
Runs through the internal execution adapter; external systems can instead call
an authenticated webhook or a registered channel provider. Every path starts a
normal Session and uses the existing turn authority.

Identity: each webhook is bound to one agent instance; the conversation runs as
that agent's creator (``agent.user_id``). Auth on the inbound endpoint is the
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
from datetime import datetime, timezone
from typing import Any

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
)
from astrabox.core.service.orchestrator.channel_credentials import (
    ChannelCredentialService,
)
from astrabox.core.service.orchestrator.deployment_run_runtime import (
    PermanentDeploymentRunError,
    deployment_run_runtime,
)
from astrabox.seams.channel import (
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
        run_runtime: Any | None = None,
        session_snapshots_repo: Any | None = None,
        session_events_repo: Any | None = None,
    ) -> None:
        self._deployment_repo = deployment_repo
        self._agent_repo = agent_repo
        self._agent_service_getter = agent_service_getter
        self._agent_config = agent_config
        # platform_service.stream_message_events_ds (bound method)
        self._stream_message_events_ds = stream_message_events_ds
        # platform_service.dispatch_turn_input (bound method). Scheduled Runs
        # need the durable admission receipt, not the turn's output stream.
        self._dispatch_turn_input = dispatch_turn_input
        self._sessions_repo = sessions_repo
        self._run_runtime = run_runtime or deployment_run_runtime
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

    async def list_manageable(self, user: UserContext) -> list[dict[str, Any]]:
        """List the caller's Deployments without one request per Agent."""

        rows = await self._deployment_repo.list_active()
        agent_ids = [
            str(row.get("agent_id") or "").strip()
            for row in rows
            if str(row.get("agent_id") or "").strip()
        ]
        agents = await self._agent_repo.list_agents_by_ids(agent_ids)
        visible: list[dict[str, Any]] = []
        for row in rows:
            agent_id = str(row.get("agent_id") or "").strip()
            agent = agents.get(agent_id)
            if not self._can_manage_agent(user, agent):
                continue
            rendered = self._sanitize(row)
            rendered["agent_name"] = str((agent or {}).get("name") or agent_id)
            visible.append(rendered)
        return visible

    async def create(
        self,
        *,
        agent_id: str,
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
    ) -> dict[str, Any]:
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
        deployment_id = uuid.uuid4().hex
        doc = {
            "deployment_id": deployment_id,
            "agent_id": str(agent_id).strip(),
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
            # The repository is the desired-state authority. Write it first so
            # any invocation created during projection sees committed config;
            # startup reconciliation repairs a crash between these two writes.
            stored = await self._deployment_repo.upsert(deployment_id, doc)
            try:
                await self._run_runtime.apply(doc)
                return self._sanitize(stored)
            except Exception:
                # The runtime restores its prior projection before raising. A
                # fresh random ID therefore has no execution state to delete;
                # only the desired-state write needs to be rolled back here.
                cleanup_error: Exception | None = None
                try:
                    await self._deployment_repo.soft_delete_for_deployment(
                        deployment_id, str(agent_id).strip()
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
        agent_id: str,
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        # Resolve visibility before validating the patch so missing, deleted and
        # foreign ids remain indistinguishable.  The repository repeats the
        # same scope/deleted predicates on the eventual atomic write, closing
        # the read -> mutation race.
        existing = await self._get_for_deployment(deployment_id, agent_id)
        allowed = {
            "enabled",
            "prompt_prefix",
            "scene",
            "attention_policy",
            "channel_config",
            "credentials",
            "name",
            "schedule",
        }
        updates = {k: v for k, v in patch.items() if k in allowed}
        existing_scene = str(existing.get("scene") or "")
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
            # The execution engine validates and changes its projection first.
            # If the guarded product write then fails or loses a delete race,
            # restore the old projection; startup reconciliation is the
            # crash-safe backstop.
            await self._run_runtime.apply(candidate)
            try:
                updated = await self._deployment_repo.update_for_deployment(
                    deployment_id,
                    agent_id,
                    {**updates, "updated_at": candidate["updated_at"]},
                )
            except Exception:
                await self._restore_schedule_projection(existing)
                raise
            if updated is None:
                # The row disappeared after the visibility read. Its desired
                # state is deletion, so leaving either old or candidate
                # execution config would create an orphan invocation source.
                await self._run_runtime.delete(deployment_id)
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

    async def delete(self, deployment_id: str, *, agent_id: str) -> None:
        existing = await self._deployment_repo.get_for_deployment(
            deployment_id, agent_id
        )
        if not existing:
            # Repeat the guarded mutation even for an invisible id. Besides
            # preserving the one atomic repository boundary, this keeps a
            # missing/deleted/foreign id indistinguishable under a race.
            await self._deployment_repo.soft_delete_for_deployment(
                deployment_id, agent_id
            )
            self._raise_deployment_not_found()
        if str(existing.get("scene") or "") == SCENE_SCHEDULE:
            await self._run_runtime.delete(deployment_id)
        channel_name = channel_scene_name(str(existing.get("scene") or ""))
        prior_credentials: dict[str, Any] | None = None
        if channel_name is not None and get_channel(channel_name).describe().credential_fields:
            prior_credentials = await self._channel_credentials.get(deployment_id)
            await self._channel_credentials.purge(deployment_id)
        try:
            deleted = await self._deployment_repo.soft_delete_for_deployment(
                deployment_id, agent_id
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
            await self._run_runtime.apply(deployment)
        except Exception:
            logger.exception(
                "could not restore scheduled Deployment projection %s",
                deployment.get("deployment_id"),
            )
            raise

    async def _get_for_deployment(
        self, deployment_id: str, agent_id: str
    ) -> dict[str, Any]:
        existing = await self._deployment_repo.get_for_deployment(
            deployment_id, agent_id
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
        agent_id = str(deployment.get("agent_id") or "").strip()
        agent = await self._agent_repo.get_agent(agent_id)
        if not agent:
            raise APIError(code="NOT_FOUND", message="bound agent not found", status_code=404)
        creator_user_id = str(agent.get("user_id") or "").strip()
        if not creator_user_id:
            raise APIError(
                code="DEPLOYMENT_NO_OWNER",
                message="bound agent has no creator to run as",
                status_code=409,
            )
        user = UserContext(user_id=creator_user_id)
        agent_service = self._agent_service_getter()
        started = await agent_service.start_conversation(user, agent_id)
        session_id = str(started.get("session_id") or "")
        self._spawn_background_task(
            self._fire_when_ready(user, session_id, content, deployment_id),
            name=f"deployment-fire-{deployment_id}",
        )
        logger.warning(
            "deployment trigger accepted: deployment=%s agent=%s creator=%s session=%s scene=%s",
            deployment_id, agent_id, creator_user_id, session_id,
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
        agent_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        deployment = await self._get_for_deployment(deployment_id, agent_id)
        self._require_schedule(deployment)
        rows = await self._run_runtime.list_runs(deployment_id, limit=limit)
        session_ids = [
            self._run_binding(row)[0]
            for row in rows
            if self._run_binding(row)[0]
        ]
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
                row,
                snapshot=snapshots.get(self._run_binding(row)[0]),
            )
            for row in rows
        ]

    async def trigger_now(
        self, deployment_id: str, *, agent_id: str
    ) -> dict[str, Any]:
        deployment = await self._get_for_deployment(deployment_id, agent_id)
        self._require_schedule(deployment)
        row = await self._run_runtime.run_now(deployment)
        return self._render_run(row, snapshot=await self._snapshot_for_run(row))

    async def replay_run(
        self,
        run_id: str,
        *,
        deployment_id: str,
        agent_id: str,
    ) -> dict[str, Any]:
        deployment = await self._get_for_deployment(deployment_id, agent_id)
        self._require_schedule(deployment)
        row = await self._run_runtime.replay(deployment, run_id)
        return self._render_run(row, snapshot=await self._snapshot_for_run(row))

    async def _snapshot_for_run(
        self, run: dict[str, Any]
    ) -> dict[str, Any] | None:
        session_id, _ = self._run_binding(run)
        if not session_id:
            return None
        return await self._session_snapshots_repo.get_snapshot(session_id)

    async def start_run_session(
        self, context: dict[str, Any], *, run_id: str
    ) -> str:
        """DBOS step target: idempotently create one Run's Session."""

        deployment_id = str(context.get("deployment_id") or "").strip()
        agent_id = str(context.get("agent_id") or "").strip()
        deployment = await self._deployment_repo.get_by_id(deployment_id)
        if (
            not deployment
            or deployment.get("deleted") is True
            or str(deployment.get("scene") or "") != SCENE_SCHEDULE
            or str(deployment.get("agent_id") or "") != agent_id
        ):
            raise PermanentDeploymentRunError("scheduled Deployment no longer exists")
        if (
            str(context.get("trigger") or "schedule") == "schedule"
            and deployment.get("enabled") is False
        ):
            raise PermanentDeploymentRunError("scheduled Deployment is disabled")
        agent = await self._agent_repo.get_agent(agent_id)
        creator_user_id = str((agent or {}).get("user_id") or "").strip()
        if not agent or not creator_user_id:
            raise PermanentDeploymentRunError(
                "scheduled Deployment's Agent or owner is missing"
            )
        started = await self._agent_service_getter().start_conversation(
            UserContext(user_id=creator_user_id),
            agent_id,
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
        """DBOS step target: idempotently admit one Session turn."""

        agent_id = str(context.get("agent_id") or "").strip()
        agent = await self._agent_repo.get_agent(agent_id)
        creator_user_id = str((agent or {}).get("user_id") or "").strip()
        if not agent or not creator_user_id:
            raise PermanentDeploymentRunError(
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

    @staticmethod
    def _run_binding(run: dict[str, Any]) -> tuple[str, str]:
        attributes = run.get("attributes")
        if not isinstance(attributes, dict):
            attributes = {}
        return (
            str(attributes.get("session_id") or ""),
            str(attributes.get("turn_id") or ""),
        )

    @classmethod
    def _render_run(
        cls, run: dict[str, Any], *, snapshot: dict[str, Any] | None
    ) -> dict[str, Any]:
        workflow_id = str(run.get("workflow_id") or "")
        dbos_status = str(run.get("status") or "")
        attributes = run.get("attributes")
        if not isinstance(attributes, dict):
            attributes = {}
        context = deployment_run_runtime.context_from_status(run)
        session_id, turn_id = cls._run_binding(run)
        if isinstance(snapshot, dict) and session_id and not turn_id:
            # A Run owns a fresh Session, so its in-flight turn is unambiguous
            # before the durable workflow has copied the turn ID to its ledger.
            turn_id = str(snapshot.get("current_turn_id") or "")
        status = {
            "ENQUEUED": "QUEUED",
            "DELAYED": "QUEUED",
            "PENDING": "RUNNING",
            "SUCCESS": "COMPLETED",
            "ERROR": "FAILED",
            "MAX_RECOVERY_ATTEMPTS_EXCEEDED": "FAILED",
            "CANCELLED": "CANCELLED",
        }.get(dbos_status, "UNKNOWN")
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
        if error is None and status == "FAILED" and run.get("error") is not None:
            # The durable engine stores Python exception details for operators.
            # They are implementation diagnostics, not part of the Run API.
            error = "Deployment Run execution failed"

        def timestamp(value: Any) -> str | None:
            if value is None:
                return None
            try:
                return datetime.fromtimestamp(
                    int(value) / 1000, tz=timezone.utc
                ).isoformat()
            except (TypeError, ValueError, OverflowError):
                return None

        trigger = str(
            attributes.get("trigger") or context.get("trigger") or "schedule"
        )
        scheduled_for = str(attributes.get("scheduled_for") or "").strip() or None
        if scheduled_for is None and trigger == "schedule":
            workflow_input = run.get("input")
            args = workflow_input.get("args") if isinstance(workflow_input, dict) else None
            if isinstance(args, (list, tuple)) and args and isinstance(args[0], datetime):
                value = args[0]
                scheduled_for = (
                    value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
                ).astimezone(timezone.utc).isoformat()
        return {
            "run_id": workflow_id,
            "deployment_id": str(
                attributes.get("deployment_id") or context.get("deployment_id") or ""
            ),
            "agent_id": str(
                attributes.get("agent_id") or context.get("agent_id") or ""
            ),
            "trigger": trigger,
            "status": status,
            "scheduled_for": scheduled_for,
            "replayed_from_run_id": str(
                attributes.get("replayed_from_run_id")
                or context.get("replayed_from_run_id")
                or ""
            )
            or None,
            "session_id": session_id or None,
            "turn_id": turn_id or None,
            "error": error,
            "created_at": timestamp(run.get("created_at")),
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
        if not include_secret:
            clean.pop("secret", None)
        return clean


def _strip_bearer(value: str) -> str:
    v = str(value or "").strip()
    if v.lower().startswith("bearer "):
        return v[7:].strip()
    return v
