"""The DeepSeek Harness launcher keeps the vendor's tokenized URL private and out of
logs.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import sys


SCRIPT = Path(__file__).resolve().parents[1] / "containers/sandbox-deepseek-harness/astrabox-dsh-web"


def test_vendor_url_is_private_and_never_copied_to_logs(tmp_path: Path) -> None:
    url = "http://127.0.0.1:44781/?token=native-issued-secret"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "serve", sys.executable, "-c",
         f"print('dsh web: {url}'); print('ordinary diagnostic')"],
        env={**os.environ, "HOME": str(tmp_path)}, capture_output=True, text=True,
        check=True, timeout=10,
    )
    path = tmp_path / ".deepseek-harness/web-url"
    assert path.read_text().strip() == url
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "native-issued-secret" not in result.stdout + result.stderr
    assert "ordinary diagnostic" in result.stdout
