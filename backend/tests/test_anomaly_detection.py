"""
Track T5 — Isolation Forest anomaly detection.

Everything here is hermetic: no network, no live mesh, no reliance on
whether a model happens to exist in this checkout. Model + feature paths are
redirected to tmp_path so these tests never touch (or destroy) a real fit.
"""
import random

import pytest

from app.config import settings
from app.ml import collector, scoring
from app.ml.collector import (
    FEATURE_NAMES,
    MODEL_FEATURES,
    build_features,
    parse_metrics,
)
from app.ml.train import MIN_SAMPLES, train_model

# NOTE: isolated_model / isolated_features live in conftest.py so the Phase 0
# contract test can reuse them.

SAMPLE_METRICS = """
# HELP service_up Whether the service is running and accepting traffic
# TYPE service_up gauge
service_up{service="frontend"} 1
# HELP http_requests_total Total number of HTTP requests processed
# TYPE http_requests_total counter
http_requests_total{service="frontend",status="200"} 40
http_requests_total{service="frontend",status="500"} 10
# HELP http_request_duration_seconds HTTP request latency
# TYPE http_request_duration_seconds gauge
http_request_duration_seconds{service="frontend"} 0.025
# HELP process_cpu_percent Process CPU utilization percentage
# TYPE process_cpu_percent gauge
process_cpu_percent{service="frontend"} 12.5
# HELP process_resident_memory_bytes Process Resident Memory in bytes
# TYPE process_resident_memory_bytes gauge
process_resident_memory_bytes{service="frontend"} 104857600
"""


def _row(i, cpu=8.0, latency=0.03, error_rate=0.0, requests=120.0, up=1.0):
    return {
        "timestamp": 1_700_000_000.0 + i,
        "service": "frontend",
        "cpu_percent": cpu,
        "memory_mb": 121.0,
        "latency_s": latency,
        "error_rate": error_rate,
        "requests_total": requests,
        "service_up": up,
    }


def _baseline(n=40, seed=7):
    """Steady, mildly jittery traffic — the 'normal' distribution."""
    rng = random.Random(seed)
    return [
        _row(i, cpu=rng.uniform(4.0, 14.0), latency=rng.uniform(0.01, 0.05))
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def test_parse_metrics_handles_labelled_counters():
    parsed = parse_metrics(SAMPLE_METRICS)

    # http_requests_total appears twice, once per status label
    counters = {lbls: v for (name, lbls), v in parsed.items()
                if name == "http_requests_total"}
    assert counters[(("service", "frontend"), ("status", "200"))] == 40.0
    assert counters[(("service", "frontend"), ("status", "500"))] == 10.0

    assert dict(parsed)[("service_up", (("service", "frontend"),))] == 1.0
    assert ("# comment", ()) not in parsed   # HELP/TYPE lines skipped


def test_build_features_derives_error_rate():
    feats = build_features(parse_metrics(SAMPLE_METRICS))
    assert feats is not None
    assert feats["cpu_percent"] == 12.5
    assert feats["memory_mb"] == 100.0            # 104857600 / 1024**2
    assert feats["latency_s"] == 0.025
    assert feats["requests_total"] == 50.0        # 40 ok + 10 failed
    assert feats["error_rate"] == pytest.approx(0.2)
    assert feats["service_up"] == 1.0
    assert list(feats) == FEATURE_NAMES


def test_build_features_ignores_a_service_without_up_signal():
    """Anything that is not one of our services must return None, not zeros."""
    assert build_features(parse_metrics("node_cpu_seconds_total 3.0")) is None


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------
def test_train_rejects_too_few_samples(isolated_model):
    with pytest.raises(ValueError) as exc:
        train_model(_baseline(n=MIN_SAMPLES - 1))
    assert f"at least {MIN_SAMPLES} samples" in str(exc.value)


def test_isolation_forest_flags_spike_but_not_steady_traffic(isolated_model):
    baseline = _baseline()
    spike = _row(99, cpu=99.0, latency=42.0, error_rate=1.0, requests=0.0)

    meta = train_model(baseline + [spike])
    assert meta["n_samples"] == len(baseline) + 1
    assert meta["algorithm"] == "IsolationForest"
    assert meta["feature_names"] == MODEL_FEATURES
    # requests_total is a monotonic counter and must never reach the model
    assert "requests_total" not in meta["feature_names"]
    assert set(meta["feature_names"]) <= set(FEATURE_NAMES)

    # The centroid of the training distribution is the safest possible
    # 'normal' point — a flat line must never be called an anomaly.
    typical = {k: sum(r[k] for r in baseline) / len(baseline) for k in FEATURE_NAMES}
    typical["timestamp"] = 0.0
    typical["service"] = "frontend"

    scores, flags = scoring.score([typical, spike])
    assert flags == [False, True], (
        f"steady traffic flagged={flags[0]}, injected spike flagged={flags[1]}"
    )
    assert scores[1] > scores[0], "spike must score as more anomalous"


def test_training_persists_model_and_metadata(isolated_model):
    train_model(_baseline())
    assert (isolated_model / "model.joblib").exists()
    assert (isolated_model / "model.meta.json").exists()

    status = scoring.model_status()
    assert status["loaded"] is True
    assert status["algorithm"] == "IsolationForest"
    assert status["training_samples"] == len(_baseline())
    assert status["contamination"] == settings.ML_CONTAMINATION


def test_training_reports_features_it_cannot_monitor(isolated_model):
    """A zero-variance feature is invisible to the trees — say so out loud."""
    meta = train_model(_baseline())

    # memory_mb / error_rate / service_up never move in this fixture
    assert set(meta["constant_features"]) == {"memory_mb", "error_rate", "service_up"}
    # ...while latency and cpu do vary, so they carry the signal
    assert "latency_s" not in meta["constant_features"]
    assert "cpu_percent" not in meta["constant_features"]


# ---------------------------------------------------------------------------
# Scoring without a model must fail loudly
# ---------------------------------------------------------------------------
def test_score_without_model_raises(isolated_model):
    scoring.invalidate_cache()
    with pytest.raises(RuntimeError) as exc:
        scoring.score([_row(1)])
    assert "train" in str(exc.value)


def test_api_score_returns_409_when_untrained(test_client, isolated_model):
    scoring.invalidate_cache()
    res = test_client.post("/api/v1/anomalies/score", json={
        "samples": [_row(1)]
    })
    assert res.status_code == 409
    assert "train" in res.json()["detail"].lower()


def test_api_get_reports_untrained_model(test_client, isolated_model):
    scoring.invalidate_cache()
    body = test_client.get("/api/v1/anomalies").json()
    assert body["status"] == "UNTRAINED"
    assert body["model"]["loaded"] is False
    assert isinstance(body["anomalies"], list)


# ---------------------------------------------------------------------------
# End-to-end through the API contract
# ---------------------------------------------------------------------------
def test_api_train_then_score_end_to_end(test_client, isolated_model,
                                         isolated_features):
    # Seed the collector's CSV with real-shaped rows
    rows = _baseline() + [_row(99, cpu=99.0, latency=42.0, error_rate=1.0)]
    collector.append_rows(rows)
    assert isolated_features.exists()

    scoring.invalidate_cache()
    train = test_client.post("/api/v1/anomalies/train")
    assert train.status_code == 200, train.text
    body = train.json()
    assert body["status"] == "TRAINED"
    assert body["owner"] == "Track T5 (Anomaly Detection)"
    assert body["model"]["loaded"] is True
    assert body["model"]["training_samples"] == len(rows)

    scored = test_client.post("/api/v1/anomalies/score", json={
        "samples": [_row(99, cpu=99.0, latency=42.0, error_rate=1.0)]
    })
    assert scored.status_code == 200, scored.text
    out = scored.json()
    assert out["status"] == "SCORED"
    assert out["scored"] == 1
    assert out["anomaly_count"] == 1
    assert out["anomalies"][0]["is_anomaly"] is True
    assert out["anomalies"][0]["service"] == "frontend"

    # GET now reports READY and surfaces the persisted detection
    after = test_client.get("/api/v1/anomalies").json()
    assert after["status"] == "READY"
    assert any(a["is_anomaly"] for a in after["anomalies"])

    # Cleanup so a later test in the same session sees a clean slate
    scoring.reset_anomalies()


def test_api_train_refuses_when_features_are_missing(test_client, isolated_model,
                                                     isolated_features):
    scoring.invalidate_cache()
    res = test_client.post("/api/v1/anomalies/train")
    assert res.status_code == 409
    assert "sample" in res.json()["detail"].lower()
