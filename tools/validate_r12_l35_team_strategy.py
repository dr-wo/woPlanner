#!/usr/bin/env python3
"""Reproduce the causal R12/L35 live team-strategy acceptance evidence.

Run this from the woPlanner feature worktree (or pass all three repository roots).
The only file this program writes is the explicit --output JSON path.  In
particular, model and strategy persistence are disabled and workspace writes are
rejected by an audit hook.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


COMPOUNDS = ("HARD", "MEDIUM", "SOFT")
LAP_CUTOFF = 35
SAMPLE_COUNT = 5_000


def _arguments() -> argparse.Namespace:
    here = Path(__file__).resolve()
    default_planner = here.parents[1]
    default_workspace = default_planner.parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--planner-root", type=Path, default=default_planner)
    parser.add_argument("--strategy-root", type=Path, default=default_workspace / "woStrategy")
    parser.add_argument("--data-root", type=Path, default=default_workspace / "woData")
    parser.add_argument(
        "--input-manifest",
        type=Path,
        help="Recovery manifest; defaults below DATA_ROOT for 2026 R12 Race.",
    )
    parser.add_argument(
        "--expected-feature-src",
        type=Path,
        help="Expected woplanner source directory; defaults to PLANNER_ROOT/src.",
    )
    parser.add_argument("--output", type=Path, required=True, help="Only output file (JSON).")
    return parser.parse_args()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _install_read_only_guard(repo_roots: tuple[Path, ...], output: Path) -> None:
    if any(_is_within(output, root) for root in repo_roots):
        raise SystemExit("--output must be outside all repository roots")

    def guard(event: str, args: tuple[Any, ...]) -> None:
        if event != "open":
            return
        path, mode, flags = args
        if not isinstance(path, (str, bytes, os.PathLike)):
            return
        candidate = Path(path).resolve()
        if not any(_is_within(candidate, root) for root in repo_roots):
            return
        writes = (isinstance(mode, str) and any(char in mode for char in "wax+")) or (
            isinstance(flags, int)
            and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
        )
        if writes:
            raise RuntimeError(f"validation forbids repository writes: {candidate}")

    sys.addaudithook(guard)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _module_path(module: Any) -> str:
    return str(Path(module.__file__).resolve())


def _stringify_overrides(values: dict[tuple[str, str], float]) -> dict[str, float]:
    return {f"{parameter}:{compound}": value for (parameter, compound), value in values.items()}


def main() -> None:
    args = _arguments()
    started = time.perf_counter()
    planner_root = args.planner_root.resolve()
    strategy_root = args.strategy_root.resolve()
    data_root = args.data_root.resolve()
    repo_roots = (planner_root, strategy_root, data_root)
    output = args.output.resolve()
    manifest_path = (args.input_manifest or (
        data_root
        / "wodata/live_timing/schema_v1/year=2026/round=12/session=R/recovered_replay_manifest.json"
    )).resolve()
    expected_feature_src = (args.expected_feature_src or planner_root / "src").resolve()

    for root in repo_roots:
        if not root.is_dir():
            raise SystemExit(f"repository root does not exist: {root}")
    if not manifest_path.is_file():
        raise SystemExit(f"input manifest does not exist: {manifest_path}")
    if not expected_feature_src.is_dir():
        raise SystemExit(f"feature source directory does not exist: {expected_feature_src}")

    # Put the selected repositories first, so a base checkout or installed wheel
    # cannot silently satisfy these imports.
    for source in reversed((planner_root / "src", strategy_root / "src", data_root / "src", data_root)):
        sys.path.insert(0, str(source.resolve()))
    sys.dont_write_bytecode = True
    _install_read_only_guard(repo_roots, output)

    import wodata  # noqa: PLC0415
    import woplanner.analysis.service as analysis_service_module  # noqa: PLC0415
    import woplanner.analysis.tyre_inputs as tyre_inputs_module  # noqa: PLC0415
    import woplanner.live.service as live_service_module  # noqa: PLC0415
    from wodata.contracts import StrategySearchState  # noqa: PLC0415
    from wodata.live import LiveSessionStore, LiveTimingMessage  # noqa: PLC0415
    from wodata.live.replay import load_replay  # noqa: PLC0415
    from wodata.live.replay.schema import parse_timestamp  # noqa: PLC0415
    from woplanner.analysis.service import (  # noqa: PLC0415
        OfflineAnalysisService,
        _completion_clock,
        _leader_lap,
        _live_analysis_laps,
        snapshot_as_of_leader_lap,
    )

    imports = {
        "woplanner.analysis.service": _module_path(analysis_service_module),
        "woplanner.analysis.tyre_inputs": _module_path(tyre_inputs_module),
        "woplanner.live.service": _module_path(live_service_module),
        "wodata": _module_path(wodata),
    }
    for name in ("woplanner.analysis.service", "woplanner.analysis.tyre_inputs", "woplanner.live.service"):
        if not _is_within(Path(imports[name]), expected_feature_src):
            raise AssertionError(f"{name} imported from stale source: {imports[name]}")
    if not (_is_within(Path(imports["wodata"]), data_root / "src") or _is_within(Path(imports["wodata"]), data_root)):
        raise AssertionError(f"wodata imported outside selected data root: {imports['wodata']}")
    print(json.dumps({"stage": "imports", "module_paths": imports}, sort_keys=True), flush=True)

    manifest = json.loads(manifest_path.read_text())
    events, invalid = load_replay(manifest["canonical_replay_path"])
    store = LiveSessionStore(allow_retirement_reactivation=True)
    consumed = 0
    cutoff_event = None
    snapshot = None
    for event in events:
        message = LiveTimingMessage(
            topic=event.topic,
            payload=event.payload,
            source_timestamp_utc=parse_timestamp(event.original_timestamp),
            received_at_utc=datetime.now(timezone.utc),
            schema_version=event.schema_version,
            source=event.source,
            sequence=event.sequence,
            session_time_seconds=(
                event.message_session_time_seconds
                if event.preserve_message_session_time
                else event.session_time_seconds
            ),
        )
        store.reduce(message)
        consumed += 1
        if event.topic != "TimingData" or not isinstance(event.payload, dict):
            continue
        lines = event.payload.get("Lines", {})
        reached_cutoff = any(
            isinstance(row, dict) and int(row.get("NumberOfLaps") or 0) >= LAP_CUTOFF
            for row in lines.values()
        )
        if reached_cutoff:
            candidate = store.snapshot()
            if _leader_lap(candidate) >= LAP_CUTOFF:
                snapshot = snapshot_as_of_leader_lap(candidate, LAP_CUTOFF)
                cutoff_event = event
                break
    if snapshot is None or cutoff_event is None:
        raise RuntimeError(f"causal replay never reached leader lap {LAP_CUTOFF}")

    assert _leader_lap(snapshot) == LAP_CUTOFF
    assert max(lap.lap_number for lap in snapshot.completed_laps) == LAP_CUTOFF
    cutoff_clock = max(_completion_clock(lap) for lap in snapshot.completed_laps)
    filtering = {
        "quick_lap_threshold": 1.1,
        "min_clean_laps": 4,
        "allowed_compounds": COMPOUNDS,
        "exclude_stint_start_laps": 0,
        "exclude_stint_end_laps": 0,
        "use_sector_boundary_traffic": True,
        "min_gap_ahead_seconds": 2.5,
        "min_gap_behind_seconds": 1.0,
    }
    clean = _live_analysis_laps(snapshot, **filtering)
    support = clean.groupby(["Team", "Compound"]).agg(
        laps=("LapNumber", "size"),
        runs=("RunId", "nunique"),
        min_lap=("LapNumber", "min"),
        max_lap=("LapNumber", "max"),
    ).reset_index().to_dict("records")
    print(json.dumps({
        "stage": "causal_cutoff",
        "leader_lap": LAP_CUTOFF,
        "messages_consumed": consumed,
        "clean_laps": len(clean),
        "clean_runs": int(clean.RunId.nunique()),
    }), flush=True)

    service = OfflineAnalysisService(data_root)
    model_result = service.recalculate_from_live_snapshot(
        year=2026,
        round_number=12,
        snapshot=snapshot,
        sample_count=SAMPLE_COUNT,
        persist=False,
        **filtering,
    )
    summary = service.load_model(2026, 12)
    assert model_result.sample_count == SAMPLE_COUNT

    state = StrategySearchState(
        current_completed_lap=LAP_CUTOFF,
        total_laps=int(service.last_live_calculation_inputs["event_context"]["scheduled_total_laps"]),
        current_compound="HARD",
        current_tyre_age=10,
        max_future_stops=2,
        provenance="validation_common_state",
    )
    original_build = service.build_strategy_model
    captures: list[dict[str, Any]] = []

    def capture_build(model_summary: Any, overrides: dict[tuple[str, str], float] | None = None) -> Any:
        model = original_build(model_summary, overrides)
        captures.append({
            "overrides": _stringify_overrides(overrides or {}),
            "strategy_model": asdict(model),
        })
        return model

    service.build_strategy_model = capture_build
    teams = service.strategy_teams(summary)
    if "Mercedes" not in teams or "McLaren" not in teams:
        raise AssertionError(f"required teams absent: {teams}")
    modes: dict[str, Any] = {}
    for team in (None, "Mercedes", "McLaren"):
        resolved = service.resolve_tyre_inputs(2026, 12, team=team)
        effective, assumptions = service.effective_tyre_inputs(
            summary, resolved, manual_override=False, team=team
        )
        plans = service.generate_continuations(
            year=2026,
            round_number=12,
            state=state,
            pit_loss=22.0,
            k=3,
            overrides=effective,
            persist=False,
            model_summary=summary,
            tyre_input_snapshot={"team": team, "assumptions": assumptions},
        )
        capture = captures[-1]
        for compound in COMPOUNDS:
            assert capture["strategy_model"]["degradation"][compound] == effective[("degradation", compound)]
            assert capture["strategy_model"]["compound_delta"][compound] == effective[("compound_delta", compound)]
        if team:
            for row in summary.parameters:
                if row.get("parameter") == "team_degradation" and row.get("team") == team:
                    assert math.isclose(
                        effective[("degradation", row["compound"])],
                        float(row["median"]),
                        rel_tol=0,
                        abs_tol=1e-14,
                    )
        modes[team or "Global"] = {
            "effective": _stringify_overrides(effective),
            "assumptions": [{
                key: row.get(key)
                for key in (
                    "compound",
                    "degradation_value_source",
                    "degradation_anchor_compound",
                    "degradation_initial_ratio_to_medium",
                    "compound_delta_source",
                    "performance_model_scope",
                )
            } for row in assumptions],
            "actual_build_boundary": capture,
            "search": {
                "result_count": len(plans),
                "best": asdict(plans[0]) if plans else None,
            },
        }

    assert modes["Global"]["effective"]["degradation:HARD"] != modes["Mercedes"]["effective"]["degradation:HARD"]
    assert modes["Mercedes"]["effective"]["degradation:HARD"] != modes["McLaren"]["effective"]["degradation:HARD"]
    mercedes_soft = next(row for row in modes["Mercedes"]["assumptions"] if row["compound"] == "SOFT")
    assert mercedes_soft["degradation_value_source"] == "team_live_derived"
    assert mercedes_soft["performance_model_scope"] == "global"

    wanted_parameters = []
    for row in summary.parameters:
        is_global = row.get("parameter") in ("degradation", "compound_delta")
        is_selected_team = row.get("parameter") == "team_degradation" and row.get("team") in ("Mercedes", "McLaren")
        if is_global or is_selected_team:
            wanted_parameters.append({
                key: row.get(key)
                for key in ("parameter", "team", "compound", "p10", "median", "p90", "ess")
            })

    quality = summary.manifest["sampler_quality"]
    evidence = {
        "schema": "r12_l35_team_strategy_validation_v1",
        "result": "PASS",
        "inputs": {
            "manifest": str(manifest_path),
            "manifest_sha256": _sha256(manifest_path),
            "accepted_sources": manifest["accepted_sources"],
            "repositories": {"planner": str(planner_root), "strategy": str(strategy_root), "data": str(data_root)},
            "module_paths": imports,
        },
        "run": {
            "year": 2026,
            "round": 12,
            "session": "R",
            "leader_lap_cutoff": LAP_CUTOFF,
            "sample_count": SAMPLE_COUNT,
            "persist": False,
            "filtering": filtering,
            "strategy_state": asdict(state),
            "pit_loss": 22.0,
            "k": 3,
        },
        "causal_cutoff": {
            "replay_total_messages": len(events),
            "invalid_records": invalid,
            "messages_consumed": consumed,
            "event": {
                "sequence": cutoff_event.sequence,
                "topic": cutoff_event.topic,
                "session_time_seconds": cutoff_event.session_time_seconds,
            },
            "completion_clock": cutoff_clock,
            "max_completed_lap": max(lap.lap_number for lap in snapshot.completed_laps),
            "max_clean_lap": int(clean.LapNumber.max()),
        },
        "clean_support": {
            "laps": len(clean),
            "runs": int(clean.RunId.nunique()),
            "by_team_compound": support,
        },
        "model": {
            "status": model_result.status,
            "analysis_id": summary.manifest["analysis_id"],
            "sample_bank_id": summary.manifest["sample_bank_id"],
            "input_fingerprint": summary.manifest["input_fingerprint"],
            "config_hash": summary.manifest["config_hash"],
            "parameters": wanted_parameters,
            "shared_sampler_quality": {
                key: quality[key]
                for key in ("sample_count", "ess", "ess_fraction", "baseline_group", "weight_strategy", "evaluator_backend")
            },
            "calculation_inputs": service.last_live_calculation_inputs,
        },
        "selectors": modes,
        "assertions": {
            "causal_l35_only": True,
            "sample_count_5000": True,
            "persist_false": True,
            "actual_build_consumed_all_effective_overrides": True,
            "team_direct_values_equal_absolute_team_medians": True,
            "global_mercedes_mclaren_differ": True,
            "mercedes_soft_is_team_live_derived": True,
            "team_performance_scope_is_global": True,
        },
        "notes": {
            "ess": "Each parameter repeats the shared global joint-sampler ESS; it is not team-specific evidence ESS.",
            "writes": "No durable model, strategy, manual, clean-CSV, or repository artifact was written.",
        },
        "elapsed_seconds": time.perf_counter() - started,
    }
    if not output.parent.is_dir():
        raise SystemExit(f"--output parent directory does not exist: {output.parent}")
    output.write_text(json.dumps(evidence, indent=2, default=str, allow_nan=False) + "\n")
    print(json.dumps({
        "stage": "complete",
        "output": str(output),
        "result": evidence["result"],
        "effective": {name: value["effective"] for name, value in modes.items()},
    }, indent=2), flush=True)


if __name__ == "__main__":
    main()
