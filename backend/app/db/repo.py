"""
Repository layer (Track T2): read/write the two tables, plus the one metric
the scorecard could not compute before incidents had somewhere to live.

Every function degrades to a no-write/no-read with a logged warning rather
than raising: persistence must never take down a chaos injection or the
dashboard. Callers decide what the empty result means for them.
"""
import logging
import time
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.db.models import GroundTruth, Incident
from app.db.session import session_scope

logger = logging.getLogger(__name__)

#: Fields of GroundTruth we accept from a ledger dict. Anything else in the
#: dict is ignored rather than exploding on an unexpected kwarg.
_GROUND_TRUTH_FIELDS = (
    "experiment_id", "service", "experiment_type", "tenant_id",
    "ground_truth_cause", "expected_symptom", "injected_at", "status",
    "real_mesh_action", "injection_mode", "ai_diagnosis", "confidence_score",
    "investigation_duration_seconds",
)

_INCIDENT_FIELDS = (
    "incident_id", "primary_service", "root_cause_candidate", "severity",
    "tenant_id", "created_at", "updated_at", "total_alerts", "status",
    "affected_services", "alert_ids", "sample_alerts",
)


# ---------------------------------------------------------------------------
# Ground truth ledger
# ---------------------------------------------------------------------------
def save_ground_truth(entry: Dict[str, Any]) -> bool:
    """Upsert one chaos experiment. Returns False if persistence failed."""
    if not entry or "experiment_id" not in entry:
        return False
    try:
        with session_scope() as session:
            session.merge(GroundTruth(**{k: entry.get(k) for k in _GROUND_TRUTH_FIELDS}))
        return True
    except Exception as exc:                       # noqa: BLE001
        logger.warning("Could not persist ground truth %s: %s",
                       entry.get("experiment_id"), exc)
        return False


def load_ground_truth(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """All experiments, newest first (matches the in-memory list's ordering)."""
    try:
        with session_scope() as session:
            rows = session.scalars(
                select(GroundTruth).order_by(GroundTruth.injected_at.desc())
            ).all()
            result = [r.to_dict() for r in rows]     # inside the session
    except Exception as exc:                       # noqa: BLE001
        logger.warning("Could not read ground truth ledger: %s", exc)
        return []
    return result[:limit] if limit else result


def count_ground_truth() -> int:
    try:
        with session_scope() as session:
            return len(session.scalars(select(GroundTruth)).all())
    except Exception:                              # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# Correlated incidents
# ---------------------------------------------------------------------------
def save_incident(incident: Any) -> bool:
    """Upsert one incident (accepts CorrelatedIncident or a plain dict)."""
    try:
        data = (incident.model_dump() if hasattr(incident, "model_dump")
                else dict(incident))
        if not data.get("incident_id"):
            return False
        with session_scope() as session:
            session.merge(Incident(**{k: data.get(k) for k in _INCIDENT_FIELDS}))
        return True
    except Exception as exc:                       # noqa: BLE001
        logger.warning("Could not persist incident: %s", exc)
        return False


def load_incidents(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    try:
        with session_scope() as session:
            rows = session.scalars(
                select(Incident).order_by(Incident.created_at.desc())
            ).all()
            result = [r.to_dict() for r in rows]     # inside the session
    except Exception as exc:                       # noqa: BLE001
        logger.warning("Could not read incidents: %s", exc)
        return []
    return result[:limit] if limit else result


# ---------------------------------------------------------------------------
# Metric that only becomes possible once incidents persist
# ---------------------------------------------------------------------------
def _live_incidents() -> List[Dict[str, Any]]:
    """Incidents the correlation engine currently holds in memory."""
    try:
        from app.correlation.engine import get_correlation_engine
        return [i.model_dump()
                for i in get_correlation_engine().get_all_incidents()]
    except Exception:                              # noqa: BLE001
        return []


def compute_sla_protection_rate() -> Optional[float]:
    """
    Percentage of incidents whose SLA was still intact when detection and
    investigation finished.

    ``elapsed = updated_at - created_at`` is the platform's own detection +
    RCA time; it is compared against that tenant's SLA budget via the existing
    :class:`~app.sla.calculator.SLARiskCalculator`, so the severity rules and
    per-tenant budgets stay in one place.

    Persisted rows win over the in-memory engine so a restart does not erase
    history. Returns None only when no incident has ever been recorded —
    never a fabricated 0 or 100.

    :return: 0.0–100.0, or None when there is nothing to measure.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    for src in (_live_incidents(), load_incidents()):
        for row in src:
            rid = row.get("incident_id")
            if rid:
                merged[rid] = row
    if not merged:
        return None

    from app.sla.calculator import SLARiskCalculator

    protected = 0
    for row in merged.values():
        created = float(row.get("created_at") or 0.0)
        finished = float(row.get("updated_at") or created)
        status = SLARiskCalculator.calculate_sla_status(
            tenant_id=str(row.get("tenant_id") or "tenant_a"),
            created_at=created,
            current_time=max(finished, created),
            severity=str(row.get("severity") or "warning"),
        )
        if not status["is_breached"]:
            protected += 1

    return round((protected / len(merged)) * 100.0, 1)
