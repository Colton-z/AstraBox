from __future__ import annotations

from collections.abc import Awaitable, Callable
from logging import Logger
from typing import TypeVar

from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception as tenacity_retry_if_exception,
    stop_after_attempt,
    stop_after_delay,
    stop_any,
    wait_exponential,
    wait_fixed,
)

_T = TypeVar("_T")


def _build_stop_condition(
    *,
    max_attempts: int | None,
    max_delay_seconds: float | None,
):
    stop_conditions = []
    if max_attempts is not None:
        stop_conditions.append(stop_after_attempt(max_attempts))
    if max_delay_seconds is not None:
        stop_conditions.append(stop_after_delay(max_delay_seconds))
    if not stop_conditions:
        raise ValueError("retry helper requires max_attempts or max_delay_seconds")
    if len(stop_conditions) == 1:
        return stop_conditions[0]
    return stop_any(*stop_conditions)


def build_retry_warning_before_sleep(
    logger: Logger,
    message_builder: Callable[[RetryCallState, Exception], str],
) -> Callable[[RetryCallState], None]:
    def _before_sleep(retry_state: RetryCallState) -> None:
        outcome = retry_state.outcome
        exc = outcome.exception() if outcome is not None and outcome.failed else None
        if not isinstance(exc, Exception):
            return
        logger.warning(message_builder(retry_state, exc))

    return _before_sleep


async def retry_async_call(
    op: Callable[[], Awaitable[_T]],
    *,
    should_retry_exception: Callable[[Exception], bool],
    max_attempts: int | None = None,
    max_delay_seconds: float | None = None,
    wait_seconds: float = 0.5,
    wait_exponential_max: float | None = None,
    before_sleep: Callable[[RetryCallState], None] | None = None,
    on_before_retry: Callable[[], Awaitable[None]] | None = None,
) -> _T:
    """Run ``op`` under tenacity's ``AsyncRetrying`` — the one retry engine.

    ``wait_seconds`` is a fixed inter-attempt wait; pass ``wait_exponential_max``
    to switch to exponential backoff (``wait_seconds`` becomes the multiplier,
    capped at ``wait_exponential_max``). ``on_before_retry`` is an async hook run
    at the start of every retry attempt (attempt 2+), for callers that must do
    async work between attempts — e.g. invalidating a cached client — which
    tenacity's synchronous ``before_sleep`` cannot.
    """
    wait = (
        wait_exponential(multiplier=wait_seconds, max=wait_exponential_max)
        if wait_exponential_max is not None
        else wait_fixed(wait_seconds)
    )
    async for attempt in AsyncRetrying(
        retry=tenacity_retry_if_exception(should_retry_exception),
        stop=_build_stop_condition(
            max_attempts=max_attempts,
            max_delay_seconds=max_delay_seconds,
        ),
        wait=wait,
        before_sleep=before_sleep,
        reraise=True,
    ):
        with attempt:
            if on_before_retry is not None and attempt.retry_state.attempt_number > 1:
                await on_before_retry()
            return await op()

    raise RuntimeError("retry_async_call exhausted without returning")
