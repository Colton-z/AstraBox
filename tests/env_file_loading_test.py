"""``.env`` reaches typed settings and direct ``os.getenv`` readers.

Pydantic reads ``env_file`` into typed fields, while direct readers require the
values in the process environment. ``load_env_file_into_process_env`` gives both
paths one precedence rule: real environment > .env > defaults.
"""

from __future__ import annotations

import os

import pytest

from astrabox.config.settings import load_env_file_into_process_env


@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setenv("ASTRABOX_ENV_FILE", str(path))
    return path


def test_env_file_fills_process_env_for_getenv_readers(env_file, monkeypatch) -> None:
    monkeypatch.delenv("ASTRABOX_TEST_ONLY_IN_DOTENV", raising=False)
    env_file.write_text(
        'ASTRABOX_TEST_ONLY_IN_DOTENV="from-dotenv"\n'
        "# comments and blank lines are dotenv-legal\n\n"
    )
    load_env_file_into_process_env()
    assert os.getenv("ASTRABOX_TEST_ONLY_IN_DOTENV") == "from-dotenv"
    monkeypatch.delenv("ASTRABOX_TEST_ONLY_IN_DOTENV", raising=False)


def test_real_environment_always_wins_over_env_file(env_file, monkeypatch) -> None:
    env_file.write_text("ASTRABOX_TEST_PRECEDENCE=from-dotenv\n")
    monkeypatch.setenv("ASTRABOX_TEST_PRECEDENCE", "from-real-env")
    load_env_file_into_process_env()
    assert os.getenv("ASTRABOX_TEST_PRECEDENCE") == "from-real-env"


def test_missing_env_file_is_a_noop(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ASTRABOX_ENV_FILE", str(tmp_path / "absent.env"))
    load_env_file_into_process_env()  # must not raise
