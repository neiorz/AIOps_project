"""
ANOMALY DETECTION API — interface contract (owned by Track T5).

This file is a PHASE 0 STUB. It fixes the request/response schema so that:
  * Track T5 can implement the real Isolation Forest logic,
  * Track T6 (Streamlit) can build its UI against a stable contract,
  * both can proceed in parallel without renegotiating shapes.

Track T5 replaces the bodies of these handlers; the schemas stay.
"""
from typing import Any, Dict, List, Optional
from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.config import settings

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
    return {
        "status": "STUB",
        "owner": STUB_OWNER,
        "message": STUB_MESSAGE,
        "model": ModelStatus().model_dump(),
        "anomalies": [],
        "series": [],
    }


@router.post("/score", response_model=ScoreResponse)
def score_samples(req: ScoreRequest) -> ScoreResponse:
    """Score a batch of feature vectors with the trained model."""
    return ScoreResponse(
        status="STUB",
        model=ModelStatus(),
        scored=len(req.samples),
        anomaly_count=0,
        anomalies=[],
    )


@router.post("/train", response_model=TrainResponse)
def train_model() -> TrainResponse:
    """(Re)train Isolation Forest on collected telemetry."""
    return TrainResponse(status="NOT_IMPLEMENTED", model=ModelStatus())
