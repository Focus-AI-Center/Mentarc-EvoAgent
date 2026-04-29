"""Prometheus metrics for sandbox execution.

Single-process default registry. The server runs uvicorn single-worker, so
the in-process registry is correct. If we ever move to gunicorn with
multiple workers, set ``PROMETHEUS_MULTIPROC_DIR`` and switch to
``MultiProcessCollector`` — the metric definitions here stay the same, only
the exposition endpoint changes.

Histogram buckets cover the full sandbox latency range we expect:
container start (~1s) on the low end, generous user code budgets (~120s
``_MAX_TIMEOUT`` from ``execute_code``) on the high end. Default
prometheus_client buckets stop at 10s, which is too coarse for distinguishing
"warm container reuse" (~0.1s) from "cold container start" (~2s) once #1
lands.
"""

from prometheus_client import CONTENT_TYPE_LATEST, Gauge, Histogram, generate_latest

SANDBOX_EXEC_DURATION = Histogram(
    "sandbox_exec_duration_seconds",
    "Sandbox invocation wall-clock duration, in seconds.",
    labelnames=("backend", "language", "outcome"),
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0),
)

SANDBOX_ACTIVE = Gauge(
    "sandbox_active",
    "Currently running sandbox invocations.",
    labelnames=("backend",),
)


__all__ = [
    "CONTENT_TYPE_LATEST",
    "SANDBOX_ACTIVE",
    "SANDBOX_EXEC_DURATION",
    "generate_latest",
]
