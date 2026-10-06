"""Validate the closed capability manifest before publishing a version."""

from __future__ import annotations

from typing import Any

from astrabox.common.utils.errors import APIError

from .limits import LEVELS, RULE_COUNT

CAPABILITIES = frozenset({"artifact", "db", "sample", "agent", "user", "room", "downloads"})


def invalid(message: str) -> APIError:
    return APIError(code="ARTIFACT_MANIFEST_INVALID", message=message, status_code=400)


def path_segments(path: str, *, rule: bool = False) -> list[str]:
    if not isinstance(path, str):
        raise invalid("path must be a string")
    if rule and path in {"", "/"}:
        return []
    parts = path.split("/")
    if any(not p or p in {".", ".."} or "|" in p or "\x00" in p for p in parts):
        raise invalid("path must contain non-empty slash-separated segments")
    for index, part in enumerate(parts):
        if "{" in part or "}" in part:
            if not rule or part != "{self}" or index != len(parts) - 1:
                raise invalid("{self} is allowed only as a rule's final segment")
    return parts


def validate_rules(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > RULE_COUNT:
        raise invalid(f"db rules must be an array of at most {RULE_COUNT} rules")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rule in value:
        if (
            not isinstance(rule, dict)
            or set(rule) - {"path", "read", "write"}
            or "path" not in rule
        ):
            raise invalid("each db rule has path and optional read/write levels")
        parts = path_segments(rule["path"], rule=True)
        path = "/".join(parts)
        if path in seen:
            raise invalid("db rule paths must be unique")
        seen.add(path)
        if not ({"read", "write"} & set(rule)):
            raise invalid("db rule must set read or write")
        if any(
            not isinstance(rule[key], str) or rule[key] not in {"view", "interact", "owner"}
            for key in ("read", "write")
            if key in rule
        ):
            raise invalid("db rule levels are view, interact or owner")
        result.append({**rule, "path": path})
    by_path = {r["path"]: r for r in result}
    for rule in result:
        parts = path_segments(rule["path"], rule=True)
        if parts and parts[-1] == "{self}":
            prefix = by_path.get("/".join(parts[:-1]))
            if prefix is not None and not {"read", "write"} <= set(prefix):
                raise invalid("a rule at a {self} prefix must set both read and write")
        read, write = "view", "interact"
        for ancestor in sorted(result, key=lambda r: len(path_segments(r["path"], rule=True))):
            ap = path_segments(ancestor["path"], rule=True)
            if parts[: len(ap)] == ap:
                read = ancestor.get("read", read)
                write = ancestor.get("write", write)
        if LEVELS[write] < LEVELS[read]:
            raise invalid("write level must be at least the inherited read level")
    return result


def validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise invalid("capabilities must be an object")
    for name in value:
        if name not in CAPABILITIES:
            raise APIError(
                code="ARTIFACT_CAPABILITY_UNKNOWN",
                message=f"unknown capability: {name}",
                status_code=400,
            )
    result: dict[str, Any] = {}
    for name, config in value.items():
        if name == "downloads":
            if config is not True:
                raise invalid("downloads must be true")
            result[name] = True
            continue
        if not isinstance(config, dict):
            raise invalid(f"{name} must be an object")
        allowed = {"rules"} if name == "db" else {"topics"} if name == "room" else set()
        if set(config) - allowed:
            raise invalid(f"unknown {name} configuration field")
        result[name] = dict(config)
        if name == "db" and "rules" in config:
            result[name]["rules"] = validate_rules(config["rules"])
        if name == "room" and "topics" in config:
            topics = config["topics"]
            if not isinstance(topics, dict) or any(
                not isinstance(k, str)
                or not k
                or "/" in k
                or "\x00" in k
                or not isinstance(v, str)
                or v not in {"view", "interact", "owner"}
                for k, v in topics.items()
            ):
                raise invalid("room topics map non-empty names to view, interact or owner")
    return result
