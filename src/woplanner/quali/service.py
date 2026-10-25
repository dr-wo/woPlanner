"""Live qualifying application service."""

from __future__ import annotations

from dataclasses import replace
from collections import OrderedDict, deque
import math
from pathlib import Path
from time import monotonic
from functools import lru_cache
import json
from importlib.resources import files

from .runtime_paths import ArtifactError, RuntimePaths, resolve_runtime_paths

from wodata.live import (
    FastF1LiveTimingSource,
    LiveConfig,
    LiveTimingController,
    PitLaneGeometry,
    QualiPitState,
    QualiRawDriverState,
    QualiRawSnapshot,
    QualiSessionStore,
    ReplayConfig,
)

from ..common.session_info import parse_session_identity
from .models import (
    CutoffProjection,
    FinishedReason,
    QualiDriverState,
    QualiSessionSnapshot,
)
from .lap_state import LapPaceReference, classify_lap_state
from .planning import SPRINT_QUALI_PARTS, STANDARD_QUALI_PARTS, QualiTimingValues
from .track_evolution import (
    TrackEvolutionDiagnostic, TrackEvolutionSample,
)
from .physical_traffic import PhysicalLapTraffic, RollingQualiPhysicalTraffic
from .traffic_finalisation import PendingTraffic, TelemetryDelayMonitor
from .q_only_track import QOnlyTrackEvolution
from wostrategy.analysis.quali_forecast import (
    DriverPace, ForecastPrior, forecast_final_cutoff, forecast_partial_field_bounds,
)


QUALI_PARTS = (*STANDARD_QUALI_PARTS, *SPRINT_QUALI_PARTS)


@lru_cache(maxsize=1)
def _packaged_cutoff_prior():
    """Read the frozen chronological calibration once, outside message work."""
    try:
        path = files("wostrategy.analysis").joinpath("quali_forecast_prior_v1.json")
        return ForecastPrior.from_dict(json.loads(path.read_text(encoding="utf-8"))), None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, f"Calibration prior unavailable: {type(exc).__name__}: {exc}"


def _finite_float(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


class QualiLiveService:
    def __init__(
        self,
        controller: LiveTimingController | None = None,
        *,
        eliminated_counts: dict[str, int] | None = None,
        out_lap_s: float = 100.0,
        cool_lap_s: float | None = None,
        track_rate_s_per_field_push: float = 0.0,
        push_threshold_fraction: float = 0.02,
        clean_air_gap_s: float = 3.0,
        pit_lane_geometry: PitLaneGeometry | None = None,
        telemetry_delay_lap_threshold: int = 3,
        telemetry_delay_window_s: float = 600.,
        shadow_track_evolution: bool = False,
        runtime_paths: RuntimePaths | None = None,
    ) -> None:
        self.runtime_paths = runtime_paths or resolve_runtime_paths()
        self.operational_artifact_root: Path | None = None
        self.operational_artifact_error: str | None = None
        self._geometry_data_root = self.runtime_paths.resource_root
        self.controller = controller or LiveTimingController(
            store=QualiSessionStore(pit_lane_geometry=pit_lane_geometry)
        )
        self.eliminated_counts = {part: 6 for part in QUALI_PARTS}
        self.eliminated_counts.update(eliminated_counts or {})
        self.operational_timings = QualiTimingValues(
            out_lap_s=float(out_lap_s),
            cool_lap_s=None if cool_lap_s is None else float(cool_lap_s),
        )
        self.operational_timing_sources = {
            name: "built-in fallback" for name in vars(self.operational_timings)
        }
        self.operational_timing_sources["cool_lap_s"] = (
            "unavailable — manual override required"
        )
        self._fp_timing_values: dict[str, float] = {}
        self._fp_timing_sources: dict[str, str] = {}
        self._fp_best_lap_s: float | None = None
        self._fp_best_lap_source = "FP best"
        self._manual_timing_values: dict[str, float] = {}
        self._manual_timing_sources: dict[str, str] = {}
        if out_lap_s != QualiTimingValues().out_lap_s:
            self._manual_timing_values["out_lap_s"] = float(out_lap_s)
            self._manual_timing_sources["out_lap_s"] = "manual override"
            self.operational_timing_sources["out_lap_s"] = "manual override"
        if cool_lap_s is not None:
            self._manual_timing_values["cool_lap_s"] = float(cool_lap_s)
            self._manual_timing_sources["cool_lap_s"] = "manual override"
            self.operational_timing_sources["cool_lap_s"] = "manual override"
        self._operational_artifact_identity: tuple[int, int] | None = None
        self._message_driven = isinstance(self.controller, LiveTimingController)
        if self._message_driven:
            self.controller.on_message_snapshot = self._consume_message_snapshot
        self._fp_track_rate_s_per_field_push = float(track_rate_s_per_field_push)
        self._track_fallback_provenance = {"kind": "CONFIGURED_DEFAULT", "reference": None}
        self._automatic_track_rate_s_per_field_push = (
            self._fp_track_rate_s_per_field_push
        )
        self._automatic_track_source = "Configured fallback"
        self._track_override_s_per_field_push: float | None = None
        self.track_rate_s_per_field_push = self._automatic_track_rate_s_per_field_push
        self.push_threshold_fraction = float(push_threshold_fraction)
        self.clean_air_gap_s = float(clean_air_gap_s)
        self.track_source = "Configured fallback"
        self.raw_snapshot = QualiRawSnapshot.empty()
        self.session_part = "Q1"
        self._initial_timing_received = False
        self._participation_pending: set[str] = set()
        self._local_participation: set[str] = set()
        self._states: dict[str, QualiDriverState] = {}
        self._completed_laps: dict[str, int] = {}
        self._seen_completed_lap_keys: set[tuple[str, str, int, float | None]] = set()
        self._new_completed_lap_numbers: dict[str, set[int]] = {}
        self.lap_state_references: dict[str, LapPaceReference] = {}
        self._common_lap_state_reference: LapPaceReference | None = None
        self._slow_lap_evidence: list[tuple[str, float]] = []
        self._reference_total_s: float | None = None
        self._reference_checkpoints: dict[tuple[str, int], float] = {}
        self._driver_best_references: dict[str, tuple[float, dict[tuple[str, int], float]]] = {}
        self._actual_cutoffs: dict[str, float | None] = {}
        # Legacy traffic evaluation slots only: these samples and indices never
        # supply the live estimator. The canonical policy owns its global index.
        self._track_samples: list[TrackEvolutionSample] = []
        self._physical_traffic = RollingQualiPhysicalTraffic()
        self._pending_traffic = OrderedDict()
        self.traffic_finalisation_records = deque(maxlen=1024)
        self._track_samples_revision = 0
        self._traffic_now = None
        self._traffic_clock_kind = None
        self._traffic_wall_anchor = None
        self._traffic_source_ended = False
        self.telemetry_delays = TelemetryDelayMonitor(
            threshold=telemetry_delay_lap_threshold, window_s=telemetry_delay_window_s,
        )
        self._latest_raw: dict[str, QualiRawDriverState] = {}
        self._event_geometry_identity = None
        if self.runtime_paths.isolated:
            self._operational_artifact_identity = None
        self.received_session_info = self.raw_snapshot.session_info
        self.received_session_name = ""
        self.session_is_qualifying: bool | None = None
        from .track_evolution_policy import TrackEvolutionPolicy
        self.track_evolution_policy = TrackEvolutionPolicy()
        self.q_only_track_evolution = QOnlyTrackEvolution()
        self._final_cutoff_prior, self._final_cutoff_prior_error = _packaged_cutoff_prior()
        self._final_cutoff_key = None
        self._final_cutoff_result = None
        self.final_cutoff_calculation_count = 0
        self._track_publication_in_progress = False
        # Deprecated opt-in debug handle aliases the single production state.
        self.shadow_track_evolution = self.track_evolution_policy if shadow_track_evolution else None
        self.snapshot = self._build_snapshot()

    def start_live(self, config: LiveConfig) -> None:
        self.reset()
        if self.runtime_paths.isolated:
            config = replace(config, data_root=self.runtime_paths.check_output())
        else:
            self._geometry_data_root = config.data_root
        self._load_geometry(config.year, config.round_number, self._geometry_data_root)
        self.controller.start_live(config)

    def start_replay(self, config: ReplayConfig) -> None:
        self.reset()
        self.controller.start_replay(config)

    def _load_geometry(
        self, year: int, round_number: int, data_root, *, force: bool = False
    ) -> None:
        store = getattr(self.controller, "store", None)
        if not isinstance(store, QualiSessionStore):
            return
        if store.pit_lane_geometry is not None and not force:
            return
        if force:
            store.pit_lane_geometry = None
        if self.runtime_paths.isolated:
            from wodata.artifacts import pit_lane_geometry_path
            self.runtime_paths.checked(self.runtime_paths.resource_root, pit_lane_geometry_path(
                year=year, round_number=round_number, data_root=data_root
            ))
        try:
            from wodata.artifacts import read_pit_lane_geometry

            geometry = read_pit_lane_geometry(
                year=year, round_number=round_number, data_root=data_root
            )
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            return
        store.pit_lane_geometry = PitLaneGeometry(
            tuple(tuple(point) for point in geometry["centreline"]),
            float(geometry.get("maximum_distance", 400.0)),
        )

    def configure_event_geometry(
        self, year: int, round_number: int, data_root: str | Path | None = None
    ) -> None:
        self._geometry_data_root = self.runtime_paths.resource_root if self.runtime_paths.isolated else data_root
        self._load_geometry(year, round_number, self._geometry_data_root, force=True)

    def reset(self) -> None:
        self.track_evolution_policy.reset()
        self.q_only_track_evolution.reset()
        self._final_cutoff_key = None
        self._final_cutoff_result = None
        self._track_publication_in_progress = False
        self.raw_snapshot = QualiRawSnapshot.empty()
        self.session_part = "Q1"
        self._initial_timing_received = False
        self._participation_pending.clear()
        self._local_participation.clear()
        self._states.clear()
        self._completed_laps.clear()
        self._seen_completed_lap_keys.clear()
        self._new_completed_lap_numbers.clear()
        self._reference_total_s = None
        self._reference_checkpoints.clear()
        self._driver_best_references.clear()
        self._actual_cutoffs.clear()
        self._track_samples.clear()
        self._physical_traffic = RollingQualiPhysicalTraffic()
        self._pending_traffic.clear()
        self.traffic_finalisation_records.clear()
        self._track_samples_revision = 0
        self._traffic_now = None
        self._traffic_clock_kind = None
        self._traffic_wall_anchor = None
        self._traffic_source_ended = False
        self.telemetry_delays = TelemetryDelayMonitor(
            threshold=self.telemetry_delays.threshold, window_s=self.telemetry_delays.window_s,
        )
        self._automatic_track_rate_s_per_field_push = (
            self._fp_track_rate_s_per_field_push
        )
        self._automatic_track_source = "Configured fallback"
        self._track_override_s_per_field_push = None
        self.track_rate_s_per_field_push = self._automatic_track_rate_s_per_field_push
        self.track_source = self._automatic_track_source
        self._latest_raw.clear()
        self._event_geometry_identity = None
        if self.runtime_paths.isolated:
            self._operational_artifact_identity = None
        self.received_session_info = self.raw_snapshot.session_info
        self.received_session_name = ""
        self.session_is_qualifying = None
        self.snapshot = self._build_snapshot()

    def set_fp_track_rate(self, value: float, *, reference: str | None = None,
                          loaded_fp: bool = False) -> None:
        self._fp_track_rate_s_per_field_push = float(value)
        self._track_fallback_provenance = {
            "kind": "LOADED_FP_PRIOR" if loaded_fp else "CONFIGURED_DEFAULT",
            "reference": reference,
        }
        self.snapshot = self._build_snapshot()

    def set_track_override(self, value: float | None) -> None:
        self._track_override_s_per_field_push = (
            None if value is None else float(value)
        )
        self.snapshot = self._build_snapshot()

    @property
    def automatic_track_rate_s_per_field_push(self) -> float:
        """Signed lap-time seconds per global field PUSH; negative improves."""
        return self._automatic_track_rate_s_per_field_push

    @property
    def automatic_track_source(self) -> str:
        return self._automatic_track_source

    @property
    def automatic_track_estimate(
        self,
    ) -> tuple[float | None, int, str] | None:
        diagnostic = self.track_evolution_diagnostic()
        rate = _finite_float(diagnostic.aggregate_slope_s_per_push)
        if rate is None:
            return None
        return rate, diagnostic.lap_count, "estimated"

    @property
    def out_lap_s(self) -> float:
        return self.operational_timings.out_lap_s

    @property
    def cool_lap_s(self) -> float | None:
        return self.operational_timings.cool_lap_s

    @property
    def track_override_s_per_field_push(self) -> float | None:
        return self._track_override_s_per_field_push

    def set_operational_timings(self, timings: QualiTimingValues, *, source: str) -> None:
        """Compatibility entry point for an explicit manual timing policy."""
        self.operational_timings = timings
        for category, value in vars(timings).items():
            if value is None:
                continue
            self._manual_timing_values[category] = value
            self._manual_timing_sources[category] = source
            self.operational_timing_sources[category] = source

    def set_clean_air_gap_s(self, value: float) -> None:
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Operational timing must be positive and finite")
        if value <= 0 or not math.isfinite(value):
            raise ValueError("clean-air threshold must be positive and finite")
        self.clean_air_gap_s = value
        self.snapshot = self._build_snapshot()

    def set_manual_operational_timing(self, category: str, value: float) -> None:
        if category not in vars(self.operational_timings):
            raise ValueError(f"Unknown operational timing category: {category}")
        value = float(value)
        self._manual_timing_values[category] = value
        self._manual_timing_sources[category] = "manual override"
        self.operational_timings = replace(self.operational_timings, **{category: value})
        self.operational_timing_sources[category] = "manual override"

    def clear_manual_operational_timings(self) -> None:
        self._manual_timing_values.clear()
        self._manual_timing_sources.clear()
        self._apply_operational_sources()

    def load_operational_analysis(self, result) -> None:
        self._fp_timing_values.clear()
        self._fp_timing_sources.clear()
        self._fp_best_lap_s = None
        self._fp_best_lap_source = "FP best"
        for _, row in result.durations.iterrows():
            value = _finite_float(row.get("selected_default_s"))
            category = str(row["category"])
            if value is not None and value > 0 and category in vars(self.operational_timings):
                self._fp_timing_values[category] = value
                self._fp_timing_sources[category] = (
                    f"FP-derived (n={int(row['selected_count'])})"
                )
        laps = getattr(result, "laps", None)
        if laps is not None and hasattr(laps, "columns"):
            if "is_driver_fp_reference" in laps.columns and "raw_lap_time_s" in laps.columns:
                marked = laps.loc[laps["is_driver_fp_reference"].fillna(False)]
                references = [_finite_float(value) for value in marked["raw_lap_time_s"]]
                self._fp_best_lap_s = min(
                    (value for value in references if value is not None and value > 0),
                    default=None,
                )
        if self._fp_best_lap_s is None:
            self._fp_best_lap_s = self._fp_timing_values.get("push_lap_s")
            self._fp_best_lap_source = "FP representative"
        self._apply_operational_sources()
        self._load_lap_state_references(laps)

    def _load_lap_state_references(self, laps) -> None:
        self.lap_state_references = {}
        self._common_lap_state_reference = None
        if (
            laps is None
            or not hasattr(laps, "columns")
            or not {"is_driver_fp_reference"}.issubset(laps.columns)
        ):
            return
        references = {}
        rows = laps.loc[laps["is_driver_fp_reference"].fillna(False)]
        if rows.empty:
            return
        row = rows.iloc[0]
        reference = LapPaceReference(
                _finite_float(row.get("fp_start_to_5pct_s")),
                _finite_float(row.get("fp_start_to_s1_s")),
                _finite_float(row.get("fp_s1_to_s2_s")),
                f"FP common reference: {row.get('driver', 'unknown')}",
            )
        self._common_lap_state_reference = reference

    def _apply_operational_sources(self) -> None:
        values = vars(QualiTimingValues()).copy()
        sources = {name: "built-in fallback" for name in values}
        sources["cool_lap_s"] = "unavailable — manual override required"
        for category, value in self._fp_timing_values.items():
            values[category] = value
            sources[category] = self._fp_timing_sources[category]
        if "cool_lap_s" not in self._fp_timing_values and self._fp_best_lap_s is not None:
            values["cool_lap_s"] = self._fp_best_lap_s * 1.30
            sources["cool_lap_s"] = f"{self._fp_best_lap_source} × 1.30 fallback"
        for category, value in self._manual_timing_values.items():
            values[category] = value
            sources[category] = self._manual_timing_sources[category]
        self.operational_timings = QualiTimingValues(**values)
        self.operational_timing_sources = sources

    def _effective_cool_timing(self, current_fastest_s: float | None = None) -> tuple[float | None, str]:
        return self.operational_timings.cool_lap_s, self.operational_timing_sources["cool_lap_s"]

    def reload_operational_artifacts(self, year: int, round_number: int) -> None:
        """Re-select saved artifacts after an explicit FP analysis write."""
        self._operational_artifact_identity = None
        self._load_operational_artifact(year, round_number)

    def _load_operational_artifact(self, year: int, round_number: int) -> None:
        identity = (year, round_number)
        if identity == self._operational_artifact_identity:
            return
        self._operational_artifact_identity = identity
        try:
            from wodata.artifacts import read_pre_quali_analysis

            if self.runtime_paths.isolated:
                (laps, durations, _), self.operational_artifact_root = self.runtime_paths.read_fp(year, round_number)
                self.operational_artifact_error = None
            else:
                laps, durations, _ = read_pre_quali_analysis(
                    year=year, round_number=round_number,
                    data_root=getattr(self, "_geometry_data_root", None),
                )
        except (FileNotFoundError, ValueError, OSError) as exc:
            if self.runtime_paths.isolated:
                self.operational_artifact_root = None
                self.operational_artifact_error = str(exc)
                if not isinstance(exc, FileNotFoundError):
                    self._operational_artifact_identity = None
                    raise ArtifactError(str(exc)) from exc
            self._fp_timing_values.clear()
            self._fp_timing_sources.clear()
            self._fp_best_lap_s = None
            self._fp_best_lap_source = "FP best"
            self._apply_operational_sources()
            return
        self.load_operational_analysis(type("Analysis", (), {"laps": laps, "durations": durations})())

    def _consume_message_snapshot(self, raw):
        self.received_session_info = raw.session_info
        identity = parse_session_identity(raw.session_info)
        self.received_session_name = identity.session_name
        event_key = (identity.year, identity.round_number)
        if all(event_key) and event_key != self._event_geometry_identity:
            self._event_geometry_identity = event_key
            self._load_geometry(*event_key, getattr(self, "_geometry_data_root", None), force=True)
            self._load_operational_artifact(*event_key)
        if identity.is_qualifying is False:
            if self.session_is_qualifying is not False:
                self.reset()
                self.received_session_info = raw.session_info
                self.received_session_name = identity.session_name
            self.session_is_qualifying = False
            return
        if identity.is_qualifying is None:
            return
        self.session_is_qualifying = True
        self.consume(raw)

    def poll(self):
        result = self.controller.drain()
        # Real controllers deliver every intermediate message state, not just
        # the final state of a drain. Snapshot-only test adapters remain usable.
        if not self._message_driven:
            self._consume_message_snapshot(result.snapshot)
        traffic_changed = False
        if any(status.state in {"disconnected", "error"} for status in result.statuses):
            self._traffic_source_ended = True
        queued = getattr(self.controller, "_messages", None)
        # A drain is capped at 500 messages. Reduce already-received evidence
        # before a wall-clock deadline can freeze its lap, including at EOF.
        if queued is None or queued.empty():
            now = self._traffic_now
            if self._traffic_wall_anchor is not None:
                clock, wall = self._traffic_wall_anchor
                now = clock + max(0., monotonic() - wall)
            if self._traffic_source_ended:
                traffic_changed = self.advance_traffic_clock(now, force=True, reason="source ended")
                self._traffic_source_ended = False
            elif self._traffic_wall_anchor is not None:
                traffic_changed = self.advance_traffic_clock(now)
        return self.snapshot, result.statuses, (result.changed or traffic_changed) and self.session_is_qualifying is True

    def consume(self, raw: QualiRawSnapshot) -> QualiSessionSnapshot:
        source = getattr(self.controller, "_source", None)
        if (getattr(raw, "message_source", None) == "signalr_live"
                and isinstance(source, FastF1LiveTimingSource)):
            receipt = getattr(raw, "receive_monotonic_s", None)
            # Acquisition/ordinary clocks remain untouched. Active live
            # traffic uses source-captured receipt; an old envelope without
            # it cannot certify an elapsed live deadline.
            raw = replace(raw, traffic_clock_time_s=receipt,
                          traffic_clock_kind="live_monotonic" if receipt is not None else None,
                          receive_time_trusted=raw.receive_time_trusted and receipt is not None)
        self._track_publication_in_progress = True
        self.track_evolution_policy.begin_publication(raw)
        self.q_only_track_evolution.begin_publication(raw)
        previous_part = self.session_part
        previous_part_established = getattr(self.raw_snapshot, "session_part_established", True)
        if (raw.session_part != previous_part and previous_part_established
                and self._pending_traffic):
            self.advance_traffic_clock(force=True, reason="segment transition")
        # Keep the last established target interval when withdrawal removes
        # its current bounds; dependent traffic can still lose support.
        for pending in self._pending_traffic.values():
            bounds = self._physical_traffic.completion_bounds(pending.key)
            if bounds is not None:
                pending.acquisition_bounds = bounds
                if hasattr(self._physical_traffic, "completion_coordinate"):
                    pending.acquisition_coordinate = self._physical_traffic.completion_coordinate(pending.key)
        # Resolve strictly earlier deadlines before a new receipt can trim
        # acquisition history or withdraw derived calibration. Equal-time
        # receipts still belong to that deadline and are ingested first.
        self._resolve_prior_traffic_deadlines(raw)
        self.raw_snapshot = raw
        self._protect_calibration_history()
        self._physical_traffic.ingest(raw)
        clock = getattr(raw, "traffic_clock_time_s", None)
        kind = getattr(raw, "traffic_clock_kind", None)
        if clock is not None:
            if self._traffic_clock_kind not in {None, kind}:
                self.advance_traffic_clock(force=True, reason="clock changed")
                self._traffic_now = None
            self._traffic_clock_kind = kind
            self._traffic_now = max(clock, self._traffic_now if self._traffic_now is not None else clock)
            # Poll advances only a genuine active live source. Recorded and
            # archive playback progress comes exclusively from message clocks.
            source = getattr(self.controller, "_source", None)
            self._traffic_wall_anchor = (
                (self._traffic_now, self._traffic_now)
                if getattr(raw, "message_source", None) == "signalr_live"
                and isinstance(source, FastF1LiveTimingSource)
                and kind == "live_monotonic"
                else None
            )
        if raw.session_part != previous_part:
            if not self._states or not previous_part_established:
                self.session_part = raw.session_part
            else:
                self.transition_to(raw.session_part)
                # Local transitions have already selected their participants.
                self._initial_timing_received = True
        self._new_completed_lap_numbers = {}
        for event in raw.completed_lap_sequence:
            # Legacy completion bookkeeping remains available under version
            # skew. This tuple is never used as projection provenance.
            key = getattr(event, "event_key", None)
            if key is None:
                key = (event.session_part, event.driver_number, event.lap_number, event.session_time_s)
            if key in self._seen_completed_lap_keys:
                continue
            self._seen_completed_lap_keys.add(key)
            self.telemetry_delays.completed(key, clock, getattr(raw, "receive_time_trusted", False))
            if event.session_part == raw.session_part:
                self._new_completed_lap_numbers.setdefault(event.driver_number, set()).add(event.lap_number)
        for driver in raw.drivers:
            self._reconcile_participation(driver)
            self._consume_driver(driver)
        self.q_only_track_evolution.consume_new_completions(
            raw, {number for number, state in self._states.items() if state.active},
        )
        for key in getattr(self._physical_traffic, "changed_revoked_keys", ()):
            self.telemetry_delays.revoke(key)
        self.telemetry_delays.observe(raw, self._physical_traffic)
        if clock is not None:
            self.advance_traffic_clock(clock, rebuild=False)
        if str(raw.session_status or "").lower() in {"aborted", "finalised", "ends", "inactive"}:
            self.advance_traffic_clock(force=True, reason="session stopped", rebuild=False)
        if raw.drivers:
            # This reports resolution of the current view, never roster closure.
            self._initial_timing_received = (
                self.session_part in {"Q1", "SQ1"}
                or all(d.driver_number in self._local_participation
                       or d.knocked_out is not None for d in raw.drivers)
            )
        self._refresh_projection_reference()
        for number, driver in self._latest_raw.items():
            state = self._states.get(number)
            if state is not None and state.active:
                projected = self._project(driver)
                self._states[number] = replace(state, projected_lap_s=projected)
        self._rank_and_project()
        self.track_evolution_policy.finish_publication(self)
        self.q_only_track_evolution.finish_publication(self)
        self._track_publication_in_progress = False
        self.snapshot = self._build_snapshot()
        return self.snapshot

    def _reconcile_participation(self, driver: QualiRawDriverState) -> None:
        if (self.session_part in {"Q1", "SQ1"}
                or driver.driver_number in self._local_participation):
            return
        number = driver.driver_number
        existing = self._states.get(number)
        if existing is None:
            existing = QualiDriverState(number, driver.driver_code, driver.team)
            self._states[number] = existing
        if driver.knocked_out is None:
            self._participation_pending.add(number)
            self._states[number] = replace(existing, active=False)
        elif driver.knocked_out:
            if existing.finished_reason != FinishedReason.ELIMINATED:
                # Freeze the same physical/official row as ordinary initial
                # bootstrap, without admitting laps from an unknown participant.
                existing = replace(
                    existing, rank=None, official_position=driver.position,
                    status="NO TIME",
                    lap_state=classify_lap_state(
                        driver, previous_state=existing.lap_state, new_lap=False,
                        reference=self.lap_state_references.get(number, self._common_lap_state_reference)),
                    pit_state=driver.pit_state, pit_lane_progress=driver.pit_lane_progress,
                    tyre_history=driver.tyre_history,
                )
            self._participation_pending.discard(number)
            history = dict(driver.historical_results)
            frozen = max(history, key=lambda part: int(part[-1])) if history else (
                self.session_part[:-1] + str(int(self.session_part[-1]) - 1))
            self._states[number] = replace(
                existing, active=False, finished=True,
                finished_reason=FinishedReason.ELIMINATED, frozen_part=frozen,
                best_lap_s=None, last_lap_s=None, sectors_s=(None, None, None),
                projected_lap_s=None, gap_s=None, interval_s=None, projected_gap_s=None,
                effective_finished=False, effective_finish_remaining_s=None,
                effective_finish_recovery_s=None, effective_finish_timing_source=None,
            )
        elif number in self._participation_pending:
            self._participation_pending.discard(number)
            self._states[number] = replace(existing, active=True)

    def _consume_driver(self, raw: QualiRawDriverState) -> None:
        existing = self._states.get(raw.driver_number)
        if existing is not None and not existing.active:
            self._states[raw.driver_number] = replace(
                existing, driver=raw.driver_code, team=raw.team,
                historical_results=raw.historical_results)
            return
        previous_laps = self._completed_laps.get(raw.driver_number)
        completed_lap = (
            previous_laps is not None
            and raw.completed_laps > previous_laps
            and raw.last_lap_s is not None
            and bool(self._new_completed_lap_numbers.get(raw.driver_number))
        )
        # Read the current feed snapshot, not the previous service states: a
        # deletion and another driver's completion can share one update.
        current_valid_bests = (
            driver.best_lap_s for driver in self.raw_snapshot.drivers
            if driver.best_lap_s is not None
            and (self._states.get(driver.driver_number) is None
                 or self._states[driver.driver_number].active)
        )
        fastest_before = min(current_valid_bests, default=None)
        lap_state = classify_lap_state(
            raw,
            previous_state=existing.lap_state if existing else None,
            new_lap=completed_lap,
            reference=self.lap_state_references.get(raw.driver_number, self._common_lap_state_reference),
        )
        # Operational FAST classification is independent of evolution admission.
        completed_operational_push = (
            completed_lap
            and raw.timed_lap_eligible
            and lap_state == "FAST"
        )
        # Preserve the reviewed physical-traffic evaluation schedule. This legacy
        # field+2 subset is auxiliary only and cannot admit or veto observations.
        auxiliary_traffic_eligible = (
            completed_operational_push
            and fastest_before is not None
            and raw.last_lap_s <= fastest_before * (1.0 + self.push_threshold_fraction)
        )
        if completed_lap:
            self.track_evolution_policy.consider(
                raw, self.raw_snapshot, completed_operational_push, fastest_before,
                max(self._new_completed_lap_numbers[raw.driver_number]),
            )
        self._latest_raw[raw.driver_number] = raw
        if auxiliary_traffic_eligible:
            tyre = raw.tyre_history[-1] if raw.tyre_history else None
            completed_number = max(self._new_completed_lap_numbers[raw.driver_number])
            clock = getattr(self.raw_snapshot, "traffic_clock_time_s", None)
            traffic = (self._physical_traffic.finalize(raw.driver_code, completed_number)
                       if clock is None else PhysicalLapTraffic(status="pending"))
            self._track_samples.append(TrackEvolutionSample(
                self.session_part, raw.driver_code, raw.team,
                completed_number,
                float(raw.last_lap_s), len(self._track_samples) + 1,
                tyre.compound if tyre else None,
                tyre.start_laps if tyre else None,
                tyre.fresh if tyre else None,
                traffic.mean_ahead_gap_s, traffic.ahead_coverage,
                traffic.status, traffic.method, traffic.target_samples,
                traffic.ahead_samples, traffic.evaluator_version,
                (traffic.mean_ahead_gap_s > self.clean_air_gap_s)
                if traffic.available else None,
                self.clean_air_gap_s,
                traffic.circuit_length_m,
                tyre.stint_index if tyre else None,
            ))
            # Forward auxiliary metadata under the exact completion identity.
            event = next((event for event in reversed(self.raw_snapshot.completed_lap_sequence)
                          if event.session_part == self.session_part
                          and event.driver_number == raw.driver_number
                          and event.lap_number == completed_number), None)
            event_key = getattr(event, "event_key", None)
            if clock is not None:
                if event_key is not None:
                    self._pending_traffic.setdefault(event_key, PendingTraffic(
                        event_key, raw.driver_code, completed_number,
                        len(self._track_samples) - 1, clock,
                        self.raw_snapshot.traffic_clock_kind,
                        acquisition_bounds=self._physical_traffic.completion_bounds(event_key),
                        acquisition_coordinate=(self._physical_traffic.completion_coordinate(event_key)
                                                if hasattr(self._physical_traffic, "completion_coordinate") else None),
                    ))
                    if len(self._pending_traffic) > 1024:
                        oldest_key = next(iter(self._pending_traffic))
                        oldest = self._pending_traffic.pop(oldest_key)
                        self._publish_traffic(oldest, min(clock, oldest.normal_deadline), "pending capacity")
                else:
                    # Version-skewed completion objects keep ordinary timing,
                    # but cannot certify a revisable physical traffic result.
                    self._track_samples[-1] = replace(self._track_samples[-1], traffic_status="unknown")
            if event_key is not None:
                self.track_evolution_policy.update_traffic(event_key, self._track_samples[-1])
        elif completed_lap and existing is not None and existing.lap_state == "SLOW":
            # A completed non-pit SLOW lap is operational COOL/PREP evidence,
            # never a PUSH/track-evolution sample.
            self._slow_lap_evidence.append((raw.driver_number, float(raw.last_lap_s)))
        self._completed_laps[raw.driver_number] = raw.completed_laps
        projected = self._project(raw)
        finished = existing.finished if existing else False
        reason = existing.finished_reason if existing else None
        remaining = self.raw_snapshot.remaining_time_s
        effective = existing.effective_finished if existing else False
        effective_remaining = existing.effective_finish_remaining_s if existing else None
        recovery = existing.effective_finish_recovery_s if existing else None
        timing_source = existing.effective_finish_timing_source if existing else None
        cool_requirement, cool_source = self._effective_cool_timing()
        clock_has_started = remaining is not None and (
            remaining > 0 or self.raw_snapshot.normal_finished
        )
        # Reuse the existing opportunity thresholds: OUT from the pit, or
        # COOL/PREP after a completed PUSH, to reach the next PUSH start.
        pit_no_outlap_time = clock_has_started and raw.in_pit and remaining < self.out_lap_s
        push_no_recovery_time = (
            clock_has_started and completed_operational_push
            and cool_requirement is not None and remaining < cool_requirement
        )
        if not effective and (pit_no_outlap_time or push_no_recovery_time):
            effective = True
            effective_remaining = remaining
            recovery, timing_source = (
                (self.out_lap_s, self.operational_timing_sources["out_lap_s"])
                if pit_no_outlap_time else (cool_requirement, cool_source)
            )
        # A recognised lap after the real chequered flag supersedes an earlier
        # predictive finish.  An archive bootstrap zero is not a finish.
        if completed_lap and self.raw_snapshot.normal_finished:
            finished, reason = True, FinishedReason.FINAL_LAP_COMPLETED
        elif clock_has_started and not finished:
            if pit_no_outlap_time:
                finished, reason = True, FinishedReason.IN_PIT_NO_OUTLAP_TIME
            elif push_no_recovery_time:
                finished, reason = True, FinishedReason.NO_TIME_FOR_NEXT_PUSH
        # Current official timing is authoritative: a deletion/retraction may
        # clear the best entirely, so never retain a historical minimum here.
        best = raw.best_lap_s
        state = QualiDriverState(
            raw.driver_number,
            raw.driver_code,
            raw.team,
            official_position=raw.position,
            lap_state=lap_state,
            sectors_s=raw.sectors_s,
            last_lap_s=raw.last_lap_s,
            best_lap_s=best,
            projected_lap_s=projected,
            finished=finished,
            finished_reason=reason,
            pit_state=raw.pit_state,
            pit_lane_progress=raw.pit_lane_progress,
            tyre_history=raw.tyre_history,
            historical_results=raw.historical_results,
            effective_finished=effective,
            effective_finish_remaining_s=effective_remaining,
            effective_finish_recovery_s=recovery,
            effective_finish_timing_source=timing_source,
        )
        self._states[raw.driver_number] = state
        # Revalidate identity and evidence even when the displayed best time
        # stays equal (deletion, correction, or a late sector publication).
        checkpoints = _associated_best_sector_checkpoints(
            raw, self.raw_snapshot.completed_lap_sequence, self.session_part
        )
        if checkpoints:
            self._driver_best_references[raw.driver_number] = (best, checkpoints)
        else:
            self._driver_best_references.pop(raw.driver_number, None)

    def _refresh_projection_reference(self) -> None:
        """Use the fastest *current valid* result as the projection reference."""
        candidates = (
            (state.best_lap_s, number)
            for number, state in self._states.items()
            if state.active and state.best_lap_s is not None
            and number in self._driver_best_references
        )
        best = min(candidates, default=None, key=lambda item: (item[0], item[1]))
        self._reference_total_s = best[0] if best is not None else None
        self._reference_checkpoints = (
            self._driver_best_references[best[1]][1] if best is not None else {}
        )

    def _project(self, raw: QualiRawDriverState) -> float | None:
        if not raw.timed_lap_eligible:
            return None
        if self._reference_total_s is None:
            return None
        checkpoints = _checkpoint_elapsed(raw)
        common = [key for key in checkpoints if key in self._reference_checkpoints]
        if not common:
            return raw.last_lap_s
        priority = lambda key: (key[0] == "segment", key[1])
        key = max(common, key=priority)
        reference_elapsed = self._reference_checkpoints[key]
        if reference_elapsed <= 0:
            return None
        return checkpoints[key] * self._reference_total_s / reference_elapsed

    def _rank_and_project(self) -> None:
        active = [state for state in self._states.values() if state.active]
        ranked = sorted(
            active,
            key=lambda state: (
                state.best_lap_s is None,
                state.best_lap_s or float("inf"),
                state.driver,
            ),
        )
        leader = next(
            (
                state.best_lap_s
                for state in ranked
                if state.best_lap_s is not None
            ),
            None,
        )
        previous = None
        for rank, state in enumerate(ranked, start=1):
            gap = (
                state.best_lap_s - leader
                if state.best_lap_s is not None and leader is not None
                else None
            )
            interval = (
                state.best_lap_s - previous
                if state.best_lap_s is not None and previous is not None
                else None
            )
            projected_gap = (
                state.projected_lap_s - leader
                if state.projected_lap_s is not None and leader is not None
                else None
            )
            self._states[state.driver_number] = replace(
                state,
                rank=rank,
                gap_s=gap,
                interval_s=interval,
                projected_gap_s=projected_gap,
                status=state.lap_state,
            )
            if state.best_lap_s is not None:
                previous = state.best_lap_s

    def transition_to(self, new_part: str) -> None:
        new_part = new_part.upper()
        if new_part == self.session_part:
            return
        self.advance_traffic_clock(force=True, reason="segment transition", rebuild=False)
        current = [state for state in self._states.values() if state.active]
        current.sort(
            key=lambda state: (
                state.official_position is None,
                state.official_position if state.official_position is not None else state.rank or 999,
                state.driver,
            )
        )
        eliminated_count = min(
            self.eliminated_counts.get(self.session_part, 0), len(current)
        )
        advancing_count = len(current) - eliminated_count
        if self.session_part in {"Q1", "Q2", "SQ1", "SQ2"}:
            cutoff = self._completed_part_cutoff_s(current, advancing_count)
        else:
            cutoff = current[advancing_count - 1].best_lap_s if advancing_count > 0 else None
        self._actual_cutoffs[self.session_part] = cutoff
        eliminated_numbers = {state.driver_number for state in current[advancing_count:]}
        for number, state in tuple(self._states.items()):
            if number in eliminated_numbers:
                self._states[number] = replace(
                    state,
                    active=False,
                    finished=True,
                    finished_reason=FinishedReason.ELIMINATED,
                    frozen_part=self.session_part,
                    best_lap_s=None, last_lap_s=None,
                    sectors_s=(None, None, None), projected_lap_s=None,
                    gap_s=None, interval_s=None, projected_gap_s=None,
                    status="UNKNOWN", lap_state="UNKNOWN",
                    effective_finished=False, effective_finish_remaining_s=None,
                    effective_finish_recovery_s=None, effective_finish_timing_source=None,
                )
            elif state.active:
                self._states[number] = QualiDriverState(
                    state.driver_number,
                    state.driver,
                    state.team,
                    pit_state=state.pit_state,
                    pit_lane_progress=state.pit_lane_progress,
                    tyre_history=state.tyre_history,
                )
        self._local_participation = set(self._states) - self._participation_pending
        self.session_part = new_part
        self._completed_laps.clear()
        self._latest_raw.clear()
        self._reference_total_s = None
        self._reference_checkpoints.clear()
        self._driver_best_references.clear()
        self.snapshot = self._build_snapshot()

    def _completed_part_cutoff_s(
        self, participants: list[QualiDriverState], advancing_count: int,
    ) -> float | None:
        """Last advancing time in the reducer's final corrected part results.

        The participant count includes untimed drivers. Current minima and
        next-part feed positions may already be reset; neither supplies history.
        Fewer valid results than advancing places means no timed threshold.
        """
        numbers = {state.driver_number for state in participants}
        results = []
        for driver in self.raw_snapshot.drivers:
            if driver.driver_number not in numbers:
                continue
            best = _finite_float(dict(driver.historical_results).get(self.session_part))
            if best is not None and best > 0:
                results.append(best)
        results.sort()
        return results[advancing_count - 1] if 0 < advancing_count <= len(results) else None

    def _update_track_from_diagnostic(self, diagnostic: TrackEvolutionDiagnostic) -> None:
        """Resolve manual > supported session diagnostic > existing FP prior.

        All rates are signed lap-time change in seconds per global field PUSH:
        negative improves, zero is stable, positive worsens. The generic pooled
        estimator's improvement-positive rate must never enter this live path.
        """
        rate = _finite_float(diagnostic.aggregate_slope_s_per_push)
        if rate is None:
            self._automatic_track_rate_s_per_field_push = self._fp_track_rate_s_per_field_push
            self._automatic_track_source = "Configured fallback"
        else:
            self._automatic_track_rate_s_per_field_push = rate
            self._automatic_track_source = "Live per-driver diagnostic (session-wide)"
        if self._track_override_s_per_field_push is None:
            self.track_rate_s_per_field_push = self._automatic_track_rate_s_per_field_push
            self.track_source = self._automatic_track_source
        else:
            self.track_rate_s_per_field_push = self._track_override_s_per_field_push
            self.track_source = "Manual override"

    def shadow_track_evolution_payload(self):
        """Deprecated debug entry point; returns the new production schema."""
        if self.shadow_track_evolution is None:
            return None
        return self.shadow_track_evolution.payload(self)

    def track_evolution_payload(self, *, detailed=True):
        """Production quality/source result and optional exact publication ledger."""
        return self.track_evolution_policy.payload(self, detailed=detailed)

    def readiness_diagnostics(self):
        """Describe current inputs without changing classification or forecasts."""
        def usable(reference):
            return reference is not None and any(
                value is not None and math.isfinite(value) and value > 0
                for value in (reference.start_to_5pct_s, reference.start_to_s1_s)
            )

        common_available = usable(self._common_lap_state_reference)
        missing = tuple(sorted(
            state.driver for number, state in self._states.items()
            if state.active and not usable(self.lap_state_references.get(
                number, self._common_lap_state_reference))
        ))
        cool = self.cool_lap_s
        cool_available = cool is not None and math.isfinite(cool) and cool > 0
        return {
            "pace_reference_available": common_available or any(
                usable(reference) for reference in self.lap_state_references.values()),
            "common_pace_reference_available": common_available,
            "drivers_without_pace_reference": missing,
            "cool_duration_available": cool_available,
            "cool_duration_s": cool if cool_available else None,
            "cool_duration_source": self.operational_timing_sources["cool_lap_s"],
        }

    def set_final_cutoff_prior(self, prior: ForecastPrior | None) -> None:
        """Explicit calibration selection; never alter legacy correction state."""
        if prior is not None:
            prior._validate()
        self._final_cutoff_prior = prior
        self._final_cutoff_prior_error = None
        self._final_cutoff_key = None
        self.snapshot = replace(self.snapshot, final_cutoff_forecast=self._final_cutoff_payload())

    def _final_cutoff_payload(self):
        raw = self.raw_snapshot
        identity = parse_session_identity(raw.session_info)
        event_id = (f"{identity.year}-R{identity.round_number}"
                    if identity.year and identity.round_number else None)
        active = [state for state in self._states.values() if state.active]
        source = {driver.driver_number: driver for driver in raw.drivers}
        drivers = []
        for state in active:
            driver = source.get(state.driver_number)
            previous = (dict(driver.historical_results).get("Q1")
                        if driver is not None and self.session_part == "Q2" else None)
            no_further_attempt = (
                raw.normal_finished and driver is not None
                and (driver.in_pit is True
                     or state.finished_reason == FinishedReason.FINAL_LAP_COMPLETED)
            )
            drivers.append(DriverPace(state.driver, state.best_lap_s, previous,
                                     False if no_further_attempt else None))
        # A chequered/zero clock alone does not rule out laps already in flight.
        finalised = (raw.normal_finished
                     and str(raw.session_status or "").lower() in {"finalised", "ends"})
        remaining = raw.remaining_time_s
        numeric_remaining = _finite_float(remaining)
        bucket = int(numeric_remaining // 30) if numeric_remaining is not None else None
        key = (event_id, self.session_part, tuple(drivers), bucket, finalised,
               self._final_cutoff_prior.revision if self._final_cutoff_prior else None)
        if key != self._final_cutoff_key:
            self._final_cutoff_result = forecast_final_cutoff(
                drivers, event_id=event_id, part=self.session_part,
                remaining_s=remaining, prior=self._final_cutoff_prior,
                part_finalised=finalised,
            )
            missing_anchor = any(
                driver.current_best_s is None
                and (self.session_part != "Q2" or driver.previous_part_best_s is None)
                for driver in drivers
            )
            if (not finalised and self._final_cutoff_prior is not None and missing_anchor
                    and self._final_cutoff_result.status == "UNAVAILABLE"
                    and self.session_part in {"Q1", "Q2"}):
                self._final_cutoff_result = forecast_partial_field_bounds(
                    drivers, event_id=event_id, part=self.session_part,
                    remaining_s=remaining, prior=self._final_cutoff_prior,
                )
            self._final_cutoff_key = key
            self.final_cutoff_calculation_count += 1
        result = self._final_cutoff_result
        limitations = list(result.limitations)
        if self._final_cutoff_prior_error:
            limitations.append(self._final_cutoff_prior_error)
        return {
            "status": result.status, "source": result.source,
            "event_id": result.event_id, "part": result.part,
            "target_rank": result.target_rank,
            "estimate_s": result.estimate_s, "lower_s": result.lower_s,
            "upper_s": result.upper_s, "interval_coverage": result.interval_coverage,
            "prior_revision": result.prior_revision, "scenario_count": result.scenario_count,
            "training_events": list(result.training_events),
            "current_pace_count": result.current_pace_count,
            "previous_part_pace_count": result.previous_part_pace_count,
            "driver_count": result.driver_count,
            "calculated_remaining_s": result.remaining_s,
            "remaining_bucket_s": 30, "limitations": limitations,
        }

    def track_evolution_diagnostic(self):
        """The promoted policy's one immutable shared per-driver diagnostic."""
        policy = self.track_evolution_policy
        # Intermediate part/traffic rebuilds must not create intra-publication
        # fits or jump history. Finish the causal publication before refreshing.
        if not self._track_publication_in_progress or policy._diagnostic is None:
            policy.refresh()
        return policy._diagnostic

    def finalise(self) -> None:
        self.advance_traffic_clock(force=True, reason="session finalised", rebuild=False)
        final_part = "SQ3" if self.session_part.startswith("SQ") else "Q3"
        for number, state in tuple(self._states.items()):
            if state.active:
                self._states[number] = replace(
                    state,
                    finished=True,
                    finished_reason=FinishedReason.SESSION_FINALISED,
                    frozen_part=final_part,
                )
        self._actual_cutoffs.setdefault(final_part, None)
        self.snapshot = self._build_snapshot()

    def pit_status_groups(self) -> dict[str, tuple[str, ...]]:
        fired = sorted(
            state.driver
            for state in self._states.values()
            if state.pit_state in {QualiPitState.FIRE_UP, QualiPitState.FIRED_UP}
        )
        queue = sorted(
            (
                state
                for state in self._states.values()
                if state.pit_state == QualiPitState.QUEUE
            ),
            key=lambda state: -(
                state.pit_lane_progress
                if state.pit_lane_progress is not None
                else -1
            ),
        )
        on_track = sorted(
            state.driver
            for state in self._states.values()
            if state.pit_state == QualiPitState.ON_TRACK
        )
        return {
            "Fired-up": tuple(fired),
            "Queue": tuple(state.driver for state in queue),
            "On-track": tuple(on_track),
        }

    @property
    def recording_path(self) -> Path | None:
        return self.controller.recording_path

    def stop(self) -> None:
        self.controller.stop()
        queued = getattr(self.controller, "_messages", None)
        while queued is not None and not queued.empty():
            self.controller.drain()
        self.advance_traffic_clock(force=True, reason="session interrupted")

    def _protect_calibration_history(self):
        protect = getattr(self._physical_traffic, "protect_calibration_cutoffs", None)
        if protect is not None:
            protect((deadline, pending.clock_kind) for pending in self._pending_traffic.values()
                    for deadline in (pending.normal_deadline, pending.hard_deadline))

    def _resolve_prior_traffic_deadlines(self, raw):
        clock = getattr(raw, "traffic_clock_time_s", None)
        kind = getattr(raw, "traffic_clock_kind", None)
        if clock is not None and kind == self._traffic_clock_kind:
            stages = self._traffic_deadline_stages(clock)
            if stages:
                self._traffic_now = max(clock, self._traffic_now if self._traffic_now is not None else clock)
                for deadline in stages:
                    self._advance_traffic_at(deadline)

    def _traffic_deadline_stages(self, now):
        return sorted({deadline for pending in self._pending_traffic.values()
                       if pending.clock_kind == self._traffic_clock_kind
                       for deadline in ((pending.normal_deadline, pending.hard_deadline)
                                        if not pending.normal_done else (pending.hard_deadline,))
                       if deadline < now})

    def advance_traffic_clock(self, now=None, *, force=False, reason="deadline", rebuild=True):
        """Resolve due stages in receipt-clock order before any hard expiry.

        Multiple deadlines can fall between callbacks. An earlier target must
        see in-window calibration admitted at another lap's normal deadline
        before it locks. Publication progress remains the actual first advance;
        only the evidence calculations use the intervening logical stages.
        """
        now = self._traffic_now if now is None else now
        if now is None:
            return False
        now = max(now, self._traffic_now if self._traffic_now is not None else now)
        self._traffic_now = now
        changed = False
        for deadline in self._traffic_deadline_stages(now):
            changed |= self._advance_traffic_at(deadline, reason=reason)
        changed |= self._advance_traffic_at(now, force=force, reason=reason)
        if changed and rebuild:
            self.snapshot = self._build_snapshot()
        return changed

    def _advance_traffic_at(self, now, *, force=False, reason="deadline"):
        self._protect_calibration_history()
        before_warning = self.telemetry_delays.warning
        if self._traffic_clock_kind in {"receive", "live_monotonic"}:
            self.telemetry_delays.advance(now)
        changed = False
        times = (*getattr(self._physical_traffic, "changed_sample_times", ()),
                 *getattr(self._physical_traffic, "changed_state_times", ()))
        for key, pending in sorted(tuple(self._pending_traffic.items()),
                                   key=lambda item: item[1].normal_deadline):
            if pending.clock_kind != self._traffic_clock_kind:
                continue
            if not pending.normal_done and (force or now >= pending.normal_deadline):
                cutoff = min(now, pending.normal_deadline)
                changed |= self._publish_traffic(pending, cutoff, reason)
                pending.normal_done = True
                receipt_clock, receipt_kind = getattr(self._physical_traffic, "_ingest_clock", (None, None))
                if (cutoff < self._traffic_now and (receipt_clock is None
                                      or receipt_kind == pending.clock_kind and receipt_clock > cutoff)):
                    # A sparse stream can deliver the first timer advance
                    # together with grace evidence excluded by the 2s cutoff.
                    pending.last_revision = -1
            bounds = self._physical_traffic.completion_bounds(key)
            if bounds is not None:
                pending.acquisition_bounds = bounds
                if hasattr(self._physical_traffic, "completion_coordinate"):
                    pending.acquisition_coordinate = self._physical_traffic.completion_coordinate(key)
            bounds = pending.acquisition_bounds
            coordinate = pending.acquisition_coordinate
            points = getattr(self._physical_traffic, "changed_acquisition_points", None)
            relevant = key in getattr(self._physical_traffic, "changed_revoked_keys", ())
            relevant |= any(domain == pending.clock_kind and received is not None
                            and pending.normal_deadline < received <= min(now, pending.hard_deadline)
                            for received, domain in getattr(self._physical_traffic, "changed_calibration_clocks", ()))
            if bounds is not None:
                if points is None:
                    relevant |= any(bounds[0] <= stamp <= bounds[1] for _, stamp in times)
                else:
                    relevant |= any((coordinate is None or domain in {None, coordinate})
                                    and bounds[0] <= stamp <= bounds[1]
                                    for _, domain, stamp in points)
                relevant |= any((coordinate is None or domain in {None, coordinate})
                                and start <= bounds[1] and end >= bounds[0]
                                for _, domain, start, end in getattr(self._physical_traffic, "changed_phase_intervals", ()))
            revision = getattr(self._physical_traffic, "revision", 0)
            if (pending.normal_done and pending.normal_deadline < now <= pending.hard_deadline
                    and relevant and pending.last_revision != revision):
                changed |= self._publish_traffic(pending, now, "exceptional grace")
        # All admissions at a cutoff precede every hard expiry at that
        # cutoff. A bounded sweep also refreshes earlier normal publications
        # when another same-cutoff contribution changes their calibration.
        for _ in range(len(self._pending_traffic) + 1):
            before_calibration = self._physical_traffic.revision
            calibration = getattr(self._physical_traffic, "changed_calibration_clocks", ())
            for pending in self._pending_traffic.values():
                if (pending.clock_kind == self._traffic_clock_kind and pending.normal_done
                        and pending.normal_deadline <= now <= pending.hard_deadline
                        and pending.last_revision != self._physical_traffic.revision
                        and any(domain == pending.clock_kind and received is not None
                                and pending.normal_deadline <= received <= now
                                for received, domain in calibration)):
                    changed |= self._publish_traffic(
                        pending, now, "deadline" if now == pending.normal_deadline else "exceptional grace")
            if self._physical_traffic.revision == before_calibration:
                break
        for key, pending in tuple(self._pending_traffic.items()):
            if pending.clock_kind == self._traffic_clock_kind and (force or now >= pending.hard_deadline):
                del self._pending_traffic[key]
        self._protect_calibration_history()
        changed |= before_warning != self.telemetry_delays.warning
        return changed

    def _publish_traffic(self, pending, cutoff, reason):
        traffic = self._physical_traffic.finalize(
            pending.driver, pending.lap_number,
            evidence_deadline_s=cutoff, clock_kind=pending.clock_kind,
        )
        sample = self._track_samples[pending.sample_index]
        updated = replace(
            sample, physical_mean_ahead_gap_s=traffic.mean_ahead_gap_s,
            ahead_coverage=traffic.ahead_coverage, traffic_status=traffic.status,
            traffic_method=traffic.method, traffic_target_samples=traffic.target_samples,
            traffic_ahead_samples=traffic.ahead_samples,
            traffic_evaluator_version=traffic.evaluator_version,
            traffic_circuit_length_m=traffic.circuit_length_m,
            clean_air_at_completion=(traffic.mean_ahead_gap_s > self.clean_air_gap_s)
            if traffic.available else None,
        )
        pending.last_revision = getattr(self._physical_traffic, "revision", 0)
        callback_clock = (monotonic() if pending.clock_kind == "live_monotonic"
                          and isinstance(getattr(self.controller, "_source", None), FastF1LiveTimingSource)
                          else self._traffic_now)
        self.traffic_finalisation_records.append({
            "key": pending.key, "clock_kind": pending.clock_kind,
            "delay_s": cutoff - pending.completed_at, "reason": reason,
            "evidence_deadline_s": cutoff, "publication_clock_s": callback_clock,
            "publication_evidence_progress_s": self._traffic_now,
            "publication_delay_s": (callback_clock - pending.completed_at) if callback_clock is not None else None,
            "status": traffic.status,
        })
        changed = updated != sample
        if changed:
            self._track_samples[pending.sample_index] = updated
            self._track_samples_revision += 1
        if self.track_evolution_policy is not None:
            self.track_evolution_policy.update_traffic(
                pending.key, self._track_samples[pending.sample_index],
                publication=self.traffic_finalisation_records[-1],
            )
        return changed

    def _build_snapshot(self) -> QualiSessionSnapshot:
        # Resolve from the very same immutable diagnostic published in this
        # snapshot, including immediately after transitions or policy edits.
        diagnostic = self.track_evolution_diagnostic()
        self._update_track_from_diagnostic(diagnostic)
        active = sorted(
            (state for state in self._states.values() if state.active),
            key=lambda state: state.rank or 999,
        )
        count = len(active)
        p1_target = self.session_part == "Q3"
        cutoff_rank = 1 if p1_target and count else max(
            count - self.eliminated_counts.get(self.session_part, 0), 0
        )
        cutoff_state = active[cutoff_rank - 1] if cutoff_rank else None
        unfinished = sum(not state.finished for state in active)
        # This slope is lap-time change per future field push.  A negative
        # rate means improving conditions and therefore a faster cutoff.
        correction = self.track_rate_s_per_field_push * unfinished
        current = cutoff_state.best_lap_s if cutoff_state else None
        projected = current + correction if current is not None else None
        # Future slower laps cannot erase an already observed valid cutoff.
        if current is not None and projected is not None:
            projected = min(projected, current)
        cutoff = CutoffProjection(
            "P1" if p1_target else "CUTOFF",
            cutoff_rank,
            cutoff_state.driver if cutoff_state else None,
            current,
            unfinished,
            self.track_rate_s_per_field_push,
            correction,
            projected,
        )
        rows = sorted(
            self._states.values(),
            key=lambda state: (
                state.active is False,
                state.official_position is None,
                (state.official_position if state.official_position is not None else state.rank or 999),
                state.driver,
            ),
        )
        return QualiSessionSnapshot(
            self.session_part,
            self.raw_snapshot.remaining_time_s,
            self.raw_snapshot.session_status,
            tuple(rows),
            cutoff,
            tuple(self._actual_cutoffs.items()),
            self.track_source,
            self.raw_snapshot.normal_finished,
            self.raw_snapshot.normal_finish_source,
            clock_running=self.raw_snapshot.clock_running,
            clock_anchor_session_time_s=self.raw_snapshot.clock_anchor_session_time_s,
            track_evolution_diagnostic=diagnostic,
            track_evolution_result=(self.track_evolution_payload(detailed=False)
                                    if not self._track_publication_in_progress else None),
            automatic_track_rate_s_per_field_push=self._automatic_track_rate_s_per_field_push,
            automatic_track_source=self._automatic_track_source,
            telemetry_delay_warning=self.telemetry_delays.warning,
            telemetry_delayed_lap_count=self.telemetry_delays.count,
            q_only_track_evolution_result=(self.q_only_track_evolution.payload()
                                          if not self._track_publication_in_progress else None),
            q_only_track_evolution_diagnostic=self.q_only_track_evolution.diagnostic,
            final_cutoff_forecast=(self._final_cutoff_payload()
                                   if not self._track_publication_in_progress else None),
        )

    @staticmethod
    def _previous_part(part: str) -> str:
        return {
            "Q2": "Q1",
            "Q3": "Q2",
            "SQ2": "SQ1",
            "SQ3": "SQ2",
        }.get(part, "FP")


def _checkpoint_elapsed(raw: QualiRawDriverState) -> dict[tuple[str, int], float]:
    checkpoints = {(item.kind, item.index): item.elapsed_s for item in raw.segments}
    elapsed = 0.0
    for index, sector in enumerate(raw.sectors_s, start=1):
        if sector is None:
            break
        elapsed += sector
        checkpoints[("sector", index)] = elapsed
    return checkpoints


def _associated_best_sector_checkpoints(
    raw: QualiRawDriverState,
    completed_laps,
    session_part: str,
) -> dict[tuple[str, int], float]:
    """Join the official best to durations owned by that accepted event."""
    best = _finite_float(raw.best_lap_s)
    event_key = getattr(raw, "best_lap_event_key", None)
    if best is None or event_key is None:
        return {}
    event = next((lap for lap in completed_laps
                  if getattr(lap, "event_key", None) == event_key
                  and lap.session_part == session_part
                  and lap.driver_number == raw.driver_number
                  and lap.classification == "normal_timed"), None)
    if event is None or not math.isclose(event.lap_time_s, best, abs_tol=.01, rel_tol=0):
        return {}
    sectors = tuple(_finite_float(value) for value in getattr(event, "sectors_s", ()))
    if (len(sectors) != 3
            or any(value is None or value <= 0 for value in sectors)):
        return {}
    if not math.isclose(best, sum(sectors), abs_tol=0.01, rel_tol=0.0):
        return {}
    return {
        ("sector", index): sum(sectors[:index])
        for index in (1, 2, 3)
    }
