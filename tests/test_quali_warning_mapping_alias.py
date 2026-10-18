"""Independent warning counterexample: mapping cannot invent a new receipt."""
from datetime import timedelta

from wodata.live import FastF1LiveTimingSource, LiveTimingMessage
from woplanner.quali.service import QualiLiveService
from test_quali_c1_review_corrections import EPOCH


def test_validated_mapping_cannot_turn_retransmitted_position_into_measured_delay():
    service = QualiLiveService(telemetry_delay_lap_threshold=1)
    service.controller._source = FastF1LiveTimingSource()
    store = service.controller.store

    def packet(topic, payload, mono, session=None, source=None):
        message = LiveTimingMessage(
            topic, payload, EPOCH + timedelta(seconds=source) if source is not None else None,
            EPOCH + timedelta(seconds=mono), source="signalr_live",
            session_time_seconds=session, received_at_monotonic_s=mono,
        )
        raw = store.apply(message)
        service.consume(raw)
        return raw

    packet("DriverList", {"A": {"Tla": "A"}}, 90, 90, 90)
    packet("TimingData", {"Lines": {"A": {"NumberOfLaps": 1, "InPit": False}}},
           100, 100, 100)
    position = {"Position": [{"Timestamp": (EPOCH + timedelta(seconds=105)).isoformat(),
                            "Entries": {"A": {"X": 1, "Y": 2, "Status": "OnTrack"}}}]}
    first = packet("Position.z", position, 105)
    assert first.position_observations[0].acquisition_coordinate == "utc"
    packet("ExtrapolatedClock", {"Utc": (EPOCH + timedelta(seconds=106)).isoformat(),
                                "Remaining": "10:00"}, 106, 106)
    packet("TimingData", {"Lines": {"A": {"NumberOfLaps": 2,
                                          "LastLapTime": {"Value": "0:10.000"}}}},
           110, 110, 110)
    key = next(iter(service.telemetry_delays.completions))
    assert service._physical_traffic.completion_bounds(key) == (100., 110.)
    repeated = packet("Position.z", position, 113)
    assert repeated.position_observations[0].acquisition_coordinate == "session"
    assert service.telemetry_delays.count == 0
    assert service.telemetry_delays.warning == ""
    assert service.telemetry_delays.observed_late_samples == 0

    # A genuinely new acquisition, first received after completion, still warns.
    fresh = {"Position": [{"Timestamp": (EPOCH + timedelta(seconds=106)).isoformat(),
                          "Entries": {"A": {"X": 1, "Y": 2, "Status": "OnTrack"}}}]}
    packet("Position.z", fresh, 113.1)
    assert service.telemetry_delays.count == 1
    assert service.telemetry_delays.observed_late_samples == 1
