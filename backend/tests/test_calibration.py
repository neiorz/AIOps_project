"""
Tests for decision-threshold calibration (Track T5 remake).

The property under test is narrow and important: calibration moves *where
the line is drawn* without touching the model that draws it.

A regression here would show up as production either going silent (cut
pushed so high nothing is ever flagged) or screaming (cut left where a
different training environment put it). The second case is not hypothetical
— fitted on the labelled corpus alone, 330 of 330 live rows were called
anomalous.
"""

import numpy as np
import pytest
from sklearn.ensemble import IsolationForest

from app.ml.calibration import calibrate, flag_rate
from app.ml.evaluation import feature_matrix


def _rows(n, seed, cpu_mean=2.0, mem_mean=100.0, latency_mean=0.02):
    """Well-formed normal traffic from one distribution."""
    rng = np.random.default_rng(seed)
    return [
        {
            "cpu_percent": float(c),
            "memory_mb": float(m),
            "latency_s": float(l),
            "error_rate": 0.001,
            "service_up": 1.0,
        }
        for c, m, l in zip(
            rng.normal(cpu_mean, 0.4, n),
            rng.normal(mem_mean, 8.0, n),
            rng.normal(latency_mean, 0.004, n),
        )
    ]


def _anomalies(n=60):
    """Rows no threshold should be able to excuse."""
    rng = np.random.default_rng(99)
    return [
        {
            "cpu_percent": float(c),
            "memory_mb": float(m),
            "latency_s": float(l),
            "error_rate": 0.5,
            "service_up": 1.0,
        }
        for c, m, l in zip(
            rng.normal(90.0, 3.0, n),
            rng.normal(400.0, 20.0, n),
            rng.normal(0.9, 0.1, n),
        )
    ]


@pytest.fixture
def fitted():
    """A fitted model plus disjoint rows to calibrate it against.

    Function-scoped on purpose: calibrate() mutates ``model.offset_``, and a
    shared model would leak one test's threshold into the next.
    """
    train = _rows(600, seed=1)
    model = IsolationForest(n_estimators=100, contamination=0.05, random_state=42)
    model.fit(feature_matrix(train))
    return model, _rows(300, seed=2)


# --------------------------------------------------------------------------
# hitting the requested rate
# --------------------------------------------------------------------------

def test_calibration_flags_the_requested_share(fitted):
    model, calib = fitted
    result = calibrate(model, calib, target_rate=0.05)

    assert result["target_rate"] == 0.05
    assert result["n_rows"] == len(calib)
    assert result["actual_rate"] == pytest.approx(0.05, abs=0.02)
    assert result["flagged"] == round(result["actual_rate"] * len(calib))


def test_reported_rate_is_what_predict_actually_does(fitted):
    """The dict must describe reality, not the quantile we aimed for."""
    model, calib = fitted
    result = calibrate(model, calib, target_rate=0.10)

    assert flag_rate(model, calib) == result["actual_rate"]
    assert result["flagged"] == int(
        (model.predict(feature_matrix(calib)) == -1).sum()
    )


def test_calibration_moves_the_offset(fitted):
    model, calib = fitted
    before = model.offset_
    calibrate(model, calib, target_rate=0.05)
    assert model.offset_ != before


def test_higher_target_flags_more_rows(fitted):
    model, calib = fitted
    calibrate(model, calib, target_rate=0.02)
    low = flag_rate(model, calib)
    calibrate(model, calib, target_rate=0.25)
    high = flag_rate(model, calib)
    assert high > low
    assert high == pytest.approx(0.25, abs=0.02)


# --------------------------------------------------------------------------
# the trees themselves are untouched
# --------------------------------------------------------------------------

def test_calibration_leaves_detection_intact(fitted):
    """Moving the cut must not blind the model to obvious faults."""
    model, calib = fitted
    calibrate(model, calib, target_rate=0.05)
    assert flag_rate(model, _anomalies()) > 0.9


def test_calibration_does_not_retrain_the_trees(fitted):
    """Only offset_ may change; the forest must be byte-for-byte the same."""
    model, calib = fitted
    before_offsets = [
        (est.tree_.threshold.copy(), est.tree_.feature.copy())
        for est in model.estimators_
    ]
    calibrate(model, calib, target_rate=0.05)
    after_offsets = [
        (est.tree_.threshold.copy(), est.tree_.feature.copy())
        for est in model.estimators_
    ]
    for (t1, f1), (t2, f2) in zip(before_offsets, after_offsets):
        np.testing.assert_array_equal(t1, t2)
        np.testing.assert_array_equal(f1, f2)


def test_score_samples_ordering_is_preserved(fitted):
    """Calibration is an additive shift: relative scores must not scramble.

    If two rows ranked the same way before calibrating ranked differently
    after, the cut would have been moved by rewriting the model.
    """
    model, calib = fitted
    before = model.score_samples(feature_matrix(calib))
    calibrate(model, calib, target_rate=0.05)
    after = model.score_samples(feature_matrix(calib))
    # identical values: score_samples does not depend on offset_ at all
    np.testing.assert_array_almost_equal(before, after)


# --------------------------------------------------------------------------
# flag_rate
# --------------------------------------------------------------------------

def test_flag_rate_is_none_for_an_empty_set(fitted):
    """A gate over zero rows must read 'undefined', not pass by default."""
    model, _ = fitted
    assert flag_rate(model, []) is None


def test_flag_rate_matches_predict(fitted):
    model, calib = fitted
    expected = round(float((model.predict(feature_matrix(calib)) == -1).mean()), 4)
    assert flag_rate(model, calib) == expected


# --------------------------------------------------------------------------
# rejected inputs
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [0.0, 1.0, -0.1, 1.5])
def test_rejects_an_impossible_target(fitted, bad):
    model, calib = fitted
    with pytest.raises(ValueError):
        calibrate(model, calib, target_rate=bad)


def test_rejects_a_missing_calibration_set(fitted):
    """Silently skipping calibration would leave the fit-time cut in place
    and look like it worked."""
    model, _ = fitted
    with pytest.raises(ValueError):
        calibrate(model, [], target_rate=0.05)


def test_rejected_calibration_leaves_the_model_untouched(fitted):
    """Validation happens before mutation, so a bad call cannot half-apply."""
    model, calib = fitted
    before = model.offset_
    with pytest.raises(ValueError):
        calibrate(model, calib, target_rate=0.0)
    assert model.offset_ == before
