"""Retiring capacity cancels an allocated but unreturned native sandbox create."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from tests.e2e._engine_profile import current_profile
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container

pytestmark = pytest.mark.e2e


def test_retirement_reclaims_a_create_before_its_response_reaches_the_sdk(
    live_test_deadline: float,
) -> None:
    script = Path(__file__).with_name("pool_create_cancellation.py").read_text()
    result = subprocess.run(
        ["docker", "exec", "-i", require_service_container(SERVER_CONTAINER_HANDLE),
         "python", "-", current_profile()["image"]],
        input=script, capture_output=True, text=True, check=False,
        timeout=max(1, live_test_deadline - time.monotonic()),
    )
    print(result.stdout, flush=True)
    assert result.returncode == 0, result.stderr[-4000:]
