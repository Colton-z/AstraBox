"""Run Hermes' profile-local cron provider under the image's supervisor."""

from __future__ import annotations

# Native bootstrap selects the profile's installed dependency environment first.
import hermes_bootstrap  # noqa: F401

import signal
import threading
from types import FrameType

from cron.scheduler_provider import resolve_cron_scheduler
from hermes_logging import setup_logging


def main() -> None:
    setup_logging(mode="gateway")
    stop = threading.Event()

    def request_stop(_signal: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    provider = resolve_cron_scheduler()
    try:
        # The native provider owns its interval, claims, recovery and execution.
        provider.start(stop)
        # External providers can return after registering their triggers.
        stop.wait()
    finally:
        provider.stop()


if __name__ == "__main__":
    main()
