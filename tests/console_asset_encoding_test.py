"""The console's built files are answered with the compressed copy the browser accepts."""

from __future__ import annotations

import gzip
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from astrabox.api.app import _mount_frontend

_SCRIPT = b"export const console = 'astrabox';\n" * 200


def _dist(root: Path) -> Path:
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<script type="module" src="__ASTRABOX_BASE_PATH__/assets/main-abcdefgh.js"></script>',
        encoding="utf-8",
    )
    script = dist / "assets" / "main-abcdefgh.js"
    script.write_bytes(_SCRIPT)
    (dist / "assets" / "main-abcdefgh.js.gz").write_bytes(gzip.compress(_SCRIPT))
    # The Brotli copy's bytes are only compared, never decoded.
    (dist / "assets" / "main-abcdefgh.js.br").write_bytes(b"brotli-copy")
    (dist / "assets" / "tiny-abcdefgh.css").write_bytes(b"a{}")
    return dist


def _get_raw(client: TestClient, path: str, accept_encoding: str) -> tuple[dict[str, str], bytes]:
    # Always sent: the test client otherwise offers gzip on its own.
    with client.stream("GET", path, headers={"Accept-Encoding": accept_encoding}) as response:
        assert response.status_code == 200
        return dict(response.headers), b"".join(response.iter_raw())


def test_a_built_file_is_sent_in_the_coding_the_browser_accepts(tmp_path: Path) -> None:
    app = FastAPI()
    _mount_frontend(app, _dist(tmp_path))
    client = TestClient(app)
    script = "/assets/main-abcdefgh.js"

    headers, body = _get_raw(client, script, "gzip, deflate, br, zstd")
    assert headers["content-encoding"] == "br"
    assert body == b"brotli-copy"
    assert headers["content-type"].startswith("text/javascript")
    assert headers["cache-control"] == "public, max-age=31536000, immutable"
    assert headers["vary"] == "Accept-Encoding"

    # Browsers offer gzip but not Brotli over plain HTTP; q=0 refuses a coding.
    for offered in ("gzip, deflate", "br;q=0, gzip"):
        headers, body = _get_raw(client, script, offered)
        assert headers["content-encoding"] == "gzip"
        assert gzip.decompress(body) == _SCRIPT
        assert headers["content-type"].startswith("text/javascript")

    for offered in ("", "identity"):
        headers, body = _get_raw(client, script, offered)
        assert "content-encoding" not in headers
        assert body == _SCRIPT
        assert headers["vary"] == "Accept-Encoding"

    # A file the build did not compress is sent as it is, with nothing to vary on.
    headers, body = _get_raw(client, "/assets/tiny-abcdefgh.css", "gzip, br")
    assert "content-encoding" not in headers and "vary" not in headers
    assert body == b"a{}"
