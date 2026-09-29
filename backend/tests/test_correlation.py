import time
from app.correlation.engine import AlertCorrelationEngine, RawAlert

def test_alert_clustering_and_deduplication():
    engine = AlertCorrelationEngine(time_window_seconds=120)

    # 1. First alert for cartservice
    alert1 = RawAlert(
        id="alt-001",
        alertname="CartServiceDown",
        service="cartservice",
        severity="warning",
        tenant_id="tenant_a",
        description="Cart service connection refused"
    )
    incident1 = engine.correlate(alert1)
    assert incident1.total_alerts == 1
    assert incident1.primary_service == "cartservice"
    assert incident1.affected_services == ["cartservice"]

    # 2. Cascading alert 5 seconds later for frontend failing to talk to cartservice
    alert2 = RawAlert(
        id="alt-002",
        alertname="FrontendHttp500Spike",
        service="frontend",
        severity="warning",
        tenant_id="tenant_a",
        description="Frontend cannot load user cart"
    )
    incident2 = engine.correlate(alert2)
    # Deduplicated into the same incident!
    assert incident2.incident_id == incident1.incident_id
    assert incident2.total_alerts == 2
    assert "frontend" in incident2.affected_services
    assert "cartservice" in incident2.affected_services

    # 3. Third alert with critical severity elevates incident severity
    alert3 = RawAlert(
        id="alt-003",
        alertname="CheckoutFailureSpike",
        service="checkoutservice",
        severity="critical",
        tenant_id="tenant_a",
        description="Checkout transactions dropping"
    )
    incident3 = engine.correlate(alert3)
    assert incident3.incident_id == incident1.incident_id
    assert incident3.total_alerts == 3
    assert incident3.severity == "critical"

def test_different_tenant_creates_separate_incident():
    engine = AlertCorrelationEngine(time_window_seconds=120)
    
    alert_tenant_a = RawAlert(
        id="alt-a",
        alertname="HighLatency",
        service="paymentservice",
        tenant_id="tenant_a"
    )
    alert_tenant_b = RawAlert(
        id="alt-b",
        alertname="HighLatency",
        service="paymentservice",
        tenant_id="tenant_b"
    )

    inc_a = engine.correlate(alert_tenant_a)
    inc_b = engine.correlate(alert_tenant_b)

    assert inc_a.incident_id != inc_b.incident_id
    assert inc_a.tenant_id == "tenant_a"
    assert inc_b.tenant_id == "tenant_b"

