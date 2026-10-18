"""Read-only full R16 comparison for the Q17-01/02 review corrections."""
from collections import Counter
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
from wodata.live import LiveTimingController, QualiSessionStore
from wodata.live.replay import ReplayPlayer
from woplanner.quali.service import QualiLiveService

ROOT = Path(__file__).resolve().parents[1]
RECORDING = ROOT / 'temp/r16_q_diagnostic_20261003_01a10147/data/r16_Q_recording.jsonl'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def verify(reducer_before, service_before):
    old_store = load('wodata.live._review_r16_before', reducer_before).QualiSessionStore()
    old_service = load('woplanner.quali._review_r16_before', service_before).QualiLiveService(LiveTimingController(store=old_store))
    store = QualiSessionStore()
    service = QualiLiveService(LiveTimingController(store=store))
    for instance in (service, old_service):
        instance._geometry_data_root = ROOT.parent / 'woData'
    previous_part = None
    transitions = []
    counts = {}
    events = 0
    compared_projection_rows = 0
    source_transitions = []

    def consume(message):
        nonlocal events, previous_part, compared_projection_rows
        old_raw = old_store.apply(message)
        raw = store.apply(message)
        old_service._consume_message_snapshot(old_raw)
        service._consume_message_snapshot(raw)
        # Compare every event at every publication, including later sector
        # enrichment, retractions and corrections to already accepted events.
        assert raw.completed_lap_sequence == old_raw.completed_lap_sequence, (message.sequence, 'completed events')
        assert len(raw.completed_lap_sequence) == len(old_raw.completed_lap_sequence)
        events = len(raw.completed_lap_sequence)
        actual = {d.driver_number: (d.projected_lap_s, d.projected_gap_s) for d in service.snapshot.drivers}
        expected = {d.driver_number: (d.projected_lap_s, d.projected_gap_s) for d in old_service.snapshot.drivers}
        assert actual == expected, (message.sequence, 'projection values')
        compared_projection_rows += len(actual)
        assert raw.session_part == old_raw.session_part
        count = counts.setdefault(raw.session_part, Counter())
        count['states'] += 1
        count['states_with_reference'] += int(service._reference_total_s is not None)
        count['projection_outputs'] += sum(d.projected_lap_s is not None for d in service.snapshot.drivers)
        if isinstance(message.payload, dict) and message.topic == 'TimingData' and 'SessionPart' in message.payload:
            part = message.payload['SessionPart']
            if not source_transitions or source_transitions[-1]['part'] != part:
                source_transitions.append({'sequence': message.sequence, 'part': part, 'source_timestamp': str(message.source_timestamp_utc)})
        if previous_part != raw.session_part:
            transitions.append({'sequence': message.sequence, 'part': raw.session_part, 'topic': message.topic, 'declared_part': message.payload.get('SessionPart')})
            previous_part = raw.session_part

    result = ReplayPlayer(RECORDING, speed=0).play(consume)
    assert result.messages_emitted == 16496 and result.status == 'completed'
    assert service.snapshot.actual_cutoffs == old_service.snapshot.actual_cutoffs
    cutoffs = dict(service.snapshot.actual_cutoffs)
    assert abs(cutoffs['Q1'] - 97.883) < 1e-10 and abs(cutoffs['Q2'] - 96.814) < 1e-10
    assert [t['part'] for t in transitions] == ['Q1', 'Q2', 'Q3']
    assert all(t['topic'] == 'TimingData' and t['declared_part'] == int(t['part'][-1]) for t in transitions[1:])
    for part, expected in {'Q1': (5840, 47324), 'Q2': (3524, 20123), 'Q3': (2882, 14907)}.items():
        assert (counts[part]['states_with_reference'], counts[part]['projection_outputs']) == expected
    return {'recording': str(RECORDING), 'sha256': sha256(RECORDING.read_bytes()).hexdigest(),
            'messages_compared': result.messages_emitted, 'completed_events_compared': events,
            'changed_completed_events': 0, 'projection_rows_compared': compared_projection_rows,
            'changed_projection_outputs': 0, 'cutoffs': cutoffs, 'counts': counts,
            'transitions': transitions, 'source_transitions': source_transitions,
            'baseline_reducer_sha256': sha256(Path(reducer_before).read_bytes()).hexdigest(),
            'baseline_service_sha256': sha256(Path(service_before).read_bytes()).hexdigest()}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline-reducer', type=Path, required=True)
    parser.add_argument('--baseline-service', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.baseline_reducer, args.baseline_service), indent=2))
