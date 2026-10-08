"""
Held-out evaluation for the anomaly model (Track T5 remake).

The original T5 could only report *in-sample* numbers — how many training
rows the model flagged — because it had no labels. The collected datasets
have them, so this module measures the model the way it should be measured:
train on healthy rows, score rows the model has never seen.

Two kinds of number, deliberately kept apart:

**Threshold-free** — AUROC. Answers "do fault rows score more anomalous than
healthy rows?" regardless of where the decision cut is placed. This is the
number that says whether the model learned anything.

**At the configured cut** — precision / recall / F1 / false-positive rate.
These *do* depend on ``settings.ML_CONTAMINATION``, so the report prints that
value alongside them. A false-positive rate is not a discovery when the
threshold was set to produce it.

No metric here is ever clamped, defaulted, or rounded upward. When a number
cannot be computed (one class missing, empty test set) it is reported as
``None`` with a stated reason, not as a reassuring zero.
"""

import logging
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from app.ml.collector import MODEL_FEATURES

logger = logging.getLogger(__name__)


def feature_matrix(rows: Sequence[Dict[str, Any]]) -> np.ndarray:
    """Rows -> the exact feature matrix the model was trained on."""
    return np.array(
        [[float(r.get(name, 0.0) or 0.0) for name in MODEL_FEATURES] for r in rows],
        dtype=float,
    )


def evaluate(
    model_bundle: Dict[str, Any],
    normal_test: Sequence[Dict[str, Any]],
    chaos_test: Sequence[Dict[str, Any]],
    contamination: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Score held-out healthy and fault rows and return every metric.

    :param model_bundle: what :func:`app.ml.train.train_model` persisted
        (``{"model": ..., "feature_names": [...]}``).
    :param normal_test: healthy rows the model has never seen.
    :param chaos_test: labelled fault rows the model has never seen.
    :param contamination: echoed into the report, because it sets the cut.
    :return: a flat dict of metrics; ``None`` where a value is undefined.
    """
    model = model_bundle["model"]
    features = list(model_bundle.get("feature_names") or MODEL_FEATURES)

    n_normal, n_chaos = len(normal_test), len(chaos_test)
    report: Dict[str, Any] = {
        "model_features": features,
        "n_normal_test": n_normal,
        "n_chaos_test": n_chaos,
        "contamination": contamination,
        "note": None,
    }

    if n_normal + n_chaos == 0:
        report["note"] = "no test rows supplied"
        return report

    # Assemble in a stable order and remember which index is which, so a
    # feature-set change cannot silently misalign the labels.
    combined = list(normal_test) + list(chaos_test)
    X = feature_matrix(combined)
    y_true = np.array([0] * n_normal + [1] * n_chaos, dtype=int)

    # score_samples: lower = more anomalous. Negate so "bigger = worse",
    # which is what roc_auc_score expects for the positive class.
    raw_scores = model.score_samples(X)
    anomaly_scores = -np.asarray(raw_scores, dtype=float)
    predicted = (model.predict(X) == -1).astype(int)

    # --- threshold-free -------------------------------------------------
    if n_normal and n_chaos:
        from sklearn.metrics import roc_auc_score
        auroc: Optional[float] = float(roc_auc_score(y_true, anomaly_scores))
    else:
        auroc = None
        report["note"] = (
            "AUROC undefined: both a healthy and a fault class are required"
        )

    # --- confusion matrix at the configured cut --------------------------
    tp = int(((predicted == 1) & (y_true == 1)).sum())
    fp = int(((predicted == 1) & (y_true == 0)).sum())
    tn = int(((predicted == 0) & (y_true == 0)).sum())
    fn = int(((predicted == 0) & (y_true == 1)).sum())
    total = tp + fp + tn + fn

    def _ratio(num: int, den: int) -> Optional[float]:
        return round(num / den, 4) if den else None

    report.update({
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "accuracy": _ratio(tp + tn, total),
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
        "f1": None,
        "false_positive_rate": _ratio(fp, fp + tn),
        "auroc": round(auroc, 4) if auroc is not None else None,
    })

    precision, recall = report["precision"], report["recall"]
    if precision is None or recall is None or (precision + recall) == 0:
        report["f1"] = None
    else:
        report["f1"] = round(2 * precision * recall / (precision + recall), 4)

    # --- score separation (the actual evidence the model learned) --------
    normal_scores = anomaly_scores[:n_normal]
    chaos_scores = anomaly_scores[n_normal:]
    report["mean_score_normal"] = (
        round(float(normal_scores.mean()), 4) if n_normal else None
    )
    report["mean_score_chaos"] = (
        round(float(chaos_scores.mean()), 4) if n_chaos else None
    )
    report["score_gap"] = (
        round(report["mean_score_chaos"] - report["mean_score_normal"], 4)
        if n_normal and n_chaos else None
    )

    return report


def format_report(report: Dict[str, Any]) -> str:
    """Render a metric dict as an aligned, human-readable block."""
    lines = [f"  held-out evaluation  ({report.get('note') or 'ok'})"]

    def _row(label: str, value: Any) -> None:
        if value is None and label.endswith(("AUROC", "F1")):
            value = "undefined"
        lines.append(f"    {label:<34}{value}")

    _row("healthy test rows", report.get("n_normal_test"))
    _row("fault test rows", report.get("n_chaos_test"))
    _row("threshold (contamination)", report.get("contamination"))
    lines.append("    " + "-" * 44)
    _row("AUROC (threshold-free)", report.get("auroc"))
    _row("mean score, healthy", report.get("mean_score_normal"))
    _row("mean score, fault", report.get("mean_score_chaos"))
    _row("score gap (fault - healthy)", report.get("score_gap"))
    lines.append("    " + "-" * 44)
    _row("caught faults (recall)", report.get("recall"))
    _row("false alarms on healthy", report.get("false_positive_rate"))
    _row("precision", report.get("precision"))
    _row("F1", report.get("f1"))
    _row("accuracy", report.get("accuracy"))
    _row("TP / FP / TN / FN",
         f"{report.get('tp')} / {report.get('fp')} / "
         f"{report.get('tn')} / {report.get('fn')}")
    return "\n".join(lines)


def is_stronger(candidate: Dict[str, Any], incumbent: Dict[str, Any]) -> Optional[bool]:
    """
    Does ``candidate`` beat ``incumbent``?

    Ranked by AUROC first (threshold-free), then by F1. Returns ``None``
    when neither can be compared — never invents a winner.

    Both arguments must come from the **same set of rows**. The metric is
    only meaningful relative to what was scored: pooled across healthy
    bystanders and genuinely faulted services, AUROC is compressed by the
    bystanders and two models that differ sharply on real faults can land
    within 0.006 of each other. Pass the operationally relevant view.
    """
    for key in ("auroc", "f1"):
        a, b = candidate.get(key), incumbent.get(key)
        if a is None or b is None:
            continue
        if a != b:
            return a > b
    return None
