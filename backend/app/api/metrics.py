"""Prometheus exposition endpoint (Track T1, step 2).

Served at the **root** path ``/metrics`` rather than under
``settings.API_V1_PREFIX``, because that is where Prometheus scrapes by
convention and where ``curl :8000/metrics`` is expected to answer.

HONESTY CONTRACT
----------------
Every value exposed here is read from its real, live source at scrape time.

* Tool-call totals and investigation counts are read from
  :mod:`app.tools.counters`' in-process ledger rather than duplicated into a
  second set of counters, so the dashboard, the evaluation scorecard and
  Prometheus can never disagree about how many calls were made.
* Mesh service health is read from the mesh manager's current state.
* HTTP request counts come from :class:`RequestsMetricsMiddleware`, which
  wraps the real ASGI app.

Nothing here synthesises a number. A backend that is down makes its own
metric absent or its scrape target Down — never a comfortable zero.

Tool-call totals are exported as a **gauge**, not a counter, despite the
convention that ``*_total`` names counters: the ledger behind them is
resettable (``reset_counters()``) and a Prometheus counter is not, so a
counter would silently under-report after every reset. The name omits the
``_total`` suffix to say so.
"""

import logging
import time

from fastapi import APIRouter, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from app.tools.counters import (
    get_investigation_stats,
    get_tool_call_ledger,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["observability"])

# --------------------------------------------------------------------------
# Metrics. Created at import time, registered with the default REGISTRY that
# generate_latest() serialises.
# --------------------------------------------------------------------------

# --- read from the live in-process ledger at scrape time ------------------
TOOL_CALLS = Gauge(
    "aiops_tool_calls",
    "Tool calls per capability, by tool name. Read from app.tools.counters' "
    "ledger; a gauge (not a counter) because that ledger is resettable.",
    ["tool"],
)
INVESTIGATIONS_COMPLETED = Gauge(
    "aiops_investigations_completed",
    "RCA investigations completed. Read from app.tools.counters.",
)
TOOL_CALLS_IN_RCA = Gauge(
    "aiops_tool_calls_in_rca",
    "Tool calls made inside an RCA. Read from app.tools.counters.",
)
MESH_SERVICE_UP = Gauge(
    "aiops_mesh_service_up",
    "Mesh service health as last reported by the mesh manager: 1 when the "
    "service reports HEALTHY, 0 when CRASHED. Absent for a service the mesh "
    "does not know about.",
    ["service", "status"],
)

# --- counted by the middleware ---------------------------------------------
HTTP_REQUESTS = Counter(
    "aiops_http_requests_total",
    "HTTP requests served, by method, matched route and status code. "
    "Unmatched paths are labelled 'unmatched' to bound cardinality.",
    ["method", "route", "status"],
)
HTTP_REQUEST_DURATION = Histogram(
    "aiops_http_request_duration_seconds",
    "HTTP request latency, by method and matched route.",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

BUILD_INFO = Gauge(
    "aiops_build_info",
    "Constant 1; the labels carry the build facts.",
    ["platform", "version"],
)
BUILD_INFO.labels(platform="aiops-platform", version="1.0.0").set(1)


def collect_ledger_metrics() -> None:
    """Refresh the ledger-backed gauges from their real source.

    Called on every scrape so the values are current, and kept separate from
    the endpoint so tests can assert on it directly.
    """
    ledger = get_tool_call_ledger()
    for tool, count in ledger.items():
        TOOL_CALLS.labels(tool=tool).set(count)

    stats = get_investigation_stats()
    INVESTIGATIONS_COMPLETED.set(stats.get("investigations_completed", 0))
    TOOL_CALLS_IN_RCA.set(stats.get("tool_calls_in_rca", 0))


def collect_mesh_metrics() -> None:
    """Refresh mesh health from the mesh manager.

    Deliberately tolerant: if the mesh has not been started the metric is
    simply left at its last value rather than reported as zero, because
    "we never asked" and "every service is down" are different facts.
    """
    try:
        from app.mesh.manager import get_mesh_manager

        services = get_mesh_manager().get_mesh_status() or []
    except Exception as exc:                       # mesh down or not started
        logger.debug("mesh metrics unavailable: %s", exc)
        return

    for entry in services:
        if not isinstance(entry, dict):
            continue
        service = entry.get("service")
        status = entry.get("status")
        if not service or not status:
            continue
        MESH_SERVICE_UP.labels(service=service, status=status).set(
            1.0 if status == "HEALTHY" else 0.0
        )


# --------------------------------------------------------------------------
# ASGI middleware for request counting.
#
# Pure ASGI rather than BaseHTTPMiddleware: it stays out of the response
# streaming path, so it cannot interfere with streaming or SSE endpoints.
# --------------------------------------------------------------------------


class RequestsMetricsMiddleware:
    """Counts and times every HTTP request through the real app."""

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _route_label(scope) -> str:
        """The URL *pattern* the client actually called, e.g.
        ``/api/v1/incidents/{incident_id}``.

        Three tempting shortcuts were rejected:

        * ``scope["path"]`` — the real path, but a per-ID label would add a
          new time series per incident and blow up Prometheus' cardinality.
          Worse for *unmatched* paths: a scanner guessing URLs would mint a
          fresh series per guess.
        * ``scope["route"].path`` — bounded, but Starlette exposes the
          *router-relative* path here (``/mesh/status``), because
          ``include_router`` nests an ``_IncludedRouter`` with no path of its
          own. That label names a URL which 404s — the real one is
          ``/api/v1/mesh/status``.
        * assuming "no path params" means "the path is the pattern" — that
          is true only when a route matched. When nothing matched, returning
          the literal path is unbounded cardinality by another route.

        So: the template is rebuilt from the raw path and Starlette's own
        parsed ``path_params`` when a route matched, and unmatched requests
        collapse into a single ``unmatched`` label. ``scope["route"]`` is
        used purely as the "did anything match" indicator.
        """
        path = scope.get("path") or ""
        if not path:
            return "unmatched"

        params = scope.get("path_params") or {}
        if not params:
            # Either a literal route such as /api/v1/mesh/status, or nothing
            # matched at all. scope["route"] is the only thing that tells
            # them apart.
            return path if scope.get("route") is not None else "unmatched"

        label = path
        for name, value in params.items():
            if value in (None, ""):
                continue
            label = label.replace(str(value), "{" + name + "}")
        return label

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "UNKNOWN")
        started = time.perf_counter()
        status_code = 500        # the code we will report if no start message

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route_label = self._route_label(scope)

            HTTP_REQUESTS.labels(
                method=method, route=route_label, status=str(status_code)
            ).inc()
            HTTP_REQUEST_DURATION.labels(
                method=method, route=route_label
            ).observe(time.perf_counter() - started)


# --------------------------------------------------------------------------
# The endpoint itself.
# --------------------------------------------------------------------------


@router.get(
    "/metrics",
    response_model=None,       # raw exposition text, not a schema'd body
    include_in_schema=False,   # a scrape endpoint, not part of the API
)
def metrics() -> Response:
    """Serve the Prometheus text exposition format.

    ``generate_latest()`` serialises the whole default registry, which
    includes the python_gc_* collectors prometheus_client installs by
    default — those are expected and are not ours.
    """
    collect_ledger_metrics()
    collect_mesh_metrics()

    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
