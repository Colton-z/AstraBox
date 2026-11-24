from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

_HASHED_ASSET_RE = re.compile(r".+-[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+$")
_HTML_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}

#: How ``frontend/vite.config.ts`` writes the public path prefix into the built
#: ``index.html``: in front of the entry script, its preloads and stylesheet,
#: and as the ``astrabox-base-path`` meta element the console reads its router
#: basename and API base from.
CONSOLE_BASE_PATH_TOKEN = "__ASTRABOX_BASE_PATH__"


def is_hashed_asset_name(filename: str) -> bool:
    return bool(_HASHED_ASSET_RE.fullmatch(str(filename or "").strip()))


def asset_cache_control_for_name(filename: str) -> str:
    if is_hashed_asset_name(filename):
        return "public, max-age=31536000, immutable"
    return "no-cache"


#: The compressed copies ``frontend/precompress.mjs`` writes beside a built
#: file, in the order they are preferred: Brotli is smaller, gzip is accepted by
#: every browser, including over plain HTTP where browsers do not offer Brotli.
PRECOMPRESSED_CODINGS = (("br", ".br"), ("gzip", ".gz"))


def _accepted_codings(accept_encoding: str) -> dict[str, float]:
    """The ``Accept-Encoding`` codings with their weights; ``q=0`` refuses one."""

    weights: dict[str, float] = {}
    for item in accept_encoding.split(","):
        coding, _, params = item.partition(";")
        coding = coding.strip().lower()
        if not coding:
            continue
        weight = 1.0
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    weight = float(value.strip())
                except ValueError:
                    weight = 0.0
        weights[coding] = weight
    return weights


def precompressed_variants(path: Path) -> list[tuple[str, Path]]:
    """The compressed copies built beside *path*, most preferred first."""

    return [
        (coding, variant)
        for coding, suffix in PRECOMPRESSED_CODINGS
        if (variant := path.with_name(path.name + suffix)).is_file()
    ]


def precompressed_variant(path: Path, accept_encoding: str) -> tuple[str, Path] | None:
    """The built copy of *path* to send to a client sending *accept_encoding*.

    ``None`` sends the file itself: the client accepts none of the codings the
    build wrote a copy in, or the file has no copy (the shell, a small file).
    """

    weights = _accepted_codings(accept_encoding)
    for coding, variant in precompressed_variants(path):
        if weights.get(coding, weights.get("*", 0.0)) > 0:
            return coding, variant
    return None


def console_shell_response(index_html: Path, base_path: str) -> HTMLResponse:
    """Answer a console route with ``index.html`` placed at *base_path*.

    *base_path* is the public path prefix, ``""`` at the root of the host. The
    shell is read on every request, so a rebuilt console is served without a
    restart, and it is never cached: it names the hashed entry the release
    guard in ``frontend/src/frontendRelease.ts`` compares against.

    Raises:
        RuntimeError: the file carries no ``__ASTRABOX_BASE_PATH__`` token, so it
            was not built by this repository's ``frontend/vite.config.ts``. Its
            asset references would be relative to the route instead of the
            prefix, and the console would fail to load at most routes.
    """

    html = index_html.read_text(encoding="utf-8")
    if CONSOLE_BASE_PATH_TOKEN not in html:
        raise RuntimeError(
            f"{index_html} has no {CONSOLE_BASE_PATH_TOKEN} token: it was not built "
            "by this release's frontend/vite.config.ts; rebuild the console"
        )
    return HTMLResponse(
        html.replace(CONSOLE_BASE_PATH_TOKEN, base_path),
        headers=dict(_HTML_HEADERS),
    )
