"""Storage scope objects and pure mount-planning helpers.

Stateless path math: no ``await``, no sandbox handle. Every function here is a
pure transformation of its own explicit parameters — the single source of truth
for where a subject's durable files live.

Storage paths are keyed by ``workspace_id``, independently of account names and
the Agent, Assistant or Session records that refer to the workspace. This keeps
the backing directory stable when a business record changes or is deleted.

This planner is the only thing that answers "where do this subject's files
live". The sandbox seam's `uses_create_oss_mounts = True` means "the create path
mounts this instead", so a create path that plans nothing leaves that flag
asserting a durability nothing provides — an empty overlay that reads exactly
like a working workspace.
"""

from __future__ import annotations

import re
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    assistant_config_dir,
    assistant_workspace_dir,
    normalize_runtime_identity,
)


# Single per-sandbox mount of the unified network-storage root (``:/``). Per-conversation
# storage is a subdirectory of this one mount + a local bind, so no code here
# issues a second mount or an umount/remount. Matches the root assumed by the
# batch JSONL exporter (Path("/mnt/nas")).
NAS_ROOT_MOUNT = "/mnt/nas"


def _template_default_repo(template: Any) -> Any:
    if isinstance(template, dict):
        return template.get("default_repo")
    return getattr(template, "default_repo", None)


def _safe_nas_segment(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    if not text or not re.fullmatch(r"[A-Za-z0-9._@+:-]+", text):
        raise APIError(
            code="NAS_MOUNT_FAILED",
            message=f"unsafe NAS {label}: {text!r}",
            status_code=500,
        )
    return text


#: The only ``subject_kind`` values this planner recognises, kept as one tuple so
#: the unknown-kind error and any later exhaustiveness check share a source.
_KNOWN_SUBJECT_KINDS = (
    "deployment_conversation",
    "deployment_runtime",
    "assistant_runtime",
)


def workspace_storage_root(base: str, workspace_id: str) -> str:
    """Where a workspace lives on the medium, given its name.

    One function, because a second place composing this would be a second
    layout: a box built by one caller and rebuilt by another have to land on
    the same directory, and this is the only thing they share.
    """

    safe = _safe_nas_segment(workspace_id, label="workspace_id")
    return f"{str(base).rstrip('/')}/workspaces/{safe}"


def plan_subject_storage_mounts(
    *,
    subject_kind: str,
    settings: Any,
    workspace_id: str,
    agent_id: str | None = None,
    assistant_id: str | None = None,
    user_id: str | None = None,
    runtime_identity: dict[str, Any] | None = None,
) -> list[tuple[str, str]]:
    """The ``(box_path, storage_subpath)`` mounts for a runtime, by subject kind.

    One planner for every subject, so a deployment's files land in the same place
    whatever created the box — a cold create, a pool borrow, or a rebuild after
    the last one died.

    Every storage path sits under ``{base}/workspaces/{workspace_id}`` and names
    no Agent, Assistant or Session. Those are business records a developer
    creates and deletes, and the files under them are a user's to keep: a layout
    keyed by one leaves a tree that may not be deleted and cannot be attributed
    once the record is gone. The platform mints the name and the owning row
    points at it, so archiving is an operation over names the platform owns.

      - ``deployment_runtime`` — the whole conversations root at the profile's
        literal home prefix. One box serves many conversations of one Agent, and
        each conversation is a directory under that root.
      - ``deployment_conversation`` — this conversation's own workspace at the
        identity's ``workspace_source_dir``. That field is the PHYSICAL location
        (`conversation_identity` requires it under the home for an Agent-tenanted
        box, and equal to the visible workspace for a per-Session one), which is
        what makes one mount serve both tenancies without a bind.
      - ``assistant_runtime`` — one Assistant's workspace and the config beside
        it, the two halves of that product's promise.

    A ``subject_kind`` outside these raises rather than falling through to a
    default: a blank or misspelled kind that silently picked a path would put a
    user's files somewhere nothing later looks for them.
    """

    base = str(getattr(settings, "nas_base_path", "") or "").rstrip("/")
    if not base:
        raise APIError(
            code="NAS_MOUNT_FAILED",
            message=(
                "nas_base_path is required to plan durable storage for "
                f"subject_kind={subject_kind!r}"
            ),
            status_code=500,
        )
    kind = str(subject_kind or "").strip()
    root = workspace_storage_root(base, workspace_id)
    if kind == "deployment_runtime":
        # The root the profile's homes are rendered under, taken as the literal
        # prefix of `home_template` rather than assumed. A shared box serves
        # many conversations and a prepared one is created before any exists, so
        # nothing has rendered a home yet and the ROOT is the only mount that
        # can serve whichever conversation arrives.
        #
        # That is coarser than the per-conversation branch below, and the
        # difference is not free: mounting the root makes every conversation's
        # `.claude` durable as well, which the conversation branch deliberately
        # avoids. It is what a mount decided before its subject exists can
        # express, so it is taken knowingly rather than presented as the same
        # guarantee. The cost the measurement names is the medium's, not this
        # branch's: a block-backed claim does not incur it, a network one does.
        #
        # An administrator who configures a template with no literal prefix has
        # asked for something this cannot express, and is told so.
        return [(_agent_home_root(), root)]
    if kind == "deployment_conversation":
        identity = normalize_runtime_identity(runtime_identity)
        if not identity:
            raise APIError(
                code="NAS_MOUNT_FAILED",
                message=(
                    "deployment_conversation storage planning requires the "
                    "runtime identity that names the workspace"
                ),
                status_code=500,
            )
        # Mount only the workspace; adjacent engine state stays on the box's
        # disk. Native conversation state is mirrored to the platform database
        # and restored independently of this optional workspace volume.
        return [(str(identity["workspace_source_dir"]), f"{root}/workspace")]
    if kind == "assistant_runtime":
        subject_root = root
        # Optional persistent storage retains the workspace and profile in
        # separate directories. Hermes also snapshots its complete native
        # SessionDB to the platform database; startup restores that snapshot
        # through hermes_state_bootstrap.py with or without these mounts.
        # The mounted profile still needs the filesystem semantics required by
        # the vendor's live SQLite database.
        #
        # Render both paths from the profile so they match the workload's home.
        # Mount planning precedes box creation, so the runtime identity is not
        # available here.
        return [
            (
                assistant_workspace_dir(str(user_id or ""), str(assistant_id or "")),
                f"{subject_root}/workspace",
            ),
            (
                assistant_config_dir(str(user_id or ""), str(assistant_id or "")),
                f"{subject_root}/config",
            ),
        ]
    raise APIError(
        code="NAS_MOUNT_FAILED",
        message=(
            f"unknown subject_kind {subject_kind!r}; expected one of "
            f"{_KNOWN_SUBJECT_KINDS}"
        ),
        status_code=500,
    )



def _agent_home_root() -> str:
    """The literal directory an Agent conversation's home is rendered under.

    `home_template` is configuration, so this reads the template and takes the
    part before its first placeholder. A template that begins with one has no
    static root to mount, which is refused rather than guessed: a mount at the
    wrong root is a workspace nothing later looks in.
    """

    from astrabox.seams.sandbox import SANDBOX_TENANCY_AGENT
    from astrabox.core.service.orchestrator.engine.runtime_profiles import (
        composed_runtime_profile,
    )

    template = str(
        composed_runtime_profile("claude_code", SANDBOX_TENANCY_AGENT).home_template
        or ""
    )
    root = template.split("{", 1)[0].rstrip("/")
    if not root or root == "":
        raise APIError(
            code="NAS_MOUNT_FAILED",
            message=(
                "the Agent home template has no literal root to mount a prepared "
                f"box against: {template!r}"
            ),
            status_code=500,
        )
    return root
