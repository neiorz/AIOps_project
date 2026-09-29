"""
Tests for Real Microservice Mesh and Human Approval Gate.
"""

import pytest

def test_mesh_status_and_metrics(test_client):
    # Test mesh status
    res = test_client.get("/api/v1/mesh/status")
    assert res.status_code == 200
    services = res.json()
    assert len(services) == 5
    service_names = [s["service"] for s in services]
    assert "payment-service" in service_names
    assert "cart-service" in service_names
    assert "frontend" in service_names

    # Test Prometheus metrics exposition format
    metrics_res = test_client.get("/api/v1/mesh/metrics")
    assert metrics_res.status_code == 200
    metrics_text = metrics_res.text
    assert 'service_up{service="payment-service"}' in metrics_text
    assert 'http_requests_total' in metrics_text

def test_human_approval_gate_workflow(test_client):
    # 1. Set mode to Human Approval (autonomous_mode=False)
    mode_res = test_client.post("/api/v1/incidents/mode", json={"autonomous_mode": False})
    assert mode_res.status_code == 200
    assert mode_res.json()["autonomous_mode"] is False
    assert mode_res.json()["requires_approval"] is True

    # 2. Inject chaos to create an incident
    inject_res = test_client.post("/api/v1/chaos/inject", json={
        "service": "payment-service",
        "experiment_type": "PodFailure",
        "tenant_id": "tenant_a"
    })
    assert inject_res.status_code == 200
    inc_id = inject_res.json()["correlated_incident_id"]

    # 3. Trigger diagnosis - MUST transition to PENDING_APPROVAL and NOT auto-resolve
    diag_res = test_client.post(f"/api/v1/incidents/{inc_id}/diagnose")
    assert diag_res.status_code == 200
    diag_data = diag_res.json()
    assert diag_data["approval_state"]["status"] == "AWAITING_APPROVAL"

    # Verify incident state is PENDING_APPROVAL
    inc_detail = test_client.get(f"/api/v1/incidents/{inc_id}").json()
    assert inc_detail["incident"]["status"] == "PENDING_APPROVAL"
    assert inc_detail["requires_human_approval"] is True

    # 4. Human Operator approves remediation
    approve_res = test_client.post(f"/api/v1/incidents/{inc_id}/approve")
    assert approve_res.status_code == 200
    assert approve_res.json()["status"] == "SUCCESS"
    assert approve_res.json()["verified_health"] is True

    # Verify incident is now RESOLVED
    inc_resolved = test_client.get(f"/api/v1/incidents/{inc_id}").json()
    assert inc_resolved["incident"]["status"] == "RESOLVED"

def test_human_rejection_workflow(test_client):
    # Set mode to Human Approval
    test_client.post("/api/v1/incidents/mode", json={"autonomous_mode": False})

    # Inject chaos
    inject_res = test_client.post("/api/v1/chaos/inject", json={
        "service": "cart-service",
        "experiment_type": "NetworkLatency",
        "tenant_id": "tenant_b"
    })
    inc_id = inject_res.json()["correlated_incident_id"]

    # Diagnose -> PENDING_APPROVAL
    test_client.post(f"/api/v1/incidents/{inc_id}/diagnose")

    # Human Operator rejects remediation
    reject_res = test_client.post(f"/api/v1/incidents/{inc_id}/reject?reason=Need+manual+database+check+first")
    assert reject_res.status_code == 200
    assert reject_res.json()["status"] == "REJECTED"

    # Verify incident is now REJECTED
    inc_rejected = test_client.get(f"/api/v1/incidents/{inc_id}").json()
    assert inc_rejected["incident"]["status"] == "REJECTED"

