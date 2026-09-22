from __future__ import annotations

import os
import re
from urllib.parse import quote


def build_attachment_headers(filename: str) -> dict[str, str]:
    raw_name = str(filename or "").strip() or "download"
    stem, ext = os.path.splitext(raw_name)
    safe_stem = re.sub(r"[^0-9A-Za-z._-]+", "_", stem).strip("._-") or "download"
    safe_ext = re.sub(r"[^0-9A-Za-z.]+", "", ext)
    ascii_name = f"{safe_stem}{safe_ext}" if safe_ext else safe_stem
    encoded_name = quote(raw_name, safe="")
    return {
        "Content-Disposition": (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{encoded_name}"
        )
    }
