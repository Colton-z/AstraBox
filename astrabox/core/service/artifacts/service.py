"""Durable pages, scoped viewer authorization and their live document journal."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import parse_iso, plus_seconds_iso, utcnow, utcnow_iso
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.event_broker import SessionEventBroker
from astrabox.core.service.orchestrator.transcript_capability import _signing_key
from astrabox.persistence.repository.artifact_record_repository import empty_share
from astrabox.persistence.sql import from_iso, transaction
from astrabox.seams.sandbox import SandboxUnreachable
from astrabox.seams.sandbox_data import SandboxFiles

from . import limits
from .edits import apply_edits
from .manifest import path_segments, validate_manifest
from .rules import can_access

#: Journal events a live tail reads per query; a full batch is followed at once
#: by the next.
_LIVE_BATCH = 500

#: Expired journal events and refilled room buckets one room write deletes.
_EXPIRY_BATCH = 500


def invalid(message: str) -> APIError:
    return APIError(code="INVALID_REQUEST", message=message, status_code=400)


def public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v
        for k, v in row.items()
        if not k.startswith("_")
        and k
        not in {
            "revision",
            "event_seq",
            "document_count",
        }
    }


def opaque_viewer_id(artifact_id: str, user_id: str) -> str:
    subject = b"astrabox-artifact-viewer\x00" + artifact_id.encode() + b"\x00" + user_id.encode()
    return hmac.new(_signing_key(), subject, hashlib.sha256).hexdigest()


def guest_viewer_id(token: str, guest_id: str) -> str:
    subject = b"astrabox-artifact-guest\x00" + token.encode() + b"\x00" + guest_id.encode()
    return "guest_" + hmac.new(_signing_key(), subject, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class ArtifactViewer:
    artifact_id: str
    user: UserContext | None
    level: str
    id: str
    guest: bool
    token: str | None = None
    pinned_version: int | None = None

    def projection(self) -> dict[str, Any]:
        return {"level": self.level, "id": self.id, "guest": self.guest}


class ArtifactService:
    """Artifact authoring, sharing and the viewer runtime.

    A mutation of one artifact runs in one transaction that first locks the
    artifact's record, so versions, journal sequences and document counts are
    allocated by one writer at a time across replicas.
    """

    def __init__(
        self,
        *,
        records: Any,
        versions: Any,
        data: Any,
        events: Any,
        viewers: Any,
        budgets: Any,
        file_service: Any = None,
        sessions_repo: Any = None,
        runtime_manager: Any = None,
        public_base_url: str = "",
    ) -> None:
        self.records, self.versions, self.data = records, versions, data
        self.events, self.viewers, self.budgets = events, viewers, budgets
        self._broker = SessionEventBroker()
        self._live_counts: dict[str, int] = {}
        self._file_service = file_service
        self._sessions_repo = sessions_repo
        self._runtime_manager = runtime_manager
        self._public_base_url = public_base_url.rstrip("/")

    def marker(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "astrabox_artifact": {
                "id": row["artifact_id"],
                "version": row.get("version", row.get("current_version")),
                "title": row["title"],
                "kind": row["kind"],
            },
            "url": f"{self._public_base_url}/artifacts/{row['artifact_id']}",
        }

    @staticmethod
    def _not_found() -> APIError:
        return APIError(code="ARTIFACT_NOT_FOUND", message="artifact not found", status_code=404)

    async def owned(
        self,
        user: UserContext,
        artifact_id: str,
        *,
        conn: AsyncConnection | None = None,
        lock: bool = False,
    ) -> dict[str, Any]:
        row = await self._read_record(artifact_id, conn=conn, lock=lock)
        if not row or row["owner_id"] != user.user_id:
            raise self._not_found()
        return row

    async def _read_record(
        self, artifact_id: str, *, conn: AsyncConnection | None, lock: bool
    ) -> dict[str, Any] | None:
        """The record; with ``lock``, locked until ``conn``'s transaction ends."""
        if not lock:
            return await self.records.get(artifact_id, conn=conn)
        if conn is None:
            raise ValueError("locking an artifact requires the transaction's connection")
        return await self.records.lock(artifact_id, conn=conn)

    async def version(
        self,
        artifact_id: str,
        number: int,
        *,
        conn: AsyncConnection | None = None,
        content: bool = False,
    ) -> dict[str, Any]:
        """A version's metadata, with its ``content`` when ``content`` is true."""
        row = await self.versions.get(artifact_id, number, content=content, conn=conn)
        if row is None:
            raise APIError(
                code="ARTIFACT_VERSION_NOT_FOUND",
                message="artifact version not found",
                status_code=404,
            )
        return row

    @staticmethod
    def validate_content(content: Any, title: Any, kind: Any, language: Any) -> None:
        if (
            not isinstance(kind, str)
            or kind not in limits.KINDS
            or (kind == "code" and (not isinstance(language, str) or not language.strip()))
        ):
            raise APIError(
                code="ARTIFACT_KIND_INVALID",
                message="invalid kind; code requires language",
                status_code=400,
            )
        if not isinstance(content, str):
            raise invalid("content must be a string")
        if len(content.encode("utf-8")) > limits.CONTENT_BYTES:
            raise APIError(
                code="ARTIFACT_CONTENT_TOO_LARGE", message="content exceeds 4 MiB", status_code=413
            )
        if not isinstance(title, str) or not title.strip():
            raise invalid("title must be non-empty")
        if len(title) > limits.TITLE_CHARACTERS:
            raise APIError(
                code="ARTIFACT_TITLE_LIMIT", message="title exceeds 200 characters", status_code=413
            )

    @staticmethod
    def _version_row(record: dict[str, Any], content: str, author_kind: str) -> dict[str, Any]:
        """The metadata of ``record``'s current version, published with ``content``."""
        encoded = content.encode("utf-8")
        return {
            "artifact_id": record["artifact_id"],
            "version": record["current_version"],
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "byte_size": len(encoded),
            "kind": record["kind"],
            "title": record["title"],
            "language": record.get("language"),
            "manifest": record["manifest"],
            "author": {"user_id": record["owner_id"], "kind": author_kind},
            "created_at": utcnow_iso(),
        }

    async def _event(
        self, record: dict[str, Any], conn: AsyncConnection, kind: str, payload: dict[str, Any]
    ) -> None:
        """Journal an event at the next sequence of the locked ``record``."""
        record["event_seq"] += 1
        await self.events.append(
            record["artifact_id"],
            record["event_seq"],
            kind,
            payload,
            created_at=utcnow(),
            conn=conn,
        )

    async def _save(self, record: dict[str, Any], conn: AsyncConnection) -> None:
        revision = record["revision"]
        record.update(revision=revision + 1, updated_at=utcnow_iso())
        if not await self.records.save(record, expected_revision=revision, conn=conn):
            raise APIError(
                code="ARTIFACT_VERSION_CONFLICT",
                message="artifact changed concurrently",
                status_code=409,
            )

    async def _wake(self, artifact_id: str) -> None:
        await self._broker.publish(artifact_id, {"type": "wake"})

    async def content_input(
        self, args: dict[str, Any], binding: dict[str, Any] | None = None
    ) -> str:
        supplied = [name for name in ("content", "file_path") if name in args]
        if len(supplied) != 1:
            raise invalid("provide exactly one of content or file_path")
        if "content" in args:
            if not isinstance(args["content"], str):
                raise invalid("content must be a string")
            return args["content"]
        if not binding or not isinstance(args["file_path"], str) or not args["file_path"].strip():
            raise invalid("file_path requires a live platform binding")
        return await self._read_binding_file(binding, args["file_path"])

    async def create(
        self,
        user: UserContext,
        args: dict[str, Any],
        *,
        origin: dict[str, Any],
        binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        content = await self.content_input(args, binding)
        manifest = validate_manifest(args.get("capabilities", {}))
        self.validate_content(content, args.get("title"), args.get("kind"), args.get("language"))
        if not isinstance(args.get("description", ""), str):
            raise invalid("description must be a string")
        aid, now = "art_" + secrets.token_hex(12), utcnow_iso()
        record = {
            "artifact_id": aid,
            "owner_id": user.user_id,
            "title": args["title"],
            "description": args.get("description", ""),
            "kind": args["kind"],
            "language": args.get("language"),
            "current_version": 1,
            "manifest": manifest,
            "origin": origin,
            "share": empty_share(),
            "revision": 0,
            # The creation's version event below is the journal's first.
            "event_seq": 1,
            "document_count": 0,
            "created_at": now,
            "updated_at": now,
        }

        async def write(conn: AsyncConnection) -> None:
            await self.records.insert(record, conn=conn)
            await self.versions.insert(
                self._version_row(record, content, "agent"), content, conn=conn
            )
            await self.events.append(
                aid,
                1,
                "version",
                {
                    "version": 1,
                    "title": record["title"],
                    "kind": record["kind"],
                    "author_kind": "agent",
                },
                created_at=utcnow(),
                conn=conn,
            )

        await transaction(write)
        await self._wake(aid)
        return {**public(record), **self.marker(record)}

    async def update(
        self,
        user: UserContext,
        artifact_id: str,
        args: dict[str, Any],
        *,
        binding: dict[str, Any] | None = None,
        author_kind: str = "agent",
        viewer: ArtifactViewer | None = None,
    ) -> dict[str, Any]:
        if sum(key in args for key in ("content", "file_path", "edits")) != 1:
            raise invalid("provide exactly one of content, file_path or edits")
        content = None if "edits" in args else await self.content_input(args, binding)
        if "base_version" in args and (
            type(args["base_version"]) is not int or args["base_version"] < 1
        ):
            raise invalid("base_version must be a positive integer")

        async def write(conn: AsyncConnection) -> dict[str, Any]:
            record = await self.owned(user, artifact_id, conn=conn, lock=True)
            if viewer:
                checked, served = await self._resolve_record(record, viewer, conn)
                self.require(checked, served, "artifact")
                if checked.pinned_version is not None:
                    raise APIError(
                        code="ARTIFACT_NOT_WRITER",
                        message="pinned shares cannot publish",
                        status_code=403,
                    )
            if "base_version" in args and args["base_version"] != record["current_version"]:
                raise APIError(
                    code="ARTIFACT_VERSION_CONFLICT",
                    message="base_version is stale",
                    status_code=409,
                )
            if record["current_version"] >= limits.VERSION_COUNT:
                raise APIError(
                    code="ARTIFACT_VERSION_LIMIT",
                    message="artifact has 1000 versions",
                    status_code=409,
                )
            if "edits" in args:
                current = await self.version(
                    artifact_id, record["current_version"], conn=conn, content=True
                )
                text = apply_edits(current["content"], args["edits"])
            else:
                text = content
            for key in ("title", "description"):
                if key in args:
                    if not isinstance(args[key], str):
                        raise invalid(f"{key} must be a string")
                    record[key] = args[key]
            if "capabilities" in args:
                record["manifest"] = validate_manifest(args["capabilities"])
            self.validate_content(text, record["title"], record["kind"], record.get("language"))
            record["current_version"] += 1
            await self.versions.insert(
                self._version_row(record, text, author_kind), text, conn=conn
            )
            await self._event(
                record,
                conn,
                "version",
                {
                    "version": record["current_version"],
                    "title": record["title"],
                    "kind": record["kind"],
                    "author_kind": author_kind,
                },
            )
            await self._save(record, conn)
            return {**public(record), **self.marker(record)}

        result = await transaction(write)
        await self._wake(artifact_id)
        return result

    async def get(
        self, user: UserContext, artifact_id: str, number: int | None = None
    ) -> dict[str, Any]:
        record = await self.owned(user, artifact_id)
        version = await self.version(
            artifact_id,
            number if number is not None else record["current_version"],
            content=True,
        )
        return {
            **public(record),
            "version": public(version),
            "content": version["content"],
            **self.marker(version),
        }

    async def list(
        self,
        user: UserContext,
        *,
        cursor: str | None = None,
        limit: int = 50,
        session_id: str | None = None,
        assistant_id: str | None = None,
        agent_id: str | None = None,
        q: str | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(100, limit))
        after = None
        if cursor:
            try:
                position = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                if (
                    not isinstance(position, list)
                    or len(position) != 2
                    or any(not isinstance(v, str) for v in position)
                ):
                    raise ValueError()
                stamp = from_iso(position[0])
                if stamp is None:
                    raise ValueError()
                after = (stamp, position[1])
            except (ValueError, TypeError, UnicodeError) as exc:
                raise invalid("invalid artifact cursor") from exc
        rows = await self.records.list_owned(
            user.user_id,
            limit=limit + 1,
            session_id=session_id,
            assistant_id=assistant_id,
            agent_id=agent_id,
            search=q,
            after=after,
        )
        more, rows = len(rows) > limit, rows[:limit]
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps([rows[-1]["updated_at"], rows[-1]["artifact_id"]]).encode()
            )
            .decode()
            .rstrip("=")
            if more
            else None
        )
        return {
            "items": [{**public(row), **self.marker(row)} for row in rows],
            "has_more": more,
            "next_cursor": next_cursor,
        }

    async def patch(
        self, user: UserContext, artifact_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        if set(patch) - {"title", "description"} or any(
            not isinstance(v, str) for v in patch.values()
        ):
            raise invalid("metadata accepts title and description strings")

        async def write(conn: AsyncConnection) -> dict[str, Any]:
            row = await self.owned(user, artifact_id, conn=conn, lock=True)
            row.update(patch)
            self.validate_content("", row["title"], row["kind"], row.get("language"))
            await self._save(row, conn)
            return {**public(row), **self.marker(row)}

        return await transaction(write)

    async def list_versions(self, user: UserContext, artifact_id: str) -> list[dict[str, Any]]:
        await self.owned(user, artifact_id)
        rows = await self.versions.list_for_artifact(artifact_id, limit=limits.VERSION_COUNT)
        return [public(row) for row in rows]

    async def restore(self, user: UserContext, artifact_id: str, number: int) -> dict[str, Any]:
        record = await self.owned(user, artifact_id)
        version = await self.version(artifact_id, number, content=True)
        return await self.update(
            user,
            artifact_id,
            {
                "content": version["content"],
                "title": version["title"],
                "capabilities": version["manifest"],
                "base_version": record["current_version"],
            },
            author_kind="owner",
        )

    async def delete(self, user: UserContext, artifact_id: str) -> dict[str, Any]:
        """Delete the artifact with its versions, share, documents, journal and viewers."""
        if not await self.records.delete_owned(artifact_id, user.user_id):
            raise self._not_found()
        await self._wake(artifact_id)
        return {"deleted": True}

    async def get_share(self, user: UserContext, artifact_id: str) -> dict[str, Any]:
        return (await self.owned(user, artifact_id))["share"]

    async def enable_share(
        self,
        user: UserContext,
        artifact_id: str,
        *,
        access: str = "view",
        audience: str = "anyone",
        version: int | None = None,
        expires_in_seconds: int | None = None,
    ) -> dict[str, Any]:
        if access not in {"view", "interact"} or audience not in {"anyone", "signed_in"}:
            raise invalid("invalid share access or audience")
        if version is not None and (type(version) is not int or version < 1):
            raise invalid("version must be a positive integer")
        if expires_in_seconds is not None and (
            type(expires_in_seconds) is not int or expires_in_seconds <= 0
        ):
            raise invalid("expires_in_seconds must be positive")

        async def write(conn: AsyncConnection) -> dict[str, Any]:
            row = await self.owned(user, artifact_id, conn=conn, lock=True)
            if audience == "anyone" and {"sample", "agent"} & row["manifest"].keys():
                raise APIError(
                    code="ARTIFACT_SHARE_AUDIENCE_RESTRICTED",
                    message="model capabilities require a signed-in audience",
                    status_code=409,
                )
            if version is not None:
                await self.version(artifact_id, version, conn=conn)
            existing = row["share"]
            row["share"] = {
                "enabled": True,
                "token": existing["token"] or uuid.uuid4().hex + uuid.uuid4().hex,
                "access": access,
                "audience": audience,
                "version": version,
                "created_at": existing["created_at"] or utcnow_iso(),
                "expires_at": plus_seconds_iso(expires_in_seconds)
                if expires_in_seconds is not None
                else None,
            }
            await self.records.put_share(artifact_id, row["share"], conn=conn)
            await self._save(row, conn)
            return row["share"]

        return await transaction(write)

    async def disable_share(self, user: UserContext, artifact_id: str) -> dict[str, Any]:
        async def write(conn: AsyncConnection) -> dict[str, Any]:
            row = await self.owned(user, artifact_id, conn=conn, lock=True)
            row["share"] = {**row["share"], "enabled": False}
            # An artifact never shared has no link to disable.
            if row["share"]["token"]:
                await self.records.put_share(artifact_id, row["share"], conn=conn)
            await self._save(row, conn)
            return row["share"]

        result = await transaction(write)
        await self._wake(artifact_id)
        return result

    @staticmethod
    def _validate_share(row: dict[str, Any], token: str) -> None:
        share = row.get("share", {})
        valid = bool(token) and share.get("token") == token and share.get("enabled") is True
        pin = share.get("version")
        valid = valid and (pin is None or (type(pin) is int and pin >= 1))
        expiry = share.get("expires_at")
        if expiry is not None:
            valid = valid and utcnow() < parse_iso(expiry)
        if (
            not valid
            or share.get("access") not in {"view", "interact"}
            or share.get("audience") not in {"anyone", "signed_in"}
        ):
            raise APIError(
                code="ARTIFACT_SHARE_NOT_FOUND", message="share link not found", status_code=404
            )

    async def resolve_viewer(
        self,
        *,
        user: UserContext | None,
        artifact_id: str | None = None,
        token: str | None = None,
        guest_id: str | None = None,
        grant: dict[str, Any] | None = None,
    ) -> ArtifactViewer:
        if grant is not None:
            if not user or grant.get("artifact_id") != artifact_id:
                raise self._not_found()
            if grant.get("source") == "share" and isinstance(grant.get("token"), str):
                token = grant["token"]
            elif grant.get("source") != "owner":
                raise self._not_found()
        row = (
            await self.records.get_by_share_token(token)
            if token and grant is None
            else await self.records.get(artifact_id)
        )
        if not row:
            if token:
                raise APIError(
                    code="ARTIFACT_SHARE_NOT_FOUND", message="share link not found", status_code=404
                )
            raise self._not_found()
        aid = row["artifact_id"]
        level = "owner" if user and user.user_id == row["owner_id"] else "none"
        if grant is not None and grant.get("source") == "owner" and level != "owner":
            raise self._not_found()
        if not token and level == "none":
            raise self._not_found()
        vid = (
            opaque_viewer_id(aid, user.user_id)
            if user
            else guest_viewer_id(token or "", guest_id if isinstance(guest_id, str) else "")
        )
        seed = ArtifactViewer(aid, user, level, vid, user is None, token=token)
        viewer, _ = await self._resolve_record(row, seed)
        if not user and (
            not isinstance(guest_id, str)
            or not guest_id
            or len(guest_id) > 144
            or not all(c.isalnum() or c in "_-" for c in guest_id)
        ):
            raise invalid("invalid guest_id")
        # A signed-in viewer is registered for user.profiles. The artifact's
        # foreign key, not its lock, keeps the row from outliving a concurrent
        # delete, so registration never waits behind a write to the artifact.
        if user and not await self.viewers.register(
            aid, vid, user_id=user.user_id, display_name=user.display_name or "Viewer"
        ):
            if token:
                raise APIError(
                    code="ARTIFACT_SHARE_NOT_FOUND", message="share link not found", status_code=404
                )
            raise self._not_found()
        return viewer

    async def _resolve_record(
        self,
        row: dict[str, Any],
        viewer: ArtifactViewer,
        conn: AsyncConnection | None = None,
        *,
        content: bool = False,
        versions: dict[int, dict[str, Any]] | None = None,
    ) -> tuple[ArtifactViewer, dict[str, Any]]:
        """Authorize ``viewer`` against ``row``'s current share and manifest; the served version.

        The served version carries the current manifest, and its content only
        when ``content`` is true. ``versions`` caches served versions by number
        for a caller that resolves repeatedly: versions are immutable.
        """
        level = "owner" if viewer.user and row["owner_id"] == viewer.user.user_id else "none"
        pin = None
        if viewer.token:
            self._validate_share(row, viewer.token)
            share = row["share"]
            pin = share["version"]
            if level != "owner":
                level = share["access"] if viewer.user else "view"
        elif level != "owner":
            raise self._not_found()
        number = pin if pin is not None else row["current_version"]
        served = versions.get(number) if versions is not None else None
        if served is None:
            served = await self.version(row["artifact_id"], number, conn=conn, content=content)
            if versions is not None:
                versions[number] = served
        # The served page is pinned content; live authorization is the owner's current policy.
        served = {**served, "manifest": row["manifest"]}
        if (
            viewer.guest
            and viewer.token
            and (
                row["share"]["audience"] != "anyone"
                or {"sample", "agent"} & served["manifest"].keys()
            )
        ):
            raise APIError(
                code="ARTIFACT_SIGN_IN_REQUIRED",
                message="this artifact requires sign-in",
                status_code=401,
            )
        return ArtifactViewer(
            viewer.artifact_id,
            viewer.user,
            level,
            viewer.id,
            viewer.guest,
            viewer.token,
            pin,
        ), served

    async def refresh(
        self,
        viewer: ArtifactViewer,
        *,
        conn: AsyncConnection | None = None,
        lock: bool = False,
        content: bool = False,
    ) -> tuple[ArtifactViewer, dict[str, Any], dict[str, Any]]:
        """Re-authorize ``viewer`` now: the checked viewer, the record and the served version.

        The served version includes its content only when ``content`` is true.
        """
        row = await self._read_record(viewer.artifact_id, conn=conn, lock=lock)
        if row is None:
            if viewer.token:
                raise APIError(
                    code="ARTIFACT_SHARE_NOT_FOUND", message="share link not found", status_code=404
                )
            raise self._not_found()
        checked, served = await self._resolve_record(row, viewer, conn, content=content)
        return checked, row, served

    @staticmethod
    def require(viewer: ArtifactViewer, version: dict[str, Any], capability: str) -> None:
        if capability not in version["manifest"]:
            raise APIError(
                code="ARTIFACT_CAPABILITY_NOT_DECLARED",
                message=f"{capability} is not declared",
                status_code=403,
            )

    async def runtime(self, viewer: ArtifactViewer) -> dict[str, Any]:
        viewer, record, served = await self.refresh(viewer, content=True)
        artifact = {
            k: record[k] for k in ("artifact_id", "description", "created_at", "updated_at")
        }
        artifact.update(
            title=served["title"],
            kind=served["kind"],
            language=served.get("language"),
            current_version=served["version"],
            manifest=served["manifest"],
            origin=(
                record["origin"]
                if viewer.level == "owner"
                else {
                    k: v for k, v in record["origin"].items()
                    if not viewer.guest and k in {"session_kind", "agent_id", "assistant_id"}
                }
            ),
        )
        if viewer.token:
            artifact["share"] = {
                k: record["share"][k] for k in ("version", "access", "audience")
            }
        return {
            "artifact": artifact,
            "version": served["version"],
            "content": served["content"],
            "manifest": served["manifest"],
            "viewer": viewer.projection(),
            "pinned": viewer.pinned_version is not None,
            "after_seq": record["event_seq"],
        }

    async def publish(
        self, viewer: ArtifactViewer, content: str, base_version: int
    ) -> dict[str, Any]:
        checked, _, served = await self.refresh(viewer)
        self.require(checked, served, "artifact")
        if checked.level != "owner" or checked.pinned_version is not None or checked.user is None:
            raise APIError(
                code="ARTIFACT_NOT_WRITER",
                message="only owners on unpinned views may publish",
                status_code=403,
            )
        return await self.update(
            checked.user,
            checked.artifact_id,
            {"content": content, "base_version": base_version},
            author_kind="owner",
            viewer=viewer,
        )

    @staticmethod
    def _data_path(path: str, *, collection: bool = False) -> list[str]:
        parts = path_segments(path)
        if len(parts) % 2 != (1 if collection else 0):
            raise invalid("collections have odd and documents even path segments")
        return parts

    @staticmethod
    def _data_access(
        viewer: ArtifactViewer, version: dict[str, Any], path: str, *, write: bool = False
    ) -> None:
        if not can_access(
            version["manifest"]["db"].get("rules", []),
            path,
            viewer.level,
            viewer.id if not viewer.guest else None,
            write=write,
            guest=viewer.guest,
        ):
            raise APIError(
                code="ARTIFACT_DATA_DENIED", message="document access denied", status_code=403
            )

    async def data_get(self, viewer: ArtifactViewer, path: str) -> dict[str, Any]:
        self._data_path(path)
        viewer, _, version = await self.refresh(viewer)
        self.require(viewer, version, "db")
        self._data_access(viewer, version, path)
        row = await self.data.get(viewer.artifact_id, path)
        return {"path": path, "exists": row is not None, "data": row["data"] if row else None}

    @staticmethod
    def _json_size(data: Any) -> int:
        try:
            return len(
                json.dumps(
                    data, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                ).encode()
            )
        except (TypeError, ValueError) as exc:
            raise invalid("data must be finite JSON") from exc

    async def data_write(
        self, viewer: ArtifactViewer, writes: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if not isinstance(writes, list) or not writes:
            raise invalid("writes must be a non-empty array")
        requested: list[Any] = list(writes)

        async def write(conn: AsyncConnection) -> dict[str, Any]:
            checked, record, version = await self.refresh(viewer, conn=conn, lock=True)
            self.require(checked, version, "db")
            results = []
            for item in requested:
                if not isinstance(item, dict) or item.get("action") not in {
                    "set",
                    "update",
                    "delete",
                }:
                    raise invalid("write action must be set, update or delete")
                path, action = item.get("path"), item["action"]
                self._data_path(path)
                self._data_access(checked, version, path, write=True)
                if action == "delete":
                    if await self.data.delete(viewer.artifact_id, path, conn=conn):
                        record["document_count"] -= 1
                else:
                    data = item.get("data")
                    if not isinstance(data, dict):
                        raise invalid("db documents must be JSON objects")
                    old = await self.data.get(viewer.artifact_id, path, conn=conn)
                    if action == "update":
                        if not old:
                            raise APIError(
                                code="ARTIFACT_DATA_NOT_FOUND",
                                message="document not found",
                                status_code=404,
                            )
                        data = {**old["data"], **data}
                    if self._json_size(data) > limits.DOCUMENT_BYTES:
                        raise APIError(
                            code="ARTIFACT_DATA_LIMIT",
                            message="document exceeds 256 KiB",
                            status_code=413,
                        )
                    if not old:
                        record["document_count"] += 1
                        if record["document_count"] > limits.DOCUMENT_COUNT:
                            raise APIError(
                                code="ARTIFACT_DATA_COUNT_LIMIT",
                                message="artifact exceeds 10000 documents",
                                status_code=413,
                            )
                    await self.data.put(
                        viewer.artifact_id,
                        path,
                        collection="/".join(path_segments(path)[:-1]),
                        data=data,
                        updated_by=checked.id,
                        now=utcnow(),
                        conn=conn,
                    )
                await self._event(record, conn, "data", {"path": path, "op": action})
                results.append({"path": path, "op": action})
            await self._save(record, conn)
            return {"writes": results}

        result = await transaction(write)
        await self._wake(viewer.artifact_id)
        return result

    async def data_add(
        self, viewer: ArtifactViewer, collection: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        self._data_path(collection, collection=True)
        path = f"{collection}/{uuid.uuid4().hex}"
        await self.data_write(viewer, [{"action": "set", "path": path, "data": data}])
        return {"id": path.rsplit("/", 1)[1], "path": path}

    async def data_query(
        self,
        viewer: ArtifactViewer,
        collection: str,
        *,
        where: list[dict[str, Any]] | None = None,
        order_by: list[dict[str, Any]] | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        self._data_path(collection, collection=True)
        where, order_by = where or [], order_by or []
        validate_query(where, order_by, limit)
        viewer, _, version = await self.refresh(viewer)
        self.require(viewer, version, "db")
        # The collection arrives in path order. Access rules and predicates are
        # applied here because they follow this module's JSON semantics (the
        # number/boolean distinction, the type order of json_sort_key), which
        # differ from PostgreSQL's jsonb comparison.
        rows = await self.data.in_collection(viewer.artifact_id, collection)
        rows = [
            r
            for r in rows
            if can_access(
                version["manifest"]["db"].get("rules", []),
                r["path"],
                viewer.level,
                viewer.id if not viewer.guest else None,
                guest=viewer.guest,
            )
            and matches_where(r["data"], where)
        ]
        for order in reversed(order_by):
            rows = [r for r in rows if field_value(r["data"], order["field"]) is not _MISSING]
            rows.sort(
                key=lambda r: json_sort_key(field_value(r["data"], order["field"])),
                reverse=order.get("direction", "asc") == "desc",
            )
        return {
            "items": [
                {"id": r["path"].rsplit("/", 1)[1], "path": r["path"], "data": r["data"]}
                for r in rows[:limit]
            ]
        }

    async def profiles(self, viewer: ArtifactViewer, ids: list[str]) -> dict[str, Any]:
        viewer, _, version = await self.refresh(viewer)
        self.require(viewer, version, "user")
        if len(ids) > 100:
            raise invalid("at most 100 profile ids")
        rows = await self.viewers.profiles(viewer.artifact_id, ids)
        return {
            "profiles": [{"id": r["viewer_id"], "display_name": "" if viewer.guest else r["display_name"]} for r in rows]
        }

    @staticmethod
    def _viewer_budget_id(viewer: ArtifactViewer) -> str:
        return ("user:" + viewer.user.user_id) if viewer.user else viewer.id

    @staticmethod
    def _rate_limited() -> APIError:
        return APIError(code="ARTIFACT_LIVE_RATE_LIMITED", message="artifact live request limit reached", status_code=429)

    async def _room_token(self, viewer: ArtifactViewer, conn: AsyncConnection) -> None:
        key = "room_budget:" + opaque_viewer_id("room-budget", self._viewer_budget_id(viewer))
        if not await self.budgets.take_room_token(
            key,
            now=utcnow(),
            burst=limits.ROOM_WRITE_BURST,
            per_second=limits.ROOM_WRITES_PER_SECOND,
            conn=conn,
        ):
            raise self._rate_limited()

    async def _expire_live_rows(self) -> None:
        """Delete a batch of expired room and presence events and of refilled room buckets.

        Every room or presence write runs this first, so expired rows of every
        artifact and viewer are removed while room traffic continues anywhere,
        not only when that artifact or viewer writes again.
        """
        now = utcnow()
        await self.events.prune(
            rooms_before=now - timedelta(seconds=limits.ROOM_RETENTION_SECONDS),
            presence_before=now - timedelta(seconds=limits.PRESENCE_SECONDS),
            limit=_EXPIRY_BATCH,
        )
        refill = limits.ROOM_WRITE_BURST / limits.ROOM_WRITES_PER_SECOND
        await self.budgets.prune_room_tokens(
            before=now - timedelta(seconds=refill), limit=_EXPIRY_BATCH
        )

    async def room_write(
        self,
        viewer: ArtifactViewer,
        room: str,
        *,
        topic: str | None = None,
        data: Any = None,
        presence: bool = False,
    ) -> dict[str, Any]:
        if not room or len(room) > 200 or "/" in room or "\x00" in room:
            raise invalid("invalid room")
        if self._json_size(data) > limits.ROOM_BYTES:
            raise APIError(
                code="ARTIFACT_ROOM_LIMIT", message="room payload exceeds 16 KiB", status_code=413
            )

        async def write(conn: AsyncConnection) -> dict[str, Any]:
            checked, record, version = await self.refresh(viewer, conn=conn, lock=True)
            self.require(checked, version, "room")
            if not presence:
                if not isinstance(topic, str) or not topic or "/" in topic or "\x00" in topic:
                    raise invalid("invalid room topic")
                required = version["manifest"]["room"].get("topics", {}).get(topic, "owner")
                if limits.LEVELS[checked.level] < limits.LEVELS[required]:
                    raise APIError(
                        code="ARTIFACT_ROOM_DENIED",
                        message="room topic access denied",
                        status_code=403,
                    )
            await self._room_token(checked, conn)
            payload = (
                {"room": room, "viewer_id": checked.id, "state": data, "guest": checked.guest}
                if presence
                else {
                    "room": room,
                    "topic": topic,
                    "data": data,
                    "from": checked.id,
                    "from_kind": "guest" if checked.guest else "user",
                }
            )
            await self._event(record, conn, "presence" if presence else "room", payload)
            await self._save(record, conn)
            return {"seq": record["event_seq"]}

        await self._expire_live_rows()
        result = await transaction(write)
        await self._wake(viewer.artifact_id)
        return result

    async def live(self, viewer: ArtifactViewer, after_seq: int = 0) -> AsyncIterator[str]:
        """Tail the artifact's journal after ``after_seq`` as SSE, as ``viewer`` may see it.

        Each poll reads the record, which re-authorizes the viewer against the
        current share and manifest and reveals deletion. The journal is read
        only once the record's sequence has passed what was already read, and
        only after the position reached. The served version's metadata is read
        again only when the served version changes; content is never read.
        """
        if after_seq < 0:
            raise invalid("after_seq must be nonnegative")
        admission = self._viewer_budget_id(viewer)
        if self._live_counts.get(admission, 0) >= limits.LIVE_STREAMS_PER_VIEWER:
            raise self._rate_limited()
        self._live_counts[admission] = self._live_counts.get(admission, 0) + 1
        queue = None
        loop = asyncio.get_running_loop()
        heartbeat = loop.time()
        presence_seen: dict[tuple[str, str], tuple[dict[str, Any], float]] = {}
        served: dict[int, dict[str, Any]] = {}
        # Every event at or before this sequence has been read.
        read_through = after_seq
        try:
            queue = await self._broker.subscribe(viewer.artifact_id)
            record = await self.records.get(viewer.artifact_id)
            if record is None:
                yield sse("deleted", {}, after_seq + 1)
                return
            checked, version = await self._resolve_record(record, viewer, versions=served)
            if "room" in version["manifest"] and after_seq:
                existing = await self.events.presence(
                    viewer.artifact_id,
                    through_seq=after_seq,
                    since=utcnow() - timedelta(seconds=limits.PRESENCE_SECONDS),
                )
                for row in existing:
                    payload = row["payload"]
                    expiry = (
                        limits.PRESENCE_SECONDS
                        - (utcnow() - parse_iso(row["created_at"])).total_seconds()
                    )
                    presence_seen[(payload["room"], payload["viewer_id"])] = (
                        payload,
                        loop.time() + expiry,
                    )
                for payload, _ in presence_seen.values():
                    yield sse("presence", payload, after_seq)
            while True:
                record = await self.records.get(viewer.artifact_id)
                if record is None:
                    # Absence is durable deletion evidence after the cascade removed the journal.
                    yield sse("deleted", {}, after_seq + 1)
                    return
                checked, version = await self._resolve_record(record, viewer, versions=served)
                rows: list[dict[str, Any]] = []
                if record["event_seq"] > read_through:
                    rows = await self.events.after(
                        viewer.artifact_id, after_seq, limit=_LIVE_BATCH
                    )
                for row in rows:
                    after_seq = row["seq"]
                    kind, payload = row["kind"], row["payload"]
                    visible = True
                    if kind == "version":
                        visible = checked.pinned_version is None
                    elif kind == "data":
                        visible = "db" in version["manifest"] and can_access(
                            version["manifest"]["db"].get("rules", []),
                            payload["path"],
                            checked.level,
                            checked.id if not checked.guest else None,
                            guest=checked.guest,
                        )
                    elif kind in {"room", "presence"}:
                        age = (utcnow() - parse_iso(row["created_at"])).total_seconds()
                        visible = (
                            "room" in version["manifest"] and age < limits.ROOM_RETENTION_SECONDS
                        )
                        if kind == "presence":
                            visible = visible and age < limits.PRESENCE_SECONDS
                            if visible:
                                presence_seen[(payload["room"], payload["viewer_id"])] = (
                                    payload,
                                    loop.time() + limits.PRESENCE_SECONDS - age,
                                )
                    if visible:
                        yield sse(kind, payload, after_seq)
                for peer, (payload, expiry) in list(presence_seen.items()):
                    if loop.time() >= expiry:
                        # Expiry changes presence state without allocating a journal sequence.
                        yield sse("presence", {**payload, "state": None}, after_seq)
                        del presence_seen[peer]
                if len(rows) == _LIVE_BATCH:
                    read_through = after_seq
                    continue
                # The record's sequence was read before the journal, so every
                # event up to it was committed by then and the read included it.
                read_through = max(read_through, after_seq, record["event_seq"])
                if loop.time() - heartbeat >= limits.LIVE_HEARTBEAT_SECONDS:
                    yield ": heartbeat\n\n"
                    heartbeat = loop.time()
                try:
                    await asyncio.wait_for(queue.get(), timeout=limits.LIVE_POLL_SECONDS)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._live_counts[admission] -= 1
            if not self._live_counts[admission]:
                del self._live_counts[admission]
            if queue is not None:
                await self._broker.unsubscribe(viewer.artifact_id, queue)

    async def _read_binding_file(self, binding: dict[str, Any], path: str) -> str:
        from astrabox.core.service.orchestrator.session_file_service import SessionFileService

        session_id = binding.get("session_id")
        if session_id:
            session = await self._sessions_repo.get_session(session_id)
            if not session or session.get("user_id") != binding.get("user_id"):
                raise invalid("binding has no live owned Session")
            async with self._file_service._resolve_session_context(
                UserContext(binding["user_id"]), session_id, prefetched_session=session
            ) as (resolved, sandbox, root):
                filesystem = SessionFileService._resolve_filesystem(
                    sandbox, resolved.get("runtime_identity")
                )
                return await self._read_guarded_file(filesystem, root, path)
        if (
            binding.get("scope_kind") != "assistant_workspace"
            or not binding.get("sandbox_id")
            or not binding.get("root_path")
        ):
            raise invalid("file_path requires a bound live workspace")
        sandbox = await self._runtime_manager.connect_sandbox_only(binding["sandbox_id"])
        try:
            from astrabox.core.service.orchestrator.runtime.conversation_identity import (
                identity_file_root_dir,
            )

            identity = binding.get("runtime_identity")
            root = identity_file_root_dir(identity) if identity else binding["root_path"]
            filesystem = SessionFileService._resolve_filesystem(sandbox, identity)
            return await self._read_guarded_file(filesystem, root, path)
        finally:
            await sandbox.close()

    async def _read_guarded_file(self, filesystem: SandboxFiles, root: str, path: str) -> str:
        target = self._file_service._resolve_client_path(root, path)
        info = await self._file_service._assert_path_resolves_within_root(
            filesystem, root_path=root, target_path=target
        )
        if info is None or info.kind == "directory":
            raise invalid("file_path must be a file")
        if info.size > limits.CONTENT_BYTES:
            raise APIError(
                code="ARTIFACT_CONTENT_TOO_LARGE", message="file exceeds 4 MiB", status_code=413
            )
        stream = filesystem.read_chunks(target)
        chunks, size = [], 0
        try:
            async for chunk in stream:
                size += len(chunk)
                if size > limits.CONTENT_BYTES:
                    raise APIError(
                        code="ARTIFACT_CONTENT_TOO_LARGE",
                        message="file exceeds 4 MiB",
                        status_code=413,
                    )
                chunks.append(bytes(chunk))
        except SandboxUnreachable as exc:
            # An APIError too, but its words are the transport's and stay in the log.
            raise self._file_service._filesystem_api_error(
                exc, op_label="artifact content", path=target
            ) from exc
        except APIError:
            raise
        except Exception as exc:
            raise self._file_service._filesystem_api_error(
                exc, op_label="artifact content", path=target
            ) from exc
        finally:
            await stream.aclose()
        try:
            return b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise invalid("artifact files must be UTF-8") from exc


def sse(kind: str, payload: dict[str, Any], seq: int | None = None) -> str:
    return (
        f"id: {seq}\n" if seq is not None else ""
    ) + f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False, allow_nan=False)}\n\n"


_MISSING = object()


def field_value(data: dict[str, Any], field: str) -> Any:
    value: Any = data
    for key in field.split("."):
        if not isinstance(value, dict) or key not in value:
            return _MISSING
        value = value[key]
    return value


def json_sort_key(value: Any) -> tuple[int, Any]:
    if value is None:
        return (0, 0)
    if isinstance(value, bool):
        return (1, value)
    if isinstance(value, (int, float)):
        return (2, value)
    if isinstance(value, str):
        return (3, value)
    return (4, json.dumps(value, sort_keys=True))


def validate_query(where: Any, order_by: Any, limit: Any) -> None:
    if type(limit) is not int or not 1 <= limit <= limits.DOCUMENT_COUNT:
        raise invalid("query limit must be between 1 and 10000")
    if not isinstance(where, list) or not isinstance(order_by, list):
        raise invalid("where and order_by must be arrays")
    for condition in where:
        if (
            not isinstance(condition, dict)
            or set(condition) != {"field", "op", "value"}
            or not isinstance(condition["field"], str)
            or not condition["field"]
            or not isinstance(condition["op"], str)
            or condition["op"] not in {"==", "!=", "<", "<=", ">", ">=", "in", "array-contains"}
        ):
            raise invalid("where entries have field, op and value")
        if condition["op"] == "in" and not isinstance(condition["value"], list):
            raise invalid("in requires an array")
        ArtifactService._json_size(condition["value"])
    for order in order_by:
        if (
            not isinstance(order, dict)
            or set(order) - {"field", "direction"}
            or not isinstance(order.get("field"), str)
            or not order["field"]
            or order.get("direction", "asc") not in {"asc", "desc"}
        ):
            raise invalid("order_by entries have field and asc/desc direction")


def json_equal(left: Any, right: Any) -> bool:
    """Compare JSON numbers together while keeping booleans distinct, including nested values."""
    if type(left) in {int, float} and type(right) in {int, float}:
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(json_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(json_equal(v, right[k]) for k, v in left.items())
    return left == right


def matches_where(data: dict[str, Any], where: list[dict[str, Any]]) -> bool:
    for condition in where:
        left, right, op = field_value(data, condition["field"]), condition["value"], condition["op"]
        if left is _MISSING:
            return False
        if op == "array-contains":
            matched = isinstance(left, list) and any(json_equal(item, right) for item in left)
        elif op == "in":
            matched = any(json_equal(left, item) for item in right)
        elif op == "==":
            matched = json_equal(left, right)
        elif op == "!=":
            matched = not json_equal(left, right)
        else:
            comparable = type(left) is type(right) or (
                type(left) in {int, float} and type(right) in {int, float}
            )
            if not comparable or isinstance(left, (dict, list)):
                return False
            try:
                matched = {
                    "<": lambda: left < right,
                    "<=": lambda: left <= right,
                    ">": lambda: left > right,
                    ">=": lambda: left >= right,
                }[op]()
            except TypeError:
                return False
        if not matched:
            return False
    return True
