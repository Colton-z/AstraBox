"""Observability seam — metrics registry (prometheus_client facade) + JSON logs.

The metrics registry is a lazy counter/gauge facade over a private
``prometheus_client`` ``CollectorRegistry`` (see ``astrabox/observability/``).
These tests pin the public facade (increment / set_gauge / snapshot / render)
and the spec-correct exposition it now emits.
"""

from __future__ import annotations

import json
import logging
import math

import pytest

import astrabox.observability.metrics as metrics


@pytest.fixture(autouse=True)
def _clean_metrics():
    metrics.reset_for_tests()
    yield
    metrics.reset_for_tests()


def test_counter_accumulates_and_renders() -> None:
    metrics.increment("astrabox_turns_accepted_total")
    metrics.increment("astrabox_turns_accepted_total", value=2)
    text = metrics.render_prometheus()
    # prometheus_client renders the accumulated counter as a float and carries
    # the # TYPE (with the _total suffix retained) a scrape expects.
    assert "astrabox_turns_accepted_total 3.0" in text
    assert "# TYPE astrabox_turns_accepted_total counter" in text


def test_labels_render_and_key_separately() -> None:
    metrics.increment("m", labels={"kind": "turn"})
    metrics.increment("m", labels={"kind": "sandbox"})
    metrics.increment("m", labels={"kind": "turn"})
    text = metrics.render_prometheus()
    # A counter's value series carries the _total suffix under prometheus_client.
    assert 'm_total{kind="turn"} 2.0' in text
    assert 'm_total{kind="sandbox"} 1.0' in text


def test_gauge_set_and_render() -> None:
    metrics.set_gauge("astrabox_queue_depth", 5)
    metrics.set_gauge("astrabox_queue_depth", 2)  # overwrite
    text = metrics.render_prometheus()
    assert "astrabox_queue_depth 2.0" in text
    assert "# TYPE astrabox_queue_depth gauge" in text


def test_non_finite_values_use_openmetrics_tokens() -> None:
    # OpenMetrics requires the canonical `+Inf`, `-Inf`, and `NaN` tokens for
    # non-finite samples.
    metrics.set_gauge("astrabox_val_a", math.inf)
    metrics.set_gauge("astrabox_val_b", -math.inf)
    metrics.set_gauge("astrabox_val_c", math.nan)
    text = metrics.render_prometheus()
    assert "astrabox_val_a +Inf" in text
    assert "astrabox_val_b -Inf" in text
    assert "astrabox_val_c NaN" in text
    # Sample lines must not expose Python's lowercase spellings.
    sample_lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert not any(ln.endswith(("inf", "nan")) for ln in sample_lines)


def test_snapshot_read_seam() -> None:
    metrics.increment("c", value=4)
    metrics.set_gauge("g", 1)
    snap = metrics.snapshot()
    # Snapshot reports the accumulated value under the caller-facing name, with
    # the counter _created timestamp series omitted.
    assert {"name": "c", "labels": {}, "value": 4.0} in snap["counters"]
    assert {"name": "g", "labels": {}, "value": 1.0} in snap["gauges"]


def test_label_value_is_escaped() -> None:
    metrics.increment("m", labels={"path": 'a"b\\c'})
    text = metrics.render_prometheus()
    assert 'path="a\\"b\\\\c"' in text


def test_empty_registry_renders_empty() -> None:
    assert metrics.render_prometheus() == ""


def test_json_log_formatter_emits_one_json_object() -> None:
    from astrabox.common.logger.logger_factory import _JsonFormatter

    fmt = _JsonFormatter()
    record = logging.LogRecord(
        name="astrabox.default", level=logging.WARNING, pathname="x.py",
        lineno=42, msg="hello %s", args=("world",), exc_info=None,
    )
    line = fmt.format(record)
    obj = json.loads(line)
    assert obj["level"] == "WARNING"
    assert obj["logger"] == "astrabox.default"
    assert obj["msg"] == "hello world"
    assert obj["line"] == 42


def test_admission_accept_increments_turn_counter() -> None:
    import asyncio

    import astrabox.seams.admission as adm

    async def _go():
        await adm.enforce_admission(
            adm.AdmissionRequest(kind=adm.ADMISSION_KIND_TURN, user_id="u1")
        )

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_go())
    text = metrics.render_prometheus()
    assert "astrabox_turns_accepted_total 1.0" in text
