"""
Labeled dataset loading and schema mapping (Track T5 remake).

The collected datasets use a slightly different vocabulary from the live
collector in :mod:`app.ml.collector`, and — critically — a different latency
unit:

    collected CSV        our feature row
    ------------------   --------------------------
    service_name    ->   service
    latency_ms      ->   latency_s   (value / 1000)
    cpu_percent     ->   cpu_percent  (already %)
    memory_mb       ->   memory_mb    (already MiB)
    error_rate      ->   error_rate  (already 0..1)
    service_up      ->   service_up   (already 0/1)

Feeding the raw file straight to the model would send latency in as
20.65 instead of 0.02065 — one thousand times too large — and every
prediction would be meaningless. The conversion therefore happens exactly
once, here, at load time, and is asserted by the tests.

Labels (``is_anomaly``, ``fault_type``, ``root_cause_service``) ride along in
the returned rows so the same loader serves both training and evaluation.
``train_model`` only ever reads :data:`app.ml.collector.MODEL_FEATURES`, so
the extra keys are ignored during fitting and available afterwards.
"""

import csv
import logging
import random
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from app.ml.collector import DATA_DIR

logger = logging.getLogger(__name__)

#: Friendly name -> file on disk. ``combined_telemetry_dataset.csv`` is
#: deliberately absent: it is byte-identical to ``aiops_chaos_dataset.csv``.
DATASETS: Dict[str, str] = {
    "normal": "normal_baseline_dataset.csv",
    "chaos": "chaos_labeled_dataset.csv",
    "master": "aiops_chaos_dataset.csv",
}

#: Columns the loader must find in every source file.
REQUIRED_COLUMNS = ("timestamp", "service_name", "cpu_percent", "memory_mb",
                    "latency_ms", "error_rate", "service_up")

#: Collected name -> our name. Anything not listed is passed through as-is.
_COLUMN_MAP = {
    "service_name": "service",
    "latency_ms": "latency_s",
}

#: Label columns preserved for evaluation, never used as model inputs.
LABEL_COLUMNS = ("is_anomaly", "fault_type", "root_cause_service")

#: 1 ms = 0.001 s. Applied only to the latency column.
_LATENCY_SCALE = 1000.0


class SchemaError(ValueError):
    """The source file does not have the columns this loader requires."""


def dataset_path(name: str) -> Path:
    """Absolute path of a named dataset."""
    try:
        filename = DATASETS[name]
    except KeyError:
        raise SchemaError(
            f"unknown dataset {name!r}; available: {', '.join(sorted(DATASETS))}"
        ) from None
    return DATA_DIR / filename


def map_row(raw: Dict[str, str]) -> Dict[str, Any]:
    """Convert one raw CSV row into our feature vocabulary.

    Raises :class:`SchemaError` naming the columns that were missing rather
    than silently defaulting them — a typo'd header must not turn into a row
    of zeros that quietly pollutes the training set.
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in raw]
    if missing:
        raise SchemaError(
            f"dataset row is missing required column(s): {', '.join(missing)}. "
            f"Found: {', '.join(raw.keys())}"
        )

    out: Dict[str, Any] = {}
    for source, value in raw.items():
        target = _COLUMN_MAP.get(source, source)
        if source == "latency_ms":
            out[target] = float(value) / _LATENCY_SCALE
        elif source == "service_name":
            out[target] = value
        elif source in LABEL_COLUMNS:
            out[target] = value
        elif source == "timestamp":
            out[source] = float(value)
        else:
            try:
                out[target] = float(value)
            except (TypeError, ValueError):
                raise SchemaError(
                    f"column {source!r} has non-numeric value {value!r}"
                ) from None
    return out


def load_labeled(name: str) -> List[Dict[str, Any]]:
    """Load and schema-map a whole dataset by name."""
    path = dataset_path(name)
    if not path.exists():
        raise SchemaError(
            f"dataset {name!r} not found at {path}. Copy it into app/ml/data/ "
            f"first (it is tracked in git, so a fresh clone already has it)."
        )

    rows: List[Dict[str, Any]] = []
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        missing = [c for c in REQUIRED_COLUMNS if c not in header]
        if missing:
            raise SchemaError(
                f"{path.name} is missing column(s): {', '.join(missing)}. "
                f"Header was: {', '.join(header)}"
            )
        for i, raw in enumerate(reader, start=2):   # start=2: row 1 is the header
            try:
                rows.append(map_row(raw))
            except SchemaError as exc:
                raise SchemaError(f"{path.name} line {i}: {exc}") from None

    logger.info("loaded %d rows from %s", len(rows), path.name)
    return rows


def split_rows(
    rows: List[Dict[str, Any]], ratio: float = 0.8, seed: int = 42
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Deterministic train/test split (default 80/20).

    Shuffled with a fixed seed so a re-run reproduces the same split — an
    evaluation whose numbers change every run is not evidence of anything.
    """
    if not 0.0 < ratio < 1.0:
        raise ValueError(f"ratio must be between 0 and 1, got {ratio}")
    if not rows:
        return [], []

    shuffled = list(rows)
    random.Random(seed).shuffle(shuffled)
    cut = int(len(shuffled) * ratio)
    return shuffled[:cut], shuffled[cut:]


def describe(rows: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
    """Small summary dict, used by the retrain report and its tests."""
    if not rows:
        return {"label": label, "rows": 0}

    def _values(key: str) -> List[float]:
        return [float(r[key]) for r in rows if key in r]

    summary: Dict[str, Any] = {"label": label, "rows": len(rows)}
    for key in ("cpu_percent", "memory_mb", "latency_s", "error_rate", "service_up"):
        vals = _values(key)
        if not vals:
            continue
        mean = sum(vals) / len(vals)
        summary[key] = {
            "min": min(vals),
            "max": max(vals),
            "mean": round(mean, 6),
            "stddev": round(
                (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5, 6
            ),
        }
    # A zero-variance feature cannot be split on by the trees; surface it so
    # nobody believes we are monitoring something we effectively are not.
    summary["constant_features"] = sorted(
        k for k, v in summary.items()
        if isinstance(v, dict) and v["stddev"] == 0.0
    )
    return summary


def partition_chaos(
    rows: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split labelled fault rows into faulted-service and bystander rows.

    A chaos run labels every service's row ``is_anomaly=1``, but only one
    service was actually broken. In ``chaos_labeled_dataset.csv`` that is a
    320 / 1,280 split: the 320 culprit rows show error rates ~450x normal,
    while the 1,280 bystanders are up, running, and merely mildly degraded.

    Pooling them makes a *correct* model look mediocre, because 80% of the
    "positive" class is rows where almost nothing is wrong. Evaluating the
    two separately answers the operational question — can we find the broken
    service? — instead of asking the model to treat healthy bystanders as
    faults it must flag.

    Rows whose ``root_cause_service`` is empty or ``none`` land in the
    bystander bucket, so this stays correct if it is ever pointed at
    unlabelled data.

    :return: ``(culprit_rows, bystander_rows)``
    """
    culprit: List[Dict[str, Any]] = []
    bystander: List[Dict[str, Any]] = []
    for row in rows:
        culprit_service = str(row.get("root_cause_service") or "").strip()
        service = str(row.get("service") or "").strip()
        is_culprit = (
            culprit_service
            and culprit_service.lower() != "none"
            and service == culprit_service
        )
        (culprit if is_culprit else bystander).append(row)
    return culprit, bystander
