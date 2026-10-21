"""Offline preflight must reject incomplete future inputs and never create them."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "preflight_live_mc.py"
spec = importlib.util.spec_from_file_location("live_mc_preflight", SCRIPT)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def prepare(tmp_path, *, year=2026, round_number=18):
    event = tmp_path / "race.json"
    event.write_text(json.dumps({"season": year, "round_number": round_number,
                                 "session": "R", "race": {"total_laps": 56}}))
    root = tmp_path / "data"
    model = root / "wostrategy/weekend_model/schema_v1" / f"year={year}" / f"round={round_number}"
    model.mkdir(parents=True)
    (model / "model_config.json").write_text(json.dumps({
        "sample_count": 80000, "random_seed": 13, "fuel_rate_bounds": [0.01, 0.1],
        "track_rate_bounds": [-0.1, 0.1], "default_degradation_bounds": [0.01, 0.2],
        "default_compound_delta_bounds": [-2, 2],
    }))
    (model / "manual_tyre_inputs_v1.json").write_text(json.dumps({
        "schema_version": 1, "year": year, "round_number": round_number,
        "values": {"Deg_HARD": 0.05, "Deg_MEDIUM": 0.07, "Deg_SOFT": 0.1,
                   "Perf_HARD_vs_MEDIUM": 0.5, "Perf_SOFT_vs_MEDIUM": -0.5},
    }))
    return event, root, model


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def invoke(event, root):
    return preflight.run_preflight(year=2026, round_number=18, event_config=event,
                                  data_root=root, probe_worker=False)


def test_local_checks_accept_real_manual_contract_without_generating_prediction(tmp_path):
    event, root, _ = prepare(tmp_path)
    before = snapshot(tmp_path)
    result = invoke(event, root)
    assert result["local_preflight_passed"], result
    assert result["status"] == "CONDITIONAL"
    assert result["exit_code"] == 0
    assert "future live network and authentication" in result["not_validated"]
    assert snapshot(tmp_path) == before
    prediction = next(row for row in result["checks"] if row["check"] == "cached_pre_race_prediction")
    assert prediction["status"] == "warning"


@pytest.mark.parametrize("problem", ["identity", "distance", "model", "tyres", "automatic", "disabled", "bounds", "legacy_sector"])
def test_missing_or_conflicting_inputs_are_specific_blockers(tmp_path, problem):
    event, root, model = prepare(tmp_path)
    if problem in {"identity", "distance"}:
        payload = json.loads(event.read_text())
        if problem == "identity":
            payload["round_number"] = 17
        else:
            payload["race"]["total_laps"] = None
        event.write_text(json.dumps(payload))
    elif problem == "model":
        (model / "model_config.json").unlink()
    elif problem == "tyres":
        (model / "manual_tyre_inputs_v1.json").unlink()
    elif problem == "bounds":
        payload = json.loads((model / "model_config.json").read_text())
        payload["fuel_rate_bounds"] = [0.1, 0.01]
        (model / "model_config.json").write_text(json.dumps(payload))
    else:
        controls = {"traffic_source": "sector" if problem == "automatic" else "automatic",
                    "auto_recalculate": problem != "disabled"}
        if problem == "legacy_sector":
            controls = {"sector_traffic_filter": True}
        (model / "strategy_prediction_state.json").write_text(json.dumps({
            "year": 2026, "round_number": 18,
            "controls": controls,
        }))
    before = snapshot(tmp_path)
    result = invoke(event, root)
    assert result["exit_code"] == 2
    assert not result["local_preflight_passed"]
    assert any(row["status"] == "blocked" for row in result["checks"])
    assert snapshot(tmp_path) == before


def test_cli_requires_explicit_target_event():
    with pytest.raises(SystemExit) as exc:
        preflight.main([])
    assert exc.value.code == 2
