"""Synthetic deployment integration for the live lifecycle case; no auth service.

Test controls and receipts live under the server's state directory: the test
writes which Sessions to refuse or to hold for a user action, and reads which
Agents were synchronized.
"""
import json
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
        with SYNCHRONIZED.open("a") as receipts:
            receipts.write(json.dumps({
                "agent_id": context.operation.agent_id,
                "operation_id": context.operation.operation_id,
                "runtime_generation": context.runtime_generation,
            }) + "\n")

    async def recover(self, *, operation):
        return True

    def _material(self, context, phase):
        revision = self.revisions.get(context.session_id, 0) + 1
        self.revisions[context.session_id] = revision
        return RuntimeFile("agent-token.json", json.dumps({
            "session_id": context.session_id,
            "principal_id": context.principal_id,
            "agent_id": getattr(context, "agent_id", None),
            "sandbox_id": context.sandbox_id,
            "phase": phase,
            "revision": revision,
        }).encode())

    async def prepare_session(self, *, context):
        if not self.plan(config=context.config).session_bound:
            return RuntimeMaterials()
        return RuntimeMaterials(
            files=(self._material(context, "startup"),),
            environment={"ASTRABOX_CFUSE_AGENT_TOKEN_FILE": context.file_path("agent-token.json")},
        )

    async def prepare_turn(self, *, context):
        if _listed(REFUSALS, context.session_id):
            raise RuntimeError("SECRET_SENTINEL_MUST_NOT_APPEAR")
        if _listed(USER_ACTIONS, context.session_id):
            raise PreparationUserActionRequired(message="Authorize this Agent to continue", url=ACTION_URL)
        if context.session_id not in self.revisions:
            return RuntimeMaterials()
        return RuntimeMaterials(files=(self._material(context, "turn"),))

    async def read_user_actions(self, *, context):
        if not _listed(USER_ACTIONS, context.session_id):
            return SessionUserActionState(ready=True)
        return SessionUserActionState(
            ready=False,
            user_action={"message": "Authorize this Agent to continue", "url": ACTION_URL},
            actions=(SessionUserActionCommand(id="authorize", label="I have authorized"),),
        )

    async def perform_user_action(self, *, context, action_id):
        if action_id == "authorize" and USER_ACTIONS.is_file():
            pending = json.loads(USER_ACTIONS.read_text())
            USER_ACTIONS.write_text(json.dumps([sid for sid in pending if sid != context.session_id]))
        return await self.read_user_actions(context=context)
