"""
Isolation Forest training (Track T5).

Fits on whatever real rows the collector produced and writes the fitted model
plus its feature contract to settings.ML_MODEL_PATH.

Nothing here fabricates data: if there are too few samples, training fails
loudly instead of returning a fake "trained" model.
"""
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest

from app.config import settings
from app.ml.collector import FEATURES_CSV, MODEL_FEATURES, load_features

logger = logging.getLogger(__name__)

# IsolationForest needs a meaningful sample to learn normal behaviour.
MIN_SAMPLES = 12


def model_meta_path(model_path: Optional[Path] = None) -> Path:
    """Derived at call time so tests can redirect settings.ML_MODEL_PATH.

    Accepts an explicit path so a candidate model's metadata lands beside
    *that* candidate instead of overwriting the production model's record.
    """
    target = Path(model_path) if model_path else Path(settings.ML_MODEL_PATH)
    return target.with_suffix(".meta.json")


def _to_matrix(rows: List[Dict[str, Any]]) -> np.ndarray:
    return np.array(
        [[float(r.get(name, 0.0) or 0.0) for name in MODEL_FEATURES] for r in rows],
        dtype=float,
    )


def train_model(
    rows: List[Dict[str, Any]] = None,
    model_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Fit IsolationForest and persist it. Raises ValueError when too small.

    ``model_path`` defaults to the production location, so ordinary training
    behaves exactly as before. Passing a path trains a *candidate* that can be
    evaluated against the incumbent before anything in production changes.
    """
    if rows is None:
        rows = load_features()
    if len(rows) < MIN_SAMPLES:
        raise ValueError(
            f"Need at least {MIN_SAMPLES} samples to train, got {len(rows)}. "
            f"Run: python -m app.ml.collector --samples 30 --interval 1"
            + (f" (looked in {FEATURES_CSV})" if not rows else "")
        )

    X = _to_matrix(rows)

    # A feature that never varies during training can never be split on —
    # the trees ignore it silently, which would let us claim to monitor
    # something we effectively do not. Surface it instead of hiding it.
    constant = [MODEL_FEATURES[i] for i in range(X.shape[1]) if np.ptp(X[:, i]) == 0.0]
    if constant:
        logger.warning(
            "Zero-variance features excluded from detection by the trees: %s",
            ", ".join(constant),
        )

    model = IsolationForest(
        n_estimators=200,
        contamination=settings.ML_CONTAMINATION,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X)

    # Diagnostics on the training set itself (honest - this is in-sample).
    preds = model.predict(X)
    scores = model.decision_function(X)
    flagged = int((preds == -1).sum())

    target = Path(model_path) if model_path else Path(settings.ML_MODEL_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "model": model,
            "feature_names": list(MODEL_FEATURES),
            "trained_at": time.time(),
            "n_samples": len(rows),
        },
        target,
    )

    # A fresh fit must never be served from a stale cache.
    from app.ml.scoring import invalidate_cache
    invalidate_cache()

    meta = {
        "trained_at": time.time(),
        "n_samples": len(rows),
        "algorithm": "IsolationForest",
        "contamination": settings.ML_CONTAMINATION,
        "feature_names": list(MODEL_FEATURES),
        "flagged_in_training": flagged,
        "constant_features": constant,
        "score_min": float(np.min(scores)),
        "score_max": float(np.max(scores)),
        "model_path": str(target),
    }
    model_meta_path(target).write_text(json.dumps(meta, indent=2))
    return meta


def main() -> None:
    meta = train_model()
    print(f"  trained on {meta['n_samples']} samples")
    print(f"  flagged {meta['flagged_in_training']} as anomalous in-sample")
    print(f"  decision range: {meta['score_min']:.4f} .. {meta['score_max']:.4f}")
    print(f"  saved to {settings.ML_MODEL_PATH}")


if __name__ == "__main__":
    main()
