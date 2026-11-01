"""Isolated identity-material provider for the Session MCP lifecycle journey."""
import json
import secrets
import time
import uuid
from pathlib import Path
from astrabox.seams.runtime_preparation import (
    PreparationUserActionRequired, RuntimeFile, RuntimeMaterials,
    RuntimePreparationPlan, RuntimePreparationProvider, SessionMCPServer,
    SessionUserActionState,
)

STATE = Path("/data/session-mcp-fixture")


def blocked(principal):
    path = STATE / "blocked.json"
    return path.exists() and principal in json.loads(path.read_text())


class SessionMCPFixture(RuntimePreparationProvider):
    name = "session-mcp-fixture"

    async def plan(self, *, config):
        return RuntimePreparationPlan(session_mcp_servers=(
            SessionMCPServer("personal"),
            SessionMCPServer("personal", plugin="fixture-identity"),
        ))

    async def read_user_actions(self, *, context):
        if blocked(context.principal_id):
            return SessionUserActionState(ready=False, user_action={"message": "Authorize your personal tools"})
        return SessionUserActionState(ready=True)

    async def prepare_session(self, *, context):
        if blocked(context.principal_id):
            raise PreparationUserActionRequired(message="Authorize your personal tools")
        STATE.mkdir(exist_ok=True)
        material = {"session_id": context.session_id, "principal_id": context.principal_id, "not_before": time.time() + 2.0, "revision": uuid.uuid4().hex}
        with (STATE / "bindings.jsonl").open("a") as journal:
            journal.write(json.dumps({**material, "sandbox_id": context.sandbox_id,
                                      "engine_initialized": context.engine_initialized}) + "\n")
        return RuntimeMaterials(
            files=(RuntimeFile("mcp-owner.json", json.dumps(material).encode()),),
            environment={"MCP_IDENTITY_MATERIAL": context.file_path("mcp-owner.json"), "MCP_IDENTITY_REVISION": material["revision"]},
        )


class SessionMCPIdentity:
    """Two task-owned opaque tokens, confined to this isolated fixture."""

    async def resolve(self, headers):
        from astrabox.common.utils.user_context import UserContext

        path = STATE / "identities.private.json"
        if not path.is_file():
            return None
        presented = headers.get("authorization", "")
        for row in json.loads(path.read_text()):
            if secrets.compare_digest(presented, "Bearer " + row["token"]):
                return UserContext(user_id=row["user_id"], roles=row["roles"])
        return None

    def login_url(self, next_url="/"):
        return None
