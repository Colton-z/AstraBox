"""Satori resource mapping onto the existing Agent and Session services.

Resources are Satori ``internal:`` links of this endpoint's Login,
``internal:astrabox/{agent_id}/{path}``, read through the standard proxy route:

* ``_tmp/{id}``: a file the caller uploaded and has not sent yet;
* ``{channel_id}/attachments/{id}``: a file a message in that Channel carried;
* ``{channel_id}/files/{path}``: a file in that Channel's workspace.

Every read checks the caller against the file's owner or the Channel's.
"""

from __future__ import annotations

import mimetypes
import posixpath
from typing import Any
from urllib.parse import quote, unquote

from satori.element import File, Image, Text
from satori.model import Channel, ChannelType, Event, Login, MessageObject, User
from starlette.datastructures import UploadFile

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.service_registry import get_agent_service, get_platform_service
from astrabox.core.service.orchestrator.session_runtime.attachments import (
    FILE, IMAGE, MAX_ATTACHMENT_BYTES, MAX_ATTACHMENTS, STAGED_TTL, SentAttachment, attachment_name,
    require_attachment_size,
)
from astrabox.core.service.orchestrator.session_runtime.ui_message import fold_message, public_part
from astrabox.core.service.orchestrator.user_actions import user_action_state
from astrabox.persistence.repository.input_attachment_repository import InputAttachmentRepository
from astrabox.persistence.repository.satori_repository import SatoriRepository
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.session_runtime_repository import SessionRuntimeRepository
from astrabox.persistence.sql.base import from_iso
from astrabox.seams.runtime_preparation import PreparationSubject, SessionUserActionContext

FEATURES = ["login.get", "channel.create", "channel.get", "channel.list", "message.create", "message.get",
            "message.list", "upload.create"]


def channel(row: dict[str, Any]) -> dict[str, Any]:
    return Channel(id=row["session_id"], type=ChannelType.TEXT, name=row.get("title")).dump()


def resource_url(agent_id: str, path: str) -> str:
    return f"internal:astrabox/{agent_id}/{path}"


def _resource(kind: str, src: str, title: str) -> str:
    return str(Image(src=src, title=title) if kind == IMAGE else File(src=src, title=title))


def _workspace_kind(path: str) -> str:
    return IMAGE if (mimetypes.guess_type(path)[0] or "").startswith("image/") else FILE


def message(row: dict[str, Any], sid: str, user: User, agent_id: str) -> dict[str, Any]:
    """A UI message as Satori content: its words, then the files it carried or wrote.

    A person's message names the files it sent; a reply names the workspace
    files its tools reported changing.
    """
    parts = row.get("parts", [])
    content = str(Text("\n".join(str(part.get("text") or "") for part in parts if part.get("type") == "text")))
    for item in (row.get("metadata") or {}).get("attachments") or ():
        content += _resource(str(item.get("kind") or FILE),
                             resource_url(agent_id, f"{sid}/attachments/{item['attachment_id']}"), str(item["name"]))
    seen: set[str] = set()
    for part in parts:
        if part.get("type") != "data-file-changes":
            continue
        for change in (part.get("data") or {}).get("files") or ():
            path = str(change.get("path") or "")
            if path.startswith("/") and path not in seen:
                seen.add(path)
                src = resource_url(agent_id, f"{sid}/files/{quote(path.lstrip('/'), safe='/')}")
                content += _resource(_workspace_kind(path), src, posixpath.basename(path))
    return MessageObject(id=row["id"], content=content, channel=Channel(id=sid), user=user).dump()


class SatoriService:
    def __init__(self, user: UserContext, agent_id: str) -> None:
        self.user = user
        self.agent_id = agent_id
        self.repo = SatoriRepository()
        self.runtime_repo = SessionRuntimeRepository()
        self.attachments = InputAttachmentRepository()
        self.scope = f"satori:{agent_id}"

    async def login(self) -> Login:
        agent = await get_agent_service().require_conversation_access(self.user, self.agent_id)
        return Login(platform="astrabox", user=User(id=self.agent_id, name=agent.get("name"), is_bot=True),
                     adapter="astrabox", features=FEATURES)

    async def owned(self, sid: str) -> dict[str, Any]:
        row = await SessionRepository().get_owned_session(sid, self.user.user_id)
        if row is None or row.get("agent_id") != self.agent_id or row.get("hidden") or row.get("deleted"):
            raise APIError(code="SESSION_NOT_FOUND", message="Channel not found", status_code=404)
        return row

    async def preparation(self, *, action_id: str | None = None) -> dict[str, Any]:
        return await user_action_state(SessionUserActionContext(
            session_id=None, principal_id=self.user.user_id, subject=PreparationSubject("agent", self.agent_id),
        ), action_id=action_id)

    async def require_prepared(self) -> None:
        state = await self.preparation()
        if not state["ready"]:
            raise APIError(code="TURN_PREPARATION_USER_ACTION_REQUIRED", message="Complete the required user operation",
                           status_code=409, data=state)

    async def call(self, action: str, body: dict[str, Any], *, key: str | None) -> Any:
        if action not in FEATURES:
            raise APIError(code="NOT_FOUND", message="Satori action is not supported", status_code=404)
        login = await self.login()
        platform = get_platform_service()
        if action == "login.get":
            self._fields(body)
            return login.dump()
        if action == "channel.create":
            self._fields(body, "guild_id", "data")
            if body.get("guild_id") != self.agent_id:
                self._invalid("guild_id must identify the endpoint's Agent")
            data = body.get("data", {})
            if not isinstance(data, dict) or set(data) - {"type", "name"} or data.get("type", 0) != ChannelType.TEXT:
                self._invalid("Only text Channels and an optional name are supported")
            await self.require_prepared()
            row = await get_agent_service().start_conversation(self.user, self.agent_id, idempotency_key=key)
            if data.get("name"):
                await platform.rename_session(self.user, row["session_id"], title=str(data["name"]))
            return channel({**row, "title": data.get("name")})
        if action == "channel.list":
            self._fields(body, "guild_id", "next")
            if body.get("guild_id") != self.agent_id:
                self._invalid("guild_id must identify the endpoint's Agent")
            rows = await self.repo.channels(self.user.user_id, self.agent_id, after=str(body.get("next") or ""))
            return {"data": [channel(row) for row in rows], "next": rows[-1]["session_id"] if len(rows) == 100 else None}
        sid = self._string(body, "channel_id")
        row = await self.owned(sid)
        if action == "channel.get":
            self._fields(body, "channel_id")
            return channel(row)
        if action == "message.create":
            self._fields(body, "channel_id", "content")
            content = self._string(body, "content")
            elements = MessageObject(id="", content=content).message
            if not elements or any(not isinstance(element, (Text, Image, File)) for element in elements):
                self._unsupported("message.create supports Satori text, img and file elements")
            text = "".join(element.text for element in elements if isinstance(element, Text))
            resources = [element for element in elements if isinstance(element, (Image, File))]
            if not text.strip() and not resources:
                self._invalid("Message is empty")
            if len(resources) > MAX_ATTACHMENTS:
                self._invalid(f"A message carries up to {MAX_ATTACHMENTS} files")
            attachments = [SentAttachment(self._staged_id(element.src), IMAGE if isinstance(element, Image) else FILE)
                           for element in resources]
            await self.require_prepared()
            receipt = await platform.session_runtime.send(
                self.user, sid, text, client_message_id=key, attachments=attachments, attachment_scope=self.scope,
            )
            return [MessageObject(id=receipt["client_message_id"], content=content, channel=Channel(id=sid),
                                  user=User(id=self.user.user_id), created_at=from_iso(receipt["accepted_at"])).dump()]
        if action in {"message.list", "message.get"}:
            self._fields(body, "channel_id", *(("message_id",) if action == "message.get" else ("next", "direction", "order", "limit")))
            if body.get("direction", "before") != "before" or body.get("order", "asc") not in {"asc", "desc"}:
                self._invalid("message.list supports direction=before and order=asc or desc")
            limit = body.get("limit", 50)
            if type(limit) is not int or limit < 1:
                self._invalid("limit must be a positive integer")
            result = await platform.get_messages(self.user, sid, before=body.get("next"), limit=min(limit, 100))
            messages = result["messages"]
            if action == "message.get":
                wanted = self._string(body, "message_id")
                # An id lookup is not restricted to the newest history page.
                while not any(item["id"] == wanted for item in messages) and result.get("before"):
                    result = await platform.get_messages(self.user, sid, before=result["before"], limit=100)
                    messages = result["messages"]
                messages = [item for item in messages if item["id"] == wanted]
                if not messages:
                    raise APIError(code="NOT_FOUND", message="Message not found", status_code=404)
            converted = [message(item, sid, login.user if item["role"] == "assistant" else User(id=self.user.user_id),
                                 self.agent_id) for item in messages]
            if action == "message.get":
                return converted[0]
            if body.get("order") == "desc":
                converted.reverse()
            return {"data": converted, "prev": result.get("before"), "next": result.get("before")}
        raise APIError(code="NOT_FOUND", message="Satori action is not supported", status_code=404)

    async def internal(self, action: str, data: dict[str, Any]) -> Any:
        if not isinstance(data, dict):
            self._invalid("internal data must be an object")
        if action in {"astrabox.preparation.get", "astrabox.preparation.perform"}:
            self._fields(data, *( ("action_id",) if action.endswith("perform") else ()))
            return await self.preparation(action_id=self._string(data, "action_id") if action.endswith("perform") else None)
        sid = self._string(data, "channel_id")
        await self.owned(sid)
        platform = get_platform_service()
        if action == "astrabox.history":
            self._fields(data, "channel_id", "before")
            return await platform.get_messages(self.user, sid, before=data.get("before"), limit=100)
        if action == "astrabox.conversation.get":
            self._fields(data, "channel_id")
            return await platform.get_session(self.user, sid)
        if action == "astrabox.files.list":
            self._fields(data, "channel_id", "path")
            path = data.get("path")
            if path is not None and not isinstance(path, str):
                self._invalid("path must be a string")
            listing = await platform.list_session_files(self.user, sid, path=path)
            for entry in listing.get("entries") or ():
                if entry.get("kind") == "file":
                    entry["url"] = resource_url(self.agent_id, f"{sid}/files/{quote(str(entry['path']).lstrip('/'), safe='/')}")
            return listing
        if action == "astrabox.interaction.answer":
            self._fields(data, "channel_id", "interaction_id", "answer")
            if not isinstance(data.get("answer"), dict):
                self._invalid("answer must be an object")
            await self.require_prepared()
            return await platform.session_runtime.answer(self.user, sid, self._string(data, "interaction_id"), data["answer"])
        if action == "astrabox.response.stop":
            self._fields(data, "channel_id")
            return await platform.session_runtime.interrupt(self.user, sid)
        raise APIError(code="NOT_FOUND", message="Satori internal action is not supported", status_code=404)

    async def event(self, row: dict[str, Any], login: Login) -> dict[str, Any]:
        sid, descriptor = row["session_id"], row["descriptor"]
        kind, rid = descriptor["kind"], descriptor.get("response_id")
        event = Event(type="internal", timestamp=from_iso(row["created_at"]), login=login,
                      channel=Channel(id=sid), sn=row["sn"])
        if kind == "state":
            event._type, event._data = "astrabox.state", descriptor["state"]
        else:
            if kind == "output":
                rows = await self.runtime_repo.read_output(sid, after_seq=descriptor["first_seq"] - 1, limit=500)
                parts = [part for item in rows if item["seq"] <= descriptor["through_seq"]
                         if (part := public_part(item["part"], shared=False)) is not None]
                event._type = "astrabox.output"
                event._data = {"session_id": sid, "response_id": rid, "first_seq": descriptor["first_seq"],
                               "through_seq": descriptor["through_seq"], "parts": parts}
            else:
                rows = await self.runtime_repo.output_for_responses(sid, [rid], through_seq=descriptor["through_seq"])
                parts = [part for item in rows if (part := public_part(item["part"], shared=False)) is not None]
                event.type = kind
                event.user = login.user
                event.message = MessageObject.parse(message(fold_message(rid, parts), sid, login.user, self.agent_id))
        return event.dump()

    async def upload(self, parts: list[tuple[str, Any]]) -> dict[str, str]:
        """Stage each uploaded file for the caller; ``{part name: resource link}``.

        A link stays valid for the caller until a message sends it or
        :data:`STAGED_TTL` passes, whichever comes first.
        """
        await self.login()
        names = [name for name, _value in parts]
        if not parts or len(set(names)) != len(names) or len(parts) > MAX_ATTACHMENTS:
            self._invalid(f"upload.create takes 1..{MAX_ATTACHMENTS} file parts with distinct names")
        links: dict[str, str] = {}
        for name, value in parts:
            if not isinstance(value, UploadFile) or not value.filename:
                self._invalid(f"{name} must be a file part with a filename")
            # One byte past the limit is enough to refuse a larger file.
            data = await value.read(MAX_ATTACHMENT_BYTES + 1)
            require_attachment_size(name, len(data))
            row = await self.attachments.stage(
                owner_user_id=self.user.user_id, scope=self.scope,
                name=attachment_name(value.filename, fallback=name),
                media_type=str(value.content_type or "application/octet-stream"), data=data, ttl=STAGED_TTL,
            )
            links[name] = resource_url(self.agent_id, f"_tmp/{row['attachment_id']}")
        return links

    async def proxy(self, url: str) -> tuple[Any, str]:
        """The resource ``url`` names, with its media type, for a caller allowed to read it.

        Returns the stored file's bytes, or an open workspace download.
        """
        if not url.startswith("internal:"):
            # This server proxies only its own resources; it lists no proxy_urls.
            raise APIError(code="FORBIDDEN", message="Only this Login's internal resources are proxied", status_code=403)
        platform_name, _sep, rest = url.removeprefix("internal:").partition("/")
        login_id, _sep, path = rest.partition("/")
        if not platform_name or not login_id or not path:
            self._invalid("internal resource links name a platform, a Login and a path")
        if platform_name != "astrabox" or login_id != self.agent_id:
            raise APIError(code="NOT_FOUND", message="Resource not found", status_code=404)
        await self.login()
        head, _sep, tail = path.partition("/")
        if head == "_tmp":
            row = await self.attachments.staged_file(tail, owner_user_id=self.user.user_id, scope=self.scope)
            if row is None:
                raise APIError(code="NOT_FOUND", message="Resource not found or expired", status_code=404)
            return row["data"], str(row["media_type"])
        await self.owned(head)
        kind, _sep, name = tail.partition("/")
        if kind == "attachments":
            row = await self.attachments.bound_file(head, name)
            if row is None:
                raise APIError(code="NOT_FOUND", message="Resource not found", status_code=404)
            return row["data"], str(row["media_type"])
        if kind == "files" and name:
            path = "/" + unquote(name)
            download = await get_platform_service().download_session_file(self.user, head, path=path)
            return download, mimetypes.guess_type(path)[0] or "application/octet-stream"
        raise APIError(code="NOT_FOUND", message="Resource not found", status_code=404)

    def _staged_id(self, src: str) -> str:
        prefix = resource_url(self.agent_id, "_tmp/")
        attachment_id = src.removeprefix(prefix) if src.startswith(prefix) else ""
        if not attachment_id or "/" in attachment_id:
            self._unsupported("img and file elements must name a resource upload.create returned")
        return attachment_id

    @staticmethod
    def _unsupported(message: str) -> None:
        raise APIError(code="INPUT_ATTACHMENT_UNSUPPORTED", message=message, status_code=400)

    @staticmethod
    def _invalid(message: str) -> None:
        raise APIError(code="INVALID_REQUEST", message=message, status_code=400)

    @classmethod
    def _fields(cls, body: dict[str, Any], *allowed: str) -> None:
        if set(body) - set(allowed):
            cls._invalid("Unsupported Satori parameters: " + ", ".join(sorted(set(body) - set(allowed))))

    @classmethod
    def _string(cls, body: dict[str, Any], key: str) -> str:
        value = body.get(key)
        if not isinstance(value, str) or not value.strip():
            cls._invalid(f"{key} must be a non-empty string")
        return value
