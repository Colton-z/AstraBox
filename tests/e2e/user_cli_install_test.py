"""The workload can install a CLI in its standard user bin directory."""

from __future__ import annotations

import json
import shlex
import time

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    create_session,
    get_admin_session_detail,
    poll_until_agent_ready,
    release_session,
    send_and_follow,
    transcript_entries,
    wait_until_settled,
)
from tests.e2e.test_prepared_runtime_activation import _run_json, _sandbox_exec

pytestmark = pytest.mark.e2e


def test_user_cli_install_survives_identity_reestablishment(
    e2e_client: httpx.Client, live_test_deadline: float,
) -> None:
    session = create_session(e2e_client)
    sid = session["session_id"]
    release_session(sid)
    poll_until_agent_ready(e2e_client, sid)
    detail = get_admin_session_detail(e2e_client, sid)
    identity = detail["runtime_identity"]
    execute = _sandbox_exec(detail["sandbox_id"])
    user_uid = identity["uid"]
    assert user_uid != 0, identity["linux_user"]

    def run_as_agent(script: str, marker: str) -> None:
        reply = send_and_follow(
            e2e_client, sid,
            "Use Bash to execute exactly this shell script. Do not use sudo or change the environment. "
            "Report its output after execution.\n```bash\n" + script + "\n```",
            until="end",
        )
        assert reply.error is None, reply.response
        settled = wait_until_settled(e2e_client, sid)
        assert settled["last_response"]["outcome"] == "completed", settled
        while time.monotonic() < live_test_deadline:
            entries = transcript_entries(sid)
            bash_ids = {
                block["id"] for row in entries
                if row["entry"].get("type") == "assistant"
                for block in row["entry"].get("message", {}).get("content", [])
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Bash"
            }
            results = [
                block for row in entries
                if row["entry"].get("type") == "user"
                for block in row["entry"].get("message", {}).get("content", [])
                if isinstance(block, dict) and block.get("type") == "tool_result"
                and block.get("tool_use_id") in bash_ids
                and not block.get("is_error") and marker in json.dumps(block.get("content"))
            ]
            if results:
                return
            time.sleep(0.2)
        pytest.fail(f"native Bash result did not contain {marker!r}")

    command = """
set -eu
test "$(id -u)" != 0
test -w "$HOME/.local"
test -w "$HOME/.local/bin"
install -m 755 /usr/bin/printf "$HOME/.local/bin/astrabox-user-cli-probe"
test "$(command -v astrabox-user-cli-probe)" = "$HOME/.local/bin/astrabox-user-cli-probe"
astrabox-user-cli-probe 'USER_CLI_OK uid=%s\\n' "$(id -u)"
test ! -w "$HOME/.ssh"
test ! -w CONFIG_PLUGIN_DIR
test ! -w /usr/local/bin/astrabox-provision-conversation
""".replace("CONFIG_PLUGIN_DIR", shlex.quote(identity["config_dir"] + "/plugins"))
    run_as_agent(command, f"USER_CLI_OK uid={user_uid}")

    # A pre-existing file with a different owner catches a recursive chown.
    setup = """
import json,os,sys
from pathlib import Path
sentinel=Path(sys.argv[1])/'.local/bin/owner-sentinel'
sentinel.write_text('retain this owner and mode')
os.chown(sentinel,65534,65534)
sentinel.chmod(0o640)
print(json.dumps({'created':str(sentinel)}))
"""
    _run_json([*execute, "python3", "-c", setup, identity["home_dir"]])
    facts_script = """
import hashlib,json,sys
from pathlib import Path
home,config,workspace=map(Path,sys.argv[1:])
sentinel=home/'.local/bin/owner-sentinel'
paths=[home,home/'.local',home/'.local/bin',home/'.local/bin/astrabox-user-cli-probe',
       sentinel,home/'.ssh',config/'plugins',workspace,
       Path('/usr/local/bin/astrabox-provision-conversation')]
result={}
for path in paths:
    st=path.stat()
    result[str(path)]={'uid':st.st_uid,'gid':st.st_gid,'mode':st.st_mode & 0o7777,
                       'sha256':hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None}
print(json.dumps(result))
"""
    facts_command = [*execute, "python3", "-c", facts_script, identity["home_dir"],
                     identity["config_dir"], identity["workspace_source_dir"]]
    before = _run_json(facts_command)
    assert before[f"{identity['home_dir']}/.local/bin"]["uid"] == user_uid
    assert before[f"{identity['home_dir']}/.ssh"]["uid"] == 0
    assert before[f"{identity['config_dir']}/plugins"]["uid"] == 0

    env = {
        "CONV_USER": identity["linux_user"], "CONV_HOME": identity["home_dir"],
        "CONV_WORKSPACE": identity["workspace_source_dir"], "CONV_CONFIG": identity["config_dir"],
        "CONV_CACHE": identity["cache_dir"], "CONV_TMP": identity["temp_dir"],
    }
    result = _run_json([
        *execute, "python3", "-c",
        "import json,subprocess,sys; p=subprocess.run(sys.argv[1:],capture_output=True,text=True); "
        "print(json.dumps({'code':p.returncode,'stdout':p.stdout,'stderr':p.stderr}))",
        "env", *(f"{key}={value}" for key, value in env.items()),
        "bash", "/usr/local/bin/astrabox-provision-conversation",
    ])
    assert result["code"] == 0, result
    assert _run_json(facts_command) == before, "identity bootstrap changed existing ownership/content"
    run_as_agent(
        "astrabox-user-cli-probe 'USER_CLI_REUSED uid=%s\\n' \"$(id -u)\"",
        f"USER_CLI_REUSED uid={user_uid}",
    )
    print("USER_CLI_INSTALL native Bash install, PATH and repeated identity bootstrap passed", flush=True)
