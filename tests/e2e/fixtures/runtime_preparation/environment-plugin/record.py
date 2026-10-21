"""Native plugin receipt, outside the task workspace."""
import json
import os
from pathlib import Path
import sys

sys.stdin.read()
record = {key: os.environ.get(key) for key in (
    "ASTRABOX_SESSION_URL", "RUNTIME_IDENTITY_SESSION_ID", "BINDING_FIXTURE_OWNER",
    "PREWARM_ENV_BASELINE", "CLAUDE_PID",
)}
path = Path.home() / ".prewarm-session-environment.jsonl"
with os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a") as stream:
    stream.write(json.dumps(record) + "\n")
