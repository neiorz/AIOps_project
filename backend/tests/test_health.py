def test_root_endpoint_serves_dashboard(test_client):
    response = test_client.get("/")
    assert response.status_code == 200
    assert "AIOPS COMMAND CENTER" in response.text
    assert "<!DOCTYPE html>" in response.text

def test_api_meta_endpoint(test_client):
    response = test_client.get("/api")
    assert response.status_code == 200
    data = response.json()
    assert "platform" in data
    assert data["docs_url"] == "/docs"

def test_health_check_endpoint(test_client):
    response = test_client.get("/api/v1/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["mode"] == "autonomous"
