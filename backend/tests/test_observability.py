"""Tests for Track T1 — observability (Prometheus / Loki / Tempo).

Three layers, in order of how much they prove:

1. **The endpoint and middleware** — no backends needed. These always run.
2. **The Loki handler's contract** — payload shape, bounded labels, and the
   rule that a delivery failure is reported rather than faked.
3. **Live-backend integration** — skipped, loudly, when the stack is not
   running. These are the ones that satisfy T1's step 5 ("prove PromQL /
   LogQL / TraceQL are actually called"), so they are written to hit the
   real docker-compose backends started by ``make observability-up``.

Live detection uses the ``*_LOCAL_URL`` settings rather than
``PROMETHEUS_URL``/``LOKI_URL``, so these tests work from any working
directory regardless of where ``.env`` happens to live. The tools under test
are then pointed at the local stack explicitly, which exercises the real
code path instead of a reimplementation of it.
"""

import json
import logging
import queue
import re
import time
from typing import Dict, List, Tuple

import httpx
import pytest

from app.api.metrics import RequestsMetricsMiddleware
from app.config import settings


def asyncio_run(coro):
    """Run a coroutine from a sync test without needing a plugin fixture."""
    import asyncio

    return asyncio.run(coro)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _series(text: str, metric_name: str) -> List[Tuple[Dict[str, str], float]]:
    """Parse one metric family out of Prometheus exposition text.

    Anchored so ``aiops_tool_calls`` does not also swallow
    ``aiops_tool_calls_in_rca``.
    """
    pattern = re.compile(
        rf"^{re.escape(metric_name)}(?:\{{(.*)\}})?\s+([^\s#]+)$"
    )
    found: List[Tuple[Dict[str, str], float]] = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = pattern.match(line)
        if not match:
            continue
        raw_labels, raw_value = match.group(1), match.group(2)
        labels = dict(re.findall(r'(\w+)="([^"]*)"', raw_labels or ""))
        found.append((labels, float(raw_value)))
    return found


def _label_index(
    entries: List[Tuple[Dict[str, str], float]], key: str
) -> Dict[str, float]:
    return {labels[key]: value for labels, value in entries if key in labels}


def _reachable(url: str, path: str, timeout: float = 2.0) -> bool:
    try:
        return httpx.get(f"{url.rstrip('/')}{path}", timeout=timeout).is_success
    except Exception:
        return False


PROMETHEUS_UP = _reachable(settings.PROMETHEUS_LOCAL_URL, "/-/ready")
LOKI_UP = _reachable(settings.LOKI_LOCAL_URL, "/ready")
TEMPO_UP = _reachable(settings.TEMPO_LOCAL_URL, "/ready")

requires_prometheus = pytest.mark.skipif(
    not PROMETHEUS_UP,
    reason=(
        f"Prometheus not reachable at {settings.PROMETHEUS_LOCAL_URL} — "
        f"start the stack with `make observability-up` to run this test"
    ),
)
requires_loki = pytest.mark.skipif(
    not LOKI_UP,
    reason=(
        f"Loki not reachable at {settings.LOKI_LOCAL_URL} — "
        f"start the stack with `make observability-up` to run this test"
    ),
)
requires_tempo = pytest.mark.skipif(
    not TEMPO_UP,
    reason=(
        f"Tempo not reachable at {settings.TEMPO_LOCAL_URL} — "
        f"start the stack with `make observability-up` to run this test"
    ),
)


# --------------------------------------------------------------------------
# 1. the /metrics endpoint
# --------------------------------------------------------------------------


def test_metrics_is_served_at_the_root_where_prometheus_scrapes(test_client):
    assert test_client.get("/metrics").status_code == 200


def test_metrics_is_not_hiding_under_the_api_prefix(test_client):
    """Scrapers do not know about API_V1_PREFIX, so /metrics must not need it."""
    assert test_client.get("/api/v1/metrics").status_code == 404


def test_metrics_uses_the_prometheus_text_format(test_client):
    response = test_client.get("/metrics")

    # prometheus_client negotiates its own exposition version, so the exact
    # `version=` parameter is not ours to pin — only the media type is.
    assert "text/plain" in response.headers["content-type"]
    assert "# HELP " in response.text
    assert "# TYPE " in response.text


def test_metrics_build_info_is_a_constant_one(test_client):
    entries = _series(test_client.get("/metrics").text, "aiops_build_info")
    assert entries, "build info gauge is missing"
    labels, value = entries[0]
    assert value == 1.0, "build_info is a constant 1; the labels carry the facts"
    assert labels["platform"] == "aiops-platform"
    assert labels["version"]


def test_tool_call_gauges_mirror_the_real_ledger(test_client):
    """The single most important property of this endpoint.

    The gauge must be read from app.tools.counters' ledger, not kept as a
    second set of counters — otherwise Prometheus and the dashboard could
    drift, and the dashboard would be reporting a number nothing else could
    confirm.
    """
    from app.tools.counters import get_tool_call_ledger, record_tool_call

    record_tool_call("promql")             # mutate the real ledger
    record_tool_call("promql")
    ledger = get_tool_call_ledger()

    entries = _series(test_client.get("/metrics").text, "aiops_tool_calls")
    exposed = _label_index(entries, "tool")

    assert exposed == {tool: float(count) for tool, count in ledger.items()}, (
        "/metrics tool-call values must equal the ledger exactly"
    )


def test_ledger_backed_gauges_are_exported_as_gauges_not_counters(test_client):
    """The ledger is resettable; a Prometheus counter is not, so a *_total
    counter name would silently under-report after every reset."""
    body = test_client.get("/metrics").text
    assert re.search(r"# TYPE aiops_tool_calls gauge", body), (
        "aiops_tool_calls must be a gauge: reset_counters() can take it down"
    )
    assert "aiops_tool_calls_total" not in body, (
        "a *_total name promises a counter; this value can decrease"
    )


def test_investigation_counters_are_exported(test_client):
    from app.tools.counters import get_investigation_stats, record_investigation

    record_investigation(["promql", "logql"])
    stats = get_investigation_stats()
    body = test_client.get("/metrics").text

    completed = _series(body, "aiops_investigations_completed")
    in_rca = _series(body, "aiops_tool_calls_in_rca")

    assert completed[0][1] == float(stats["investigations_completed"])
    assert in_rca[0][1] == float(stats["tool_calls_in_rca"])


def test_mesh_health_is_exported_when_the_mesh_is_running(test_client):
    """Mesh health is read live; if the mesh is down the series is absent,
    never reported as 'every service is down'."""
    from app.mesh.manager import get_mesh_manager

    body = test_client.get("/metrics").text
    entries = _series(body, "aiops_mesh_service_up")

    try:
        services = get_mesh_manager().get_mesh_status() or []
    except Exception:
        services = []

    if not services:
        assert entries == [], "no mesh data means no series, not zero"
        return

    healthy = _label_index(entries, "service")
    assert len(healthy) == len(services)
    for service, value in healthy.items():
        assert value in (0.0, 1.0), "mesh health is 1 (HEALTHY) or 0 (CRASHED)"


def test_health_values_match_what_the_mesh_actually_reports(test_client):
    from app.mesh.manager import get_mesh_manager

    try:
        services = get_mesh_manager().get_mesh_status() or []
    except Exception:
        pytest.skip("mesh manager unavailable in this environment")

    if not services:
        pytest.skip("mesh manager reported no services")

    body = test_client.get("/metrics").text
    entries = _series(body, "aiops_mesh_service_up")

    reported = {
        (labels["service"], labels["status"]): value
        for labels, value in entries
    }
    for service in services:
        name, status = service.get("service"), service.get("status")
        expected = 1.0 if status == "HEALTHY" else 0.0
        assert reported.get((name, status)) == expected


# --------------------------------------------------------------------------
# 2. the request-counting middleware
# --------------------------------------------------------------------------


def test_route_label_keeps_a_matched_literal_path_alone():
    scope = {
        "path": "/api/v1/health",
        "path_params": {},
        "route": object(),          # Starlette sets this when a route matched
    }
    assert RequestsMetricsMiddleware(None)._route_label(scope) == "/api/v1/health"


def test_route_label_collapses_a_path_that_no_route_matched():
    """Unbounded-cardinality guard: a scanner guessing URLs must not be able
    to mint a new time series per guess."""
    scope = {"path": "/api/v1/definitely-not-a-route", "path_params": {}}
    assert RequestsMetricsMiddleware(None)._route_label(scope) == "unmatched"


def test_route_label_rebuilds_the_real_url_pattern():
    """Two different incident ids must collapse into one bounded series."""
    scope = {
        "path": "/api/v1/incidents/INC-0001",
        "path_params": {"incident_id": "INC-0001"},
        "route": object(),
    }
    assert RequestsMetricsMiddleware(None)._route_label(scope) == "/api/v1/incidents/{incident_id}"


def test_route_label_is_unchanged_for_a_second_value_of_the_same_param():
    first = RequestsMetricsMiddleware(None)._route_label({
        "path": "/api/v1/incidents/INC-0001",
        "path_params": {"incident_id": "INC-0001"},
        "route": object(),
    })
    second = RequestsMetricsMiddleware(None)._route_label({
        "path": "/api/v1/incidents/INC-9999",
        "path_params": {"incident_id": "INC-9999"},
        "route": object(),
    })
    assert first == second, "one incident id per time series would not scale"


def test_route_label_handles_several_params_and_ignores_empty_ones():
    scope = {
        "path": "/api/v1/tenants/tenant_b/incidents/INC-1",
        "path_params": {"tenant_id": "tenant_b", "incident_id": "INC-1",
                        "note": "", "missing": None},
        "route": object(),
    }
    label = RequestsMetricsMiddleware(None)._route_label(scope)
    assert label == "/api/v1/tenants/{tenant_id}/incidents/{incident_id}"


def test_route_label_never_returns_none_for_an_empty_scope():
    assert RequestsMetricsMiddleware(None)._route_label({}) == "unmatched"


def test_requests_are_counted_with_the_real_url_pattern(test_client):
    """End-to-end: two distinct ids must not create two time series.

    Asserted without reference to what was counted *before* — other test
    files legitimately request this same route, so "the series is new" would
    be an order-dependent assertion that fails in the full suite while
    passing in isolation.
    """
    from app.api.metrics import HTTP_REQUESTS

    test_client.get("/api/v1/incidents/INC-T1-A")
    test_client.get("/api/v1/incidents/INC-T1-B")

    routes = {
        sample.labels["route"] for sample in HTTP_REQUESTS.collect()[0].samples
    }

    assert "/api/v1/incidents/{incident_id}" in routes, (
        "the middleware must label by URL pattern, not by literal id"
    )
    leaked = sorted(route for route in routes if "INC-T1-" in route)
    assert leaked == [], f"a raw incident id leaked into a label: {leaked}"


def test_unmatched_paths_collapse_into_one_label(test_client):
    from app.api.metrics import HTTP_REQUESTS

    test_client.get("/definitely/not/a/route/one")
    test_client.get("/definitely/not/a/route/two")

    routes = {
        sample.labels["route"]
        for sample in HTTP_REQUESTS.collect()[0].samples
    }
    leaked = [r for r in routes if "definitely/not/a/route" in r]
    assert leaked == [], (
        f"unknown paths must share the 'unmatched' label, got {leaked}"
    )


def test_latency_is_observed_alongside_the_count(test_client):
    from app.api.metrics import HTTP_REQUEST_DURATION

    test_client.get("/metrics")

    samples = [
        s for s in HTTP_REQUEST_DURATION.collect()[0].samples
        if s.name.endswith("_count") and s.labels.get("route") == "/metrics"
    ]
    assert samples, "no latency sample recorded for /metrics"
    assert all(s.value >= 1 for s in samples)


def test_non_http_scopes_pass_through_untouched():
    """ASGI scopes other than http (websocket, lifespan) must not be counted."""
    import asyncio

    called = []

    async def inner_app(scope, receive, send):
        called.append(scope["type"])

    middleware = RequestsMetricsMiddleware(inner_app)
    asyncio.run(middleware({"type": "lifespan"}, None, None))

    assert called == ["lifespan"]


# --------------------------------------------------------------------------
# 3. the Loki log-shipping handler
# --------------------------------------------------------------------------


@pytest.fixture
def clean_root_logger():
    """Temporarily remove any Loki handler the app's lifespan installed.

    The session-scoped TestClient boots the app, which installs a real
    LokiLoggingHandler. Without this, install tests would hit the
    idempotency path and never exercise the decision they are about.
    """
    import logging as _logging

    root = _logging.getLogger()
    removed = [
        h for h in root.handlers
        if h.__class__.__name__ == "LokiLoggingHandler"
    ]
    for handler in removed:
        root.removeHandler(handler)
    yield
    for handler in removed:
        root.addHandler(handler)


def _handler(**kwargs):
    from app.tools.loki_handler import LokiLoggingHandler

    handler = LokiLoggingHandler(**kwargs)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    return handler


def test_loki_payload_uses_the_documented_push_shape():
    handler = _handler()
    try:
        record = logging.LogRecord(
            name="app.test", level=logging.INFO, pathname=__file__,
            lineno=1, msg="cart-service latency 300ms", args=(), exc_info=None,
        )
        payload = handler._build_payload([record])
    finally:
        handler.close()

    assert set(payload) == {"streams"}
    stream = payload["streams"][0]
    assert set(stream) == {"stream", "values"}

    nanos, line = stream["values"][0]
    assert nanos.isdigit() and len(nanos) == 19, "Loki wants nanosecond strings"
    assert "cart-service latency 300ms" in line


def test_labels_are_bounded_and_never_carry_the_message():
    """Labels are indexed by Loki; putting message text in one would explode
    its index. Only app/level/logger may appear."""
    handler = _handler()
    try:
        records = [
            logging.LogRecord(
                name="app.mesh.manager", level=logging.ERROR, pathname=__file__,
                lineno=1,
                msg=f"unique-failure-{n}-for-cardinality-check", args=(),
                exc_info=None,
            )
            for n in range(5)
        ]
        payload = handler._build_payload(records)
    finally:
        handler.close()

    labels = payload["streams"][0]["stream"]
    assert set(labels) == {"app", "level", "logger"}
    assert labels["app"] == "aiops"
    assert labels["level"] == "ERROR"

    # Five distinct messages must still be ONE stream, not five.
    assert len(payload["streams"]) == 1
    assert len(payload["streams"][0]["values"]) == 5


def test_records_are_grouped_by_level_and_logger():
    handler = _handler()
    try:
        def rec(name, level):
            return logging.LogRecord(
                name=name, level=level, pathname=__file__, lineno=1,
                msg="x", args=(), exc_info=None,
            )

        payload = handler._build_payload([
            rec("app.a", logging.INFO),
            rec("app.a", logging.WARNING),
            rec("app.b", logging.INFO),
        ])
    finally:
        handler.close()

    assert len(payload["streams"]) == 3


def test_emit_never_raises_into_the_logging_call_path():
    """A broken log aggregator must not take down the request that logged."""
    handler = _handler()

    class _Full:
        def put_nowait(self, record):
            raise RuntimeError("queue exploded")

    handler._queue = _Full()
    record = logging.LogRecord(
        name="app", level=logging.INFO, pathname=__file__, lineno=1,
        msg="x", args=(), exc_info=None,
    )

    try:
        handler.emit(record)        # must not raise
    finally:
        # Restore a working queue so close() can drain and stop the worker.
        handler._queue = queue.Queue()
        handler.close()


def test_the_handler_does_not_re_enter_itself():
    """It logs its own delivery failures; those must not come back round."""
    handler = _handler()
    try:
        tagged = logging.LogRecord(
            name="app.tools.loki_handler", level=logging.WARNING,
            pathname=__file__, lineno=0,
            msg="delivery failed", args=(), exc_info=None,
        )
        tagged._shipped_by_loki_handler = True

        before = handler._queue.qsize()
        handler.emit(tagged)
        assert handler._queue.qsize() == before, (
            "the handler's own records must be dropped, not re-queued"
        )
    finally:
        handler.close()


def test_a_failed_push_is_reported_never_faked(monkeypatch):
    """THE honesty contract: a delivery failure returns False and warns. It
    must never return True, and must never raise."""
    import app.tools.loki_handler as mod

    def _refuse(*args, **kwargs):
        return httpx.Response(500, text="ingester unhappy", request=httpx.Request("POST", "http://x"))

    monkeypatch.setattr(mod.httpx, "post", _refuse)
    handler = _handler()

    try:
        assert handler._post({"streams": []}) is False, (
            "a rejected push reported as success would be a lie"
        )
    finally:
        handler.close()


def test_a_transport_failure_is_reported_never_faked(monkeypatch):
    import app.tools.loki_handler as mod

    def _boom(*args, **kwargs):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(mod.httpx, "post", _boom)
    handler = _handler()

    try:
        assert handler._post({"streams": []}) is False
    finally:
        handler.close()


def test_an_unreachable_loki_is_not_installed(clean_root_logger, monkeypatch):
    """Installing against a dead Loki would spawn a thread that fails
    silently forever. It must refuse instead, leaving logging untouched."""
    import app.tools.loki_handler as mod

    monkeypatch.setattr(mod, "loki_reachable", lambda *a, **k: False)
    root = logging.getLogger()
    before = list(root.handlers)

    assert mod.install_loki_logging(url="http://localhost:3199") is None
    assert list(root.handlers) == before, "logging must be left exactly as it was"


def test_install_is_idempotent(clean_root_logger, monkeypatch):
    """A second install must not attach a duplicate handler — that would
    ship every log line twice."""
    import app.tools.loki_handler as mod

    monkeypatch.setattr(mod, "loki_reachable", lambda *a, **k: True)
    root = logging.getLogger()
    before = list(root.handlers)

    first = second = None
    try:
        first = mod.install_loki_logging(url="http://localhost:3199")
        second = mod.install_loki_logging(url="http://localhost:3199")
        assert first is not None
        assert second is first
        assert list(root.handlers).count(first) == 1
    finally:
        if first is not None:
            root.removeHandler(first)
            first.close()
        assert list(root.handlers) == before


def test_a_queued_record_is_delivered_before_close(monkeypatch):
    """close() must drain what is pending rather than dropping it."""
    import app.tools.loki_handler as mod

    pushed = []

    class _Resp:
        status_code = 204
        text = ""

    def _post(url, json=None, timeout=None):
        pushed.append(json)
        return _Resp()

    monkeypatch.setattr(mod.httpx, "post", _post)
    handler = _handler(flush_interval=60.0)   # never auto-flushes

    try:
        record = logging.LogRecord(
            name="app.test", level=logging.INFO, pathname=__file__, lineno=1,
            msg="final words", args=(), exc_info=None,
        )
        handler.emit(record)
        handler.flush_remaining()

        assert pushed, "close/flush must deliver what is queued, not drop it"
        delivered = pushed[0]["streams"][0]["values"][0][1]
        assert "final words" in delivered
    finally:
        handler.close()


# --------------------------------------------------------------------------
# 4. live backends — the real proof (T1 step 5)
# --------------------------------------------------------------------------


@pytest.fixture
def local_prometheus(monkeypatch):
    monkeypatch.setattr(settings, "PROMETHEUS_URL", settings.PROMETHEUS_LOCAL_URL)


@pytest.fixture
def local_loki(monkeypatch):
    monkeypatch.setattr(settings, "LOKI_URL", settings.LOKI_LOCAL_URL)


@pytest.fixture
def local_tempo(monkeypatch):
    monkeypatch.setattr(settings, "TEMPO_URL", settings.TEMPO_LOCAL_URL)


@requires_prometheus
def test_promql_returns_real_series_not_a_fallback(local_prometheus):
    """The done-when for step 4: real JSON, not an error envelope."""
    from app.tools.telemetry import TelemetryTools

    result = asyncio_run(TelemetryTools.query_prometheus("up"))

    assert "error" not in result, (
        f"PromQL fell back instead of querying: {result.get('error')}"
    )
    assert result.get("resultType") == "vector"
    assert result.get("result"), "'up' must return at least Prometheus itself"


@requires_prometheus
def test_the_backend_is_actually_being_scraped(local_prometheus):
    """Prometheus must be reaching the backend's /metrics, or the whole
    endpoint is decorative."""
    from app.tools.telemetry import TelemetryTools

    result = asyncio_run(TelemetryTools.query_prometheus(
        'up{job="aiops-backend"}'
    ))
    assert "error" not in result

    values = [
        float(s["value"][1]) for s in result.get("result", []) if "value" in s
    ]
    assert values, "no aiops-backend target configured in Prometheus"
    assert values == [1.0], (
        "Prometheus is configured to scrape the backend but cannot reach it"
    )


@requires_prometheus
def test_the_scrape_pipeline_delivers_the_numbers_actually_being_served(
    local_prometheus,
):
    """The strongest evidence the pipeline is real.

    It compares two views of the **same** process: what the running backend
    serves at :8000/metrics, and what Prometheus has stored for that target.
    Comparing against this test process's own ledger would be meaningless —
    the ledger is in-process state, and Prometheus scrapes the *backend*, a
    different process with a different ledger.

    Prometheus is allowed to lag: it scrapes every 15s, so its view is
    legitimately behind by up to one interval. The assertion is therefore a
    **bound**, not equality — after waiting out an interval its value must
    have reached the first snapshot, and it can never exceed the second. A
    broken or stalled scrape fails both directions.
    """
    from app.tools.telemetry import TelemetryTools

    def backend_values() -> dict:
        body = httpx.get("http://localhost:8000/metrics", timeout=5.0).text
        return _label_index(_series(body, "aiops_tool_calls"), "tool")

    def prometheus_values() -> dict:
        result = asyncio_run(TelemetryTools.query_prometheus("aiops_tool_calls"))
        assert "error" not in result, result.get("error")
        return {
            s["metric"].get("tool"): float(s["value"][1])
            for s in result.get("result", [])
        }

    try:
        earlier = backend_values()
    except Exception as exc:
        pytest.skip(f"no backend running on :8000 to compare against: {exc}")
    assert earlier, "the running backend exposes no tool-call gauges"

    # Longer than one 15s scrape interval, so a working scrape must land.
    time.sleep(20)

    later = backend_values()
    stored = prometheus_values()

    assert stored, "Prometheus has scraped no aiops_tool_calls series at all"

    for tool, value in stored.items():
        assert tool in later, f"Prometheus has a series the backend does not serve: {tool}"
        assert value <= later[tool], (
            f"Prometheus holds {tool}={value}, ahead of the backend's own "
            f"{later[tool]}; it is scraping something other than this backend"
        )
        assert value >= earlier[tool], (
            f"Prometheus still holds {tool}={value}, which the backend had "
            f"{earlier[tool]} more than one scrape interval ago. Either the "
            f"scrape is stalled or the target is down."
        )


@requires_prometheus
def test_every_declared_tool_appears_in_prometheus(local_prometheus):
    """T1 step 5's done-when, part one: the three query tools are counted."""
    from app.tools.telemetry import TelemetryTools

    asyncio_run(TelemetryTools.query_prometheus("up"))          # promql
    result = asyncio_run(TelemetryTools.query_prometheus("aiops_tool_calls"))

    tools = {
        s["metric"].get("tool") for s in result.get("result", [])
    }
    assert {"promql", "logql", "traceql"} <= tools, (
        f"missing from Prometheus: {{'promql','logql','traceql'}} - {tools}"
    )


@requires_loki
def test_logql_returns_real_log_lines_not_a_fallback(local_loki):
    from app.tools.telemetry import TelemetryTools

    result = asyncio_run(TelemetryTools.query_loki('{app="aiops"}', limit=10))

    assert "error" not in result, (
        f"LogQL fell back instead of querying: {result.get('error')}"
    )
    assert result.get("result"), (
        "no {app=\"aiops\"} streams in Loki — the app is not shipping logs"
    )


@requires_loki
def test_a_log_line_ships_end_to_end_and_comes_back(local_loki):
    """Write through the handler, read back through the LogQL tool. This is
    the only test that proves step 3 rather than assuming it."""
    import app.tools.loki_handler as mod
    from app.tools.telemetry import TelemetryTools

    marker = f"t1-e2e-marker-{int(time.time() * 1000)}"

    handler = mod.LokiLoggingHandler(url=settings.LOKI_LOCAL_URL)
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        record = logging.LogRecord(
            name="app.t1.test", level=logging.INFO, pathname=__file__,
            lineno=1, msg=marker, args=(), exc_info=None,
        )
        handler.emit(record)
        handler.flush_remaining()
    finally:
        handler.close()

    # Loki indexes asynchronously; poll briefly rather than assert on luck.
    found = []
    for _ in range(12):
        time.sleep(1.0)
        result = asyncio_run(TelemetryTools.query_loki(
            '{app="aiops"} |= "t1-e2e-marker"', limit=10
        ))
        for stream in result.get("result", []):
            for _, line in stream.get("values", []):
                if marker in line:
                    found.append(line)
        if found:
            break

    assert found, (
        f"log line {marker!r} was pushed to Loki but never became queryable"
    )


@requires_tempo
def test_traceql_reaches_tempo_and_reports_an_absent_trace_honestly(
    local_tempo,
):
    """A missing trace must be reported as not-found, not as a connection
    failure and not as an invented empty trace."""
    from app.tools.telemetry import TelemetryTools

    result = asyncio_run(TelemetryTools.query_tempo("0" * 32))

    assert "error" in result, "Tempo answered 404; that must surface as an error"
    assert "404" in result["error"], (
        f"expected an honest not-found, got: {result['error']}"
    )
    assert "mock_data" not in result, "a fabricated trace must never appear"


@requires_tempo
def test_tempo_is_reachable_on_its_health_endpoint(local_tempo):
    assert _reachable(settings.TEMPO_LOCAL_URL, "/ready"), (
        f"Tempo reported not-ready at {settings.TEMPO_LOCAL_URL}"
    )
