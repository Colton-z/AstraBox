"""Restore the profile and enter Hermes' CLI in one interpreter."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import runpy
import sys
import time
from typing import Iterator

from hermes_state_bootstrap import restore_once


@contextmanager
def stage(name: str) -> Iterator[None]:
    started = time.monotonic()
    try:
        yield
    except BaseException:
        print(f"ASTRABOX_HERMES_PREPARE_FAILED stage={name} seconds={time.monotonic() - started:.3f}", flush=True)
        raise
    print(f"ASTRABOX_HERMES_PREPARE_COMPLETE stage={name} seconds={time.monotonic() - started:.3f}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-env", type=Path, required=True)
    args = parser.parse_args()
    with stage("restore"):
        restore_once(args.profile_env)
    with stage("configuration"):
        result = runpy.run_path("/usr/local/bin/astrabox-hermes-config-merge")["main"]()
        if result:
            raise SystemExit(result)
    # Native origin records in the restored profile protect edited bundled Skills.
    with stage("bundled_skills"):
        from tools.skills_sync import sync_skills

        sync_skills(quiet=True)
    # This still runs after the merge and loads Hermes' current platform catalog.
    # A tool's bot credential must not activate a messaging adapter.
    with stage("messaging_platforms"):
        from hermes_gateway_platforms import main as disable_messaging_platforms

        result = disable_messaging_platforms()
        if result:
            raise SystemExit(result)
    # The native CLI is the same public entry point as the ``hermes`` command.
    # Keep its imports in this process, including the profile loader's cache
    # invalidation, rather than importing Hermes again in a second interpreter.
    # The bridge alone holds its outward-port credential; tools must not inherit it.
    os.environ.pop("HERMES_DASHBOARD_SESSION_TOKEN", None)
    sys.argv = ["hermes", "gateway", "run", "--no-supervise", "--external-supervisor"]
    with stage("gateway_cli_import"):
        from hermes_cli.main import main as gateway_main

    gateway_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
