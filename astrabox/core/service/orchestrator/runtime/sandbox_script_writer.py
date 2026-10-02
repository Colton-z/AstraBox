"""Verified command-channel writer for sandbox scripts."""

from __future__ import annotations

import base64
import hashlib
import json
import textwrap
import uuid
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    identity_path_to_source,
    normalize_runtime_identity,
)

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


def _write_text_file_commands(path: str, content: str) -> list[str]:
    data_b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    chunks = [
        data_b64[index : index + _SCRIPT_COMMAND_CHUNK_SIZE]
        for index in range(0, len(data_b64), _SCRIPT_COMMAND_CHUNK_SIZE)
    ] or [""]
    commands = []
    for index, chunk in enumerate(chunks):
        commands.append(
            _python_heredoc_command(
                f"""
import base64
from pathlib import Path

path = {json.dumps(path)}
chunk = {json.dumps(chunk)}
with Path(path).open({json.dumps('wb' if index == 0 else 'ab')}) as handle:
    handle.write(base64.b64decode(chunk.encode("ascii")))
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


def _publish_text_file_command(
    *, temp_path: str, target_path: str, content: str, mode: int
) -> str:
    data = content.encode("utf-8")
    expected_json = json.dumps(
        {
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        separators=(",", ":"),
    )
    return _python_heredoc_command(
        f"""
import hashlib
import json
import os
import sys
from pathlib import Path

expected = json.loads({json.dumps(expected_json)})
temp_path = {json.dumps(temp_path)}
target_path = {json.dumps(target_path)}

def verify(path):
    expected["path"] = path
    target = Path(path)
    if not target.is_file():
        print(json.dumps({{"ok": False, "error": "missing", "expected": expected}}))
        sys.exit(1)
    data = target.read_bytes()
    actual = {{"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}}
    ok = actual["size"] == expected["size"] and actual["sha256"] == expected["sha256"]
    print(json.dumps({{"ok": ok, "expected": expected, "actual": actual}}, ensure_ascii=False))
    if not ok:
        sys.exit(1)

os.chmod(temp_path, {int(mode)})
verify(temp_path)
os.replace(temp_path, target_path)
verify(target_path)
"""
    )


async def install_verified_text_script(
    sandbox: Any,
    *,
    path: str,
    content: str,
    runtime_identity: dict[str, Any] | None,
    mode: int = 0o755,
    error_code: str,
    error_message: str,
) -> None:
    """Publish a runtime-visible path through the box command channel.

    Identity owns the workspace mapping for both cold and prepared placements.
    A null identity explicitly addresses the box filesystem directly.
    """

    if runtime_identity is not None and normalize_runtime_identity(runtime_identity) is None:
        raise APIError(
            code=error_code,
            message=f"{error_message}: runtime identity is incomplete",
            status_code=500,
        )
    path = identity_path_to_source(runtime_identity, path)
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
        for command in _write_text_file_commands(temp_path, content):
            result = await run_fn(command)
            if getattr(result, "error", None):
                output = _extract_command_log_text(result)
                raise RuntimeError(
                    f"{getattr(result, 'error', None)}; output={output[:1000]!r}"
                )
        publish_result = await run_fn(
            _publish_text_file_command(
                temp_path=temp_path,
                target_path=path,
                content=content,
                mode=mode,
            )
        )
        if getattr(publish_result, "error", None):
            output = _extract_command_log_text(publish_result)
            raise RuntimeError(
                f"{getattr(publish_result, 'error', None)}; output={output[:1000]!r}"
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
