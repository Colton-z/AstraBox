from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

_HASHED_ASSET_RE = re.compile(r".+-[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+$")
_HTML_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


def is_hashed_asset_name(filename: str) -> bool:
    return bool(_HASHED_ASSET_RE.fullmatch(str(filename or "").strip()))


def asset_cache_control_for_name(filename: str) -> str:
    if is_hashed_asset_name(filename):
        return "public, max-age=31536000, immutable"
    return "no-cache"


def frontend_html_response(path: str | Path) -> FileResponse:
    return FileResponse(path, headers=dict(_HTML_HEADERS))


class FrontendAssetStaticFiles(StaticFiles):
    def file_response(
        self,
        full_path: str | Path,
        stat_result: Any,
        scope: Any,
        status_code: int = 200,
    ) -> FileResponse:
        response = super().file_response(
            full_path,
            stat_result,
            scope,
            status_code=status_code,
        )
        response.headers.setdefault(
            "Cache-Control",
            asset_cache_control_for_name(Path(full_path).name),
        )
        return response
