"""
Log anomaly detection — DeepLog-style template transitions.

Hermetic: pure functions on small synthetic corpora, plus the API contract
for the log evaluation block. No cluster, no kubectl, no network.
"""
import json

import pytest

from app.ml import train_logs
from app.ml.log_model import (
    TemplateStore,
    chronological_split,
    evaluate,
    normalize_line,
    train_log_model,
)

# A tiny stand-in for real service logs: two endpoints with bursty sessions.
PATTERN = [
    "[00:00:00] HTTP GET /cart/<ID> 200 OK",
    "[00:00:00] HTTP GET /cart/<ID>/items 200 OK",
]


def _bursty(n=60, seed_switch=7):
    """Sticky sessions: hold an endpoint for a while, then switch."""
    out, current = [], 0
    for i in range(n):
        if i % seed_switch == 0:
            current = (current + 1) % len(PATTERN)
        out.append(PATTERN[current].replace("<ID>", str(i)))
    return out


NORMAL = {"cart-service": _bursty()}


# --------------------------------------------------------------------------
# template mining
# --------------------------------------------------------------------------

def test_normalize_strips_timestamp_and_wildcards_identifiers():
    tokens = normalize_line(
        "[07:15:48] [WARN] NetworkChaos Latency: delayed by 850.0ms")
    assert tokens == ["[WARN]", "NetworkChaos", "Latency:",
                      "delayed", "by", "<*>.<*>ms"]
    assert normalize_line("[06:51:47] HTTP GET /products/396 200 OK") == \
        ["HTTP", "GET", "/products/<*>", "<*>", "OK"]


def test_same_shape_lines_share_a_template_different_shapes_do_not():
    store = TemplateStore()
    a = store.observe("[06:51:47] HTTP GET /products/396 200 OK")
    b = store.observe("[06:51:48] HTTP GET /products/1 200 OK")
    c = store.observe("[06:51:49] HTTP GET / 200 OK")
    assert a == b                      # only the id differs
    assert c != a                      # different endpoint shape
    assert len(store.templates) == 2


def test_lookup_is_frozen_and_flags_unseen_lines():
    store = TemplateStore()
    known = store.observe("[06:51:47] HTTP GET /products/396 200 OK")
    assert store.lookup("[06:51:48] HTTP GET /products/12 200 OK") == known
    unseen = store.lookup("[07:15:48] [WARN] boom happened")
    assert unseen < 0                          # negative id = never seen
    assert unseen == store.lookup("[07:15:49] [WARN] boom happened")


# --------------------------------------------------------------------------
# sequences -> model -> evaluation
# --------------------------------------------------------------------------

def test_chronological_split_never_leaks_the_future():
    lines = NORMAL["cart-service"]
    train, held = chronological_split(NORMAL, ratio=0.7)
    cut = int(len(lines) * 0.7)
    assert train["cart-service"] == lines[:cut]   # past
    assert held["cart-service"] == lines[cut:]    # future


def test_split_rejects_a_nonsense_ratio():
    with pytest.raises(ValueError):
        chronological_split(NORMAL, ratio=1.5)


def test_model_learns_normal_sequence_and_flags_unseen_fault_lines():
    train, held = chronological_split(NORMAL, ratio=0.7)
    model = train_log_model(train, window=3, top_k=2)

    healthy = evaluate(model, held)
    assert healthy["events"] > 0
    assert healthy["next_event_accuracy"] >= 0.9
    assert healthy["unseen_template_events"] == 0

    # A fault line whose shape never occurred in training, preceded by three
    # ordinary lines so the model has a context to predict from.
    fault = {"cart-service": [
        "[00:00:00] HTTP GET /cart/91 200 OK",
        "[00:00:00] HTTP GET /cart/92/items 200 OK",
        "[00:00:00] HTTP GET /cart/93 200 OK",
        "[00:00:00] [WARN] NetworkChaos Latency: delayed cart-service "
        "response by 850.0ms",
    ]}
    detected = evaluate(model, fault)
    assert detected["events"] == 1
    assert detected["flagged_rate"] == 1.0
    assert detected["unseen_template_events"] == 1


# --------------------------------------------------------------------------
# training CLI — sidecars must be written where the API looks for them
# --------------------------------------------------------------------------

def test_train_logs_run_records_model_and_sidecars(isolated_model, tmp_path):
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps(NORMAL))
    summary = train_logs.run(corpus_path=corpus, save_corpus=False,
                             anomaly_paths=[], window=3, top_k=2, ratio=0.7)

    model_dir = isolated_model          # ML_MODEL_PATH's parent (tmp_path)
    assert (model_dir / "log_model.joblib").exists()
    meta = json.loads((model_dir / "log_model.meta.json").read_text())
    assert meta["algorithm"] == "LogTemplateTransitions"
    assert summary["train_events"] == int(len(NORMAL["cart-service"]) * 0.7)

    recorded = json.loads((model_dir / "log_model.eval.json").read_text())
    assert recorded["algorithm"] == "LogTemplateTransitions"
    assert recorded["held_out"]["events"] > 0


# --------------------------------------------------------------------------
# GET /anomalies/evaluation — the logs block follows the same contract
# --------------------------------------------------------------------------

def test_evaluation_endpoint_says_so_when_no_log_model_is_trained(
    test_client, isolated_model
):
    body = test_client.get("/api/v1/anomalies/evaluation").json()
    assert "logs" in body
    assert body["logs"]["available"] is False
    assert "train_logs" in body["logs"]["note"]


def test_evaluation_endpoint_serves_a_recorded_log_evaluation(
    test_client, isolated_model
):
    (isolated_model / "log_model.eval.json").write_text(json.dumps({
        "trained_at": 5.0,
        "algorithm": "LogTemplateTransitions",
        "held_out": {"next_event_accuracy": 0.92, "flagged_rate": 0.08},
        "anomaly_sets": [{"source": "chaos.json", "events": 297,
                          "flagged_rate": 1.0}],
    }))
    body = test_client.get("/api/v1/anomalies/evaluation").json()
    assert body["logs"]["available"] is True
    assert body["logs"]["held_out"]["next_event_accuracy"] == 0.92
    assert body["logs"]["anomaly_sets"][0]["flagged_rate"] == 1.0
