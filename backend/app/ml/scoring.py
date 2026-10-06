"""
Model loading + scoring (Track T5).

Single place that knows the feature contract, so the API handlers in
app/api/anomalies.py stay thin and the schemas stay frozen.
"""
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import joblib
import numpy as np

from app.config import settings
from app.ml.collector import ANOMALIES_JSON, MODEL_FEATURES
from app.ml.train import model_meta_path

_CACHE: Optional[Dict[str, Any]] = None


def load_model() -> Optional[Dict[str, Any]]:
    """Load the fitted bundle. Returns None when T5 has not trained yet."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    path = Path(settings.ML_MODEL_PATH)
    if not path.exists():
        return None
    try:
        _CACHE = joblib.load(path)
    except Exception:
        return None
    return _CACHE


def invalidate_cache() -> None:
    global _CACHE
    _CACHE = None


def is_model_loaded() -> bool:
    return load_model() is not None


def model_status() -> Dict[str, Any]:
    """Populates the frozen ModelStatus schema."""
    bundle = load_model()
    trained_at = None
    n_samples = None
    if bundle:
        trained_at = bundle.get("trained_at")
        n_samples = bundle.get("n_samples")
    elif model_meta_path().exists():
        try:
            meta = json.loads(model_meta_path().read_text())
            trained_at = meta.get("trained_at")
            n_samples = meta.get("n_samples")
        except Exception:
            pass
    return {
        "loaded": bundle is not None,
        "algorithm": "IsolationForest",
        "model_path": str(settings.ML_MODEL_PATH),
        "trained_at": trained_at,
        "feature_window": settings.ML_FEATURE_WINDOW,
        "contamination": settings.ML_CONTAMINATION,
        "training_samples": n_samples,
    }


def _to_matrix(feature_dicts: List[Dict[str, float]]) -> np.ndarray:
    """Order columns exactly as they were during training."""
    return np.array(
        [[float(fd.get(name, 0.0) or 0.0) for name in MODEL_FEATURES]
         for fd in feature_dicts],
        dtype=float,
    )


def score(feature_dicts: List[Dict[str, float]]) -> Tuple[List[float], List[bool]]:
    """
    Returns (anomaly_score, is_anomaly) aligned with the input list.
    sklearn's decision_function: lower = more anomalous, so we negate it so a
    HIGHER score means MORE anomalous (what callers expect).
    Raises RuntimeError when no model exists yet.
    """
    bundle = load_model()
    if bundle is None:
        raise RuntimeError(
            "No model trained yet. POST /api/v1/anomalies/train first "
            "(requires app/ml/data/features.csv)."
        )
    X = _to_matrix(feature_dicts)
    raw = bundle["model"].decision_function(X)
    preds = bundle["model"].predict(X)
    return [-float(v) for v in raw], [bool(p == -1) for p in preds]


# ---------------------------------------------------------------------------
# Persisted anomaly log (feeds the Streamlit timeline)
# ---------------------------------------------------------------------------
def append_anomalies(points: List[Dict[str, Any]]) -> None:
    if not points:
        return
    ANOMALIES_JSON.parent.mkdir(parents=True, exist_ok=True)
    existing = load_anomalies()
    existing.extend(points)
    # keep the most recent 500 so the file cannot grow without bound
    ANOMALIES_JSON.write_text(json.dumps(existing[-500:], indent=2))


def load_anomalies() -> List[Dict[str, Any]]:
    if not ANOMALIES_JSON.exists():
        return []
    try:
        data = json.loads(ANOMALIES_JSON.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def reset_anomalies() -> None:
    if ANOMALIES_JSON.exists():
        ANOMALIES_JSON.unlink()


def now() -> float:
    return time.time()
