"""String pushdown keeps scalar identity reads on their SQL indexes."""

from __future__ import annotations

from sqlalchemy.dialects import postgresql

from astrabox.persistence.repository.sqlite.collection import AsyncCollection


def _postgresql_candidate_sql(key: str) -> str:
    collection = object.__new__(AsyncCollection)
    collection._is_postgresql = True
    expression = collection._string_equality_candidate(key, "value")
    return str(
        expression.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_scalar_identity_pushdown_has_no_array_scan_arm() -> None:
    for key in ("session_id", "platform_session_id", "event_id", "id"):
        sql = _postgresql_candidate_sql(key)
        assert "jsonb_typeof" not in sql, (
            f"{key} must remain a pure indexed equality, got {sql}"
        )


def test_plural_reference_pushdown_retains_array_membership_candidates() -> None:
    sql = _postgresql_candidate_sql("credential_vault_ids")
    assert "jsonb_typeof" in sql, sql
    assert "array" in sql, sql
