"""In-process metric registry — a lazy facade over ``prometheus_client``.

Counters and gauges are created on first use (their label *names* fixed by that
first call, as Prometheus requires) against a private ``CollectorRegistry``, and
:func:`render_prometheus` exposes them via ``generate_latest`` in the standard
text-exposition format. ``prometheus_client`` stays behind this seam so callers
keep the same tiny :func:`increment` / :func:`set_gauge` API while the wire form
is spec-correct — non-finite values render as ``+Inf`` / ``-Inf`` / ``NaN`` (not
Python's ``inf`` / ``nan``) and counters carry the ``_total`` / ``_created``
series a scrape expects. ``snapshot()`` is the read seam for a deployment's own
exporter. Thread-safe via a module lock (metrics are written from many
task/threads); the underlying collectors are individually thread-safe too.
"""

from __future__ import annotations

import threading
from typing import Iterable, Mapping

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest

_lock = threading.Lock()
_registry = CollectorRegistry()
#: caller-facing name -> collector; the lazy facade over ``_registry``.
_counters: dict[str, Counter] = {}
_gauges: dict[str, Gauge] = {}
#: HELP text for a metric, baked into its collector when first created.
_HELP: dict[str, str] = {}


def register_help(name: str, help_text: str) -> None:
    """Register # HELP text for ``name`` (applied when its collector is created)."""
    with _lock:
        _HELP[str(name)] = str(help_text)


def _labelnames(labels: Mapping[str, str] | None) -> tuple[str, ...]:
    return tuple(sorted(str(k) for k in (labels or {})))


def _label_kwargs(labels: Mapping[str, str]) -> dict[str, str]:
    return {str(k): str(v) for k, v in labels.items()}


def increment(name: str, *, labels: Mapping[str, str] | None = None, value: float = 1.0) -> None:
    """Add ``value`` (default 1) to a counter."""
    name = str(name)
    with _lock:
        counter = _counters.get(name)
        if counter is None:
            counter = Counter(name, _HELP.get(name, ""), _labelnames(labels), registry=_registry)
            _counters[name] = counter
        child = counter.labels(**_label_kwargs(labels)) if labels else counter
        child.inc(float(value))


def set_gauge(name: str, value: float, *, labels: Mapping[str, str] | None = None) -> None:
    """Set a gauge to ``value``."""
    name = str(name)
    with _lock:
        gauge = _gauges.get(name)
        if gauge is None:
            gauge = Gauge(name, _HELP.get(name, ""), _labelnames(labels), registry=_registry)
            _gauges[name] = gauge
        child = gauge.labels(**_label_kwargs(labels)) if labels else gauge
        child.set(float(value))


def snapshot() -> dict[str, list[dict[str, object]]]:
    """A read-seam copy of the current registry (for a custom exporter/test).

    Reports the accumulated value per ``(name, labels)`` under the caller-facing
    name; a counter's ``_created`` timestamp series is exposition wire form, not
    state, so it is omitted here.
    """
    with _lock:
        counter_items = list(_counters.items())
        gauge_items = list(_gauges.items())
    counters: list[dict[str, object]] = []
    for name, collector in counter_items:
        for family in collector.collect():
            for sample in family.samples:
                if sample.name.endswith("_created"):
                    continue
                counters.append({"name": name, "labels": dict(sample.labels), "value": sample.value})
    gauges: list[dict[str, object]] = []
    for name, collector in gauge_items:
        for family in collector.collect():
            for sample in family.samples:
                gauges.append({"name": name, "labels": dict(sample.labels), "value": sample.value})
    return {"counters": counters, "gauges": gauges}


def reset_for_tests() -> None:  # pragma: no cover - test hygiene
    global _registry
    with _lock:
        _registry = CollectorRegistry()
        _counters.clear()
        _gauges.clear()


def render_prometheus() -> str:
    """Render the registry in Prometheus text-exposition format."""
    with _lock:
        return generate_latest(_registry).decode("utf-8")


# ── base signals the platform records (help text) ────────────────────────────
METRIC_TURNS_ACCEPTED = "astrabox_turns_accepted_total"
METRIC_TURNS_DENIED = "astrabox_turns_admission_denied_total"
METRIC_SANDBOXES_CREATED = "astrabox_sandboxes_created_total"

for _name, _help in (
    (METRIC_TURNS_ACCEPTED, "StartTurn commands accepted (post-admission)."),
    (METRIC_TURNS_DENIED, "Turn/sandbox requests denied by the admission controller."),
    (METRIC_SANDBOXES_CREATED, "User-facing sessions (and their sandboxes) created."),
):
    register_help(_name, _help)


def base_metric_names() -> Iterable[str]:
    return (METRIC_TURNS_ACCEPTED, METRIC_TURNS_DENIED, METRIC_SANDBOXES_CREATED)
