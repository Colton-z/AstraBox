"""Correlate startup phase durations across the host and the sandbox Runner."""

from collections.abc import Iterator
from contextlib import contextmanager
from time import monotonic, time

from astrabox.common.logger.logger_factory import get_logger

logger = get_logger(__name__)


@contextmanager
def startup_stage(session_id: str, stage: str, *, started_at: float | None = None) -> Iterator[None]:
    """Log only the Session, phase and outcome, including on failure."""
    started = monotonic() if started_at is None else started_at
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "failed"
        raise
    finally:
        logger.info(
            "conversation_startup session_id=%s stage=%s outcome=%s duration_ms=%.3f at_unix_ms=%d",
            session_id, stage, outcome, (monotonic() - started) * 1000, int(time() * 1000),
        )
