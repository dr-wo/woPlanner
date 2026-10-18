"""Compare the two lifecycle repairs against frozen pre-repair source files.

The baseline adapter is explicitly bound to the baseline shared evaluator, so
the before run cannot accidentally inherit the repaired state/phase logic.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from statistics import median
import sys

import pandas as pd

from verify_quali_traffic_phase_review import ROOT, DATA, audit, load_module
from woplanner.quali.physical_traffic import RollingQualiPhysicalTraffic
from wostrategy.analysis import traffic


def lifecycle_reproductions(adapter_class, evaluator):
    sys.path.insert(0, str(ROOT / "tests"))
    from test_quali_traffic_lifecycle import completion_series
    from test_quali_traffic_review_blockers import real_field, timing

    store, adapter = real_field(adapter_type=adapter_class,
                                starts={"A": 100., "B": 94., "C": 98.})
    timing(store, adapter, "B", {"InPit": True}, 104.25)
    timing(store, adapter, "B", {"InPit": False}, 105.)
    rows = pd.DataFrame([row for values in adapter._rows.values() for row in values])
    states = pd.DataFrame([dict(row, Driver=number)
                           for number, values in adapter._candidate_states.items() for row in values])
    result = evaluator.evaluate_physical_traffic(
        rows, config=evaluator.TrafficEvaluationConfig(1000., require_phase_validity=True),
        candidate_states=states, target_lap=("A", 2))
    samples = result.samples.set_index("SessionTime")
    returned = {}
    for stamp in (104., 104.5, 105., 105.5):
        row = samples.loc[stamp]
        returned[str(stamp)] = {key: None if pd.isna(row[key]) else row[key]
                               for key in ("PhysicalDriverAhead", "PhysicalDistanceToDriverAhead",
                                           "PhysicalTimeDeltaToDriverAhead")}
        returned[str(stamp)]["uncertain"] = bool(row.TrafficPhaseUnavailable)

    _, history, keys = completion_series(adapter_type=adapter_class)
    before = list(history._span_history.items())
    before_median = median(history._lap_spans)
    repeated = []
    for _ in range(3):
        history.finalize("A", 2)
        repeated.append(median(history._lap_spans))
    return {
        "on_track_return": returned,
        "history_eviction": {
            "completion_count": len(keys), "active_count": len(before),
            "oldest_key": keys[0], "oldest_evicted_before_refinalization": keys[0] not in dict(before),
            "median_before_m": before_median, "medians_after_repeated_finalization_m": repeated,
            "history_unchanged": before == list(history._span_history.items()),
            "active_history_before": before, "active_history_after": list(history._span_history.items()),
        },
    }


def compatibility(baseline):
    strategy = ROOT.parent / "woStrategy"
    fixture = load_module("lifecycle_shared_fixture", strategy / "tests/test_quali_traffic_phase_validity.py")
    full, states = fixture.returning_field()
    cases = [("anchored_return", full, ("A", 1), 1000., states),
             ("partial_return", full[~((full.Driver == "B") & (full.SessionTime > 101))],
              ("A", 1), 1000., states)]
    saved = strategy / "tests/fixtures/quali_traffic_phase"
    provenance = json.loads((saved / "provenance.json").read_text())
    for driver in ("OCO", "BEA"):
        cases.append((driver, pd.read_csv(saved / f"{driver}.csv.gz"), (driver, 6),
                      provenance[driver]["circuit_length_m"], None))
    results = []
    for name, frame, target, length, candidate_states in cases:
        outputs = [module.evaluate_physical_traffic(
            frame, config=module.TrafficEvaluationConfig(length, require_phase_validity=False),
            target_lap=target, candidate_states=candidate_states) for module in (baseline, traffic)]
        pd.testing.assert_frame_equal(outputs[0].samples, outputs[1].samples)
        pd.testing.assert_frame_equal(outputs[0].laps, outputs[1].laps)
        results.append({"case": name, "samples": len(outputs[0].samples),
                        "samples_identical": True, "lap_summaries_identical": True})
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, nargs="*", default=[13, 14, 16])
    args = parser.parse_args()
    baseline_paths = [args.baseline_directory / name for name in ("traffic.py", "physical_traffic.py")]
    baseline_evaluator = load_module("lifecycle_baseline_evaluator", baseline_paths[0])
    baseline_adapter = load_module("lifecycle_baseline_adapter", baseline_paths[1])
    for name in ("evaluate_physical_traffic", "validated_complete_lap_span", "lap_span_within_circuit_range"):
        setattr(baseline_adapter, name, getattr(baseline_evaluator, name))
    result = {
        "baseline_sha256": {str(path): sha256(path.read_bytes()).hexdigest() for path in baseline_paths},
        "reproductions": {
            "before": lifecycle_reproductions(baseline_adapter.RollingQualiPhysicalTraffic, baseline_evaluator),
            "after": lifecycle_reproductions(RollingQualiPhysicalTraffic, traffic),
        },
        "phase_validity_disabled_compatibility": compatibility(baseline_evaluator),
        "events": [],
    }
    old, new = (result["reproductions"][key] for key in ("before", "after"))
    assert old["on_track_return"]["105.0"]["PhysicalDistanceToDriverAhead"] == 200.
    assert old["on_track_return"]["105.0"]["PhysicalTimeDeltaToDriverAhead"] == 2.
    assert not old["on_track_return"]["105.0"]["uncertain"]
    assert new["on_track_return"]["105.0"]["uncertain"]
    assert new["on_track_return"]["105.0"]["PhysicalDriverAhead"] is None
    assert old["on_track_return"]["104.5"] == new["on_track_return"]["104.5"]
    assert old["history_eviction"]["median_before_m"] == new["history_eviction"]["median_before_m"] == 1100.
    assert old["history_eviction"]["medians_after_repeated_finalization_m"] == [1000.] * 3
    assert new["history_eviction"]["medians_after_repeated_finalization_m"] == [1100.] * 3
    assert new["history_eviction"]["history_unchanged"]
    sources = [Path(__file__), ROOT / "scripts/verify_quali_traffic_phase_review.py",
               ROOT / "src/woplanner/quali/physical_traffic.py",
               ROOT / "tests/test_quali_traffic_lifecycle.py",
               ROOT / "tests/test_quali_traffic_review_blockers.py",
               ROOT.parent / "woStrategy/src/wostrategy/analysis/traffic.py",
               ROOT.parent / "woStrategy/tests/test_quali_traffic_phase_validity.py",
               DATA / "src/wodata/live/quali_models.py", DATA / "src/wodata/live/quali_reducer.py"]
    result["source_sha256"] = {str(path): sha256(path.read_bytes()).hexdigest() for path in sources}
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    for round_number in args.rounds:
        result["events"].append(audit(round_number, baseline_adapter.RollingQualiPhysicalTraffic))
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"reproductions_verified": True,
                      "compatibility": result["phase_validity_disabled_compatibility"],
                      "events": [{"round": event["round"], "metrics": event["metrics"],
                                  "traffic_changes": event["traffic_changes"]} for event in result["events"]]},
                     indent=2))


if __name__ == "__main__":
    main()
