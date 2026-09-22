"""Slash command metadata normalization shared by backend write/read paths."""

from __future__ import annotations

from typing import Any


def _clean_command_name(value: Any) -> str:
    return str(value or "").strip().lstrip("/")


def _clean_description(value: Any) -> str:
    return str(value or "").strip()


def normalize_slash_command_details(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []

    explicit_prefixes = _explicit_command_prefixes(value)
    details_by_key: dict[str, dict[str, str]] = {}
    ordered_keys: list[str] = []

    def add_command(raw_name: Any, raw_description: Any = "") -> None:
        name = _clean_command_name(raw_name)
        if not name:
            return
        description = _clean_description(raw_description)
        description_prefix = _description_plugin_prefix(description)
        if (
            description_prefix
            and description_prefix in explicit_prefixes
            and ":" not in name
        ):
            name = f"{description_prefix}:{name}"
        key = name.lower()
        existing = details_by_key.get(key)
        if existing is None:
            detail = {"name": name}
            if description:
                detail["description"] = description
            details_by_key[key] = detail
            ordered_keys.append(key)
            return
        if description and not existing.get("description"):
            existing["description"] = description

    for item in value:
        raw_name = item
        description = ""
        if isinstance(item, dict):
            raw_name = item.get("name") or item.get("command")
            description = _clean_description(item.get("description"))
        add_command(raw_name, description)
        if isinstance(item, dict):
            aliases = item.get("aliases")
            if isinstance(aliases, list):
                for alias in aliases:
                    add_command(alias, description)

    return [details_by_key[key] for key in ordered_keys]


def merge_slash_command_details(
    *,
    commands: Any,
    slash_commands: Any = None,
    skills: Any = None,
) -> list[dict[str, str]]:
    described_details = normalize_slash_command_details(commands)
    canonical_details = normalize_slash_command_details(
        [
            *(_list_items(slash_commands)),
            *(_list_items(skills)),
        ]
    )
    if not canonical_details:
        return described_details

    described_by_key = {
        item["name"].lower(): item
        for item in described_details
    }
    described_by_suffix: dict[str, list[dict[str, str]]] = {}
    for item in described_details:
        suffix = item["name"].rsplit(":", 1)[-1].lower()
        described_by_suffix.setdefault(suffix, []).append(item)

    details_by_key: dict[str, dict[str, str]] = {}
    ordered_keys: list[str] = []
    covered_described_keys: set[str] = set()

    def add_detail(raw_name: Any, raw_description: Any = "") -> None:
        name = _clean_command_name(raw_name)
        if not name:
            return
        key = name.lower()
        description = _clean_description(raw_description)
        existing = details_by_key.get(key)
        if existing is None:
            detail = {"name": name}
            if description:
                detail["description"] = description
            details_by_key[key] = detail
            ordered_keys.append(key)
            return
        if description and not existing.get("description"):
            existing["description"] = description

    for item in canonical_details:
        name = item["name"]
        key = name.lower()
        source = described_by_key.get(key)
        if source is None and ":" in name:
            suffix_matches = described_by_suffix.get(name.rsplit(":", 1)[-1].lower(), [])
            if len(suffix_matches) == 1:
                source = suffix_matches[0]
        description = item.get("description") or ""
        if source is not None:
            covered_described_keys.add(source["name"].lower())
            description = source.get("description") or description
        add_detail(name, description)

    for item in described_details:
        key = item["name"].lower()
        if key in covered_described_keys:
            continue
        add_detail(item["name"], item.get("description") or "")

    return [details_by_key[key] for key in ordered_keys]


def normalize_slash_commands(value: Any) -> list[str]:
    return [item["name"] for item in normalize_slash_command_details(value)]


def _list_items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _explicit_command_prefixes(value: list[Any]) -> set[str]:
    prefixes: set[str] = set()
    for item in value:
        raw_name = item
        if isinstance(item, dict):
            raw_name = item.get("name") or item.get("command")
        name = _clean_command_name(raw_name)
        if ":" not in name:
            continue
        prefix = name.split(":", 1)[0].strip()
        if prefix:
            prefixes.add(prefix)
    return prefixes


def _description_plugin_prefix(description: str) -> str:
    if not description.startswith("("):
        return ""
    close = description.find(")")
    if close <= 1:
        return ""
    prefix = description[1:close].strip()
    if not prefix:
        return ""
    if any(ch.isspace() for ch in prefix):
        return ""
    if any(ch in prefix for ch in (":", "/", "\\")):
        return ""
    return prefix
