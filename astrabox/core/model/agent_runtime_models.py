"""Data models for the Resident Agent feature.

NOTE: Do NOT add `from __future__ import annotations` to this file.
Deferred (string) field annotations make `dataclasses._is_type()` crash
under some import setups on Python 3.12, so the field annotations
must stay eagerly evaluated.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class AgentState(str, Enum):
    HIBERNATING = "HIBERNATING"
    PROVISIONING = "PROVISIONING"
    ACTIVE = "ACTIVE"
    DELETING = "DELETING"
    DELETED = "DELETED"


class AgentExposureMode(str, Enum):
    CHAT_ONLY = "chat_only"
    MCP_ONLY = "mcp_only"
    BOTH = "both"


class AgentIdentityMode(str, Enum):
    OWNER_DELEGATED = "owner_delegated"
    CALLER_PASSTHROUGH = "caller_passthrough"


class AgentRunState(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_INPUT = "WAITING_INPUT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AgentRunMode(str, Enum):
    MAIN_SESSION = "main_session"
    EPHEMERAL_SESSION = "ephemeral_session"
    MAINTENANCE = "maintenance"


@dataclass
class AgentRecord:
    agent_id: str
    user_id: str
    name: str
    template_name: str
    state: str = "HIBERNATING"
    profile_id: Optional[str] = None
    lease_id: Optional[str] = None
    sandbox_id: Optional[str] = None
    storage_scope_key: Optional[str] = None
    exposure_mode: str = "chat_only"
    identity_mode: str = "owner_delegated"
    delegated_identity_id: Optional[str] = None
    idle_hibernate_seconds: int = 1800
    deleted: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


@dataclass
class AgentRunRecord:
    run_id: str
    agent_id: str
    source: str = "chat_http"
    mode: str = "main_session"
    state: str = "QUEUED"
    caller_user_id: Optional[str] = None
    caller_app: Optional[str] = None
    input_text: str = ""
    output_text: Optional[str] = None
    hidden_session_id: Optional[str] = None
    lease_id: Optional[str] = None
    sandbox_id: Optional[str] = None
    pending_interaction: Optional[dict] = None
    artifact_manifest: Optional[dict] = None
    error: Optional[str] = None
    worker_id: Optional[str] = None
    claimed_until: Optional[int] = None
    heartbeat_at: Optional[str] = None
    enqueued_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)
