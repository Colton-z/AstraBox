#!/usr/bin/env python3
"""Write the model catalog a Codex home needs, from the delivered JSON.

One writer for both service shapes — the box-level server and a
conversation's own instance — because the catalog is load-bearing in the
same way for both: Codex looks a model up by slug in this file, and a slug
it cannot find falls back to behaviour that answered one message twice
against this gateway. The vendor's integration guide calls creating the
file a required step, and the transport-deciding fields
(``use_responses_lite``, ``multi_agent_version``) live in it.

Usage: astrabox-model-catalog.py <codex_home>
Reads ``CODEX_MODEL_CATALOG_JSON`` from the environment; doing nothing when
it is unset is correct — a deployment that sends no catalog gets the
vendor's stock behaviour, stated rather than guessed.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: astrabox-model-catalog.py <codex_home>", file=sys.stderr)
        return 64
    raw = os.environ.get("CODEX_MODEL_CATALOG_JSON", "")
    if not raw:
        return 0
    codex_home = pathlib.Path(sys.argv[1])
    codex_home.mkdir(parents=True, exist_ok=True)
    catalog = codex_home / "models.json"
    try:
        json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"astrabox-codex: CODEX_MODEL_CATALOG_JSON is not valid JSON: {exc}", file=sys.stderr)
        return 65
    catalog.write_text(raw)

    # Named in config.toml, where the vendor's own integration guide puts it.
    # A key written after a table header would belong to that table, so it is
    # inserted above the first one rather than appended.
    config = codex_home / "config.toml"
    line = f'model_catalog_json = "{catalog}"'
    body = config.read_text() if config.exists() else ""
    if line in body:
        return 0
    rest = [row for row in body.splitlines() if not row.startswith("model_catalog_json")]
    config.write_text("\n".join([line, "", *rest]).rstrip() + "\n")
    print(f"astrabox-codex: model catalog written ({catalog.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
