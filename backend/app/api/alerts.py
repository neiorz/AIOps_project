from fastapi import APIRouter, BackgroundTasks
from app.correlation.engine import RawAlert, get_correlation_engine
from app.agent.sre_agent import get_sre_agent

router = APIRouter(prefix="/alerts", tags=["Alerts"])

@router.post("/webhook")
async def receive_alert(alert: RawAlert, background_tasks: BackgroundTasks):
    """
    Ingests Prometheus Alertmanager alerts, deduplicates and clusters them into Incidents.
    Triggers Autonomous AI Investigation Agent asynchronously.
    """
    engine = get_correlation_engine()
    incident = engine.correlate(alert)

    # Trigger agent investigation in background
    agent = get_sre_agent()
    background_tasks.add_task(agent.investigate_incident, incident)

    return {
        "status": "received",
        "alert_id": alert.id,
        "correlated_incident_id": incident.incident_id,
        "total_clustered_alerts": incident.total_alerts,
        "primary_service": incident.primary_service
    }

