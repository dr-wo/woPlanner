#!/usr/bin/env python3
"""Read-only, offline Live MC readiness check. Exit 0: local checks pass; 2: blocked.

Run with the same Python environment used to launch woPlanner, for example:
  .venv/bin/python woPlanner/scripts/preflight_live_mc.py --year 2026 --round 18
No timing connection, prediction generation, MC fit, or artifact write is performed.
An optional --state-directory checks a specific recording's saved GUI selection.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import fields
import hashlib
import importlib
import importlib.metadata
import json
import math
import multiprocessing
import os
from pathlib import Path
import sys


def runtime_fingerprint():
    """Also executed in a genuine spawned Python process; never runs MC."""
    modules = (
        "woplanner.analysis.service", "woplanner.analysis.worker",
        "woplanner.analysis.gui", "woplanner.live.telemetry",
        "wodata.live.reducer", "wodata.live.controller", "wodata.live.models", "wostrategy",
    )
    sources = {}
    for name in modules:
        path = Path(importlib.import_module(name).__file__).resolve()
        sources[name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return {
        "python": sys.executable,
        "sources": sources,
        "versions": {name: importlib.metadata.version(name)
                     for name in ("woplanner", "wodata", "wostrategy", "PySide6")},
    }


def _check_bounds(config):
    int(config["random_seed"])
    for key in ("fuel_rate_bounds", "track_rate_bounds", "default_degradation_bounds",
                "default_compound_delta_bounds"):
        bounds = config[key]
        if len(bounds) != 2 or not all(math.isfinite(float(x)) for x in bounds):
            raise ValueError(f"{key} must contain two finite values")
        if float(bounds[0]) >= float(bounds[1]):
            raise ValueError(f"{key} must be strictly increasing")


def run_preflight(*, year, round_number, event_config, data_root=None,
                  state_directory=None, probe_worker=True):
    from wodata import get_data_root
    from wodata.artifacts import weekend_model_root
    from wodata.live.models import CompletedLap, LiveSessionSnapshot
    from woplanner.analysis.service import (
        LIVE_SECTOR_METHOD_VERSION, OfflineAnalysisService,
    )
    from woplanner.operational_inputs import load_operational_race_inputs
    from wostrategy.analysis.pre_race_tyre_prediction import get_pre_race_tyre_prediction

    # Match the default source checkout GUI's explicit sibling woData root.
    if data_root is None:
        sibling = Path(importlib.import_module("woplanner").__file__).resolve().parents[3] / "woData"
        data_root = sibling if sibling.is_dir() else get_data_root()
    root = get_data_root(data_root).resolve()
    model_root = weekend_model_root(year, round_number, root)
    service = OfflineAnalysisService(root)
    checks = []

    def record(name, function, *, mandatory=True):
        try:
            detail = function()
        except Exception as exc:
            checks.append({"check": name, "status": "blocked" if mandatory else "warning",
                           "detail": f"{type(exc).__name__}: {exc}"})
            return None
        checks.append({"check": name, "status": "passed", "detail": detail})
        return detail

    def event():
        operational = load_operational_race_inputs(event_config)
        payload = json.loads(Path(event_config).read_text())
        if (operational.season, operational.round_number) != (year, round_number):
            raise ValueError(f"Config is {operational.event_id}; requested {year:04d}-{round_number:02d}")
        if str(payload.get("session", "R")).upper() != "R":
            raise ValueError("Live MC requires Race session R")
        if operational.total_laps is None:
            raise ValueError("Race distance unresolved; set race.total_laps or verify live LapCount before fitting")
        return {"path": str(Path(event_config).resolve()), "event_id": operational.event_id,
                "session": "R", "scheduled_total_laps": operational.total_laps}

    record("event_identity_and_distance", event)
    def config():
        effective = service.live_model_config(year, round_number, "R")
        if not effective:
            raise ValueError("Missing event model_config.json; run the owning pre_race_analysis producer")
        _check_bounds(effective)
        service._live_retro_config(year, round_number, sample_count=int(effective.get("sample_count", 80000)),
                                   stored=effective)
        return {"path": str(model_root / "model_config.json"),
                "settings_source": effective.get("settings_source"),
                "sample_count": int(effective.get("sample_count", 80000))}
    record("mc_model_configuration", config)

    def prediction():
        result = get_pre_race_tyre_prediction(season=year, round_number=round_number, data_root=root)
        missing = set(("HARD", "MEDIUM", "SOFT")) - set(result.compounds)
        if missing:
            raise ValueError(f"Prediction lacks {sorted(missing)}")
        return {"artifact_id": result.artifact_id, "provenance": dict(result.provenance)}
    record("cached_pre_race_prediction", prediction, mandatory=False)

    def tyres():
        resolved = service.resolve_tyre_inputs(year, round_number)
        resolved.strategy_overrides()  # Same five required coordinates as the GUI.
        return {"artifact": resolved.legacy_artifact,
                "parameters": [{"parameter": row.parameter, "value": row.selected_value,
                                "source": row.selected_source} for row in resolved.parameters]}
    record("required_tyre_inputs", tyres)

    def state():
        path = (Path(state_directory) if state_directory else model_root) / "strategy_prediction_state.json"
        payload = json.loads(path.read_text()) if path.is_file() else {}
        if payload and (int(payload.get("year", -1)), int(payload.get("round_number", -1))) != (year, round_number):
            raise ValueError("Saved GUI state belongs to another event")
        controls = payload.get("controls", {})
        selected = controls.get("traffic_source")
        if selected is None:
            selected = "sector" if controls.get("sector_traffic_filter", False) else "automatic"
        if selected != "automatic":
            raise ValueError("Saved traffic selection is not Automatic")
        if not controls.get("auto_recalculate", True):
            raise ValueError("Saved automatic recalculation is disabled")
        return {"path": str(path), "saved": bool(payload), "requested": "automatic",
                "effective": "SECTOR_BOUNDARY_FALLBACK", "method_version": LIVE_SECTOR_METHOD_VERSION,
                "state_scope": "specified recording" if state_directory else "event default; a recording may have its own state"}
    record("automatic_gui_configuration", state)

    def contract():
        lap_fields = {row.name for row in fields(CompletedLap)}
        required = {"compound", "tyre_life", "stint", "traffic_sector_times_s", "traffic_recovered_sector"}
        if not required <= lap_fields:
            raise ValueError(f"Installed timing contract lacks {sorted(required - lap_fields)}")
        if "scheduled_total_laps" not in {row.name for row in fields(LiveSessionSnapshot)}:
            raise ValueError("Installed timing snapshot loses scheduled_total_laps")
        return {"input_recovery": True, "race_distance": True,
                "live_validation": "Await real feed driver/tyre metadata, LapCount and complete sector evidence"}
    record("timing_input_contract", contract)

    def storage():
        nearest = model_root
        while not nearest.exists() and nearest != nearest.parent:
            nearest = nearest.parent
        if not nearest.is_dir() or not os.access(nearest, os.W_OK | os.X_OK):
            raise PermissionError(f"Result parent is not writable: {nearest}")
        for path in model_root.rglob("*") if model_root.is_dir() else ():
            if path.is_file() and (path.name in {"manifest.json", "parameters.csv", "candidate_costs.csv"}
                                   or "history" in path.parts) and not os.access(path, os.W_OK):
                raise PermissionError(f"Existing result file is not writable: {path}")
        return {"result_root": str(model_root), "permission_check_only": True,
                "note": "No test write was performed; actual persistence and Qt publication require replay/live execution"}
    record("result_storage_permissions", storage)

    def saved_model():
        summary = service.load_model(year, round_number)
        return {"analysis_id": summary.manifest.get("analysis_id"),
                "as_of_leader_lap": summary.manifest.get("as_of_leader_lap"),
                "created_at": summary.manifest.get("created_at"),
                "fresh_this_process": False,
                "display": "Previously saved model; successful preflight is not a fresh MC calculation"}
    record("previous_model", saved_model, mandatory=False)
    fingerprint = record("installed_runtime", runtime_fingerprint)
    if probe_worker:
        def worker():
            with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
                child = pool.submit(runtime_fingerprint).result(timeout=45)
            if child != fingerprint:
                raise RuntimeError("Spawned worker imports differ from the launching process")
            return {"spawn": "passed", "same_runtime_and_source_hashes": True, "ran_mc": False}
        record("spawned_worker_import", worker)
    else:
        checks.append({"check": "spawned_worker_import", "status": "warning", "detail": "Explicitly skipped"})
    blocked = any(row["status"] == "blocked" for row in checks)
    return {
        "schema_version": 1, "event": f"{year:04d}-{round_number:02d}",
        "status": "NOT READY" if blocked else "CONDITIONAL",
        "local_preflight_passed": not blocked, "exit_code": 2 if blocked else 0,
        "data_root": str(root), "checks": checks,
        "not_validated": ["future live network and authentication", "actual clean laps and tyre metadata",
                          "current GUI process loaded these hashes", "fresh MC persistence and Qt publication"],
    }


def main(argv=None):
    from woplanner.operational_inputs import DEFAULT_EVENT_CONFIG_PATH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", required=True, type=int)
    parser.add_argument("--round", required=True, type=int, dest="round_number")
    parser.add_argument("--event-config", type=Path, default=DEFAULT_EVENT_CONFIG_PATH)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--state-directory", type=Path)
    parser.add_argument("--skip-worker-probe", action="store_true")
    args = parser.parse_args(argv)
    result = run_preflight(year=args.year, round_number=args.round_number,
                           event_config=args.event_config, data_root=args.data_root,
                           state_directory=args.state_directory, probe_worker=not args.skip_worker_probe)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
