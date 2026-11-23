#!/usr/bin/env python3
"""Write the model catalog a Codex home needs for the platform's model.

One writer for both service shapes — the box-level server and a
conversation's own instance — because the catalog is load-bearing in the
same way for both. Codex looks its model up by slug: its release's bundled
catalog names the models it knows, by the longest slug that prefixes the
model, retried once without a ``namespace/`` prefix. A model it does not know
falls back to behaviour that answered one message twice against this
gateway, and the transport-deciding fields (``use_responses_lite``,
``multi_agent_version``) live in the entry.

So a model the bundled catalog names needs no file, and Codex keeps its own
entry. Any other model gets this image's gateway-model template completed
with the facts the platform delivers in ``CODEX_GATEWAY_MODEL_JSON``: the
slug, and what the model connection states about it. The entry joins the
bundled catalog rather than replacing it, because ``model_catalog_json``
replaces the whole catalog for the process.

Usage: astrabox-model-catalog.py <codex_home>
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

CODEX_PREFIX = pathlib.Path("/opt/codex")
BUNDLED_CATALOG = CODEX_PREFIX / "bundled-models.json"
GATEWAY_TEMPLATE = CODEX_PREFIX / "gateway-model-template.json"
CATALOG_KEY = "model_catalog_json"


def _known(slug: str, bundled: list[str]) -> bool:
    candidates = [slug, *([slug.split("/", 1)[1]] if "/" in slug else [])]
    return any(candidate.startswith(known) for candidate in candidates for known in bundled)


def _name_catalog(config: pathlib.Path, catalog: pathlib.Path | None) -> None:
    # Named in config.toml, where the vendor's own integration guide puts it.
    # A key written after a table header would belong to that table, so it is
    # inserted above the first one rather than appended.
    body = config.read_text() if config.exists() else ""
    rest = [row for row in body.splitlines() if not row.startswith(CATALOG_KEY)]
    head = [f'{CATALOG_KEY} = "{catalog}"', ""] if catalog is not None else []
    config.write_text("\n".join([*head, *rest]).rstrip() + "\n")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: astrabox-model-catalog.py <codex_home>", file=sys.stderr)
        return 64
    raw = os.environ.get("CODEX_GATEWAY_MODEL_JSON", "")
    if not raw:
        print("astrabox-codex: CODEX_GATEWAY_MODEL_JSON is unset; the platform names the model", file=sys.stderr)
        return 65
    try:
        facts = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"astrabox-codex: CODEX_GATEWAY_MODEL_JSON is not valid JSON: {exc}", file=sys.stderr)
        return 65
    slug = facts.get("slug") if isinstance(facts, dict) else None
    if not isinstance(slug, str) or not slug:
        print("astrabox-codex: CODEX_GATEWAY_MODEL_JSON names no slug", file=sys.stderr)
        return 65

    codex_home = pathlib.Path(sys.argv[1])
    codex_home.mkdir(parents=True, exist_ok=True)
    config = codex_home / "config.toml"
    bundled = json.loads(BUNDLED_CATALOG.read_text())["models"]
    if _known(slug, [str(model["slug"]) for model in bundled]):
        _name_catalog(config, None)
        print(f"astrabox-codex: {slug} is in Codex's bundled catalog")
        return 0

    template = json.loads(GATEWAY_TEMPLATE.read_text())
    catalog = codex_home / "models.json"
    catalog.write_text(json.dumps({"models": [*bundled, {**template, **facts}]}, ensure_ascii=False))
    _name_catalog(config, catalog)
    print(f"astrabox-codex: model catalog written for {slug} ({catalog.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
