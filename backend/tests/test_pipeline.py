"""Phase 2 — the end-to-end pipeline: anomaly → agent → LLM → gate → remediation.

What is under test is the WIRING: that each stage actually invokes the next
and that the 0.65 gate means something. The individual stages keep their own
suites (T5 scores, T4 reasons, T3 generates) — here they must compose.

The model seam is conftest's offline default (UNREACHABLE) unless a test
replaces it, telemetry answers through patched scoring, and the mesh is
bombed when it must NOT be called — a pipeline that heals when it promised
not to must fail loudly, not quietly.
"""

import pytest

from app.agent.sre_agent import AutonomousSREAgent
from app.api.incidents import platform_settings
from app.api.llm import RCAResponse
from app.correlation.engine import get_correlation_engine


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
SPIKE_SAMPLE = {
    "timestamp": 1791320900.0,
    "service": "payment-service",
    "features": {
        "cpu_percent": 97.5, "memory_mb": 412.0, "latency_s": 1.4,
        "error_rate": 0.62, "requests_total": 310.0, "service_up": 0.0,
    },
}

CANNED_NARRATIVE = (
    "The payment-service latency matches the network latency runbook; "
    "promql evidence shows the spike began at injection time."
)
CANNED_DIAGNOSIS = "Runbook: Network Latency & Inter-Service Packet Loss"


@pytest.fixture
def clean_engine():
    """Give a pipeline test an empty correlation world, then put it back.

    Pipeline tests mint real incidents in the global singleton. They also
    must not MERGE into a leftover same-tenant incident from an earlier
    test file (anomaly incidents are tenant_b and the window is 120 s) —
    which would make every assertion here depend on test order.
    """
    engine = get_correlation_engine()
    saved = dict(engine.active_incidents)
    engine.active_incidents.clear()
    yield engine
    engine.active_incidents.clear()
    engine.active_incidents.update(saved)


@pytest.fixture
def scored_anomalies(monkeypatch):
    """Make POST /anomalies/score report one spike, without training a model.

    The handler imports these three at call time, so patching the module
    attributes is enough — and nothing writes to the real feature store.
    """
    from app.ml import scoring

    monkeypatch.setattr(scoring, "score", lambda _features: ([0.91], [True]))
    monkeypatch.setattr(scoring, "append_anomalies", lambda _rows: None)
    monkeypatch.setattr(scoring, "model_status", lambda: {"loaded": True})

    def _post(test_client):
        res = test_client.post("/api/v1/anomalies/score", json={"samples": [SPIKE_SAMPLE]})
        assert res.status_code == 200, res.text
        assert res.json()["anomaly_count"] == 1
        incidents = test_client.get("/api/v1/incidents").json()
        mine = [
            e for e in incidents
            if e["incident"]["root_cause_candidate"] == "AnomalyDetected"
        ]
        assert mine, "the detection must have raised an incident"
        return mine[0]["incident"]["incident_id"]

    return _post


def _canned_llm(_request):
    return RCAResponse(
        status="OK",
        diagnosis=CANNED_DIAGNOSIS,
        narrative=CANNED_NARRATIVE,
        citations=["Runbook: Network Latency & Inter-Service Packet Loss "
                   "(network_latency.md)"],
    )


class _MeshRecorder:
    """Fake mesh manager: records the call, returns a healthy result."""

    def __init__(self):
        self.calls = []

    def remediate_service(self, service):
        self.calls.append(service)
        return {"service": service, "message": f"{service} restarted", "status": "HEALTHY"}


class _MeshBomb:
    def remediate_service(self, _service):
        raise AssertionError("the mesh must NOT be remediated while the gate blocks")


def _force_confidence(monkeypatch, value):
    """Pin the agent's blended confidence (formula itself is T4's suite)."""
    monkeypatch.setattr(
        AutonomousSREAgent, "_confidence",
        staticmethod(lambda *_a, **_k: (value, value, value)),
    )


# --------------------------------------------------------------------------
# Stage 1: anomaly -> incident
# --------------------------------------------------------------------------
def test_a_detection_raises_a_clustered_incident(clean_engine, scored_anomalies,
                                                 test_client, monkeypatch):
    inc_id = scored_anomalies(test_client)
    incident = clean_engine.get_incident(inc_id)

    assert incident.root_cause_candidate == "AnomalyDetected"
    assert incident.primary_service == "payment-service"
    assert incident.status == "OPEN"
    assert len(incident.alert_ids) == 1
    assert incident.sample_alerts[0].labels["source"] == "anomaly-detector"

    # A second detection inside the window clusters, and the honesty
    # invariant total_alerts == len(alert_ids) survives (ns-unique ids).
    first = clean_engine.correlate_anomaly("payment-service", 0.93)
    second = clean_engine.correlate_anomaly("payment-service", 0.95)
    assert first.incident_id == second.incident_id == inc_id
    assert first.total_alerts == len(first.alert_ids) == 3


def test_score_without_a_detection_raises_nothing(clean_engine, test_client,
                                                  monkeypatch):
    """Normal telemetry must not mint incidents — only detections do."""
    from app.ml import scoring

    monkeypatch.setattr(scoring, "score", lambda _features: ([0.01], [False]))
    monkeypatch.setattr(scoring, "append_anomalies", lambda _rows: None)
    monkeypatch.setattr(scoring, "model_status", lambda: {"loaded": True})
    before = dict(clean_engine.active_incidents)

    res = test_client.post("/api/v1/anomalies/score", json={"samples": [SPIKE_SAMPLE]})

    assert res.status_code == 200
    assert res.json()["anomaly_count"] == 0
    assert clean_engine.active_incidents == before


# --------------------------------------------------------------------------
# Stages 2 + 3: agent -> LLM, narrated into the response
# --------------------------------------------------------------------------
def test_diagnose_carries_the_llm_narrative_when_the_model_answers(
        clean_engine, test_client, monkeypatch):
    incident = clean_engine.correlate_anomaly("payment-service", 0.91)
    monkeypatch.setattr("app.agent.sre_agent._generate_rca", _canned_llm)

    res = test_client.post(f"/api/v1/incidents/{incident.incident_id}/diagnose")

    assert res.status_code == 200
    rca = res.json()["root_cause_analysis"]
    assert rca["narrative"] == CANNED_NARRATIVE
    assert rca["diagnosis"] == CANNED_DIAGNOSIS
    assert rca["llm"]["status"] == "OK"
    assert rca["llm"]["model"]                  # which model wrote it
    # The agent's own blend stays the gate's number — not the LLM's.
    assert isinstance(rca["confidence_score"], float)
    assert len(rca["evidence_gathered"]) >= 2   # ReAct really ran


def test_diagnose_degrades_honestly_when_the_model_is_down(
        clean_engine, test_client):
    incident = clean_engine.correlate_anomaly("payment-service", 0.91)
    # No patch: conftest's offline default (UNREACHABLE) is the point — this
    # is exactly what a machine without Ollama sees.

    res = test_client.post(f"/api/v1/incidents/{incident.incident_id}/diagnose")

    assert res.status_code == 200
    rca = res.json()["root_cause_analysis"]
    assert rca["llm"]["status"] == "UNREACHABLE"
    assert rca["narrative"] is None             # no narrative is invented
    assert rca["diagnosis"]                     # but the runbook fallback stands
    assert rca["confidence_score"] >= 0         # the gate still computed


# --------------------------------------------------------------------------
# Stage 4: the confidence gate decides whether stage 5 may happen
# --------------------------------------------------------------------------
def test_low_confidence_never_auto_heals(clean_engine, test_client, monkeypatch):
    incident = clean_engine.correlate_anomaly("payment-service", 0.91)
    monkeypatch.setattr(platform_settings, "autonomous_mode", True)
    _force_confidence(monkeypatch, 0.3)
    monkeypatch.setattr("app.api.incidents.get_mesh_manager", _MeshBomb)
    monkeypatch.setattr("app.agent.sre_agent._generate_rca", _canned_llm)

    res = test_client.post(f"/api/v1/incidents/{incident.incident_id}/diagnose")

    assert res.status_code == 200
    body = res.json()
    approval = body["approval_state"]
    assert approval["status"] == "REMEDIATION_BLOCKED"
    assert "mesh_result" not in approval        # nothing was executed
    assert "0.300" in approval["message"]       # the number that blocked it
    assert "0.65" in approval["message"]        # the threshold that blocked it
    assert body["root_cause_analysis"]["remediation_blocked"] is True

    state = test_client.get(f"/api/v1/incidents/{incident.incident_id}").json()
    assert state["incident"]["status"] == "INVESTIGATING"   # never RESOLVED
    # (_MeshBomb would have raised if the mesh had been called)


def test_confident_incident_auto_heals_in_autonomous_mode(
        clean_engine, test_client, monkeypatch):
    incident = clean_engine.correlate_anomaly("payment-service", 0.91)
    monkeypatch.setattr(platform_settings, "autonomous_mode", True)
    _force_confidence(monkeypatch, 0.9)
    mesh = _MeshRecorder()
    monkeypatch.setattr(
        "app.api.incidents.get_mesh_manager", lambda: mesh
    )
    monkeypatch.setattr("app.agent.sre_agent._generate_rca", _canned_llm)

    res = test_client.post(f"/api/v1/incidents/{incident.incident_id}/diagnose")

    assert res.status_code == 200
    body = res.json()
    approval = body["approval_state"]
    assert approval["status"] == "AUTO_EXECUTED"
    assert mesh.calls == ["payment-service"]    # exactly one heal, right target

    state = test_client.get(f"/api/v1/incidents/{incident.incident_id}").json()
    assert state["incident"]["status"] == "RESOLVED"


def test_human_approval_remains_the_operator_override(clean_engine, test_client,
                                                      monkeypatch):
    """The gate governs AUTO-healing (plan step 3 says "auto-heal"); an
    operator who approves explicitly is the safety valve and stays free."""
    incident = clean_engine.correlate_anomaly("payment-service", 0.91)
    monkeypatch.setattr(platform_settings, "autonomous_mode", False)
    _force_confidence(monkeypatch, 0.3)
    monkeypatch.setattr("app.agent.sre_agent._generate_rca", _canned_llm)

    res = test_client.post(f"/api/v1/incidents/{incident.incident_id}/diagnose")

    # Human mode pauses at the gate instead of blocking the operator.
    assert res.json()["approval_state"]["status"] == "AWAITING_APPROVAL"
    assert res.json()["root_cause_analysis"]["remediation_blocked"] is True


# --------------------------------------------------------------------------
# The headline: one incident through ALL stages
# --------------------------------------------------------------------------
def test_one_incident_flows_through_all_stages(clean_engine, scored_anomalies,
                                               test_client, monkeypatch):
    # Stage 1 — the detector raises the incident.
    inc_id = scored_anomalies(test_client)

    # Stages 2-5 are armed: the model answers, confidence clears the gate,
    # and the mesh records what happens next.
    monkeypatch.setattr("app.agent.sre_agent._generate_rca", _canned_llm)
    _force_confidence(monkeypatch, 0.9)
    mesh = _MeshRecorder()
    monkeypatch.setattr("app.api.incidents.get_mesh_manager", lambda: mesh)
    monkeypatch.setattr(platform_settings, "autonomous_mode", True)

    # One POST walks the incident through agent -> LLM -> gate -> remediation.
    res = test_client.post(f"/api/v1/incidents/{inc_id}/diagnose")

    assert res.status_code == 200
    body = res.json()
    rca = body["root_cause_analysis"]

    # agent: real evidence was gathered
    assert len(rca["evidence_gathered"]) >= 2
    assert rca["citations"]
    # LLM: a narrative was generated and attributed
    assert rca["narrative"] == CANNED_NARRATIVE
    assert rca["llm"]["status"] == "OK"
    # gate: confidence cleared 0.65, so remediation was allowed
    assert rca["confidence_score"] == 0.9
    assert rca["remediation_blocked"] is False
    # remediation: exactly one heal of exactly the affected service
    assert body["approval_state"]["status"] == "AUTO_EXECUTED"
    assert mesh.calls == ["payment-service"]
    # and the incident closed the loop
    state = test_client.get(f"/api/v1/incidents/{inc_id}").json()
    assert state["incident"]["status"] == "RESOLVED"
    assert body["investigation_duration_seconds"] >= 0
