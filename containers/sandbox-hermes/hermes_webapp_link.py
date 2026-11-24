"""Get a Webapp launch link from the ready gateway's owner-only control socket."""

from hermes_cli.webapp_launcher import GatewayUnreachable, _control
from hermes_constants import get_hermes_home


def main() -> int:
    try:
        response = _control(get_hermes_home(), "webapp-launch")
    except GatewayUnreachable:
        return 75
    if not response.get("ok"):
        return 75
    print(response["result"]["url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
