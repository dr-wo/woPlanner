"""Final lifecycle review reproductions, including real reducer completions."""
from datetime import timedelta
from statistics import median

import pandas as pd
import pytest

from wodata.live import QualiSessionStore
from woplanner.quali.physical_traffic import RollingQualiPhysicalTraffic
from woplanner.quali.service import QualiLiveService
from test_quali_traffic_review_blockers import EPOCH, apply, timing, real_field, samples


def test_real_return_and_delayed_states_use_source_time():
    store, adapter = real_field(starts={"A": 100., "B": 94., "C": 98.})
    timing(store, adapter, "B", {"InPit": True}, 104.25)
    before = samples(adapter)
    assert before.loc[104.5, "PhysicalDriverAhead"] == "C"
    assert before.loc[105., "PhysicalTimeDeltaToDriverAhead"] == pytest.approx(2.)
    timing(store, adapter, "B", {"InPit": False}, 105.)
    after = samples(adapter)
    pd.testing.assert_frame_equal(before.loc[:104.5], after.loc[:104.5])
    assert after.loc[105., "TrafficPhaseUnavailable"]
    assert pd.isna(after.loc[105., "PhysicalDriverAhead"])
    # Late evidence is ordered by original publication, even though the
    # reducer's operational clock already reached 110 s.
    timing(store, adapter, "B", {"InPit": True}, 104.75)
    timing(store, adapter, "B", {"InPit": False}, 104.)
    delayed = samples(adapter)
    assert delayed.loc[104.5, "PhysicalDriverAhead"] == "C"
    assert delayed.loc[105., "TrafficPhaseUnavailable"]
    timing(store, adapter, "B", {"InPit": True}, 105.)
    # Distinct same-receipt corrections follow causal delivery order. The
    # final authoritative off-track publication excludes B, so C's supported
    # phase is usable rather than preserving the superseded on-track return.
    corrected = samples(adapter)
    assert not corrected.loc[105., "TrafficPhaseUnavailable"]
    assert corrected.loc[105., "PhysicalDriverAhead"] == "C"
    assert corrected.loc[105., "PhysicalTimeDeltaToDriverAhead"] == pytest.approx(2.)


def completion_series(count=34, *, adapter_type=RollingQualiPhysicalTraffic):
    store, adapter = QualiSessionStore(), adapter_type()
    apply(store, adapter, "DriverList", {"A": {"Tla": "A"}}, 99.)
    timing(store, adapter, "A", {"NumberOfLaps": 1, "InPit": False}, 100.)
    keys = []
    for index in range(count):
        # First two low contributions are evicted; active history has 16 of
        # each length, with a high contribution first in insertion order.
        span = 1200. if 2 <= index < 18 else 1000.
        finish = append_completion(store, adapter, index, span)
        keys.append(store.snapshot().completed_lap_sequence[-1].event_key)
        adapter.finalize("A", index + 2)
        assert keys[-1][-1] == finish
    return store, adapter, keys


def append_completion(store, adapter, index, span):
    start = 100. + index * 2.5
    # The first sample is 0.5 s after the original counter, within the frozen
    # boundary tolerance; the retained 2 s traversal integrates to span.
    for step in range(1, 6):
        stamp = start + step * .5
        utc = (EPOCH + timedelta(seconds=stamp)).isoformat()
        apply(store, adapter, "CarData.z", {"Entries": [{"Utc": utc, "Cars": {
            "A": {"Channels": {"2": span / 2 * 3.6, "0": 12000}},
        }}]}, stamp)
        apply(store, adapter, "Position.z", {"Position": [{"Timestamp": utc, "Entries": {
            "A": {"X": 100, "Y": 100, "Status": "OnTrack"},
        }}]}, stamp)
    timing(store, adapter, "A", {"NumberOfLaps": index + 2,
                                "LastLapTime": {"Value": "0:02.500"}}, stamp)
    return stamp


def test_evicted_successful_completion_cannot_displace_new_evidence():
    store, adapter, keys = completion_series()
    assert len(keys) == 34
    assert tuple(adapter._span_history) == tuple(keys[2:])
    assert keys[0] not in adapter._span_history
    assert median(adapter._lap_spans) == 1100.
    before = tuple(adapter._span_history.items())
    for _ in range(3):
        result = adapter.finalize("A", 2)
        assert tuple(adapter._span_history.items()) == before
        assert result.circuit_length_m == 1100.
    append_completion(store, adapter, 34, 1200.)
    new_key = store.snapshot().completed_lap_sequence[-1].event_key
    adapter.finalize("A", 36)
    assert tuple(adapter._span_history) == (*keys[3:], new_key)
    assert median(adapter._lap_spans) == 1100.


def test_evicted_and_retained_revocations_remain_barred():
    _, adapter, keys = completion_series()
    before = tuple(adapter._span_history.items())
    adapter._revoke_completion(keys[0])
    adapter.finalize("A", 2)
    assert keys[0] in adapter._revoked
    assert tuple(adapter._span_history.items()) == before
    adapter._revoke_completion(keys[2])
    assert tuple(adapter._span_history) == tuple(keys[3:])
    adapter.finalize("A", 4)
    assert tuple(adapter._span_history) == tuple(keys[3:])
    assert median(adapter._lap_spans) == 1000.


def test_never_successfully_admitted_completion_can_retry():
    store, adapter = QualiSessionStore(), RollingQualiPhysicalTraffic()
    apply(store, adapter, "DriverList", {"A": {"Tla": "A"}}, 99.)
    timing(store, adapter, "A", {"NumberOfLaps": 1, "InPit": False}, 100.)
    append_completion(store, adapter, 0, 1000.)
    row = adapter._rows["A"][-1]
    anchor = row["LapEndTime"]
    row["LapEndTime"] = None
    adapter.finalize("A", 2)
    assert not adapter._lap_spans
    assert not adapter._admitted_completion_keys
    row["LapEndTime"] = anchor
    adapter.finalize("A", 2)
    assert adapter._lap_spans == (1000.,)
    assert adapter._admitted_completion_keys == {row["_CompletionKey"]}


def test_admission_identity_follows_service_session_lifecycle():
    store, adapter, keys = completion_series(count=1)
    service = QualiLiveService()
    service._physical_traffic = adapter
    service.transition_to("Q2")
    # The existing service keeps circuit evidence across parts. Stable keys
    # include the part; transitioning alone must not re-admit an old Q1 lap.
    assert service._physical_traffic is adapter
    assert adapter._admitted_completion_keys == set(keys)
    adapter.finalize("A", 2)
    assert adapter._lap_spans == (1000.,)
    apply(store, adapter, "SessionInfo", {"SessionPart": "Q2"}, 102.5)
    append_completion(store, adapter, 1, 1200.)
    q2_key = store.snapshot().completed_lap_sequence[-1].event_key
    assert q2_key[0] == "Q2"
    adapter.finalize("A", 3)
    assert adapter._admitted_completion_keys == {*keys, q2_key}
    assert adapter._lap_spans == (1000., 1200.)
    service.reset()
    assert service._physical_traffic is not adapter
    assert not service._physical_traffic._admitted_completion_keys
    assert not service._physical_traffic._revoked
    # A fresh session can admit even identical key values from an independent
    # store; the old session's identity must not be global/class-level state.
    _, fresh, fresh_keys = completion_series(count=1)
    assert fresh_keys == keys
    assert fresh._lap_spans == (1000.,)
