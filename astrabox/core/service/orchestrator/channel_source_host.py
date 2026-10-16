"""Process-lifetime host for binding-scoped channel sources.

A sourcing provider opens one long-lived consumer for each enabled channel
binding. The host reconciles those consumers from the Deployment repository:
each pass compares the enabled bindings' revisions (their ``updated_at``) with
the consumers it runs, and reads and hydrates a binding in full only to start
its consumer. Every envelope then goes through the channel spine's single
typed ingress:

    envelope → durable ingest → durable source cursor → source ack

That order is the loss boundary. A crash before the work item lands leaves the
cursor unchanged, so a resumable source replays the event. A crash after the
work item lands may replay it, but the spine's dedup key makes that harmless.
"""

from __future__ import annotations

import asyncio
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.channel_credentials import (
    ChannelCredentialService,
)
from astrabox.seams.channel import (
    channel_if_registered,
    channel_scene_name,
    registered_channels,
)

logger = get_logger(__name__)

_RECONCILE_INTERVAL_SECONDS = 5.0
_REOPEN_BACKOFF_INITIAL_SECONDS = 5.0
_REOPEN_BACKOFF_MAX_SECONDS = 300.0


class _SourceRedelivery(RuntimeError):
    """The current connection must reopen without advancing its cursor."""


class ChannelSourceHost:
    """Reconcile and consume one source per enabled channel binding."""

    def __init__(
        self,
        *,
        ingress_service: Any,
        deployment_repo: Any,
        spawn_background_task: Any,
        channel_credentials: ChannelCredentialService | None = None,
    ) -> None:
        self._ingress = ingress_service
        self._deployment_repo = deployment_repo
        self._spawn_background_task = spawn_background_task
        self._channel_credentials = channel_credentials or ChannelCredentialService()
        self._supervisor_task: Any = None
        self._tasks: dict[str, Any] = {}
        self._fingerprints: dict[str, str] = {}
        self._reconcile_lock = asyncio.Lock()
        self._closed = False

    # ── Lifecycle ───────────────────────────────────────────────────────

    def ensure_started(self) -> None:
        if self._closed:
            logger.warning("channel_source_host: not starting (closed)")
            return
        if not any(
            provider.supports_source for provider in registered_channels().values()
        ):
            return
        task = self._supervisor_task
        if task is not None and not task.done():
            return
        self._supervisor_task = self._spawn_background_task(
            self._reconcile_loop(), name="channel-source-host"
        )

    def quiesce(self) -> None:
        self._closed = True
        task = self._supervisor_task
        if task is not None and not task.done():
            task.cancel("shutdown")
        for task in self._tasks.values():
            if task is not None and not task.done():
                task.cancel("shutdown")

    # ── Binding reconciliation ─────────────────────────────────────────

    async def _reconcile_loop(self) -> None:
        while not self._closed:
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("channel source binding reconciliation failed")
            try:
                await asyncio.sleep(_RECONCILE_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                raise

    async def reconcile(self) -> None:
        """Converge binding consumers before a management mutation returns."""
        if self._closed:
            return
        async with self._reconcile_lock:
            await self._reconcile_once()

    async def _reconcile_once(self) -> None:
        rows = await self._deployment_repo.list_channel_bindings()
        desired: dict[str, tuple[str, Any, str]] = {}
        for binding in rows:
            deployment_id = str(binding.get("deployment_id") or "").strip()
            channel_name = channel_scene_name(str(binding.get("scene") or ""))
            if not deployment_id or channel_name is None:
                continue
            provider = channel_if_registered(channel_name)
            if provider is None:
                logger.warning(
                    "channel source binding %s names an unregistered provider %r",
                    deployment_id,
                    channel_name,
                )
                continue
            if not provider.supports_source:
                continue
            desired[deployment_id] = (
                channel_name,
                provider,
                self._binding_fingerprint(binding),
            )

        cancelled: list[tuple[str, Any]] = []
        for deployment_id in set(self._tasks) - set(desired):
            task = self._cancel_binding(deployment_id, "binding disabled or deleted")
            if task is not None:
                cancelled.append((deployment_id, task))

        replacements: list[tuple[str, str, Any, str]] = []
        for deployment_id, (name, provider, fingerprint) in desired.items():
            task = self._tasks.get(deployment_id)
            if (
                task is not None
                and not task.done()
                and self._fingerprints.get(deployment_id) == fingerprint
            ):
                continue
            if task is not None:
                cancelled_task = self._cancel_binding(
                    deployment_id, "binding configuration changed"
                )
                if cancelled_task is not None:
                    cancelled.append((deployment_id, cancelled_task))
            replacements.append((deployment_id, name, provider, fingerprint))

        if cancelled:
            results = await asyncio.gather(
                *(task for _, task in cancelled), return_exceptions=True
            )
            for (deployment_id, _), result in zip(cancelled, results, strict=True):
                if isinstance(result, Exception):
                    logger.warning(
                        "channel source binding %s failed while stopping: %s",
                        deployment_id,
                        result,
                    )

        for deployment_id, name, provider, fingerprint in replacements:
            self._fingerprints[deployment_id] = fingerprint
            self._tasks[deployment_id] = self._spawn_background_task(
                self._consume_binding_loop(
                    name=name,
                    provider=provider,
                    deployment_id=deployment_id,
                    fingerprint=fingerprint,
                ),
                name=f"channel-source-{name}-{deployment_id}",
            )
            logger.info(
                "channel_source_host: consuming %r binding %s", name, deployment_id
            )

    def _cancel_binding(self, deployment_id: str, reason: str) -> Any:
        task = self._tasks.pop(deployment_id, None)
        self._fingerprints.pop(deployment_id, None)
        if task is not None and not task.done():
            task.cancel(reason)
            return task
        return None

    @staticmethod
    def _binding_fingerprint(binding: dict[str, Any]) -> str:
        """The binding revision a consumer runs.

        Every configuration write, credentials included, changes a binding's
        ``updated_at``, so a consumer opened at another revision is replaced.
        """
        return f"{binding.get('scene')}@{binding.get('updated_at')}"

    # ── Consume loop ────────────────────────────────────────────────────

    async def _consume_binding_loop(
        self,
        *,
        name: str,
        provider: Any,
        deployment_id: str,
        fingerprint: str,
    ) -> None:
        backoff = _REOPEN_BACKOFF_INITIAL_SECONDS
        while not self._closed:
            binding = await self._deployment_repo.get_by_id(deployment_id)
            if (
                not binding
                or binding.get("enabled") is False
                or channel_scene_name(str(binding.get("scene") or "")) != name
                or self._binding_fingerprint(binding) != fingerprint
            ):
                return
            try:
                hydrated = await self._channel_credentials.hydrate(binding, provider)
                async for envelope in provider.open_source(binding=hydrated):
                    if not await self._handle_envelope(name, envelope):
                        raise _SourceRedelivery(
                            "source envelope was not durably acknowledged"
                        )
                    backoff = _REOPEN_BACKOFF_INITIAL_SECONDS
                # A cleanly ended source is reopened like a disconnected one.
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "channel source %r binding %s failed (reopening in %.0fs): %s",
                    name,
                    deployment_id,
                    backoff,
                    exc,
                )
            if self._closed:
                return
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(backoff * 2, _REOPEN_BACKOFF_MAX_SECONDS)

    async def _handle_envelope(self, name: str, envelope: Any) -> bool:
        """Ingest one envelope and acknowledge only durable ownership."""
        deployment_id = str(getattr(envelope, "deployment_id", "") or "")
        try:
            receipt = await self._ingress.ingest(deployment_id, envelope.inbound)
        except Exception as exc:
            logger.warning(
                "channel source %r ingest failed; requesting redelivery: %s",
                name,
                exc,
            )
            await self._nack(name, envelope, f"{type(exc).__name__}: {exc}")
            return False
        if getattr(receipt, "status", "") == "enrich_unmatched":
            await self._nack(name, envelope, "enrichment anchor not yet durable")
            return False

        source_cursor = getattr(envelope, "source_cursor", None)
        if source_cursor is not None:
            if not isinstance(source_cursor, int) or isinstance(source_cursor, bool):
                await self._nack(name, envelope, "source cursor must be an integer")
                return False
            advanced = await self._deployment_repo.advance_channel_source_cursor(
                deployment_id,
                scene=f"channel:{name}",
                source_cursor=source_cursor,
            )
            if not advanced:
                await self._nack(name, envelope, "source cursor was not persisted")
                return False
        try:
            await envelope.ack(receipt)
        except Exception:
            # The work item and optional cursor are already durable, so a
            # transport-level ack failure can only cause an idempotent replay.
            logger.exception("channel source %r ack failed (durable claim retained)", name)
        return True

    @staticmethod
    async def _nack(name: str, envelope: Any, reason: str) -> None:
        try:
            await envelope.nack(reason)
        except Exception:
            logger.exception("channel source %r nack failed", name)
