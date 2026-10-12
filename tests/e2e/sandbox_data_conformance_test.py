"""A box of the built-in provider keeps the data-plane contract core relies on."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from tests.e2e._engine_profile import current_profile
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container

pytestmark = pytest.mark.e2e


def test_a_box_keeps_the_data_plane_contract(live_test_deadline: float) -> None:
    """Every clause of ``astrabox.testing.sandbox_data_conformance`` on a real box.

    The check runs in the server container, where the deployment's sandbox
    wiring is, against a box booted from this engine profile's image.
    """

    script = Path(__file__).with_name("sandbox_data_conformance.py").read_text()
    result = subprocess.run(
        ["docker", "exec", "-i", require_service_container(SERVER_CONTAINER_HANDLE),
         "python", "-", current_profile()["image"], current_profile()["engine_kind"]],
        input=script, capture_output=True, text=True, check=False,
        timeout=max(1, live_test_deadline - time.monotonic()),
    )
    print(result.stdout, flush=True)
    assert result.returncode == 0, result.stderr[-4000:]
