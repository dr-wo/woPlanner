"""Qt entry point for the qualifying monitor."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from wodata.colours import get_team_colour
from wodata.live import LiveConfig, ReplayConfig

from .analysis import QualiAnalysisService
from .service import QualiLiveService
from .widgets import CutoffTable, QualiPlanDialog, format_time


WORKSPACE_DATA_ROOT = Path(__file__).resolve().parents[4] / "woData"


class QualiWindow(QtWidgets.QMainWindow):
    COLUMNS = (
        "Pos",
        "Driver",
        "Team",
        "Status",
        "S1",
        "S2",
        "S3",
        "Last",
        "Best",
        "Gap",
        "Interval",
        "Projected",
        "Projected Gap",
    )

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("woPlanner Qualifying Monitor")
        self.resize(1500, 900)
        data_root = WORKSPACE_DATA_ROOT if WORKSPACE_DATA_ROOT.is_dir() else None
        self.analysis_service = QualiAnalysisService(data_root)
        self.service = QualiLiveService()
        self.plan_dialog = None

        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        layout.addLayout(self._build_controls())
        layout.addLayout(self._build_status_row())

        self.table = CutoffTable()
        self.table.setColumnCount(len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.table, 1)

        self.pit_table = QtWidgets.QTableWidget(3, 2)
        self.pit_table.setHorizontalHeaderLabels(["State", "Drivers"])
        self.pit_table.verticalHeader().hide()
        self.pit_table.setMaximumHeight(145)
        self.pit_table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.pit_table)
        self.setCentralWidget(central)

        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._poll)
        self.connect_button.clicked.connect(self._connect)
        self.replay.clicked.connect(self._start_replay)
        self.browse.clicked.connect(self._browse)
        self.analyze.clicked.connect(self._analyze)
        self.plan.clicked.connect(self._show_plan)

    def _build_controls(self) -> QtWidgets.QHBoxLayout:
        controls = QtWidgets.QHBoxLayout()
        self.year = QtWidgets.QSpinBox()
        self.year.setRange(1950, 2100)
        self.year.setValue(datetime.now().year)
        self.round = QtWidgets.QSpinBox()
        self.round.setRange(1, 30)
        self.connect_button = QtWidgets.QPushButton("Connect")
        self.replay_path = QtWidgets.QLineEdit()
        self.replay_path.setPlaceholderText("Replay file")
        self.browse = QtWidgets.QPushButton("Browse")
        self.replay = QtWidgets.QPushButton("Replay")
        self.analyze = QtWidgets.QPushButton("Analyse FP1–FP3")
        self.plan = QtWidgets.QPushButton("Quali Plan")
        for label, widget in (
            ("Year", self.year),
            ("Round", self.round),
            ("Replay", self.replay_path),
        ):
            controls.addWidget(QtWidgets.QLabel(label))
            controls.addWidget(widget)
        for widget in (
            self.browse,
            self.connect_button,
            self.replay,
            self.analyze,
            self.plan,
        ):
            controls.addWidget(widget)

        self.elimination = {}
        for part in ("Q1", "Q2", "Q3"):
            spin = QtWidgets.QSpinBox()
            spin.setRange(0, 20)
            spin.setValue(6)
            spin.valueChanged.connect(
                lambda value, p=part: self.service.eliminated_counts.__setitem__(
                    p, value
                )
            )
            controls.addWidget(QtWidgets.QLabel(f"{part} eliminated"))
            controls.addWidget(spin)
            self.elimination[part] = spin
        controls.addStretch(1)
        return controls

    def _build_status_row(self) -> QtWidgets.QHBoxLayout:
        status = QtWidgets.QHBoxLayout()
        self.connection = QtWidgets.QLabel("Idle")
        self.clock = QtWidgets.QLabel("Q1 —:—")
        self.cutoff = QtWidgets.QLabel("Projected cutoff: —")
        status.addWidget(self.connection)
        status.addStretch(1)
        status.addWidget(self.clock)
        status.addWidget(self.cutoff)
        return status

    def _connect(self) -> None:
        try:
            self.service.start_live(
                LiveConfig(
                    self.year.value(),
                    self.round.value(),
                    "Q",
                    data_root=WORKSPACE_DATA_ROOT,
                )
            )
            self.timer.start()
            self.connection.setText("Connecting…")
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Live timing", str(exc))

    def _start_replay(self) -> None:
        path = Path(self.replay_path.text()).expanduser()
        if not path.is_file():
            QtWidgets.QMessageBox.warning(
                self, "Replay", "Choose an existing replay file."
            )
            return
        try:
            self.service.configure_event_geometry(
                self.year.value(), self.round.value(), WORKSPACE_DATA_ROOT
            )
            self.service.start_replay(ReplayConfig(path, 0.0))
            self.timer.start()
            self.connection.setText("Replaying…")
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Replay", str(exc))

    def _browse(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Replay file")
        if path:
            self.replay_path.setText(path)

    def _analyze(self) -> None:
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            result = self.analysis_service.analyze(
                self.year.value(), self.round.value()
            )
            rate, _, _ = self.analysis_service.effective_fp_track_rate()
            self.service.track_rate_s_per_field_push = rate
            if self.plan_dialog:
                self.plan_dialog.load_analysis(result)
            QtWidgets.QMessageBox.information(
                self,
                "Pre-qualifying analysis",
                f"Analysed {len(result.laps)} FP laps and saved planner artifacts.",
            )
        except Exception as exc:
            QtWidgets.QMessageBox.critical(
                self, "Pre-qualifying analysis", str(exc)
            )
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    def _show_plan(self) -> None:
        if self.analysis_service.result is None:
            try:
                self.analysis_service.load(self.year.value(), self.round.value())
            except Exception:
                pass
        if self.plan_dialog is None:
            self.plan_dialog = QualiPlanDialog(
                self.analysis_service, self.service, self
            )
            self.plan_dialog.finished.connect(
                lambda: setattr(self, "plan_dialog", None)
            )
        self.plan_dialog.show()
        self.plan_dialog.raise_()

    def _poll(self) -> None:
        snapshot, statuses, _ = self.service.poll()
        for status in statuses:
            self.connection.setText(status.message)
        remaining = snapshot.remaining_time_s
        remaining_text = format_time(remaining) if remaining is not None else "—:—"
        self.clock.setText(f"{snapshot.session_part} {remaining_text}")
        projected_cutoff = snapshot.cutoff.projected_cutoff_s
        if snapshot.cutoff.current_cutoff_s is None:
            self.cutoff.setText("Projected cutoff: —")
        else:
            self.cutoff.setText(
                f"Projected cutoff: {format_time(projected_cutoff)}"
            )
        self._render_timing_table(snapshot)
        self._render_pit_status()

    def _render_timing_table(self, snapshot) -> None:
        self.table.setRowCount(len(snapshot.drivers))
        self.table.cutoff_rank = snapshot.cutoff.cutoff_rank
        for row, state in enumerate(snapshot.drivers):
            values = (
                state.rank or "—",
                state.driver,
                state.team,
                state.status,
                *(format_time(value) for value in state.sectors_s),
                format_time(state.last_lap_s),
                format_time(state.best_lap_s),
                format_time(state.gap_s, gap=True),
                format_time(state.interval_s, gap=True),
                format_time(state.projected_lap_s),
                format_time(state.projected_gap_s, gap=True),
            )
            colour = get_team_colour(state.team)
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(str(value))
                if column == 1 and colour:
                    item.setBackground(QtGui.QColor(colour))
                    item.setForeground(QtGui.QColor("white"))
                if not state.active:
                    item.setForeground(QtGui.QColor("#777777"))
                self.table.setItem(row, column, item)
        self.table.viewport().update()

    def _render_pit_status(self) -> None:
        groups = self.service.pit_status_groups()
        for row, label in enumerate(("Fired-up", "Queue", "On-track")):
            self.pit_table.setItem(row, 0, QtWidgets.QTableWidgetItem(label))
            self.pit_table.setItem(
                row,
                1,
                QtWidgets.QTableWidgetItem(", ".join(groups[label])),
            )

    def closeEvent(self, event) -> None:
        self.timer.stop()
        self.service.stop()
        event.accept()


def main() -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    window = QualiWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
