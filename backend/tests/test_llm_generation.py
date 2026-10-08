"""Track T3 — local LLM + RAG generation.

The suite must never need a running model or network (T3 step 5), so every
test replaces the two module seams in app.api.llm:

    _probe_ollama()  -> reachability / model presence
    _call_ollama()   -> one generation

Retrieval is stubbed everywhere except test_rca_cites_runbooks_retrieved_
from_the_index, which exercises the real Chroma wiring end to end.
"""
import json

import pytest

from app.api import llm as llm_mod
from app.config import settings

GOOD_RCA_REPLY = json.dumps({
    "diagnosis": "Runbook: Network Latency Between Services",
    "narrative": (
        "P99 latency on cart-service matches the Network Latency runbook; "
        "promql evidence shows the spike began at injection time."
    ),
})

GOOD_POSTMORTEM_REPLY = json.dumps({
    "summary": "The injected NetworkChaos delay was diagnosed as network latency.",
    "root_cause": "300ms RTT delay injected between cart and payment services.",
    "impact": "Checkout requests exceeded the 504 threshold for 60 seconds.",
    "detection": "The Prometheus latency alert fired shortly after injection.",
    "remediation": "Chaos Mesh recovered the experiment after its duration.",
    "prevention": "Keep the confidence gate blocking low-evidence remediation.",
})

CANNED_RUNBOOKS = [
    {
        "id": "runbook_network_latency",
        "title": "Network Latency",
        "category": "network",
        "filename": "network_latency.md",
        "content": "# Network Latency\nInvestigate P99 and cross-service RTT.",
        "similarity_score": 0.9,
    },
]


def _stub_retrieve(runbooks=None, error=None):
    """Replacement for llm_mod._retrieve with a fixed result."""
    fixed = CANNED_RUNBOOKS if runbooks is None else runbooks

    def retrieve(query, n_results=3):
        return list(fixed), error

    return retrieve


def _post_rca(test_client, **overrides):
    payload = {
        "incident_id": "inc_t3",
        "primary_service": "cart-service",
        "symptom": "HTTP 504 gateway timeout",
    }
    payload.update(overrides)
    return test_client.post("/api/v1/llm/rca", json=payload).json()


def _post_postmortem(test_client, **overrides):
    payload = {
        "incident_id": "inc_t3_pm",
        "ground_truth_cause": "NetworkChaos: 300ms delay",
        "ai_diagnosis": "Runbook: Network Latency",
        "timeline": ["t+0 injected", "t+10 alert fired"],
    }
    payload.update(overrides)
    return test_client.post("/api/v1/llm/postmortem", json=payload).json()


# ---------------------------------------------------------------------------
# /llm/health — the probe must not lie in either direction
# ---------------------------------------------------------------------------
def test_health_is_ready_when_server_and_model_answer(test_client, monkeypatch):
    monkeypatch.setattr(
        llm_mod, "_probe_ollama",
        lambda: {"reachable": True, "model_loaded": True},
    )

    body = test_client.get("/api/v1/llm/health").json()

    assert body["reachable"] is True
    assert body["status"] == "READY"
    assert body["model"] and body["host"]


def test_health_reports_unreachable_server(test_client, monkeypatch):
    monkeypatch.setattr(
        llm_mod, "_probe_ollama",
        lambda: {"reachable": False, "model_loaded": False},
    )

    body = test_client.get("/api/v1/llm/health").json()

    assert body["reachable"] is False
    assert body["status"] == "UNREACHABLE"


def test_health_reports_missing_model_not_a_false_ready(test_client, monkeypatch):
    """A server with the model never pulled answers /api/tags fine but
    would 404 every generation — READY there would be a false green."""
    monkeypatch.setattr(
        llm_mod, "_probe_ollama",
        lambda: {"reachable": True, "model_loaded": False},
    )

    body = test_client.get("/api/v1/llm/health").json()

    assert body["reachable"] is True
    assert body["status"] == "MODEL_MISSING"


def test_disabled_llm_never_calls_the_model(test_client, monkeypatch):
    monkeypatch.setattr(
        llm_mod, "settings",
        llm_mod.settings.model_copy(update={"LLM_ENABLED": False}),
    )
    monkeypatch.setattr(llm_mod, "_retrieve", _stub_retrieve())

    def _bomb(system, user):
        raise AssertionError("the model must not be called while LLM_ENABLED=false")

    monkeypatch.setattr(llm_mod, "_call_ollama", _bomb)

    health = test_client.get("/api/v1/llm/health").json()
    assert health["status"] == "DISABLED"
    assert health["enabled"] is False
    assert health["reachable"] is None  # not probed, not guessed

    rca = _post_rca(test_client, retrieval_confidence=0.9)
    assert rca["status"] == "DISABLED"
    assert rca["diagnosis"] is None
    # The gate stays confidence-only even when disabled.
    assert rca["remediation_blocked"] is False

    postmortem = _post_postmortem(test_client)
    assert postmortem["status"] == "DISABLED"
    assert postmortem["sections"] == {}


# ---------------------------------------------------------------------------
# /llm/rca — generation, grounding, and the 0.65 gate
# ---------------------------------------------------------------------------
def test_rca_generates_diagnosis_narrative_and_citations(test_client, monkeypatch):
    monkeypatch.setattr(llm_mod, "_retrieve", _stub_retrieve())
    captured = {}

    def _call(system, user):
        captured["system"], captured["user"] = system, user
        return GOOD_RCA_REPLY

    monkeypatch.setattr(llm_mod, "_call_ollama", _call)

    body = _post_rca(
        test_client,
        severity="critical",
        evidence=[{"tool": "promql", "observation": "p99=1400ms",
                   "verdict": "degraded"}],
        retrieval_confidence=0.9,
    )

    assert body["status"] == "OK"
    assert body["diagnosis"] == "Runbook: Network Latency Between Services"
    assert body["narrative"]
    assert body["confidence"] == 0.9
    assert body["citations"] == ["Network Latency (network_latency.md)"]
    assert body["remediation_blocked"] is False

    # T3 step 2 wiring: symptom, evidence and retrieved runbooks all reach
    # the prompt the model actually received.
    assert "HTTP 504 gateway timeout" in captured["user"]
    assert "- promql: p99=1400ms (verdict: degraded)" in captured["user"]
    assert "Network Latency" in captured["user"]
    assert "JSON object" in captured["system"]


def test_rca_gate_blocks_below_threshold_but_still_generates(
        test_client, monkeypatch):
    monkeypatch.setattr(llm_mod, "_retrieve", _stub_retrieve())
    monkeypatch.setattr(llm_mod, "_call_ollama", lambda system, user: GOOD_RCA_REPLY)

    body = _post_rca(test_client, retrieval_confidence=0.50)

    assert body["remediation_blocked"] is True
    # Below the gate blocks automation, not the human reading the draft.
    assert body["status"] == "OK"
    assert body["narrative"]


def test_rca_gate_boundary_is_exactly_the_setting(test_client, monkeypatch):
    monkeypatch.setattr(llm_mod, "_retrieve", _stub_retrieve())
    monkeypatch.setattr(llm_mod, "_call_ollama", lambda system, user: GOOD_RCA_REPLY)

    below = _post_rca(
        test_client, retrieval_confidence=settings.RCA_MIN_CONFIDENCE - 0.01
    )
    at_gate = _post_rca(
        test_client, retrieval_confidence=settings.RCA_MIN_CONFIDENCE
    )

    assert below["remediation_blocked"] is True
    assert at_gate["remediation_blocked"] is False


def test_rca_computes_confidence_from_retrieval_when_caller_is_silent(
        test_client, monkeypatch):
    monkeypatch.setattr(llm_mod, "_call_ollama", lambda system, user: GOOD_RCA_REPLY)

    strong = [{**CANNED_RUNBOOKS[0], "similarity_score": 0.9}]
    monkeypatch.setattr(llm_mod, "_retrieve", _stub_retrieve(strong))
    confident = _post_rca(test_client)

    assert confident["confidence"] == pytest.approx(0.9)
    assert confident["remediation_blocked"] is False

    weak = [{**CANNED_RUNBOOKS[0], "similarity_score": 0.2}]
    monkeypatch.setattr(llm_mod, "_retrieve", _stub_retrieve(weak))
    unconfident = _post_rca(test_client)

    assert unconfident["confidence"] == pytest.approx(0.2)
    assert unconfident["remediation_blocked"] is True


def test_rca_blocks_when_nothing_was_retrieved(test_client, monkeypatch):
    """The stub gate was `0 < conf < min`, so 0.0 skipped it entirely. In a
    real pipeline 0.0 means "no grounding" — the case that must block
    hardest, not the one exempted from the check."""
    monkeypatch.setattr(llm_mod, "_retrieve", _stub_retrieve([]))
    monkeypatch.setattr(llm_mod, "_call_ollama", lambda system, user: GOOD_RCA_REPLY)

    body = _post_rca(test_client)

    assert body["confidence"] == 0.0
    assert body["remediation_blocked"] is True
    assert body["citations"] == []


def test_rca_reports_unreachable_without_fabricating_output(
        test_client, monkeypatch):
    monkeypatch.setattr(llm_mod, "_retrieve", _stub_retrieve())

    def _down(system, user):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(llm_mod, "_call_ollama", _down)
    monkeypatch.setattr(
        llm_mod, "_probe_ollama",
        lambda: {"reachable": False, "model_loaded": False},
    )

    body = _post_rca(test_client, retrieval_confidence=0.9)

    assert body["status"] == "UNREACHABLE"
    assert body["diagnosis"] is None
    assert body["narrative"] is None
    # A dead model must not flip the flag the scorecard reads as
    # "evidence too weak": the gate is confidence-only by contract.
    assert body["remediation_blocked"] is False


def test_rca_reports_generation_failure_when_server_answers(
        test_client, monkeypatch):
    monkeypatch.setattr(llm_mod, "_retrieve", _stub_retrieve())

    def _boom(system, user):
        raise RuntimeError("model crashed mid-generation")

    monkeypatch.setattr(llm_mod, "_call_ollama", _boom)
    monkeypatch.setattr(
        llm_mod, "_probe_ollama",
        lambda: {"reachable": True, "model_loaded": True},
    )

    body = _post_rca(test_client, retrieval_confidence=0.9)

    assert body["status"] == "GENERATION_FAILED"
    assert body["diagnosis"] is None


def test_rca_reports_missing_model(test_client, monkeypatch):
    monkeypatch.setattr(llm_mod, "_retrieve", _stub_retrieve())

    def _missing(system, user):
        raise RuntimeError("model 'llama3.2:3b' not found")

    monkeypatch.setattr(llm_mod, "_call_ollama", _missing)
    monkeypatch.setattr(
        llm_mod, "_probe_ollama",
        lambda: {"reachable": True, "model_loaded": False},
    )

    body = _post_rca(test_client, retrieval_confidence=0.9)

    assert body["status"] == "MODEL_MISSING"
    assert body["diagnosis"] is None


def test_rca_non_json_reply_keeps_prose_but_fails_the_contract(
        test_client, monkeypatch):
    """The model answered in prose: keep what it said (inspectable), but
    never report a diagnosis we did not parse."""
    monkeypatch.setattr(llm_mod, "_retrieve", _stub_retrieve())
    monkeypatch.setattr(
        llm_mod, "_call_ollama",
        lambda system, user: "The cart service is timing out on Redis.",
    )

    body = _post_rca(test_client, retrieval_confidence=0.9)

    assert body["status"] == "GENERATION_FAILED"
    assert body["diagnosis"] is None
    assert body["narrative"] == "The cart service is timing out on Redis."


def test_rca_survives_a_broken_vector_store_and_still_gates(
        test_client, monkeypatch):
    """Retrieval failing must degrade to 'no grounding', not to a 500 that
    hides the incident behind it."""
    def _broken():
        raise RuntimeError("vector store unavailable")

    monkeypatch.setattr("app.rag.retriever.get_runbook_retriever", _broken)
    monkeypatch.setattr(llm_mod, "_call_ollama", lambda system, user: GOOD_RCA_REPLY)

    res = test_client.post("/api/v1/llm/rca", json={
        "incident_id": "inc_broken_index",
        "primary_service": "cart-service",
    })

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "OK"
    assert body["citations"] == []
    assert body["confidence"] == 0.0
    assert body["remediation_blocked"] is True  # no grounding -> blocked


def test_rca_cites_runbooks_retrieved_from_the_index(test_client, monkeypatch):
    """The real RAG path: index the runbooks, let the endpoint query Chroma
    itself, and prove the retrieved text actually reaches the prompt."""
    from app.rag.indexer import index_all_runbooks

    assert index_all_runbooks(settings.RUNBOOKS_DIR) >= 1

    captured = {}

    def _call(system, user):
        captured["system"], captured["user"] = system, user
        return GOOD_RCA_REPLY

    # _retrieve deliberately NOT stubbed — this test is the RAG wiring.
    monkeypatch.setattr(llm_mod, "_call_ollama", _call)

    body = _post_rca(test_client, symptom="P99 latency spike after network fault")

    assert body["status"] == "OK"
    assert body["diagnosis"] == "Runbook: Network Latency Between Services"
    assert body["citations"], "retrieval must surface runbook citations"
    assert all(".md" in citation for citation in body["citations"])
    assert body["confidence"] is not None
    assert "Retrieved runbooks:" in captured["user"]
    assert ".md" in captured["user"]  # the runbook text reached the model


# ---------------------------------------------------------------------------
# /llm/postmortem — sections only when a model produced them
# ---------------------------------------------------------------------------
def test_postmortem_populates_all_sections(test_client, monkeypatch):
    captured = {}

    def _call(system, user):
        captured["system"], captured["user"] = system, user
        return GOOD_POSTMORTEM_REPLY

    monkeypatch.setattr(llm_mod, "_call_ollama", _call)

    body = _post_postmortem(test_client)

    assert body["status"] == "OK"
    assert set(body["sections"]) >= set(llm_mod.POSTMORTEM_SECTIONS)
    assert all(body["sections"].values())
    assert body["narrative"] == body["sections"]["summary"]

    # The prompt carries this experiment's truth, not a generic request.
    assert "NetworkChaos: 300ms delay" in captured["user"]
    assert "t+0 injected" in captured["user"]
    assert "blameless post-mortem" in captured["system"]


def test_postmortem_unreachable_returns_no_sections(test_client, monkeypatch):
    def _down(system, user):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(llm_mod, "_call_ollama", _down)
    monkeypatch.setattr(
        llm_mod, "_probe_ollama",
        lambda: {"reachable": False, "model_loaded": False},
    )

    body = _post_postmortem(test_client)

    assert body["status"] == "UNREACHABLE"
    assert body["sections"] == {}  # six plausible paragraphs nobody wrote
    assert body["narrative"] is None


def test_postmortem_rejects_reply_without_the_core_sections(
        test_client, monkeypatch):
    monkeypatch.setattr(
        llm_mod, "_call_ollama",
        lambda system, user: json.dumps({
            "summary": "only a summary",
            "impact": "some impact",
        }),
    )

    body = _post_postmortem(test_client)

    assert body["status"] == "GENERATION_FAILED"
    assert "root_cause" not in body["sections"]
    # What did come back is kept for inspection, honestly labelled.
    assert body["sections"]["summary"] == "only a summary"
    assert body["narrative"] == "only a summary"


# ---------------------------------------------------------------------------
# Parser / formatter units
# ---------------------------------------------------------------------------
def test_json_parsing_tolerates_code_fences():
    fenced = '```json\n{"diagnosis": "d", "narrative": "n"}\n```'

    diagnosis, narrative = llm_mod._parse_rca(fenced)

    assert (diagnosis, narrative) == ("d", "n")


def test_evidence_formatting_keeps_unknown_shapes_visible():
    known = llm_mod._fmt_evidence(
        {"tool": "promql", "observation": "p99=1400ms", "verdict": "degraded"}
    )
    assert known == "- promql: p99=1400ms (verdict: degraded)"

    # Unknown structure degrades to JSON — visible, never silent.
    unknown = llm_mod._fmt_evidence({"weird": {"nested": 1}})
    assert "weird" in unknown and "nested" in unknown

    assert llm_mod._fmt_evidence("plain string") == "- plain string"
