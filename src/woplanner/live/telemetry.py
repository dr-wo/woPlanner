from __future__ import annotations

import logging
import queue
import statistics
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from wodata.live.models import LiveTimingMessage
from wodata.live.reducer import LiveSessionStore

from ..analysis.service import _live_traffic_gaps
from ..analysis.worker import LiveAnalysisInput


@dataclass(frozen=True)
class TrackMapDriver:
    driver_id: str
    x: float
    y: float
    z: float
    on_track: bool | None
    sample_time_seconds: float
    sample_timestamp: datetime | None
    valid: bool = True
    driver_code: str = ""
    team: str = ""
    in_pit: bool | None = None
    fresh_age_s: float | None = None


@dataclass(frozen=True)
class TrackMapReferencePoint:
    x: float
    y: float
    elapsed_s: float


@dataclass(frozen=True)
class TrackMapReference:
    driver_id: str
    driver_code: str
    lap_number: int
    lap_time_s: float
    points: tuple[TrackMapReferencePoint, ...]
    source: str = "actual clean-lap Position.z trajectory"


@dataclass(frozen=True)
class TrackMapSnapshot:
    generation: int
    source_timestamp: datetime | None
    position_visual_version: int
    drivers: tuple[TrackMapDriver, ...]
    reference: TrackMapReference | None = None


@dataclass(frozen=True)
class CausalAnalysisCut:
    leader_lap_number: int
    leader_lap_end_session_time: float
    ingress_sequence: int
    session_generation: int
    leader_lap_end_source_timestamp: datetime | None = None


@dataclass(frozen=True)
class PreparedAnalysisResult:
    generation: int
    request_id: int
    leader_lap: int
    analysis_input: LiveAnalysisInput | None
    elapsed_seconds: float
    error: str | None = None
    cut: CausalAnalysisCut | None = None


@dataclass(frozen=True)
class TelemetryWorkerDiagnostics:
    generation: int
    telemetry_messages_processed: int
    position_messages_processed: int
    queue_depth: int
    message_queue_depth: int
    control_queue_depth: int
    pending_build_count: int
    thread_alive: bool
    last_error: str | None
    latest_ingress_sequence: int
    processed_ingress_sequence: int


@dataclass(frozen=True)
class ReplayPipelineConfig:
    """Project-level bounds for the lossless replay analysis pipeline."""

    replay_telemetry_high_watermark: int = 256
    replay_telemetry_low_watermark: int = 128
    replay_mc_max_queued_requests: int = 1

    def __post_init__(self) -> None:
        high = int(self.replay_telemetry_high_watermark)
        low = int(self.replay_telemetry_low_watermark)
        max_queued = int(self.replay_mc_max_queued_requests)
        if low < 1:
            raise ValueError("replay_telemetry_low_watermark must be at least 1")
        if high <= low:
            raise ValueError(
                "replay_telemetry_high_watermark must exceed the low watermark"
            )
        if max_queued < 1:
            raise ValueError("replay_mc_max_queued_requests must be at least 1")
        object.__setattr__(self, "replay_telemetry_high_watermark", high)
        object.__setattr__(self, "replay_telemetry_low_watermark", low)
        object.__setattr__(self, "replay_mc_max_queued_requests", max_queued)


class _ReplayTelemetryWatermarks:
    """Hysteresis gate: pause at high, remain paused until strictly below low."""

    def __init__(self, config: ReplayPipelineConfig) -> None:
        self.config = config
        self.paused = False

    def reset(self) -> None:
        self.paused = False

    def has_capacity(self, queue_depth: int) -> bool:
        depth = int(queue_depth)
        if self.paused:
            if depth < self.config.replay_telemetry_low_watermark:
                self.paused = False
        elif depth >= self.config.replay_telemetry_high_watermark:
            self.paused = True
        return not self.paused


class TelemetryAnalysisWorker:
    """Own raw telemetry reduction and traffic preparation outside the Qt thread."""

    def __init__(
        self,
        *,
        logger=None,
        replay_pipeline_config: ReplayPipelineConfig | None = None,
    ) -> None:
        self.logger = logger or logging.getLogger(__name__)
        self.replay_pipeline_config = replay_pipeline_config or ReplayPipelineConfig()
        self._replay_telemetry_watermarks = _ReplayTelemetryWatermarks(
            self.replay_pipeline_config
        )
        # Raw packets are lossless, but control work must not wait behind an
        # accelerated replay's entire packet backlog.
        self._messages: queue.Queue[tuple[int, int, LiveTimingMessage]] = queue.Queue()
        self._controls: queue.Queue[tuple] = queue.Queue()
        self._results: queue.Queue[PreparedAnalysisResult] = queue.Queue()
        self._generation = 0
        self._request_id = 0
        self._ingress_sequence = 0
        self._processed_ingress_sequence = 0
        self._state_lock = threading.Lock()
        self._track_map = TrackMapSnapshot(0, None, 0, ())
        self._track_map_reference: TrackMapReference | None = None
        self._track_reference_lap_signature: tuple = ()
        self._track_clock_position_s: float | None = None
        self._track_clock_event_s: float | None = None
        self._telemetry_messages_processed = 0
        self._position_messages_processed = 0
        self._pending_build_count = 0
        self._last_error: str | None = None
        self._shutdown_requested = threading.Event()
        self._bounded_replay_mc_pipeline_capacity_probe = None
        self._thread = threading.Thread(
            target=self._run, name="woplanner-telemetry", daemon=True
        )
        self._thread.start()

    @property
    def generation(self) -> int:
        with self._state_lock:
            return self._generation

    def reset(self) -> int:
        with self._state_lock:
            self._generation += 1
            generation = self._generation
            self._track_map = TrackMapSnapshot(generation, None, 0, ())
            self._track_map_reference = None
            self._track_reference_lap_signature = ()
            self._track_clock_position_s = None
            self._track_clock_event_s = None
            self._telemetry_messages_processed = 0
            self._position_messages_processed = 0
            self._pending_build_count = 0
            self._last_error = None
            self._ingress_sequence = 0
            self._processed_ingress_sequence = 0
            self._replay_telemetry_watermarks.reset()
        self._controls.put(("reset", generation))
        return generation

    def enqueue_message(self, message: LiveTimingMessage) -> int:
        with self._state_lock:
            generation = self._generation
            self._ingress_sequence += 1
            ingress_sequence = self._ingress_sequence
        self._messages.put((generation, ingress_sequence, message))
        return ingress_sequence

    def request_analysis_input(
        self,
        *,
        leader_lap: int,
        use_sector_boundary_traffic: bool,
    ) -> int:
        with self._state_lock:
            self._request_id += 1
            request_id = self._request_id
            generation = self._generation
        self._controls.put(
            (
                "build",
                generation,
                request_id,
                int(leader_lap),
                bool(use_sector_boundary_traffic),
            )
        )
        return request_id

    def poll_analysis_inputs(
        self, *, max_results: int | None = None
    ) -> tuple[PreparedAnalysisResult, ...]:
        results = []
        generation = self.generation
        while True:
            if max_results is not None and len(results) >= int(max_results):
                break
            try:
                result = self._results.get_nowait()
            except queue.Empty:
                break
            if result.generation == generation:
                results.append(result)
        return tuple(results)

    def cancel_analysis_inputs(self) -> None:
        """Cancel pending preparation without disturbing replay state reduction."""
        self._controls.put(("cancel_builds", self.generation))

    def track_map_snapshot(self) -> TrackMapSnapshot:
        with self._state_lock:
            return self._track_map

    def diagnostics(self) -> TelemetryWorkerDiagnostics:
        with self._state_lock:
            message_depth = self._messages.qsize()
            control_depth = self._controls.qsize()
            return TelemetryWorkerDiagnostics(
                self._generation,
                self._telemetry_messages_processed,
                self._position_messages_processed,
                message_depth + control_depth + self._pending_build_count,
                message_depth,
                control_depth,
                self._pending_build_count,
                self._thread.is_alive(),
                self._last_error,
                self._ingress_sequence,
                self._processed_ingress_sequence,
            )

    def wait_for_replay_capacity(self, cancel_event=None) -> None:
        """Apply source-thread backpressure only when accelerated replay outruns us."""
        while (
            not self._shutdown_requested.is_set()
            and not (cancel_event is not None and cancel_event.is_set())
            and self._thread.is_alive()
        ):
            with self._state_lock:
                capacity_probe = self._bounded_replay_mc_pipeline_capacity_probe
            analysis_has_capacity = True
            if capacity_probe is not None:
                try:
                    analysis_has_capacity = bool(capacity_probe())
                except Exception:
                    analysis_has_capacity = True
            telemetry_has_capacity = self._replay_telemetry_watermarks.has_capacity(
                self._messages.qsize()
            )
            if telemetry_has_capacity and analysis_has_capacity:
                return
            self._shutdown_requested.wait(0.002)

    def set_bounded_replay_mc_pipeline_capacity_probe(self, probe) -> None:
        with self._state_lock:
            self._bounded_replay_mc_pipeline_capacity_probe = probe

    def shutdown(self) -> None:
        self._shutdown_requested.set()
        self._controls.put(("stop",))
        self._thread.join(timeout=5.0)

    def _run(self) -> None:
        # Retain raw messages so a request made after accelerated replay has moved
        # ahead can reconstruct the exact state at its immutable ingress cut.
        store = _analysis_store()
        active_generation = 0
        processed_ingress = 0
        message_history: list[tuple[int, LiveTimingMessage]] = []
        cuts: dict[int, CausalAnalysisCut] = {}
        snapshot_builder = _CausalSnapshotBuilder()
        processed = 0
        processing_seconds = 0.0
        position_snapshot_seconds = 0.0
        last_profile = time.monotonic()
        pending_builds: list[tuple] = []
        while True:
            stop = False
            while True:
                try:
                    command = self._controls.get_nowait()
                except queue.Empty:
                    break
                kind = command[0]
                if kind == "stop":
                    stop = True
                    break
                if kind == "reset":
                    active_generation = int(command[1])
                    processed_ingress = 0
                    store.reset()
                    message_history.clear()
                    cuts.clear()
                    snapshot_builder.reset()
                    pending_builds.clear()
                    self._set_pending_build_count(0)
                    continue
                if kind == "cancel_builds" and int(command[1]) == active_generation:
                    pending_builds.clear()
                    self._set_pending_build_count(0)
                    continue
                if int(command[1]) == active_generation and kind == "build":
                    pending_builds.append(command)
                    self._set_pending_build_count(len(pending_builds))
            if stop:
                return

            # A prepared input can be substantially larger than its lightweight
            # historical cut request.  Keep this hand-off bounded too: otherwise
            # a fast replay can accumulate an entire race of prepared snapshots
            # while the single MC process consumes them sequentially.
            ready = []
            if self._results.qsize() < int(
                self.replay_pipeline_config.replay_mc_max_queued_requests
            ):
                ready, pending_builds = _partition_ready_builds(
                    pending_builds,
                    cuts,
                    processed_ingress,
                    max_ready=1,
                )
            self._set_pending_build_count(len(pending_builds))
            for command, cut in ready:
                if cut is None:
                    self._results.put(PreparedAnalysisResult(
                        int(command[1]), int(command[2]), int(command[3]), None, 0.0,
                        "The requested leader-lap boundary was not observed in the recording.",
                        None,
                    ))
                    continue
                cut_snapshot = snapshot_builder.build(message_history, cut)
                self._prepare_analysis_input(cut_snapshot, command, cut)

            try:
                generation, ingress_sequence, message = self._messages.get(timeout=0.05)
            except queue.Empty:
                continue

            if generation > active_generation:
                # A packet can wake the worker just before its reset command is
                # observed. Apply the queued generation handoff before this packet
                # without moving the packet behind later telemetry.
                while generation > active_generation:
                    command = self._controls.get()
                    kind = command[0]
                    if kind == "stop":
                        return
                    if kind == "reset":
                        active_generation = int(command[1])
                        processed_ingress = 0
                        store.reset()
                        message_history.clear()
                        cuts.clear()
                        snapshot_builder.reset()
                        pending_builds.clear()
                        self._set_pending_build_count(0)
                    elif int(command[1]) == active_generation and kind == "build":
                        pending_builds.append(command)
                        self._set_pending_build_count(len(pending_builds))
            if generation == active_generation:
                started = time.perf_counter()
                message_history.append((ingress_sequence, message))
                try:
                    store._apply_message(message)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    with self._state_lock:
                        self._last_error = error
                    self.logger.warning(
                        "Telemetry worker skipped malformed %s packet: %s",
                        message.topic,
                        error,
                    )
                else:
                    processing_seconds += time.perf_counter() - started
                    processed += 1
                    if message.topic in {"CarData.z", "Position.z"}:
                        with self._state_lock:
                            self._telemetry_messages_processed += 1
                            if message.topic == "Position.z":
                                self._position_messages_processed += 1
                    if message.topic == "Position.z":
                        position_started = time.perf_counter()
                        try:
                            self._publish_track_map(store, generation, message)
                        except Exception as exc:
                            error = f"{type(exc).__name__}: {exc}"
                            with self._state_lock:
                                self._last_error = error
                            self.logger.warning(
                                "Telemetry worker could not publish track map: %s", error
                            )
                        position_snapshot_seconds += (
                            time.perf_counter() - position_started
                        )
                    if message.topic == "TimingData":
                        _capture_new_leader_cuts(
                            store,
                            generation=active_generation,
                            ingress_sequence=ingress_sequence,
                            message=message,
                            cuts=cuts,
                        )
                        self._publish_track_reference(store, generation, message)
                        # TimingData owns the pit flag and also advances the
                        # session clock when Position.z pauses.  Republish the
                        # same coordinates so pit/freshness state cannot lag
                        # until the next position packet.
                        self._publish_track_map(store, generation, message)
                processed_ingress = ingress_sequence
                with self._state_lock:
                    if generation == self._generation:
                        self._processed_ingress_sequence = processed_ingress
            now = time.monotonic()
            if now - last_profile >= 5.0:
                self.logger.debug(
                    "Telemetry worker profile: generation=%d messages=%d processing=%.1fms "
                    "position_snapshots=%.1fms queue=%d",
                    active_generation,
                    processed,
                    processing_seconds * 1000.0,
                    position_snapshot_seconds * 1000.0,
                    self._messages.qsize(),
                )
                processed = 0
                processing_seconds = 0.0
                position_snapshot_seconds = 0.0
                last_profile = now

    def _set_pending_build_count(self, value: int) -> None:
        with self._state_lock:
            self._pending_build_count = value

    def _prepare_analysis_input(
        self,
        snapshot,
        command: tuple,
        cut: CausalAnalysisCut,
    ) -> None:
        _, generation, request_id, leader_lap, force_sector = command
        started = time.perf_counter()
        try:
            snapshot = _snapshot_at_cut(snapshot, cut)
            traffic_started = time.perf_counter()
            traffic = _live_traffic_gaps(snapshot, force_sector=bool(force_sector))
            traffic_seconds = time.perf_counter() - traffic_started
            analysis_input = LiveAnalysisInput(
                tuple(snapshot.drivers),
                tuple(snapshot.completed_laps),
                tuple(snapshot.completed_sectors),
                (),
                tuple(traffic.to_dict("records")),
            )
            error = None
        except Exception as exc:
            analysis_input = None
            error = f"{type(exc).__name__}: {exc}"
            with self._state_lock:
                self._last_error = error
            traffic_seconds = 0.0
        elapsed = time.perf_counter() - started
        self.logger.info(
            "Prepared MC input: leader=%d cut=i%d total=%.3fs traffic=%.3fs",
            int(leader_lap), cut.ingress_sequence, elapsed, traffic_seconds,
        )
        self._results.put(
            PreparedAnalysisResult(
                int(generation),
                int(request_id),
                int(leader_lap),
                analysis_input,
                elapsed,
                error,
                cut,
            )
        )

    def _publish_track_map(
        self,
        store: LiveSessionStore,
        generation: int,
        message: LiveTimingMessage,
    ) -> None:
        official = {
            str(number): data
            for number, data in store._drivers.items()
            if isinstance(data, dict) and str(data.get("Tla") or "").strip()
        }
        official_sample_times = [
            samples[-1].session_time_seconds
            for number, samples in store._position_samples.items()
            if str(number) in official and samples
        ]
        event_clock_s = _message_clock_seconds(message)
        with self._state_lock:
            prior_position_clock_s = self._track_clock_position_s
            prior_event_clock_s = self._track_clock_event_s
        if (
            event_clock_s is not None
            and prior_event_clock_s is not None
            and event_clock_s < prior_event_clock_s
        ):
            event_clock_s = prior_event_clock_s
        position_clock_s = prior_position_clock_s
        if official_sample_times:
            if prior_position_clock_s is None or prior_event_clock_s is None:
                # A median gives a safe initial anchor if one driver's first
                # sample carries a malformed future timestamp.
                position_clock_s = float(statistics.median(official_sample_times))
            elif event_clock_s is not None:
                expected_clock_s = prior_position_clock_s + max(
                    0.0, float(event_clock_s - prior_event_clock_s)
                )
                if message.topic == "Position.z":
                    # Once anchored, packet/session chronology is authoritative.
                    # A partial packet advances the clock for omitted cars; an
                    # arbitrary future sample can invalidate only itself.
                    position_clock_s = expected_clock_s
                else:
                    position_clock_s = expected_clock_s
        latest = []
        for driver_number, samples in store._position_samples.items():
            identity = official.get(str(driver_number))
            if not samples or identity is None:
                continue
            sample = samples[-1]
            age_s = (
                None
                if position_clock_s is None
                else float(position_clock_s - sample.session_time_seconds)
            )
            timing = store._timing.get(str(driver_number), {})
            in_pit = _optional_bool(timing.get("InPit"))
            latest.append(
                TrackMapDriver(
                    driver_number,
                    sample.x,
                    sample.y,
                    sample.z,
                    sample.on_track,
                    sample.session_time_seconds,
                    _timestamp_from_sample_time(sample.session_time_seconds),
                    valid=bool(
                        age_s is not None
                        and -1.0 <= age_s <= 5.0
                        and (sample.x != 0.0 or sample.y != 0.0)
                    ),
                    driver_code=str(identity.get("Tla") or "").strip(),
                    team=str(identity.get("TeamName") or "").strip(),
                    in_pit=in_pit,
                    fresh_age_s=age_s,
                )
            )
        with self._state_lock:
            if generation != self._generation:
                return
            if position_clock_s is not None and event_clock_s is not None:
                self._track_clock_position_s = float(position_clock_s)
                self._track_clock_event_s = float(event_clock_s)
            version = self._track_map.position_visual_version + 1
            self._track_map = TrackMapSnapshot(
                generation,
                message.source_timestamp_utc,
                version,
                tuple(sorted(latest, key=lambda item: item.driver_id)),
                self._track_map_reference,
            )

    def _publish_track_reference(
        self,
        store: LiveSessionStore,
        generation: int,
        message: LiveTimingMessage,
    ) -> None:
        if self._track_map_reference is not None:
            return
        lap_signature = tuple(
            (str(number), max(laps, default=0), len(laps))
            for number, laps in sorted(store._laps.items())
        )
        if lap_signature == self._track_reference_lap_signature:
            return
        self._track_reference_lap_signature = lap_signature
        official = {
            str(number): data
            for number, data in store._drivers.items()
            if isinstance(data, dict) and str(data.get("Tla") or "").strip()
        }
        reference = _reference_from_store(store, official)
        if reference is None:
            return
        with self._state_lock:
            if generation != self._generation or self._track_map_reference is not None:
                return
            self._track_map_reference = reference
            self._track_map = replace(
                self._track_map,
                source_timestamp=message.source_timestamp_utc,
                position_visual_version=self._track_map.position_visual_version + 1,
                reference=reference,
            )


def _reference_from_store(
    store: LiveSessionStore,
    official: dict[str, dict],
) -> TrackMapReference | None:
    """Extract one completed non-pit lap in the Position.z time domain."""
    for driver_number in sorted(official):
        laps = store._laps.get(driver_number, {})
        samples = store._position_samples.get(driver_number, ())
        if not laps or len(samples) < 30:
            continue
        lap_numbers = sorted(laps)
        for lap_number in reversed(lap_numbers):
            previous = laps.get(lap_number - 1)
            current = laps.get(lap_number)
            if not isinstance(current, dict):
                continue
            if any(
                bool(row.get(key))
                for row in (previous, current)
                if isinstance(row, dict)
                for key in ("pit_in", "pit_out")
            ):
                continue
            end_s = current.get("lap_end_time_s")
            start_s = (
                previous.get("lap_end_time_s")
                if isinstance(previous, dict)
                else None
            )
            if start_s is None and end_s is not None:
                observed_lap_time_s = current.get("source_lap_time_s")
                if observed_lap_time_s is not None:
                    start_s = float(end_s) - float(observed_lap_time_s)
            if start_s is None or end_s is None or float(end_s) <= float(start_s):
                continue
            lap_samples = [
                sample for sample in samples
                if float(start_s) <= sample.session_time_seconds <= float(end_s)
            ]
            if len(lap_samples) < 30:
                continue
            raw_points = tuple(
                TrackMapReferencePoint(
                    float(sample.x), float(sample.y),
                    float(sample.session_time_seconds - float(start_s)),
                )
                for sample in lap_samples
            )
            lap_duration_s = float(end_s) - float(start_s)
            points = raw_points
            if raw_points[0].elapsed_s > 0.0:
                points = (
                    TrackMapReferencePoint(raw_points[0].x, raw_points[0].y, 0.0),
                    *points,
                )
            if points[-1].elapsed_s < lap_duration_s:
                points = (*points, TrackMapReferencePoint(
                    points[0].x, points[0].y, lap_duration_s,
                ))
            extent = max(
                max(point.x for point in raw_points) - min(point.x for point in raw_points),
                max(point.y for point in raw_points) - min(point.y for point in raw_points),
            )
            closure = (
                (raw_points[0].x - raw_points[-1].x) ** 2
                + (raw_points[0].y - raw_points[-1].y) ** 2
            ) ** 0.5
            if extent <= 0.0 or closure > extent * 0.2:
                continue
            identity = official[driver_number]
            return TrackMapReference(
                driver_id=driver_number,
                driver_code=str(identity.get("Tla") or "").strip(),
                lap_number=int(lap_number),
                lap_time_s=lap_duration_s,
                points=points,
            )
    return None


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().casefold()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def _timestamp_from_sample_time(value: float) -> datetime | None:
    # Archive Position.Timestamp values are UTC epoch seconds.  Synthetic/live
    # sources may use a relative session clock, in which case no wall timestamp
    # is claimed.
    if not 946684800.0 <= float(value) <= 4102444800.0:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def _message_clock_seconds(message: LiveTimingMessage) -> float | None:
    """Return a monotonic event clock independent of Position sample units."""
    if message.session_time_seconds is not None:
        try:
            return float(message.session_time_seconds)
        except (TypeError, ValueError):
            pass
    # Raw live recordings often have no topic source timestamp.  ReplayPlayer
    # preserves their original recorded receive time, so Max playback speed and
    # pause duration cannot distort this monotonic ingestion clock.  Source
    # timestamps are intentionally not mixed in: late topic corrections can
    # move that clock backwards.
    if message.received_at_utc is not None:
        return float(message.received_at_utc.timestamp())
    return None


def _partition_ready_builds(
    pending_builds: list[tuple],
    cuts: dict[int, CausalAnalysisCut],
    processed_ingress: int,
    *,
    max_ready: int | None = None,
) -> tuple[list[tuple[tuple, CausalAnalysisCut | None]], list[tuple]]:
    ready = []
    waiting = []
    for command in pending_builds:
        if max_ready is not None and len(ready) >= int(max_ready):
            waiting.append(command)
            continue
        leader_lap = int(command[3])
        cut = cuts.get(leader_lap)
        if cut is not None and processed_ingress >= cut.ingress_sequence:
            ready.append((command, cut))
        elif cuts and leader_lap < max(cuts):
            # Never substitute a later observation for an unobserved boundary.
            ready.append((command, None))
        else:
            waiting.append(command)
    return ready, waiting


def _capture_new_leader_cuts(
    store: LiveSessionStore,
    *,
    generation: int,
    ingress_sequence: int,
    message: LiveTimingMessage,
    cuts: dict[int, CausalAnalysisCut],
) -> None:
    leader_laps = [
        lap
        for number, timing in store._timing.items()
        if store._positions.get(number) == 1
        for lap in (_positive_lap_number(timing.get("NumberOfLaps")),)
        if lap is not None
    ]
    if not leader_laps:
        leader_laps = [
            lap
            for timing in store._timing.values()
            for lap in (_positive_lap_number(timing.get("NumberOfLaps")),)
            if lap is not None
        ]
    if not leader_laps:
        return
    latest_leader_lap = max(leader_laps)
    if latest_leader_lap <= max(cuts, default=0):
        # Late corrections must not manufacture a historical cut after a later
        # leader boundary has already been observed.
        return
    session_time = message.session_time_seconds
    source_timestamp = message.source_timestamp_utc
    if session_time is None and source_timestamp is None:
        # Legacy replay is still causally ordered by ingress.
        end_time = float(ingress_sequence)
    else:
        end_time = (
            float(session_time)
            if session_time is not None
            else float(source_timestamp.timestamp())
        )
    cuts[latest_leader_lap] = CausalAnalysisCut(
        latest_leader_lap, end_time, int(ingress_sequence), int(generation), source_timestamp,
    )


def _positive_lap_number(value: object) -> int | None:
    try:
        lap = int(value)
    except (TypeError, ValueError):
        return None
    return lap if lap > 0 else None


class _CausalSnapshotBuilder:
    """Incrementally reconstruct increasing cuts; replay FIFO is linear, not quadratic."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._store = _analysis_store()
        self._history_index = 0
        self._ingress_sequence = 0

    def build(
        self,
        message_history: list[tuple[int, LiveTimingMessage]],
        cut: CausalAnalysisCut,
    ):
        if cut.ingress_sequence < self._ingress_sequence:
            return _rebuild_snapshot_at_cut(message_history, cut)
        while self._history_index < len(message_history):
            ingress_sequence, message = message_history[self._history_index]
            if ingress_sequence > cut.ingress_sequence:
                break
            self._history_index += 1
            _apply_replay_message(self._store, message)
        self._ingress_sequence = cut.ingress_sequence
        return self._store.snapshot()


def _rebuild_snapshot_at_cut(
    message_history: list[tuple[int, LiveTimingMessage]],
    cut: CausalAnalysisCut,
):
    store = _analysis_store()
    for ingress_sequence, message in message_history:
        if ingress_sequence > cut.ingress_sequence:
            break
        _apply_replay_message(store, message)
    return store.snapshot()


def _apply_replay_message(store: LiveSessionStore, message: LiveTimingMessage) -> None:
    try:
        store._apply_message(message)
    except Exception:
        pass


def _analysis_store() -> LiveSessionStore:
    return LiveSessionStore(
        telemetry_lap_window=None,
        # One retention policy serves the track map and causal replay builder:
        # latest 1,200 sample-time seconds, capped at 6,000 samples per driver.
        # Cuts retain diagnostic positions; raw positions never enter MC inputs.
        position_sample_limit=6000,
    )


def _snapshot_at_cut(snapshot, cut: CausalAnalysisCut):
    """Build an analysis cut from committed history, excluding live display state."""
    return replace(
        snapshot,
        drivers=tuple(
            driver
            for driver in snapshot.drivers
            if driver.source_timestamp_utc is None
            or _value_is_within_cut(driver, cut)
        ),
        completed_laps=tuple(
            lap for lap in snapshot.completed_laps
            if _value_is_within_cut(lap, cut)
        ),
        completed_sectors=tuple(
            sector for sector in snapshot.completed_sectors
            if _value_is_within_cut(sector, cut)
        ),
        telemetry_samples=tuple(
            sample for sample in snapshot.telemetry_samples
            if _value_is_within_cut(sample, cut)
        ),
        position_samples=tuple(
            sample for sample in snapshot.position_samples
            if _value_is_within_cut(sample, cut)
        ),
        provisional_sectors=(),
        live_timing=(),
    )


def _value_is_within_cut(value: Any, cut: CausalAnalysisCut) -> bool:
    session_time = getattr(value, "session_time_seconds", None)
    if session_time is not None:
        return float(session_time) <= cut.leader_lap_end_session_time
    source_timestamp = getattr(value, "source_timestamp_utc", None)
    if (
        source_timestamp is not None
        and cut.leader_lap_end_source_timestamp is not None
    ):
        return source_timestamp <= cut.leader_lap_end_source_timestamp
    # The legacy live recorder did not carry timestamps. Its snapshot builder
    # has already stopped at the cut's immutable ingress sequence.
    return session_time is None and source_timestamp is None
