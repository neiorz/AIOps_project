"""
Tests for the autonomous SRE agent (Track T4).

These are the tests that were missing when T4 was parked as "not yet
tested". They earned their keep immediately: as written, the loop crashed
with ``AttributeError: 'CorrelatedIncident' object has no attribute
'labels'`` on every single investigation, at its fifth ReAct step. The code
parsed, was never executed, and would have shipped broken.

What is covered:

* the registry — every capability callable by name, and the honesty contract
  that a dead backend yields ``ok=False`` with the payload *dropped*, never
  a substitute reading;
* the loop — observation-driven tool ordering, termination, evidence
  sufficiency;
* confidence — computed from real evidence, with no invented fallback;
* the dashboard seam — ``avg_tool_calls_per_rca > 1``, which the pre-T4
  agent could never satisfy but a single tool call could.

``ToolSpec`` is a frozen dataclass, so tools are substituted with
``monkeypatch.setitem(TOOL_REGISTRY, ...)`` rather than by assigning to
``spec.run``.
"""

import asyncio
import re
from pathlib import Path

import pytest

from app.agent import sre_agent as agent_module
from app.agent.sre_agent import MAX_STEPS, MIN_EVIDENCE, AutonomousSREAgent
from app.agent.tools import (
    TOOL_REGISTRY,
    ToolSpec,
    call_tool,
    get_tool,
    list_tools,
    tool_names,
)
from app.correlation.engine import CorrelatedIncident, RawAlert

REPO_ROOT = Path(__file__).resolve().parents[2]

SERVICE = "cart-service"


# --------------------------------------------------------------------------
# fixtures and helpers
# --------------------------------------------------------------------------

def _incident(trace_id=None, service=SERVICE):
    labels = {"trace_id": trace_id} if trace_id else {}
    return CorrelatedIncident(
        incident_id="INC-42",
        primary_service=service,
        root_cause_candidate="cpu_stress",
        severity="HIGH",
        tenant_id="tenant_b",
        created_at=1_700_000_000.0,
        updated_at=1_700_000_010.0,
        total_alerts=2,
        affected_services=[service],
        alert_ids=["a1", "a2"],
        sample_alerts=[
            RawAlert(id="a1", alertname="HighCPU", service=service, labels=labels),
        ],
    )


def _mesh(status, service=SERVICE):
    return [{"service": service, "status": status, "port": 8181,
             "cpu_percent": 5.0, "memory_mb": 64.0}]


def _envelope(name, kwargs, data, ok=True, error=None):
    target = (kwargs.get("service") or kwargs.get("trace_id")
              or kwargs.get("query") or "")
    return {"tool": name, "target": str(target), "ok": ok,
            "data": data if ok else None, "error": error}


def _replace_tool(monkeypatch, name, run):
    """Swap a registry entry. ToolSpec is frozen, so mutate the dict."""
    monkeypatch.setitem(
        TOOL_REGISTRY, name, ToolSpec(name, get_tool(name).description, run)
    )


@pytest.fixture(scope="module")
def agent():
    # One agent per module: __init__ builds the retriever, which is the
    # expensive part and holds no per-test state.
    return AutonomousSREAgent()


@pytest.fixture
def scripted(monkeypatch):
    """Replace call_tool with one driven by a dict of scripted responses.

    Values are either an envelope or a sync callable ``(name, **kwargs) ->
    envelope``. The default answers every tool successfully with an empty
    payload, so a test only has to state what it cares about.
    """
    script = {}

    async def _call(name, **kwargs):
        if name in script:
            entry = script[name]
            return entry(name, **kwargs) if callable(entry) else entry
        return _envelope(name, kwargs, {"result": []})

    monkeypatch.setattr(agent_module, "call_tool", _call)
    return script


def _observation(tool, ok=True, data=None, error=None, verdict="healthy"):
    return {"tool": tool, "target": "", "ok": ok, "data": data,
            "error": error, "verdict": verdict}


def _run(coro):
    """Run an async agent call without needing an asyncio pytest plugin."""
    return asyncio.run(coro)


def _plan_complete_evidence():
    """Four observations, as a healthy investigation produces."""
    return [
        _observation("mesh_status", data=_mesh("HEALTHY")),
        _observation("promql"),
        _observation("logql"),
        _observation("k8s_api"),
    ]


def _dashboard_signal():
    """The exact value streamlit_app.py's T4 row reads.

    The dashboard does not touch the counters directly — it reads
    ``investigation_efficiency.avg_tool_calls_per_rca`` out of the scorecard
    API. Going through that path means these tests fail if the scorecard
    stops exposing the number, not only if the counters change.
    ``.get(..., 0)`` mirrors the dashboard's own default.
    """
    from app.api.benchmarks import get_evaluation_scorecard

    scorecard = get_evaluation_scorecard()
    efficiency = scorecard.get("investigation_efficiency", {})
    return efficiency.get("avg_tool_calls_per_rca", 0)


# --------------------------------------------------------------------------
# the registry
# --------------------------------------------------------------------------

def test_every_capability_is_registered_by_name():
    assert tool_names() == ["mesh_status", "k8s_api", "promql", "logql", "traceql"]
    assert set(TOOL_REGISTRY) == set(tool_names())


def test_list_tools_describes_each_one():
    described = list_tools()
    assert len(described) == len(TOOL_REGISTRY)
    for entry in described:
        assert entry["name"]
        assert len(entry["description"]) > 20, "a bare name is not a description"


def test_get_tool_reports_the_valid_names_on_a_typo():
    with pytest.raises(ValueError) as excinfo:
        get_tool("promethues")            # deliberate misspelling
    message = str(excinfo.value)
    assert "promql" in message
    assert "mesh_status" in message


def test_registered_tools_are_callable_objects():
    for name in tool_names():
        assert callable(get_tool(name).run)


# --------------------------------------------------------------------------
# the honesty contract
# --------------------------------------------------------------------------

def test_a_successful_call_returns_a_complete_envelope(monkeypatch):
    async def _run_tool(**kwargs):
        return _envelope("k8s_api", kwargs, {"pods": []})

    _replace_tool(monkeypatch, "k8s_api", _run_tool)
    result = asyncio.run(call_tool("k8s_api", service=SERVICE))

    assert set(result) == {"tool", "target", "ok", "data", "error"}
    assert result["ok"] is True
    assert result["tool"] == "k8s_api"
    assert result["target"] == SERVICE
    assert result["error"] is None


def test_mock_payload_is_dropped_when_the_backend_is_down(monkeypatch):
    """THE contract.

    The telemetry layer reports failure by returning an `error` beside a
    `mock_data` payload. That payload must never reach the agent as though
    it were an observation — the agent uses `ok` both to choose its next
    step and to price its confidence, so a substitute reading would inflate
    both.
    """
    def _down(**kwargs):
        return {"error": "prometheus unreachable at :9091",
                "mock_data": {"result": [{"value": [0, "0.99"]}]}}   # lie

    _replace_tool(monkeypatch, "promql", _down)
    result = asyncio.run(call_tool("promql", query="rate(x[1m])"))

    assert result["ok"] is False
    assert result["data"] is None, "the fabricated reading must not survive"
    assert "unreachable" in result["error"]
    assert "0.99" not in str(result)


def test_a_tool_that_explodes_does_not_take_down_the_call(monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("connection reset")

    _replace_tool(monkeypatch, "logql", _boom)
    result = asyncio.run(call_tool("logql", query="{app='x'}"))

    assert result["ok"] is False
    assert result["error"].startswith("RuntimeError:")
    assert "connection reset" in result["error"]
    assert result["data"] is None


def test_none_from_a_tool_is_reported_as_no_data(monkeypatch):
    _replace_tool(monkeypatch, "traceql", lambda **kwargs: None)
    result = asyncio.run(call_tool("traceql", trace_id="abc"))

    assert result["ok"] is False
    assert result["error"] == "tool returned no data"


def test_calling_a_tool_that_is_not_registered_is_a_programming_error():
    """call_tool never raises for a *registered* tool; an unknown name is a
    different failure and deserves a readable message, not a swallowed one."""
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(call_tool("does_not_exist"))
    assert "does_not_exist" in str(excinfo.value)


def test_mesh_status_counts_itself_in_the_ledger(monkeypatch):
    """The mesh reader has no TelemetryTools wrapper, so it counts its own
    calls — and must therefore appear in the ledger."""
    import app.mesh.manager as mesh_manager
    from app.tools.counters import get_tool_call_ledger

    class _FakeManager:
        def get_mesh_status(self):
            return _mesh("HEALTHY")

    monkeypatch.setattr(mesh_manager, "get_mesh_manager", lambda: _FakeManager())
    before = get_tool_call_ledger()["mesh_status"]

    asyncio.run(call_tool("mesh_status", service=SERVICE))

    assert get_tool_call_ledger()["mesh_status"] == before + 1


# --------------------------------------------------------------------------
# THINK — what comes next
# --------------------------------------------------------------------------

def test_the_service_itself_is_always_checked_first(agent):
    action = agent._next_action(_incident(), [])
    assert action[0] == "mesh_status"


def test_a_healthy_service_goes_to_promql_before_kubernetes(agent):
    evidence = [_observation("mesh_status", data=_mesh("HEALTHY"))]
    action = agent._next_action(_incident(), evidence)
    assert action[0] == "promql", "observations must choose the order"


def test_a_degraded_service_goes_to_kubernetes_before_promql(agent):
    evidence = [_observation("mesh_status", data=_mesh("CRASHED"),
                             verdict="degraded")]
    action = agent._next_action(_incident(), evidence)
    assert action[0] == "k8s_api"


def test_each_thought_explains_the_choice(agent):
    evidence = [_observation("mesh_status", data=_mesh("CRASHED"))]
    _, _, thought = agent._next_action(_incident(), evidence)
    assert isinstance(thought, str) and len(thought) > 15


def test_traceql_runs_when_a_trace_id_exists(agent):
    action = agent._next_action(_incident(trace_id="abc123"),
                                _plan_complete_evidence())
    assert action is not None
    assert action[0] == "traceql"
    assert action[1]["trace_id"] == "abc123"


def test_the_plan_exhausts_without_a_trace_id(agent):
    """Regression — the original crashed HERE.

    With every other tool already used, the traceql branch ran and read
    ``incident.labels``, a field CorrelatedIncident does not define. The
    AttributeError escaped ``investigate_incident`` and killed every
    investigation at its fifth step.
    """
    assert agent._next_action(_incident(), _plan_complete_evidence()) is None


def test_trace_id_is_read_from_the_alert_labels_not_the_incident():
    """Labels live on RawAlert; CorrelatedIncident has none of its own."""
    assert AutonomousSREAgent._trace_id(_incident(trace_id="abc123")) == "abc123"
    assert AutonomousSREAgent._trace_id(_incident()) is None


def test_trace_id_also_reads_an_incident_level_labels_field():
    """Belt and braces: if a correlation path ever attaches labels to the
    incident, that location must work too rather than raising."""
    incident = _incident()
    incident.__dict__["labels"] = {"trace_id": "from-incident"}
    assert AutonomousSREAgent._trace_id(incident) == "from-incident"


def test_the_step_and_evidence_limits_are_sane():
    assert MAX_STEPS >= 2
    assert MIN_EVIDENCE >= 2


# --------------------------------------------------------------------------
# OBSERVE — what an envelope means
# --------------------------------------------------------------------------

def test_a_failed_tool_has_no_opinion(agent):
    obs = _observation("promql", ok=False, error="down", verdict="unknown")
    assert agent._verdict("promql", obs, _incident()) == "unknown"


def test_mesh_verdict_follows_the_service_status(agent):
    assert agent._verdict(
        "mesh_status", _observation("mesh_status", data=_mesh("HEALTHY")),
        _incident()) == "healthy"
    assert agent._verdict(
        "mesh_status", _observation("mesh_status", data=_mesh("CRASHED")),
        _incident()) == "degraded"
    # A service the mesh does not report at all is unknown, not healthy.
    assert agent._verdict(
        "mesh_status",
        _observation("mesh_status",
                     data=_mesh("HEALTHY", service="other-service")),
        _incident()) == "unknown"


def test_promql_verdict_counts_a_nonzero_error_rate(agent):
    busy = _observation("promql", data={"result": [{"value": [0, "0.42"]}]})
    idle = _observation("promql", data={"result": [{"value": [0, "0"]}]})
    assert agent._verdict("promql", busy, _incident()) == "degraded"
    assert agent._verdict("promql", idle, _incident()) == "healthy"


def test_logql_verdict_scans_for_error_markers(agent):
    noisy = _observation("logql", data={"result": [
        {"values": [[0, "cart-service ERROR: timeout exceeded"]]}]})
    quiet = _observation("logql", data={"result": [
        {"values": [[0, "cart-service GET /cart 200 4ms"]]}]})
    assert agent._verdict("logql", noisy, _incident()) == "degraded"
    assert agent._verdict("logql", quiet, _incident()) == "healthy"


def test_k8s_verdict_counts_non_running_pods(agent):
    down = _observation("k8s_api", data={
        "pods": [{"phase": "CrashLoopBackOff", "restart_count": 3}]})
    up = _observation("k8s_api", data={
        "pods": [{"phase": "Running", "restart_count": 0}]})
    nothing = _observation("k8s_api", data={"pods": []})
    assert agent._verdict("k8s_api", down, _incident()) == "degraded"
    assert agent._verdict("k8s_api", up, _incident()) == "healthy"
    assert agent._verdict("k8s_api", nothing, _incident()) == "unknown"


# --------------------------------------------------------------------------
# confidence — no invented numbers
# --------------------------------------------------------------------------

def test_no_runbook_means_no_retrieval_confidence(agent):
    """The previous implementation substituted 0.88 here."""
    confidence, retrieval, agreement = agent._confidence(
        [], [_observation("promql", verdict="degraded")]
    )
    assert retrieval == 0.0
    assert confidence == pytest.approx(0.5 * 0.0 + 0.5 * agreement)


def test_a_tool_that_could_not_answer_does_not_corroborate(agent):
    evidence = [
        _observation("promql", ok=False, verdict="unknown"),
        _observation("logql", ok=False, verdict="unknown"),
    ]
    _, _, agreement = agent._confidence([], evidence)
    assert agreement == 0.0


def test_confidence_is_the_documented_blend_and_stays_in_range(agent):
    runbooks = [{"similarity_score": 0.8}]
    evidence = [_observation("promql", verdict="degraded"),
                _observation("logql", verdict="degraded")]
    confidence, retrieval, agreement = agent._confidence(runbooks, evidence)

    assert retrieval == pytest.approx(0.8)
    assert agreement == pytest.approx(1.0)
    assert confidence == pytest.approx(0.5 * 0.8 + 0.5 * 1.0, abs=1e-4)
    assert 0.0 <= confidence <= 1.0


def test_retrieval_scores_out_of_range_are_bounded(agent):
    confidence, retrieval, _ = agent._confidence([{"similarity_score": 42.0}], [])
    assert retrieval == 1.0
    assert 0.0 <= confidence <= 1.0


# --------------------------------------------------------------------------
# the loop, end to end
# --------------------------------------------------------------------------

def test_an_investigation_completes_without_any_live_backend(agent, scripted):
    out = _run(agent.investigate_incident(_incident()))

    # keys read by the API contract and by chaos.py
    assert "root_cause_analysis" in out
    assert "investigation_duration_seconds" in out

    rca = out["root_cause_analysis"]
    for key in ("diagnosis", "confidence_score", "referenced_runbooks",
                "recommended_remediation", "citations",
                "confidence_breakdown", "remediation_blocked"):
        assert key in rca, f"contract key {key!r} missing"


def test_the_react_trace_records_each_thought_and_verdict(agent, scripted):
    steps = _run(agent.investigate_incident(_incident()))["react"]["steps"]

    assert 2 <= len(steps) <= MAX_STEPS
    for step in steps:
        assert set(step) >= {"step", "thought", "tool", "target", "ok",
                             "verdict", "observation"}
        assert step["thought"], "a ReAct step must show its reasoning"
        assert step["verdict"] in {"healthy", "degraded", "unknown"}


def test_the_loop_gathers_at_least_the_minimum_evidence(agent, scripted):
    react = _run(agent.investigate_incident(_incident()))["react"]

    assert react["evidence_entries"] >= MIN_EVIDENCE
    assert react["sufficient_evidence"] is True
    assert react["tools_available"] == tool_names()


def test_every_tool_failing_drives_confidence_down(agent, scripted):
    """The point of pricing confidence in evidence: an agent that could not
    observe anything must not sound confident."""
    for name in tool_names():
        scripted[name] = lambda n, **kw: _envelope(
            n, kw, None, ok=False, error="backend down")

    out = _run(agent.investigate_incident(_incident()))
    rca = out["root_cause_analysis"]

    assert rca["confidence_breakdown"]["tool_agreement"] == 0.0
    # Retrieval is the only term left, so confidence cannot exceed half.
    assert rca["confidence_score"] <= 0.5 + 1e-9
    assert all(e["ok"] is False for e in rca["evidence_gathered"])


def test_healthy_observations_are_recorded_as_such(agent, scripted):
    scripted["mesh_status"] = lambda n, **kw: _envelope(n, kw, _mesh("HEALTHY"))

    out = _run(agent.investigate_incident(_incident()))
    assert "healthy" in [s["verdict"] for s in out["react"]["steps"]]


def test_a_crashed_service_is_investigated_through_kubernetes_first(agent, scripted):
    scripted["mesh_status"] = lambda n, **kw: _envelope(n, kw, _mesh("CRASHED"))

    out = _run(agent.investigate_incident(_incident()))
    tools = [s["tool"] for s in out["react"]["steps"]]

    assert tools[0] == "mesh_status"
    assert tools[1] == "k8s_api", "the observation must choose the order"


def test_investigation_duration_is_reported(agent, scripted):
    out = _run(agent.investigate_incident(_incident()))
    assert isinstance(out["investigation_duration_seconds"], float)
    assert out["investigation_duration_seconds"] >= 0.0


# --------------------------------------------------------------------------
# the dashboard seam (T6's file, T4's one-token change)
# --------------------------------------------------------------------------

def test_the_readiness_signal_demands_more_than_one_tool_call():
    """Guards streamlit_app.py's T4 row.

    ``> 0`` was already true before this track existed — the old linear
    agent made exactly one tool call per investigation — so the signal went
    green while T4 sat unmerged and untested. ``> 1`` can only be satisfied
    by the ReAct loop.
    """
    source = (REPO_ROOT / "streamlit_app.py").read_text()
    index = source.index('"T4 Agent tools"')
    clause = source[index:index + 400]

    match = re.search(r'avg_tool_calls_per_rca",\s*0\)\s*>\s*(\d+)', clause)
    assert match, "could not find the T4 readiness expression"

    threshold = int(match.group(1))
    assert threshold >= 1, (
        f"T4 signal reads `> {threshold}`, which the pre-T4 agent already "
        f"satisfied; it must demand more than one tool call per RCA"
    )


def test_a_single_tool_rca_would_not_light_the_signal():
    from app.tools.counters import record_investigation, reset_counters

    reset_counters()
    record_investigation(["promql"])          # what the old agent did

    average = _dashboard_signal()
    assert average == 1.0
    assert not (average > 1), "the old behaviour must not pass the signal"


def test_the_react_loop_lights_the_signal(agent, scripted):
    from app.tools.counters import reset_counters

    reset_counters()
    _run(agent.investigate_incident(_incident()))

    average = _dashboard_signal()
    assert average > 1, "T4's own gate: more than one tool call per RCA"


def test_mesh_status_is_declared_in_the_ledger():
    from app.tools.counters import get_tool_call_ledger, reset_counters

    reset_counters()
    ledger = get_tool_call_ledger()
    assert "mesh_status" in ledger, (
        "the mesh reader counts its own calls but must also appear in the "
        "ledger, or the dashboard cannot show it"
    )
    assert ledger["mesh_status"] == 0
