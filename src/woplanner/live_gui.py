from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from wodata.live import LiveConfig, ReplayConfig

from .live_service import (
    REFERENCE_MODE_AVERAGED_PACE,
    REFERENCE_MODE_LABELS,
    LivePlannerService,
)
from .plot_widget import RelativeDeltaPlotWidget


class LivePlannerWindow(QtWidgets.QMainWindow):
    def __init__(self, service: LivePlannerService | None = None) -> None:
        super().__init__()
        self.service = service or LivePlannerService()
        self._driver_signature: tuple = ()
        self._metadata_warning_shown = False
        self.setWindowTitle("woPlanner Live")
        self.resize(1200, 760)
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        controls = QtWidgets.QHBoxLayout()
        self.year_input = QtWidgets.QSpinBox()
        self.year_input.setRange(1950, 2100)
        self.year_input.setValue(datetime.now().year)
        self.round_input = QtWidgets.QSpinBox()
        self.round_input.setRange(1, 30)
        self.session_input = QtWidgets.QLineEdit("R")
        self.session_input.setMaximumWidth(50)
        self.connect_button = QtWidgets.QPushButton("Connect")
        self.disconnect_button = QtWidgets.QPushButton("Disconnect")
        self.replay_path = QtWidgets.QLineEdit()
        self.replay_path.setPlaceholderText("Replay file")
        self.replay_browse_button = QtWidgets.QPushButton("Browse")
        self.replay_button = QtWidgets.QPushButton("Replay")
        self.replay_speed = QtWidgets.QComboBox()
        for label, speed in (("Max", 0.0), ("1x", 1.0), ("5x", 5.0), ("20x", 20.0)):
            self.replay_speed.addItem(label, speed)
        self.reference_driver = QtWidgets.QComboBox()
        self.reference_mode = QtWidgets.QComboBox()
        for mode, label in REFERENCE_MODE_LABELS.items():
            self.reference_mode.addItem(label, mode)
        average_index = self.reference_mode.findData(REFERENCE_MODE_AVERAGED_PACE)
        if average_index >= 0:
            self.reference_mode.setCurrentIndex(average_index)
        self.line_width = QtWidgets.QDoubleSpinBox()
        self.line_width.setRange(0.5, 8.0)
        self.line_width.setSingleStep(0.5)
        self.line_width.setValue(3.0)
        for label, widget in (
            ("Year", self.year_input), ("Round", self.round_input),
            ("Session", self.session_input), ("Replay", self.replay_path),
            ("Speed", self.replay_speed), ("Reference", self.reference_driver),
            ("Mode", self.reference_mode), ("Width", self.line_width),
        ):
            controls.addWidget(QtWidgets.QLabel(label))
            controls.addWidget(widget)
        controls.addWidget(self.replay_browse_button)
        controls.addWidget(self.connect_button)
        controls.addWidget(self.replay_button)
        controls.addWidget(self.disconnect_button)
        status_row = QtWidgets.QHBoxLayout()
        self.connection_status = QtWidgets.QLabel("Idle")
        self.last_message_status = QtWidgets.QLabel("Last message: —")
        self.session_status = QtWidgets.QLabel("Session: —")
        status_row.addWidget(self.connection_status)
        status_row.addStretch(1)
        status_row.addWidget(self.session_status)
        status_row.addWidget(self.last_message_status)
        self.relative_plot = RelativeDeltaPlotWidget()
        self.relative_plot.set_hover_enabled(True)
        layout.addLayout(controls)
        layout.addLayout(status_row)
        layout.addWidget(self.relative_plot, stretch=1)
        self.setCentralWidget(central)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._poll)
        self.connect_button.clicked.connect(self._connect)
        self.disconnect_button.clicked.connect(self._disconnect)
        self.replay_button.clicked.connect(self._replay)
        self.replay_browse_button.clicked.connect(self._browse_replay)
        self.reference_driver.currentTextChanged.connect(self._redraw)
        self.reference_mode.currentTextChanged.connect(self._redraw)
        self.line_width.valueChanged.connect(self._redraw)
        self.relative_plot.driver_selected.connect(self._select_reference)

    def _connect(self) -> None:
        session = self.session_input.text().strip().upper()
        if not session:
            QtWidgets.QMessageBox.warning(self, "Live timing", "Session is required.")
            return
        self._prepare_start()
        try:
            self.service.start_live(
                LiveConfig(self.year_input.value(), self.round_input.value(), session)
            )
        except Exception as exc:
            self._show_start_error(exc)

    def _replay(self) -> None:
        path = Path(self.replay_path.text().strip()).expanduser()
        if not path.is_file():
            QtWidgets.QMessageBox.warning(self, "Replay", "Choose an existing replay file.")
            return
        self._prepare_start()
        try:
            self.service.start_replay(
                ReplayConfig(path, float(self.replay_speed.currentData()))
            )
        except Exception as exc:
            self._show_start_error(exc)

    def _prepare_start(self) -> None:
        self.connect_button.setEnabled(False)
        self.replay_button.setEnabled(False)
        self._driver_signature = ()
        self._metadata_warning_shown = False
        self.connection_status.setText("Starting…")
        self.timer.start()

    def _show_start_error(self, exc: Exception) -> None:
        self.timer.stop()
        self.connect_button.setEnabled(True)
        self.replay_button.setEnabled(True)
        self.connection_status.setText(f"Error: {exc}")

    def _browse_replay(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Open live timing replay",
            "",
            "Replay files (*.jsonl *.txt);;JSONL replay (*.jsonl);;"
            "Legacy replay (*.txt);;All files (*)",
        )
        if path:
            self.replay_path.setText(path)

    def _poll(self) -> None:
        update = self.service.poll()
        for status in update.statuses:
            self.connection_status.setText(status.message)
            if status.state in {"disconnected", "error"}:
                self.connect_button.setEnabled(True)
                self.replay_button.setEnabled(True)
        snapshot = update.snapshot
        self._warn_for_metadata_mismatch(snapshot.session_info)
        self.session_status.setText(f"Session: {snapshot.session_status or '—'}")
        if snapshot.last_message_at_utc is not None:
            self.last_message_status.setText(
                f"Last message: {snapshot.last_message_at_utc.astimezone().strftime('%H:%M:%S')}"
            )
        drivers = self.service.available_drivers()
        signature = tuple(drivers.get("driver", pd.Series(dtype=str)).astype(str))
        if signature != self._driver_signature:
            current = self.reference_driver.currentText()
            self.reference_driver.blockSignals(True)
            self.reference_driver.clear()
            self.reference_driver.addItems(list(signature))
            target = current if current in signature else self.service.default_reference_driver()
            if target:
                index = self.reference_driver.findText(target)
                self.reference_driver.setCurrentIndex(max(index, 0))
            self.reference_driver.blockSignals(False)
            self._driver_signature = signature
            update = type(update)(update.snapshot, update.statuses, True)
        if update.changed:
            self._redraw()

    def _warn_for_metadata_mismatch(self, session_info) -> None:
        if self._metadata_warning_shown or not session_info:
            return
        mismatches = []
        start_date = str(session_info.get("StartDate") or "")
        if len(start_date) >= 4 and start_date[:4].isdigit():
            if int(start_date[:4]) != self.year_input.value():
                mismatches.append(f"year {start_date[:4]}")
        actual_session = str(session_info.get("Name") or "").strip().upper()
        requested = self.session_input.text().strip().upper()
        aliases = {"R": "RACE", "Q": "QUALIFYING", "S": "SPRINT"}
        expected = aliases.get(requested, requested)
        if actual_session and expected and expected not in actual_session:
            mismatches.append(f"session {session_info.get('Name')}")
        if mismatches:
            self._metadata_warning_shown = True
            self.connection_status.setText(
                "Warning: feed metadata differs from requested " + ", ".join(mismatches)
            )

    def _redraw(self) -> None:
        reference = self.reference_driver.currentText()
        if not reference:
            return
        drivers = self.service.available_drivers()
        ranks = self.service.build_driver_ranks()
        start = ranks.dropna(subset=["start_rank"]).rename(columns={"start_rank": "rank"})
        current = ranks.dropna(subset=["latest_rank"]).rename(columns={"latest_rank": "rank"})
        self.relative_plot.set_line_width(self.line_width.value())
        self.relative_plot.set_session_drivers(drivers)
        self.relative_plot.set_rank_data(start, current)
        plot_data = self.service.build_plot_data(
            reference,
            reference_mode=str(
                self.reference_mode.currentData() or REFERENCE_MODE_AVERAGED_PACE
            ),
        )
        self.relative_plot.set_plot_data(plot_data)
        self.relative_plot.set_tyre_event_data(self.service.build_tyre_event_data())

    def _select_reference(self, driver: str) -> None:
        index = self.reference_driver.findText(driver)
        if index >= 0:
            self.reference_driver.setCurrentIndex(index)

    def _disconnect(self) -> None:
        self.timer.stop()
        self.service.stop()
        self.connect_button.setEnabled(True)
        self.replay_button.setEnabled(True)
        self.connection_status.setText("Disconnected by user")

    def closeEvent(self, event) -> None:
        self._disconnect()
        event.accept()


def main() -> int:
    pg.setConfigOption("background", "w")
    pg.setConfigOption("foreground", "k")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    window = LivePlannerWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
