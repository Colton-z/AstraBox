"""Satori resource mapping onto the existing Agent and Session services."""

from __future__ import annotations

from typing import Any

from satori.element import Text
from satori.model import Channel, ChannelType, Event, Login, MessageObject, User

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.service_registry import get_agent_service, get_platform_service
from astrabox.core.service.orchestrator.session_runtime.ui_message import fold_message, public_part
from astrabox.core.service.orchestrator.user_actions import user_action_state
from astrabox.persistence.repository.satori_repository import SatoriRepository
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.session_runtime_repository import SessionRuntimeRepository
from astrabox.persistence.sql.base import from_iso
from astrabox.seams.runtime_preparation import PreparationSubject, SessionUserActionContext

FEATURES = ["login.get", "channel.create", "channel.get", "channel.list", "message.create", "message.get", "message.list"]


def channel(row: dict[str, Any]) -> dict[str, Any]:
    return Channel(id=row["session_id"], type=ChannelType.TEXT, name=row.get("title")).dump()


def message(row: dict[str, Any], sid: str, user: User) -> dict[str, Any]:
    text = "\n".join(str(part.get("text") or "") for part in row.get("parts", []) if part.get("type") == "text")
    return MessageObject(id=row["id"], content=str(Text(text)), channel=Channel(id=sid), user=user).dump()


class SatoriService:
    def __init__(self, user: UserContext, agent_id: str) -> None:
        self.user = user
        self.agent_id = agent_id
        self.repo = SatoriRepository()
        self.runtime_repo = SessionRuntimeRepository()

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
            if not elements or any(not isinstance(element, Text) for element in elements):
                self._invalid("message.create supports Satori text only; non-text elements are refused")
            text = "".join(element.text for element in elements if isinstance(element, Text))
            if not text.strip():
                self._invalid("Message is empty")
            await self.require_prepared()
            receipt = await platform.session_runtime.send(self.user, sid, text, client_message_id=key)
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
            converted = [message(item, sid, login.user if item["role"] == "assistant" else User(id=self.user.user_id)) for item in messages]
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
                event.message = MessageObject.parse(message(fold_message(rid, parts), sid, login.user))
        return event.dump()

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
