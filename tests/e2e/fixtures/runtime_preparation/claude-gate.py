#!/usr/local/bin/python3.12
"""Image-baked gate before the real vendor CLI, with non-secret native receipts."""
import json
import os
import stat
import sys
from pathlib import Path

# The SDK probes the version with `-v` and without its options environment.
if sys.argv[1:] not in (["--version"], ["-v"]):
    target = os.environ.get("ASTRABOX_CFUSE_AGENT_TOKEN_FILE", "")
    home = Path(os.environ["HOME"])
    receipt = home / ".runtime-preparation-native.jsonl"
    try:
        path = Path(target)
        info = path.stat()
        assert path.parent == home
        assert stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.getuid()
        material = json.loads(path.read_text())
        assert material["phase"] == "startup"
        record = {**material, "uid": os.getuid(), "mode": stat.S_IMODE(info.st_mode), "path": str(path)}
    except Exception:
        receipt.open("a").write(json.dumps({"denied": True}) + "\n")
        print("native startup refused: Session material unavailable", file=sys.stderr)
        sys.exit(64)
    receipt.open("a").write(json.dumps(record) + "\n")

os.execv("/usr/local/bin/claude.vendor", ["/usr/local/bin/claude.vendor", *sys.argv[1:]])
