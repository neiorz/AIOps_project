"""
Retrain the anomaly model on the collected labeled datasets (T5 remake).

    python -m app.ml.retrain                 # train, evaluate, promote if it wins
    python -m app.ml.retrain --dry-run       # report only, never touch production
    python -m app.ml.retrain --force         # skip the incumbent comparison

WHY THIS EXISTS
---------------
The first T5 model was fitted on 330 rows scraped from the live mesh with no
labels at all, so the only number it could honestly report was "how many
training rows it flagged" — in-sample, and not evidence of anything.

THIS ROUTINE RUNS THREE SEPARATE STAGES.

**1. Fit on two environments.** The labelled corpus and this mesh are not the
same world: the corpus peaks at 165 MB resident memory, live idles at 214-265
MB. Fitted on the corpus alone, the model flagged 330 of 330 live rows as
anomalous. Live rows therefore join the training mix, so the model recognises
both baselines as normal. The corpus still supplies every label the
evaluation needs, and labels are never shown to the model.

**2. Calibrate the cut.** Fitting sets the decision threshold from whatever
the training mix implied. app/ml/calibration.py moves it to the quantile that
flags `ML_CONTAMINATION` of held-out live traffic — measured, not assumed.

**3. Gate the promotion.** Two checks, neither overridable by --force:

    live safety   held-out live rows (neither fitted nor calibrated) must
                  flag at or below LIVE_FLAG_RATE_LIMIT, or the anomaly
                  feed becomes wallpaper
    detection     faulted-service AUROC and recall must clear their floors,
                  or the swap buys nothing

Only then is the candidate compared against the incumbent on the
faulted-service view — the pooled numbers are compressed by 1,280 healthy
bystander rows and cannot separate the two models.

IT WILL NOT SILENTLY OVERWRITE A WORKING MODEL. The candidate is trained to a
scratch file; production changes only when every gate passes and the
candidate genuinely wins. Nothing is clamped and no loser is quietly
installed. --force skips the incumbent comparison only — it will never
install a model that floods live scoring or cannot detect faults.
"""

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import joblib

from app.config import settings
from app.ml.calibration import calibrate, flag_rate
from app.ml.collector import DATA_DIR, load_features
from app.ml.datasets import describe, load_labeled, partition_chaos, split_rows
from app.ml.evaluation import evaluate, format_report, is_stronger
from app.ml.train import train_model

logger = logging.getLogger(__name__)

#: Where a candidate is fitted before it has earned a place in production.
CANDIDATE_PATH: Path = DATA_DIR / "candidate.joblib"

DEFAULT_RATIO = 0.8
DEFAULT_SEED = 42

#: Share of live rows used for fitting. The remainder splits into a slice
#: that calibrates the threshold and a slice that verifies it — a cut chosen
#: and measured on the same rows proves nothing.
LIVE_TRAIN_RATIO = 0.6

#: A candidate may flag at most this share of held-out live traffic. Above
#: it the anomaly feed stops being a signal and becomes wallpaper. This gate
#: is NOT bypassed by --force: forcing a model that floods production is not
#: a decision worth making from a flag.
LIVE_FLAG_RATE_LIMIT = 0.15

#: The candidate must actually detect faults, or there is no point replacing
#: a model that already runs.
DETECTION_AUROC_MIN = 0.95
DETECTION_RECALL_MIN = 0.90


def _load_bundle(path: Path) -> Optional[Dict[str, Any]]:
    """Load a persisted model bundle, or None when it does not exist yet."""
    if not path.exists():
        return None
    try:
        bundle = joblib.load(path)
    except Exception as exc:                       # noqa: BLE001
        logger.warning("could not load existing model %s: %s", path, exc)
        return None
    return bundle if isinstance(bundle, dict) and bundle.get("model") is not None else None


def _record_calibration(model_path: Path, calibration: Dict[str, Any]) -> None:
    """Fold the calibration result into the model's sidecar metadata.

    The threshold is part of what a model *is*. Leaving it out of the
    record would mean a later reader sees a contamination value and assumes
    it came from fitting, when in fact the cut was moved afterwards against
    live traffic.
    """
    meta_path = model_path.with_suffix(".meta.json")
    if not meta_path.exists():
        return
    try:
        meta = json.loads(meta_path.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("could not update %s with calibration", meta_path)
        return
    meta["calibration"] = calibration
    meta_path.write_text(json.dumps(meta, indent=2))


def run(
    ratio: float = DEFAULT_RATIO,
    seed: int = DEFAULT_SEED,
    dry_run: bool = False,
    force: bool = False,
    candidate_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Execute the full retrain and return a machine-readable summary.

    ``candidate_path`` overrides where the candidate is fitted — tests pass a
    throwaway directory so nothing is written into ``app/ml/data``.
    """
    cand_path = Path(candidate_path) if candidate_path else CANDIDATE_PATH
    # --- data ----------------------------------------------------------
    healthy = load_labeled("normal")
    faults = load_labeled("chaos")
    live = load_features()

    train_healthy, test_healthy = split_rows(healthy, ratio=ratio, seed=seed)

    # The labelled corpus and this mesh are DIFFERENT ENVIRONMENTS: the
    # corpus peaks at 165 MB resident memory, live idles at 214-265 MB.
    # Fitted on the corpus alone the model flagged 330/330 live rows, so
    # live rows join the training mix. They split three ways — fit, then
    # calibrate the threshold, then verify it on rows used for neither.
    live_train, live_holdout = split_rows(live, ratio=LIVE_TRAIN_RATIO, seed=seed)
    live_cal, live_verify = split_rows(live_holdout, ratio=0.5, seed=seed)

    summary: Dict[str, Any] = {
        "train_healthy_rows": len(train_healthy),
        "test_healthy_rows": len(test_healthy),
        "test_fault_rows": len(faults),
        "live_rows": len(live),
        "live_train_rows": len(live_train),
        "live_calibrate_rows": len(live_cal),
        "live_verify_rows": len(live_verify),
        "ratio": ratio,
        "seed": seed,
        "replaced": False,
        "reason": None,
    }

    print(f"\n  data")
    print(f"    healthy corpus      : {len(healthy)} rows "
          f"-> train {len(train_healthy)} / held-out {len(test_healthy)}")
    print(f"    labelled fault rows : {len(faults)} (all held out)")
    print(f"    live mesh store     : {len(live)} rows "
          f"-> fit {len(live_train)} / calibrate {len(live_cal)} "
          f"/ verify {len(live_verify)}")
    for block in (describe(train_healthy, "train"),
                  describe(test_healthy, "held-out healthy")):
        if block.get("constant_features"):
            print(f"    constant features in {block['label']}: "
                  f"{', '.join(block['constant_features'])}")
            print(f"      -> the trees cannot split on these; they are NOT monitored")

    # --- incumbent ------------------------------------------------------
    production = Path(settings.ML_MODEL_PATH)
    incumbent = _load_bundle(production)
    incumbent_report = None
    if incumbent is not None:
        incumbent_report = evaluate(
            incumbent, test_healthy, faults,
            contamination=settings.ML_CONTAMINATION,
        )
        print(f"\n  incumbent  ({production.name}, "
              f"trained on {incumbent.get('n_samples')} rows)")
        print(format_report(incumbent_report))
    else:
        print("\n  incumbent  : none (no model at production path yet)")

    # --- candidate ------------------------------------------------------
    # The labelled corpus supplies detection power; the live rows stop the
    # model mistaking this mesh's normal shape for an anomaly.
    meta = train_model(rows=train_healthy + live_train, model_path=cand_path)
    candidate = _load_bundle(cand_path)
    if candidate is None:
        # We fitted this file on the line above, so this means the write or
        # the read went wrong. Fail loudly rather than evaluate None.
        raise RuntimeError(
            f"candidate was trained to {cand_path} but could not be loaded back"
        )

    # --- calibrate the decision cut --------------------------------------
    # Fitting set the cut from whatever the training mix implied. We want it
    # set from what live traffic looks like. calibrate() mutates the
    # in-memory model, so the bundle is written straight back — otherwise
    # production would reload the uncalibrated offset_ and the whole step
    # would be a local illusion.
    calibration = calibrate(
        candidate["model"], live_cal,
        target_rate=settings.ML_CONTAMINATION,
    )
    joblib.dump(candidate, cand_path)
    _record_calibration(cand_path, calibration)

    # Verify on rows that were neither fitted nor calibrated against.
    live_flag_rate = flag_rate(candidate["model"], live_verify)

    candidate_report = evaluate(
        candidate, test_healthy, faults,
        contamination=settings.ML_CONTAMINATION,
    )
    print(f"\n  candidate  ({cand_path.name}, trained on {meta['n_samples']} rows: "
          f"{len(train_healthy)} corpus + {len(live_train)} live)")
    if meta["constant_features"]:
        print(f"    constant features: {', '.join(meta['constant_features'])}")
    print(f"    calibrated cut   : target "
          f"{calibration['target_rate']:.0%} -> "
          f"{calibration['actual_rate']:.1%} on {calibration['n_rows']} "
          f"calibration rows")
    print(f"    held-out live    : {live_flag_rate if live_flag_rate is not None else 'n/a'} "
          f"flagged (limit {LIVE_FLAG_RATE_LIMIT:.0%})")
    print(format_report(candidate_report))

    summary["candidate_meta"] = meta
    summary["candidate_report"] = candidate_report
    summary["incumbent_report"] = incumbent_report
    summary["calibration"] = calibration
    summary["live_flag_rate"] = live_flag_rate

    # --- fault-class views ----------------------------------------------
    # Pooling the faulted service with the healthy bystanders from the same
    # chaos run buries the model's real capability, so each view is scored
    # on its own and both models are shown side by side.
    culprit, bystander = partition_chaos(faults)
    views = {
        "pooled": faults,
        "faulted service only": culprit,
        "bystanders only": bystander,
    }
    summary["n_culprit_rows"] = len(culprit)
    summary["n_bystander_rows"] = len(bystander)

    table: List[Dict[str, Any]] = []
    for label, rows in views.items():
        cand = evaluate(candidate, test_healthy, rows,
                        contamination=settings.ML_CONTAMINATION)
        inc = (
            evaluate(incumbent, test_healthy, rows,
                     contamination=settings.ML_CONTAMINATION)
            if incumbent is not None else None
        )
        table.append({"view": label, "rows": len(rows),
                      "candidate": cand, "incumbent": inc})
    summary["views"] = table

    print("\n  fault-class views (candidate vs incumbent)")
    print(f"    {'view':<22}{'rows':>6}{'AUROC cand':>12}{'AUROC inc':>11}"
          f"{'recall cand':>13}{'recall inc':>12}")
    for entry in table:
        cand, inc = entry["candidate"], entry["incumbent"]
        inc_auroc = inc["auroc"] if inc else "n/a"
        inc_recall = inc["recall"] if inc else "n/a"
        print(f"    {entry['view']:<22}{entry['rows']:>6}"
              f"{cand['auroc']:>12}{inc_auroc:>11}"
              f"{cand['recall']:>13}{inc_recall:>12}")
    print("    (bystanders are up and running — scoring them at chance is")
    print("     correct behaviour, not a defect. See partition_chaos.)")

    # --- promote, or don't ---------------------------------------------
    # The decision is taken on the FAULTED-SERVICE view, not on the pooled
    # numbers. Pooled, 80% of the fault class is healthy bystanders from the
    # same chaos run, which caps and flattens AUROC for both models — there
    # the two are 0.006 apart, i.e. indistinguishable. The question a
    # detector exists to answer is "which service is broken", so the tie is
    # broken where that question is actually measured.
    culprit_view = next(
        (v for v in table if v["view"] == "faulted service only"), None
    )
    decision_candidate = culprit_view["candidate"] if culprit_view else candidate_report
    decision_incumbent = (culprit_view or {}).get("incumbent") or incumbent_report or {}
    summary["decision_view"] = "faulted service only"
    summary["decision_candidate"] = decision_candidate
    summary["decision_incumbent"] = decision_incumbent

    # --- gates -----------------------------------------------------------
    # Two conditions must hold before anything is promoted, and --force does
    # NOT override either:
    #
    #   live safety   the model must not flood live scoring. Measured on live
    #                 rows that were neither fitted nor calibrated, so the
    #                 number means something.
    #   detection     it must actually find broken services, or swapping it
    #                 in buys nothing.
    #
    # Only after both pass does beating the incumbent matter.
    culprit_auroc = decision_candidate.get("auroc")
    culprit_recall = decision_candidate.get("recall")
    detection_passed = bool(
        culprit_auroc is not None
        and culprit_auroc >= DETECTION_AUROC_MIN
        and culprit_recall is not None
        and culprit_recall >= DETECTION_RECALL_MIN
    )
    live_passed = bool(
        live_flag_rate is not None and live_flag_rate <= LIVE_FLAG_RATE_LIMIT
    )
    summary["gates"] = {
        "live_flag_rate": {
            "value": live_flag_rate,
            "limit": LIVE_FLAG_RATE_LIMIT,
            "passed": live_passed,
        },
        "detection": {
            "auroc": culprit_auroc,
            "auroc_min": DETECTION_AUROC_MIN,
            "recall": culprit_recall,
            "recall_min": DETECTION_RECALL_MIN,
            "passed": detection_passed,
        },
    }

    if dry_run:
        summary["reason"] = "dry-run: production left untouched"
    elif not live_passed:
        summary["reason"] = (
            f"REFUSED: candidate flags {live_flag_rate} of held-out live "
            f"traffic (limit {LIVE_FLAG_RATE_LIMIT:.0%}); promoting it would "
            f"flood live scoring with anomalies. --force does not override "
            f"this gate."
        )
    elif not detection_passed:
        summary["reason"] = (
            f"REFUSED: detection gate failed — faulted-service AUROC "
            f"{culprit_auroc} (min {DETECTION_AUROC_MIN}), recall "
            f"{culprit_recall} (min {DETECTION_RECALL_MIN}). "
            f"--force does not override this gate."
        )
    elif incumbent is None:
        _promote(cand_path, production)
        summary["replaced"] = True
        summary["reason"] = "no incumbent; candidate becomes the production model"
    elif force:
        _promote(cand_path, production)
        summary["replaced"] = True
        summary["reason"] = "promoted with --force"
    else:
        verdict = is_stronger(decision_candidate, decision_incumbent)
        if verdict:
            _promote(cand_path, production)
            summary["replaced"] = True
            summary["reason"] = (
                "candidate beat the incumbent on the faulted-service view"
            )
        elif verdict is False:
            summary["reason"] = (
                "candidate did NOT beat the incumbent on the faulted-service "
                "view; keeping the incumbent"
            )
        else:
            summary["reason"] = (
                "scores are not comparable on the faulted-service view; "
                "keeping the incumbent"
            )

    print("\n  gates")
    print(f"    live safety   : {'PASS' if live_passed else 'FAIL'}  "
          f"{live_flag_rate} of held-out live rows flagged "
          f"(limit {LIVE_FLAG_RATE_LIMIT:.0%})")
    print(f"    detection     : {'PASS' if detection_passed else 'FAIL'}  "
          f"faulted-service AUROC {culprit_auroc} "
          f"(min {DETECTION_AUROC_MIN}), recall {culprit_recall} "
          f"(min {DETECTION_RECALL_MIN})")
    print(f"\n  decision  : {summary['reason']}")
    print(f"             (AUROC {decision_candidate.get('auroc')} vs "
          f"{decision_incumbent.get('auroc')} on faulted-service rows)")
    if not summary["replaced"] and cand_path.exists():
        print(f"  candidate : left at {cand_path} for inspection")

    return summary


def _promote(candidate: Path, production: Path) -> None:
    """Move the candidate into the production slot and refresh its metadata."""
    import shutil
    production.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(candidate, production)

    meta_src = candidate.with_suffix(".meta.json")
    meta_dst = production.with_suffix(".meta.json")
    if meta_src.exists():
        shutil.copy2(meta_src, meta_dst)
        # The sidecar still describes the file it was written beside.
        # Leaving it would mean production's own metadata claims the model
        # lives at the scratch path, which is actively misleading when
        # someone goes looking for what is actually being served.
        try:
            meta = json.loads(meta_dst.read_text())
        except (json.JSONDecodeError, OSError):
            meta = None
        if meta is not None:
            meta["model_path"] = str(production)
            meta_dst.write_text(json.dumps(meta, indent=2))

    from app.ml.scoring import invalidate_cache
    invalidate_cache()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Retrain the anomaly model on the labeled datasets.",
    )
    parser.add_argument("--ratio", type=float, default=DEFAULT_RATIO,
                        help=f"healthy rows used for training (default {DEFAULT_RATIO})")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED,
                        help="shuffle seed, so the split is reproducible")
    parser.add_argument("--dry-run", action="store_true",
                        help="evaluate only; never modify the production model")
    parser.add_argument("--force", action="store_true",
                        help="skip the incumbent comparison; does NOT bypass "
                             "the live-safety or detection gates")
    args = parser.parse_args(argv)

    summary = run(ratio=args.ratio, seed=args.seed,
                  dry_run=args.dry_run, force=args.force)

    print("\n  summary")
    print(json.dumps(
        {k: v for k, v in summary.items() if k not in ("candidate_meta",)},
        indent=2, default=str,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
