"""
Decision-threshold calibration (Track T5 remake).

WHY THIS EXISTS
---------------
IsolationForest picks its own cut at fit time out of ``contamination``: it
sets ``offset_`` so roughly that fraction of the *training* rows get called
anomalous. That is sound when training and serving data come from the same
world.

They do not here. The labelled corpus was collected in an environment whose
resident memory peaks at 165 MB; this mesh idles at 214-265 MB. Fitted on
the corpus alone the model treats **every** live reading as an anomaly —
measured, not assumed: 330 of 330 rows flagged.

Training on corpus + live corrects the fit, but the cut is still whatever
the training mix happened to imply. Calibration moves the line on purpose:

    set ``offset_`` so that ``target_rate`` of held-out LIVE traffic is
    flagged.

``predict`` flags a row exactly when ``score_samples(X) < offset_``, so
taking the ``target_rate`` quantile of live scores achieves that directly.

NOTHING ABOUT THE MODEL'S SHAPE CHANGES. The trees are untouched; only
where the line is drawn. Every consumer already reads that line through
``decision_function`` / ``predict`` (see app/ml/scoring.py), so one
assignment applies the calibrated cut everywhere at once, including the
frozen anomaly API.

The corpus still supplies the *detection* power — calibration only decides
how loud the alarm is.
"""

import logging
from typing import Any, Dict, Optional, Sequence

import numpy as np

from app.ml.evaluation import feature_matrix

logger = logging.getLogger(__name__)


def calibrate(
    model: Any,
    rows: Sequence[Dict[str, Any]],
    target_rate: float,
) -> Dict[str, Any]:
    """Move the model's decision cut so ``target_rate`` of ``rows`` flag.

    ``rows`` must be normal traffic the model did **not** train on, or the
    quantile measures the fit rather than what serving will see.

    :param model: a fitted sklearn anomaly detector (mutated in place).
    :param rows: held-out normal rows from the serving environment.
    :param target_rate: fraction of those rows that may be flagged,
        e.g. ``0.05`` for 5%.
    :return: what was requested, what was set, and what actually happened.
    :raises ValueError: on a nonsensical rate or an empty calibration set.
    """
    if not 0.0 < target_rate < 1.0:
        raise ValueError(f"target_rate must be strictly between 0 and 1, got {target_rate}")
    if not rows:
        raise ValueError(
            "cannot calibrate without rows: pass held-out normal traffic from "
            "the serving environment"
        )

    scores = np.asarray(model.score_samples(feature_matrix(rows)), dtype=float)
    offset = float(np.percentile(scores, 100.0 * target_rate))

    model.offset_ = offset

    # Recompute from the model's own rule rather than trusting the quantile,
    # so ties and duplicate scores are reported as they really fall out.
    flagged = int((model.predict(feature_matrix(rows)) == -1).sum())
    actual = round(flagged / len(rows), 4)

    result = {
        "target_rate": target_rate,
        "offset": offset,
        "n_rows": len(rows),
        "flagged": flagged,
        "actual_rate": actual,
    }
    logger.info(
        "calibrated threshold: target %.3f -> %.4f on %d live rows (offset=%.6f)",
        target_rate, actual, len(rows), offset,
    )
    return result


def flag_rate(
    model: Any,
    rows: Sequence[Dict[str, Any]],
) -> Optional[float]:
    """Fraction of ``rows`` the model currently calls anomalous.

    Returns ``None`` when there is nothing to measure, so a gate comparing
    this against a limit reports 'undefined' instead of quietly passing
    on an empty set.
    """
    if not rows:
        return None
    return round(float((model.predict(feature_matrix(rows)) == -1).mean()), 4)
