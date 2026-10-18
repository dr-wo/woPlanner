"""Replay R16 through the real Qt renderer and compare unchanged domain state."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

from wodata.live import LiveTimingMessage
from woplanner.quali.gui import QualiWindow
from woplanner.quali.widgets import format_time


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "historical_replay_helpers", ROOT / "scripts/verify_quali_r16_historical_cutoffs.py")
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
RECORDING = helpers.RECORDING
CHECKPOINTS = {3, 200, 4515, 7524, 7531, 8988, 9128, 9183, 9466, 11700, 12465, 12467, 15188}


def verify(recording=RECORDING, *, baseline=False):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = QualiWindow()
    window.timer.stop()
    service = window.service
    service._geometry_data_root = ROOT.parent / "woData"
    store = service.controller.store
    hashes, raw_hashes, markers, checkpoints = [], [], [], []
    raw_ledger_cache = {}
    previous_render = None
    render_count = 0
    try:
        for line in Path(recording).read_text().splitlines():
            record = json.loads(line)
            message = LiveTimingMessage(
                topic=record["topic"], payload=record["payload"],
                source_timestamp_utc=helpers._stamp(record.get("source_timestamp_utc")),
                received_at_utc=helpers._stamp(record["received_at_utc"]),
                schema_version=record.get("message_schema_version", 1),
                source=record["source"], sequence=record["sequence"],
                session_time_seconds=record.get("session_time_seconds"),
            )
            raw = store.apply(message)
            service._consume_message_snapshot(raw)
            snapshot = service.snapshot
            cutoff = snapshot.cutoff
            # Re-render whenever any old/new marker input or numeric label
            # changes. Reuse that exact table state on identical inputs.
            signature = (snapshot.session_part, tuple(d.driver_number for d in snapshot.drivers),
                         cutoff.cutoff_rank, cutoff.driver, cutoff.target_kind,
                         cutoff.current_cutoff_s, cutoff.projected_cutoff_s)
            if signature != previous_render or message.sequence in CHECKPOINTS:
                window._poll()
                previous_render = signature
                render_count += 1
                app.processEvents()
            assert window.table.rowCount() == len(snapshot.drivers)
            target = "Projected P1" if cutoff.target_kind == "P1" else "Projected cutoff"
            expected_label = (f"{target}: —" if cutoff.current_cutoff_s is None
                              else f"{target}: {format_time(cutoff.projected_cutoff_s)}")
            assert window.cutoff.text() == expected_label
            row = window.table.cutoff_row()
            if not baseline:
                rank = {"Q1": 16, "Q2": 10}.get(snapshot.session_part, 0)
                expected = rank - 1 if rank and len(snapshot.drivers) >= rank else None
                assert row == expected, (message.sequence, snapshot.session_part, row, expected)
            markers.append((message.sequence, snapshot.session_part, row, window.table.rowCount()))
            if message.sequence in CHECKPOINTS:
                checkpoints.append({
                    "sequence": message.sequence, "part": snapshot.session_part,
                    "rendered_boundary_after_rank": row + 1 if row is not None else None,
                    "row_count": window.table.rowCount(), "numeric_label": window.cutoff.text(),
                    "current_cutoff_s": cutoff.current_cutoff_s,
                    "projected_cutoff_s": cutoff.projected_cutoff_s,
                    "timed_active_drivers": sum(d.active and d.best_lap_s is not None for d in snapshot.drivers),
                    "threshold_driver": cutoff.driver,
                })
            assert service.snapshot is snapshot, "Rendering changed domain state"
            hashes.append(sha256(json.dumps(asdict(snapshot), sort_keys=True).encode()).hexdigest())
            raw_hashes.append(helpers._raw_snapshot_hash(raw, raw_ledger_cache))
        return {"message_count": len(hashes), "render_count": render_count,
                "domain_snapshot_hashes": hashes, "raw_snapshot_hashes": raw_hashes,
                "markers": markers, "checkpoints": checkpoints,
                "actual_cutoffs": dict(service.snapshot.actual_cutoffs)}
    finally:
        window.close()
        app.processEvents()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recording", type=Path, default=RECORDING)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    result = verify(args.recording, baseline=args.baseline)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    if args.compare:
        before = json.loads(args.compare.read_text())
        assert result["domain_snapshot_hashes"] == before["domain_snapshot_hashes"]
        assert result["raw_snapshot_hashes"] == before["raw_snapshot_hashes"]
        assert result["actual_cutoffs"] == before["actual_cutoffs"]
        assert [(c["current_cutoff_s"], c["projected_cutoff_s"], c["numeric_label"])
                for c in result["checkpoints"]] == [
                    (c["current_cutoff_s"], c["projected_cutoff_s"], c["numeric_label"])
                    for c in before["checkpoints"]]
        print(f"All {result['message_count']} domain/raw snapshots and numeric labels unchanged")
    print(json.dumps(result["checkpoints"], indent=2))
