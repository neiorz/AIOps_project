"""
Feature collector for anomaly detection (Track T5).

Scrapes each local mesh service's Prometheus-style /metrics endpoint and turns
it into a numeric feature row. Deliberately uses only httpx + the stdlib so it
needs no psutil and no network access (everything is 127.0.0.1).

Feature vector per (service, timestamp):
    cpu_percent       process CPU utilization
    memory_mb         resident set size
    latency_s         http_request_duration_seconds
    error_rate        5xx / (2xx + 5xx)
    requests_total    cumulative successful + failed requests
    service_up        1 if accepting traffic else 0
"""
import argparse
import csv
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings

# Local mesh services -> port (localhost only, no Kubernetes involved).
# Verified against GET /api/v1/mesh/status: payment/cart bind 8181/8182,
# frontend/productcatalog 8083/8084, redis-cart 6380.
SERVICE_PORTS: Dict[str, int] = {
    "payment-service": 8181,
    "cart-service": 8182,
    "frontend": 8083,
    "productcatalog-service": 8084,
    "redis-cart": 6380,
}

# Feature column order. The trained model MUST see them in this order.
FEATURE_NAMES: List[str] = [
    "cpu_percent",
    "memory_mb",
    "latency_s",
    "error_rate",
    "requests_total",
    "service_up",
]

# Subset the model actually trains on. requests_total is a monotonically
# increasing counter: it encodes "how long the process has been up", not
# health. Leaving it in taught the Isolation Forest to call every newer
# sample anomalous purely for having a bigger count. It stays in the CSV
# (the UI and debugging want it) but never reaches the model.
MODEL_FEATURES: List[str] = [
    "cpu_percent",
    "memory_mb",
    "latency_s",
    "error_rate",
    "service_up",
]

DATA_DIR: Path = Path(settings.BASE_DIR) / "app" / "ml" / "data"
FEATURES_CSV: Path = DATA_DIR / "features.csv"
ANOMALIES_JSON: Path = DATA_DIR / "anomalies.json"

_LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


# ---------------------------------------------------------------------------
# Prometheus text parsing
# ---------------------------------------------------------------------------
def parse_metrics(text: str) -> Dict[tuple, float]:
    """
    Parse the exposition format into {(metric_name, labels_tuple): value}.
    Kept label-aware because http_requests_total appears once per status.
    """
    out: Dict[tuple, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "{" in line:
            name, rest = line.split("{", 1)
            labels_part, _, value = rest.rpartition("}")
        else:
            name, sep, value = line.partition(" ")
            if not sep:
                continue
            labels_part = ""
        try:
            out[(name.strip(), tuple(sorted(_LABEL_RE.findall(labels_part))))] = float(value)
        except ValueError:
            continue
    return out


def _find(parsed: Dict[tuple, float], name: str, **want: str) -> Optional[float]:
    for (metric, labels), value in parsed.items():
        if metric != name:
            continue
        if all(dict(labels).get(k) == v for k, v in want.items()):
            return value
    return None


def build_features(parsed: Dict[tuple, float]) -> Optional[Dict[str, float]]:
    """Map a parsed metric dump onto the fixed feature vector."""
    up = _find(parsed, "service_up")
    if up is None:
        return None                      # not one of our services
    ok = _find(parsed, "http_requests_total", status="200") or 0.0
    err = _find(parsed, "http_requests_total", status="500") or 0.0
    total = ok + err
    return {
        "cpu_percent": _find(parsed, "process_cpu_percent") or 0.0,
        "memory_mb": (_find(parsed, "process_resident_memory_bytes") or 0.0) / 1024**2,
        "latency_s": _find(parsed, "http_request_duration_seconds") or 0.0,
        "error_rate": (err / total) if total else 0.0,
        "requests_total": total,
        "service_up": up,
    }


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------
def scrape_service(port: int, timeout: float = 2.0) -> Optional[Dict[str, float]]:
    """Scrape one local service. Returns None if it is down."""
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=timeout)
        if r.status_code != 200:
            return None
        return build_features(parse_metrics(r.text))
    except Exception:
        return None


def collect_once() -> List[Dict[str, Any]]:
    """One sample across every reachable service."""
    now = time.time()
    rows: List[Dict[str, Any]] = []
    for service, port in SERVICE_PORTS.items():
        feats = scrape_service(port)
        if feats is not None:
            rows.append({"timestamp": now, "service": service, **feats})
    return rows


def collect(samples: int, interval: float) -> List[Dict[str, Any]]:
    """Sample repeatedly and append to features.csv."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    all_rows: List[Dict[str, Any]] = []
    for i in range(samples):
        rows = collect_once()
        append_rows(rows)
        all_rows.extend(rows)
        print(f"  [{i + 1}/{samples}] sampled {len(rows)} service(s)")
        if i < samples - 1:
            time.sleep(interval)
    return all_rows


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def append_rows(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new_file = not FEATURES_CSV.exists()
    with FEATURES_CSV.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["timestamp", "service", *FEATURE_NAMES])
        if new_file:
            writer.writeheader()
        writer.writerows(rows)


def load_features() -> List[Dict[str, Any]]:
    if not FEATURES_CSV.exists():
        return []
    with FEATURES_CSV.open(newline="") as fh:
        return [row for row in csv.DictReader(fh)]


def main() -> None:
    ap = argparse.ArgumentParser(description="Collect mesh feature samples for T5.")
    ap.add_argument("--samples", type=int, default=30, help="how many rounds to sample")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between rounds")
    args = ap.parse_args()
    print(f"Collecting {args.samples} rounds from {len(SERVICE_PORTS)} services...")
    rows = collect(args.samples, args.interval)
    print(f"Done: {len(rows)} rows appended to {FEATURES_CSV}")


if __name__ == "__main__":
    main()
