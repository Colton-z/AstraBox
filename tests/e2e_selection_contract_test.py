"""Exact static contract for all maintained live-Python lanes."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from itertools import combinations
from pathlib import Path

import pytest



REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_ROOT = REPO_ROOT / "tests" / "e2e"
CONTRACT_PATH = REPO_ROOT / "tests/e2e-contract/suite-contract.json"
MATRIX_PATH = REPO_ROOT / "tests/e2e-contract/agent-engine-matrix.json"
FORBIDDEN_OUTCOMES = frozenset({"importorskip", "skip", "skipif", "xfail"})
SELECTORS = {
    "main_parallel": "e2e and not backend_restart and not assistant_live",
    "main_restart": "e2e and backend_restart and not assistant_live",
    "assistant": "e2e and assistant_live and not backend_restart",
}


def _contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))["python"]


def _collected(marker: str) -> list[str]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-m",
            marker,
            "tests/e2e",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    node_ids = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.startswith("tests/e2e/") and "::" in line
    ]
    matches = re.findall(r"(?m)^(\d+)(?:/\d+)? tests collected", result.stdout)
    assert matches == [str(len(node_ids))], result.stdout
    return node_ids


def test_every_live_contract_has_one_exact_mutually_exclusive_lane() -> None:
    contract = _contract()
    lanes = {
        "main_parallel": contract["main"]["parallel_node_ids"],
        "main_restart": contract["main"]["restart_node_ids"],
        "assistant": contract["assistant"]["parallel_node_ids"],
    }

    assert contract["main"]["restart_node_ids"]
    assert contract["assistant"]["restart_node_ids"] == []
    assert contract["main"]["tests"] == (
        len(lanes["main_parallel"]) + len(lanes["main_restart"])
    )
    matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    engine_kinds = [profile["engine_kind"] for profile in matrix["profiles"]]
    assert contract["main"]["engine_kinds"] == engine_kinds
    assert contract["main"]["matrix_tests"] == (
        len(engine_kinds) * contract["main"]["tests"]
    )
    assert contract["assistant"]["tests"] == len(lanes["assistant"])
    assert contract["tests"] == contract["main"]["tests"] + contract["assistant"]["tests"]
    assert contract["matrix_tests"] == (
        contract["main"]["matrix_tests"] + contract["assistant"]["tests"]
    )
    for left, right in combinations(lanes, 2):
        assert set(lanes[left]).isdisjoint(lanes[right]), f"{left} overlaps {right}"
    for lane, selector in SELECTORS.items():
        assert _collected(selector) == lanes[lane]

    expected = set().union(*(set(node_ids) for node_ids in lanes.values()))
    all_live = _collected("e2e")
    assert len(all_live) == contract["tests"]
    assert len(expected) == contract["tests"]
    assert set(all_live) == expected


def test_live_contracts_cannot_encode_skip_or_expected_failure() -> None:
    offenders: list[str] = []
    for path in sorted(LIVE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            forbidden = None
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_OUTCOMES:
                forbidden = node.attr
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in FORBIDDEN_OUTCOMES
            ):
                forbidden = node.func.id
            elif isinstance(node, ast.ImportFrom) and node.module == "pytest":
                imported = {alias.name for alias in node.names} & FORBIDDEN_OUTCOMES
                forbidden = ",".join(sorted(imported)) or None
            if forbidden:
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno}:{forbidden}"
                )
    assert offenders == [], (
        "live contracts must fail loud; skip/xfail/importorskip is forbidden: "
        f"{offenders}"
    )
