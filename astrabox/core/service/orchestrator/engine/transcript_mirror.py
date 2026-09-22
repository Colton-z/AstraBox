"""The two ends of mirroring an engine's own session log out of a box.

The Claude runner persists SDK transcript batches through ``SpoolSessionStore``.
Other engines write native session files: Codex writes rollouts under
``$CODEX_HOME/sessions``, and the DeepSeek Harness writes conversation logs under
its own home. An image-resident mirror relays those records to the platform
database. This module configures that mirror's destination and restores native
records into a replacement box before the engine resumes.

Nothing here parses a line. The engine's format is the engine's, versioned by
its vendor and migrated by it on read; what crosses is bytes on the way out and
the same values on the way back.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import posixpath
from typing import TYPE_CHECKING, Any

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.runtime_profiles import (
    SANDBOX_IMAGE_WORKLOAD_USER,
    SANDBOX_IMAGE_WORKSPACE_DIR,
)

if TYPE_CHECKING:
    from astrabox.core.service.orchestrator.engine.platform import EnginePlatform

#: What the in-box mirror reads its configuration from. Named on both sides of
#: the box: here, and in `runtime/astrabox-transcript-mirror`.
TRANSCRIPT_BASE_URL_ENV = "_ASTRABOX_TRANSCRIPT_BACKEND_BASE_URL"
PLATFORM_SESSION_ID_ENV = "_ASTRABOX_PLATFORM_SESSION_ID"
TRANSCRIPT_PROJECT_KEY_ENV = "_ASTRABOX_TRANSCRIPT_PROJECT_KEY"

#: The deferred alternative for a box prepared before its Session exists: the
#: create names this file instead of the three values above, and the claim
#: writes the same values into it (:func:`bind_mirror_target`) before the
#: engine conversation is created. The path sits inside the mirror's own
#: state directory (its `ASTRABOX_TRANSCRIPT_MIRROR_STATE_DIR` default), which
#: the mirror creates at boot under the workload account — so the claim-time
#: write lands in a directory that already exists and is already writable.
TRANSCRIPT_MIRROR_TARGET_FILE_ENV = "ASTRABOX_TRANSCRIPT_MIRROR_TARGET_FILE"
DEFERRED_MIRROR_TARGET_FILE = "/tmp/astrabox-transcript-mirror/target.json"


def _mirror_target_values(
    manager: "EnginePlatform", session_id: str, *, cwd: str
) -> dict[str, str]:
    """The three values the in-box mirror needs, however they are delivered.

    One derivation for both deliveries (create-time environment and the
    claim-written target file), so the two cannot drift on the capability
    token or the base address.
    """

    from astrabox.api.routes.transcript import (
        CAPABILITY_PATH_PREFIX,
        transcript_capability_required,
    )
    from astrabox.core.service.orchestrator.transcript_capability import (
        mint_transcript_capability_token,
    )

    settings = manager.deployment_settings
    base = str(getattr(settings, "mcp_proxy_base_url", "") or "").strip().rstrip("/")
    if not base:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=(
                "no sandbox-reachable backend base url is configured, so this "
                "box could not mirror its transcript; a session whose history "
                "never leaves the box cannot be resumed anywhere else"
            ),
            status_code=500,
        )
    if transcript_capability_required():
        base = f"{base}{CAPABILITY_PATH_PREFIX}/{mint_transcript_capability_token(session_id)}"
    return {
        "base_url": base,
        "session_id": session_id,
        # The store's own meaning for this field: a stable encoding of the
        # working directory the transcript belongs to.
        "project_key": str(cwd or "").strip() or SANDBOX_IMAGE_WORKSPACE_DIR,
    }


def mirror_env(
    manager: "EnginePlatform", session_id: str, workspace_plan: Any
) -> dict[str, str]:
    """What the in-box mirror needs to reach the platform's transcript store.

    Given at box create, because that is where an engine's per-session facts
    already travel and the box is created per session. Refused rather than
    omitted: a box whose transcript never leaves loses the conversation when it
    is reclaimed, and nothing observes that until someone tries to resume it.
    """

    values = _mirror_target_values(
        manager,
        session_id,
        cwd=str(getattr(workspace_plan, "cwd", "") or "").strip(),
    )
    return {
        TRANSCRIPT_BASE_URL_ENV: values["base_url"],
        PLATFORM_SESSION_ID_ENV: values["session_id"],
        TRANSCRIPT_PROJECT_KEY_ENV: values["project_key"],
    }


def deferred_mirror_env() -> dict[str, str]:
    """Box-create environment for a slot box whose Session does not exist yet.

    Instead of per-session values the create names the file a later claim
    will write; the in-box mirror relays nothing until that file holds a
    usable target and exits FATAL if a session log outlives the wait
    (`runtime/astrabox-transcript-mirror`, ``await_deferred_target``).
    """

    return {TRANSCRIPT_MIRROR_TARGET_FILE_ENV: DEFERRED_MIRROR_TARGET_FILE}


def mirror_target_payload(
    manager: "EnginePlatform", session_id: str, *, cwd: str
) -> bytes:
    """The exact bytes a claim writes into the deferred target file.

    Exposed separately from :func:`bind_mirror_target` so a test of the
    in-box reader can be fed this producer's serialization rather than a
    hand-written imitation of it.
    """

    return json.dumps(
        _mirror_target_values(manager, session_id, cwd=cwd),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def unclaimed_marker_payload(unclaimed_for: "timedelta") -> bytes:
    """The bytes that say "prepared, not yet claimed, legitimate until T".

    Written into the same file a claim later overwrites, so the in-box mirror
    reads one place for one question. The deadline is stated rather than
    assumed because only the platform knows how long an unclaimed unit may
    live; past it the box is overdue, and the mirror's ordinary fail-loud
    clock applies.
    """

    deadline = datetime.now(timezone.utc) + unclaimed_for
    return json.dumps(
        {"unclaimed_until": deadline.isoformat()},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


async def mark_mirror_unclaimed(
    sandbox: Any,
    *,
    unclaimed_for: "timedelta",
    target_file: str = DEFERRED_MIRROR_TARGET_FILE,
    owner: str | None = None,
) -> None:
    """Tell a prepared box's mirror that its own logs are not orphaned work.

    A prepared unit that holds a running engine writes its conversation file
    at once, and nothing can bind a destination for it until a Session claims
    the unit. Without this marker the mirror reads that file as work it cannot
    deliver and exits FATAL two minutes later, which is why a prepared unit
    could not hold a started engine at all.
    """

    await sandbox.files.write_file(
        target_file,
        unclaimed_marker_payload(unclaimed_for),
        mode=600,
        # The reader is the mirror, which runs as the account owning the
        # marker's home: the box account for a whole-box unit, the slot's
        # conversation account for a shared slot.
        owner=owner or SANDBOX_IMAGE_WORKLOAD_USER,
        group=owner or SANDBOX_IMAGE_WORKLOAD_USER,
    )


async def bind_mirror_target(
    sandbox: Any,
    manager: "EnginePlatform",
    session_id: str,
    *,
    cwd: str,
    target_file: str = DEFERRED_MIRROR_TARGET_FILE,
    owner: str | None = None,
) -> None:
    """Point a claimed slot box's resident mirror at this Session's store.

    Must run before the engine conversation is created: the first rollout
    line then already has a destination, and the mirror's fail-loud grace
    clock (work with no target) never starts in a healthy claim.
    """

    account = str(owner or "").strip() or SANDBOX_IMAGE_WORKLOAD_USER
    await sandbox.files.write_file(
        target_file,
        mirror_target_payload(manager, session_id, cwd=cwd),
        # The mirror runs as the account that owns the conversation — the
        # image's workload account on the box tenancy, the conversation's own
        # account under the Agent-shared tenancy — and at mode 600 the owner
        # is what makes the file readable to it at all.
        mode=600,
        owner=account,
        group=account,
    )


async def restore_mirrored_logs(
    sandbox: Any,
    session_id: str,
    *,
    namespace: str,
    root: str,
    owner: str | None = None,
) -> int:
    """Put a conversation's session logs back before a replacement box rejoins.

    An engine that rebuilds a conversation from its own file starts amnesiac
    until the file is back on disk. Each is written under the path its scope
    names — the scope IS the path relative to the engine's session root — so
    restoring is transcription rather than reconstruction, which is what makes
    a subagent's log land where its engine will look for it.

    Byte-for-byte is not on offer and is not needed: the store's contract is
    that what comes out is deep-equal to what went in, and an engine parses its
    log rather than diffing it. Order is a different matter — a log's order is
    its meaning — and the read returns entries by committed sequence.

    Returns how many logs were written.
    """

    from astrabox.persistence.repository.transcript_entry_repository import (
        TranscriptEntryRepository,
    )

    repository = TranscriptEntryRepository()
    scopes = await repository.list_scopes_by_platform_session(session_id)
    written = 0
    for scope in scopes:
        subpath = str(scope.get("subpath") or "")
        if not subpath.startswith(namespace):
            continue
        entries = await repository.load_subpath_entries_by_platform_session(
            session_id, subpath=subpath
        )
        if not entries:
            continue
        path = f"{root.rstrip('/')}/{subpath[len(namespace):]}"
        body = "".join(
            json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
            for entry in entries
        ).encode("utf-8")
        await sandbox.files.create_directories(
            [_write_entry(posixpath.dirname(path), owner=owner or SANDBOX_IMAGE_WORKLOAD_USER)]
        )
        # Owned by whoever the ENGINE runs as, which the caller names: the
        # conversation's own account on the shared tenancy, the image account
        # otherwise. The image account was hard-coded here once, and every
        # shared-tenancy restore then handed the engine a file it could not
        # read — 'Permission denied' on its own session metadata, three
        # restores per lane. The path was templated over the conversation
        # home in 9adcf17a; the owner is the other half of that rename's
        # readership.
        await sandbox.files.write_file(
            path,
            body,
            mode=600,
            owner=owner or SANDBOX_IMAGE_WORKLOAD_USER,
            group=owner or SANDBOX_IMAGE_WORKLOAD_USER,
        )
        written += 1
    return written


def _write_entry(path: str, *, owner: str) -> Any:
    from opensandbox.models.filesystem import WriteEntry

    # Native engines create locks and sibling metadata beside the restored log.
    return WriteEntry(path=path, mode=755, owner=owner, group=owner)
