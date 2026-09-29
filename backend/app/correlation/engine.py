import time
from typing import Dict, List, Optional
from pydantic import BaseModel, Field

class RawAlert(BaseModel):
    id: str
    alertname: str
    service: str
    severity: str = "warning"
    tenant_id: str = "tenant_b"  # tenant_a, tenant_b, tenant_c
    timestamp: float = Field(default_factory=time.time)
    description: Optional[str] = None
    labels: Dict[str, str] = Field(default_factory=dict)

class CorrelatedIncident(BaseModel):
    incident_id: str
    primary_service: str
    root_cause_candidate: str
    severity: str
    tenant_id: str
    created_at: float
    updated_at: float
    total_alerts: int
    affected_services: List[str]
    alert_ids: List[str]
    sample_alerts: List[RawAlert]
    status: str = "OPEN"  # OPEN, INVESTIGATING, MITIGATED, RESOLVED

class AlertCorrelationEngine:
    """
    Sliding-window alert deduplication and cross-service clustering engine.
    Correlates cascading microservice alerts into unified Incidents.
    """
    def __init__(self, time_window_seconds: int = 120):
        self.time_window = time_window_seconds
        # In-memory buffer (to be backed by Redis in production cluster)
        self.active_incidents: Dict[str, CorrelatedIncident] = {}

    def correlate(self, alert: RawAlert) -> CorrelatedIncident:
        current_time = time.time()
        
        # Check if alert matches any active incident within the time window
        matched_incident: Optional[CorrelatedIncident] = None
        for inc_id, inc in self.active_incidents.items():
            if inc.tenant_id == alert.tenant_id and inc.status not in ["RESOLVED", "REJECTED"]:
                # Same service or cascading dependency within time window
                time_diff = current_time - inc.updated_at
                if time_diff <= self.time_window:
                    matched_incident = inc
                    break

        if matched_incident:
            # Deduplicate & enrich existing incident
            matched_incident.total_alerts += 1
            matched_incident.updated_at = current_time
            if alert.id not in matched_incident.alert_ids:
                matched_incident.alert_ids.append(alert.id)
            if alert.service not in matched_incident.affected_services:
                matched_incident.affected_services.append(alert.service)
            if len(matched_incident.sample_alerts) < 5:
                matched_incident.sample_alerts.append(alert)
            
            # Elevate severity if critical alert received
            if alert.severity == "critical":
                matched_incident.severity = "critical"

            return matched_incident
        else:
            # Create a new incident cluster
            new_incident_id = f"inc_{alert.tenant_id}_{int(current_time)}_{len(self.active_incidents) + 1}"
            incident = CorrelatedIncident(
                incident_id=new_incident_id,
                primary_service=alert.service,
                root_cause_candidate=alert.alertname,
                severity=alert.severity,
                tenant_id=alert.tenant_id,
                created_at=current_time,
                updated_at=current_time,
                total_alerts=1,
                affected_services=[alert.service],
                alert_ids=[alert.id],
                sample_alerts=[alert],
                status="OPEN"
            )
            self.active_incidents[new_incident_id] = incident
            return incident

    def get_all_incidents(self) -> List[CorrelatedIncident]:
        return list(self.active_incidents.values())

    def get_incident(self, incident_id: str) -> Optional[CorrelatedIncident]:
        return self.active_incidents.get(incident_id)

_correlation_engine = AlertCorrelationEngine()

def get_correlation_engine() -> AlertCorrelationEngine:
    return _correlation_engine

