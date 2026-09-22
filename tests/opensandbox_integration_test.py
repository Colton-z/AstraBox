"""OpenSandbox integration guards for the supported SDK contract."""

from __future__ import annotations

import subprocess
import sys


def test_importing_sandbox_client_does_not_monkeypatch_opensandbox() -> None:
    """Production imports must not mutate process-global third-party SDK behavior."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from opensandbox.adapters.converter.sandbox_model_converter "
                "import SandboxModelConverter;"
                "before = SandboxModelConverter.__dict__['to_paged_sandbox_infos'];"
                "import astrabox.core.service.orchestrator.runtime.sandbox_client;"
                "after = SandboxModelConverter.__dict__['to_paged_sandbox_infos'];"
                "assert after is before"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
