"""Repository for the assistant_workspace collection.

Primary-key fence per assistant runtime: ``_id = sha1(assistant_id)`` because
the long-running sandbox belongs to the assistant class, not to each user.
User isolation is handled inside the shared sandbox by per-user assistant
profiles and by session ownership.
"""

from __future__ import annotations

import hashlib
from typing import Any

from astrabox.persistence.repository.backend import (
    get_async_collection,
    run_mongo_with_retry,
)
from astrabox.persistence.repository.index_verification import ensure_unique_index
from astrabox.persistence.repository.session_repository import _safe_create_index
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.common.utils.time_utils import utcnow_iso

logger = get_logger(__name__)


def assistant_workspace_fence_id(user_id: str, assistant_id: str) -> str:
    """Deterministic primary key for one shared assistant runtime sandbox.

    Keyed on ``assistant_id`` alone: the long-running sandbox belongs to the
    assistant class, not to each user, so ``user_id`` is accepted for call-site
    symmetry and deliberately ignored.
    """
    _ = user_id
    raw = f"assistant|{assistant_id}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


class AssistantWorkspaceRepository:
    def __init__(self) -> None:
        settings = load_astrabox_settings()
        self._collection_name = settings.assistant_workspace_collection
        self._index_ready = False

    async def _ensure_indexes(self) -> None:
        if self._index_ready:
            return
        collection = await get_async_collection(self._collection_name)
        if collection is not None:
            await ensure_unique_index(
                collection,
                "assistant_id",
                collection_name=self._collection_name,
            )
            try:
                await _safe_create_index(
                    collection,
                    [("state", 1), ("updated_at", 1)],
                )
                await _safe_create_index(collection, "current_sandbox_id")
                await _safe_create_index(
                    collection,
                    [("current_sandbox_id", 1), ("current_sandbox_expires_at", 1)],
                )
            except Exception as exc:
                logger.warning(
                    "ensure assistant_workspace indexes failed: %s", exc
                )
        self._index_ready = True

    async def materialize_workspace(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Insert workspace row keyed on sha1 ``_id`` fence.

        Caller passes creator/bootstrap ``user_id`` + ``assistant_id``; only
        ``assistant_id`` participates in the shared-runtime ``_id``.
        Returns the stored doc or raises if the primary-key fence trips.
        """
        await self._ensure_indexes()
        user_id = str(
            payload.get("user_id")
            or payload.get("created_by_user_id")
            or payload.get("bootstrap_user_id")
            or ""
        ).strip()
        assistant_id = str(payload.get("assistant_id") or "").strip()
        if not user_id or not assistant_id:
            raise ValueError(
                "assistant_workspace.materialize requires user_id and assistant_id"
            )
        fence_id = assistant_workspace_fence_id(user_id, assistant_id)
        now = utcnow_iso()
        doc = {
            **payload,
            "_id": fence_id,
            "created_at": now,
            "updated_at": now,
            "created_by_user_id": user_id,
            "state": "MATERIALIZING",
            "current_sandbox_id": None,
            "current_sandbox_expires_at": None,
        }
        collection = await get_async_collection(self._collection_name)
        await run_mongo_with_retry(
            "assistant_workspace.materialize",
            lambda: collection.insert_one(doc),
        )
        stored = await run_mongo_with_retry(
            "assistant_workspace.read_after_materialize",
            lambda: collection.find_one({"_id": fence_id}),
        )
        return stored or doc

    async def get_workspace(
        self, user_id: str, assistant_id: str
    ) -> dict[str, Any] | None:
        fence_id = assistant_workspace_fence_id(user_id, assistant_id)
        collection = await get_async_collection(self._collection_name)
        return await run_mongo_with_retry(
            "assistant_workspace.get",
            lambda: collection.find_one({"_id": fence_id}),
        )

    async def has_assistant_workspace(self, assistant_id: str) -> bool:
        collection = await get_async_collection(self._collection_name)
        found = await run_mongo_with_retry(
            "assistant_workspace.has_assistant",
            lambda: collection.find_one({"assistant_id": assistant_id}, {"_id": 1}),
        )
        return found is not None

    async def list_workspaces_by_sandbox_id(
        self, sandbox_id: str
    ) -> list[dict[str, Any]]:
        """Return every live workspace whose authoritative pointer names a box."""
        target = str(sandbox_id or "").strip()
        if not target:
            return []
        await self._ensure_indexes()
        collection = await get_async_collection(self._collection_name)

        async def _list() -> list[dict[str, Any]]:
            cursor = collection.find(
                {"current_sandbox_id": target, "deleted": {"$ne": True}}
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry(
            "assistant_workspace.list_by_sandbox_id",
            _list,
            fault_context={"sandbox_id": target},
        )

    async def list_dead_binding_probe_candidates(
        self,
        *,
        now_iso: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return workspace sandboxes whose durable lease is absent or stale."""
        page_limit = max(1, min(int(limit or 50), 500))
        query = {
            "deleted": {"$ne": True},
            "current_sandbox_id": {"$gt": ""},
            "$or": [
                {"current_sandbox_expires_at": {"$exists": False}},
                {"current_sandbox_expires_at": None},
                {"current_sandbox_expires_at": {"$lte": now_iso}},
            ],
        }
        collection = await get_async_collection(self._collection_name)

        async def _list() -> list[dict[str, Any]]:
            cursor = (
                collection.find(query)
                .sort("current_sandbox_expires_at", 1)
                .limit(page_limit)
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry(
            "assistant_workspace.list_dead_binding_probe_candidates",
            _list,
        )

    async def update_workspace(
        self, user_id: str, assistant_id: str, updates: dict[str, Any]
    ) -> bool:
        fence_id = assistant_workspace_fence_id(user_id, assistant_id)
        updates = {**updates, "updated_at": utcnow_iso()}
        collection = await get_async_collection(self._collection_name)
        result = await run_mongo_with_retry(
            "assistant_workspace.update",
            lambda: collection.update_one({"_id": fence_id}, {"$set": updates}),
        )
        return result.modified_count > 0

    async def compare_and_update_workspace(
        self,
        user_id: str,
        assistant_id: str,
        *,
        expected: dict[str, Any],
        updates: dict[str, Any],
    ) -> bool:
        fence_id = assistant_workspace_fence_id(user_id, assistant_id)
        updates = {**updates, "updated_at": utcnow_iso()}
        query = {"_id": fence_id, **dict(expected)}
        # Wake reserves a Session before it can supply that Session's generation.
        if "provisioning_sandbox_generation" in expected and expected["provisioning_sandbox_generation"] is None:
            query.pop("provisioning_sandbox_generation")
            query["$and"] = [
                *query.get("$and", []),
                {"$or": [
                    {"provisioning_sandbox_generation": None},
                    {"provisioning_sandbox_generation": {"$exists": False}},
                ]},
            ]
        collection = await get_async_collection(self._collection_name)
        result = await run_mongo_with_retry(
            "assistant_workspace.compare_and_update",
            lambda: collection.update_one(
                query,
                {"$set": updates},
            ),
        )
        return bool(
            getattr(result, "modified_count", 0)
            or getattr(result, "matched_count", 0)
        )

    # ── Durable shared-workspace provisioning authority (no TTL) ──

    async def mark_ready(
        self,
        user_id: str,
        assistant_id: str,
        *,
        provisioning_session_id: str,
        provisioning_sandbox_generation: str | None,
        sandbox_id: str,
        expires_at: str | None,
        runtime_identity: dict[str, Any] | None,
        profile_marker_key: str,
        profile_marker: dict[str, Any],
    ) -> bool:
        """Bind the started sandbox onto the workspace and publish READY.

        This is the single writer of ``current_sandbox_id``: the caller has
        already created and started the sandbox, so this transition both
        records that pointer and flips MATERIALIZING to READY under the
        assistant fence.  An already-READY row holding the same sandbox is
        treated as success so a retried startup is idempotent.
        """
        _ = user_id
        resolved_assistant_id = str(assistant_id or "").strip()
        resolved_provisioning_session_id = str(provisioning_session_id or "").strip()
        resolved_sandbox_id = str(sandbox_id or "").strip()
        if not resolved_assistant_id:
            raise ValueError("assistant_id is required")
        if not resolved_sandbox_id:
            raise ValueError("sandbox_id is required")
        if not resolved_provisioning_session_id:
            raise ValueError("provisioning_session_id is required")
        if not provisioning_sandbox_generation:
            raise ValueError("provisioning_sandbox_generation is required")
        fence_id = assistant_workspace_fence_id("", resolved_assistant_id)
        collection = await get_async_collection(self._collection_name)
        now = utcnow_iso()
        updates: dict[str, Any] = {
            "state": "READY",
            "current_sandbox_id": resolved_sandbox_id,
            "current_sandbox_expires_at": expires_at,
            "hibernated_at": None,
            "last_error": None,
            "runtime_identity": runtime_identity,
            "provisioning_session_id": None,
            "provisioning_sandbox_generation": None,
            f"assistant_profiles.{profile_marker_key}": dict(profile_marker),
            "updated_at": now,
        }
        query = {
            "_id": fence_id,
            "assistant_id": resolved_assistant_id,
            "deleted": {"$ne": True},
            "state": "MATERIALIZING",
            "provisioning_session_id": resolved_provisioning_session_id,
            "provisioning_sandbox_generation": provisioning_sandbox_generation,
        }
        result = await run_mongo_with_retry(
            "assistant_workspace.mark_ready",
            lambda: collection.update_one(query, {"$set": updates}),
        )
        if bool(getattr(result, "modified_count", 0)):
            return True
        current = await run_mongo_with_retry(
            "assistant_workspace.mark_ready.read_after_retry",
            lambda: collection.find_one(
                {
                    "_id": fence_id,
                    "assistant_id": resolved_assistant_id,
                    "deleted": {"$ne": True},
                    "state": "READY",
                    "current_sandbox_id": resolved_sandbox_id,
                    "provisioning_session_id": None,
                    "provisioning_sandbox_generation": None,
                    f"assistant_profiles.{profile_marker_key}.status": "ready",
                    f"assistant_profiles.{profile_marker_key}.sandbox_id": resolved_sandbox_id,
                }
            ),
        )
        return isinstance(current, dict)

    async def mark_post_commit_failure(
        self,
        user_id: str,
        assistant_id: str,
        *,
        sandbox_id: str,
        cleanup_confirmed: bool,
        failure_phase: str,
    ) -> bool:
        """Fence a failure that happened after the sandbox was bound.

        Confirmed cleanup clears the live pointer.  Unconfirmed cleanup retains
        it so wake can retry destroying that exact sandbox; neither branch
        permits a new sandbox while cleanup is ambiguous.

        ``cleanup_confirmed`` is a fact this layer cannot check and must be
        handed: the service above evaluates it with
        :func:`~astrabox.seams.sandbox_disposal.may_sever_last_name` against
        the destruction verdict and this exact sandbox id, so what arrives here
        is the result of the pairing rule rather than a caller's belief. This
        method's own contribution is the compare-and-set on
        ``current_sandbox_id``, which is what stops a true value computed
        about sandbox A from clearing a pointer that has since moved to B.
        """
        _ = user_id
        resolved_assistant_id = str(assistant_id or "").strip()
        resolved_sandbox_id = str(sandbox_id or "").strip()
        resolved_phase = str(failure_phase or "").strip().lower()
        if not resolved_assistant_id:
            raise ValueError("assistant_id is required")
        if not resolved_sandbox_id:
            raise ValueError("sandbox_id is required")
        if not resolved_phase:
            raise ValueError("failure_phase is required")
        if type(cleanup_confirmed) is not bool:
            raise ValueError("cleanup_confirmed must be boolean")
        fence_id = assistant_workspace_fence_id("", resolved_assistant_id)
        collection = await get_async_collection(self._collection_name)
        now = utcnow_iso()
        updates: dict[str, Any] = {
            "state": "RECOVERY_REQUIRED",
            "last_error": f"post_commit_{resolved_phase}",
            "post_commit_cleanup_pending": not cleanup_confirmed,
            "updated_at": now,
        }
        if cleanup_confirmed:
            updates.update(
                {
                    "current_sandbox_id": None,
                    "current_sandbox_expires_at": None,
                }
            )
        query = {
            "_id": fence_id,
            "assistant_id": resolved_assistant_id,
            "deleted": {"$ne": True},
            "state": {"$in": ["MATERIALIZING", "READY", "RECOVERY_REQUIRED"]},
            "current_sandbox_id": resolved_sandbox_id,
        }
        result = await run_mongo_with_retry(
            "assistant_workspace.mark_post_commit_failure",
            lambda: collection.update_one(query, {"$set": updates}),
        )
        # A match is not a convergence when this write is the one clearing the
        # pointer. `matched_count` without `modified_count` means the row
        # already looked like the target — which for the retaining branch is
        # genuinely "already converged", but for the clearing branch would
        # report a pointer as released on a write that changed nothing. Every
        # `$set` here includes `updated_at`, so a matched row is modified in
        # practice; the distinction is kept anyway, because the branch that
        # severs a name should not be the one relying on that.
        if bool(getattr(result, "modified_count", 0)):
            return True
        if not cleanup_confirmed and bool(getattr(result, "matched_count", 0)):
            return True
        convergence: dict[str, Any] = {
            "_id": fence_id,
            "assistant_id": resolved_assistant_id,
            "state": "RECOVERY_REQUIRED",
            "last_error": updates["last_error"],
            "post_commit_cleanup_pending": updates["post_commit_cleanup_pending"],
        }
        current = await run_mongo_with_retry(
            "assistant_workspace.mark_post_commit_failure.read_after_retry",
            lambda: collection.find_one(convergence),
        )
        return isinstance(current, dict)


    async def list_all_workspaces(
        self, limit: int = 500
    ) -> list[dict[str, Any]]:
        collection = await get_async_collection(self._collection_name)

        async def _list() -> list[dict[str, Any]]:
            cursor = (
                collection.find({})
                .sort("updated_at", -1)
                .limit(limit)
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("assistant_workspace.list_all", _list)

    async def list_user_workspaces(
        self, user_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        collection = await get_async_collection(self._collection_name)

        async def _list() -> list[dict[str, Any]]:
            cursor = (
                collection.find(
                    {
                        "$or": [
                            {"created_by_user_id": user_id},
                            {"user_id": user_id},
                        ]
                    }
                )
                .sort("updated_at", -1)
                .limit(limit)
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("assistant_workspace.list_user", _list)
