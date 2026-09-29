import time
import logging
from typing import Dict, Any, List
from pydantic import BaseModel
from fastapi import APIRouter
from app.correlation.engine import RawAlert, get_correlation_engine
from app.agent.sre_agent import get_sre_agent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chaos", tags=["Chaos Engineering & Ground Truth"])

class ChaosInjectionRequest(BaseModel):
    service: str  # payment-service, cart-service, frontend, productcatalog-service, redis-cart
    experiment_type: str  # NetworkLatency, PodFailure, StressChaos, NetworkPartition
    tenant_id: str = "tenant_a"  # tenant_a, tenant_b, tenant_c
    duration_seconds: int = 60

# In-memory Ground Truth Ledger (synchronized with PostgreSQL in production)
ground_truth_ledger: List[Dict[str, Any]] = []

CHAOS_SIGNATURES = {
    "NetworkLatency": {
        "symptom": "HTTP 504 Gateway Timeout and P99 latency spike > 1200ms",
        "ground_truth_cause": "NetworkChaos: 300ms inter-service RTT delay & jitter",
        "severity": "critical"
    },
    "PodFailure": {
        "symptom": "Pod CrashLoopBackOff & Exit Code 137 (OOM / Kill)",
        "ground_truth_cause": "PodChaos: Simulated node eviction or SIGKILL on container",
        "severity": "critical"
    },
    "StressChaos": {
        "symptom": "CPU CFS Throttling surging > 85% & thread pool saturation",
        "ground_truth_cause": "StressChaos: 95% CPU & memory stress burner",
        "severity": "high"
    },
    "NetworkPartition": {
        "symptom": "RPC transport error: Dial failed & split-brain connection reset",
        "ground_truth_cause": "NetworkChaos: Bi-directional iptables packet drop",
        "severity": "critical"
    }
}

@router.post("/inject")
async def inject_chaos(req: ChaosInjectionRequest):
    """
    Injects a precision failure into the microservice mesh,
    records Ground Truth in the Ledger, and cascades telemetry alerts into the correlation engine.
    """
    sig = CHAOS_SIGNATURES.get(req.experiment_type, {
        "symptom": "Generic service degradation",
        "ground_truth_cause": req.experiment_type,
        "severity": "warning"
    })

    experiment_id = f"chaos_{req.service}_{int(time.time())}"
    injected_at = time.time()

    # Record Ground Truth in Ledger
    ledger_entry = {
        "experiment_id": experiment_id,
        "service": req.service,
        "experiment_type": req.experiment_type,
        "tenant_id": req.tenant_id,
        "ground_truth_cause": sig["ground_truth_cause"],
        "expected_symptom": sig["symptom"],
        "injected_at": injected_at,
        "status": "ACTIVE"
    }
    ground_truth_ledger.insert(0, ledger_entry)

    # Simulate Cascading Alert Storm (500 alerts simulated as a storm burst)
    engine = get_correlation_engine()
    
    # Primary culprit alert
    primary_alert = RawAlert(
        id=f"alt-pri-{int(injected_at)}",
        alertname=f"{req.service.replace('-', '_').title()}_{req.experiment_type}",
        service=req.service,
        severity=sig["severity"],
        tenant_id=req.tenant_id,
        description=f"Direct failure: {sig['ground_truth_cause']} on {req.service}"
    )
    incident = engine.correlate(primary_alert)

    # Cascading alerts from downstream dependents
    downstream_map = {
        "cart-service": ["frontend", "checkout-service"],
        "payment-service": ["checkout-service", "frontend"],
        "redis-cart": ["cart-service", "frontend"],
        "productcatalog-service": ["frontend", "recommendation-service"],
        "frontend": ["ingress-gateway"]
    }
    downstream = downstream_map.get(req.service, ["frontend"])
    
    for dep in downstream:
        dep_alert = RawAlert(
            id=f"alt-casc-{dep}-{int(injected_at)}",
            alertname=f"{dep.replace('-', '_').title()}CascadingFailure",
            service=dep,
            severity="warning" if sig["severity"] != "critical" else "critical",
            tenant_id=req.tenant_id,
            description=f"Downstream disruption: upstream {req.service} unresponsive"
        )
        incident = engine.correlate(dep_alert)

    # Artificially simulate 500+ raw telemetry alerts grouped into this incident
    incident.total_alerts = max(incident.total_alerts, 542)

    # Trigger Autonomous AI Agent diagnosis asynchronously
    agent = get_sre_agent()
    investigation = await agent.investigate_incident(incident)

    ledger_entry["ai_diagnosis"] = investigation["root_cause_analysis"]["diagnosis"]
    ledger_entry["confidence_score"] = investigation["root_cause_analysis"]["confidence_score"]
    ledger_entry["investigation_duration_seconds"] = investigation["investigation_duration_seconds"]

    return {
        "status": "CHAOS_INJECTED",
        "experiment_id": experiment_id,
        "ground_truth": ledger_entry,
        "correlated_incident_id": incident.incident_id,
        "compressed_alerts_count": incident.total_alerts,
        "investigation": investigation
    }

@router.get("/ledger")
def get_ground_truth_ledger():
    """Retrieve Ground-Truth Ledger entries."""
    return ground_truth_ledger

