"""Application logging has one sink and keeps the emitting module's identity."""

from __future__ import annotations

import ast
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from astrabox.common.logger.logger_factory import MODULE_APP_NAME, get_logger

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_ROOT = _REPO_ROOT / "astrabox"


def test_module_loggers_keep_distinct_names_without_duplicate_sinks() -> None:
    application_logger = logging.getLogger(MODULE_APP_NAME)
    handlers_before = tuple(application_logger.handlers)

    first = get_logger("astrabox.tests.first_module")
    second = get_logger("astrabox.tests.second_module")

    assert first is not second
    assert first.name == "astrabox.tests.first_module"
    assert second.name == "astrabox.tests.second_module"
    assert get_logger(first.name) is first
    assert get_logger(MODULE_APP_NAME) is application_logger
    assert not first.handlers and not second.handlers
    assert first.propagate and second.propagate
    assert not application_logger.propagate
    assert tuple(application_logger.handlers) == handlers_before


def test_dunder_name_loggers_do_not_bypass_the_logging_factory() -> None:
    offenders: list[str] = []
    for path in _PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "logging"
                and node.func.attr == "getLogger"
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "__name__"
            ):
                offenders.append(f"{path.relative_to(_REPO_ROOT)}:{node.lineno}")

    assert not offenders, (
        "module logs bypass the configured AstraBox sink: " + ", ".join(offenders)
    )


def test_json_format_scope_matches_the_operator_docs() -> None:
    program = """
import logging

logging.basicConfig(level=logging.INFO, format="ROOT %(message)s")

from astrabox.common.logger.logger_factory import get_logger

get_logger("astrabox.tests.json_path").warning("application record")
logging.getLogger("third_party.component").warning("third-party record")
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=_REPO_ROOT,
        env={**os.environ, "ASTRABOX_LOG_FORMAT": "json"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    structured = []
    for line in result.stderr.splitlines():
        try:
            structured.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    assert any(
        record.get("logger") == "astrabox.tests.json_path"
        and record.get("msg") == "application record"
        for record in structured
    )
    assert "ROOT third-party record" in result.stderr.splitlines()

    configuration_doc = " ".join(
        (_REPO_ROOT / "docs/configuration.md").read_text().split()
    )
    assert (
        "`ASTRABOX_LOG_FORMAT`" in configuration_doc
        and "AstraBox application logger output format" in configuration_doc
        and "Root, Uvicorn, and third-party loggers retain their own format"
        in configuration_doc
    )
