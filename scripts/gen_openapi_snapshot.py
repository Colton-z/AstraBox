#!/usr/bin/env python3
"""Render ``tests/data/openapi_snapshot.json`` from the app ``create_app()`` builds.

The snapshot is the reviewable record of the whole HTTP surface, and the input
``scripts/check_api_client.py`` generates the console's
``frontend/src/api/schema.d.ts`` from. Run this script after changing a route or
a response model and commit the result; ``scripts/check_http_surface.py`` fails
while the committed snapshot differs from what this renders.

The app is built without entering its lifespan, so nothing here touches the
network, a database or a sandbox. Every input construction reads is pinned —
identity provider, feature flags, allowed hosts, tracing, the env file and an
empty frontend directory, which keeps the shape API-only — so local state
cannot change the rendering. ``info.version`` is ``pyproject.toml``'s version
rather than the installed distribution's, which an editable install does not
refresh.

Usage::

    python scripts/gen_openapi_snapshot.py

No arguments, no flags: it always (re)writes the snapshot.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

SNAPSHOT_PATH = _REPO_ROOT / "tests" / "data" / "openapi_snapshot.json"


def build_app() -> Any:
    """Construct the real app, its inputs pinned; the lifespan never runs."""
    with tempfile.TemporaryDirectory(prefix="openapi-snapshot-") as workdir:
        os.environ["ASTRABOX_ENV_FILE"] = str(Path(workdir) / "unused.env")
        os.environ["ASTRABOX_WEB_IDENTITY"] = "local"
        os.environ["ASTRABOX_ALLOWED_HOSTS"] = "localhost,127.0.0.1,[::1],testserver"
        os.environ["ASTRABOX_AGENT_ENABLED"] = "true"
        os.environ["ASTRABOX_FRONTEND_DIST"] = str(Path(workdir) / "no-frontend-dist")
        # init_tracing reads this during construction; tracing stays off.
        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        from astrabox.api.app import create_app

        return create_app()


def render_snapshot(app: Any) -> str:
    """The canonical snapshot text: sorted keys, two-space indent, real unicode."""
    schema = app.openapi()
    project = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    schema["info"]["version"] = project["project"]["version"]
    return json.dumps(schema, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    SNAPSHOT_PATH.write_text(render_snapshot(build_app()), encoding="utf-8")
    print(f"wrote {SNAPSHOT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
