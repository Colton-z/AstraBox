"""The e2e_smoke.sh stream judge proves delivery without judging model prose.

A successful probe has complete assistant text blocks, genuine text deltas,
a normal finish frame, and no engine error. This runs the actual Python judge
embedded in the script against synthetic SSE. It pins the prior false-green
edges (model errors containing the digit ``2``) and the duplicate-response edge
without requiring a vendor model to reproduce one exact sentence.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "e2e_smoke.sh"


def _extract_judge_source() -> str:
    """Pull the `<<'PY' ... PY` heredoc (the stream judge) out of the script."""
    lines = _SCRIPT.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.rstrip().endswith("<<'PY'"))
    body: list[str] = []
    for ln in lines[start + 1:]:
        if ln.strip() == "PY":
            return "\n".join(body) + "\n"
        body.append(ln)
    raise AssertionError("PY heredoc terminator not found in e2e_smoke.sh")


def _judge(tmp_path: Path, sse: str) -> dict[str, str]:
    src = _extract_judge_source()
    sse_file = tmp_path / "ai-stream.sse"
    sse_file.write_text(sse)
    proc = subprocess.run(
        [sys.executable, "-", str(sse_file)],
        input=src,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


def _extract_shell_function(name: str) -> str:
    lines = _SCRIPT.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line == f"{name}() {{")
    for end in range(start + 1, len(lines)):
        if lines[end] == "}":
            return "\n".join(lines[start : end + 1]) + "\n"
    raise AssertionError(f"shell function {name!r} has no closing brace")


def test_live_402_never_greens(tmp_path: Path) -> None:
    # A 402 trace carries an "API Error: ..." text block and repeats the result
    # without an is_error field. The verdict must use the error signature rather
    # than treating the digit "2" as a successful terminal code.
    sse = (
        'data: {"type":"start","messageId":"m0"}\n'
        'data: {"type":"start-step"}\n'
        'data: {"type":"text-start","id":"blk-0-0"}\n'
        'data: {"type":"text-delta","id":"blk-0-0","delta":"API Error: 402 Insufficient Balance"}\n'
        'data: {"type":"text-end","id":"blk-0-0"}\n'
        'data: {"type":"finish-step"}\n'
        'data: {"type":"data-result","data":{"result":"API Error: 402 Insufficient Balance","duration_ms":2449,"total_cost_usd":0,"num_turns":1,"usage":{"input_tokens":0,"output_tokens":0},"stop_reason":"stop_sequence"}}\n'
        'data: {"type":"finish","finishReason":"stop"}\n'
        "data: [DONE]\n"
    )
    result = _judge(tmp_path, sse)
    assert result["ERROR"] != ""           # the turn is flagged FAILED
    assert result["N_TEXT_DELTA"] == "0"    # the error text is NOT genuine assistant text
    assert result["TEXT_BLOCK_COUNT"] == "1"
    assert result["FINISH_REASON"] == "stop"
    assert "API Error" in result["ERROR"]


def test_is_error_result_still_flagged(tmp_path: Path) -> None:
    # Defensive: a backend that DOES set is_error on the result must also fail,
    # even if the result text is not the CLI "API Error" signature.
    sse = (
        'data: {"type":"text-delta","id":"b0","delta":"boom 2"}\n'
        'data: {"type":"data-result","data":{"is_error":true,"subtype":"error_during_execution","result":"engine crashed"}}\n'
        "data: [DONE]\n"
    )
    result = _judge(tmp_path, sse)
    assert result["ERROR"] != ""


def test_api_error_split_across_deltas_flagged(tmp_path: Path) -> None:
    # An "API Error" split over multiple text-deltas: per-delta misses, but the
    # assembled-text backstop still flags it.
    sse = (
        'data: {"type":"text-start","id":"b0"}\n'
        'data: {"type":"text-delta","id":"b0","delta":"API Er"}\n'
        'data: {"type":"text-delta","id":"b0","delta":"ror: 402 nope"}\n'
        'data: {"type":"text-end","id":"b0"}\n'
        'data: {"type":"data-result","data":{"result":"","stop_reason":"stop_sequence"}}\n'
        'data: {"type":"finish","finishReason":"stop"}\n'
        "data: [DONE]\n"
    )
    result = _judge(tmp_path, sse)
    assert result["ERROR"] != ""


def test_plain_error_frame_still_flagged(tmp_path: Path) -> None:
    sse = 'data: {"type":"error","errorText":"boom 402"}\ndata: [DONE]\n'
    result = _judge(tmp_path, sse)
    assert result["ERROR"] != ""


def test_genuine_streamed_answer_greens(tmp_path: Path) -> None:
    sse = (
        'data: {"type":"text-start","id":"b0"}\n'
        'data: {"type":"text-delta","id":"b0","delta":"2"}\n'
        'data: {"type":"text-end","id":"b0"}\n'
        'data: {"type":"data-result","data":{"is_error":false,"subtype":"success","result":"2"}}\n'
        'data: {"type":"finish","finishReason":"stop"}\n'
        "data: [DONE]\n"
    )
    result = _judge(tmp_path, sse)
    assert result["N_TEXT_DELTA"] == "1"
    assert result["TEXT_BLOCK_COUNT"] == "1"
    assert result["FINISH_REASON"] == "stop"
    assert result["PROTOCOL_ERROR"] == ""
    assert result["ERROR"] == ""


def test_verbose_model_wording_does_not_decide_the_probe(tmp_path: Path) -> None:
    sse = (
        'data: {"type":"text-start","id":"b0"}\n'
        'data: {"type":"text-delta","id":"b0","delta":"1 + 1 = 2, so the number is 2."}\n'
        'data: {"type":"text-end","id":"b0"}\n'
        'data: {"type":"data-result","data":{"is_error":false,"subtype":"success"}}\n'
        'data: {"type":"finish","finishReason":"stop"}\n'
        "data: [DONE]\n"
    )
    result = _judge(tmp_path, sse)
    assert result["N_TEXT_DELTA"] == "1"
    assert result["TEXT_BLOCK_COUNT"] == "1"
    assert result["FINISH_REASON"] == "stop"
    assert result["PROTOCOL_ERROR"] == ""
    assert result["ERROR"] == ""


def test_distinct_blocks_are_the_vendors_message_shape_not_a_double_body(
    tmp_path: Path,
) -> None:
    """An engine may split an answer across message items ('1' then '2').

    Each item is faithfully one complete block; a second item is not a replay.
    """

    sse = (
        'data: {"type":"text-start","id":"b0"}\n'
        'data: {"type":"text-delta","id":"b0","delta":"first response"}\n'
        'data: {"type":"text-end","id":"b0"}\n'
        'data: {"type":"text-start","id":"b1"}\n'
        'data: {"type":"text-delta","id":"b1","delta":"second response"}\n'
        'data: {"type":"text-end","id":"b1"}\n'
        'data: {"type":"data-result","data":{"is_error":false,"subtype":"success"}}\n'
        'data: {"type":"finish","finishReason":"stop"}\n'
        "data: [DONE]\n"
    )
    result = _judge(tmp_path, sse)
    assert result["N_TEXT_DELTA"] == "2"
    assert result["TEXT_BLOCK_COUNT"] == "2"
    assert result["FINISH_REASON"] == "stop"
    assert result["PROTOCOL_ERROR"] == ""
    assert result["ERROR"] == ""


@pytest.mark.parametrize("second_id", ["b0", "b1"])
def test_replayed_block_identity_is_rejected_not_repeated_model_wording(
    tmp_path: Path, second_id: str,
) -> None:
    """Different blocks may say the same thing; a completed ID cannot restart."""

    sse = (
        'data: {"type":"text-start","id":"b0"}\n'
        'data: {"type":"text-delta","id":"b0","delta":"the answer"}\n'
        'data: {"type":"text-end","id":"b0"}\n'
        f'data: {{"type":"text-start","id":"{second_id}"}}\n'
        f'data: {{"type":"text-delta","id":"{second_id}","delta":"the answer"}}\n'
        f'data: {{"type":"text-end","id":"{second_id}"}}\n'
        'data: {"type":"data-result","data":{"is_error":false,"subtype":"success"}}\n'
        'data: {"type":"finish","finishReason":"stop"}\n'
        "data: [DONE]\n"
    )
    result = _judge(tmp_path, sse)
    assert result["N_TEXT_DELTA"] == "2"
    assert result["FINISH_REASON"] == "stop"
    assert result["ERROR"] == ""
    if second_id == "b0":
        assert result["TEXT_BLOCK_COUNT"] == "1"
        assert result["PROTOCOL_ERROR"] == "duplicate text-start id 'b0'"
    else:
        assert result["TEXT_BLOCK_COUNT"] == "2"
        assert result["PROTOCOL_ERROR"] == ""


def test_incomplete_text_block_is_a_protocol_failure(tmp_path: Path) -> None:
    sse = (
        'data: {"type":"text-start","id":"b0"}\n'
        'data: {"type":"text-delta","id":"b0","delta":"partial"}\n'
        'data: {"type":"finish","finishReason":"stop"}\n'
        "data: [DONE]\n"
    )
    result = _judge(tmp_path, sse)
    assert result["TEXT_BLOCK_COUNT"] == "0"
    assert result["PROTOCOL_ERROR"] == "text block did not end"


def test_auth_error_with_a_key_and_hash_is_failed_and_redacted(tmp_path: Path) -> None:
    fake_key = "sk-test-redaction-abcdef1234567890"
    key_hash = "a" * 47 + "2" + "b" * 16
    error = (
        "Authentication Error: Invalid proxy server token. "
        f"Received API Key = {fake_key}; key hash {key_hash}"
    )
    sse = (
        'data: {"type":"text-start","id":"b0"}\n'
        + "data: "
        + json.dumps({"type": "text-delta", "id": "b0", "delta": error})
        + "\n"
        + 'data: {"type":"text-end","id":"b0"}\n'
        + "data: "
        + json.dumps({"type": "data-result", "data": {"result": error}})
        + '\ndata: {"type":"finish","finishReason":"stop"}\n'
        + "data: [DONE]\n"
    )
    result = _judge(tmp_path, sse)
    assert result["ERROR"] != ""
    assert result["N_TEXT_DELTA"] == "0"
    rendered = "\n".join(result.values())
    assert fake_key not in rendered
    assert key_hash not in rendered
    assert "[redacted]" in rendered


def test_evidence_redactor_removes_inherited_and_structural_secrets() -> None:
    inherited_secret = "provider-credential-value-12345"
    rejected_key = "sk-test-redaction-abcdef1234567890"
    key_hash = "a" * 47 + "2" + "b" * 16
    database_url = "postgresql://astrabox:database-password-123@postgres:5432/astrabox"
    raw = "\n".join(
        (
            f"opaque={inherited_secret}",
            f"Received API Key = {rejected_key}",
            f"hash={key_hash}",
            f"database={database_url}",
        )
    )
    proc = subprocess.run(
        ["bash", "-c", _extract_shell_function("redact_stream") + "redact_stream"],
        input=raw,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "VENV_PY": sys.executable,
            "ACME_ACCESS_TOKEN": inherited_secret,
        },
    )
    assert proc.returncode == 0, proc.stderr
    for secret in (inherited_secret, rejected_key, key_hash, "database-password-123"):
        assert secret not in proc.stdout
    assert proc.stdout.count("[redacted]") >= 4


def test_empty_reply_has_zero_deltas(tmp_path: Path) -> None:
    sse = (
        'data: {"type":"data-result","data":{"is_error":false,"subtype":"success"}}\n'
        'data: {"type":"finish","finishReason":"stop"}\n'
        "data: [DONE]\n"
    )
    result = _judge(tmp_path, sse)
    assert result["N_TEXT_DELTA"] == "0"
    assert result["TEXT_BLOCK_COUNT"] == "0"
    assert result["FINISH_REASON"] == "stop"


@pytest.mark.parametrize("_", range(1))
def test_script_stays_syntactically_valid_bash(_: int) -> None:
    # The script's targets are Linux deploy hosts (bash >= 4). macOS ships
    # bash 3.2, whose parser chokes on constructs bash 4+ accepts — a 3.2
    # failure says nothing about the script, so skip loudly rather than
    # fake-fail on a dev Mac.
    version = subprocess.run(
        ["bash", "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True
    ).stdout.strip()
    if version.isdigit() and int(version) < 4:
        pytest.skip(f"ambient bash {version}.x cannot parse bash>=4 scripts")
    proc = subprocess.run(["bash", "-n", str(_SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
