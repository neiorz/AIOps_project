"""Log anomaly detection — DeepLog-style next-template prediction.

The metrics model watches numbers; this one watches words. Every service
line is mined into a template (Drain in miniature: identifiers become
``<*>``), the per-service sequence of templates is learned as an order-N
transition model, and an event whose next template is not among the model's
top-k predictions is flagged.

No neural net is involved — predictions come from empirical transition
counts. That keeps training on CPU with zero extra dependencies, and the
counts *are* the model: inspectable, serializable with joblib, and honest
about what it has and has not seen.
"""
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.config import settings

PLACEHOLDER = "<*>"


def log_model_path() -> Path:
    """Where the trained log model and its sidecars live (next to the metrics
    model — ``model.joblib`` -> ``log_model.joblib``)."""
    return Path(settings.ML_MODEL_PATH).parent / "log_model.joblib"

_TS = re.compile(r"^\[\d{2}:\d{2}(?::\d{2})?\]")   # [06:55:17]
_HEX = re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE)
_DIGITS = re.compile(r"\d+")
_WS = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# template mining
# ---------------------------------------------------------------------------
def normalize_line(line: str) -> List[str]:
    """Strip the timestamp and turn every identifier into ``<*>``."""
    text = _TS.sub("", line.strip())
    text = _HEX.sub(PLACEHOLDER, text)
    text = _DIGITS.sub(PLACEHOLDER, text)
    return _WS.sub(" ", text).split()


def similarity(a: Sequence[str], b: Sequence[str]) -> float:
    """Position-wise agreement; only lines of equal shape can match."""
    if len(a) != len(b) or not a:
        return 0.0
    same = sum(1 for x, y in zip(a, b) if x == y)
    return same / len(a)


class TemplateStore:
    """Drain in miniature: same shape first, then position-wise similarity.

    ``observe`` learns (a new line becomes a new template). ``lookup`` is
    the frozen evaluation view: it maps onto the templates learned during
    training and returns a stable *negative* id for anything unseen, which
    is exactly the DeepLog signal — an event the model has no template for.

    Matching is exact on the normalized token sequence. These service lines
    are short and structured — every position is either a fixed verb/status
    or an endpoint — so a looser threshold would merge ``/cart/<*>`` with
    ``/checkout/<*>`` and erase precisely the structure the sequence model
    exists to learn.
    """

    SIMILARITY = 1.0

    def __init__(self, templates: Optional[List[Dict[str, Any]]] = None):
        self.templates: List[Dict[str, Any]] = templates if templates is not None else []
        self._oov: Dict[Tuple[str, ...], int] = {}

    def observe(self, line: str) -> int:
        """Template id of ``line``; mines a new template when nothing fits."""
        tokens = normalize_line(line)
        for tpl in self.templates:
            if (len(tpl["tokens"]) == len(tokens)
                    and similarity(tpl["tokens"], tokens) >= self.SIMILARITY):
                tpl["count"] += 1
                return tpl["id"]
        self.templates.append(
            {"id": len(self.templates), "tokens": tokens, "count": 1})
        return len(self.templates) - 1

    def lookup(self, line: str) -> int:
        """Frozen mapping: trained template id, or a negative out-of-vocab id."""
        tokens = tuple(normalize_line(line))
        for tpl in self.templates:
            if (len(tpl["tokens"]) == len(tokens)
                    and similarity(tpl["tokens"], tokens) >= self.SIMILARITY):
                return tpl["id"]
        if tokens not in self._oov:
            self._oov[tokens] = -(len(self._oov) + 1)
        return self._oov[tokens]

    def template_text(self, tid: int) -> str:
        if tid >= 0:
            return " ".join(self.templates[tid]["tokens"])
        return "<unseen template>"


# ---------------------------------------------------------------------------
# sequences -> transitions -> model
# ---------------------------------------------------------------------------
Context = Tuple[str, Tuple[int, ...]]      # (service, previous template ids)


def build_transitions(tids_by_service: Dict[str, List[int]],
                      window: int) -> Dict[Context, Counter]:
    """Count, per service, which template follows each context window."""
    transitions: Dict[Context, Counter] = {}
    for service, tids in tids_by_service.items():
        for i in range(window, len(tids)):
            ctx: Context = (service, tuple(tids[i - window:i]))
            transitions.setdefault(ctx, Counter())[tids[i]] += 1
    return transitions


def train_log_model(logs_by_service: Dict[str, List[str]],
                    window: int = 3, top_k: int = 3) -> Dict[str, Any]:
    """Mine templates, learn per-service transitions, return the model."""
    miner = TemplateStore()
    tids_by_service = {
        service: [miner.observe(line) for line in lines]
        for service, lines in logs_by_service.items()
    }
    return {
        "window": window,
        "top_k": top_k,
        "templates": miner.templates,
        "transitions": build_transitions(tids_by_service, window),
        "trained_at": time.time(),
        "services": {s: len(lines) for s, lines in logs_by_service.items()},
        "n_events": sum(len(lines) for lines in logs_by_service.values()),
    }


def top_k_predictions(model: Dict[str, Any], service: str,
                      context: Sequence[int]) -> List[int]:
    """The ``top_k`` most frequent continuations of ``context``, or []."""
    counts = model["transitions"].get((service, tuple(context)))
    if not counts:
        return []
    return [tid for tid, _ in counts.most_common(model["top_k"])]


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------
def evaluate(model: Dict[str, Any],
             logs_by_service: Dict[str, List[str]]) -> Dict[str, Any]:
    """Replay streams through the frozen model and score every event.

    An event is *flagged* when its actual next template is not among the
    model's top-k predictions — including when the context itself was never
    seen in training, or the line matches no trained template at all
    (negative id). Those are the anomalies; everything else is a hit.
    """
    miner = TemplateStore(model["templates"])
    window = model["window"]

    per_service: Dict[str, Dict[str, Any]] = {}
    totals = {"events": 0, "hits": 0, "flagged": 0, "unseen_template": 0}
    flagged_templates: Counter = Counter()      # which lines got flagged

    for service, lines in logs_by_service.items():
        tids = [miner.lookup(line) for line in lines]
        s_events = s_hits = s_flagged = s_unseen = 0
        for i in range(window, len(tids)):
            actual = tids[i]
            preds = top_k_predictions(model, service, tids[i - window:i])
            s_events += 1
            if actual in preds:
                s_hits += 1
            else:
                s_flagged += 1
                flagged_templates[miner.template_text(actual)] += 1
            if actual < 0:
                s_unseen += 1
        per_service[service] = {
            "events": s_events,
            "hits": s_hits,
            "flagged": s_flagged,
            "unseen_template": s_unseen,
            "next_event_accuracy": round(s_hits / s_events, 4) if s_events else None,
            "flagged_rate": round(s_flagged / s_events, 4) if s_events else None,
        }
        totals["events"] += s_events
        totals["hits"] += s_hits
        totals["flagged"] += s_flagged
        totals["unseen_template"] += s_unseen

    n = totals["events"]
    return {
        "events": n,
        "next_event_accuracy": round(totals["hits"] / n, 4) if n else None,
        "flagged_rate": round(totals["flagged"] / n, 4) if n else None,
        "unseen_template_events": totals["unseen_template"],
        "unseen_template_rate": round(totals["unseen_template"] / n, 4) if n else None,
        "flagged_templates": dict(flagged_templates.most_common(10)),
        "per_service": per_service,
    }


def chronological_split(logs_by_service: Dict[str, List[str]],
                        ratio: float = 0.8) -> Tuple[Dict[str, List[str]],
                                                     Dict[str, List[str]]]:
    """Time-ordered split: the model only ever sees the past."""
    if not 0.0 < ratio < 1.0:
        raise ValueError(f"ratio must be between 0 and 1, got {ratio}")
    train, test = {}, {}
    for service, lines in logs_by_service.items():
        cut = int(len(lines) * ratio)
        train[service], test[service] = lines[:cut], lines[cut:]
    return train, test
