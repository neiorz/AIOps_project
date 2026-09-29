import time
from app.sla.calculator import SLARiskCalculator

def test_sla_client_a_5_minutes():
    """Client A has a strict 5 minute (300s) SLA window."""
    now = time.time()
    created_at = now - 60  # 1 minute elapsed
    status = SLARiskCalculator.calculate_sla_status(
        tenant_id="tenant_a",
        created_at=created_at,
        current_time=now,
        severity="warning"
    )
    assert status["max_sla_seconds"] == 300
    assert status["elapsed_seconds"] == 60.0
    assert status["time_to_breach_seconds"] == 240.0
    assert status["is_breached"] is False
    assert status["priority"] in ["P3-MEDIUM", "P4-LOW"]

def test_sla_client_b_15_minutes():
    """Client B has a 15 minute (900s) SLA window."""
    now = time.time()
    created_at = now - 450  # half time elapsed
    status = SLARiskCalculator.calculate_sla_status(
        tenant_id="tenant_b",
        created_at=created_at,
        current_time=now,
        severity="warning"
    )
    assert status["max_sla_seconds"] == 900
    assert status["risk_score"] == 50.0
    assert status["priority"] == "P2-HIGH"

def test_sla_client_c_30_minutes():
    """Client C has a 30 minute (1800s) SLA window."""
    now = time.time()
    created_at = now - 1800  # Exactly at threshold
    status = SLARiskCalculator.calculate_sla_status(
        tenant_id="tenant_c",
        created_at=created_at,
        current_time=now,
        severity="warning"
    )
    assert status["max_sla_seconds"] == 1800
    assert status["is_breached"] is True
    assert status["priority"] == "P1-CRITICAL"

def test_sla_critical_severity_acceleration():
    """Critical severity accelerates risk calculation score."""
    now = time.time()
    created_at = now - 100
    status_warning = SLARiskCalculator.calculate_sla_status("tenant_a", created_at, now, "warning")
    status_critical = SLARiskCalculator.calculate_sla_status("tenant_a", created_at, now, "critical")
    assert status_critical["risk_score"] > status_warning["risk_score"]

