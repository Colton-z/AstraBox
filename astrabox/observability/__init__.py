"""Observability seam — a MINIMAL in-process metrics registry.

This is the injection point for metrics, NOT a metrics platform. The community
base ships a tiny lazy counter/gauge facade over a private ``prometheus_client``
``CollectorRegistry`` and a ``/metrics`` endpoint that renders it in Prometheus
text-exposition format — enough for a scrape to pick up a handful of core
signals. A deployment that wants dashboards, histograms, or a push gateway wires
its own exporter (the registry's ``snapshot()`` is the read seam).

The base instruments a few load-bearing signals (turns accepted/denied,
sandboxes created); everything else is the deployment's to add via
:func:`increment` / :func:`set_gauge`.
"""

from astrabox.observability.metrics import (
    increment,
    render_prometheus,
    reset_for_tests,
    set_gauge,
    snapshot,
)

__all__ = [
    "increment",
    "set_gauge",
    "snapshot",
    "render_prometheus",
    "reset_for_tests",
]
