from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from astrabox.api.routes.http_common import map_storage_api_error
from astrabox.persistence.repository import backend


def test_a_postgresql_outage_is_the_retryable_record_store_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # PostgreSQL is the default backend. Its connection loss reached the
    # generic 500 handler because only pymongo's exceptions were classified.
    monkeypatch.setattr(backend, "active_backend_name", lambda: "postgresql")
    lost = OperationalError("SELECT 1", {}, ConnectionRefusedError("connection refused"))

    mapped = map_storage_api_error(lost)

    assert mapped is not None
    assert (mapped.code, mapped.status_code, mapped.retryable, mapped.owner) == (
        "PERSISTENCE_UNAVAILABLE", 503, True, "store",
    )
    assert "mongodb" not in mapped.message
