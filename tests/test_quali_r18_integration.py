"""Recorded R18 packets: prerequisite failures, exact laps and cutoff consumption.

The packet subset is genuine; synthetic FP pace references and COOL120 controls
are explicitly installed by this test, never represented as historical inputs.
Reduced-stream PUSH indices are not the original full-session coordinate.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
import importlib.util
import json
from pathlib import Path

import pytest

from wodata.live import LiveTimingController, LiveTimingMessage, QualiSessionStore
from wodata.live.replay import ReplayPlayer
from woplanner.quali.experimental_cutoff import calculate_half_live
from woplanner.quali.lap_state import LapPaceReference
from woplanner.quali.models import FinishedReason
from woplanner.quali.runtime_paths import RuntimePaths
from woplanner.quali.service import QualiLiveService
from woplanner.quali.track_evolution import build_track_evolution_diagnostic


FIXTURE = Path(__file__).parent / "fixtures" / "quali_r18"
PROVENANCE = json.loads((FIXTURE / "provenance.json").read_text())
CHECKPOINTS = PROVENANCE["original_checkpoints"]
LAPS = (4568, 4644, 6555, 6623)


def _utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


@dataclass
class RecordedRun:
    service: QualiLiveService
    checkpoints: dict
    message_sequences: list


def _run(resources, *, reference=False, cool=None, native_replay=True):
    """Use the native controller callback for each preserved original envelope."""
    controller = LiveTimingController(store=QualiSessionStore())
    service = QualiLiveService(controller, cool_lap_s=cool, runtime_paths=resources)
    if reference:
        # Deliberately permissive synthetic control, NOT the historical R18 FP
        # reference. It isolates pace-reference availability from data ingestion.
        service._common_lap_state_reference = LapPaceReference(
            start_to_s1_s=40.0, s1_to_s2_s=40.0, source="synthetic R18 regression control",
        )
    checkpoints, seen, delivery_sequences = {}, [], []
    current_sequence = None
    original_callback = controller.on_message_snapshot

    def observe(raw):
        original_callback(raw)
        seen.append(current_sequence)
        if str(current_sequence) in CHECKPOINTS:
            checkpoints[current_sequence] = {
                "raw": raw,
                "snapshot": service.snapshot,
                "result": service.track_evolution_payload(),
            }

    controller.on_message_snapshot = observe

    def deliver(message):
        nonlocal current_sequence
        # apply_faults in native ReplayPlayer numbers delivery envelopes1..N
        # even with no faults. Original sequence numbers remain byte-exact in
        # the fixture; map by delivery ordinal solely for checkpoint labels.
        current_sequence = (PROVENANCE["selected_sequences"][message.sequence - 1]
                            if native_replay else message.sequence)
        delivery_sequences.append(message.sequence)
        controller._enqueue_message(message)
        assert controller.drain(max_messages=1).messages_processed == 1

    if native_replay:
        # Production replay preserves source/receipt timestamps but deliberately
        # identifies its envelopes as f1_archive_replay, just like the original
        # investigation's full-capture run. It does not impersonate a live source.
        result = ReplayPlayer(FIXTURE / "packets.jsonl", speed=0).play(deliver)
        assert result.status == "completed"
        assert result.messages_emitted == PROVENANCE["fixture_packets"]
        assert delivery_sequences == list(range(1, PROVENANCE["fixture_packets"] + 1))
    else:
        # Separate direct-ingress control retains the original signalr_live tag.
        for line in (FIXTURE / "packets.jsonl").read_text().splitlines():
            row = json.loads(line)
            deliver(LiveTimingMessage(
                topic=row["topic"], payload=row["payload"],
                source_timestamp_utc=_utc(row["source_timestamp_utc"]),
                received_at_utc=_utc(row["received_at_utc"]), source=row["source"],
                sequence=row["sequence"], schema_version=row["message_schema_version"],
                session_time_seconds=row["session_time_seconds"],
                playback_time_seconds=row["playback_time_seconds"],
                received_at_is_trusted=row["received_at_is_trusted"],
            ))
    assert seen == PROVENANCE["selected_sequences"]
    if not native_replay:
        assert delivery_sequences == PROVENANCE["selected_sequences"]
    return RecordedRun(service, checkpoints, seen)


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    root = tmp_path_factory.mktemp("r18-resources")
    resource, output = root / "resource", root / "output"
    resource.mkdir()
    output.mkdir()
    paths = RuntimePaths(resource, output, isolated=True)
    return {
        (reference, cool): _run(paths, reference=reference, cool=cool)
        for reference in (False, True) for cool in (None, 120.0)
    }


def _driver(snapshot, code):
    return next(driver for driver in snapshot.drivers if driver.driver == code)


def _event(checkpoint, sequence):
    expected = CHECKPOINTS[str(sequence)]
    return next(event for event in checkpoint["raw"].completed_lap_sequence
                if event.event_key == tuple(expected["key"]))


def test_fixture_is_an_exact_portable_recording_subset():
    content = (FIXTURE / "packets.jsonl").read_bytes()
    assert sha256(content).hexdigest() == PROVENANCE["fixture_sha256"]
    assert len(content) == PROVENANCE["fixture_bytes"] < 400_000
    lines = content.splitlines(keepends=True)
    assert len(lines) == PROVENANCE["fixture_packets"] < 600
    assert PROVENANCE["source_packets"] == 12026
    assert PROVENANCE["source_sha256"] == "533e7dc2b054568ede8142d2b32f3ce0e88778165b8e586939881102bb03de17"
    spec = importlib.util.spec_from_file_location("r18_extract", FIXTURE / "extract.py")
    extraction = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(extraction)
    for line in lines:
        row = json.loads(line)
        assert extraction.selected(row)
        assert sha256(line).hexdigest() == PROVENANCE["line_sha256"][str(row["sequence"])]
        assert row["source"] == "signalr_live"
        assert row["received_at_utc"]
    assert [json.loads(line)["sequence"] for line in lines] == PROVENANCE["selected_sequences"]


@pytest.mark.parametrize("reference", [False, True])
@pytest.mark.parametrize("cool", [None, 120.0])
def test_alb_recorded_push_requires_both_independent_prerequisites(runs, reference, cool):
    checkpoint = runs[reference, cool].checkpoints[520]
    event = _event(checkpoint, 520)
    raw = next(driver for driver in checkpoint["raw"].drivers if driver.driver_code == "ALB")
    alb = _driver(checkpoint["snapshot"], "ALB")
    assert event.lap_time_s == pytest.approx(96.967)
    assert raw.timed_lap_eligible and raw.in_pit is False
    assert checkpoint["snapshot"].remaining_time_s == 58.0
    assert alb.lap_state == ("FAST" if reference else "UNKNOWN")
    assert alb.effective_finished is (reference and cool is not None)
    assert alb.finished is alb.effective_finished
    if alb.effective_finished:
        assert alb.finished_reason == FinishedReason.NO_TIME_FOR_NEXT_PUSH
        assert alb.effective_finish_remaining_s == 58.0
        assert alb.effective_finish_recovery_s == 120.0
        assert alb.effective_finish_timing_source == "manual override"
    else:
        assert alb.finished_reason is None


def test_alb_pit_entry_independently_resolves_the_missing_reference_case(runs):
    run = runs[False, None]
    assert not _driver(run.checkpoints[520]["snapshot"], "ALB").effective_finished
    checkpoint = run.checkpoints[1446]
    alb = _driver(checkpoint["snapshot"], "ALB")
    assert checkpoint["raw"].remaining_time_s == 0.0
    assert checkpoint["raw"].normal_finished
    assert alb.effective_finished and alb.finished
    assert alb.finished_reason == FinishedReason.IN_PIT_NO_OUTLAP_TIME
    assert alb.effective_finish_recovery_s == 100.0
    assert alb.effective_finish_remaining_s == 0.0
    assert alb.effective_finish_timing_source == "built-in fallback"


def test_original_live_envelopes_and_native_replay_keep_their_distinct_clock_semantics(runs):
    native = runs[False, None]
    original = _run(native.service.runtime_paths, native_replay=False)
    live_checkpoint = original.checkpoints[520]
    replay_checkpoint = native.checkpoints[520]
    assert live_checkpoint["raw"].message_source == "signalr_live"
    assert replay_checkpoint["raw"].message_source == "f1_archive_replay"
    assert _event(live_checkpoint, 520).event_key == _event(replay_checkpoint, 520).event_key
    # The initial undated live clock packet receives an existing source-specific
    # latency correction:135s minus2.063477s, then77 whole source-clock seconds.
    # Native archive replay retains135s minus77. This is an intentional exercised
    # adapter difference, not a changed production clock or a fabricated fixture.
    assert live_checkpoint["raw"].remaining_time_s == pytest.approx(55.936523)
    assert replay_checkpoint["raw"].remaining_time_s == 58.0
    assert not _driver(live_checkpoint["snapshot"], "ALB").effective_finished
    assert not _driver(replay_checkpoint["snapshot"], "ALB").effective_finished
    for sequence in LAPS:
        assert _event(original.checkpoints[sequence], sequence).event_key == _event(
            native.checkpoints[sequence], sequence).event_key
    assert _driver(original.checkpoints[1446]["snapshot"], "ALB").effective_finished


@pytest.mark.parametrize("sequence", LAPS)
def test_four_q2_laps_have_exact_ingestion_identity_and_preexisting_new_soft_tyres(runs, sequence):
    checkpoint = runs[False, None].checkpoints[sequence]
    expected = CHECKPOINTS[str(sequence)]
    event = _event(checkpoint, sequence)
    raw = next(driver for driver in checkpoint["raw"].drivers
               if driver.driver_code == expected["driver"])
    assert event.classification == "normal_timed"
    assert event.lap_time_s == pytest.approx(expected["lap_time_s"])
    assert event.official_lap_number == event.lap_number - 1
    assert raw.timed_lap_eligible and raw.in_pit is False
    tyre = raw.tyre_history[-1]
    assert (tyre.compound, tyre.fresh, tyre.start_laps, tyre.stint_index) == (
        "SOFT", True, 0, expected["installation"],
    )
    decision = next(row for row in checkpoint["result"]["admission_decisions"]
                    if row["completion_key"] == event.event_key)
    assert decision["reason"] == "NOT_OPERATIONAL_FAST"
    assert not decision["pace_pass"]


def test_fresh_laps_become_observations_before_two_installations_support_a_driver_fit(runs):
    run = runs[True, None]
    for sequence in LAPS:
        checkpoint = run.checkpoints[sequence]
        event = _event(checkpoint, sequence)
        decision = next(row for row in checkpoint["result"]["admission_decisions"]
                        if row["completion_key"] == event.event_key)
        assert decision["operational_fast"] and decision["pace_pass"]
        assert decision["reason"] is None
    first = run.checkpoints[4644]["snapshot"].track_evolution_diagnostic
    assert {sample.driver for sample in first.observations} >= {"RUS", "ANT"}
    assert not {series.driver for series in first.series}.intersection({"RUS", "ANT"})
    first_pair = tuple(sample for sample in first.observations if sample.driver in {"RUS", "ANT"})
    pair_diagnostic = build_track_evolution_diagnostic(
        first_pair, clean_air_gap_s=3.0, require_traffic=False,
    )
    assert len(first_pair) == 2
    assert pair_diagnostic.aggregate_slope_s_per_push is None
    assert not pair_diagnostic.series
    final = run.checkpoints[6623]["snapshot"].track_evolution_diagnostic
    series = {series.driver: series for series in final.series}
    for code in ("RUS", "ANT"):
        values = series[code].samples
        assert len(values) == 2
        assert [sample.tyre_install_index for sample in values] == [1, 2]
        assert series[code].slope_s_per_push < 0
        # Signed lap-time seconds per REDUCED-stream field PUSH, not s/minute,
        # per-driver lap index, or the omitted full-session PUSH coordinate.
        expected_slope = (values[1].lap_time_s - values[0].lap_time_s) / (
            values[1].global_push_index - values[0].global_push_index)
        assert series[code].slope_s_per_push == pytest.approx(expected_slope)
    assert run.checkpoints[6623]["result"]["live_estimate_s_per_global_push"] < 0
    assert run.checkpoints[6623]["result"]["status"] in {"LIVE_LIMITED", "LIVE_SUPPORTED"}
    no_reference = runs[False, None].checkpoints[6623]
    assert no_reference["result"]["live_estimate_s_per_global_push"] is None
    assert no_reference["result"]["status"] == "FALLBACK"
    assert "NO_SUPPORTED_LIVE_FIT" in no_reference["result"]["reasons"]


def test_q2_cutoff_is_unavailable_until_the_recorded_field_has_a_target(runs):
    run = runs[False, None]
    assert run.service._event_geometry_identity == (2026, 18)
    assert run.service.cool_lap_s is None
    assert run.service._common_lap_state_reference is None
    assert not run.service.lap_state_references
    before = run.checkpoints[2285]["snapshot"]
    after = run.checkpoints[4350]["snapshot"]
    assert before.session_part == after.session_part == "Q2"
    assert before.cutoff.current_cutoff_s is None
    unavailable = calculate_half_live(before)
    assert unavailable.status == "UNAVAILABLE"
    assert unavailable.reason == "No observed cutoff target"
    assert after.cutoff.cutoff_rank == 10
    assert sum(driver.active for driver in after.drivers) == 16
    assert after.cutoff.current_cutoff_s == pytest.approx(96.031)
    for checkpoint in run.checkpoints.values():
        snapshot = checkpoint["snapshot"]
        if snapshot.cutoff.current_cutoff_s is None:
            continue
        prediction = calculate_half_live(snapshot)
        assert prediction.status == "FALLBACK"
        assert prediction.rate_s_per_field_push == 0.0
        assert prediction.projected_cutoff_s == snapshot.cutoff.projected_cutoff_s
        assert prediction.projected_cutoff_s == snapshot.cutoff.current_cutoff_s


def test_recorded_supported_fit_drives_half_live_without_manual_or_traffic_inputs(runs):
    # The module fixture has finished processing. Changing publication settings
    # below is reversible and cannot alter the checkpoint snapshots above.
    service = runs[True, 120.0].service
    snapshot = service.snapshot
    live = snapshot.track_evolution_result["live_estimate_s_per_global_push"]
    assert live < 0
    prediction = calculate_half_live(snapshot)
    assert prediction.rate_s_per_field_push == pytest.approx(live / 2)
    assert prediction.projected_cutoff_s == pytest.approx(min(
        snapshot.cutoff.current_cutoff_s + live / 2 * snapshot.cutoff.unfinished_driver_count,
        snapshot.cutoff.current_cutoff_s,
    ))
    try:
        service.set_track_override(0.25)
        assert service.snapshot.cutoff.track_rate_s_per_field_push == 0.25
        assert service.snapshot.cutoff.projected_cutoff_s == snapshot.cutoff.current_cutoff_s
        assert calculate_half_live(service.snapshot) == prediction
    finally:
        service.set_track_override(None)
    diagnostic = snapshot.track_evolution_diagnostic
    # Canonical fitting ignores auxiliary physical traffic, including absent
    # telemetry in this fixture and even deliberately adverse synthetic traffic.
    samples = tuple(service.track_evolution_policy.samples.values())
    baseline = build_track_evolution_diagnostic(samples, clean_air_gap_s=3.0, require_traffic=False)
    adverse = build_track_evolution_diagnostic([
        replace(sample, traffic_status="ok", physical_mean_ahead_gap_s=0.1, ahead_coverage=1.0)
        for sample in samples
    ], clean_air_gap_s=3.0, require_traffic=False)
    assert baseline.aggregate_slope_s_per_push == adverse.aggregate_slope_s_per_push
    assert baseline.aggregate_slope_s_per_push == diagnostic.aggregate_slope_s_per_push
    assert [(series.driver, len(series.samples)) for series in baseline.series] == [
        (series.driver, len(series.samples)) for series in adverse.series
    ]
