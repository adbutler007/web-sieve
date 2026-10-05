"""The calibration decision rule (calibration/calibrate.py, decide) scans every threshold.

Why: on 2026-10-05 the rule took each configuration's highest threshold with recall of at
least 0.90 and only afterwards checked that recall against Haiku's and page misses, so it
chose a point that could not be adopted while a lower threshold of another configuration
passed every condition. A rule that applies those conditions while choosing the threshold
cannot do that.
"""

import importlib.util
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
CALIBRATE = os.path.join(HERE, "..", "calibration", "calibrate.py")


@pytest.fixture(scope="module")
def calibrate():
    spec = importlib.util.spec_from_file_location("calibrate", CALIBRATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row(threshold, precision, recall, page_misses=0, bridge=False, fraction=0.03):
    return {"threshold": threshold, "precision": precision, "recall": recall,
            "page_misses": page_misses, "bridge": bridge, "fraction_selected": fraction}


def rows(points):
    """points: {threshold: (precision, recall)}; a bridged twin row is added for each."""
    out = []
    for t, (p, r) in points.items():
        out.append(row(t, p, r))
        out.append(row(t, p, r, bridge=True))
    return out


HAIKU = {"recall": 0.96}
COSTS = {"C2": {"requests_needed": 141, "median_wall_s": 0.5},
         "C2h": {"requests_needed": 141, "median_wall_s": 0.42}}


def test_a_threshold_that_fails_the_haiku_recall_check_is_skipped_for_a_lower_one(calibrate):
    # The 2026-10-05 shape: C2 at 0.85 has the highest recall >= 0.90 but is below Haiku's
    # recall minus the slack; C2h at 0.80 passes everything.
    metrics = {
        "C2": rows({0.80: (0.504, 0.965), 0.85: (0.533, 0.925), 0.90: (0.664, 0.816)}),
        "C2h": rows({0.80: (0.523, 0.965), 0.85: (0.510, 0.851), 0.90: (0.650, 0.711)}),
    }
    out = calibrate.decide(metrics, COSTS, HAIKU)
    assert out["adopt"] is True
    assert (out["chosen"]["config"], out["chosen"]["threshold"]) == ("C2h", 0.80)
    assert all(out["checks"].values())
    # C2 is represented by its 0.80 point, not its 0.85 point.
    c2 = next(c for c in out["candidates"] if c["config"] == "C2")
    assert c2["threshold"] == 0.80


def test_page_misses_disqualify_a_threshold(calibrate):
    metrics = {"C2": [row(0.80, 0.6, 0.97, page_misses=1), row(0.80, 0.6, 0.97, page_misses=1, bridge=True),
                      row(0.75, 0.5, 0.98), row(0.75, 0.5, 0.98, bridge=True)]}
    out = calibrate.decide(metrics, {"C2": COSTS["C2"]}, HAIKU)
    assert out["chosen"]["threshold"] == 0.75
    assert out["adopt"] is True


def test_no_qualifying_threshold_is_reported_not_adopted(calibrate):
    metrics = {"C2": rows({0.80: (0.6, 0.93), 0.85: (0.7, 0.90)})}  # both below Haiku 0.96 - 0.02
    out = calibrate.decide(metrics, {"C2": COSTS["C2"]}, HAIKU)
    assert out["adopt"] is False
    assert "Haiku" in out["reason"]


def test_without_a_haiku_baseline_nothing_qualifies(calibrate):
    metrics = {"C2": rows({0.80: (0.6, 0.97)})}
    out = calibrate.decide(metrics, {"C2": COSTS["C2"]}, {})
    assert out["adopt"] is False
