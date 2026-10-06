"""
Phase 0 interface-contract tests (P0.12).

Guarantees the stub endpoints Tracks T3/T5/T6 build against stay reachable and
keep their agreed response shape.
"""


def test_anomaly_contract_endpoints_are_registered(test_client, isolated_model,
                                                   isolated_features):
    """T5 has implemented these handlers, so the assertions cover the real
    (deterministic) no-model path: isolated_model + isolated_features give us
    a guaranteed-empty state regardless of what exists in this checkout.
    Behaviour with a fitted model is covered in test_anomaly_detection.py."""
    # GET /api/v1/anomalies
    res = test_client.get("/api/v1/anomalies")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "UNTRAINED"
    assert "Track T5" in body["owner"]
    assert set(body["model"]) >= {"loaded", "algorithm", "model_path", "contamination"}
    assert body["model"]["loaded"] is False
    assert isinstance(body["anomalies"], list)
    assert isinstance(body["series"], list)

    # POST /api/v1/anomalies/score — 409 until a model exists
    res = test_client.post("/api/v1/anomalies/score", json={
        "samples": [
            {"timestamp": 1.0, "service": "cart-service",
             "features": {"cpu_percent": 94.6}, "anomaly_score": 0.0},
            {"timestamp": 2.0, "service": "frontend",
             "features": {"cpu_percent": 1.1}, "anomaly_score": 0.0},
        ]
    })
    assert res.status_code == 409
    assert "train" in res.json()["detail"].lower()

    # POST /api/v1/anomalies/train — 409 while the feature store is empty
    res = test_client.post("/api/v1/anomalies/train")
    assert res.status_code == 409
    assert "sample" in res.json()["detail"].lower()


def test_llm_contract_endpoints_are_registered(test_client):
    # GET /api/v1/llm/health
    res = test_client.get("/api/v1/llm/health")
    assert res.status_code == 200
    health = res.json()
    assert set(health) >= {"enabled", "host", "model", "reachable", "status"}
    assert health["model"]  # non-empty model name comes from config

    # POST /api/v1/llm/rca — must honour the confidence gate contract
    res = test_client.post("/api/v1/llm/rca", json={
        "incident_id": "inc_test_1",
        "primary_service": "cart-service",
        "symptom": "HTTP 504",
        "retrieval_confidence": 0.50,   # below RCA_MIN_CONFIDENCE (0.65)
    })
    assert res.status_code == 200
    rca = res.json()
    assert set(rca) >= {"status", "model", "diagnosis", "confidence",
                        "citations", "remediation_blocked"}
    assert rca["remediation_blocked"] is True

    # ...and must NOT block when confidence clears the gate
    res = test_client.post("/api/v1/llm/rca", json={
        "incident_id": "inc_test_2",
        "primary_service": "cart-service",
        "retrieval_confidence": 0.90,
    })
    assert res.json()["remediation_blocked"] is False

    # POST /api/v1/llm/postmortem
    res = test_client.post("/api/v1/llm/postmortem", json={
        "incident_id": "inc_test_1",
        "ground_truth_cause": "NetworkChaos",
        "ai_diagnosis": "Network Latency",
    })
    assert res.status_code == 200
    assert res.json()["status"] == "NOT_IMPLEMENTED"


def test_topology_reports_only_measured_values(test_client):
    """P0.6: /topology must not invent replicas or latency figures."""
    res = test_client.get("/api/v1/topology")
    assert res.status_code == 200
    body = res.json()

    for svc in body["services"]:
        # No fabricated fields anywhere in the payload
        assert "replicas" not in svc
        assert "latency_p99" not in svc
        assert "memory_mb" in svc
        # Local mesh services must declare their real bound port
        if svc["source"] == "local-mesh":
            assert isinstance(svc["port"], int)
        else:
            assert svc["status"] in ("NOT_DEPLOYED", "DEGRADED")

    # Counts must be internally consistent
    assert body["total_microservices"] == len(body["services"])
    assert (body["healthy_count"] + body["degraded_count"]
            + body["not_running_count"] + body["not_deployed_count"]
            == body["total_microservices"])
