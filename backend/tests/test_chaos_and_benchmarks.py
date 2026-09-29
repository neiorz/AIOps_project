def test_chaos_injection_and_ground_truth(test_client):
    payload = {
        "service": "payment-service",
        "experiment_type": "NetworkLatency",
        "tenant_id": "tenant_a",
        "duration_seconds": 60
    }
    response = test_client.post("/api/v1/chaos/inject", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "CHAOS_INJECTED"
    assert "ground_truth" in data
    assert data["ground_truth"]["service"] == "payment-service"
    assert data["ground_truth"]["experiment_type"] == "NetworkLatency"
    assert data["compressed_alerts_count"] >= 500
    assert "correlated_incident_id" in data

def test_evaluation_scorecard(test_client):
    response = test_client.get("/api/v1/benchmarks/scorecard")
    assert response.status_code == 200
    scorecard = response.json()
    assert "rca_accuracy_percentage" in scorecard
    assert scorecard["rca_accuracy_percentage"] >= 80.0
    assert "mean_time_to_detect_seconds" in scorecard
    assert "sla_protection_rate" in scorecard
    assert "investigation_efficiency" in scorecard

def test_incident_remediation_execution(test_client):
    # First inject chaos to get an incident
    inject_res = test_client.post("/api/v1/chaos/inject", json={
        "service": "cart-service",
        "experiment_type": "PodFailure",
        "tenant_id": "tenant_b"
    })
    incident_id = inject_res.json()["correlated_incident_id"]

    # Remediate
    rem_res = test_client.post(f"/api/v1/incidents/{incident_id}/remediate", json={
        "action_type": "ansible"
    })
    assert rem_res.status_code == 200
    rem_data = rem_res.json()
    assert rem_data["status"] == "SUCCESS"
    assert rem_data["verified_health"] is True
    assert len(rem_data["execution_output"]) > 0

    # Verify incident is now resolved
    inc_res = test_client.get(f"/api/v1/incidents/{incident_id}")
    assert inc_res.status_code == 200
    assert inc_res.json()["incident"]["status"] == "RESOLVED"

