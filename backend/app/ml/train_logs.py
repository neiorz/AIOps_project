"""Train the log anomaly model on the microservices' own log streams.

Collects live pod logs via kubectl (or loads a saved corpus), splits every
service's stream in time order, trains on the past, scores the held-out
future — and, when chaos-window logs are supplied, measures how much of the
chaos signature the model actually catches. Every number printed or
persisted is measured here; nothing is assumed.

    python -m app.ml.train_logs                          # collect live, train
    python -m app.ml.train_logs --corpus data/log_corpus.json
    python -m app.ml.train_logs --anomaly data/log_anomaly_cart.json
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import joblib

from app.ml.log_model import (chronological_split, evaluate, log_model_path,
                              train_log_model)

WORKLOAD_LABEL = "app.kubernetes.io/name"
NAMESPACE = "default"
# The cache is deployed beside the services but is not part of the workload
# under test — its startup lines would pad the scores with trivia.
SKIP_SERVICES = {"redis-cart"}


def collect_live_logs() -> Dict[str, List[str]]:
    """Read every workload pod's stdout/stderr, grouped by service label."""
    listing = subprocess.run(
        ["kubectl", "get", "pods", "-n", NAMESPACE, "-o", "json"],
        capture_output=True, text=True, timeout=30,
    )
    if listing.returncode != 0:
        raise RuntimeError(f"kubectl get pods failed: {listing.stderr.strip()}")

    logs: Dict[str, List[str]] = {}
    for pod in json.loads(listing.stdout)["items"]:
        service = pod["metadata"].get("labels", {}).get(WORKLOAD_LABEL)
        if not service or service in SKIP_SERVICES:
            continue
        res = subprocess.run(
            ["kubectl", "logs", pod["metadata"]["name"], "-n", NAMESPACE],
            capture_output=True, text=True, timeout=60,
        )
        lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
        if lines:
            logs.setdefault(service, []).extend(lines)
    return logs


def _sidecar(suffix: str) -> Path:
    return log_model_path().with_suffix(suffix)


def run(corpus_path: Path, save_corpus: bool, anomaly_paths: List[Path],
        window: int, top_k: int, ratio: float) -> Dict[str, Any]:
    """Collect/load, split, train, evaluate, persist; return the summary."""
    if corpus_path.exists():
        logs = json.loads(corpus_path.read_text())
        source = f"corpus file {corpus_path}"
    else:
        logs = collect_live_logs()
        source = "live kubectl collection"
        if save_corpus:
            corpus_path.parent.mkdir(parents=True, exist_ok=True)
            corpus_path.write_text(json.dumps(logs, indent=1))

    if not logs:
        raise RuntimeError("no logs collected — is the cluster running?")

    train_logs, held_out = chronological_split(logs, ratio=ratio)
    model = train_log_model(train_logs, window=window, top_k=top_k)
    held_out_report = evaluate(model, held_out)

    anomaly_sets = []
    for path in anomaly_paths:
        report = evaluate(model, json.loads(Path(path).read_text()))
        anomaly_sets.append({"source": str(path), **report})

    target = log_model_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, target)

    meta = {
        "trained_at": model["trained_at"],
        "algorithm": "LogTemplateTransitions",
        "note": "DeepLog-style next-template prediction over mined log "
                "templates; predictions are empirical transition counts",
        "window": window,
        "top_k": top_k,
        "n_templates": len(model["templates"]),
        "n_events": model["n_events"],
        "services": model["services"],
        "corpus": source,
    }
    _sidecar(".meta.json").write_text(json.dumps(meta, indent=2))

    evaluation = {
        "evaluated_at": time.time(),
        "trained_at": model["trained_at"],
        "algorithm": "LogTemplateTransitions",
        "window": window,
        "top_k": top_k,
        "n_templates": len(model["templates"]),
        "train_events": model["n_events"],
        "held_out_events": held_out_report["events"],
        "held_out": held_out_report,
        "anomaly_sets": anomaly_sets,
        "services": model["services"],
        "corpus": source,
    }
    _sidecar(".eval.json").write_text(json.dumps(evaluation, indent=2))

    summary: Dict[str, Any] = {
        "corpus": source,
        "services": model["services"],
        "window": window,
        "top_k": top_k,
        "n_templates": len(model["templates"]),
        "train_events": model["n_events"],
        "held_out": held_out_report,
        "anomaly_sets": anomaly_sets,
        "model_path": str(target),
        "evaluation_path": str(_sidecar(".eval.json")),
    }
    return summary


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Train the log anomaly model on live pod logs.")
    parser.add_argument("--corpus", type=Path,
                        default=Path(__file__).parent / "data" / "log_corpus.json",
                        help="saved corpus to train from instead of kubectl")
    parser.add_argument("--save-corpus", action="store_true",
                        help="persist a live collection beside the model")
    parser.add_argument("--anomaly", type=Path, action="append", default=[],
                        help="JSON {service: [lines]} captured during a "
                             "fault; repeatable")
    parser.add_argument("--window", type=int, default=3,
                        help="context templates the model predicts from")
    parser.add_argument("--top-k", type=int, default=3,
                        help="predictions considered a hit")
    parser.add_argument("--ratio", type=float, default=0.8,
                        help="share of each stream used for training")
    args = parser.parse_args(argv)

    try:
        summary = run(corpus_path=args.corpus, save_corpus=args.save_corpus,
                      anomaly_paths=args.anomaly, window=args.window,
                      top_k=args.top_k, ratio=args.ratio)
    except (RuntimeError, ValueError) as exc:
        print(f"  error: {exc}", file=sys.stderr)
        return 2

    held = summary["held_out"]
    print("\n  log model (DeepLog-style template transitions)")
    print(f"    corpus        : {summary['corpus']}")
    print(f"    services      : "
          + ", ".join(f"{s} {n}" for s, n in summary["services"].items()))
    print(f"    window/top_k  : {summary['window']} / {summary['top_k']}   "
          f"templates mined: {summary['n_templates']}")
    print(f"    held-out      : {held['events']} events — next-event accuracy "
          f"{held['next_event_accuracy']}, flagged {held['flagged_rate']} "
          f"(unseen template {held['unseen_template_rate']})")
    for entry in summary["anomaly_sets"]:
        print(f"    anomaly set   : {entry['source']} — {entry['events']} "
              f"events, flagged {entry['flagged_rate']}")
        for text, count in list(entry["flagged_templates"].items())[:3]:
            print(f"        {count:>5} x  {text[:70]}")
    print(f"\n  model      : {summary['model_path']}")
    print(f"  evaluation : {summary['evaluation_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
