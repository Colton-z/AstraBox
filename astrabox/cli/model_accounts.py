"""``astrabox model-account`` — link a ChatGPT account to your AstraBox user, and list them.

OpenAI's "Sign in with ChatGPT" sends the browser back to an HTTP loopback
address, ``http://127.0.0.1:<port>/auth/callback``. That address reaches the
computer running the browser, not the AstraBox server, so ``login`` runs the
listener here: it binds a free port on ``127.0.0.1`` before anything opens the
browser, asks the deployment to start a sign-in for that address, opens the
sign-in page, and hands the redirect's query to the deployment, which exchanges
the code and keeps the tokens. No token reaches this machine.
"""

from __future__ import annotations

import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from astrabox.cli.client import ApiClient, as_items, resolve_endpoint
from astrabox.cli.flags import connection_flags
from astrabox.cli.output import EXIT_FAILED, EXIT_OK, CliError, emit

_AUTHORIZATIONS = "/api/v1/me/model-accounts/chatgpt/authorizations"
_ACCOUNTS = "/api/v1/me/model-accounts"
_CALLBACK_PATH = "/auth/callback"

#: How long ``login`` waits for the browser to come back. The deployment keeps
#: a started sign-in open for ten minutes.
DEFAULT_LOGIN_TIMEOUT_SECONDS = 600

#: Completing a sign-in makes the deployment call OpenAI's token endpoint and
#: read its signing keys, so its answer can take longer than an ordinary call.
_COMPLETE_TIMEOUT_SECONDS = 120.0

_ACCOUNT_COLUMNS = ("label", "email", "status", "is_default", "account_id")

_DONE_PAGE = (
    b"<!doctype html><html><head><meta charset=\"utf-8\"><title>AstraBox</title></head>"
    b"<body style=\"font-family: system-ui, sans-serif; margin: 3rem\">"
    b"<h1>AstraBox received the sign-in</h1>"
    b"<p>You can close this tab and return to your terminal.</p></body></html>"
)


def register(subparsers: Any) -> None:
    """Add ``astrabox model-account`` to the top-level parser."""
    group = subparsers.add_parser(
        "model-account",
        help="Link a ChatGPT account whose plan pays for your model requests.",
        description=(
            "Manage the model accounts linked to your AstraBox user. A linked "
            "ChatGPT account lets conversations you start spend your ChatGPT plan."
        ),
    )
    commands = group.add_subparsers(dest="model_account_command", required=True)

    login = commands.add_parser(
        "login",
        parents=[connection_flags()],
        help="Sign in to a ChatGPT account in your browser and link it.",
        description=(
            "Open OpenAI's sign-in page in your browser and link the account you "
            "approve. Run this on the computer whose browser signs in: OpenAI "
            "returns the browser to a listener on 127.0.0.1 here."
        ),
    )
    login.add_argument("vendor", choices=["chatgpt"], help="The account's vendor.")
    login.add_argument(
        "--account",
        help="Sign in to this linked account again (its account_id) instead of linking another.",
    )
    login.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_LOGIN_TIMEOUT_SECONDS,
        help="Seconds to wait for the browser to return (default: %(default)s).",
    )
    login.set_defaults(func=_cmd_login)

    listing = commands.add_parser(
        "list",
        parents=[connection_flags()],
        help="List your linked model accounts.",
        description="List the model accounts linked to your AstraBox user.",
    )
    listing.set_defaults(func=_cmd_list)


class _Callback:
    """The first redirect that carries the sign-in's state, once the state is known."""

    def __init__(self) -> None:
        self.state = ""
        self.query: dict[str, str] = {}
        self.arrived = threading.Event()


def _handler(callback: _Callback) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's method name
            parts = urlsplit(self.path)
            query = {name: values[0] for name, values in parse_qs(parts.query).items()}
            # A request without this sign-in's state is not OpenAI's redirect;
            # forwarding it would use the sign-in up.
            if (
                parts.path != _CALLBACK_PATH
                or not callback.state
                or query.get("state") != callback.state
            ):
                self.send_error(404)
                return
            if not callback.arrived.is_set():
                callback.query = query
                callback.arrived.set()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_DONE_PAGE)))
            self.end_headers()
            self.wfile.write(_DONE_PAGE)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature
            # The default writes each request line to stderr, and the redirect's
            # line carries the authorization code.
            return

    return Handler


def _cmd_login(args: Any) -> int:
    """Handle ``astrabox model-account login chatgpt``."""
    endpoint = resolve_endpoint(endpoint=args.endpoint, token=args.token)
    callback = _Callback()
    # The listener runs before the sign-in starts, so the browser can never
    # come back to a closed port.
    server = HTTPServer(("127.0.0.1", 0), _handler(callback))
    listener = threading.Thread(target=server.serve_forever, daemon=True)
    listener.start()
    try:
        redirect_uri = f"http://127.0.0.1:{server.server_address[1]}{_CALLBACK_PATH}"
        body: dict[str, Any] = {"redirect_uri": redirect_uri}
        if args.account:
            body["account_id"] = args.account
        with ApiClient(endpoint, timeout=_COMPLETE_TIMEOUT_SECONDS) as client:
            started = client.post(_AUTHORIZATIONS, json=body)
            authorize_url = str(started["authorize_url"])
            callback.state = parse_qs(urlsplit(authorize_url).query).get("state", [""])[0]
            print(
                "Opening the ChatGPT sign-in in your browser. If it does not open, "
                f"visit:\n\n  {authorize_url}\n",
                file=sys.stderr,
            )
            webbrowser.open(authorize_url)
            if not callback.arrived.wait(timeout=args.timeout):
                raise CliError(
                    f"the browser did not return to {redirect_uri} within {args.timeout}s; "
                    "run the command again",
                    exit_code=EXIT_FAILED,
                )
            query = callback.query
            completion = {
                name: query[name] for name in ("code", "client_id", "error") if query.get(name)
            }
            account = client.post(
                f"{_AUTHORIZATIONS}/{started['authorization_id']}/callback",
                json={"state": query["state"], **completion},
            )
    finally:
        server.shutdown()
        server.server_close()
    emit(account, output=args.output, table=[account], columns=_ACCOUNT_COLUMNS)
    return EXIT_OK


def _cmd_list(args: Any) -> int:
    """Handle ``astrabox model-account list``."""
    with ApiClient(resolve_endpoint(endpoint=args.endpoint, token=args.token)) as client:
        accounts = as_items(client.get(_ACCOUNTS))
    emit(accounts, output=args.output, table=accounts, columns=_ACCOUNT_COLUMNS)
    return EXIT_OK


__all__ = ["register"]
