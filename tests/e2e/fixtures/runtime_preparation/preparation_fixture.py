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
from pathlib import Path

from astrabox.seams.runtime_preparation import (
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


def _listed(path: Path, session_id: str) -> bool:
    return path.is_file() and session_id in json.loads(path.read_text())


class PreparationFixture(RuntimePreparationProvider):
    name = "native-startup-material-fixture"

    def __init__(self):
        self.revisions = {}

    def plan(self, *, config):
        return RuntimePreparationPlan(session_bound=str(config.get("name", "")).startswith("Material "))

    async def synchronize(self, *, context):
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
