"""Synthetic deployment integration for the live lifecycle case; no auth service.

Test controls and receipts live under the server's state directory: the test
writes which Sessions to refuse or to hold for a user action, which Agents'
conversations wait on a user action at startup and how long turn preparation
takes, and reads each turn preparation and which
Agents and Assistants were synchronized, with whether the prepared Skills and
packages were already in the box when they were.
"""
import asyncio
import json
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fastapi import FastAPI
from sqlalchemy import Column, MetaData, Table, Text, func, select

from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import get_asserted_user_context
from astrabox.persistence.provider import get_persistence
from astrabox.runtime_preparation import issue_preparation_commit_guard
from astrabox.seams.runtime_preparation import (
    PreparationCommitGuard,
    PreparationSubject,
    PreparationUserActionRequired,
    RuntimeFile,
    RuntimeMaterials,
    RuntimePreparationPlan,
    RuntimePreparationProvider,
    SessionUserActionCommand,
    SessionUserActionState,
)

STATE = Path("/data")
REFUSALS = STATE / "runtime-preparation-refusals.json"
USER_ACTIONS = STATE / "runtime-preparation-user-actions.json"
STARTUP_USER_ACTIONS = STATE / "runtime-preparation-startup-user-actions.json"
TURN_DELAY = STATE / "runtime-preparation-turn-delay.json"
TURN_RECEIPTS = STATE / "runtime-preparation-turns.jsonl"
SYNCHRONIZED = STATE / "runtime-preparation-synchronized.jsonl"
ACTION_URL = "https://example.com/authorize"

_GUARD_METADATA = MetaData()
_GUARD_STATE = Table(
    "runtime_preparation_fixture_state", _GUARD_METADATA,
    Column("nonce", Text, primary_key=True),
    Column("subject_kind", Text, primary_key=True),
    Column("subject_id", Text, primary_key=True),
    Column("marker", Text, primary_key=True),
)


@dataclass
class _GuardScene:
    user_id: str
    guards: dict[str, PreparationCommitGuard] = field(default_factory=dict)
    events: dict[str, asyncio.Event] = field(default_factory=dict)
    held: dict[str, int] = field(default_factory=dict)


_GUARD_SCENES: dict[str, _GuardScene] = {}
_GUARD_WATCHES: dict[tuple[str, str], str] = {}


def _guard_scene(nonce: str) -> _GuardScene:
    user = get_asserted_user_context()
    if user is None:
        raise APIError(code="UNAUTHORIZED", message="fixture requires an asserted identity", status_code=401)
    scene = _GUARD_SCENES[nonce]
    if scene.user_id != user.user_id:
        raise APIError(code="FORBIDDEN", message="fixture scene belongs to another user", status_code=403)
    return scene


async def _guarded_write(nonce: str, name: str, marker: str, *, hold: bool) -> int:
    scene = _GUARD_SCENES[nonce]
    guard = scene.guards[name]
    event = asyncio.Event()
    if hold:
        scene.events[marker] = event
    try:
        async with get_persistence().get_engine().begin() as connection:
            await guard.validate(connection)
            await connection.execute(_GUARD_STATE.insert().values(
                nonce=nonce, subject_kind=guard.subject.kind,
                subject_id=guard.subject.id, marker=marker,
            ))
            pid = int(await connection.scalar(select(func.pg_backend_pid())))
            if hold:
                scene.held[marker] = pid
                await event.wait()
        return pid
    finally:
        scene.held.pop(marker, None)


def register_guard_routes(app: FastAPI) -> None:
    """Expose nonce-owned controls behind the application's real identity middleware."""
    prefix = "/api/v1/runtime-preparation-fixture/guards/{nonce}"

    @app.post(prefix + "/capture/{kind}/{subject_id}/{name}")
    async def capture(nonce: str, kind: Literal["agent", "assistant"], subject_id: str, name: str):
        guard = await issue_preparation_commit_guard(subject=PreparationSubject(kind=kind, id=subject_id))
        if nonce not in _GUARD_SCENES:
            user = get_asserted_user_context()
            assert user is not None
            async with get_persistence().get_engine().begin() as connection:
                await connection.run_sync(_GUARD_METADATA.create_all)
            _GUARD_SCENES[nonce] = _GuardScene(user_id=user.user_id)
        _guard_scene(nonce).guards[name] = guard
        return success_response({"captured": name})

    @app.post(prefix + "/arm/{kind}/{subject_id}")
    async def arm(nonce: str, kind: Literal["agent", "assistant"], subject_id: str):
        _guard_scene(nonce)
        guard = await issue_preparation_commit_guard(subject=PreparationSubject(kind=kind, id=subject_id))
        _GUARD_WATCHES[(guard.subject.kind, guard.subject.id)] = nonce
        return success_response({"armed": True})

    @app.post(prefix + "/commit/{name}/{marker}")
    async def commit(nonce: str, name: str, marker: str, hold: bool = False):
        _guard_scene(nonce)
        pid = await _guarded_write(nonce, name, marker, hold=hold)
        return success_response({"pid": pid})

    @app.get(prefix)
    async def state(nonce: str):
        return success_response({"held": dict(_guard_scene(nonce).held)})

    @app.post(prefix + "/release/{marker}")
    async def release(nonce: str, marker: str):
        scene = _guard_scene(nonce)
        if marker == "all":
            for event in scene.events.values():
                event.set()
            for key in [key for key, value in _GUARD_WATCHES.items() if value == nonce]:
                del _GUARD_WATCHES[key]
        else:
            scene.events[marker].set()
        return success_response({"released": marker})

    @app.delete(prefix)
    async def remove(nonce: str):
        scene = _guard_scene(nonce)
        assert not scene.held, scene.held
        async with get_persistence().get_engine().begin() as connection:
            await connection.execute(_GUARD_STATE.delete().where(_GUARD_STATE.c.nonce == nonce))
        for key in [key for key, value in _GUARD_WATCHES.items() if value == nonce]:
            del _GUARD_WATCHES[key]
        del _GUARD_SCENES[nonce]
        return success_response({"removed": nonce})


def _listed(path: Path, session_id: str) -> bool:
    return path.is_file() and session_id in json.loads(path.read_text())


class PreparationFixture(RuntimePreparationProvider):
    name = "native-startup-material-fixture"

    def __init__(self):
        self.revisions = {}

    def plan(self, *, config):
        return RuntimePreparationPlan(session_bound=str(config.get("name", "")).startswith("Material "))

    async def synchronize(self, *, context):
        nonce = _GUARD_WATCHES.pop((context.operation.subject.kind, context.operation.subject.id), None)
        if nonce is not None:
            name = f"callback-{context.operation.subject.kind}"
            _GUARD_SCENES[nonce].guards[name] = context.commit_guard
            await _guarded_write(nonce, name, name, hold=True)
        paths = [package.checkout_path for package in context.packages]
        if context.skills_path:
            paths.append(context.skills_path)
        present = {}
        for path in paths:
            result = await context.sandbox.commands.run(f"test -d {shlex.quote(path)}")
            present[path] = result.exit_code == 0
        with SYNCHRONIZED.open("a") as receipts:
            receipts.write(json.dumps({
                "subject": {"kind": context.operation.subject.kind, "id": context.operation.subject.id},
                "operation_id": context.operation.operation_id,
                "owner_user_id": context.owner_user_id,
                "revision": context.revision,
                "runtime_generation": context.runtime_generation,
                "prepared_paths": present,
            }) + "\n")

    async def recover(self, *, operation):
        return True

    def _material(self, context, phase):
        revision = self.revisions.get(context.session_id, 0) + 1
        self.revisions[context.session_id] = revision
        # A turn's context names only its Session; the startup material names the subject.
        subject = getattr(context, "subject", None)
        return RuntimeFile("agent-token.json", json.dumps({
            "session_id": context.session_id,
            "principal_id": context.principal_id,
            "subject": {"kind": subject.kind, "id": subject.id} if subject else None,
            "subject_revision": getattr(context, "revision", None),
            "sandbox_id": context.sandbox_id,
            "phase": phase,
            "revision": revision,
        }).encode())

    async def prepare_session(self, *, context):
        if _listed(STARTUP_USER_ACTIONS, context.subject.id):
            raise PreparationUserActionRequired(message="Authorize this Agent to continue", url=ACTION_URL)
        if not self.plan(config=context.config).session_bound:
            return RuntimeMaterials()
        return RuntimeMaterials(
            files=(self._material(context, "startup"),),
            environment={"ASTRABOX_CFUSE_AGENT_TOKEN_FILE": context.file_path("agent-token.json")},
        )

    async def prepare_turn(self, *, context):
        # A deployment's preparation calls its own services; the delay stands in for them.
        if TURN_DELAY.is_file():
            await asyncio.sleep(float(json.loads(TURN_DELAY.read_text())))
        with TURN_RECEIPTS.open("a") as receipts:
            receipts.write(json.dumps({
                "session_id": context.session_id, "turn_id": context.turn_id,
                "dispatch_attempt_id": context.dispatch_attempt_id, "at": time.time(),
            }) + "\n")
        if _listed(REFUSALS, context.session_id):
            raise RuntimeError("SECRET_SENTINEL_MUST_NOT_APPEAR")
        if _listed(USER_ACTIONS, context.session_id):
            raise PreparationUserActionRequired(message="Authorize this Agent to continue", url=ACTION_URL)
        if context.session_id not in self.revisions:
            return RuntimeMaterials()
        return RuntimeMaterials(files=(self._material(context, "turn"),))

    async def read_user_actions(self, *, context):
        if not (_listed(USER_ACTIONS, context.session_id) or _listed(STARTUP_USER_ACTIONS, context.subject.id)):
            return SessionUserActionState(ready=True)
        return SessionUserActionState(
            ready=False,
            user_action={"message": "Authorize this Agent to continue", "url": ACTION_URL},
            actions=(SessionUserActionCommand(id="authorize", label="I have authorized"),),
        )

    async def perform_user_action(self, *, context, action_id):
        if action_id == "authorize":
            for path, key in ((USER_ACTIONS, context.session_id), (STARTUP_USER_ACTIONS, context.subject.id)):
                if path.is_file():
                    pending = json.loads(path.read_text())
                    path.write_text(json.dumps([item for item in pending if item != key]))
        return await self.read_user_actions(context=context)
