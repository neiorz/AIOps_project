"""
LLM GENERATION API — interface contract (owned by Track T3).

Phase 0 STUB. Fixes the request/response shape for local-LLM generation so
Track T3 (LLM + RAG) and Track T6 (Streamlit RCA explorer) can build in
parallel against a stable contract.

Track T3 replaces the handler bodies with real Ollama calls; schemas stay.
"""
from typing import Any, Dict, List, Optional
from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.config import settings

router = APIRouter(prefix="/llm", tags=["LLM Generation"])

STUB_OWNER = "Track T3 (Local LLM + RAG)"
STUB_MESSAGE = "Not implemented yet — Phase 0 interface stub. Track T3 fills this in."


# ---------------------------------------------------------------------------
# Contract schemas — DO NOT rename these fields.
# ---------------------------------------------------------------------------
class LLMHealth(BaseModel):
    enabled: bool = settings.LLM_ENABLED
    host: str = settings.OLLAMA_HOST
    model: str = settings.LLM_MODEL
    # None = not probed yet; Track T3 sets True/False after a real health call.
    reachable: Optional[bool] = None
    status: str = "STUB"


class RCARequest(BaseModel):
    incident_id: str
    primary_service: str
    symptom: str = ""
    severity: str = "warning"
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    runbook_context: str = ""
    # Retrieval confidence from ChromaDB; generation must respect this.
    retrieval_confidence: float = 0.0


class RCAResponse(BaseModel):
    status: str
    model: str = settings.LLM_MODEL
    diagnosis: Optional[str] = None
    narrative: Optional[str] = None
    confidence: Optional[float] = None
    citations: List[str] = Field(default_factory=list)
    # Set True when confidence < settings.RCA_MIN_CONFIDENCE and remediation
    # must be blocked.
    remediation_blocked: bool = False


class PostMortemRequest(BaseModel):
    incident_id: str
    ground_truth_cause: str = ""
    ai_diagnosis: str = ""
    timeline: List[str] = Field(default_factory=list)


class PostMortemResponse(BaseModel):
    status: str
    model: str = settings.LLM_MODEL
    narrative: Optional[str] = None
    sections: Dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Contract endpoints
# ---------------------------------------------------------------------------
@router.get("/health", response_model=LLMHealth)
def llm_health() -> LLMHealth:
    """Report LLM configuration. Track T3 adds a real reachability probe."""
    return LLMHealth(status="STUB")


@router.post("/rca", response_model=RCAResponse)
def generate_rca(req: RCARequest) -> RCAResponse:
    """Generate an evidence-grounded root-cause narrative for an incident."""
    return RCAResponse(
        status="NOT_IMPLEMENTED",
        confidence=req.retrieval_confidence or None,
        remediation_blocked=bool(
            0 < req.retrieval_confidence < settings.RCA_MIN_CONFIDENCE
        ),
    )


@router.post("/postmortem", response_model=PostMortemResponse)
def generate_postmortem(req: PostMortemRequest) -> PostMortemResponse:
    """Generate a structured post-mortem from an evaluated experiment."""
    return PostMortemResponse(status="NOT_IMPLEMENTED")
