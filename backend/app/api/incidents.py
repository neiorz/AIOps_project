from fastapi import APIRouter, HTTPException
from typing import List, Optional
from app.correlation.engine import get_correlation_engine, CorrelatedIncident
from app.sla.calculator import SLARiskCalculator
from app.agent.sre_agent import get_sre_agent

router = APIRouter(prefix="/incidents", tags=["Incidents"])

@router.get("", response_model=List[dict])
def list_incidents(tenant_id: Optional[str] = None):
    """
    List all active correlated incidents, sorted by SLA Risk priority.
    """
    engine = get_correlation_engine()
    incidents = engine.get_all_incidents()
    
    if tenant_id:
        incidents = [i for i in incidents if i.tenant_id == tenant_id]

    enriched = []
    for inc in incidents:
        sla_info = SLARiskCalculator.calculate_sla_status(
            tenant_id=inc.tenant_id,
            created_at=inc.created_at,
            severity=inc.severity
        )
        enriched.append({
            "incident": inc.model_dump(),
            "sla": sla_info
        })

    # Sort by SLA risk score descending (highest risk first)
    enriched.sort(key=lambda x: x["sla"]["risk_score"], reverse=True)
    return enriched

@router.get("/{incident_id}")
def get_incident_details(incident_id: str):
    engine = get_correlation_engine()
    incident = engine.get_incident(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")

    sla_info = SLARiskCalculator.calculate_sla_status(
        tenant_id=incident.tenant_id,
        created_at=incident.created_at,
        severity=incident.severity
    )

    return {
        "incident": incident.model_dump(),
        "sla": sla_info
    }

@router.post("/{incident_id}/diagnose")
async def trigger_diagnosis(incident_id: str):
    """Trigger on-demand Autonomous AI Investigation Agent on an incident."""
    engine = get_correlation_engine()
    incident = engine.get_incident(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")

    agent = get_sre_agent()
    result = await agent.investigate_incident(incident)
    return result
