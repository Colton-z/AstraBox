"""Verified command-channel writer for sandbox scripts."""

from __future__ import annotations

import base64
import hashlib
import json
import textwrap
import uuid
from typing import Any

from astrabox.common.utils.errors import APIError

_SCRIPT_COMMAND_CHUNK_SIZE = 16_000


def _underlying_sandbox(sandbox: Any) -> Any:
    current = sandbox
    seen: set[int] = set()
    while current is not None and hasattr(current, "sandbox"):
        ident = id(current)
        if ident in seen:
            break
        seen.add(ident)
        current = getattr(current, "sandbox")
    return current


def _extract_command_log_text(result: Any) -> str:
    logs = getattr(result, "logs", None)
    if logs is None:
        return ""

    chunks: list[str] = []
    for stream_name in ("stdout", "stderr"):
        stream = getattr(logs, stream_name, None)
        if not stream:
            continue
        for item in stream:
            text = getattr(item, "text", None)
            chunks.append(str(item if text is None else text))
    return "".join(chunks)


def _python_heredoc_command(script: str) -> str:
    return "python - <<'PY'\n" + textwrap.dedent(script).strip() + "\nPY"


def _write_text_file_commands(path: str, content: str, *, mode: int) -> list[str]:
    data_b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    chunks = [
        data_b64[index : index + _SCRIPT_COMMAND_CHUNK_SIZE]
        for index in range(0, len(data_b64), _SCRIPT_COMMAND_CHUNK_SIZE)
    ]
    commands = [
        _python_heredoc_command(
            f"""
from pathlib import Path

path = {json.dumps(path)}
target = Path(path)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_bytes(b"")
"""
        )
    ]
    for chunk in chunks:
        commands.append(
            _python_heredoc_command(
                f"""
import base64
from pathlib import Path

path = {json.dumps(path)}
chunk = {json.dumps(chunk)}
with Path(path).open("ab") as handle:
    handle.write(base64.b64decode(chunk.encode("ascii")))
"""
            )
        )
    commands.append(
        _python_heredoc_command(
            f"""
import json
import os
from pathlib import Path

path = {json.dumps(path)}
target = Path(path)
os.chmod(target, {int(mode)})
print(json.dumps({{"path": path, "bytes": target.stat().st_size}}))
"""
        )
    )
    return commands


def _acquire_install_lock_command(lock_path: str, target_path: str) -> str:
    return _python_heredoc_command(
        f"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

lock_path = {json.dumps(lock_path)}
target_path = {json.dumps(target_path)}
Path(target_path).parent.mkdir(parents=True, exist_ok=True)
deadline = time.time() + 120
stale_after = 600
while True:
    try:
        os.mkdir(lock_path)
        Path(lock_path, "owner").write_text(str(os.getpid()))
        print(json.dumps({{"lock": lock_path, "acquired": True}}))
        break
    except FileExistsError:
        try:
            age = time.time() - os.stat(lock_path).st_mtime
        except FileNotFoundError:
            continue
        if age > stale_after:
            shutil.rmtree(lock_path, ignore_errors=True)
            continue
        if time.time() >= deadline:
            print(json.dumps({{"lock": lock_path, "acquired": False, "error": "timeout"}}))
            sys.exit(1)
        time.sleep(0.2)
"""
    )


def _release_install_lock_command(lock_path: str) -> str:
    return _python_heredoc_command(
        f"""
import json
import shutil

lock_path = {json.dumps(lock_path)}
shutil.rmtree(lock_path, ignore_errors=True)
print(json.dumps({{"lock": lock_path, "released": True}}))
"""
    )


def _atomic_replace_text_file_command(*, temp_path: str, target_path: str, mode: int) -> str:
    return _python_heredoc_command(
        f"""
import json
import os
from pathlib import Path

temp_path = {json.dumps(temp_path)}
target_path = {json.dumps(target_path)}
target = Path(target_path)
target.parent.mkdir(parents=True, exist_ok=True)
os.chmod(temp_path, {int(mode)})
os.replace(temp_path, target_path)
print(json.dumps({{"path": target_path, "bytes": target.stat().st_size}}))
"""
    )


def _verify_text_file_command(path: str, content: str) -> str:
    data = content.encode("utf-8")
    expected_json = json.dumps(
        {
            "path": path,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        separators=(",", ":"),
    )
    return _python_heredoc_command(
        f"""
import hashlib
import json
import sys
from pathlib import Path

expected = json.loads({json.dumps(expected_json)})
target = Path(expected["path"])
if not target.is_file():
    print(json.dumps({{"ok": False, "error": "missing", "expected": expected}}))
    sys.exit(1)
data = target.read_bytes()
actual = {{"path": str(target), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}}
ok = actual["size"] == int(expected["size"]) and actual["sha256"] == str(expected["sha256"])
print(json.dumps({{"ok": ok, "expected": expected, "actual": actual}}, ensure_ascii=False))
if not ok:
    sys.exit(1)
"""
    )


async def install_verified_text_script(
    sandbox: Any,
    *,
    path: str,
    content: str,
    mode: int = 0o755,
    error_code: str,
    error_message: str,
) -> None:
    target_sandbox = _underlying_sandbox(sandbox)
    commands = getattr(target_sandbox, "commands", None)
    run_fn = getattr(commands, "run", None) if commands is not None else None
    if not callable(run_fn):
        raise APIError(
            code=error_code,
            message=f"{error_message}: sandbox command runner is unavailable",
            status_code=502,
        )
    lock_path = f"{path}.install.lock"
    temp_path = f"{path}.tmp.{uuid.uuid4().hex}"
    lock_acquired = False
    try:
        lock_result = await run_fn(_acquire_install_lock_command(lock_path, path))
        if getattr(lock_result, "error", None):
            output = _extract_command_log_text(lock_result)
            raise RuntimeError(
                f"{getattr(lock_result, 'error', None)}; output={output[:1000]!r}"
            )
        lock_acquired = True
        for command in _write_text_file_commands(temp_path, content, mode=mode):
            result = await run_fn(command)
            if getattr(result, "error", None):
                output = _extract_command_log_text(result)
                raise RuntimeError(
                    f"{getattr(result, 'error', None)}; output={output[:1000]!r}"
                )
        verify_result = await run_fn(_verify_text_file_command(temp_path, content))
        if getattr(verify_result, "error", None):
            output = _extract_command_log_text(verify_result)
            raise RuntimeError(
                f"{getattr(verify_result, 'error', None)}; output={output[:1000]!r}"
            )
        replace_result = await run_fn(
            _atomic_replace_text_file_command(
                temp_path=temp_path,
                target_path=path,
                mode=mode,
            )
        )
        if getattr(replace_result, "error", None):
            output = _extract_command_log_text(replace_result)
            raise RuntimeError(
                f"{getattr(replace_result, 'error', None)}; output={output[:1000]!r}"
            )
        verify_result = await run_fn(_verify_text_file_command(path, content))
        if getattr(verify_result, "error", None):
            output = _extract_command_log_text(verify_result)
            raise RuntimeError(
                f"{getattr(verify_result, 'error', None)}; output={output[:1000]!r}"
            )
    except Exception as exc:
        raise APIError(
            code=error_code,
            message=f"{error_message}: {exc}",
            status_code=502,
        ) from exc
    finally:
        if lock_acquired:
            release_result = await run_fn(_release_install_lock_command(lock_path))
            if getattr(release_result, "error", None):
                output = _extract_command_log_text(release_result)
                raise APIError(
                    code=error_code,
                    message=(
                        f"{error_message}: failed to release script install lock: "
                        f"{getattr(release_result, 'error', None)}; output={output[:1000]!r}"
                    ),
                    status_code=502,
                )
