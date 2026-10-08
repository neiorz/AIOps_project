"""
ANOMALY DETECTION API — interface contract (owned by Track T5).

Phase 0 fixed the request/response schema so T5 and T6 could build in
parallel. This file is T5's implementation: the SCHEMAS below are unchanged,
only the handler bodies were filled in.

Live behaviour:
  GET  /anomalies        -> model status + persisted anomalies + recent series
  POST /anomalies/score  -> 409 until a model exists, otherwise scores a batch
  POST /anomalies/train  -> fits IsolationForest on app/ml/data/features.csv
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.correlation.engine import get_correlation_engine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/anomalies", tags=["Anomaly Detection"])

STUB_OWNER = "Track T5 (Anomaly Detection)"
STUB_MESSAGE = "Not implemented yet — Phase 0 interface stub. Track T5 fills this in."


# ---------------------------------------------------------------------------
# Contract schemas — DO NOT rename these fields.
# ---------------------------------------------------------------------------
class AnomalyPoint(BaseModel):
    timestamp: float
    service: str
    features: Dict[str, float] = Field(default_factory=dict)
    anomaly_score: float = 0.0
    is_anomaly: bool = False


class ModelStatus(BaseModel):
    loaded: bool = False
    algorithm: str = "IsolationForest"
    model_path: str = str(settings.ML_MODEL_PATH)
    trained_at: Optional[float] = None
    feature_window: int = settings.ML_FEATURE_WINDOW
    contamination: float = settings.ML_CONTAMINATION
    training_samples: Optional[int] = None


class ScoreRequest(BaseModel):
    samples: List[AnomalyPoint] = Field(default_factory=list)


class ScoreResponse(BaseModel):
    status: str
    model: ModelStatus
    scored: int = 0
    anomaly_count: int = 0
    anomalies: List[AnomalyPoint] = Field(default_factory=list)


class TrainResponse(BaseModel):
    status: str
    owner: str = STUB_OWNER
    message: str = STUB_MESSAGE
    model: ModelStatus


# ---------------------------------------------------------------------------
# Contract endpoints
# ---------------------------------------------------------------------------
@router.get("", response_model=Dict[str, Any])
def get_anomalies() -> Dict[str, Any]:
    """Current model status plus detected anomalies (most recent first)."""
    from app.ml.collector import load_features
    from app.ml.scoring import load_anomalies, model_status

    status = model_status()
    persisted = list(reversed(load_anomalies()))     # newest first
    recent = load_features()[-200:]

    return {
        "status": "READY" if status["loaded"] else "UNTRAINED",
        "owner": STUB_OWNER,
        "message": ("Isolation Forest loaded — anomalies below are real detections."
                    if status["loaded"]
                    else "No model yet. Collect features, then POST /anomalies/train."),
        "model": status,
        "anomalies": persisted,
        "series": recent,
    }


@router.post("/score", response_model=ScoreResponse)
def score_samples(req: ScoreRequest) -> ScoreResponse:
    """Score a batch of feature vectors with the trained model."""
    from app.ml.scoring import append_anomalies, model_status, score

    if not req.samples:
        return ScoreResponse(status="EMPTY", model=model_status(),
                             scored=0, anomaly_count=0, anomalies=[])

    feature_dicts = [s.features for s in req.samples]
    try:
        scores, flags = score(feature_dicts)
    except RuntimeError as exc:
        # Honest failure: we never report a result the model did not produce.
        raise HTTPException(status_code=409, detail=str(exc))

    results: List[AnomalyPoint] = []
    for sample, sc, is_anom in zip(req.samples, scores, flags):
        results.append(sample.model_copy(update={"anomaly_score": sc, "is_anomaly": is_anom}))

    # Persist only the actual detections so the timeline stays meaningful.
    detected = [p for p in results if p.is_anomaly]
    append_anomalies([p.model_dump() for p in detected])

    # Phase 2 pipeline: a detection is the START of the incident lifecycle,
    # not the end of it. Correlate each anomaly so it can flow
    # agent -> LLM -> confidence gate -> remediation like any other alert.
    for point in detected:
        incident = get_correlation_engine().correlate_anomaly(
            service=point.service,
            anomaly_score=point.anomaly_score,
            timestamp=point.timestamp,
        )
        logger.info(
            "anomaly on %s (score=%.4f) -> incident %s (%d alert(s) clustered)",
            point.service, point.anomaly_score, incident.incident_id,
            incident.total_alerts,
        )

    return ScoreResponse(
        status="SCORED",
        model=model_status(),
        scored=len(results),
        anomaly_count=len(detected),
        anomalies=detected,
    )


@router.post("/train", response_model=TrainResponse)
def train_model() -> TrainResponse:
    """(Re)train Isolation Forest on collected telemetry."""
    from app.ml.scoring import invalidate_cache, model_status
    from app.ml.train import train_model as _train

    try:
        meta = _train()
    except ValueError as exc:
        # Too little data is a client error, not a server crash.
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:                      # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail=f"Training failed: {exc}")

    invalidate_cache()
    ignored = meta.get("constant_features") or []
    return TrainResponse(
        status="TRAINED",
        message=(f"IsolationForest fitted on {meta['n_samples']} samples; "
                 f"flagged {meta['flagged_in_training']} in-sample; "
                 f"contamination={meta['contamination']}"
                 + (f". Ignored (no variance in training data): "
                    f"{', '.join(ignored)}" if ignored else "")),
        model=model_status(),
    )
