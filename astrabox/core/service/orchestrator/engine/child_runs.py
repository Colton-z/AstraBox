"""Engine-neutral child-run facts emitted by engine adapters.

An adapter selects one stable native reference for a child and resolves any
other native aliases before emitting a fact.  The platform treats that
reference, its optional parent, and its optional control handle as opaque.
They remain in the private Session journal; the read model mints public ids.
An optional lifecycle ``toolCallId`` associates an existing UI tool invocation
with the child. The read model retains these associations as ``tool_call_ids``;
the console navigates to the public child id, never the tool invocation id.
"""

from __future__ import annotations

import uuid
from typing import Any

_PUBLIC_CHILD_RUN_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL,
    "https://astrabox.ai/resources/child-run",
)
_PUBLIC_CHILD_MESSAGE_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL,
    "https://astrabox.ai/resources/child-run-message",
)
_CHILD_RUN_EVENTS = frozenset({"opened", "updated", "closed"})
_CHILD_RUN_OPERATIONS = frozenset({"stop"})


class ChildRunProjectionError(RuntimeError):
    """An engine emitted child-run data outside the platform contract."""


def public_child_run_id(
    *,
    session_id: str,
    engine_kind: str,
    engine_ref: str,
) -> str:
    """Mint the stable public UUID for one adapter-owned child identity."""

    clean_session_id = str(session_id or "").strip()
    clean_engine_kind = str(engine_kind or "").strip()
    clean_engine_ref = str(engine_ref or "").strip()
    if not clean_session_id or not clean_engine_kind or not clean_engine_ref:
        raise ChildRunProjectionError(
            "child-run public identity requires session, engine, and engine reference"
        )
    material = "\0".join(
        (clean_session_id, clean_engine_kind, clean_engine_ref)
    )
    return str(uuid.uuid5(_PUBLIC_CHILD_RUN_NAMESPACE, material))


def public_child_message_id(
    *,
    session_id: str,
    engine_kind: str,
    engine_ref: str,
    message_ref: str,
) -> str:
    """Mint an opaque stable id without exposing an engine message handle."""

    clean_message_ref = str(message_ref or "").strip()
    if not clean_message_ref:
        raise ChildRunProjectionError("child-run public message identity requires a reference")
    material = "\0".join(
        (
            str(session_id or "").strip(),
            str(engine_kind or "").strip(),
            str(engine_ref or "").strip(),
            clean_message_ref,
        )
    )
    if not all(material.split("\0")):
        raise ChildRunProjectionError(
            "child-run public message identity requires session, engine, and child references"
        )
    return str(uuid.uuid5(_PUBLIC_CHILD_MESSAGE_NAMESPACE, material))


def canonical_child_run_data(
    raw: dict[str, Any],
    *,
    engine_kind: str,
) -> dict[str, Any]:
    """Validate one adapter-authored private lifecycle or message fact."""

    clean_engine_kind = str(engine_kind or "").strip()
    if not clean_engine_kind:
        raise ChildRunProjectionError("child-run projection lacks engine kind")

    data = dict(raw)
    kind = str(data.get("kind") or "").strip()
    if kind not in {"lifecycle", "message"}:
        raise ChildRunProjectionError(f"child-run payload has unsupported kind={kind!r}")

    forbidden_public_fields = {
        "childRunId",
        "parentChildRunId",
        "controlId",
    }.intersection(data)
    if forbidden_public_fields:
        raise ChildRunProjectionError(
            "engine child-run fact contains public projection fields "
            f"fields={sorted(forbidden_public_fields)!r}"
        )

    engine_ref = str(data.get("engineRef") or "").strip()
    if not engine_ref:
        raise ChildRunProjectionError("child-run fact lacks engineRef")
    parent_engine_ref = str(data.get("parentEngineRef") or "").strip()
    if parent_engine_ref == engine_ref:
        raise ChildRunProjectionError(
            f"child-run is its own parent engine_ref={engine_ref!r}"
        )

    projected_engine_kind = str(data.get("engineKind") or "").strip()
    if projected_engine_kind and projected_engine_kind != clean_engine_kind:
        raise ChildRunProjectionError(
            "child-run engine mismatch "
            f"expected={clean_engine_kind!r} actual={projected_engine_kind!r}"
        )

    data["engineRef"] = engine_ref
    data["engineKind"] = clean_engine_kind
    if parent_engine_ref:
        data["parentEngineRef"] = parent_engine_ref
    else:
        data.pop("parentEngineRef", None)

    if "controlRef" in data:
        control_ref = str(data.get("controlRef") or "").strip()
        if not control_ref:
            raise ChildRunProjectionError("child-run controlRef must be non-empty")
        data["controlRef"] = control_ref

    if kind == "lifecycle":
        event = str(data.get("event") or "").strip()
        if event not in _CHILD_RUN_EVENTS:
            raise ChildRunProjectionError(
                f"child-run lifecycle has unsupported event={event!r}"
            )
        engine_event = str(data.get("engineEvent") or "").strip()
        if not engine_event:
            raise ChildRunProjectionError("child-run lifecycle lacks engineEvent")
        data["event"] = event
        data["engineEvent"] = engine_event
        for field_name in ("engineStatus", "engineReason"):
            if field_name not in data:
                continue
            field_value = str(data.get(field_name) or "").strip()
            if not field_value:
                raise ChildRunProjectionError(
                    f"child-run {field_name} must be non-empty when present"
                )
            data[field_name] = field_value
        operations_raw = data.get("operations")
        if not isinstance(operations_raw, list):
            raise ChildRunProjectionError(
                "child-run lifecycle operations must be a list"
            )
        operations: list[str] = []
        for raw_operation in operations_raw:
            operation = str(raw_operation or "").strip()
            if operation not in _CHILD_RUN_OPERATIONS:
                raise ChildRunProjectionError(
                    f"child-run lifecycle has unsupported operation={operation!r}"
                )
            if operation in operations:
                raise ChildRunProjectionError(
                    f"child-run lifecycle repeats operation={operation!r}"
                )
            operations.append(operation)
        if "stop" in operations and not str(data.get("controlRef") or "").strip():
            raise ChildRunProjectionError(
                "child-run lifecycle declares stop without controlRef"
            )
        if event == "closed" and operations:
            raise ChildRunProjectionError(
                "closed child-run lifecycle cannot declare operations"
            )
        data["operations"] = operations
    if kind == "message" and not isinstance(data.get("content"), list):
        raise ChildRunProjectionError("child-run message content must be a list")
    return data


def canonicalize_child_run_blocks(
    blocks: list[dict[str, Any]],
    *,
    engine_kind: str,
) -> list[dict[str, Any]]:
    """Validate child blocks while preserving ordinary message blocks."""

    canonical: list[dict[str, Any]] = []
    parent_by_child: dict[str, str] = {}
    engine_refs: set[str] = set()
    for raw in blocks:
        block = dict(raw)
        if str(block.get("type") or "").strip() != "subagent":
            canonical.append(block)
            continue
        data_raw = block.get("data")
        if not isinstance(data_raw, dict):
            raise ChildRunProjectionError("child-run block data must be an object")
        data = canonical_child_run_data(data_raw, engine_kind=engine_kind)
        block_id = str(block.get("id") or "").strip()
        if not block_id:
            raise ChildRunProjectionError("child-run block lacks stable id")
        engine_ref = str(data["engineRef"])
        engine_refs.add(engine_ref)
        parent_engine_ref = str(data.get("parentEngineRef") or "")
        if parent_engine_ref:
            existing = parent_by_child.get(engine_ref)
            if existing and existing != parent_engine_ref:
                raise ChildRunProjectionError(
                    "child-run has conflicting parents "
                    f"engine_ref={engine_ref!r} "
                    f"parents={existing!r},{parent_engine_ref!r}"
                )
            parent_by_child[engine_ref] = parent_engine_ref
        block["data"] = data
        canonical.append(block)

    for engine_ref in engine_refs:
        seen: set[str] = set()
        current = engine_ref
        while current in parent_by_child:
            if current in seen:
                raise ChildRunProjectionError(
                    f"child-run parent cycle includes engine_ref={current!r}"
                )
            seen.add(current)
            current = parent_by_child[current]
    return canonical
