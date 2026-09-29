def test_post_alert_and_auto_correlate(test_client):
    payload = {
        "id": "alt-test-101",
        "alertname": "PodCrashLooping",
        "service": "cartservice",
        "severity": "critical",
        "tenant_id": "tenant_a",
        "description": "Cart service container crashed with exit code 137"
    }
    response = test_client.post("/api/v1/alerts/webhook", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "received"
    assert "correlated_incident_id" in data
    assert data["primary_service"] == "cartservice"

def test_list_incidents_with_sla_ordering(test_client):
    response = test_client.get("/api/v1/incidents")
    assert response.status_code == 200
    incidents = response.json()
    assert isinstance(incidents, list)
    if len(incidents) > 0:
        first = incidents[0]
        assert "incident" in first
        assert "sla" in first
        assert "risk_score" in first["sla"]
        assert "priority" in first["sla"]

def test_rag_search_api(test_client):
    response = test_client.get("/api/v1/rag/search?q=latency%20packet%20loss&n=2")
    assert response.status_code == 200
    data = response.json()
    assert "results" in data
    assert len(data["results"]) > 0
    assert "title" in data["results"][0]

def test_on_demand_diagnosis_api(test_client):
    # First post an alert to ensure an incident exists
    test_client.post("/api/v1/alerts/webhook", json={
        "id": "alt-diag-1",
        "alertname": "RedisConnectionFailed",
        "service": "cartservice",
        "severity": "warning",
        "tenant_id": "tenant_b"
    })
    
    incidents = test_client.get("/api/v1/incidents?tenant_id=tenant_b").json()
    assert len(incidents) > 0
    incident_id = incidents[0]["incident"]["incident_id"]

    diag_response = test_client.post(f"/api/v1/incidents/{incident_id}/diagnose")
    assert diag_response.status_code == 200
    diag_data = diag_response.json()
    assert "root_cause_analysis" in diag_data
    assert "confidence_score" in diag_data["root_cause_analysis"]
    assert "referenced_runbooks" in diag_data["root_cause_analysis"]

