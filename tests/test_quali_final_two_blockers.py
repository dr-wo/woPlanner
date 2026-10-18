"""Public adapter and record/recover/player regressions for the final review."""
from dataclasses import replace
from datetime import timedelta
import shutil
import types

import pytest

from wodata.live import LiveTimingController, LiveTimingMessage, QualiSessionStore
from wodata.live.recorder import LiveTimingRecorder
from wodata.live.recovery import recover_replay_directory
from wodata.live.replay import ReplayPlayer
from woplanner.quali.service import QualiLiveService
from test_quali_projection_lifecycle_alias import NOW, A, B, PENDING, EMPTY, sectors
from test_quali_publication_contract import Delivery, adapter_messages


def message(patch, publication, arrival=None, *, number="31", topic="TimingData", sequence=0):
    return LiveTimingMessage(topic, {"Lines": {number: patch}} if topic == "TimingData" else patch,
        None if publication is None else NOW + timedelta(seconds=publication),
        NOW + timedelta(seconds=arrival if arrival is not None else publication), sequence=sequence)


def completion(boundary, alias, values=None):
    patch = {"InPit": False, "NumberOfLaps": boundary, "LastLapTime": {"Value": "1:30"},
             "BestLapTime": {"Value": "1:30", "Lap": alias}}
    if values is not None:
        patch["Sectors"] = sectors(values)
    return patch


@pytest.mark.parametrize("events", [1, 2])
@pytest.mark.parametrize("path", ["apply", "apply_many", "queue", "adapter"])
def test_unknown_best_cannot_recover_on_unrelated_update(events, path, monkeypatch):
    messages = [message({"NumberOfLaps": 1, "InPit": False}, 1, number="1"),
                message(completion(3, 2, A), 100)]
    if events == 2:
        messages.append(message(completion(4, 3, B), 200))
    messages += [message({"BestLapTime": {"Value": ""}}, None, 210),
                 message({"BestLapTime": {"Value": "1:30", "Lap": 2}}, None, 211),
                 message({"Position": 1}, 212),
                 message({"Sectors": {"0": {"Value": "24"}}}, 400, number="1")]
    if path == "adapter":
        records = [([m.topic, m.payload, m.source_timestamp_utc.isoformat()]
                    if m.source_timestamp_utc else types.SimpleNamespace(result={m.topic: m.payload}))
                   for m in messages]
        messages = adapter_messages(monkeypatch, records)
    store = QualiSessionStore()
    service = QualiLiveService(eliminated_counts={"Q1": 0, "Q2": 0})
    if path in {"queue", "adapter"}:
        controller = LiveTimingController(store=store)
        controller.on_message_snapshot = service.consume
        for m in messages:
            controller._enqueue_message(m)
        raw = controller.drain().snapshot
    elif path == "apply_many":
        raw = store.apply_many(messages)
        service.consume(raw)
    else:
        for m in messages:
            raw = store.apply(m)
            service.consume(raw)
    assert next(d for d in raw.drivers if d.driver_number == "31").best_lap_s == 90
    assert next(d for d in raw.drivers if d.driver_number == "31").best_lap_event_key is None
    assert [(e.official_lap_number, e.sectors_s) for e in raw.completed_lap_sequence] == [(2, A)] + ([(3, B)] if events == 2 else [])
    assert next(d for d in service.snapshot.drivers if d.driver_number == "1").projected_lap_s is None


@pytest.mark.parametrize("original", [100, 200])
@pytest.mark.parametrize("clear_first", [False, True])
def test_positive_publication_recovers_after_unknown_best(original, clear_first):
    h = Delivery(utc_source=True)
    h.complete(alias=2)
    h.complete(4, 200, B, alias=3)
    if clear_first:
        h.send({"BestLapTime": {"Value": ""}}, None, delivery=210)
    h.send({"BestLapTime": {"Value": "1:30", "Lap": 2}}, None, delivery=211)
    h.send({"Position": 1}, 212)
    assert h.owner() is None
    alias = 2 if original == 100 else 3
    h.send({"BestLapTime": {"Value": "1:30", "Lap": alias}}, original, delivery=213)
    assert h.owner() == ("Q1", "31", alias + 1, (NOW + timedelta(seconds=original)).timestamp())
    assert h.projection(24) == pytest.approx(108 if alias == 2 else 86.4)


def test_unknown_checkpoint_keeps_certified_reference_available():
    h = Delivery(utc_source=True)
    h.complete(alias=2)
    key = h.owner()
    h.send({"Sectors": {"0": {"Value": "24"}}}, None, delivery=200, number="1")
    assert h.owner() == key
    assert next(d for d in h.result.drivers if d.driver_number == "1").projected_lap_s == pytest.approx(108)


def recovery_sequence(case):
    messages = [message({"Name": "Qualifying", "StartDate": NOW.isoformat(),
                         "Meeting": {"RoundNumber": 16}}, 0, topic="SessionInfo"),
                message({"NumberOfLaps": 1, "InPit": False}, 1, number="1"),
                message(completion(3, 2, A), 100)]
    clear = {"Sectors": sectors(EMPTY)}
    if case == "old_sectors":
        messages += [message(clear, 110), message(clear, 190),
                     message({"Sectors": sectors(PENDING)}, 150, 200),
                     message(completion(4, 3), 300)]
        target = 25
    else:
        unknown = message(clear, None, 110)
        messages += [unknown, message(clear, 120), message({"Sectors": {"0": {"Value": "25"}}}, 145)]
        if case == "unknown_repeat_exact":
            messages.insert(4, unknown)  # Adjacent copies retain capture multiplicity.
        elif case == "unknown_repeat":
            messages.append(message(clear, None, 160))
        messages += [message({"Sectors": {"1": {"Value": "26"}}}, 171),
                     message(completion(4, 3), 220), message({"Sectors": {"2": {"Value": "39"}}}, 221)]
        target = 24
    messages.append(message({"Sectors": {"0": {"Value": str(target)}}}, 400, number="1"))
    numbered = [replace(m, sequence=i) for i, m in enumerate(messages, 1)]
    if case == "unknown_repeat_exact":
        numbered[4] = numbered[3]  # Byte-identical envelope repeated within one capture.
    return numbered


def play_result(path):
    store = QualiSessionStore()
    service = QualiLiveService(eliminated_counts={"Q1": 0, "Q2": 0})
    emitted = []
    def consume(m):
        emitted.append(m)
        service.consume(store.apply(m))
    ReplayPlayer(path, speed=0).play(consume)
    raw = store.snapshot()
    driver = next(d for d in raw.drivers if d.driver_number == "31")
    projection = next(d for d in service.snapshot.drivers if d.driver_number == "1").projected_lap_s
    return emitted, raw, driver.best_lap_event_key, projection


@pytest.mark.parametrize("case", ["old_sectors", "valid_sectors", "unknown_repeat", "unknown_repeat_exact"])
@pytest.mark.parametrize("duplicate_input", [False, True])
def test_record_recover_replay_preserves_lifecycle(case, duplicate_input, tmp_path):
    messages = recovery_sequence(case)
    capture = tmp_path / "live_timing_raw.jsonl"
    recorder = LiveTimingRecorder(capture)
    for m in messages:
        recorder.write(m)
    recorder.close()
    if duplicate_input:
        shutil.copyfile(capture, tmp_path / "live_timing_raw_copy.jsonl")
    direct = play_result(capture)
    recovery = recover_replay_directory(tmp_path, year=2026, round_number=16, session="Q")
    canonical = play_result(recovery.path)
    expected_sectors = B if case in {"valid_sectors", "unknown_repeat_exact"} else EMPTY
    expected_projection = pytest.approx(86.4) if case in {"valid_sectors", "unknown_repeat_exact"} else None
    for result in (direct, canonical):
        emitted, raw, key, projection = result
        assert raw.completed_lap_sequence[0].sectors_s == A
        assert raw.completed_lap_sequence[-1].sectors_s == expected_sectors
        assert raw.completed_lap_sequence[-1].official_lap_number == 3
        assert key == raw.completed_lap_sequence[-1].event_key
        assert projection == expected_projection
        assert len(emitted) == len(messages)
    assert [m.publication_time for m in canonical[0]] == [m.publication_time for m in messages]
    assert recover_replay_directory(tmp_path, year=2026, round_number=16, session="Q").reused


@pytest.mark.parametrize("unknown_best", [
    {"Value": "1:30"}, {"Value": "1:30", "Lap": 3}, {"Lap": 2}, "",
])
def test_any_explicit_unknown_best_invalidates_only_current_recovery_eligibility(unknown_best):
    h = Delivery(utc_source=True)
    h.complete(alias=2)
    h.complete(4, 200, B, alias=3)
    h.send({"BestLapTime": unknown_best}, None, delivery=210)
    h.send({"Position": 1}, 211)
    assert h.owner() is None
    assert [(e.official_lap_number, e.sectors_s) for e in h.raw.completed_lap_sequence] == [(2, A), (3, B)]
    # A new timestamped best/alias publication can still use historical alias
    # knowledge; unlike Position-only delivery, this explicitly certifies best.
    h.send({"BestLapTime": {"Value": "1:30", "Lap": 2}}, 250)
    assert h.owner() == ("Q1", "31", 3, (NOW + timedelta(seconds=100)).timestamp())
    assert h.projection(24) == pytest.approx(108)
