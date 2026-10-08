"""
SQLAlchemy ORM models (Track T2).

Two tables, matching the shapes the platform already produces:

  GroundTruth  one row per chaos experiment — mirrors the dict that
               ``app/api/chaos.py`` builds as ``ledger_entry``
  Incident     one row per correlated incident — mirrors
               ``app/correlation/engine.py::CorrelatedIncident``

Field names are deliberately identical to the source dictionaries so
``model_dump()``/``to_dict()`` round-trips without a translation layer.
"""
from typing import Any, Dict, List, Optional

from sqlalchemy import Float, Integer, JSON, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for every Track T2 table."""


class GroundTruth(Base):
    """One chaos experiment, from injection through AI diagnosis."""

    __tablename__ = "ground_truth"

    experiment_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    service: Mapped[str] = mapped_column(String(80), index=True)
    experiment_type: Mapped[str] = mapped_column(String(60))
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    ground_truth_cause: Mapped[str] = mapped_column(Text)
    expected_symptom: Mapped[str] = mapped_column(Text)
    injected_at: Mapped[float] = mapped_column(Float, index=True)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE")

    #: Whatever the mesh actually did (dict, may contain k8s details)
    real_mesh_action: Mapped[Dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )

    #: Filled in once the agent finishes investigating
    ai_diagnosis: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confidence_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    investigation_duration_seconds: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )

    def to_dict(self) -> Dict[str, Any]:
        """Shape matches the in-memory ``ledger_entry`` exactly.

        The optional diagnosis fields are omitted while unset, because the
        in-memory ledger only gains those keys once the agent has finished.
        Emitting them as ``None`` would break consumers written against the
        old shape — e.g. the scorecard's ``item.get(key, 0.0)`` would receive
        ``None`` instead of its default.
        """
        data: Dict[str, Any] = {
            "experiment_id": self.experiment_id,
            "service": self.service,
            "experiment_type": self.experiment_type,
            "tenant_id": self.tenant_id,
            "ground_truth_cause": self.ground_truth_cause,
            "expected_symptom": self.expected_symptom,
            "injected_at": self.injected_at,
            "status": self.status,
            "real_mesh_action": self.real_mesh_action or {},
        }
        for key, value in (
            ("ai_diagnosis", self.ai_diagnosis),
            ("confidence_score", self.confidence_score),
            ("investigation_duration_seconds", self.investigation_duration_seconds),
        ):
            if value is not None:
                data[key] = value
        return data


class Incident(Base):
    """One correlated incident, as produced by the alert correlation engine."""

    __tablename__ = "incidents"

    incident_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    primary_service: Mapped[str] = mapped_column(String(80), index=True)
    root_cause_candidate: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(20), default="warning")
    tenant_id: Mapped[str] = mapped_column(String(40), index=True)
    created_at: Mapped[float] = mapped_column(Float, index=True)
    updated_at: Mapped[float] = mapped_column(Float)
    total_alerts: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="OPEN", index=True)

    affected_services: Mapped[List[str]] = mapped_column(JSON, default=list)
    alert_ids: Mapped[List[str]] = mapped_column(JSON, default=list)
    #: Serialized RawAlert samples — kept as JSON, not a child table, because
    #: they are diagnostics, not entities anything joins against.
    sample_alerts: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list)

    def to_dict(self) -> Dict[str, Any]:
        """Shape matches ``CorrelatedIncident.model_dump()``."""
        return {
            "incident_id": self.incident_id,
            "primary_service": self.primary_service,
            "root_cause_candidate": self.root_cause_candidate,
            "severity": self.severity,
            "tenant_id": self.tenant_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "total_alerts": self.total_alerts,
            "status": self.status,
            "affected_services": self.affected_services or [],
            "alert_ids": self.alert_ids or [],
            "sample_alerts": self.sample_alerts or [],
        }
