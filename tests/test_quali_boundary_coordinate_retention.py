"""Boundary capacity and anchor selection respect independent acquisition domains."""
from dataclasses import replace

from wodata.live.models import PublicationTime
from wodata.live.quali_models import QualiRawSnapshot, QualiTrafficEvidence
from woplanner.quali.physical_traffic import RollingQualiPhysicalTraffic


def evidence(lap, seconds, coordinate):
    return QualiTrafficEvidence("1", "lap_counter", PublicationTime(coordinate, seconds), lap_number=lap)


def test_mixed_domain_capacity_preserves_latest_session_boundary():
    adapter = RollingQualiPhysicalTraffic()
    entries = [evidence(1, 100., "session")]
    for i in range(140):
        entries.append(evidence(i + 2, 1767225700. + i, "utc"))
        adapter.ingest(replace(QualiRawSnapshot.empty(), traffic_evidence_sequence=tuple(entries),
                               traffic_clock_time_s=float(i), traffic_clock_kind="playback"))
    assert len(adapter._boundary_order["1"]) == 128
    assert adapter.lap_at_sample("1", 101., "session") == 2
    assert len(adapter._boundary_versions) == 128
    assert len(adapter._boundary_ordinals) == 128


def test_unmapped_start_selection_is_ambiguous_across_domains():
    adapter = RollingQualiPhysicalTraffic()
    entries = [evidence(1, 100., "session"), evidence(1, 1767225700., "utc")]
    adapter.ingest(replace(QualiRawSnapshot.empty(), traffic_evidence_sequence=tuple(entries),
                           traffic_clock_time_s=100., traffic_clock_kind="playback"))
    assert adapter._start_publication("1", 2) is None
    assert adapter._start_publication("1", 2, coordinate="session").seconds == 100.
    assert adapter._start_publication("1", 2, coordinate="utc").seconds == 1767225700.
