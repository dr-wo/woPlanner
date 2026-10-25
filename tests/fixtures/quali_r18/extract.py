"""Rebuild the small R18 packet fixture from the original local recording.

Usage: python extract.py /path/to/live_timing_e664885c31e14b29a0a2c3633090c1fd.jsonl
The recording is optional for running tests; it is only required to regenerate.
Selected lines are copied byte for byte. No payload, driver, timestamp, or
sequence field is edited, and no FP reference or operational policy is added.
"""

import argparse
import hashlib
import json
from pathlib import Path


SOURCE_SHA256 = "533e7dc2b054568ede8142d2b32f3ce0e88778165b8e586939881102bb03de17"
LAST_SEQUENCE = 6623
# Original nearest preceding CarData packets for the ALB completion/pit entry
# and the four RUS/ANT completions. Speed evidence clears the reducer's outbound
# pit latch; omitting it would change these packets' real eligibility semantics.
DEPARTURE_SPEED_SEQUENCES = {513, 1440, 4562, 4641, 6550, 6617}


def selected(record):
    sequence, topic, payload = record["sequence"], record["topic"], record["payload"]
    if sequence > LAST_SEQUENCE:
        return False
    if sequence == 1 or sequence in DEPARTURE_SPEED_SEQUENCES:
        return True
    if topic in {"SessionInfo", "SessionStatus", "ExtrapolatedClock", "RaceControlMessages"}:
        return True
    if topic == "DriverList":
        return sequence == 2
    if topic not in {"TimingData", "TimingAppData"} or not isinstance(payload, dict):
        return False
    lines = payload.get("Lines", {})
    if topic == "TimingAppData":
        return any(number in lines for number in ("12", "63"))
    if "SessionPart" in payload:
        return True
    if sequence <= 1446 and "23" in lines:
        return True
    if any(number in lines for number in ("12", "63")):
        return True
    # Retain field ownership/ranking updates up to the first Q2 cutoff. The
    # complete original packet is kept even when other drivers share it.
    return sequence <= 4350 and any(
        isinstance(patch, dict)
        and {"Position", "BestLapTime", "BestLapTimes", "KnockedOut"}.intersection(patch)
        for patch in lines.values()
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    args = parser.parse_args()
    original = args.recording.read_bytes()
    assert hashlib.sha256(original).hexdigest() == SOURCE_SHA256, "Unexpected source recording"
    lines = original.splitlines(keepends=True)
    chosen = [(json.loads(line), line) for line in lines if selected(json.loads(line))]
    content = b"".join(line for _, line in chosen)
    destination = Path(__file__).parent
    metadata = {
        "schema_version": 1,
        "source_filename": args.recording.name,
        "source_sha256": SOURCE_SHA256,
        "source_bytes": len(original),
        "source_packets": len(lines),
        "validated_identity": {"year": 2026, "round_number": 18, "event_name": "United States Grand Prix", "session": "Q"},
        "selection": "extract.py::selected; complete original JSONL lines, original ordering; through sequence6623; initial heartbeat/driver roster, identity/status/clocks/race-control, ALB through1446, RUS/ANT timing and tyres, field ranking/best/knockout updates through4350, six nearest preceding CarData packets for departure-speed evidence",
        "reduction_limits": [
            "Most telemetry, non-target driver timing and later field updates are omitted; six original speed packets preserve target lap eligibility.",
            "Global PUSH indices belong to this reduced stream, not full-session indices28/29/31/32.",
            "No full-session fit, traffic coverage, lap total or pre-capture Q1 history is claimed.",
            "Synthetic references and COOL durations are test controls only, absent from the recording fixture.",
        ],
        "fixture_sha256": hashlib.sha256(content).hexdigest(),
        "fixture_bytes": len(content),
        "fixture_packets": len(chosen),
        "clock_contract": "The original full-capture investigation used native archive replay: ALB520 remaining58s. Direct original signalr_live envelopes apply the existing live receipt-latency correction and report55.936523s; both paths retain source/receipt UTC and completion identity.",
        "replay_sequence_contract": "Native ReplayPlayer renumbers deliveries1..N even without faults. Tests map delivery ordinal to preserved original sequence only to label checkpoints; direct signalr ingress retains original sequence numbers.",
        "selected_sequences": [record["sequence"] for record, _ in chosen],
        "line_sha256": {str(record["sequence"]): hashlib.sha256(line).hexdigest() for record, line in chosen},
        "original_checkpoints": {
            "520": {"driver": "ALB", "key": ["Q1", "23", 9, 1792876624.848], "lap_time_s": 96.967, "remaining_s": 58.0},
            "1446": {"driver": "ALB", "in_pit": True, "effective_finish": True, "remaining_s": 0.0},
            "2285": {"part": "Q2", "cutoff_s": None},
            "4350": {"part": "Q2", "cutoff_s": 96.031},
            "4568": {"driver": "RUS", "key": ["Q2", "63", 9, 1792877509.223], "lap_time_s": 94.780, "installation": 1},
            "4644": {"driver": "ANT", "key": ["Q2", "12", 9, 1792877521.705], "lap_time_s": 94.756, "installation": 1},
            "6555": {"driver": "RUS", "key": ["Q2", "63", 12, 1792877981.288], "lap_time_s": 94.305, "installation": 2},
            "6623": {"driver": "ANT", "key": ["Q2", "12", 12, 1792877990.288], "lap_time_s": 94.278, "installation": 2},
        },
    }
    (destination / "packets.jsonl").write_bytes(content)
    (destination / "provenance.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(f"Selected {len(chosen)} packets / {len(content)} bytes from {len(lines)} packets")


if __name__ == "__main__":
    main()
