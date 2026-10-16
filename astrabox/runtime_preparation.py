"""Host APIs for authenticated runtime-preparation integrations.

Call from a route inside AstraBox's identity middleware, or bind an identity
verified by the embedding application's identity resolver. Request payloads are
not an identity source. Provider tables remain owned by the integration.
"""

from astrabox.core.service.orchestrator.runtime.preparation_commit import (
    issue_preparation_commit_guard,
)

__all__ = ["issue_preparation_commit_guard"]
