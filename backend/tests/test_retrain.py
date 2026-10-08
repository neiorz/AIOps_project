"""
Tests for the labelled-dataset retrain (Track T5 remake).

What these actually protect, in order of how badly it would go wrong:

1. **The latency conversion.** ``latency_ms`` must become ``latency_s``
   divided by 1000. If that regresses the model still *fits* — it just
   trains on values three orders of magnitude too large and quietly learns
   nothing. No exception is raised, no output changes shape. Only a test
   that asserts the number catches it.

2. **The bystander partition.** 80% of the chaos corpus comes from services
   that were never faulted. Pooling them with the real faults caps AUROC
   for *both* models and can invert the promote/keep verdict — the two
   models land 0.006 apart when pooled and 0.08 apart on actual faults.

3. **Production safety.** A dry run must not write the model the API serves.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import joblib
import pytest

from app.ml.calibration import flag_rate
from app.ml.collector import load_features
from app.ml.datasets import (
    SchemaError,
    dataset_path,
    load_labeled,
    map_row,
    partition_chaos,
    split_rows,
)
from app.ml.evaluation import evaluate, format_report, is_stronger
from app.ml.retrain import run
from app.ml.train import train_model


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _row(**overrides):
    """One well-formed collected row; override to build malformed ones."""
    row = {
        "timestamp": "1700000000.0",
        "service_name": "frontend",
        "cpu_percent": "2.5",
        "memory_mb": "100.0",
        "latency_ms": "20650.0",
        "error_rate": "0.001",
        "service_up": "1",
        "is_anomaly": "1",
        "fault_type": "network_latency",
        "root_cause_service": "frontend",
    }
    row.update(overrides)
    return row


def _synthetic_rows(n=40):
    """Enough well-formed numeric rows to satisfy train_model's MIN_SAMPLES."""
    return [
        {
            "cpu_percent": 1.0 + (i % 7),
            "memory_mb": 50.0 + i,
            "latency_s": 0.02 + (i % 3) * 0.005,
            "error_rate": (i % 5) * 0.001,
            "service_up": 1.0,
        }
        for i in range(n)
    ]


@pytest.fixture(scope="module")
def corpus():
    """Load each dataset once for the whole module."""
    return load_labeled("normal"), load_labeled("chaos")


@pytest.fixture(scope="module")
def fit(corpus, tmp_path_factory):
    """Train the candidate once per module (~0.6s, not once per test)."""
    healthy, _ = corpus
    train_rows, test_rows = split_rows(healthy, ratio=0.8, seed=42)
    path = tmp_path_factory.mktemp("ml") / "candidate.joblib"
    meta = train_model(rows=train_rows, model_path=path)
    return SimpleNamespace(
        path=path,
        meta=meta,
        bundle=joblib.load(path),
        train=train_rows,
        test=test_rows,
        healthy=healthy,
    )


# --------------------------------------------------------------------------
# the corpora themselves
# --------------------------------------------------------------------------

def test_committed_datasets_have_the_sizes_we_evaluated_against(corpus):
    healthy, faults = corpus
    # Guarding the counts is guarding the experiment: every reported metric
    # is a function of 2350 healthy and 1600 fault rows.
    assert len(healthy) == 2350
    assert len(faults) == 1600


def test_every_row_carries_a_label(corpus):
    healthy, faults = corpus
    for row in healthy + faults:
        assert row["is_anomaly"] in ("0", "1")
        assert row["service"]
        assert row["fault_type"]


def test_normal_corpus_is_all_healthy(corpus):
    healthy, _ = corpus
    assert {r["is_anomaly"] for r in healthy} == {"0"}
    # map_row converts service_up to float, so this is 1.0, not "1".
    assert {r["service_up"] for r in healthy} == {1.0}


# --------------------------------------------------------------------------
# schema mapping — the conversion that fails silently if it breaks
# --------------------------------------------------------------------------

def test_latency_is_divided_by_1000():
    row = map_row(_row(latency_ms="20650.0"))
    assert row["latency_s"] == pytest.approx(20.65)
    assert "latency_ms" not in row


def test_service_name_is_renamed():
    row = map_row(_row(service_name="payment-service"))
    assert row["service"] == "payment-service"
    assert "service_name" not in row


def test_other_numeric_columns_pass_through_unscaled():
    """Only latency changes units — scaling the others would corrupt them."""
    row = map_row(_row(cpu_percent="12.5", memory_mb="64.0",
                       error_rate="0.25", service_up="1"))
    assert row["cpu_percent"] == 12.5
    assert row["memory_mb"] == 64.0
    assert row["error_rate"] == 0.25
    assert row["service_up"] == 1.0


def test_labels_are_preserved_for_evaluation():
    row = map_row(_row())
    assert row["is_anomaly"] == "1"
    assert row["fault_type"] == "network_latency"
    assert row["root_cause_service"] == "frontend"


def test_missing_column_raises_rather_than_defaulting_to_zero():
    raw = _row()
    del raw["service_up"]
    with pytest.raises(SchemaError) as excinfo:
        map_row(raw)
    assert "service_up" in str(excinfo.value)


def test_non_numeric_value_raises():
    with pytest.raises(SchemaError) as excinfo:
        map_row(_row(cpu_percent="not-a-number"))
    assert "cpu_percent" in str(excinfo.value)


def test_unknown_dataset_name_lists_what_is_available():
    with pytest.raises(SchemaError) as excinfo:
        dataset_path("does-not-exist")
    assert "normal" in str(excinfo.value)


# --------------------------------------------------------------------------
# bystander partition
# --------------------------------------------------------------------------

def test_partition_exhausts_the_corpus_without_overlap(corpus):
    _, faults = corpus
    culprit, bystander = partition_chaos(faults)
    assert len(culprit) + len(bystander) == len(faults)
    # Split rows are the same dict objects, so identity proves no row was
    # duplicated into both halves.
    assert not ({id(r) for r in culprit} & {id(r) for r in bystander})


def test_partition_matches_the_committed_corpus(corpus):
    _, faults = corpus
    culprit, bystander = partition_chaos(faults)
    # 320 rows come from the service that was actually broken; the other
    # 1280 are healthy services observed during the same chaos run.
    assert len(culprit) == 320
    assert len(bystander) == 1280


def test_partition_predicate_holds_on_each_side(corpus):
    _, faults = corpus
    culprit, bystander = partition_chaos(faults)
    assert all(r["service"] == r["root_cause_service"] for r in culprit)
    assert all(r["service"] != r["root_cause_service"] for r in bystander)


def test_unlabelled_root_cause_falls_into_bystanders():
    """'none' must not be mistaken for a service name."""
    rows = [
        {"service": "frontend", "root_cause_service": "none"},
        {"service": "cart", "root_cause_service": ""},
        {"service": "cart", "root_cause_service": "cart"},
    ]
    culprit, bystander = partition_chaos(rows)
    assert len(culprit) == 1
    assert len(bystander) == 2


def test_bystanders_are_not_the_same_as_faults():
    """Documents *why* the two views are scored separately."""
    _, faults = load_labeled("normal"), load_labeled("chaos")
    culprit, bystander = partition_chaos(faults)
    # The faulted service really is broken...
    assert sum(1 for r in culprit if r["service_up"] == 0.0) > 0
    # ...the bystanders are all still running.
    assert all(r["service_up"] == 1.0 for r in bystander)


# --------------------------------------------------------------------------
# train/test split
# --------------------------------------------------------------------------

def test_split_is_eighty_twenty_and_leak_free(corpus):
    healthy, _ = corpus
    train, test = split_rows(healthy, ratio=0.8, seed=42)
    assert len(train) == 1880
    assert len(test) == 470
    assert len(train) + len(test) == len(healthy)
    assert not ({id(r) for r in train} & {id(r) for r in test})


def test_split_is_reproducible(corpus):
    healthy, _ = corpus
    a_train, a_test = split_rows(healthy, ratio=0.8, seed=42)
    b_train, b_test = split_rows(healthy, ratio=0.8, seed=42)
    assert [id(r) for r in a_train] == [id(r) for r in b_train]
    assert [id(r) for r in a_test] == [id(r) for r in b_test]


def test_split_changes_with_the_seed(corpus):
    healthy, _ = corpus
    a_train, _ = split_rows(healthy, ratio=0.8, seed=42)
    b_train, _ = split_rows(healthy, ratio=0.8, seed=7)
    assert [id(r) for r in a_train] != [id(r) for r in b_train]


@pytest.mark.parametrize("ratio", [0.0, 1.0, 1.5, -0.1])
def test_split_rejects_a_nonsensical_ratio(ratio, corpus):
    healthy, _ = corpus
    with pytest.raises(ValueError):
        split_rows(healthy, ratio=ratio)


def test_splitting_an_empty_corpus_is_not_an_error():
    assert split_rows([], ratio=0.8) == ([], [])


# --------------------------------------------------------------------------
# honest reporting of zero-variance features
# --------------------------------------------------------------------------

def test_service_up_is_reported_as_untrainable(fit):
    """Healthy data has service_up==1 always; the trees cannot split on it.

    If this assertion ever fails because the column *started* varying, the
    companion change is that the model can now monitor it — worth noticing.
    """
    assert "service_up" in fit.meta["constant_features"]


def test_error_rate_becomes_trainable_with_the_labelled_corpus(fit):
    """The old 330-row scrape had constant error_rate; this corpus does not."""
    assert "error_rate" not in fit.meta["constant_features"]


def test_describe_flags_zero_variance_columns(corpus):
    from app.ml.datasets import describe

    healthy, _ = corpus
    summary = describe(healthy, "healthy")
    assert "service_up" in summary["constant_features"]
    assert summary["cpu_percent"]["stddev"] > 0


# --------------------------------------------------------------------------
# evaluation
# --------------------------------------------------------------------------

def test_confusion_matrix_partitions_every_row(fit):
    _, faults = load_labeled("normal"), load_labeled("chaos")
    rep = evaluate(fit.bundle, fit.test, faults, contamination=0.05)
    assert rep["tp"] + rep["fp"] + rep["tn"] + rep["fn"] == (
        len(fit.test) + len(faults)
    )


@pytest.mark.parametrize(
    "key",
    ["accuracy", "precision", "recall", "f1", "auroc", "false_positive_rate"],
)
def test_every_rate_is_a_real_rate(fit, key):
    _, faults = load_labeled("normal"), load_labeled("chaos")
    rep = evaluate(fit.bundle, fit.test, faults, contamination=0.05)
    value = rep[key]
    assert value is None or 0.0 <= value <= 1.0


def test_auroc_is_reported_as_undefined_with_one_class(fit):
    """No faults in the test set means AUROC has no meaning — say so."""
    rep = evaluate(fit.bundle, fit.test, [], contamination=0.05)
    assert rep["auroc"] is None
    assert "fault class" in rep["note"]


def test_contamination_is_echoed_into_the_report(fit):
    """The cut is a setting, so the report must state it beside the rates."""
    _, faults = load_labeled("normal"), load_labeled("chaos")
    rep = evaluate(fit.bundle, fit.test, faults, contamination=0.05)
    assert rep["contamination"] == 0.05


def test_model_finds_the_broken_service(fit):
    """The headline result this whole track exists to produce."""
    _, faults = load_labeled("normal"), load_labeled("chaos")
    culprit, _ = partition_chaos(faults)
    rep = evaluate(fit.bundle, fit.test, culprit, contamination=0.05)

    assert rep["auroc"] > 0.95, "should essentially separate faults"
    assert rep["recall"] >= 0.95, "should catch nearly every faulted service"
    assert rep["score_gap"] > 0.10, "fault scores must sit well above healthy"


def test_bystanders_score_near_chance(fit):
    """Not a defect: they are up and only mildly degraded.

    If this number ever climbed, it would mean the model started alarming
    on services that are fine.
    """
    _, faults = load_labeled("normal"), load_labeled("chaos")
    _, bystander = partition_chaos(faults)
    rep = evaluate(fit.bundle, fit.test, bystander, contamination=0.05)
    assert rep["auroc"] < 0.75


def test_pooled_auroc_cannot_pick_a_winner_on_its_own():
    """Why retrain.py decides on the faulted-service view.

    Pooled, these two differ by 0.006 and the *worse* detector wins. On the
    faulted-service view the same models differ by 0.08 with the better
    detector ahead. Same models, same rows, different question.
    """
    pooled_candidate = {"auroc": 0.6373, "f1": 0.5266}
    pooled_incumbent = {"auroc": 0.6434, "f1": 0.1287}
    assert is_stronger(pooled_candidate, pooled_incumbent) is False

    culprit_candidate = {"auroc": 0.9983, "f1": 0.9697}
    culprit_incumbent = {"auroc": 0.9162, "f1": 0.5117}
    assert is_stronger(culprit_candidate, culprit_incumbent) is True


def test_report_renders_and_mentions_the_threshold(fit):
    _, faults = load_labeled("normal"), load_labeled("chaos")
    rep = evaluate(fit.bundle, fit.test, faults, contamination=0.05)
    text = format_report(rep)
    assert "AUROC" in text
    assert "0.05" in text
    assert "TP / FP / TN / FN" in text


# --------------------------------------------------------------------------
# is_stronger ranking
# --------------------------------------------------------------------------

def test_is_stronger_ranks_by_auroc_first():
    assert is_stronger({"auroc": 0.9}, {"auroc": 0.8}) is True
    assert is_stronger({"auroc": 0.8}, {"auroc": 0.9}) is False


def test_is_stronger_falls_back_to_f1_when_auroc_ties():
    assert is_stronger({"auroc": 0.8, "f1": 0.7},
                       {"auroc": 0.8, "f1": 0.4}) is True
    assert is_stronger({"auroc": 0.8, "f1": 0.4},
                       {"auroc": 0.8, "f1": 0.7}) is False


def test_is_stronger_never_invents_a_winner():
    assert is_stronger({"auroc": None, "f1": None},
                       {"auroc": 0.9, "f1": 0.9}) is None
    assert is_stronger({}, {}) is None
    # An exact tie is not a reason to replace a working model.
    assert is_stronger({"auroc": 0.7, "f1": 0.5},
                       {"auroc": 0.7, "f1": 0.5}) is None


# --------------------------------------------------------------------------
# train_model output paths
# --------------------------------------------------------------------------

def test_writing_to_a_candidate_path_leaves_production_alone(
    isolated_model, tmp_path
):
    from app.config import settings

    target = tmp_path / "candidate.joblib"
    train_model(rows=_synthetic_rows(), model_path=target)

    assert target.exists()
    # Metadata sits beside the candidate, not over the production record.
    assert (tmp_path / "candidate.meta.json").exists()
    assert not Path(settings.ML_MODEL_PATH).exists()


def test_candidate_metadata_records_the_path_it_was_written_to(
    isolated_model, tmp_path
):
    target = tmp_path / "candidate.joblib"
    meta = train_model(rows=_synthetic_rows(), model_path=target)
    assert meta["model_path"] == str(target)
    stored = json.loads((tmp_path / "candidate.meta.json").read_text())
    assert stored["model_path"] == str(target)


def test_training_refuses_too_little_data(tmp_path):
    """MIN_SAMPLES guards against fitting noise on a handful of rows."""
    with pytest.raises(ValueError):
        train_model(rows=_synthetic_rows(3), model_path=tmp_path / "nope.joblib")
    # It must refuse *before* writing anything.
    assert not (tmp_path / "nope.joblib").exists()


# --------------------------------------------------------------------------
# the end-to-end retrain flow
# --------------------------------------------------------------------------

def test_dry_run_never_touches_production(isolated_model, tmp_path):
    from app.config import settings

    summary = run(dry_run=True, candidate_path=tmp_path / "cand.joblib")

    assert summary["replaced"] is False
    assert not Path(settings.ML_MODEL_PATH).exists()
    assert summary["reason"].startswith("dry-run")


def test_dry_run_scores_all_three_views(isolated_model, tmp_path):
    summary = run(dry_run=True, candidate_path=tmp_path / "cand.joblib")

    views = {v["view"] for v in summary["views"]}
    assert views == {"pooled", "faulted service only", "bystanders only"}
    assert summary["n_culprit_rows"] + summary["n_bystander_rows"] == 1600


def test_the_decision_is_taken_on_the_faulted_service_view(
    isolated_model, tmp_path
):
    """Pooled AUROC is too compressed between models to choose between them."""
    from app.config import settings

    # Give the run a genuine incumbent, the same way production has one:
    # trained from features.csv, written into the isolated production slot.
    train_model(model_path=settings.ML_MODEL_PATH)

    summary = run(dry_run=True, candidate_path=tmp_path / "cand.joblib")

    assert summary["decision_view"] == "faulted service only"
    assert summary["decision_candidate"]["auroc"] > 0.95
    # An actual incumbent report to compare against, not an empty dict.
    assert summary["decision_incumbent"].get("auroc") is not None
    # And the comparison must have run on rows both models were scored against.
    assert summary["decision_candidate"]["n_chaos_test"] == (
        summary["decision_incumbent"]["n_chaos_test"]
    )


def test_both_gates_pass_on_the_real_data(isolated_model, tmp_path):
    """The whole point: detect faults without flooding live scoring."""
    summary = run(dry_run=True, candidate_path=tmp_path / "cand.joblib")

    gates = summary["gates"]
    assert gates["detection"]["passed"] is True
    assert gates["live_flag_rate"]["passed"] is True
    # Measured on live rows used for neither fitting nor calibration.
    assert summary["live_flag_rate"] <= 0.15
    assert summary["live_verify_rows"] > 0
    # Calibration was actually applied, not just reported.
    assert summary["calibration"]["target_rate"] > 0
    assert summary["calibration"]["n_rows"] > 0


def test_calibration_is_persisted_with_the_candidate(isolated_model, tmp_path):
    """The offset_ must reach disk, or production reloads an uncalibrated cut."""
    import joblib

    candidate = tmp_path / "cand.joblib"
    summary = run(dry_run=True, candidate_path=candidate)

    saved = joblib.load(candidate)
    # predict() uses offset_, so re-scoring the calibration rows through the
    # reloaded model must reproduce the rate we claimed.
    from app.ml.calibration import flag_rate
    from app.ml.collector import load_features

    live = load_features()
    _, holdout = split_rows(live, ratio=0.6, seed=summary["seed"])
    cal, _ = split_rows(holdout, ratio=0.5, seed=summary["seed"])
    assert flag_rate(saved["model"], cal) == pytest.approx(
        summary["calibration"]["actual_rate"], abs=0.05
    )

    meta = json.loads(candidate.with_suffix(".meta.json").read_text())
    assert "calibration" in meta


def test_dry_run_leaves_the_candidate_file_behind_for_inspection(
    isolated_model, tmp_path
):
    candidate = tmp_path / "cand.joblib"
    run(dry_run=True, candidate_path=candidate)
    assert candidate.exists()


# --------------------------------------------------------------------------
# the premise the design rests on
# --------------------------------------------------------------------------

def test_the_two_environments_are_genuinely_disjoint():
    """Why the training mix needs live rows and not just the corpus.

    The labelled corpus peaks at 165 MB resident memory; this mesh idles at
    214-265 MB. If these ranges ever converge, union training stops being
    necessary and calibration's justification weakens — which is precisely
    the moment a test should say so out loud.
    """
    live = load_features()
    healthy, _ = load_labeled("normal"), load_labeled("chaos")

    corpus_memory = [float(r["memory_mb"]) for r in healthy]
    live_memory = [float(r["memory_mb"]) for r in live]

    assert max(corpus_memory) < min(live_memory), (
        "corpus and live memory ranges now overlap: revisit whether union "
        "training and live threshold calibration are still both required"
    )


def test_a_corpus_only_model_floods_live_traffic(tmp_path):
    """The measured failure this design exists to prevent.

    Fitted on the labelled corpus alone, the model called 330 of 330 live
    rows anomalous — every single reading. This test keeps that number
    visible, so nobody 'simplifies' the retrain back to corpus-only without
    seeing what it costs.
    """
    healthy, _ = load_labeled("normal"), load_labeled("chaos")
    train_rows, _ = split_rows(healthy, ratio=0.8, seed=42)

    target = tmp_path / "corpus_only.joblib"
    train_model(rows=train_rows, model_path=target)
    model = joblib.load(target)["model"]

    rate = flag_rate(model, load_features())
    assert rate > 0.9, (
        f"corpus-only model flagged only {rate:.0%} of live rows, not the "
        f"expected near-total flood. If this genuinely improved, the "
        f"environments may have converged — check "
        f"test_the_two_environments_are_genuinely_disjoint."
    )


def test_the_full_retrain_avoids_that_flood(isolated_model, tmp_path):
    """The fix, asserted end to end against the same live store."""
    live = load_features()
    summary = run(dry_run=True, candidate_path=tmp_path / "cand.joblib")

    # Strictly better than the corpus-only flood above, and comfortably
    # inside the gate.
    assert summary["live_flag_rate"] < 0.15
    assert summary["gates"]["live_flag_rate"]["passed"] is True
    # ...while still detecting the broken service.
    assert summary["gates"]["detection"]["passed"] is True
    assert summary["decision_candidate"]["recall"] >= 0.9
    assert len(live) == summary["live_rows"]
