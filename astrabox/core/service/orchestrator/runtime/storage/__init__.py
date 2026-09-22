"""Storage scope builders and mount helpers.

Re-exports the storage helpers from this package's submodules, so external
import sites (``runtime_manager.py``, ``engine/hermes.py``,
``workspace/deployment_conversation.py``, and this package's own parent
``runtime/__init__.py``) import them from this package path.
"""

from __future__ import annotations

from ._identity import (
    WORKSPACE_ID_FIELD,
    claim_workspace_id,
    ensure_workspace_id,
    mint_workspace_id,
)
from ._scope import (
    workspace_storage_root,
    NAS_ROOT_MOUNT,
    plan_subject_storage_mounts,
)
from ._nas_mount import mount_assistant_workspace_storage
from ._git_clone import _normalize_deploy_private_key
from ._default_repo import clone_default_repo
from ._plugin_cache import (
    bootstrap_conversation_runtime_from_agent_cache,
    prepare_agent_runtime_plugin_cache,
)

__all__ = [
    "NAS_ROOT_MOUNT",
    "WORKSPACE_ID_FIELD",
    "claim_workspace_id",
    "ensure_workspace_id",
    "mint_workspace_id",
    "workspace_storage_root",
    "plan_subject_storage_mounts",
    "_normalize_deploy_private_key",
    "bootstrap_conversation_runtime_from_agent_cache",
    "clone_default_repo",
    "mount_assistant_workspace_storage",
    "prepare_agent_runtime_plugin_cache",
]
