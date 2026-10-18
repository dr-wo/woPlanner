"""Replay the real R16 recording through the current reducer, service and Qt.

Optional baseline modules compare every non-projection domain field against
the pre-change checkout. The default validation always uses production code.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from collections.abc import Mapping

from wodata.live import LiveTimingController, LiveTimingMessage, QualiSessionStore
from woplanner.quali.service import QualiLiveService, _checkpoint_elapsed


ROOT = Path(__file__).resolve().parents[1]
RECORDING = ROOT / "temp/r16_q_diagnostic_20261003_01a10147/data/r16_Q_recording.jsonl"


def _stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _domain(value, excluded=frozenset()):
    if is_dataclass(value):
        return {field.name: _domain(getattr(value, field.name), excluded)
                for field in fields(value) if field.name not in excluded}
    if isinstance(value, Mapping):
        return {key: _domain(item, excluded) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_domain(item, excluded) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _load_baseline(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def verify(recording=RECORDING, *, baseline_service=None, baseline_reducer=None):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from woplanner.quali.gui import QualiWindow
    from woplanner.quali.widgets import CutoffTable

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    table = CutoffTable()
    table.setColumnCount(len(QualiWindow.COLUMNS))
    renderer = SimpleNamespace(table=table)
    renderer._tyre_history_widget = lambda history: QualiWindow._tyre_history_widget(renderer, history)
    store = QualiSessionStore()
    service = QualiLiveService(LiveTimingController(store=store))
    service._geometry_data_root = ROOT.parent / "woData"
    old_store = old_service = None
    if baseline_service and baseline_reducer:
        old_store = _load_baseline("wodata.live._r16_projection_baseline", baseline_reducer).QualiSessionStore()
        old_service = _load_baseline("woplanner.quali._r16_projection_baseline", baseline_service).QualiLiveService(
            LiveTimingController(store=old_store))
        old_service._geometry_data_root = ROOT.parent / "woData"
    counts, first, anchors, transitions = {}, {}, {}, []
    previous_part = None
    compared = 0
    compared_lap_evidence = 0
    changed_projection_states = 0
    changed_provenance_states = 0

    def rendered(snapshot, driver_number):
        QualiWindow._render_timing_table(renderer, snapshot)
        row = next(index for index, driver in enumerate(snapshot.drivers)
                   if driver.driver_number == driver_number)
        return table.item(row, 12).text()

    def reference_owner(raw):
        return min((driver for driver in raw.drivers
                    if driver.driver_number in service._driver_best_references),
                   key=lambda driver: (driver.best_lap_s, driver.driver_number))

    with Path(recording).open() as stream:
        for line in stream:
            record = json.loads(line)
            message = LiveTimingMessage(
                topic=record["topic"], payload=record["payload"],
                source_timestamp_utc=_stamp(record.get("source_timestamp_utc")),
                received_at_utc=_stamp(record["received_at_utc"]),
                schema_version=record.get("message_schema_version", 1),
                source=record["source"], sequence=record["sequence"],
                session_time_seconds=record.get("session_time_seconds"),
            )
            raw = store.apply(message)
            service._consume_message_snapshot(raw)
            snapshot = service.snapshot
            part = raw.session_part
            count = counts.setdefault(part, Counter())
            initial = first.setdefault(part, {})
            count["message_states"] += 1
            if old_service is not None:
                old_raw = old_store.apply(message)
                old_service._consume_message_snapshot(old_raw)
                changed_provenance_states += int(
                    raw.completed_lap_sequence != old_raw.completed_lap_sequence
                    or [(d.driver_number, d.best_lap_event_key) for d in raw.drivers]
                    != [(d.driver_number, d.best_lap_event_key) for d in old_raw.drivers]
                    or service._driver_best_references != old_service._driver_best_references
                )
                # New fields are projection provenance only. All existing raw
                # state, completion recognition, operational state and cutoff
                # calculations must remain exactly the same at every message.
                excluded = {"best_lap_event_key", "official_lap_number", "lap_evidence_sequence"}
                assert len(raw.lap_evidence_sequence) == len(old_raw.lap_evidence_sequence)
                assert raw.lap_evidence_sequence[compared_lap_evidence:] == old_raw.lap_evidence_sequence[compared_lap_evidence:]
                compared_lap_evidence = len(raw.lap_evidence_sequence)
                raw_current = _domain(raw, excluded)
                raw_old = _domain(old_raw, excluded)
                for domain in (raw_current, raw_old):
                    for event in domain["completed_lap_sequence"]:
                        event.pop("sectors_s")
                assert raw_current == raw_old, (message.sequence, "raw state changed")
                excluded = {"projected_lap_s", "projected_gap_s"}
                assert _domain(snapshot, excluded) == _domain(old_service.snapshot, excluded), (
                    message.sequence, "non-projection service state changed")
                changed_projection_states += sum(
                    (driver.projected_lap_s, driver.projected_gap_s)
                    != (old_driver.projected_lap_s, old_driver.projected_gap_s)
                    for driver, old_driver in zip(snapshot.drivers, old_service.snapshot.drivers)
                )
                count["baseline_projected_driver_states"] += sum(
                    driver.projected_lap_s is not None for driver in old_service.snapshot.drivers)
                compared += 1
            if part != previous_part:
                assert service._reference_total_s is None
                assert not service._driver_best_references
                assert all(driver.best_lap_event_key is None for driver in raw.drivers)
                transitions.append({"sequence": message.sequence, "part": part,
                                    "reference_total_s": None, "cached_references": 0})
                previous_part = part
            if service._reference_total_s is not None:
                count["states_with_reference"] += 1
                if "reference" not in initial:
                    owner = reference_owner(raw)
                    event = next(event for event in raw.completed_lap_sequence
                                 if event.event_key == owner.best_lap_event_key)
                    initial["reference"] = {
                        "sequence": message.sequence, "driver": owner.driver_code,
                        "accepted_count": owner.completed_laps,
                        "completed_event": _domain(event), "event_key": owner.best_lap_event_key,
                        "reference_total_s": service._reference_total_s,
                    }
            projected_this_message = 0
            for driver in raw.drivers:
                state = next((state for state in snapshot.drivers
                              if state.driver_number == driver.driver_number), None)
                if state is None:
                    continue
                count["eligible_driver_states"] += int(driver.timed_lap_eligible)
                count["valid_driver_reference_states"] += int(driver.driver_number in service._driver_best_references)
                if not driver.timed_lap_eligible or not state.active:
                    assert state.projected_lap_s is None
                    count["ineligible_unprojected_driver_states"] += 1
                    count[f"unprojected_status_{state.status}"] += 1
                checkpoints = _checkpoint_elapsed(driver)
                common = checkpoints.keys() & service._reference_checkpoints.keys()
                if driver.timed_lap_eligible and not common and driver.last_lap_s is None:
                    assert state.projected_lap_s is None
                    count["insufficient_evidence_unprojected_driver_states"] += 1
                if state.projected_lap_s is None:
                    continue
                projected_this_message += 1
                count["projected_driver_states"] += 1
                assert driver.timed_lap_eligible and state.active
                if common:
                    checkpoint = max(common, key=lambda key: (key[0] == "segment", key[1]))
                    expected = checkpoints[checkpoint] * service._reference_total_s / service._reference_checkpoints[checkpoint]
                    assert math.isclose(state.projected_lap_s, expected, abs_tol=1e-10, rel_tol=0)
                else:
                    # Existing formula fallback; recorded explicitly, unchanged.
                    count["last_lap_fallback_outputs"] += 1
                owner = reference_owner(raw)
                for kind, qualifies in (
                    ("rendered_projection", True),
                    ("other_driver_sector_projection", driver.driver_number != owner.driver_number and bool(common)),
                ):
                    if kind not in initial and qualifies:
                        initial[kind] = {
                            "sequence": message.sequence, "driver": driver.driver_code,
                            "sectors_s": driver.sectors_s, "projected_lap_s": state.projected_lap_s,
                            "rendered": rendered(snapshot, driver.driver_number),
                            "reference_driver": owner.driver_code, "reference_event_key": owner.best_lap_event_key,
                        }
            count["states_with_projection"] += int(projected_this_message > 0)
            # Every cached reference must still have live, part-scoped provenance.
            for number in service._driver_best_references:
                driver = next(driver for driver in raw.drivers if driver.driver_number == number)
                assert driver.best_lap_event_key is not None and driver.best_lap_event_key[0] == part
                assert any(event.event_key == driver.best_lap_event_key for event in raw.completed_lap_sequence)
            if message.sequence in (1685, 2465):
                oco = next(driver for driver in raw.drivers if driver.driver_code == "OCO")
                ver = next(driver for driver in snapshot.drivers if driver.driver == "VER")
                anchors[str(message.sequence)] = {
                    "oco_accepted_count": oco.completed_laps, "oco_event_key": oco.best_lap_event_key,
                    "reference_total_s": service._reference_total_s,
                    "reference_checkpoints": [[list(key), value] for key, value in service._reference_checkpoints.items()],
                    "ver": _domain(ver), "ver_rendered": rendered(snapshot, ver.driver_number),
                }
    table.close()
    app.processEvents()
    # SessionPart=2 is published at sequence 7525, before the clock reset.
    assert counts["Q1"]["message_states"] == 7524
    assert first["Q1"]["reference"]["sequence"] == 1685
    assert first["Q1"]["reference"]["accepted_count"] != first["Q1"]["reference"]["completed_event"]["lap_number"]
    assert anchors["2465"]["ver"]["projected_lap_s"] is not None
    assert anchors["2465"]["ver_rendered"] == "1:36.722"
    sources = {"reducer": str(Path(sys.modules[QualiSessionStore.__module__].__file__).resolve()),
               "service": str(Path(sys.modules[QualiLiveService.__module__].__file__).resolve())}
    return {
        "recording": str(recording), "recording_sha256": sha256(Path(recording).read_bytes()).hexdigest(),
        "counts": counts, "first": first, "anchors": anchors, "transitions": transitions,
        "baseline_non_projection_states_compared": compared,
        "baseline_changed_provenance_states": changed_provenance_states if compared else None,
        "baseline_changed_projection_driver_states": changed_projection_states if compared else None,
        "runtime_sources": sources,
        "source_sha256": {name: sha256(Path(path).read_bytes()).hexdigest() for name, path in sources.items()},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--recording", type=Path, default=RECORDING)
    parser.add_argument("--baseline-service", type=Path)
    parser.add_argument("--baseline-reducer", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-identical", action="store_true")
    args = parser.parse_args()
    result = verify(args.recording, baseline_service=args.baseline_service, baseline_reducer=args.baseline_reducer)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    if args.require_identical:
        assert result["baseline_non_projection_states_compared"] == 16496
        assert result["baseline_changed_provenance_states"] == 0
        assert result["baseline_changed_projection_driver_states"] == 0
        for part, expected in {"Q1": (5840, 47324), "Q2": (3524, 20123), "Q3": (2882, 14907)}.items():
            assert tuple(result["counts"][part][field] for field in
                         ("states_with_reference", "projected_driver_states")) == expected
    print(json.dumps({key: result[key] for key in ("counts", "transitions", "first", "baseline_non_projection_states_compared")}, indent=2))


if __name__ == "__main__":
    main()
