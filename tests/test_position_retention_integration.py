"""Track-map and diagnostic cuts share the same bounded position store."""
from datetime import datetime, timedelta, timezone
import math
from types import SimpleNamespace

import pytest

from wodata.live import LiveTimingMessage
from woplanner.live.telemetry import (
    CausalAnalysisCut,
    _CausalSnapshotBuilder,
    _analysis_store,
    _reference_from_store,
    _snapshot_at_cut,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def positions(times):
    return LiveTimingMessage(
        "Position.z",
        {"Position": [
            {
                "Timestamp": (NOW + timedelta(seconds=t)).isoformat(),
                "Entries": {"1": {
                    "X": 100 * math.cos(2 * math.pi * t / 100),
                    "Y": 100 * math.sin(2 * math.pi * t / 100),
                    "Z": 0, "Status": "OnTrack",
                }},
            }
            for t in times
        ]},
        NOW, NOW, session_time_seconds=0,
    )


def test_shared_store_keeps_real_track_reference_after_sample_cap_is_reached():
    store = _analysis_store()
    # 10 Hz exceeds the count cap within the time window. A recent complete
    # 100-second lap must remain available to the existing track-map extractor.
    store.reduce(positions([i / 10 for i in range(7001)]))
    samples = store.snapshot().position_samples
    assert len(samples) == 6000
    assert samples[0].session_time_seconds == pytest.approx(100.1)
    assert samples[-1].session_time_seconds == 700
    reference_input = SimpleNamespace(
        _position_samples=store._position_samples,
        _laps={"1": {
            1: {"lap_end_time_s": 600},
            2: {"lap_end_time_s": 700},
        }},
    )
    reference = _reference_from_store(reference_input, {"1": {"Tla": "AAA"}})
    assert reference is not None
    assert reference.lap_number == 2
    assert reference.lap_time_s == 100
    assert len(reference.points) == 1001

    # Old batches/duplicates cannot rewind the map or grow retained history.
    store.reduce(positions([50, 650, 700]))
    assert store.snapshot().position_samples == samples
    # The time bound applies even when fewer than 6,000 samples remain.
    store.reduce(positions([1900]))
    assert [s.session_time_seconds for s in store.snapshot().position_samples] == [700, 1900]
    store.reduce(positions([1900.1, 50]))
    assert [s.session_time_seconds for s in store.snapshot().position_samples] == [1900, 1900.1]


def test_causal_builder_reconstructs_old_cut_after_warmed_history_was_evicted():
    history = [
        (1, positions([90, 110, 80])),  # Batched future sample is excluded by time.
        (2, positions([50])),          # Late old sample is excluded by ingress.
        (3, positions([2000])),        # Warming evicts the original time window.
    ]
    builder = _CausalSnapshotBuilder()
    old_cut = CausalAnalysisCut(1, 100, 1, 1)
    warm_cut = CausalAnalysisCut(2, 2000, 3, 1)
    original = _snapshot_at_cut(builder.build(history, old_cut), old_cut)
    assert [s.session_time_seconds for s in original.position_samples] == [80, 90]
    warmed = _snapshot_at_cut(builder.build(history, warm_cut), warm_cut)
    assert [s.session_time_seconds for s in warmed.position_samples] == [2000]
    rebuilt = _snapshot_at_cut(builder.build(history, old_cut), old_cut)
    assert rebuilt.position_samples == original.position_samples
