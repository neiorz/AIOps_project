import time
from typing import Dict, Any
from app.config import settings

TENANT_SLA_MAPPING = {
    "tenant_a": settings.SLA_CLIENT_A_SECONDS,  # 300s (5 min)
    "tenant_b": settings.SLA_CLIENT_B_SECONDS,  # 900s (15 min)
    "tenant_c": settings.SLA_CLIENT_C_SECONDS,  # 1800s (30 min)
}

class SLARiskCalculator:
    """
    Multi-tenant SLA Risk Engine.
    Computes time-to-breach (TTB), SLA breach percentage, and assigns incident priority.
    """

    @classmethod
    def calculate_sla_status(
        cls,
        tenant_id: str,
        created_at: float,
        current_time: float = None,
        severity: str = "warning"
    ) -> Dict[str, Any]:
        if current_time is None:
            current_time = time.time()

        max_sla_seconds = TENANT_SLA_MAPPING.get(tenant_id.lower(), settings.SLA_CLIENT_B_SECONDS)
        elapsed_seconds = max(0.0, current_time - created_at)
        time_to_breach_seconds = max(0.0, max_sla_seconds - elapsed_seconds)
        
        # Risk score calculation (0 - 100%)
        # Severity weighting multiplier: critical=1.5x risk acceleration
        severity_multiplier = 1.5 if severity == "critical" else 1.0
        raw_risk = (elapsed_seconds / max_sla_seconds) * 100.0 * severity_multiplier
        risk_score = min(100.0, round(raw_risk, 2))

        is_breached = elapsed_seconds >= max_sla_seconds

        # Dynamic priority ranking
        if is_breached or risk_score >= 80.0:
            priority = "P1-CRITICAL"
        elif risk_score >= 50.0:
            priority = "P2-HIGH"
        elif risk_score >= 25.0:
            priority = "P3-MEDIUM"
        else:
            priority = "P4-LOW"

        return {
            "tenant_id": tenant_id,
            "max_sla_seconds": max_sla_seconds,
            "elapsed_seconds": round(elapsed_seconds, 1),
            "time_to_breach_seconds": round(time_to_breach_seconds, 1),
            "risk_score": risk_score,
            "is_breached": is_breached,
            "priority": priority
        }

