from __future__ import annotations

from typing import Any

from astrabox.persistence.repository.backend import (
    ReturnDocument,
    get_async_collection,
    run_mongo_with_retry,
)
from astrabox.common.utils.settings import load_astrabox_settings


class DeploymentRepository:
    """Webhook configs that let external systems trigger an agent conversation.

    One document per webhook, keyed by a random ``deployment_id``. Each webhook is
    bound to one agent instance (``agent_id``); an inbound POST to the webhook
    starts a new conversation on that agent, running as the agent's creator
    (durable IAM identity). Mirrors EnvironmentRepository's minimal storage shape.
    """

    def __init__(self) -> None:
        settings = load_astrabox_settings()
        self._collection_name = settings.deployments_collection

    @staticmethod
    def _active_deployment_filter(
        deployment_id: str, agent_id: str
    ) -> dict[str, Any] | None:
        """The nested-resource fence shared by every management mutation.

        ``agent_id`` is part of the write filter, not a value checked only
        by the service before a later id-only write.  The explicit absent/false
        ``deleted`` clauses are the live-row filter described in
        :meth:`list_by_agent`.
        """

        wid = str(deployment_id or "").strip()
        aid = str(agent_id or "").strip()
        if not wid or not aid:
            return None
        return {
            "deployment_id": wid,
            "agent_id": aid,
            "$or": [{"deleted": {"$exists": False}}, {"deleted": False}],
        }

    async def get_by_id(self, deployment_id: str) -> dict[str, Any] | None:
        wid = str(deployment_id or "").strip()
        if not wid:
            return None
        collection = await get_async_collection(self._collection_name)
        return await run_mongo_with_retry(
            "deployments.get_by_id",
            lambda: collection.find_one({"deployment_id": wid}),
        )

    async def get_for_deployment(
        self, deployment_id: str, agent_id: str
    ) -> dict[str, Any] | None:
        """Return one active webhook only inside ``agent_id``'s scope."""

        query = self._active_deployment_filter(deployment_id, agent_id)
        if query is None:
            return None
        collection = await get_async_collection(self._collection_name)
        return await run_mongo_with_retry(
            "deployments.get_for_deployment",
            lambda: collection.find_one(query),
        )

    async def list_by_agent(self, agent_id: str) -> list[dict[str, Any]]:
        aid = str(agent_id or "").strip()
        if not aid:
            return []

        async def _list() -> list[dict[str, Any]]:
            collection = await get_async_collection(self._collection_name)
            # A freshly-created webhook carries no `deleted` field at all, so the
            # live-row filter spells out both the absent and the false case
            # instead of depending on how a backend treats a missing field.
            cursor = collection.find(
                {
                    "agent_id": aid,
                    "$or": [{"deleted": {"$exists": False}}, {"deleted": False}],
                }
            )
            docs = [doc async for doc in cursor]
            docs.sort(key=lambda item: str(item.get("created_at") or ""))
            return docs

        return await run_mongo_with_retry("deployments.list_by_agent", _list)

    async def list_active(self) -> list[dict[str, Any]]:
        """Return every live Deployment in one collection read."""

        async def _list() -> list[dict[str, Any]]:
            collection = await get_async_collection(self._collection_name)
            cursor = collection.find(
                {"$or": [{"deleted": {"$exists": False}}, {"deleted": False}]}
            )
            docs = [doc async for doc in cursor]
            docs.sort(key=lambda item: str(item.get("created_at") or ""))
            return docs

        return await run_mongo_with_retry("deployments.list_active", _list)

    async def list_scheduled(self) -> list[dict[str, Any]]:
        """Return every live scheduled Deployment for DBOS projection sync."""

        async def _list() -> list[dict[str, Any]]:
            collection = await get_async_collection(self._collection_name)
            cursor = collection.find(
                {
                    "scene": "schedule",
                    "$or": [{"deleted": {"$exists": False}}, {"deleted": False}],
                }
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("deployments.list_scheduled", _list)

    async def list_channel_bindings(self) -> list[dict[str, Any]]:
        """Return every enabled channel binding for source reconciliation."""

        async def _list() -> list[dict[str, Any]]:
            collection = await get_async_collection(self._collection_name)
            cursor = collection.find(
                {"$or": [{"deleted": {"$exists": False}}, {"deleted": False}]}
            )
            docs = [doc async for doc in cursor]
            return [
                doc
                for doc in docs
                if doc.get("enabled") is not False
                and str(doc.get("scene") or "").startswith("channel:")
            ]

        return await run_mongo_with_retry("deployments.list_channel_bindings", _list)

    async def advance_channel_source_cursor(
        self, deployment_id: str, *, scene: str, source_cursor: int
    ) -> bool:
        """Advance one binding's resumable source cursor monotonically.

        A cursor is delivery acknowledgement state, so a disabled/deleted
        binding cannot advance it. Concurrent replicas may observe the same
        event; the conditional write lets only a newer sequence win.
        """

        wid = str(deployment_id or "").strip()
        expected_scene = str(scene or "").strip()
        cursor_value = int(source_cursor)
        if not wid or not expected_scene.startswith("channel:") or cursor_value < 0:
            return False
        collection = await get_async_collection(self._collection_name)

        async def _advance() -> dict[str, Any] | None:
            return await collection.find_one_and_update(
                {
                    "deployment_id": wid,
                    "scene": expected_scene,
                    "$and": [
                        {
                            "$or": [
                                {"deleted": {"$exists": False}},
                                {"deleted": False},
                            ]
                        },
                        {
                            "$or": [
                                {"enabled": {"$exists": False}},
                                {"enabled": True},
                            ]
                        },
                        {
                            "$or": [
                                {"source_cursor": {"$exists": False}},
                                {"source_cursor": {"$lt": cursor_value}},
                            ]
                        },
                    ],
                },
                {"$set": {"source_cursor": cursor_value}},
                return_document=ReturnDocument.AFTER,
            )

        updated = await run_mongo_with_retry(
            "deployments.advance_channel_source_cursor", _advance
        )
        if updated is not None:
            return True
        current = await self.get_by_id(wid)
        if (
            not current
            or current.get("deleted") is True
            or current.get("enabled") is False
            or str(current.get("scene") or "") != expected_scene
        ):
            return False
        stored_cursor = current.get("source_cursor")
        return (
            isinstance(stored_cursor, int)
            and not isinstance(stored_cursor, bool)
            and stored_cursor >= cursor_value
        )

    async def upsert(self, deployment_id: str, doc: dict[str, Any]) -> dict[str, Any]:
        """Insert or update a webhook config by deployment_id; returns stored doc."""
        wid = str(deployment_id or "").strip()
        collection = await get_async_collection(self._collection_name)
        doc_with_id = {**doc, "deployment_id": wid}
        await run_mongo_with_retry(
            "deployments.upsert",
            lambda: collection.update_one(
                {"deployment_id": wid},
                {"$set": doc_with_id},
                upsert=True,
            ),
        )
        stored = await self.get_by_id(wid)
        return stored or doc_with_id

    async def update_for_deployment(
        self,
        deployment_id: str,
        agent_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Atomically update and return one active deployment-scoped webhook.

        A missing, deleted, or foreign row does not match and returns ``None``.
        Scope/identity fields can never be changed through this mutation even if
        a future caller accidentally includes them in ``updates``.
        """

        query = self._active_deployment_filter(deployment_id, agent_id)
        if query is None:
            return None
        protected = {"_id", "deployment_id", "agent_id", "deleted"}
        safe_updates = {
            key: value for key, value in dict(updates or {}).items() if key not in protected
        }
        collection = await get_async_collection(self._collection_name)
        return await run_mongo_with_retry(
            "deployments.update_for_deployment",
            lambda: collection.find_one_and_update(
                query,
                {"$set": safe_updates},
                return_document=ReturnDocument.AFTER,
            ),
        )

    async def soft_delete_for_deployment(
        self, deployment_id: str, agent_id: str
    ) -> bool:
        """Atomically soft-delete one active deployment-scoped webhook.

        The active-row guard makes delete idempotency observable: the first
        matching delete returns ``True``; a duplicate, missing, deleted, or
        foreign delete returns ``False`` and the service maps all four to 404.
        """

        query = self._active_deployment_filter(deployment_id, agent_id)
        if query is None:
            return False
        collection = await get_async_collection(self._collection_name)
        deleted = await run_mongo_with_retry(
            "deployments.soft_delete_for_deployment",
            lambda: collection.find_one_and_update(
                query,
                {"$set": {"deleted": True}},
                return_document=ReturnDocument.AFTER,
            ),
        )
        return deleted is not None
