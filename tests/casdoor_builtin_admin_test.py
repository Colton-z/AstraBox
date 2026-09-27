"""Casdoor's own administrator does not keep the default password.

Casdoor creates ``built-in/admin`` with the password ``123`` when it
initializes its database, and lets that account sign in to every application,
AstraBox's included. ``containers/casdoor/secure-builtin-admin.sh`` replaces the
password through Casdoor's API at every start. These cases run the script
against a stand-in for the Casdoor endpoints it calls, which answers the way
casbin/casdoor:3.128.0 answered on a real deployment: ``/api/login`` with a
JSON body and a session cookie, ``/api/set-password`` with form fields, and a
wrong-password count that a successful sign-in resets.
"""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "containers/casdoor/secure-builtin-admin.sh"
_GENERATED = "a" * 64
_WRONG = "password or code is incorrect, you have {left} remaining chances"
_LOCKED = (
    "You have entered the wrong password or code too many times, "
    "please wait for 15 minutes and try again"
)


class _Casdoor:
    """The built-in/admin account as Casdoor keeps it."""

    def __init__(self, password: str, *, locked: bool = False, refuse_set: bool = False):
        self.password = password
        self.locked = locked
        self.refuse_set = refuse_set
        self.wrong_attempts = 0
        self.set_password_calls: list[dict[str, str]] = []
        self.sessions: set[str] = set()

    def handler(self) -> type[BaseHTTPRequestHandler]:
        casdoor = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def _answer(self, body: dict[str, object], cookie: str | None = None) -> None:
                # Compact, as Go's encoding/json writes it: Casdoor answers
                # {"status":"ok",...} with no space after the colon.
                payload = json.dumps(body, separators=(",", ":")).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if cookie:
                    self.send_header("Set-Cookie", f"casdoor_session_id={cookie}; Path=/")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                if self.path == "/.well-known/openid-configuration":
                    self._answer({"issuer": "http://casdoor"})
                else:
                    self.send_error(404)

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                if self.path == "/api/login":
                    body = json.loads(raw)
                    assert (body["application"], body["organization"], body["username"]) == (
                        "app-built-in",
                        "built-in",
                        "admin",
                    )
                    if casdoor.locked:
                        self._answer({"status": "error", "msg": _LOCKED})
                    elif body["password"] == casdoor.password:
                        casdoor.wrong_attempts = 0
                        session = f"s{len(casdoor.sessions)}"
                        casdoor.sessions.add(session)
                        self._answer({"status": "ok", "data": "built-in/admin"}, cookie=session)
                    else:
                        casdoor.wrong_attempts += 1
                        left = 5 - casdoor.wrong_attempts
                        self._answer({"status": "error", "msg": _WRONG.format(left=left)})
                elif self.path == "/api/set-password":
                    fields = {key: values[0] for key, values in parse_qs(raw).items()}
                    casdoor.set_password_calls.append(fields)
                    cookie = self.headers.get("Cookie") or ""
                    signed_in = any(f"casdoor_session_id={s}" in cookie for s in casdoor.sessions)
                    if casdoor.refuse_set or not signed_in:
                        self._answer({"status": "error", "msg": "Please login first"})
                    elif (fields["userOwner"], fields["userName"]) != ("built-in", "admin"):
                        self._answer({"status": "error", "msg": "wrong user"})
                    elif fields["oldPassword"] != casdoor.password:
                        self._answer({"status": "error", "msg": "old password is wrong"})
                    else:
                        casdoor.password = fields["newPassword"]
                        self._answer({"status": "ok"})
                else:
                    self.send_error(404)

        return Handler


def _run(tmp_path: Path, casdoor: _Casdoor) -> tuple[subprocess.CompletedProcess[str], Path]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), casdoor.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    password_file = tmp_path / "casdoor_builtin_admin_password"
    password_file.write_text(_GENERATED + "\n", encoding="ascii")
    checked = tmp_path / "built-in-admin-checked"
    try:
        result = subprocess.run(
            [
                "/bin/sh",
                str(_SCRIPT),
                f"http://127.0.0.1:{server.server_port}",
                str(password_file),
                str(checked),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        server.shutdown()
        server.server_close()
    return result, checked


def test_the_default_password_is_replaced_with_the_generated_one(tmp_path: Path) -> None:
    casdoor = _Casdoor("123")

    result, checked = _run(tmp_path, casdoor)

    assert result.returncode == 0, result.stderr
    assert casdoor.password == _GENERATED
    assert [call["oldPassword"] for call in casdoor.set_password_calls] == ["123"]
    assert checked.exists()
    # The script ends signed in with the generated password, which resets
    # Casdoor's count of wrong ones.
    assert casdoor.wrong_attempts == 0
    assert list(tmp_path.glob("built-in-admin.*")) == [], "the session cookie outlived the check"


def test_a_secured_account_is_left_alone_without_a_wrong_sign_in(tmp_path: Path) -> None:
    casdoor = _Casdoor(_GENERATED)

    result, checked = _run(tmp_path, casdoor)

    assert result.returncode == 0, result.stderr
    assert casdoor.set_password_calls == []
    assert casdoor.wrong_attempts == 0
    assert checked.exists()


def test_a_password_an_administrator_chose_is_kept(tmp_path: Path) -> None:
    casdoor = _Casdoor("chosen-by-an-administrator")

    result, checked = _run(tmp_path, casdoor)

    assert result.returncode == 0, result.stderr
    assert casdoor.password == "chosen-by-an-administrator"
    assert casdoor.set_password_calls == []
    assert "it is kept" in result.stderr
    assert checked.exists()


@pytest.mark.parametrize(
    ("casdoor", "reason"),
    [
        (_Casdoor("123", locked=True), "please wait for 15 minutes"),
        (_Casdoor("123", refuse_set=True), "cannot replace the default password"),
    ],
    ids=["locked-account", "set-password-refused"],
)
def test_an_account_that_may_still_accept_the_default_is_never_reported_checked(
    tmp_path: Path, casdoor: _Casdoor, reason: str
) -> None:
    """Casdoor's health check reads the checked file, and AstraBox waits for it."""

    result, checked = _run(tmp_path, casdoor)

    assert result.returncode != 0
    assert reason in result.stderr
    assert not checked.exists()
    assert casdoor.password == "123"
