"""Ignore only the accepted per-consume canonical envelope counter and receipt-envelope timestamp."""
from copy import deepcopy
from dataclasses import replace


def domain_snapshot(snapshot):
    result = deepcopy(snapshot.track_evolution_result)
    if result is not None:
        # Production increments this once per consume, even for retransmission.
        # Keep source/part/event provenance, estimates, quality metrics and ordinary rows.
        result['as_of']['publication_index'] = 0
        result['as_of']['published_at'] = None
    return replace(snapshot, track_evolution_result=result)
