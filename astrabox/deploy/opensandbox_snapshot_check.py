"""Post-install proof that OpenSandbox pause and resume preserve files."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import timedelta
from typing import Any

from opensandbox import Sandbox, SandboxManager

from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.providers.open_sandbox import _config
from astrabox.providers.sandbox_image import AIO_IMAGE_ENTRYPOINT, resolve_agent_image

# The pinned controller gives its image-commit Job ten minutes.  This whole-path
# probe also creates and reconnects the sandbox, so its outer deadline must be
# longer than that controller-owned operation.
MAX_VERIFICATION_SECONDS = 720
_MARKER_PATH = "/tmp/astrabox-snapshot-verification"
# OpenSandbox serializes this integer as octal digits for execd to parse.  The
# SDK examples therefore use ``600``; Python's ``0o600`` would put decimal 384
# on the wire and execd rejects the digit 8.
_MARKER_MODE = 600


class SnapshotVerificationError(RuntimeError):
    """The live snapshot verification did not complete successfully."""


async def _wait_for_state(
    manager: SandboxManager,
    sandbox_id: str,
    wanted: str,
) -> str:
    """Wait for one terminal lifecycle state inside the outer deadline."""
    while True:
        info = await manager.get_sandbox_info(sandbox_id)
        state = str(info.status.state)
        if state.lower() == wanted.lower():
            return state
        if state.lower() in {"failed", "terminated"}:
            reason = str(info.status.reason or "unknown")
            message = str(info.status.message or "no detail")
            raise SnapshotVerificationError(
                f"sandbox reached {state} while waiting for {wanted}: {reason}: {message}"
            )
        await asyncio.sleep(0.5)


async def verify_opensandbox_snapshots(
    *,
    image: str | None = None,
    lifecycle_base_url: str | None = None,
    timeout_seconds: int = MAX_VERIFICATION_SECONDS,
) -> dict[str, Any]:
    """Run create, write, pause, resume, read, and cleanup against the deployment."""
    if not 1 <= timeout_seconds <= MAX_VERIFICATION_SECONDS:
        raise ValueError(
            f"timeout_seconds must be between 1 and {MAX_VERIFICATION_SECONDS}"
        )

    settings = load_astrabox_settings()
    connection = _config.sdk_connection_config(
        settings,
        lifecycle_base_url_override=lifecycle_base_url,
    )
    secret = connection.get_api_key() or None
    selected_image = str(image or resolve_agent_image()).strip()
    if not selected_image:
        raise SnapshotVerificationError("the sandbox image is empty")

    marker = f"astrabox-snapshot-{uuid.uuid4()}"
    sandbox_id: str | None = None
    active: Sandbox | None = None
    manager: SandboxManager | None = None
    stage = "create"
    started = time.monotonic()
    timings: dict[str, float] = {}
    primary_error: BaseException | None = None

    try:
        async with asyncio.timeout(timeout_seconds):
            stage_started = time.monotonic()
            active = await Sandbox.create(
                image=selected_image,
                entrypoint=list(AIO_IMAGE_ENTRYPOINT),
                env={"IS_SANDBOX": "1", "DISABLE_BROWSER": "true"},
                timeout=timedelta(minutes=10),
                ready_timeout=timedelta(
                    seconds=min(
                        int(settings.sandbox_ready_timeout_seconds),
                        timeout_seconds,
                    )
                ),
                metadata={
                    "astrabox.managed-by": "snapshot-verifier",
                    "astrabox.verification-id": marker,
                },
                connection_config=connection,
            )
            sandbox_id = str(active.id)
            timings[stage] = time.monotonic() - stage_started

            stage = "write"
            stage_started = time.monotonic()
            await active.files.write_file(_MARKER_PATH, marker, mode=_MARKER_MODE)
            timings[stage] = time.monotonic() - stage_started

            stage = "pause"
            stage_started = time.monotonic()
            await active.pause()
            await active.close()
            active = None
            manager = await SandboxManager.create(connection_config=connection)
            await _wait_for_state(manager, sandbox_id, "Paused")
            timings[stage] = time.monotonic() - stage_started

            stage = "resume"
            stage_started = time.monotonic()
            await manager.resume_sandbox(sandbox_id)
            await _wait_for_state(manager, sandbox_id, "Running")
            await manager.close()
            manager = None
            active = await Sandbox.connect(
                sandbox_id,
                connection_config=connection,
                connect_timeout=timedelta(seconds=timeout_seconds),
            )
            timings[stage] = time.monotonic() - stage_started

            stage = "read"
            stage_started = time.monotonic()
            restored = await active.files.read_file(_MARKER_PATH)
            if restored != marker:
                raise SnapshotVerificationError(
                    "the restored marker does not match the file written before pause"
                )
            timings[stage] = time.monotonic() - stage_started
    except BaseException as exc:
        if isinstance(exc, SnapshotVerificationError):
            primary_error = exc
            raise
        if isinstance(exc, TimeoutError):
            failure = SnapshotVerificationError(
                f"snapshot verification exceeded {timeout_seconds} seconds during {stage}"
            )
            primary_error = failure
            raise failure from exc
        detail = _config.scrub_secret(str(exc), secret=secret)
        failure = SnapshotVerificationError(
            f"snapshot verification failed during {stage}: {detail}"
        )
        primary_error = failure
        raise failure from exc
    finally:
        cleanup_errors: list[str] = []
        if active is not None:
            try:
                await active.close()
            except Exception as exc:
                cleanup_errors.append(f"close: {_config.scrub_secret(str(exc), secret=secret)}")
        if manager is not None:
            try:
                await manager.close()
            except Exception as exc:
                cleanup_errors.append(
                    f"manager close: {_config.scrub_secret(str(exc), secret=secret)}"
                )
        if sandbox_id is not None:
            cleanup_manager: SandboxManager | None = None
            try:
                cleanup_manager = await SandboxManager.create(
                    connection_config=connection
                )
                await cleanup_manager.kill_sandbox(sandbox_id)
            except Exception as exc:
                cleanup_errors.append(f"kill: {_config.scrub_secret(str(exc), secret=secret)}")
            finally:
                if cleanup_manager is not None:
                    try:
                        await cleanup_manager.close()
                    except Exception as exc:
                        cleanup_errors.append(
                            "cleanup manager close: "
                            f"{_config.scrub_secret(str(exc), secret=secret)}"
                        )
        if cleanup_errors:
            message = "; ".join(cleanup_errors)
            if primary_error is not None:
                primary_error.add_note(f"snapshot verifier cleanup also failed: {message}")
            else:
                raise SnapshotVerificationError(
                    f"snapshot verification passed but cleanup failed: {message}"
                )

    return {
        "image": selected_image,
        "sandbox_id": sandbox_id,
        "state": "PASS",
        "timings_seconds": {
            name: round(seconds, 3) for name, seconds in timings.items()
        },
        "total_seconds": round(time.monotonic() - started, 3),
    }


__all__ = [
    "MAX_VERIFICATION_SECONDS",
    "SnapshotVerificationError",
    "verify_opensandbox_snapshots",
]
