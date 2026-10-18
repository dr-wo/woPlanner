"""Manual, non-persistent P1 reference using the established live Retro MC fit.

Only the rear-gap exclusion is waived. The operator assesses tactical suitability;
position one is never evidence of physically clear track ahead.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from wostrategy.analysis.long_run_performance import select_consecutive_clean_air_runs
from wostrategy.analysis.traffic import traffic_clean_mask
from wostrategy.model.live_retro_performance import run_live_retro_model

if TYPE_CHECKING:
    from .service import ModelSummary


@dataclass(frozen=True)
class P1ReferenceResult:
    status: str
    message: str
    driver_code: str
    first_lap: int
    last_lap: int
    leader_lap: int
    team: str = ""
    eligible_lap_count: int = 0
    selected_lap_numbers: tuple[int, ...] = ()
    summary: ModelSummary | None = None
    sampler_quality: tuple[tuple[str, object], ...] = ()
    exclusion_diagnostics: tuple[tuple[str, int], ...] = ()
    applicable_parameters: tuple[dict[str, object], ...] = ()
    elapsed_seconds: float = 0.0


def current_p1_driver(snapshot) -> str | None:
    """Use current published position, or committed P1 history if unavailable."""
    drivers = tuple(getattr(snapshot, "drivers", ()))
    leaders = [str(d.driver_code) for d in drivers
               if getattr(d, "position", None) == 1 and getattr(d, "driver_code", None)
               and not getattr(d, "retired", False)]
    if len(leaders) == 1:
        return str(leaders[0])
    if any(getattr(d, "position", None) is not None for d in drivers):
        return None
    leaders = [lap for lap in getattr(snapshot, "completed_laps", ())
               if getattr(lap, "position", None) == 1
               and getattr(lap, "driver_code", None)
               and getattr(lap, "lap_number", None) is not None]
    if not leaders:
        return None
    latest = max(lap.lap_number for lap in leaders)
    codes = {str(lap.driver_code) for lap in leaders if lap.lap_number == latest}
    return next(iter(codes)) if len(codes) == 1 else None


def select_p1_reference_laps(
    snapshot, *, driver_code: str, first_lap: int, last_lap: int,
    quick_lap_threshold: float, min_clean_laps: int,
    allowed_compounds: tuple[str, ...] = ("SOFT", "MEDIUM", "HARD"),
    exclude_stint_start_laps: int = 0, exclude_stint_end_laps: int = 0,
    min_gap_ahead_seconds: float = 2.5,
):
    from .service import (
        _exclude_clean_run_edges, _live_individual_clean_laps,
        _sector_as_selected_traffic, _sector_boundary_traffic_gaps,
    )

    if min_clean_laps < 1 or min(exclude_stint_start_laps, exclude_stint_end_laps) < 0:
        raise ValueError("Minimum clean laps must be positive and edge exclusions non-negative.")
    # Keep full observed context for fastest-lap and stint-age preparation. Do not
    # crop sectors: preceding physical crossings may belong to earlier laps.
    traffic = _sector_as_selected_traffic(
        _sector_boundary_traffic_gaps(snapshot, require_behind=False),
        reason="manual_p1_reference_rear_gap_bypass",
    )
    prepared = _live_individual_clean_laps(
        snapshot, quick_lap_threshold=quick_lap_threshold,
        allowed_compounds=allowed_compounds, use_sector_boundary_traffic=True,
        min_gap_ahead_seconds=min_gap_ahead_seconds, min_gap_behind_seconds=None,
        traffic_override=traffic,
    )
    requested = [lap for lap in snapshot.completed_laps
                 if lap.driver_code == driver_code and first_lap <= lap.lap_number <= last_lap]
    leader_laps = [lap for lap in requested if lap.position == 1]
    selected_numbers = {int(lap.lap_number) for lap in leader_laps}
    diagnostics = {
        "observed_interval_laps": len(requested),
        "not_p1_laps": len(requested) - len(leader_laps),
        "missing_tyre_metadata": sum(l.compound is None or l.tyre_life is None or l.stint is None for l in leader_laps),
        "invalid_lap": sum(not getattr(l, "is_valid", True) for l in leader_laps),
        "disallowed_compound": sum(l.compound is not None and str(l.compound).upper() not in tuple(c.upper() for c in allowed_compounds) for l in leader_laps),
    }
    if prepared.empty:
        prepared.attrs["exclusion_diagnostics"] = {**diagnostics, "selected_clean_laps": 0}
        return prepared
    prepared = prepared.loc[prepared.Driver.eq(driver_code) & prepared.LapNumber.isin(selected_numbers)].copy()
    # Fail closed on missing ahead evidence; shared compatibility rules otherwise
    # allow a missing side when the opposite side has evidence.
    ahead_ok = traffic_clean_mask(
        prepared, minimum_ahead_seconds=min_gap_ahead_seconds,
        minimum_behind_seconds=None, allow_missing_side_with_other_evidence=False,
    )
    prepared["IsCleanAirLongRunLap"] &= ahead_ok
    diagnostics.update({
        "pit_lap": int((prepared.IsOutLap | prepared.IsInLap).sum()),
        "non_green_track_status": int((~prepared.IsGreenTrackStatus).sum()),
        "non_quick_lap": int((~prepared.IsQuickLap).sum()),
        "traffic_ahead": int((~ahead_ok).sum()),
    })
    before_edges = int(prepared.IsCleanAirLongRunLap.sum())
    pieces = []
    run_offset = 0
    # Shared run selection assumes missing rows are represented as unclean.
    # Invalid metadata, wet compounds and non-P1 observations were removed;
    # partition numeric lap gaps explicitly so they cannot bridge a clean run.
    for _, stint in prepared.groupby(["Driver", "Stint"], sort=False):
        stint = stint.sort_values("LapNumber")
        for _, block in stint.groupby(stint.LapNumber.diff().ne(1).cumsum(), sort=False):
            block = block.copy()
            if exclude_stint_start_laps or exclude_stint_end_laps:
                _exclude_clean_run_edges(block, start_count=exclude_stint_start_laps,
                                         end_count=exclude_stint_end_laps)
            prepared.loc[block.index, "IsCleanAirLongRunLap"] = block.IsCleanAirLongRunLap
            selected_block = select_consecutive_clean_air_runs(block, min_clean_air_laps=int(min_clean_laps))
            if not selected_block.empty:
                selected_block["LongRunId"] += run_offset
                run_offset = int(selected_block.LongRunId.max())
                pieces.append(selected_block)
    individual = int(prepared.IsCleanAirLongRunLap.sum())
    diagnostics["stint_edge"] = before_edges - individual
    selected = (pd.concat(pieces) if pieces else
                select_consecutive_clean_air_runs(prepared.iloc[:0], min_clean_air_laps=int(min_clean_laps)))
    selected["RunId"] = selected["LongRunId"]
    diagnostics["insufficient_consecutive_laps"] = individual - len(selected)
    diagnostics["selected_clean_laps"] = len(selected)
    selected.attrs["exclusion_diagnostics"] = diagnostics
    return selected


def calculate_p1_reference(
    service, *, year: int, round_number: int, snapshot, driver_code: str,
    first_lap: int, last_lap: int, sample_count: int,
    quick_lap_threshold: float, min_clean_laps: int,
    allowed_compounds: tuple[str, ...] = ("SOFT", "MEDIUM", "HARD"),
    exclude_stint_start_laps: int = 0, exclude_stint_end_laps: int = 0,
    use_sector_boundary_traffic: bool = False, min_gap_ahead_seconds: float = 2.5,
    enforce_compound_order: bool | None = None,
    scheduled_total_laps_override: int | None = None,
) -> P1ReferenceResult:
    from .service import _leader_lap, _model_summary_from_snapshot, _resolve_scheduled_total_laps

    started = time.perf_counter()
    leader_lap = _leader_lap(snapshot)
    driver_code = str(driver_code)
    context = dict(driver_code=driver_code, first_lap=int(first_lap), last_lap=int(last_lap), leader_lap=leader_lap)
    leader = current_p1_driver(snapshot)
    driver_laps = [lap for lap in snapshot.completed_laps if lap.driver_code == driver_code]
    if leader != driver_code or not driver_laps:
        return P1ReferenceResult("invalid-selection", "Select the current published P1 with observed completed laps.", **context)
    team = str(driver_laps[-1].team)
    context["team"] = team
    if first_lap < 1 or last_lap < first_lap or last_lap > max(l.lap_number for l in driver_laps):
        return P1ReferenceResult("invalid-selection", "Choose an observed completed-lap interval; future laps are unavailable.", **context)
    if not use_sector_boundary_traffic:
        return P1ReferenceResult("traffic-unavailable", "Select sector-boundary traffic explicitly. P1 reference bypasses rear gap only; supported ahead traffic is still required.", **context)
    selected = select_p1_reference_laps(
        snapshot, driver_code=driver_code, first_lap=first_lap, last_lap=last_lap,
        quick_lap_threshold=quick_lap_threshold, min_clean_laps=min_clean_laps,
        allowed_compounds=allowed_compounds,
        exclude_stint_start_laps=exclude_stint_start_laps,
        exclude_stint_end_laps=exclude_stint_end_laps,
        min_gap_ahead_seconds=min_gap_ahead_seconds,
    )
    diagnostics = dict(selected.attrs.get("exclusion_diagnostics", {}))
    context.update(eligible_lap_count=len(selected), exclusion_diagnostics=tuple(diagnostics.items()))
    if selected.empty:
        reasons = ", ".join(f"{key.replace('_', ' ')}: {value}" for key, value in diagnostics.items()
                            if value and key not in {"observed_interval_laps", "selected_clean_laps"})
        return P1ReferenceResult("waiting-for-clean-laps", f"No eligible P1 run; need {min_clean_laps} consecutive clean laps. {reasons}. No other conditions were relaxed.", **context)
    context["selected_lap_numbers"] = tuple(int(n) for n in selected.LapNumber)
    config = service._live_retro_config(year, round_number, sample_count=sample_count,
                                        enforce_compound_order=enforce_compound_order)
    total_laps, distance_source = _resolve_scheduled_total_laps(snapshot,
        manual_override=scheduled_total_laps_override, current_leader_lap=leader_lap)
    result = run_live_retro_model(selected, config, as_of_leader_lap=leader_lap, race_lap_count=total_laps)
    quality = {**result.quality, "reference_kind": "manual_p1", "driver_code": driver_code,
               "selected_interval": (first_lap, last_lap), "rear_gap_exclusion_bypassed": True,
               "fuel_proxy_total_laps": total_laps, "fuel_proxy_total_laps_source": distance_source,
               "degradation_identifiability": "single-driver pace trend confounded with fuel and track corrections; prior-dependent"}
    summary = _model_summary_from_snapshot(result.aggregate_snapshot)
    summary.manifest.update(reference_kind="manual_p1", sampler_quality=quality)
    # A single driver's compound level is absorbed by its profiled intercept.
    # Never offer compound offsets/fuel/track/baseline as manual strategy inputs.
    # The effective team degradation is the quantity the team strategy consumes.
    weights = result.retro_result.sample_parameters["Weight"].to_numpy(float)
    valid_weights = bool(np.isfinite(weights).all() and (weights >= 0).all() and weights.sum() > 0)
    applicable = tuple({**row, "parameter": "degradation", "source_parameter": "team_degradation"}
                       for row in summary.parameters
                       if row.get("parameter") == "team_degradation" and row.get("team") == team
                       and all(math.isfinite(float(row[k])) for k in ("p10", "median", "p90")))
    if not valid_weights or quality.get("numerical_quality_status") == "degenerate":
        applicable = ()
    message = ("Separate P1 reference: rear-gap exclusion bypassed; other clean-lap rules retained. "
               "Tactical suitability is your assessment. Degradation is prior-dependent and confounded "
               "with fuel/track corrections; ESS is a sampling diagnostic, not proof of informative data. "
               "Only observed-compound effective degradation can be applied to the leader's team.")
    if not applicable:
        message += " No applicable values: invalid or degenerate sample weights, or missing team support."
    return P1ReferenceResult("updated" if valid_weights else "quality-unavailable", message,
        summary=summary, sampler_quality=tuple(quality.items()), applicable_parameters=applicable,
        elapsed_seconds=time.perf_counter() - started, **context)
