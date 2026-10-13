"""A2A protocol identities and provenance; execution remains in the Session journal."""

from __future__ import annotations

from typing import Any

from astrabox.persistence.repository._compat import DuplicateKeyError
from astrabox.persistence.repository.backend import get_async_collection, run_mongo_with_retry


class AgentTaskReferenceRepository:
    async def reserve(self, reference: dict[str, Any]) -> dict[str, Any]:
        collection = await get_async_collection("agent_task_references")
        try:
            await run_mongo_with_retry(
                "agent_task_references.reserve", lambda: collection.insert_one(dict(reference))
            )
        except DuplicateKeyError:
            pass
        stored = await self.get(str(reference["_id"]))
        if stored is None:
            raise RuntimeError("reserved Agent task reference is missing")
        return stored

    async def get(self, reference_id: str) -> dict[str, Any] | None:
        collection = await get_async_collection("agent_task_references")
        return await run_mongo_with_retry(
            "agent_task_references.get", lambda: collection.find_one({"_id": reference_id})
        )

    async def bind_session(self, reference_id: str, session_id: str) -> None:
        collection = await get_async_collection("agent_task_references")
        await run_mongo_with_retry(
            "agent_task_references.bind_session",
            lambda: collection.update_one(
                {
                    "_id": reference_id,
                    "$or": [
                        {"session_id": {"$exists": False}},
                        {"session_id": session_id},
                    ],
                },
                {"$set": {"session_id": session_id}},
            ),
        )
        stored = await self.get(reference_id)
        if stored is None or stored.get("session_id") != session_id:
            raise RuntimeError("Agent task reference has conflicting Session bindings")

    async def list_tasks(
        self,
        *,
        user_id: str,
        org_id: str,
        agent_id: str,
        context_id: str = "",
    ) -> list[dict[str, Any]]:
        collection = await get_async_collection("agent_task_references")
        query: dict[str, Any] = {
            "user_id": user_id,
            "org_id": org_id,
            "agent_id": agent_id,
            "kind": "task",
            "session_id": {"$exists": True},
        }
        if context_id:
            query["context_id"] = context_id

        async def read() -> list[dict[str, Any]]:
            return [row async for row in collection.find(query).sort("_id", 1)]

        return await run_mongo_with_retry("agent_task_references.list", read)
