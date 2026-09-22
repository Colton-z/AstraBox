#!/usr/bin/env python3
"""Check that the English and Chinese console catalogues stay compatible."""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
LOCALE_ROOT = REPO_ROOT / "frontend" / "src" / "i18n" / "locales"
SOURCE_ROOT = REPO_ROOT / "frontend" / "src"
# Read from disk, not listed here: a language is a directory of namespace files
# under `locales/`, and `frontend/src/i18n/index.ts` builds its resources the
# same way. A literal here would let a shipped language sit outside the gate
# that checks key parity, plurals and placeholders.
LANGUAGES = tuple(
    sorted(p.name for p in LOCALE_ROOT.iterdir() if p.is_dir())
) if LOCALE_ROOT.is_dir() else ()
SOURCE_SUFFIXES = {".js", ".jsx", ".ts", ".tsx"}
PLURAL_SUFFIXES = ("_zero", "_one", "_two", "_few", "_many", "_other")
INTERPOLATION = re.compile(r"{{\s*([^,}\s]+)")
# House-style vocabulary, per language and sparse on purpose: a newly shipped
# language has no wording rules until someone writes them, and the absence is
# not a failure.
DISCOURAGED_WORDING: dict[str, tuple[re.Pattern[str], ...]] = {
    "en": tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"\bseams?\b",
            r"\bwiring\b",
            r"\bprose\b",
            r"\bspine\b",
            r"\bprojection\b",
            r"\bcontracts?\b",
            r"\bboundar(?:y|ies)\b",
            r"\bbox(?:es)?\b",
            r"\bfail(?:s|ed|ing)? loud(?:ly)?\b",
            r"\b(?:single )?source of truth\b",
        )
    ),
    "zh": tuple(
        re.compile(pattern)
        for pattern in (
            "接缝",
            "接线",
            "散文",
            "凭证如何到达工具",
            "契约",
            "边界",
            "箱子",
            "回刷",
            "收敛",
            "大声失败",
            "失败即响",
            "唯一真相",
            "钉住",
        )
    ),
}


class DuplicateKeyError(ValueError):
    """A JSON object contains the same key more than once."""


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate key {key!r}")
        result[key] = value
    return result


def load_catalog(path: Path) -> dict[str, Any]:
    # A shipped language missing one namespace is the ordinary way a new
    # language arrives half-added. Name the file rather than raising the
    # reader's own FileNotFoundError at them.
    if not path.is_file():
        raise ValueError(f"{path.relative_to(REPO_ROOT)}: namespace file is missing")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys
        )
    except (json.JSONDecodeError, DuplicateKeyError) as exc:
        raise ValueError(f"{path.relative_to(REPO_ROOT)}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path.relative_to(REPO_ROOT)}: catalogue root must be an object")
    return value


def flatten(value: Any, prefix: str = "") -> dict[str, str]:
    leaves: dict[str, str] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else key
            leaves.update(flatten(child, child_prefix))
        return leaves
    if not isinstance(value, str):
        raise ValueError(f"{prefix}: translation value must be a string")
    leaves[prefix] = value
    return leaves


def logical_key(key: str) -> str:
    for suffix in PLURAL_SUFFIXES:
        if key.endswith(suffix):
            return key[: -len(suffix)]
    return key


def placeholders(value: str) -> set[str]:
    return set(INTERPOLATION.findall(value))


def strip_javascript_comments(source: str) -> str:
    """Remove JS comments while preserving quoted strings and line positions."""

    output: list[str] = []
    index = 0
    quote: str | None = None
    while index < len(source):
        char = source[index]
        if quote is not None:
            output.append(char)
            if char == "\\" and index + 1 < len(source):
                index += 1
                output.append(source[index])
            elif char == quote:
                quote = None
            index += 1
            continue

        if char in {"'", '"', "`"}:
            quote = char
            output.append(char)
            index += 1
            continue
        if source.startswith("//", index):
            end = source.find("\n", index)
            if end == -1:
                output.extend(" " * (len(source) - index))
                break
            output.extend(" " * (end - index))
            output.append("\n")
            index = end + 1
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            end = len(source) if end == -1 else end + 2
            output.extend("\n" if item == "\n" else " " for item in source[index:end])
            index = end
            continue
        output.append(char)
        index += 1
    return "".join(output)


def production_source_files(source_root: Path) -> list[Path]:
    return sorted(
        path
        for path in source_root.rglob("*")
        if path.is_file()
        and path.suffix in SOURCE_SUFFIXES
        and ".test." not in path.name
        and ".spec." not in path.name
        and "__tests__" not in path.parts
    )


def validate_source_usage(
    catalogues: dict[str, dict[str, str]], source_root: Path = SOURCE_ROOT
) -> list[str]:
    """Reject missing references and catalogue leaves unused by production code."""

    if not catalogues:
        return []
    namespace_pattern = "|".join(re.escape(namespace) for namespace in sorted(catalogues))
    static_key = re.compile(
        rf"(?P<quote>['\"`])(?P<key>(?:{namespace_pattern}):[A-Za-z0-9_.-]+)(?P=quote)"
    )
    dynamic_prefix = re.compile(rf"`(?P<prefix>(?:{namespace_pattern}):[A-Za-z0-9_.-]*)\$\{{")

    references: set[str] = set()
    prefixes: set[str] = set()
    for path in production_source_files(source_root):
        source = strip_javascript_comments(path.read_text(encoding="utf-8"))
        for match in static_key.finditer(source):
            namespace, key = match.group("key").split(":", 1)
            references.add(f"{namespace}:{logical_key(key)}")
        prefixes.update(match.group("prefix") for match in dynamic_prefix.finditer(source))

    catalogue_keys = {
        f"{namespace}:{logical_key(key)}"
        for namespace, leaves in catalogues.items()
        for key in leaves
    }
    missing = sorted(references - catalogue_keys)
    unused = sorted(
        key
        for key in catalogue_keys - references
        if not any(key.startswith(prefix) for prefix in prefixes)
    )
    return [
        *(f"source uses missing translation key {key}" for key in missing),
        *(f"unused translation key {key}" for key in unused),
    ]


def validate_wording(filename: str, catalogues: dict[str, dict[str, str]]) -> list[str]:
    errors: list[str] = []
    for language in LANGUAGES:
        for key, value in catalogues[language].items():
            for pattern in DISCOURAGED_WORDING[language]:
                match = pattern.search(value)
                if match:
                    errors.append(
                        f"{filename}: {language} {key} uses discouraged wording {match.group(0)!r}"
                    )
    return errors


def validate_namespace(filename: str, catalogues: dict[str, dict[str, str]]) -> list[str]:
    errors: list[str] = []
    grouped: dict[str, dict[str, list[tuple[str, str]]]] = {
        language: defaultdict(list) for language in LANGUAGES
    }
    for language, leaves in catalogues.items():
        for key, value in leaves.items():
            grouped[language][logical_key(key)].append((key, value))

    en_keys = set(grouped["en"])
    zh_keys = set(grouped["zh"])
    for key in sorted(en_keys - zh_keys):
        errors.append(f"{filename}: missing zh key for {key}")
    for key in sorted(zh_keys - en_keys):
        errors.append(f"{filename}: missing en key for {key}")

    for key in sorted(en_keys & zh_keys):
        for language in LANGUAGES:
            variants = grouped[language][key]
            suffixes = {
                suffix
                for actual, _value in variants
                for suffix in PLURAL_SUFFIXES
                if actual.endswith(suffix)
            }
            if language == "en" and suffixes and not {"_one", "_other"}.issubset(suffixes):
                errors.append(f"{filename}: en plural {key} must provide both _one and _other")

            token_sets = {frozenset(placeholders(value)) for _actual, value in variants}
            if len(token_sets) > 1:
                names = ", ".join(actual for actual, _value in variants)
                errors.append(f"{filename}: placeholder mismatch among {language} variants {names}")

        en_tokens = set().union(*(placeholders(value) for _actual, value in grouped["en"][key]))
        zh_tokens = set().union(*(placeholders(value) for _actual, value in grouped["zh"][key]))
        if en_tokens != zh_tokens:
            errors.append(
                f"{filename}: placeholders differ for {key}: "
                f"en={sorted(en_tokens)} zh={sorted(zh_tokens)}"
            )
    return errors


def main() -> int:
    errors: list[str] = []
    english_catalogues: dict[str, dict[str, str]] = {}
    files_by_language = {
        language: {path.name for path in (LOCALE_ROOT / language).glob("*.json")}
        for language in LANGUAGES
    }
    if files_by_language["en"] != files_by_language["zh"]:
        for filename in sorted(files_by_language["en"] - files_by_language["zh"]):
            errors.append(f"missing zh catalogue: {filename}")
        for filename in sorted(files_by_language["zh"] - files_by_language["en"]):
            errors.append(f"missing en catalogue: {filename}")

    checked_keys = 0
    for filename in sorted(files_by_language["en"] & files_by_language["zh"]):
        catalogues: dict[str, dict[str, str]] = {}
        try:
            for language in LANGUAGES:
                catalogues[language] = flatten(load_catalog(LOCALE_ROOT / language / filename))
            english_catalogues[Path(filename).stem] = catalogues["en"]
            checked_keys += len({logical_key(key) for key in catalogues["en"]})
            errors.extend(validate_namespace(filename, catalogues))
            errors.extend(validate_wording(filename, catalogues))
        except ValueError as exc:
            errors.append(str(exc))

    errors.extend(validate_source_usage(english_catalogues))

    if errors:
        print("Console translation catalogue errors:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print(
        f"i18n catalogues OK — {len(files_by_language['en'])} namespaces, "
        f"{checked_keys} logical keys per language"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
