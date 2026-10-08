"""
Tool registry for the autonomous SRE agent (Track T4, step 1).

Every capability the agent may invoke is registered here **by name**, so the
ReAct loop in :mod:`app.agent.sre_agent` can discover, describe and call a
tool without importing anything itself. Nothing outside this module needs to
know how a tool is implemented — only that it answers with an envelope.

HONESTY CONTRACT
----------------
Each call returns::

    {"tool", "target", "ok", "data", "error"}

``ok`` is True **only** when the backing service answered with data we are
allowed to use. A tool whose backend is unreachable comes back ``ok=False``
with the reason and **no substitute payload** — no mock, no cached value, no
fabricated reading.

That matters twice over: the agent uses ``ok`` to decide its next step, and to
price its confidence. So an absent Prometheus *lowers* the confidence score
and can push the RCA under the ``RCA_MIN_CONFIDENCE`` gate, instead of
quietly propping the number up with invented telemetry.
"""

import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.tools.counters import record_tool_call

logger = logging.getLogger(__name__)

#: Every tool answers with this envelope.
ToolResult = Dict[str, Any]


@dataclass(frozen=True)
class ToolSpec:
    """One registered capability: what it is called and how to run it."""

    name: str
    description: str
    run: Callable[..., Any]          # may be sync or async


# ---------------------------------------------------------------------------
# Tool implementations
#
# Deliberately thin: they only fetch. Deciding what a result *means* (the
# verdict that drives confidence) belongs to the agent, not to the tool.
# ---------------------------------------------------------------------------

async def _mesh_status(service: str) -> Any:
    """Live health of the local mesh services (HEALTHY/CRASHED, CPU, latency)."""
    # TelemetryTools records promql/logql/traceql/k8s_api itself; the mesh
    # reader has no equivalent, so it counts its own calls.
    record_tool_call("mesh_status")
    from app.mesh.manager import get_mesh_manager
    return get_mesh_manager().get_mesh_status()


async def _k8s_api(service: str, namespace: str = "default") -> Any:
    """Kubernetes pod phases and restart counts for a service."""
    from app.tools.telemetry import TelemetryTools
    return TelemetryTools.get_k8s_pod_status(service_name=service, namespace=namespace)


async def _promql(query: str) -> Any:
    """Prometheus PromQL query (error rate, latency)."""
    from app.tools.telemetry import TelemetryTools
    return await TelemetryTools.query_prometheus(query=query)


async def _logql(query: str, limit: int = 50) -> Any:
    """Loki LogQL query over application logs."""
    from app.tools.telemetry import TelemetryTools
    return await TelemetryTools.query_loki(query=query, limit=limit)


async def _traceql(trace_id: str) -> Any:
    """Tempo trace lookup by trace id."""
    from app.tools.telemetry import TelemetryTools
    return await TelemetryTools.query_tempo(trace_id=trace_id)


#: The registry itself — name -> spec. This is what "tools callable by name"
#: means for PHASE_PLAN T4 step 1.
TOOL_REGISTRY: Dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            "mesh_status",
            "Live health of the 5 local mesh services: status, CPU, latency, "
            "whether the process is crashed.",
            _mesh_status,
        ),
        ToolSpec(
            "k8s_api",
            "Kubernetes pod phases and restart counts for a service.",
            _k8s_api,
        ),
        ToolSpec(
            "promql",
            "Prometheus PromQL query for error rate / latency / saturation.",
            _promql,
        ),
        ToolSpec(
            "logql",
            "Loki LogQL query over the application logs.",
            _logql,
        ),
        ToolSpec(
            "traceql",
            "Tempo trace lookup by trace id, for end-to-end request paths.",
            _traceql,
        ),
    )
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def tool_names() -> List[str]:
    """Names the ReAct loop may select from, in registration order."""
    return list(TOOL_REGISTRY)


def list_tools() -> List[Dict[str, str]]:
    """Describe every tool: name + what it is for."""
    return [{"name": t.name, "description": t.description} for t in TOOL_REGISTRY.values()]


def get_tool(name: str) -> ToolSpec:
    """Look up a tool by name.

    Raises ``ValueError`` listing the valid names rather than a bare
    ``KeyError``, so a typo in the agent produces a readable message.
    """
    try:
        return TOOL_REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"unknown tool {name!r}; available: {', '.join(sorted(TOOL_REGISTRY))}"
        ) from None


def _interpret(raw: Any) -> Tuple[Any, Optional[str]]:
    """Turn a tool's raw return into ``(data, error)``.

    The existing telemetry layer reports failure by returning a dict carrying
    an ``error`` key (and sometimes a ``mock_data`` payload beside it). We
    treat any such dict as a failure and drop the payload entirely, so mock
    data can never travel downstream as if it were an observation.
    """
    if raw is None:
        return None, "tool returned no data"
    if isinstance(raw, dict):
        err = raw.get("error")
        if isinstance(err, str) and err.strip():
            return None, err
    return raw, None


async def call_tool(name: str, **kwargs: Any) -> ToolResult:
    """Invoke a registered tool and always return an envelope.

    Never raises: a tool that explodes is itself a valid observation
    (``ok=False`` with the reason), because a crash mid-investigation must not
    take the whole RCA down with it.
    """
    spec = get_tool(name)
    target = kwargs.get("service") or kwargs.get("trace_id") or kwargs.get("query") or ""

    try:
        raw = spec.run(**kwargs)
        if inspect.isawaitable(raw):
            raw = await raw
    except Exception as exc:                       # noqa: BLE001
        logger.debug("tool %s raised %s: %s", name, type(exc).__name__, exc)
        return {
            "tool": name,
            "target": str(target),
            "ok": False,
            "data": None,
            "error": f"{type(exc).__name__}: {exc}",
        }

    data, error = _interpret(raw)
    return {
        "tool": name,
        "target": str(target),
        "ok": error is None,
        "data": data,
        "error": error,
    }
