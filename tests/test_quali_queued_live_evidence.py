"""Real queued receipts survive capped delivery, source end, and interruption."""
from datetime import datetime, timedelta, timezone

import pytest
from wodata.live import FastF1LiveTimingSource, LiveConnectionStatus, LiveTimingMessage
from woplanner.quali.lap_state import LapPaceReference
from woplanner.quali.service import QualiLiveService

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("finish", ["poll", "source_end", "stop"])
def test_actual_live_queued_grace_evidence_is_used_before_late_processing_lock(monkeypatch, finish):
    service = QualiLiveService()
    service.controller._source = FastF1LiveTimingSource()
    service._common_lap_state_reference = LapPaceReference(start_to_s1_s=4.)
    wall = [90.]
    monkeypatch.setattr("wodata.live.controller.time.monotonic", lambda: wall[0])
    monkeypatch.setattr("woplanner.quali.service.monotonic", lambda: wall[0])

    def queue(topic, payload, acquired, receipt=None, *, utc_offset=0):
        wall[0] = acquired if receipt is None else receipt
        service.controller._enqueue_message(LiveTimingMessage(topic, payload,
            EPOCH + timedelta(seconds=acquired),
            EPOCH + timedelta(seconds=wall[0] + utc_offset),
            source="signalr_live", session_time_seconds=acquired))

    def send(topic, payload, acquired):
        queue(topic, payload, acquired)
        service.controller.drain()

    send("SessionInfo", {"Name": "Qualifying"}, 89.)
    send("DriverList", {number: {"Tla": number} for number in "AB"}, 90.)
    for half in range(200, 223):
        stamp = half / 2.
        for number, start in (("A", 100.), ("B", 101.)):
            if stamp == start:
                send("TimingData", {"Lines": {number: {"NumberOfLaps": 1, "InPit": False}}}, stamp)
            if not start <= stamp <= start + 10:
                continue
            utc = (EPOCH + timedelta(seconds=stamp)).isoformat()
            send("CarData.z", {"Entries": [{"Utc": utc, "Cars": {
                number: {"Channels": {"2": 360., "0": 12000}}}}]}, stamp)
            send("Position.z", {"Position": [{"Timestamp": utc, "Entries": {
                number: {"X": 100, "Y": 100, "Status": "OnTrack"}}}]}, stamp)
            if stamp == start + 3:
                send("TimingData", {"Lines": {number: {"Sectors": {"0": {"Value": "0:03.000"}}}}}, stamp)
            if stamp == 110. and number == "A":
                send("TimingData", {"Lines": {number: {"NumberOfLaps": 2,
                    "LastLapTime": {"Value": "0:10.000"}}}}, stamp)
    target_key = next(key for key in service._pending_traffic if key[1] == "A")
    assert service._pending_traffic[target_key].completed_at == 110.
    for _ in range(500):
        queue("Heartbeat", {}, 112.)
    # Genuine arrival at +3, with a +60 UTC correction, waits behind one capped
    # batch. The execution callback at +50 must not replace this receipt.
    queue("TimingData", {"Lines": {"B": {"NumberOfLaps": 2,
        "LastLapTime": {"Value": "0:10.000"}}}}, 111., 113., utc_offset=60)
    assert service.controller._messages.queue[-1].received_at_monotonic_s == 113.
    wall[0] = 160.
    if finish == "source_end":
        service.controller._statuses.put(LiveConnectionStatus("disconnected", "Source ended"))
    if finish == "stop":
        monkeypatch.setattr(service.controller, "stop", lambda: None)
        service.stop()
    else:
        service.poll()
        assert service.controller._messages.qsize() == 1
        assert target_key in service._pending_traffic
        assert service._traffic_now == 112.
        assert next(sample for sample in service._track_samples if sample.driver == "A").traffic_status != "ok"
        service.poll()
    result = next(sample for sample in service._track_samples if sample.driver == "A")
    assert result.traffic_status == "ok"
    assert result.physical_mean_ahead_gap_s == pytest.approx(9.)
    assert result.ahead_coverage == pytest.approx(19 / 21)
    assert target_key not in service._pending_traffic
    records = [record for record in service.traffic_finalisation_records if record["key"] == target_key]
    assert records[0]["evidence_deadline_s"] == 112.
    assert records[1]["evidence_deadline_s"] == 113.
    assert records[1]["reason"] == "exceptional grace"
    assert records[0]["publication_clock_s"] == 160.
    assert records[1]["publication_clock_s"] == 160.
    assert records[1]["publication_delay_s"] == 50.
