"""Sandbox request-path modules use the application's configured logger.

The application does not configure the root logger, so these modules must use
``astrabox.common.logger.logger_factory`` for lifecycle and pool diagnostics to
reach the active handlers. The test checks logger wiring directly because an
unhandled logger accepts calls without exposing the missing output.
"""

from __future__ import annotations

import importlib

import pytest

from astrabox.common.logger.logger_factory import get_logger

# The modules that serve a Session: the provider, its create/transport helper,
# and the official SDK client-pool adapter. When one cannot explain what it did,
# a live deployment cannot be diagnosed at all.
_SANDBOX_PATH_MODULES = (
    "astrabox.providers.open_sandbox.sandbox",
    "astrabox.providers.open_sandbox.executor",
    "astrabox.providers.open_sandbox.agent_pool",
)


@pytest.mark.parametrize("module_name", _SANDBOX_PATH_MODULES)
def test_the_module_logs_through_the_application_factory(module_name: str) -> None:
    """A module here must share the logger the application actually configures.

    Asserting on the logger OBJECT rather than on the import line is what makes
    this survive a refactor: any route back to a bare ``logging.getLogger`` —
    including an aliased import — produces a different logger and fails here.
    """
    module = importlib.import_module(module_name)
    logger = getattr(module, "logger", None)
    assert logger is not None, f"{module_name} has no module-level `logger`"
    assert logger is get_logger(module_name), (
        f"{module_name}.logger is not the application's logger, so everything it "
        "emits is discarded. Use `get_logger(__name__)` from "
        "astrabox.common.logger.logger_factory."
    )


@pytest.mark.parametrize("module_name", _SANDBOX_PATH_MODULES)
def test_the_logger_has_somewhere_to_write(module_name: str) -> None:
    """The property that actually failed: a handler at the end of the chain.

    The assertion above pins the convention; this one pins the CONSEQUENCE, so
    the guard still means something if the factory itself ever stops attaching
    handlers. ``hasHandlers`` walks ancestors, which is the same walk a real
    emit does.
    """
    module = importlib.import_module(module_name)
    assert module.logger.hasHandlers(), (
        f"{module_name}.logger has no handler anywhere in its chain — its output "
        "is discarded silently"
    )
