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
    plan_subject_storage_mounts,
)
from ._default_repo import clone_default_repo
from ._plugin_cache import (
    bootstrap_conversation_runtime_from_agent_cache,
    cached_package_dirs,
    prepare_agent_runtime_plugin_cache,
)

__all__ = [
    "WORKSPACE_ID_FIELD",
    "claim_workspace_id",
    "ensure_workspace_id",
    "mint_workspace_id",
    "workspace_storage_root",
    "plan_subject_storage_mounts",
    "bootstrap_conversation_runtime_from_agent_cache",
    "cached_package_dirs",
    "clone_default_repo",
    "prepare_agent_runtime_plugin_cache",
]
