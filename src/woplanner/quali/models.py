"""Planner-owned qualifying state contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from wodata.live import QualiPitState, QualiTyreStint

if TYPE_CHECKING:
    from .track_evolution import TrackEvolutionDiagnostic


class FinishedReason(str, Enum):
    IN_PIT_NO_OUTLAP_TIME = "IN_PIT_NO_OUTLAP_TIME"
    NO_TIME_FOR_NEXT_PUSH = "NO_TIME_FOR_NEXT_PUSH"
    FINAL_LAP_COMPLETED = "FINAL_LAP_COMPLETED"
    ELIMINATED = "ELIMINATED"
    SESSION_FINALISED = "SESSION_FINALISED"


@dataclass(frozen=True)
class QualiDriverState:
    driver_number: str
    driver: str
    team: str
    rank: int | None = None
    # Feed classification is distinct from the calculated valid-lap rank.
    official_position: int | None = None
    status: str = "NO TIME"
    lap_state: str = "UNKNOWN"
    sectors_s: tuple[float | None, float | None, float | None] = (None, None, None)
    last_lap_s: float | None = None
    best_lap_s: float | None = None
    gap_s: float | None = None
    interval_s: float | None = None
    projected_lap_s: float | None = None
    projected_gap_s: float | None = None
    finished: bool = False
    finished_reason: FinishedReason | None = None
    pit_state: QualiPitState = QualiPitState.UNKNOWN
    pit_lane_progress: float | None = None
    tyre_history: tuple[QualiTyreStint, ...] = ()
    active: bool = True
    frozen_part: str | None = None
    historical_results: tuple[tuple[str, float | None], ...] = ()
    effective_finished: bool = False
    effective_finish_remaining_s: float | None = None
    effective_finish_recovery_s: float | None = None
    effective_finish_timing_source: str | None = None


@dataclass(frozen=True)
class CutoffProjection:
    """Raw slope contribution plus the published non-worsening threshold.

    ``projected_correction_s`` is the signed model contribution before the
    observed-result invariant; ``projected_cutoff_s`` is what the UI publishes.
    ``track_rate_s_per_field_push`` is signed lap-time seconds per global field
    PUSH: negative improves, zero is stable, positive worsens.
    """

    target_kind: str
    cutoff_rank: int
    driver: str | None
    current_cutoff_s: float | None
    unfinished_driver_count: int
    track_rate_s_per_field_push: float
    projected_correction_s: float
    projected_cutoff_s: float | None


@dataclass(frozen=True)
class QualiSessionSnapshot:
    session_part: str
    remaining_time_s: float | None
    session_status: str | None
    drivers: tuple[QualiDriverState, ...]
    cutoff: CutoffProjection
    actual_cutoffs: tuple[tuple[str, float | None], ...]
    track_source: str
    normal_finished: bool = False
    normal_finish_source: str | None = None
    clock_running: bool = False
    clock_anchor_session_time_s: float | None = None
    track_evolution_diagnostic: TrackEvolutionDiagnostic | None = None
    # Automatic provenance remains available while a manual override supplies
    # the effective rate in cutoff.track_rate_s_per_field_push / track_source.
    automatic_track_rate_s_per_field_push: float = 0.0
    automatic_track_source: str = "Configured fallback"
    telemetry_delay_warning: str = ""
    telemetry_delayed_lap_count: int = 0
    track_evolution_result: dict | None = None
    # Separate analysis products: neither writes operational FAST/finish state
    # nor replaces the legacy correction/Half-live comparison.
    q_only_track_evolution_result: dict | None = None
    q_only_track_evolution_diagnostic: TrackEvolutionDiagnostic | None = None
    final_cutoff_forecast: dict | None = None
