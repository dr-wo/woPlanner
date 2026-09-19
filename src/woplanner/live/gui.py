from __future__ import annotations

import sys
import logging
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

import pandas as pd
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from wodata.live import LiveConfig, ReplayConfig, recover_replay_directory

from .service import (
    REFERENCE_MODE_AVERAGED_PACE,
    REFERENCE_MODE_LABELS,
    LivePlannerService,
)
from ..common.plot_widget import RelativeDeltaPlotWidget
from ..common.axis_controls import AxisRangeControls
from ..common.pit_window import PitWindowOverlay
from ..common.session_info import resolve_session_identity
from ..analysis.service import OfflineAnalysisService


WORKSPACE_DATA_ROOT = Path(__file__).resolve().parents[4] / "woData"
PLANNER_VISUAL_CHANGES = frozenset(
    {"drivers", "session", "timing", "tyres", "track_status", "lap_count"}
)


class LivePlannerWindow(QtWidgets.QMainWindow):
    def __init__(self, service: LivePlannerService | None = None) -> None:
        super().__init__()
        self.service = service or LivePlannerService()
        self._driver_signature: tuple = ()
        self._metadata_warning_shown = False
        self._session_metadata_signature: tuple[object, ...] | None = None
        self._active_source_mode: str | None = None
        self.custom_ranges = False
        self._applying_plot_ranges = False
        self.strategy_prediction_window = None
        self._profile_redraw_count = 0
        self._profile_redraw_seconds = 0.0
        self._profile_last_log = time.monotonic()
        self._tyre_event_signature: tuple = ()
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
        self.strategy_prediction_button = QtWidgets.QPushButton("Strategy Prediction")
        self.replay_path = QtWidgets.QLineEdit()
        self.replay_path.setPlaceholderText("Replay file")
        self.replay_browse_button = QtWidgets.QPushButton("Browse")
        self.replay_directory_button = QtWidgets.QPushButton("Directory")
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
        self.normal_pit_loss = QtWidgets.QDoubleSpinBox()
        self.normal_pit_loss.setRange(0.0, 120.0)
        self.normal_pit_loss.setValue(20.0)
        self.normal_pit_loss.setSuffix(" s")
        self.sc_pit_loss = QtWidgets.QDoubleSpinBox()
        self.sc_pit_loss.setRange(0.0, 120.0)
        self.sc_pit_loss.setValue(10.0)
        self.sc_pit_loss.setSuffix(" s")
        for label, widget in (
            ("Year", self.year_input), ("Round", self.round_input),
            ("Session", self.session_input), ("Replay", self.replay_path),
            ("Speed", self.replay_speed), ("Reference", self.reference_driver),
            ("Mode", self.reference_mode), ("Width", self.line_width),
            ("Pit", self.normal_pit_loss), ("SC/VSC", self.sc_pit_loss),
        ):
            controls.addWidget(QtWidgets.QLabel(label))
            controls.addWidget(widget)
        controls.addWidget(self.replay_browse_button)
        controls.addWidget(self.replay_directory_button)
        controls.addWidget(self.connect_button)
        controls.addWidget(self.replay_button)
        controls.addWidget(self.disconnect_button)
        controls.addWidget(self.strategy_prediction_button)
        self.axis_controls = AxisRangeControls()
        self.x_min_input = self.axis_controls.x_min_input
        self.x_max_input = self.axis_controls.x_max_input
        self.y_min_input = self.axis_controls.y_min_input
        self.y_max_input = self.axis_controls.y_max_input
        self.apply_range_button = self.axis_controls.apply_button
        self.auto_range_button = self.axis_controls.auto_button
        status_row = QtWidgets.QHBoxLayout()
        self.connection_status = QtWidgets.QLabel("Idle")
        self.last_message_status = QtWidgets.QLabel("Last message: —")
        self.session_status = QtWidgets.QLabel("Session: —")
        self.plot_progress_status = QtWidgets.QLabel("Plot: —")
        status_row.addWidget(self.connection_status)
        status_row.addStretch(1)
        status_row.addWidget(self.session_status)
        status_row.addWidget(self.plot_progress_status)
        status_row.addWidget(self.last_message_status)
        self.relative_plot = RelativeDeltaPlotWidget()
        self.relative_plot.set_hover_enabled(True)
        self.relative_plot.plot.getViewBox().sigRangeChanged.connect(
            self._plot_range_changed
        )
        self.pit_window = PitWindowOverlay(self.relative_plot.plot)
        layout.addLayout(controls)
        layout.addWidget(self.axis_controls)
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
        self.replay_directory_button.clicked.connect(self._browse_replay_directory)
        self.reference_driver.currentTextChanged.connect(self._redraw)
        self.reference_mode.currentTextChanged.connect(self._redraw)
        self.line_width.valueChanged.connect(self._redraw)
        self.relative_plot.driver_selected.connect(self._select_driver)
        self.relative_plot.selection_cleared.connect(self._clear_selection)
        self.normal_pit_loss.valueChanged.connect(self._update_pit_window)
        self.sc_pit_loss.valueChanged.connect(self._update_pit_window)
        self.apply_range_button.clicked.connect(self._apply_custom_ranges)
        self.auto_range_button.clicked.connect(self._reset_auto_ranges)
        self.strategy_prediction_button.clicked.connect(
            self._show_strategy_prediction_window
        )

    def _connect(self) -> None:
        session = self.session_input.text().strip().upper()
        if not session:
            QtWidgets.QMessageBox.warning(self, "Live timing", "Session is required.")
            return
        self._prepare_start()
        try:
            self.service.start_live(
                LiveConfig(
                    self.year_input.value(), self.round_input.value(), session,
                    data_root=(WORKSPACE_DATA_ROOT if WORKSPACE_DATA_ROOT.is_dir() else None),
                )
            )
            self._active_source_mode = "live"
            if self.strategy_prediction_window is not None:
                self.strategy_prediction_window.configure_session_context(
                    persistence_mode="durable",
                    state_directory=self._strategy_state_directory(),
                    auto_save_history=True,
                )
        except Exception as exc:
            self._show_start_error(exc)

    def _replay(self) -> None:
        path = Path(self.replay_path.text().strip()).expanduser()
        if not path.exists() or not (path.is_file() or path.is_dir()):
            QtWidgets.QMessageBox.warning(self, "Replay", "Choose an existing replay file or directory.")
            return
        try:
            if path.is_dir():
                recovered = recover_replay_directory(
                    path, year=self.year_input.value(),
                    round_number=self.round_input.value(), session=self.session_input.text(),
                )
                path = recovered.path
                self.replay_path.setText(str(path))
            config_service = OfflineAnalysisService(
                WORKSPACE_DATA_ROOT if WORKSPACE_DATA_ROOT.is_dir() else None
            )
            _config, config_source = config_service.resolve_live_model_config(
                self.year_input.value(), self.round_input.value(),
                replay_directory=path.parent,
            )
            logging.getLogger(__name__).info(
                "Replay model configuration resolved from %s", config_source
            )
            self._prepare_start()
            self.service.start_replay(
                ReplayConfig(path, float(self.replay_speed.currentData()))
            )
            self._active_source_mode = "replay"
            if self.strategy_prediction_window is not None:
                try:
                    self.strategy_prediction_window.configure_session_context(
                        persistence_mode="temporary",
                        state_directory=self._strategy_state_directory(),
                        auto_save_history=False,
                    )
                except RuntimeError:
                    self.strategy_prediction_window = None
        except Exception as exc:
            self._show_start_error(exc)

    def _prepare_start(self) -> None:
        self._active_source_mode = None
        self.connect_button.setEnabled(False)
        self.replay_button.setEnabled(False)
        self._driver_signature = ()
        self._tyre_event_signature = ()
        self._metadata_warning_shown = False
        self._session_metadata_signature = None
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

    def _browse_replay_directory(self) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Open live timing replay directory", ""
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
        if (
            self.strategy_prediction_window is not None
            and update.changes.intersection({"drivers", "timing", "tyres", "lap_count"})
        ):
            try:
                if self.strategy_prediction_window.isVisible():
                    self.strategy_prediction_window.consume_live_snapshot(snapshot)
            except RuntimeError:
                self.strategy_prediction_window = None
        self._sync_session_context(snapshot.session_info)
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
            update = type(update)(
                update.snapshot,
                update.statuses,
                True,
                update.changes.union({"drivers"}),
            )
        if update.changes.intersection(PLANNER_VISUAL_CHANGES):
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

    def _sync_session_context(self, session_info) -> None:
        if not session_info:
            return
        meeting = session_info.get("Meeting", {})
        meeting = meeting if isinstance(meeting, Mapping) else {}
        signature = (
            session_info.get("StartDate"),
            session_info.get("Name"),
            session_info.get("Type"),
            meeting.get("Name"),
            meeting.get("Number"),
            meeting.get("RoundNumber"),
        )
        if signature == self._session_metadata_signature:
            return
        self._session_metadata_signature = signature
        identity = resolve_session_identity(
            session_info, fallback_year=self.year_input.value()
        )
        if identity.year is not None:
            self.year_input.setValue(identity.year)
        if identity.round_number is not None:
            self.round_input.setValue(identity.round_number)
        if identity.event_name:
            self.setWindowTitle(f"woPlanner Live — {identity.event_name}")

    def _redraw(self) -> None:
        started = time.perf_counter()
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
        calculation_started = time.perf_counter()
        plot_data = self.service.build_plot_data(
            reference,
            reference_mode=str(
                self.reference_mode.currentData() or REFERENCE_MODE_AVERAGED_PACE
            ),
        )
        calculation_seconds = time.perf_counter() - calculation_started
        if not self.custom_ranges:
            self._update_range_controls_from_data(plot_data)
        widget_started = time.perf_counter()
        self.relative_plot.set_plot_data(plot_data)
        widget_seconds = time.perf_counter() - widget_started
        leader_laps = [lap.lap_number for lap in self.service.snapshot.completed_laps if lap.position == 1]
        all_laps = [lap.lap_number for lap in self.service.snapshot.completed_laps]
        replay_lap = max(leader_laps or all_laps or [0])
        data_lap = int(plot_data["lap"].max()) if not plot_data.empty else 0
        displayed_lap = data_lap
        timing_unavailable = bool(
            not plot_data.empty
            and "timing_available" in plot_data
            and not plot_data["timing_available"].fillna(False).any()
        )
        progress_prefix = "Timing unavailable" if timing_unavailable else "Plot"
        self.plot_progress_status.setText(
            f"{progress_prefix}: replay {replay_lap} / data {data_lap} / "
            f"displayed {displayed_lap}"
        )
        signature = (replay_lap, data_lap, displayed_lap)
        if signature != getattr(self, "_plot_progress_signature", None):
            logger = logging.getLogger(__name__)
            log = logger.warning if replay_lap != data_lap else logger.debug
            log(
                "Cumulative Delta progress: replay leader lap=%d, data lap=%d, displayed lap=%d",
                replay_lap, data_lap, displayed_lap,
            )
            self._plot_progress_signature = signature
        if not plot_data.empty:
            self._apply_plot_ranges()
        self._update_pit_window()
        tyre_started = time.perf_counter()
        tyre_events = self.service.build_tyre_event_data()
        signature_columns = [
            column
            for column in (
                "driver", "lap", "compound", "tyre_age_label",
                "scenario_id", "is_simulated",
            )
            if column in tyre_events.columns
        ]
        signature = tuple(
            tyre_events[signature_columns]
            .fillna("").itertuples(index=False, name=None)
        ) if not tyre_events.empty else ()
        # set_plot_data() rebuilds the managed plot lifecycle and therefore
        # clears every overlay item. Reconcile tyre annotations from the same
        # authoritative historical plot data on every redraw, even when their
        # data signature is unchanged.
        self.relative_plot.set_tyre_event_data(tyre_events)
        self._tyre_event_signature = signature
        tyre_seconds = time.perf_counter() - tyre_started
        self._profile_redraw_count += 1
        self._profile_redraw_seconds += time.perf_counter() - started
        now = time.monotonic()
        if now - self._profile_last_log >= 5.0:
            logging.getLogger(__name__).debug(
                "Live plot profile: redraws=%d total=%.1fms average=%.1fms "
                "latest_calculation=%.1fms latest_widget=%.1fms latest_tyre=%.1fms",
                self._profile_redraw_count,
                self._profile_redraw_seconds * 1000.0,
                (
                    self._profile_redraw_seconds * 1000.0
                    / max(self._profile_redraw_count, 1)
                ),
                calculation_seconds * 1000.0,
                widget_seconds * 1000.0,
                tyre_seconds * 1000.0,
            )
            self._profile_redraw_count = 0
            self._profile_redraw_seconds = 0.0
            self._profile_last_log = now

    def _select_driver(self, driver: str) -> None:
        self.relative_plot.set_selected_driver(driver)
        self._update_pit_window()

    def _clear_selection(self) -> None:
        self.relative_plot.set_selected_driver(None)
        self._update_pit_window()

    def _update_pit_window(self) -> None:
        self.pit_window.update(
            self.service._last_plot_data,
            self.relative_plot.selected_driver,
            normal_pit_loss_s=self.normal_pit_loss.value(),
            sc_vsc_pit_loss_s=self.sc_pit_loss.value(),
            x_column="x_lap",
        )

    def _update_range_controls_from_data(self, plot_data: pd.DataFrame) -> None:
        self.axis_controls.update_from_data(plot_data)

    def _apply_custom_ranges(self) -> None:
        self.custom_ranges = True
        self.axis_controls.custom_ranges = True
        self._apply_plot_ranges()

    def _reset_auto_ranges(self) -> None:
        self.custom_ranges = False
        self.axis_controls.custom_ranges = False
        if self.service._last_plot_data is not None:
            self._update_range_controls_from_data(self.service._last_plot_data)
        self._apply_plot_ranges()

    def _apply_plot_ranges(self) -> None:
        lap_min, lap_max = self.axis_controls.lap_range()
        y_min, y_max = self.axis_controls.y_range()
        self._applying_plot_ranges = True
        try:
            self.relative_plot.plot.setXRange(
                lap_min - 1.0 / 3.0, lap_max + 1.0 / 3.0, padding=0
            )
            self.relative_plot.plot.setYRange(y_min, y_max, padding=0)
        finally:
            self._applying_plot_ranges = False

    def _plot_range_changed(self, _view_box, ranges, _changed=None) -> None:
        if self._applying_plot_ranges:
            return
        x_range, y_range = ranges
        # The live plot deliberately renders one third of a lap beyond the
        # entered lap limits, so invert that transform before storing it.
        changed = _changed if _changed is not None else (True, True)
        if changed[0]:
            self.x_min_input.setValue(float(x_range[0]) + 1.0 / 3.0)
            self.x_max_input.setValue(float(x_range[1]) - 1.0 / 3.0)
        if changed[1]:
            self.y_min_input.setValue(float(y_range[0]))
            self.y_max_input.setValue(float(y_range[1]))
        self.axis_controls.custom_ranges = True
        self.custom_ranges = True

    def _disconnect(self) -> None:
        self.timer.stop()
        self.service.stop()
        self.connect_button.setEnabled(True)
        self.replay_button.setEnabled(True)
        self.connection_status.setText("Disconnected by user")

    def _show_strategy_prediction_window(self) -> None:
        from ..analysis.gui import StrategyPredictionWindow

        if self.strategy_prediction_window is not None:
            try:
                if self.strategy_prediction_window.isVisible():
                    self.strategy_prediction_window.raise_()
                    self.strategy_prediction_window.activateWindow()
                    return
            except RuntimeError:
                self.strategy_prediction_window = None
        self.strategy_prediction_window = StrategyPredictionWindow(
            year=self.year_input.value(),
            round_number=self.round_input.value(),
            state_directory=self._strategy_state_directory(),
            persistence_mode=(
                "temporary" if self._active_source_mode == "replay" else "durable"
            ),
            auto_save_history=self._active_source_mode == "live",
            telemetry_worker=self.service.telemetry_worker,
            parent=self,
        )
        self.strategy_prediction_window.destroyed.connect(
            lambda: setattr(self, "strategy_prediction_window", None)
        )
        self.strategy_prediction_window.show()
        self.strategy_prediction_window.consume_live_snapshot(self.service.snapshot)

    def _strategy_state_directory(self) -> Path | None:
        if self._active_source_mode == "replay":
            replay = Path(self.replay_path.text().strip()).expanduser()
            return replay.parent if replay.name else None
        if self._active_source_mode == "live" and self.service.recording_path is not None:
            return self.service.recording_path.parent
        return None

    def closeEvent(self, event) -> None:
        if self.strategy_prediction_window is not None:
            try:
                self.strategy_prediction_window.close()
            except RuntimeError:
                pass
        self.timer.stop()
        self.service.shutdown()
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
