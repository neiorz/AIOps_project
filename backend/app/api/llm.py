"""
LLM GENERATION API — Track T3 (Local LLM + RAG).

The Phase 0 stub is gone: these handlers probe a real local Ollama server,
ground generation in the ChromaDB runbook index, and enforce the RCA
confidence gate. The response SCHEMAS stay exactly as Phase 0 fixed them
(Track T6 and the contract tests build against them).

Design notes worth keeping:

- Generation is faithful, never optimistic. If the server is down, the model
  is not pulled, or the reply does not match the contract, the status says
  so and diagnosis/narrative stay None (or keep the raw reply) — fabricated
  output is never presented as generated.

- The confidence gate depends ONLY on retrieval confidence. A dead model
  must not flip `remediation_blocked`: callers (and the Phase 0 contract)
  read that flag as "the evidence is too weak to act on", and the scorecard
  computes remediation rates from it.

- _probe_ollama / _call_ollama are module-level seams so `make test` runs
  with no server and no network (T3 step 5): tests replace them wholesale.

Statuses in use:
  health     READY | MODEL_MISSING | UNREACHABLE | DISABLED
  rca        OK | UNREACHABLE | MODEL_MISSING | GENERATION_FAILED | DISABLED
  postmortem OK | UNREACHABLE | MODEL_MISSING | GENERATION_FAILED | DISABLED
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm", tags=["LLM Generation"])

# The six post-mortem sections. One constant so prompt, parser and tests
# cannot drift apart.
POSTMORTEM_SECTIONS = (
    "summary", "root_cause", "impact", "detection", "remediation", "prevention",
)


# ---------------------------------------------------------------------------
# Contract schemas — DO NOT rename these fields.
# ---------------------------------------------------------------------------
class LLMHealth(BaseModel):
    enabled: bool = settings.LLM_ENABLED
    host: str = settings.OLLAMA_HOST
    model: str = settings.LLM_MODEL
    # None = not probed (e.g. LLM disabled); True/False after a real probe.
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
# Prompts
# ---------------------------------------------------------------------------
RCA_SYSTEM_PROMPT = """\
You are the SRE root-cause analyst inside an AIOps platform watching a
microservices demo shop. You receive an incident, the evidence observed
while investigating it, and excerpts from the team's runbooks.

Answer with ONLY a JSON object of exactly this shape:
{"diagnosis": "<one line, runbook-style failure label>", "narrative": "<3-6 sentences>"}

Rules:
- Ground every claim in the evidence or the runbook excerpts, and name the
  runbook you relied on inside the narrative.
- If the evidence contradicts the runbooks, say so instead of forcing a match.
- Never invent metrics, timestamps or services that are not in the input.
- If the evidence is insufficient, say what is missing rather than guessing.
"""

POSTMORTEM_SYSTEM_PROMPT = """\
You are the SRE writing a blameless post-mortem for an AIOps platform that
just evaluated a chaos experiment. You receive the ground-truth fault that
was injected, the AI diagnosis that was produced, and the incident timeline.

Answer with ONLY a JSON object with exactly these six string keys:
{"summary": "...", "root_cause": "...", "impact": "...", "detection": "...", "remediation": "...", "prevention": "..."}

Rules:
- Compare the AI diagnosis against the ground truth honestly: state whether
  it matched, was close, or was wrong, and what misled it.
- Ground every section in the timeline you were given; never invent events.
- Keep each section to 1-4 sentences.
"""


# ---------------------------------------------------------------------------
# Seams — tests replace these so the suite needs no server and no network.
# ---------------------------------------------------------------------------
def _probe_ollama() -> Dict[str, bool]:
    """Reachability probe that distinguishes "no server" from "model not
    pulled", so /health cannot be READY when generation would fail."""
    probe = {"reachable": False, "model_loaded": False}
    if not settings.LLM_ENABLED:
        return probe

    try:
        import ollama

        listing = ollama.Client(
            host=settings.OLLAMA_HOST, timeout=5
        ).list()
    except Exception:
        return probe

    probe["reachable"] = True
    names = _model_names(listing)
    wanted = settings.LLM_MODEL
    wanted_base = wanted.split(":")[0].split("/")[-1]
    # A listed "llama3.2:3b" satisfies a configured "llama3.2:3b" or
    # "llama3.2" and vice versa; tags and host prefixes must not hide it.
    probe["model_loaded"] = any(
        name == wanted or name.split(":")[0].split("/")[-1] == wanted_base
        for name in names
    )
    return probe


def _model_names(listing: Any) -> List[str]:
    """Model names from an SDK list() response (object or dict shaped)."""
    models = getattr(listing, "models", None)
    if models is None and isinstance(listing, dict):
        models = listing.get("models")
    names = []
    for model in models or []:
        if isinstance(model, dict):
            raw = model.get("model") or model.get("name") or ""
        else:
            raw = getattr(model, "model", None) or getattr(model, "name", None) or ""
        if raw:
            names.append(str(raw))
    return names


def _call_ollama(system: str, user: str) -> str:
    """One JSON-mode chat completion against the local Ollama server.

    Raises on any failure (server down, model missing, empty reply) — the
    callers classify the failure instead of guessing at it.
    """
    import ollama

    client = ollama.Client(
        host=settings.OLLAMA_HOST, timeout=settings.LLM_TIMEOUT_SECONDS
    )
    response = client.chat(
        model=settings.LLM_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        format="json",
        options={"num_predict": settings.LLM_MAX_TOKENS},
    )
    if isinstance(response, dict):
        content = (response.get("message") or {}).get("content")
    else:
        content = getattr(getattr(response, "message", None), "content", None)
    if not content or not str(content).strip():
        raise RuntimeError(f"Ollama returned no message content: {response!r:.200}")
    return str(content)


def _retrieve(query: str, n_results: int = 3) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Search the runbook index.

    Returns (runbooks, error-or-None) and never raises: retrieval failing
    must degrade to "no grounding" (confidence 0 -> gate blocks), not to a
    500 that hides the incident behind it.
    """
    try:
        from app.rag.retriever import get_runbook_retriever

        return get_runbook_retriever().search_relevant_runbooks(
            query, n_results=n_results
        ), None
    except Exception as exc:
        logger.warning("runbook retrieval failed for query %r: %s", query[:80], exc)
        return [], str(exc)


def _failure_status() -> str:
    """Classify why a generation attempt failed: no server, missing model,
    or the server answered and the call still failed."""
    probe = _probe_ollama()
    if not probe["reachable"]:
        return "UNREACHABLE"
    if not probe["model_loaded"]:
        return "MODEL_MISSING"
    return "GENERATION_FAILED"


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------
def _fmt_evidence(entry: Any) -> str:
    """One evidence line for the prompt.

    The agent's evidence entries are dicts with varying keys; unknown shapes
    degrade to JSON rather than disappearing — an evidence item we cannot
    read is still something the model should know exists.
    """
    if not isinstance(entry, dict):
        return f"- {entry}"
    tool = entry.get("tool") or entry.get("name")
    verdict = entry.get("verdict")
    observation = entry.get("observation", entry.get("result", entry.get("summary")))
    if isinstance(observation, (dict, list)):
        observation = json.dumps(observation, ensure_ascii=False, default=str)
    elif observation is None:
        observation = json.dumps(entry, ensure_ascii=False, default=str)
    line = f"- {tool}: {observation}" if tool else f"- {observation}"
    if verdict:
        line += f" (verdict: {verdict})"
    return line[:600]


def _rca_query(req: RCARequest) -> str:
    """Retrieval query: service + symptom. Runbooks are written about
    failures, not about incident identifiers."""
    query = f"{req.primary_service} {req.symptom}".strip()
    return query or req.incident_id


def _rca_user_prompt(req: RCARequest, runbooks: List[Dict[str, Any]]) -> str:
    lines = [
        f"Incident: {req.incident_id}",
        f"Primary service: {req.primary_service}",
        f"Severity: {req.severity}",
        f"Symptom: {req.symptom or '(not provided)'}",
        "",
        "Evidence observed:",
    ]
    if req.evidence:
        lines += [_fmt_evidence(entry) for entry in req.evidence]
    else:
        lines.append("- (none)")

    lines += ["", "Runbook context provided by the caller:"]
    lines.append(req.runbook_context.strip() or "(none)")

    lines += ["", "Retrieved runbooks:"]
    if runbooks:
        for index, runbook in enumerate(runbooks, 1):
            lines += [
                f"[{index}] {runbook.get('title', 'runbook')}"
                f" ({runbook.get('filename', '')})",
                (runbook.get("content") or "")[:1500],
                "",
            ]
    else:
        lines.append("(none — retrieval found nothing relevant)")
    return "\n".join(lines)


def _postmortem_user_prompt(req: PostMortemRequest) -> str:
    lines = [
        f"Incident: {req.incident_id}",
        f"Ground-truth injected fault: {req.ground_truth_cause or '(unknown)'}",
        f"AI diagnosis: {req.ai_diagnosis or '(none produced)'}",
        "",
        "Timeline:",
    ]
    if req.timeline:
        lines += [f"- {entry}" for entry in req.timeline]
    else:
        lines.append("- (none provided)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------
def _load_json_object(raw: str) -> Optional[Dict[str, Any]]:
    """Parse a model reply as a JSON object, tolerating code fences and
    surrounding prose (models emit both even in JSON mode)."""
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            parsed = json.loads(text[start:end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return None
    return None


def _as_optional_str(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parse_rca(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """(diagnosis, narrative) from the model's reply.

    Non-JSON prose is kept as the narrative (status will say
    GENERATION_FAILED, because the diagnosis half is missing) — discarding
    what the model actually said would hide a real failure mode.
    """
    obj = _load_json_object(raw)
    if obj is None:
        text = (raw or "").strip()
        return None, (text or None)
    return _as_optional_str(obj.get("diagnosis")), _as_optional_str(obj.get("narrative"))


def _parse_postmortem(raw: str) -> Dict[str, str]:
    """Map the reply to {section: text}, keeping only non-empty strings —
    a section the model fudged into a list or number is dropped rather than
    stringified into something that looks like prose."""
    obj = _load_json_object(raw)
    if not obj:
        return {}
    return {
        key: value.strip()
        for key, value in obj.items()
        if isinstance(value, str) and value.strip()
    }


# ---------------------------------------------------------------------------
# Shared logic
# ---------------------------------------------------------------------------
def _resolve_confidence(requested: float, runbooks: List[Dict[str, Any]]) -> float:
    """The caller's number wins when it carries signal; otherwise fall back
    to the best retrieval similarity.

    The old stub treated 0.0 as "skip the gate" (0 < conf < min). In a real
    pipeline 0.0 means "nothing was retrieved" — the case that must block
    hardest — so the plain `< RCA_MIN_CONFIDENCE` comparison is the honest
    one, with retrieval similarity filling in when the caller scored nothing.
    """
    if requested > 0:
        return float(requested)
    if runbooks:
        return max(float(rb.get("similarity_score") or 0.0) for rb in runbooks)
    return 0.0


def _citation(runbook: Dict[str, Any]) -> str:
    title = runbook.get("title") or runbook.get("id") or "runbook"
    filename = runbook.get("filename")
    return f"{title} ({filename})" if filename else str(title)


# ---------------------------------------------------------------------------
# Contract endpoints
# ---------------------------------------------------------------------------
@router.get("/health", response_model=LLMHealth)
def llm_health() -> LLMHealth:
    """Real reachability probe: READY only when the server answers AND the
    configured model is pulled — a green health that would fail generation
    is worse than an honest red one."""
    if not settings.LLM_ENABLED:
        # Pass enabled explicitly: the schema default was captured at import
        # time, and a caller that toggles settings at runtime would otherwise
        # see enabled=True next to DISABLED.
        return LLMHealth(enabled=False, reachable=None, status="DISABLED")

    probe = _probe_ollama()
    if not probe["reachable"]:
        status = "UNREACHABLE"
    elif not probe["model_loaded"]:
        status = "MODEL_MISSING"
    else:
        status = "READY"
    return LLMHealth(
        enabled=True, reachable=probe["reachable"], status=status
    )


@router.post("/rca", response_model=RCAResponse)
def generate_rca(req: RCARequest) -> RCAResponse:
    """Evidence-grounded root-cause narrative, gated on retrieval confidence.

    Flow: retrieve runbooks -> resolve confidence -> gate -> generate. The
    gate is evaluated on confidence alone (module docstring); generation is
    still attempted below the gate so a human gets the draft while
    automated remediation stays blocked.
    """
    runbooks, _retrieval_error = _retrieve(_rca_query(req))

    confidence = _resolve_confidence(req.retrieval_confidence, runbooks)
    citations = [_citation(rb) for rb in runbooks]
    blocked = confidence < settings.RCA_MIN_CONFIDENCE

    if not settings.LLM_ENABLED:
        return RCAResponse(
            status="DISABLED",
            confidence=confidence,
            citations=citations,
            remediation_blocked=blocked,
        )

    try:
        raw = _call_ollama(RCA_SYSTEM_PROMPT, _rca_user_prompt(req, runbooks))
    except Exception as exc:
        logger.warning("RCA generation failed for %s: %s", req.incident_id, exc)
        return RCAResponse(
            status=_failure_status(),
            confidence=confidence,
            citations=citations,
            remediation_blocked=blocked,
        )

    diagnosis, narrative = _parse_rca(raw)
    return RCAResponse(
        status="OK" if diagnosis and narrative else "GENERATION_FAILED",
        diagnosis=diagnosis,
        narrative=narrative,
        confidence=confidence,
        citations=citations,
        remediation_blocked=blocked,
    )


@router.post("/postmortem", response_model=PostMortemResponse)
def generate_postmortem(req: PostMortemRequest) -> PostMortemResponse:
    """Structured post-mortem from an evaluated experiment.

    Sections are only ever populated by a successful generation: an empty
    sections dict behind an honest status beats six plausible-looking
    paragraphs nobody can attribute to a model.
    """
    if not settings.LLM_ENABLED:
        return PostMortemResponse(status="DISABLED")

    try:
        raw = _call_ollama(POSTMORTEM_SYSTEM_PROMPT, _postmortem_user_prompt(req))
    except Exception as exc:
        logger.warning("Post-mortem generation failed for %s: %s", req.incident_id, exc)
        return PostMortemResponse(status=_failure_status())

    sections = _parse_postmortem(raw)
    if sections.get("summary") and sections.get("root_cause"):
        return PostMortemResponse(
            status="OK",
            narrative=sections.get("summary"),
            sections=sections,
        )
    # Contract failed: keep whatever came back (and the raw reply when
    # nothing parsed) so the failure can be inspected instead of guessed at.
    logger.warning(
        "post-mortem reply for %s lacked the core sections (keys=%s)",
        req.incident_id, sorted(sections),
    )
    return PostMortemResponse(
        status="GENERATION_FAILED",
        narrative=sections.get("summary") or ((raw or "").strip() or None),
        sections=sections,
    )
