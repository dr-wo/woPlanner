"""An independently corrected traversal replaces withdrawn calibration safely."""
from datetime import timedelta

import pytest
from test_quali_third_historical_evidence import (
    EPOCH, calibrated_field, withdraw_old, timing, send, target,
)


@pytest.mark.parametrize("live", [False, True])
def test_withdrawal_then_new_valid_traversal_preserves_earlier_cutoffs_and_locked_target(live):
    service = calibrated_field(live)
    old_key = next(key for key in service._physical_traffic._span_history if key[1] == "C")
    withdraw_old(service, 113.)
    service.advance_traffic_clock(115.)
    locked = target(service)
    assert locked.traffic_circuit_length_m == pytest.approx(1000.)
    assert old_key in service._physical_traffic._revoked
    # A fresh independent pair of boundaries resolves future ownership, not
    # the permanently ambiguous original publication. The first traversal
    # after the pit exit is an out-lap; the next timed traversal is valid.
    timing(service, "C", {"NumberOfLaps": 3, "InPit": False}, 120.)
    for lap, start in ((4, 120.), (5, 130.)):
        for step in range(21):
            stamp = start + step / 2
            utc = (EPOCH + timedelta(seconds=stamp)).isoformat()
            # The shared 130s endpoint belongs to lap 4. Lap 5 starts
            # integration at its first distinct sample, 130.5s, so its
            # independently supplied speed covers 1200m in 9.5s.
            speed = 432. if lap == 4 else 1200. / 9.5 * 3.6
            send(service, "CarData.z", {"Entries": [{"Utc": utc, "Cars": {
                "C": {"Channels": {"2": speed}}}}]}, stamp)
            send(service, "Position.z", {"Position": [{"Timestamp": utc, "Entries": {
                "C": {"Status": "OnTrack", "X": 1, "Y": 1}}}]}, stamp)
            if step == 6:
                timing(service, "C", {"Sectors": {"0": {"Value": "0:03.000"}}}, stamp)
        timing(service, "C", {"NumberOfLaps": lap, "LastLapTime": {"Value": "0:10.000"}}, start + 10.)
    service.advance_traffic_clock(142.)
    adapter = service._physical_traffic
    corrected = next(key for key in adapter._span_history if key[1:3] == ("C", 5))
    assert corrected != old_key
    assert adapter._span_history[corrected] == pytest.approx(1200.)
    assert old_key not in adapter._span_history
    assert target(service) == locked
    earlier = adapter.finalize("A", 2, admit_span=False, evidence_deadline_s=112.,
                               clock_kind=service._traffic_clock_kind)
    withdrawn = adapter.finalize("A", 2, admit_span=False, evidence_deadline_s=113.,
                                 clock_kind=service._traffic_clock_kind)
    current = adapter.finalize("A", 2, admit_span=False)
    assert earlier.circuit_length_m == pytest.approx(1100.)
    assert withdrawn.circuit_length_m == pytest.approx(1000.)
    assert current.circuit_length_m == pytest.approx(1100.)
