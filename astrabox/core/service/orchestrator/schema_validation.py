"""Shared thin schema-payload validation for the admin form schemas.

`agent_schema` and `environment_schema` both declare a `list[dict]` field schema
(the JSON the frontend form editor consumes) and validate a write payload against
it: types / required / enum / nested item-schema only, never rewriting the
payload or filling defaults. Both schemas call this implementation so nested
``item_schema`` rules receive the same validation.

Enum resolution is pluggable: agent fields carry a static `enum`, while
environment fields resolve dynamic enums (engine_kind / sandbox_backend /
endpoint_provider) from the live registries — so `validate_payload` takes an
optional `resolve_enum(field)` hook.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from astrabox.common.utils.errors import APIError


def invalid_request(message: str) -> APIError:
    return APIError(code="INVALID_REQUEST", message=message, status_code=400)


def get_by_path(payload: dict[str, Any], path: str) -> tuple[bool, Any]:
    """Resolve a dotted path. Returns (present, value)."""
    node: Any = payload
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def check_scalar(field_key: str, field_type: str, value: Any, enum: list[str] | None) -> None:
    """Validate a single declared field value. None is treated as 'absent'."""
    if value is None:
        return
    if field_type in ("string", "text", "env_ref"):
        if not isinstance(value, str):
            raise invalid_request(f"{field_key} must be a string")
    elif field_type == "boolean":
        if not isinstance(value, bool):
            raise invalid_request(f"{field_key} must be a boolean")
    elif field_type == "integer":
        # bool is a subclass of int; reject it explicitly.
        if not isinstance(value, int) or isinstance(value, bool):
            raise invalid_request(f"{field_key} must be an integer")
    elif field_type == "enum":
        if not isinstance(value, str) or value not in (enum or []):
            raise invalid_request(f"{field_key} must be one of {enum}")
    elif field_type == "string_list":
        if not isinstance(value, list) or any(not isinstance(s, str) for s in value):
            raise invalid_request(f"{field_key} must be a list of strings")
    elif field_type == "key_value":
        if not isinstance(value, dict) or any(not isinstance(v, str) for v in value.values()):
            raise invalid_request(f"{field_key} must be an object of string values")
    elif field_type == "object":
        if not isinstance(value, dict):
            raise invalid_request(f"{field_key} must be an object")
    elif field_type == "object_list":
        if not isinstance(value, list) or any(not isinstance(it, dict) for it in value):
            raise invalid_request(f"{field_key} must be a list of objects")
    # Unknown types are not validated here (should not happen for declared fields).


def check_item_schema(field_key: str, obj: dict[str, Any], item_schema: list[dict[str, Any]]) -> None:
    """Validate declared sub-fields of a nested object. Undeclared keys pass."""
    for sub in item_schema:
        sub_key = str(sub["key"])
        if sub.get("required") and not str(obj.get(sub_key) or "").strip():
            raise invalid_request(f"{field_key}.{sub_key} is required")
        if sub_key in obj:
            check_scalar(
                f"{field_key}.{sub_key}",
                str(sub["type"]),
                obj[sub_key],
                sub.get("enum"),
            )


def validate_payload(
    payload: dict[str, Any],
    field_schema: list[dict[str, Any]],
    *,
    resolve_enum: Callable[[dict[str, Any]], list[str] | None] | None = None,
    payload_label: str = "payload",
) -> None:
    """Types / required / enum / item-schema validation against a field schema.

    Only validates keys the schema declares; any field outside the schema is
    passed through untouched (preserves the doc's open shape). On any violation
    raises APIError(INVALID_REQUEST, 400). Never mutates payload.
    """
    if not isinstance(payload, dict):
        raise invalid_request(f"{payload_label} must be an object")

    for field in field_schema:
        field_key = str(field["key"])
        field_type = str(field["type"])
        path = field.get("path")

        if path:
            present, value = get_by_path(payload, str(path))
        else:
            present = field_key in payload
            value = payload.get(field_key)

        if field.get("required"):
            # Required applies to the resolved location; treat blank as missing.
            if not present or not str(value or "").strip():
                raise invalid_request(f"{field_key} is required")

        if not present:
            continue

        enum = resolve_enum(field) if resolve_enum is not None else field.get("enum")
        check_scalar(field_key, field_type, value, enum)

        item_schema = field.get("item_schema")
        if not item_schema or value is None:
            continue
        if field_type == "object" and isinstance(value, dict):
            check_item_schema(field_key, value, item_schema)
        elif field_type == "object_list" and isinstance(value, list):
            for idx, item in enumerate(value):
                if isinstance(item, dict):
                    check_item_schema(f"{field_key}[{idx}]", item, item_schema)


def validate_declared_config_bag(
    bag: dict[str, Any],
    field_schema: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    bag_label: str,
    owner_label: str,
) -> None:
    """Closed validation of a config bag against a declared field schema.

    Unlike :func:`validate_payload`, a key the schema does not declare is a
    violation, not pass-through: the bag's whole contract is "only what the
    declaring owner understands", and an undeclared key is a knob nothing will
    ever read. An empty declaration therefore rejects any non-empty bag.
    """
    if not isinstance(bag, dict):
        raise invalid_request(f"{bag_label} must be an object")
    if not bag:
        return
    schema_list = list(field_schema or ())
    if not schema_list:
        raise invalid_request(
            f"{owner_label} declares no {bag_label} keys; remove the values or "
            "choose an environment whose engine declares them"
        )
    declared = {str(field["key"]) for field in schema_list}
    unknown = sorted(set(bag) - declared)
    if unknown:
        raise invalid_request(
            f"{bag_label} contains keys {owner_label} does not declare: "
            + ", ".join(unknown)
        )
    validate_payload(bag, schema_list, payload_label=bag_label)
    for field in schema_list:
        key = str(field["key"])
        block = bag.get(key)
        if key in bag and field["type"] == "object" and not isinstance(block, dict):
            raise invalid_request(f"{bag_label}.{key} must be an object")
        if not isinstance(block, dict):
            continue
        protected = sorted(set(block).intersection(field.get("protected_keys", ())))
        if protected:
            raise invalid_request(
                f"{bag_label}.{field['key']} contains platform-managed keys: "
                + ", ".join(protected)
            )
