from __future__ import annotations

from abc import ABC, abstractmethod
from astrabox.core.service.orchestrator.session_kernel.workers.models import (
    WorkerOutcome,
    WorkerWakeup,
)

class KernelWorkerBase(ABC):
    """Base helper for a per-session-channel worker.

    Concurrency control is the turn-id fence: snapshot writes compare
    ``current_turn_id``. There is no second worker-lease authority.
    """

    channel: str

    def __init__(
        self,
        *,
        worker_id: str,
    ) -> None:
        self._worker_id = worker_id

    async def run(self, wakeup: WorkerWakeup) -> WorkerOutcome:
        return await self.run_once(wakeup)

    @abstractmethod
    async def run_once(
        self,
        wakeup: WorkerWakeup,
    ) -> WorkerOutcome:
        """Process one wakeup."""
