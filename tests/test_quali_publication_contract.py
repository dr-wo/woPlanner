"""Cross-layer closure regressions; expected owners are explicit, not selected by production."""
from datetime import timedelta
import json
import logging
import sys
import types

import pytest

from wodata.live import LiveTimingController, LiveTimingMessage, QualiSessionStore
from wodata.live.fastf1_source import FastF1LiveTimingSource
from wodata.live.replay import ArchiveRecord, ReplayFaultConfig, ReplayPlayer, merge_records
from test_quali_projection_lifecycle_alias import Ingestion, NOW, A, B, PENDING, EMPTY, sectors


class Delivery(Ingestion):
    def __init__(self, path="apply", *, utc_source=False):
        self.path = path
        super().__init__(utc_source=utc_source)

    def send(self, patch, source, *, number="31", topic="TimingData", delivery=None, utc_source=None):
        payload = {"Lines": {number: patch}} if topic == "TimingData" else patch
        received = NOW + timedelta(seconds=delivery if delivery is not None else source)
        message = LiveTimingMessage(topic, payload,
            NOW + timedelta(seconds=source) if self.utc_source and source is not None else None,
            received, session_time_seconds=None if self.utc_source else source)
        if self.path == "apply_many":
            self.raw = self.store.apply_many([message])
        elif self.path == "queue":
            controller = LiveTimingController(store=self.store)
            controller._enqueue_message(message)
            self.raw = controller.drain().snapshot
        else:
            self.raw = self.store.apply(message)
        self.result = self.service.consume(self.raw)
        return self.raw


@pytest.mark.parametrize("path", ["apply", "apply_many", "queue"])
@pytest.mark.parametrize("known_alias", [False, True])
@pytest.mark.parametrize("retract", [False, True])
def test_contradictory_original_publication_precedes_established_owner(path, known_alias, retract):
    h = Delivery(path)
    h.complete(alias=2 if known_alias else None)
    clear = {"BestLapTime": {"Value": ""}}
    if retract:
        clear.update({"NumberOfLaps": 3, "LastLapTime": {"Value": ""}})
    h.send(clear, 101)
    h.complete(4, 200, B)
    h.send({"BestLapTime": {"Value": "1:30"}}, 200, delivery=201)
    assert h.owner() == ("Q1", "31", 4, 200.0)
    h.send({"BestLapTime": {"Value": "1:30", "Lap": 2}}, 100, delivery=202)
    first, second = h.raw.completed_lap_sequence
    assert second.official_lap_number is None
    assert h.owner() == ("Q1", "31", 4, 200.0)
    assert (first.sectors_s, second.sectors_s) == (A, B)
    h.send({"BestLapTime": {"Value": ""}}, 203)
    h.send({"BestLapTime": {"Value": "1:30", "Lap": 2}}, 204)
    assert h.owner() == (None if retract else ("Q1", "31", 3, 100.0))
    assert h.projection(24) == (None if retract else pytest.approx(108.0))


@pytest.mark.parametrize("path", ["apply", "apply_many", "queue"])
@pytest.mark.parametrize("utc_source", [False, True])
def test_unknown_clear_does_not_authorize_older_values(path, utc_source):
    h = Delivery(path, utc_source=utc_source)
    h.complete(alias=2)
    h.send({"Sectors": sectors(EMPTY)}, None, delivery=110)
    h.send({"Sectors": sectors(PENDING)}, 10, delivery=150)
    h.complete(4, 200, None, alias=3)
    assert h.raw.completed_lap_sequence[0].sectors_s == A
    assert h.raw.completed_lap_sequence[-1].sectors_s == EMPTY
    assert h.projection() is None


@pytest.mark.parametrize("path", ["apply", "apply_many", "queue"])
@pytest.mark.parametrize("parts", [("Q1", "Q2"), ("Q2", "Q3")])
@pytest.mark.parametrize("utc_source", [False, True])
def test_transition_fence_uses_transition_publication_not_latest_heartbeat(path, parts, utc_source):
    h = Delivery(path, utc_source=utc_source)
    h.send({"SessionPart": parts[0]}, 1, topic="SessionInfo")
    h.complete(alias=2)
    h.send({}, 150, topic="Heartbeat")
    h.send({"SessionPart": parts[1]}, 110, topic="SessionInfo", delivery=151)
    h.send({"Sectors": sectors(EMPTY)}, 120, delivery=152)
    h.send({"Sectors": {"0": {"Value": "25"}}}, 145, delivery=153)
    h.send({"Sectors": sectors(PENDING)}, 90, delivery=154)
    h.send({"Sectors": {"1": {"Value": "26"}}}, 171)
    h.complete(4, 220, None, alias=3)
    h.send({"Sectors": {"2": {"Value": "39"}}}, 221)
    source = (NOW + timedelta(seconds=220)).timestamp() if utc_source else 220.0
    assert h.owner() == (parts[1], "31", 4, source)
    assert h.raw.completed_lap_sequence[0].sectors_s == A
    assert h.raw.completed_lap_sequence[-1].sectors_s == B
    assert h.projection(24) == pytest.approx(86.4)


@pytest.mark.parametrize("path", ["apply", "apply_many", "queue"])
def test_distinct_accepted_publications_can_repeat_identical_sectors(path):
    h = Delivery(path)
    h.complete(alias=2)
    h.complete(4, 200, A, alias=3)
    assert h.owner() == ("Q1", "31", 4, 200.0)
    assert [(e.official_lap_number, e.sectors_s) for e in h.raw.completed_lap_sequence] == [(2, A), (3, A)]
    h.send({"Sectors": sectors(A)}, 100, delivery=210)
    assert h.owner() == ("Q1", "31", 4, 200.0)
    assert h.projection() == pytest.approx(112.5)


def test_archive_delay_changes_delivery_not_q1_publication(tmp_path):
    records = [
        ArchiveRecord("TimingData", 0, {"Lines": {"1": {"InPit": False, "NumberOfLaps": 1}}}, 0),
        ArchiveRecord("TimingData", 100, {"Lines": {"31": {
            "InPit": False, "NumberOfLaps": 3, "LastLapTime": {"Value": "1:30"},
            "BestLapTime": {"Value": "1:30", "Lap": 2}, "Sectors": sectors(A)}}}, 1),
        ArchiveRecord("TimingData", 190, {"Lines": {"31": {"Sectors": sectors(EMPTY)}}}, 2),
        ArchiveRecord("TimingData", 200, {"Lines": {"31": {"Sectors": sectors(PENDING)}}}, 3),
        ArchiveRecord("SessionInfo", 210, {"SessionPart": "Q2"}, 0),
        ArchiveRecord("TimingData", 300, {"Lines": {"31": {
            "InPit": False, "NumberOfLaps": 4, "LastLapTime": {"Value": "1:30"},
            "BestLapTime": {"Value": "1:30", "Lap": 3}}}}, 4),
    ]
    path = tmp_path / "archive.jsonl"
    path.write_text("".join(json.dumps(e.to_dict()) + "\n" for e in merge_records(records)))
    h = Delivery("queue")
    messages = []
    ReplayPlayer(path, speed=0, fault_config=ReplayFaultConfig(topic_delay_seconds={"TimingData": 60})).play(messages.append)
    for message in messages:
        h.raw = h.store.apply(message)
        h.result = h.service.consume(h.raw)
    assert h.raw.completed_lap_sequence[-1].sectors_s == EMPTY
    assert h.projection() is None
    delayed = [m for m in messages if m.topic == "TimingData" and "Sectors" in m.payload["Lines"].get("31", {})][-2:]
    assert [m.session_time_seconds for m in delayed] == [190, 200]
    assert [m.playback_time_seconds for m in delayed] == [250, 260]


def adapter_messages(monkeypatch, records):
    """Exercise the supported callback/subscription-result adapter without a network."""
    class FakeSignalRClient:
        def __init__(self, **kwargs):
            self.logger = logging.getLogger(__name__)
            self._connection = self._output_file = None
        def start(self):
            for record in records:
                self._on_message(record)
    module = types.ModuleType("fastf1.livetiming.client")
    module.SignalRClient = FakeSignalRClient
    monkeypatch.setitem(sys.modules, "fastf1.livetiming.client", module)
    messages = []
    FastF1LiveTimingSource().run(messages.append, lambda status: None)
    assert len(messages) == len(records)
    return messages


def test_subscription_result_clear_through_adapter_and_controller(monkeypatch):
    def callback(patch, seconds):
        return ["TimingData", {"Lines": {"31": patch}}, (NOW + timedelta(seconds=seconds)).isoformat()]
    records = [
        callback({"InPit": False, "NumberOfLaps": 3, "LastLapTime": {"Value": "1:30"},
                  "BestLapTime": {"Value": "1:30", "Lap": 2}, "Sectors": sectors(A)}, 100),
        types.SimpleNamespace(result={"TimingData": {"Lines": {"31": {"Sectors": sectors(EMPTY)}}}}),
        callback({"Sectors": sectors(PENDING)}, 10),
        callback({"NumberOfLaps": 4, "LastLapTime": {"Value": "1:30"},
                  "BestLapTime": {"Value": "1:30", "Lap": 3}}, 200),
    ]
    messages = adapter_messages(monkeypatch, records)
    assert messages[1].source_timestamp_utc is None
    assert messages[1].session_time_seconds is None
    assert [m.sequence for m in messages] == [1, 2, 3, 4]
    controller = LiveTimingController(store=QualiSessionStore())
    for message in messages:
        controller._enqueue_message(message)
    raw = controller.drain().snapshot
    assert raw.completed_lap_sequence[0].sectors_s == A
    assert raw.completed_lap_sequence[-1].sectors_s == EMPTY


@pytest.mark.parametrize("path", ["apply_many", "queue"])
def test_contradictory_alias_with_whole_stream_batched(path):
    h = Delivery()
    h.messages = []
    # Record at the public reducer entry point, then deliver all at once below.
    original_apply = h.store.apply
    def record(message):
        h.messages.append(message)
        return original_apply(message)
    h.store.apply = record
    h.complete()
    h.send({"NumberOfLaps": 3, "LastLapTime": {"Value": ""}, "BestLapTime": {"Value": ""}}, 101)
    h.complete(4, 200, B)
    h.send({"BestLapTime": {"Value": "1:30"}}, 200, delivery=201)
    h.send({"BestLapTime": {"Value": "1:30", "Lap": 2}}, 100, delivery=202)
    h.send({"BestLapTime": {"Value": ""}}, 203)
    h.send({"BestLapTime": {"Value": "1:30", "Lap": 2}}, 204)
    store = QualiSessionStore()
    if path == "apply_many":
        raw = store.apply_many(h.messages)
    else:
        controller = LiveTimingController(store=store)
        for message in h.messages:
            controller._enqueue_message(message)
        raw = controller.drain().snapshot
    assert [(e.event_key, e.official_lap_number, e.sectors_s) for e in raw.completed_lap_sequence] == [
        (("Q1", "31", 3, 100.0), 2, A), (("Q1", "31", 4, 200.0), None, B)]
    assert raw.drivers[0].best_lap_event_key is None


@pytest.mark.parametrize("delivery", ["callback", "v2"])
def test_original_publication_alias_through_supported_live_and_recorded_paths(monkeypatch, tmp_path, delivery):
    patches = [
        (100, {"InPit": False, "NumberOfLaps": 3, "LastLapTime": {"Value": "1:30"},
               "BestLapTime": {"Value": "1:30"}, "Sectors": sectors(A)}),
        (101, {"NumberOfLaps": 3, "LastLapTime": {"Value": ""}, "BestLapTime": {"Value": ""}}),
        (200, {"NumberOfLaps": 4, "LastLapTime": {"Value": "1:30"},
               "BestLapTime": {"Value": "1:30"}, "Sectors": sectors(B)}),
        (200, {"BestLapTime": {"Value": "1:30"}}),
        (100, {"BestLapTime": {"Value": "1:30", "Lap": 2}}),
        (203, {"BestLapTime": {"Value": ""}}),
        (204, {"BestLapTime": {"Value": "1:30", "Lap": 2}}),
    ]
    records = [["TimingData", {"Lines": {"31": patch}}, (NOW + timedelta(seconds=t)).isoformat()] for t, patch in patches]
    messages = adapter_messages(monkeypatch, records)
    if delivery == "v2":
        from wodata.live.recorder import LiveTimingRecorder
        path = tmp_path / "live.jsonl"
        recorder = LiveTimingRecorder(path)
        for message in messages:
            recorder.write(message)
        recorder.close()
        original = messages
        messages = []
        ReplayPlayer(path, speed=0).play(messages.append)
        assert [(m.source_timestamp_utc, m.received_at_utc, m.session_time_seconds) for m in messages] == [
            (m.source_timestamp_utc, m.received_at_utc, m.session_time_seconds) for m in original]
    controller = LiveTimingController(store=QualiSessionStore())
    for message in messages:
        controller._enqueue_message(message)
    raw = controller.drain().snapshot
    assert [(e.official_lap_number, e.sectors_s) for e in raw.completed_lap_sequence] == [(2, A), (None, B)]
    assert raw.completed_lap_sequence[0].event_key == ("Q1", "31", 3, (NOW + timedelta(seconds=100)).timestamp())
    assert raw.completed_lap_sequence[1].event_key == ("Q1", "31", 4, (NOW + timedelta(seconds=200)).timestamp())
    assert raw.drivers[0].best_lap_event_key is None


@pytest.mark.parametrize("same_completion", [False, True])
def test_known_alias_recovery_cannot_override_contradictory_original_publication(same_completion):
    h = Delivery()
    h.complete(alias=2)
    h.complete(4, 200, B, alias=3)
    h.send({"BestLapTime": {"Value": ""}}, 201)
    patch = {"BestLapTime": {"Value": "1:30", "Lap": 3}}
    if same_completion:
        patch.update({"NumberOfLaps": 5, "LastLapTime": {"Value": "1:30"}, "Sectors": sectors(A)})
    h.send(patch, 100, delivery=300)
    assert h.owner() is None
    assert [(e.official_lap_number, e.sectors_s) for e in h.raw.completed_lap_sequence[:2]] == [(2, A), (3, B)]
    if same_completion:
        assert h.raw.completed_lap_sequence[-1].sectors_s == EMPTY
        assert h.raw.completed_lap_sequence[-1].official_lap_number is None
    assert h.projection() is None


@pytest.mark.parametrize("known_alias", [False, True])
def test_unknown_values_and_metadata_cannot_certify_historical_owner(known_alias):
    h = Delivery(utc_source=True)
    h.complete(values=None, alias=2 if known_alias else None)
    h.send({"Sectors": sectors(A)}, None, delivery=110)
    h.send({"BestLapTime": {"Value": ""}}, 111)
    h.send({"BestLapTime": {"Value": "1:30", "Lap": 2}}, None, delivery=112)
    assert h.raw.completed_lap_sequence[0].sectors_s == EMPTY
    assert h.raw.completed_lap_sequence[0].official_lap_number == (2 if known_alias else None)
    assert h.owner() is None
    assert h.projection() is None


def test_unknown_live_checkpoint_can_use_an_already_certified_reference():
    h = Delivery(utc_source=True)
    h.complete(alias=2)
    h.send({"InPit": False, "NumberOfLaps": 1, "Sectors": {"0": {"Value": "25"}}},
           None, delivery=150, number="1")
    assert next(d for d in h.result.drivers if d.driver_number == "1").projected_lap_s == pytest.approx(112.5)
    assert h.raw.completed_lap_sequence[0].sectors_s == A


def test_timestamped_clear_restores_cycle_after_unknown_clear():
    h = Delivery(utc_source=True)
    h.complete(alias=2)
    h.send({"Sectors": sectors(EMPTY)}, None, delivery=110)
    h.send({"Sectors": sectors(PENDING)}, 10, delivery=111)
    h.send({"Sectors": sectors(EMPTY)}, 120)
    h.send({"Sectors": {"0": {"Value": "25"}}}, 145)
    h.send({"Sectors": {"1": {"Value": "26"}}}, 171)
    h.complete(4, 220, None, alias=3)
    h.send({"Sectors": {"2": {"Value": "39"}}}, 221)
    assert h.raw.completed_lap_sequence[-1].sectors_s == B
    assert h.projection(24) == pytest.approx(86.4)


@pytest.mark.parametrize("duplicate", ["same_boundary", "upward_counter"])
def test_duplicate_publication_never_transfers_owned_values(duplicate):
    h = Delivery()
    h.complete(alias=2)
    h.complete(3 if duplicate == "same_boundary" else 4, 100, A, alias=2)
    assert h.owner() == ("Q1", "31", 3, 100.0)
    assert h.raw.completed_lap_sequence[0].sectors_s == A
    if duplicate == "same_boundary":
        assert len(h.raw.completed_lap_sequence) == 1
    else:
        assert h.raw.completed_lap_sequence[1].sectors_s == EMPTY
        assert h.raw.completed_lap_sequence[1].official_lap_number is None


def test_equal_numbers_from_different_source_coordinates_do_not_identify_history():
    h = Delivery()
    h.complete(source=100, values=None)
    h.send({"BestLapTime": {"Value": ""}}, 101)
    message = LiveTimingMessage("TimingData", {"Lines": {"31": {
        "BestLapTime": {"Value": "1:30", "Lap": 2}, "Sectors": sectors(A)}}},
        NOW.fromtimestamp(100, NOW.tzinfo), NOW + timedelta(seconds=102))
    raw = h.store.apply(message)
    assert raw.completed_lap_sequence[0].sectors_s == EMPTY
    assert raw.completed_lap_sequence[0].official_lap_number is None
    assert next(d for d in raw.drivers if d.driver_number == "31").best_lap_event_key is None


def test_unknown_rejected_boundary_retires_pending_cycle_even_after_counter_recovery():
    h = Delivery(utc_source=True)
    h.complete(alias=2)
    h.send({"Sectors": sectors(EMPTY)}, 110)
    h.send({"Sectors": sectors(PENDING)}, 150)
    h.send({"NumberOfLaps": 2, "LastLapTime": {"Value": ""}}, None, delivery=200)
    h.send({"NumberOfLaps": 3}, 210)
    h.complete(4, 300, None, alias=3)
    assert h.raw.completed_lap_sequence[-1].sectors_s == EMPTY
    assert h.projection() is None
