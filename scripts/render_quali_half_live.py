"""Render the real monitor from saved research checkpoints without replay/refitting.

Usage: python scripts/render_quali_half_live.py --research-root /path/to/track-correction-research
The screenshots use a white palette, and the JSON records exact checkpoint provenance.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtGui, QtWidgets
from wodata.live import QualiPitState, QualiTyreStint
from woplanner.quali.gui import QualiWindow, format_countdown
from woplanner.quali.models import CutoffProjection, FinishedReason, QualiDriverState, QualiSessionSnapshot


def restore_snapshot(row):
    drivers = []
    for saved in row["drivers"]:
        value = dict(saved)
        value["pit_state"] = QualiPitState(value["pit_state"])
        value["finished_reason"] = FinishedReason(value["finished_reason"]) if value["finished_reason"] else None
        value["tyre_history"] = tuple(QualiTyreStint(**stint) for stint in value["tyre_history"])
        value["sectors_s"] = tuple(value["sectors_s"])
        value["historical_results"] = tuple(tuple(item) for item in value["historical_results"])
        drivers.append(QualiDriverState(**value))
    return QualiSessionSnapshot(
        session_part=row["part"], remaining_time_s=row["remaining_s"],
        session_status="Finished" if row["normal_finished"] else "Started",
        drivers=tuple(drivers), cutoff=CutoffProjection(**row["cutoff"]),
        actual_cutoffs=tuple(tuple(item) for item in row["actual_cutoffs"]),
        track_source=row["track_source"], normal_finished=row["normal_finished"],
        clock_running=False, track_evolution_result=row["result"],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "doc/assets/half_live")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setStyle("Fusion")
    palette = app.palette()
    for role in (QtGui.QPalette.ColorRole.Window, QtGui.QPalette.ColorRole.Base):
        palette.setColor(role, QtGui.QColor("white"))
    for role in (QtGui.QPalette.ColorRole.WindowText, QtGui.QPalette.ColorRole.Text,
                 QtGui.QPalette.ColorRole.ButtonText):
        palette.setColor(role, QtGui.QColor("#202124"))
    palette.setColor(QtGui.QPalette.ColorRole.Button, QtGui.QColor("#f3f4f5"))
    app.setPalette(palette)
    evidence = []
    for stream, part, state in (
        ("R14_live", "Q2", "supported"),
        ("R17_live", "Q3", "supported"),
        ("R16_live", "Q1", "limited"),
        ("R17_live", "Q2", "fallback"),
        ("R17_live", "Q1", "unavailable"),
    ):
        source = args.research_root / "data" / f"replay_{stream}_checkpoints.jsonl"
        candidates = [json.loads(line) for line in source.read_text().splitlines()]
        row = min((r for r in candidates if r["part"] == part), key=lambda r: abs(r["remaining_s"] - 300))
        snap = restore_snapshot(row)
        view = QualiWindow()
        view._sync_session_context(row["result"]["as_of"]["event"])
        view.connection.setText("Saved research checkpoint")
        view.clock.setText(f"{part} {format_countdown(snap.remaining_time_s)}")
        view._render_normal_finish(snap)
        view._render_cutoff_predictions(snap)
        view._render_timing_table(snap)
        view.service.snapshot = snap
        view._render_pit_status()
        for name, width in (("wide", 1500), ("narrow", 1100)):
            view.resize(width, 900 if name == "wide" else 750)
            view.show()
            app.processEvents()
            current = view.cutoff.geometry()
            experimental = view.experimental_cutoff.geometry()
            assert experimental.top() > current.bottom()
            assert experimental.bottom() < view.table.geometry().top()
            assert view.centralWidget().rect().contains(experimental)
            assert experimental.height() >= view.experimental_cutoff.heightForWidth(experimental.width())
            path = args.output / f"{stream.lower()}_{part.lower()}_{state}_{name}.png"
            assert view.grab().save(str(path))
            evidence.append({
                "image": path.name, "checkpoint_file": str(source),
                "message_index": row["message_index"], "remaining_s": row["remaining_s"],
                "part": part, "status": row["result"]["status"],
                "requested_width": width, "actual_width": view.width(), "height": view.height(),
                "current_text": view.cutoff.text(), "experimental_text": view.experimental_cutoff.text(),
                "experimental_tooltip": view.experimental_cutoff.toolTip(),
                "current_rect": current.getRect(), "experimental_rect": experimental.getRect(),
                "table_rect": view.table.geometry().getRect(),
            })
        view.close()
        app.processEvents()
    (args.output / "screenshots.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps([{"image": item["image"], "size": [item["actual_width"], item["height"]]} for item in evidence]))


if __name__ == "__main__":
    main()
