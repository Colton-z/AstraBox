"""E2E-only fault injection, installed at the fault-injection hook seam.

Inert unless explicitly armed via the ``ASTRABOX_E2E_FAULTS`` environment flag
(the app installs these hooks at startup only when it is set — see
``astrabox/api/app.py``). Lets a real browser E2E deterministically exercise
otherwise-timing-dependent recovery paths against a real sandbox and host.

Fault files live on the backend host. The base file and its ``.d/*.json``
siblings are one declaration channel shared by every fault kind. Examples::

    {"faults": {"transcript_append_5xx": 3}, "match": {"session_id": "<id>"}, "consumed": [...]}
    {"faults": {"hold_after_frame": 1}, "match": {"session_id": "<id>", "frame_type": "text-delta"}}

Hook points (see :mod:`astrabox.common.fault_injection`):

- ``transcript_append_5xx`` (context: ``session_id``, ``append_id``,
  ``entry_count``) — consume one temporary append rejection.
- ``response_part_read`` (context: ``session_id``, ``frame_type``) — hold the
  session's reader after one matching part, committed, until the declaration
  releases it.
- ``reader_boundary`` (context: ``session_id``, ``phase``, ``item_id``) — hold
  before subscription or admission recording. Releasing an admission hold
  detaches that reader before it can record the held event.
- ``tool_card_lookup`` (context: ``session_id``, ``response_id``, ``cursor``, ``frame``)
  — hold before authorized card lookup, then detach that reader on release.

The reversible sandbox-egress fault uses the same armed support module but an
admin test route rather than a watched file. It is a requested mutation, not a
hot-path event to consume, and its route does not exist unless the gate is on.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Body, Request

from astrabox.common.fault_injection import register_fault_barrier, register_fault_hook
from astrabox.common.logger.logger_factory import get_logger

logger = get_logger(__name__)

_FAULT_FILE = "/tmp/astrabox-e2e-faults.json"
_HOLD_RELEASE_TIMEOUT_SECONDS = 120.0
_LOCK = threading.Lock()


def e2e_faults_enabled() -> bool:
    return str(os.getenv("ASTRABOX_E2E_FAULTS") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def install_e2e_fault_hooks() -> None:
    """Register the file-driven E2E fault handlers at the hook seam."""
    register_fault_hook("transcript_append_5xx", maybe_consume_transcript_append_5xx)
    register_fault_barrier("response_part_read", maybe_hold_after_part)
    register_fault_barrier("reader_boundary", maybe_hold_reader_boundary)
    register_fault_barrier("tool_card_lookup", maybe_detach_during_card_lookup)
    logger.warning(
        "E2E fault hooks installed (ASTRABOX_E2E_FAULTS armed) — "
        "this must never be a production deployment"
    )


def register_e2e_fault_routes(app: Any) -> None:
    """Mount the gated admin mutation route before the SPA catch-all."""
    if not e2e_faults_enabled():
        return
    route_name = "mutate_e2e_sandbox_egress"
    if any(getattr(route, "name", "") == route_name for route in app.routes):
        return

    from astrabox.api.routes._shared import _resolve_user
    from astrabox.api.routes.sandboxes import _resolve_backend, _sandbox_id
    from astrabox.common.utils.api_response import success_response
    from astrabox.common.utils.errors import APIError

    @app.post(
        "/api/v1/admin/e2e/sandboxes/{sandbox_id}/egress",
        name=route_name,
        include_in_schema=False,
    )
    async def mutate_e2e_sandbox_egress(
        sandbox_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        await _resolve_user(request)
        resolved_id = _sandbox_id(sandbox_id)
        backend, provider = _resolve_backend(request.query_params.get("backend"))
        operation = str(payload.get("operation") or "").strip().lower()
        target = str(payload.get("target") or "").strip()
        if operation not in {"patch", "delete"}:
            raise APIError(
                code="INVALID_REQUEST",
                message="egress fault operation must be 'patch' or 'delete'",
                status_code=400,
            )
        if not target:
            raise APIError(
                code="INVALID_REQUEST",
                message="egress fault target is required",
                status_code=400,
            )

        allowed_keys = {"operation", "target"}
        action: str | None = None
        if operation == "patch":
            allowed_keys.add("action")
            action = str(payload.get("action") or "").strip().lower()
            if action not in {"allow", "deny"}:
                raise APIError(
                    code="INVALID_REQUEST",
                    message="egress fault patch action must be 'allow' or 'deny'",
                    status_code=400,
                )
        unknown_keys = sorted(str(key) for key in payload if key not in allowed_keys)
        if unknown_keys:
            raise APIError(
                code="INVALID_REQUEST",
                message=f"egress fault request has unknown fields: {unknown_keys}",
                status_code=400,
            )

        if operation == "patch":
            assert action is not None
            await provider.patch_egress_rules(
                resolved_id,
                rules=((action, target),),
            )
        else:
            await provider.delete_egress_rules(
                resolved_id,
                targets=(target,),
            )
        logger.warning(
            "e2e mutated sandbox egress backend=%s sandbox=%s operation=%s "
            "action=%s target=%r",
            backend,
            resolved_id,
            operation,
            action,
            target,
        )
        return success_response(
            {
                "backend": backend,
                "sandbox_id": resolved_id,
                "operation": operation,
                "action": action,
                "target": target,
            }
        )


def _fault_base_path() -> str:
    if not e2e_faults_enabled():
        return ""
    return str(
        os.getenv("ASTRABOX_E2E_FAULT_FILE") or _FAULT_FILE
    ).strip()


def _fault_paths_for(base: str) -> list[str]:
    if not base:
        return []
    paths: list[str] = []
    if os.path.isfile(base):
        paths.append(base)
    scan_dir = f"{base}.d"
    if os.path.isdir(scan_dir):
        for name in sorted(os.listdir(scan_dir)):
            if name.endswith(".json"):
                candidate = os.path.join(scan_dir, name)
                if os.path.isfile(candidate):
                    paths.append(candidate)
    return paths


def _fault_paths() -> list[str]:
    return _fault_paths_for(_fault_base_path())


def _count(raw: Any) -> int:
    try:
        return max(int(raw), 0)
    except Exception:
        return 0


def _context_matches(payload: dict[str, Any], session_id: str) -> bool:
    match = payload.get("match")
    if not isinstance(match, dict):
        return True
    expected = str(match.get("session_id") or "").strip()
    if not expected:
        return True
    return expected == str(session_id or "").strip()


def _frame_type_matches(payload: dict[str, Any], frame_type: str) -> bool:
    match = payload.get("match")
    if not isinstance(match, dict):
        return True
    expected = str(match.get("frame_type") or "").strip()
    if not expected:
        return True
    return expected == str(frame_type or "").strip()


def _atomic_write(path: str, payload: dict[str, Any]) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, sort_keys=True)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def _consume_counted_fault(
    fault_name: str,
    *,
    session_id: str,
    consumed_value: Any,
    frame_type: str = "",
    match_context: dict[str, str] | None = None,
) -> str | None:
    """Consume one matching count and return the declaration path that fired."""
    paths = _fault_paths()
    if not paths:
        return None
    with _LOCK:
        for path in paths:
            try:
                with open(path, encoding="utf-8") as fp:
                    payload = json.load(fp)
            except FileNotFoundError:
                continue
            except Exception as exc:  # noqa: BLE001 - probe ignores malformed files
                logger.warning("e2e fault file ignored path=%s err=%s", path, exc)
                continue
            if (
                not isinstance(payload, dict)
                or not _context_matches(payload, session_id)
                or (frame_type and not _frame_type_matches(payload, frame_type))
                or (
                    match_context is not None
                    and (
                        not isinstance(payload.get("match"), dict)
                        or any(
                            payload["match"].get(key) != value
                            for key, value in match_context.items()
                        )
                    )
                )
            ):
                continue
            faults = payload.get("faults")
            if not isinstance(faults, dict):
                continue
            remaining = _count(faults.get(fault_name))
            if remaining <= 0:
                continue
            faults[fault_name] = remaining - 1
            consumed = payload.get("consumed")
            if not isinstance(consumed, list):
                consumed = []
            consumed.append(consumed_value)
            payload["faults"] = faults
            payload["consumed"] = consumed
            _atomic_write(path, payload)
            return path
    return None


def maybe_consume_transcript_append_5xx(
    *, session_id: str, append_id: str, entry_count: int
) -> bool:
    """Consume one temporary transcript-append failure when explicitly armed."""
    evidence = {
        "fault": "transcript_append_5xx",
        "session_id": str(session_id),
        "append_id": str(append_id),
        "entry_count": max(int(entry_count), 0),
    }
    path = _consume_counted_fault(
        "transcript_append_5xx",
        session_id=session_id,
        consumed_value=evidence,
    )
    if path is None:
        return False
    logger.warning(
        "e2e injecting transcript append 5xx session=%s append_id=%s "
        "entry_count=%s path=%s",
        session_id,
        append_id,
        entry_count,
        path,
    )
    return True


def _consume_part_hold(*, session_id: str, frame_type: str) -> str | None:
    return _consume_counted_fault(
        "hold_after_frame",
        session_id=session_id,
        frame_type=frame_type,
        consumed_value={
            "fault": "hold_after_frame",
            "session_id": str(session_id),
            "frame_type": str(frame_type),
        },
    )


def _wait_for_hold_release(path: str, *, label: str) -> None:
    deadline = time.monotonic() + _HOLD_RELEASE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            with open(path, encoding="utf-8") as fp:
                payload = json.load(fp)
        except FileNotFoundError:
            return
        if isinstance(payload, dict) and payload.get("release") is True:
            return
        time.sleep(0.05)
    raise TimeoutError(
        f"e2e {label} hold was not released "
        f"within {_HOLD_RELEASE_TIMEOUT_SECONDS:.0f}s"
    )


async def maybe_hold_reader_boundary(*, session_id: str, phase: str, item_id: str) -> None:
    """Stage one reconnect/custody cut without changing durable facts."""
    path = _consume_counted_fault(
        "hold_reader_boundary",
        session_id=session_id,
        match_context={"phase": phase},
        consumed_value={"fault": "hold_reader_boundary", "session_id": session_id,
                        "phase": phase, "item_id": item_id},
    )
    if path is None:
        return
    await asyncio.to_thread(_wait_for_hold_release, path, label=f"reader {phase} for {session_id}")
    if phase == "admission":
        from astrabox.core.service.orchestrator.engine.base import EngineStreamDetached

        raise EngineStreamDetached(f"E2E reader detached before {phase} for {session_id}")


async def maybe_hold_after_part(
    *,
    session_id: str,
    frame_type: str,
    prepare_hold: Callable[[], Awaitable[None]],
) -> None:
    """Hold a session's reader after one matching part until its E2E declaration releases it.

    ``prepare_hold`` commits what the reader has read, so a page sees the
    part before the hold begins.
    """
    path = _consume_part_hold(session_id=session_id, frame_type=frame_type)
    if path is None:
        return
    await prepare_hold()
    logger.warning(
        "e2e holding reader after part session=%s frame_type=%s path=%s",
        session_id,
        frame_type,
        path,
    )
    await asyncio.to_thread(
        _wait_for_hold_release,
        path,
        label=f"reader part for session {session_id!r}",
    )
    logger.warning(
        "e2e released reader part hold session=%s frame_type=%s path=%s",
        session_id,
        frame_type,
        path,
    )


async def maybe_detach_during_card_lookup(
    *, session_id: str, response_id: str, cursor: dict[str, Any] | None, frame: dict[str, Any],
) -> None:
    """Hold one card-bearing result before lookup and lose its reader on release."""
    from astrabox.core.service.orchestrator.tool_cards import card_ids_in

    tool_call_id = str(frame.get("toolCallId") or "")
    card_ids = card_ids_in(frame.get("output"))
    if not tool_call_id or not card_ids:
        return
    path = _consume_counted_fault(
        "detach_during_card_lookup",
        session_id=session_id,
        consumed_value={
            "fault": "detach_during_card_lookup",
            "session_id": session_id,
            "response_id": response_id,
            "cursor": cursor,
            "tool_call_id": tool_call_id,
            "card_ids": card_ids,
        },
    )
    if path is None:
        return
    await asyncio.to_thread(
        _wait_for_hold_release, path, label=f"card lookup for session {session_id!r}",
    )
    from astrabox.core.service.orchestrator.engine.base import EngineStreamDetached

    raise EngineStreamDetached(f"e2e reader loss before card lookup: {tool_call_id}")


