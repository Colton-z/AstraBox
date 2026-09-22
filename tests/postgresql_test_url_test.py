from __future__ import annotations

from pathlib import Path

import pytest

from tests._postgresql_support import resolve_postgresql_test_url


def test_explicit_postgresql_test_url_wins_without_local_secrets(tmp_path: Path) -> None:
    explicit = "postgresql+asyncpg://operator:secret@db.test/astrabox"

    assert resolve_postgresql_test_url(
        {"ASTRABOX_TEST_POSTGRES_URL": explicit}, repo_root=tmp_path
    ) == explicit


def test_default_postgresql_test_url_uses_compose_generated_password(
    tmp_path: Path,
) -> None:
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    (secret_dir / "astrabox_password").write_text("random/secret:@", encoding="utf-8")

    assert resolve_postgresql_test_url(
        {
            "ASTRABOX_LOCAL_DATABASE_SECRET_DIR": str(secret_dir),
            "ASTRABOX_POSTGRES_PORT": "61234",
        },
        repo_root=tmp_path,
    ) == (
        "postgresql+asyncpg://astrabox:random%2Fsecret%3A%40@"
        "127.0.0.1:61234/astrabox"
    )


def test_default_postgresql_test_url_fails_loud_without_compose_secrets(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="scripts/compose.sh up -d postgres"):
        resolve_postgresql_test_url({}, repo_root=tmp_path)
