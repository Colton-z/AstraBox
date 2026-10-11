"""Compose forwards the server settings an operator sets in containers/.env.

Compose reads containers/.env only to interpolate the Compose files: a variable
the server service does not name never reaches the server, so setting it there
would do nothing and nothing would refuse it. A Compose deployment whose
ASTRABOX_DB_URL points at MongoDB is the operator who has to set
ASTRABOX_SCHEDULER_PROVIDER=none.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_the_scheduler_and_the_enabled_backends_reach_the_server() -> None:
    compose = yaml.safe_load((ROOT / "containers" / "compose.yaml").read_text())
    environment = compose["services"]["server"]["environment"]

    # The fallback is the setting's own default, so an unset or empty value in
    # containers/.env selects what a server started without it selects.
    assert (
        environment["ASTRABOX_SCHEDULER_PROVIDER"]
        == "${ASTRABOX_SCHEDULER_PROVIDER:-dbos}"
    )
    assert environment["ASTRABOX_SANDBOX_BACKENDS"] == "${ASTRABOX_SANDBOX_BACKENDS:-}"
