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
from ..common.windows import resolve_effective_windows
from ..common.session_info import resolve_session_identity
from ..analysis.service import OfflineAnalysisService
from ..operational_inputs import (
    DEFAULT_EVENT_CONFIG_PATH,
    load_operational_race_inputs,
    save_operational_pit_losses,
)
from .track_map_widget import TrackMapWidget
from .race_info import RaceControlEventsWidget


WORKSPACE_DATA_ROOT = Path(__file__).resolve().parents[4] / "woData"
PLANNER_VISUAL_CHANGES = frozenset(
    {"drivers", "session", "timing", "tyres", "track_status", "lap_count"}
)


def _is_final_display(session_status: str | None, connection_statuses) -> bool:
    """Terminal feed status or completed replay; Aborted remains resumable."""
    return str(session_status or "").strip().lower() in {"finished", "finalised", "ends"} or any(
        status.state == "disconnected" and status.message == "Replay completed"
        for status in connection_statuses
    )


def _metadata_text(value, *keys: str) -> str | None:
    if isinstance(value, Mapping):
        for key in keys:
            text = str(value.get(key) or "").strip()
            if text:
                return text
        return None
    text = str(value or "").strip()
    return text or None


def _stable_identity_conflicts(left: object, right: object) -> bool:
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return False

    def normalized(value: object) -> str:
        return " ".join(str(value or "").casefold().split())

    for key in ("circuit_id", "circuit"):
        left_value = normalized(left.get(key))
        right_value = normalized(right.get(key))
        if left_value and right_value and left_value != right_value:
            return True
    left_pair = (normalized(left.get("country")), normalized(left.get("location")))
    right_pair = (normalized(right.get("country")), normalized(right.get("location")))
    return bool(all((*left_pair, *right_pair)) and left_pair != right_pair)


class TrackMapWindow(QtWidgets.QMainWindow):
    """A reusable, non-modal home for the same live track-map presentation."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("woPlanner Track Map")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.resize(820, 620)
        self.track_map = TrackMapWidget()
        self.setCentralWidget(self.track_map)

    def closeEvent(self, event) -> None:
        # Hiding keeps the current snapshot, selected driver, and effective
        # descriptors intact for the next invocation of Track Map.
        self.hide()
        event.ignore()


class LivePlannerWindow(QtWidgets.QMainWindow):
    def __init__(
        self,
        service: LivePlannerService | None = None,
        *,
        event_config_path: str | Path = DEFAULT_EVENT_CONFIG_PATH,
        operational_history_data_root: str | Path | None = None,
        event_resolver=None,
    ) -> None:
        super().__init__()
        self.service = service or LivePlannerService()
        self.event_config_path = Path(event_config_path)
        self.operational_history_data_root = operational_history_data_root
        self.event_resolver = event_resolver
        self._event_identity: dict[str, str] = {}
        self._event_identity_event_key: tuple[int, int] | None = None
        self._current_operational_inputs = None
        self._driver_signature: tuple = ()
        self._metadata_warning_shown = False
        self._session_metadata_signature: tuple[object, ...] | None = None
        self._active_source_mode: str | None = None
        self._final_display = False
        self.custom_ranges = False
        self._applying_plot_ranges = False
        self.strategy_prediction_window = None
        self._strategy_resume_context = None
        self._profile_redraw_count = 0
        self._profile_redraw_seconds = 0.0
        self._profile_last_log = time.monotonic()
        self._tyre_event_signature: tuple = ()
        self._track_map_signature: tuple | None = None
        self.track_map_window: TrackMapWindow | None = None
        self.setWindowTitle("woPlanner Live")
        self.resize(1200, 760)
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        controls = QtWidgets.QVBoxLayout()
        connection_controls = QtWidgets.QHBoxLayout()
        presentation_controls = QtWidgets.QHBoxLayout()
        self.year_input = QtWidgets.QSpinBox()
        self.year_input.setRange(1950, 2100)
        self.year_input.setValue(datetime.now().year)
        self.round_input = QtWidgets.QSpinBox()
        self.round_input.setRange(1, 30)
        self._effective_event_key = (
            self.year_input.value(), self.round_input.value()
        )
        self.session_input = QtWidgets.QLineEdit("R")
        self.session_input.setMaximumWidth(50)
        self.connect_button = QtWidgets.QPushButton("Connect")
        self.disconnect_button = QtWidgets.QPushButton("Disconnect")
        self.strategy_prediction_button = QtWidgets.QPushButton("Strategy")
        self.track_map_button = QtWidgets.QPushButton("Track Map")
        self.track_map_collapse_button = QtWidgets.QPushButton("Embedded Map")
        self.track_map_collapse_button.setCheckable(True)
        self.track_map_collapse_button.setChecked(True)
        self.track_map_collapse_button.setToolTip(
            "Show or hide the embedded Track Map"
        )
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
        self.normal_pit_loss = self._pit_loss_input()
        self.sc_pit_loss = self._pit_loss_input()
        self.vsc_pit_loss = self._pit_loss_input()
        self.apply_pit_loss_button = QtWidgets.QPushButton("Apply pit losses")
        self.pit_loss_status = QtWidgets.QLabel("Event inputs: not loaded")
        for label, widget in (
            ("Year", self.year_input), ("Round", self.round_input),
            ("Session", self.session_input), ("Replay", self.replay_path),
        ):
            connection_controls.addWidget(QtWidgets.QLabel(label))
            connection_controls.addWidget(widget)
        connection_controls.addWidget(self.replay_browse_button)
        connection_controls.addWidget(self.replay_directory_button)
        connection_controls.addWidget(QtWidgets.QLabel("Speed"))
        connection_controls.addWidget(self.replay_speed)
        connection_controls.addWidget(self.connect_button)
        connection_controls.addWidget(self.replay_button)
        connection_controls.addWidget(self.disconnect_button)
        connection_controls.addStretch(1)
        for label, widget in (
            ("Reference", self.reference_driver), ("Mode", self.reference_mode),
            ("Width", self.line_width),
        ):
            presentation_controls.addWidget(QtWidgets.QLabel(label))
            presentation_controls.addWidget(widget)
        presentation_controls.addWidget(self.strategy_prediction_button)
        presentation_controls.addWidget(self.track_map_button)
        presentation_controls.addWidget(self.track_map_collapse_button)
        presentation_controls.addStretch(1)
        controls.addLayout(connection_controls)
        controls.addLayout(presentation_controls)
        controls_widget = QtWidgets.QWidget()
        controls_widget.setLayout(controls)
        # The two functional rows stay readable at normal widths.  At a very
        # narrow width the horizontal scroll bar preserves every control rather
        # than shrinking fonts or clipping actions.
        self.controls_scroll = QtWidgets.QScrollArea()
        self.controls_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.controls_scroll.setWidgetResizable(False)
        self.controls_scroll.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.controls_scroll.setWidget(controls_widget)
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
        self.track_map = TrackMapWidget()
        visual_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        visual_splitter.addWidget(self.relative_plot)
        visual_splitter.addWidget(self.track_map)
        visual_splitter.setStretchFactor(0, 3)
        visual_splitter.setStretchFactor(1, 2)
        layout.addWidget(self.controls_scroll)
        layout.addLayout(status_row)
        self.tabs = QtWidgets.QTabWidget()
        live_tab = QtWidgets.QWidget()
        live_layout = QtWidgets.QVBoxLayout(live_tab)
        live_layout.setContentsMargins(0, 0, 0, 0)
        live_layout.addWidget(self.axis_controls)
        live_layout.addWidget(visual_splitter, stretch=1)
        self.tabs.addTab(live_tab, "Live")
        self.race_info_tab = QtWidgets.QWidget()
        race_info_layout = QtWidgets.QVBoxLayout(self.race_info_tab)
        self.race_control_section = QtWidgets.QGroupBox("Race Control Events")
        events_layout = QtWidgets.QVBoxLayout(self.race_control_section)
        self.race_control_events = RaceControlEventsWidget()
        events_layout.addWidget(self.race_control_events)
        race_info_layout.addWidget(self.race_control_section, stretch=1)
        self.pit_delta_section = QtWidgets.QGroupBox("Pit Delta")
        pit_layout = QtWidgets.QVBoxLayout(self.pit_delta_section)
        pit_controls = QtWidgets.QHBoxLayout()
        for label, widget in (
            ("Green", self.normal_pit_loss), ("SC", self.sc_pit_loss),
            ("VSC", self.vsc_pit_loss),
        ):
            pit_controls.addWidget(QtWidgets.QLabel(label))
            pit_controls.addWidget(widget)
        pit_controls.addWidget(self.apply_pit_loss_button)
        pit_controls.addStretch()
        pit_layout.addLayout(pit_controls)
        pit_layout.addWidget(self.pit_loss_status)
        pit_layout.addWidget(QtWidgets.QLabel(
            "Select a driver in Live to display the existing pit windows on the plot and track map."
        ))
        race_info_layout.addWidget(self.pit_delta_section)
        self.tabs.addTab(self.race_info_tab, "Race Info")
        layout.addWidget(self.tabs, stretch=1)
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
        self.track_map.driver_selected.connect(self._select_driver)
        self.normal_pit_loss.valueChanged.connect(self._update_pit_window)
        self.sc_pit_loss.valueChanged.connect(self._update_pit_window)
        self.vsc_pit_loss.valueChanged.connect(self._update_pit_window)
        self.year_input.valueChanged.connect(self._sync_operational_inputs_for_event)
        self.round_input.valueChanged.connect(self._sync_operational_inputs_for_event)
        self.apply_pit_loss_button.clicked.connect(self._apply_operational_pit_losses)
        self.apply_range_button.clicked.connect(self._apply_custom_ranges)
        self.auto_range_button.clicked.connect(self._reset_auto_ranges)
        self.strategy_prediction_button.clicked.connect(
            self._show_strategy_prediction_window
        )
        self.track_map_button.clicked.connect(self._show_track_map_window)
        self.track_map_collapse_button.toggled.connect(self._set_embedded_map_visible)
        self.tabs.currentChanged.connect(self._refresh_active_tab)
        self._load_operational_inputs()

    @staticmethod
    def _pit_loss_input() -> QtWidgets.QDoubleSpinBox:
        field = QtWidgets.QDoubleSpinBox()
        field.setRange(-1.0, 120.0)
        field.setDecimals(1)
        field.setSingleStep(0.1)
        field.setSpecialValueText("Unresolved")
        field.setSuffix(" s")
        field.setValue(-1.0)
        return field

    @staticmethod
    def _pit_loss_value(field: QtWidgets.QDoubleSpinBox) -> float | None:
        value = field.value()
        return None if value < 0.0 else float(value)

    def _load_operational_inputs(self) -> None:
        try:
            inputs = load_operational_race_inputs(self.event_config_path)
        except Exception as exc:
            self.pit_loss_status.setText(f"Event inputs unavailable: {exc}")
            return
        self._current_operational_inputs = inputs
        for field, value in (
            (self.normal_pit_loss, inputs.green_pit_loss),
            (self.sc_pit_loss, inputs.sc_pit_loss),
            (self.vsc_pit_loss, inputs.vsc_pit_loss),
        ):
            field.blockSignals(True)
            field.setValue(-1.0 if value is None else value)
            field.blockSignals(False)
        if inputs.season is not None:
            self.year_input.setValue(inputs.season)
        if inputs.round_number is not None:
            self.round_input.setValue(inputs.round_number)
        legacy = (
            f"; legacy combined SC/VSC {inputs.legacy_sc_vsc_pit_loss:.1f} s is context only"
            if inputs.legacy_sc_vsc_pit_loss is not None
            else ""
        )
        self.pit_loss_status.setText(
            f"Event inputs: {inputs.event_id or 'identity unresolved'}, "
            f"race laps {inputs.total_laps if inputs.total_laps is not None else 'unresolved'}"
            f"{legacy}"
        )
        self._update_pit_window()

    def _sync_operational_inputs_for_event(self, *_args) -> None:
        requested_key = (self.year_input.value(), self.round_input.value())
        if self._effective_event_key != requested_key:
            self._event_identity = {}
            self._event_identity_event_key = None
            self._effective_event_key = requested_key
        inputs = self._current_operational_inputs
        if inputs is None:
            return
        if (
            inputs.season == self.year_input.value()
            and inputs.round_number == self.round_input.value()
        ):
            values = (
                inputs.green_pit_loss, inputs.sc_pit_loss, inputs.vsc_pit_loss,
            )
            message = f"Event inputs: {inputs.event_id}"
        else:
            values = (None, None, None)
            message = (
                f"Event inputs unresolved: config is {inputs.event_id}, Planner is "
                f"{self.year_input.value():04d}-{self.round_input.value():02d}"
            )
        for field, value in zip(
            (self.normal_pit_loss, self.sc_pit_loss, self.vsc_pit_loss), values
        ):
            field.blockSignals(True)
            field.setValue(-1.0 if value is None else value)
            field.blockSignals(False)
        self.pit_loss_status.setText(message)
        self._update_pit_window()

    def _apply_operational_pit_losses(self) -> None:
        try:
            inputs = save_operational_pit_losses(
                self.event_config_path,
                green=self._pit_loss_value(self.normal_pit_loss),
                sc=self._pit_loss_value(self.sc_pit_loss),
                vsc=self._pit_loss_value(self.vsc_pit_loss),
                season=self.year_input.value(),
                round_number=self.round_input.value(),
                event_identity=self._event_identity,
                event_identity_event_key=self._event_identity_event_key,
                history_data_root=self.operational_history_data_root,
                event_resolver=self.event_resolver,
            )
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Current-event pit loss", str(exc))
            return
        self._current_operational_inputs = inputs
        self.pit_loss_status.setText(
            f"Saved manual event inputs for {inputs.event_id or 'current event'}"
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
                self.strategy_prediction_window._live_context_key = self._strategy_context_key(
                    self.strategy_prediction_window
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
                    self.strategy_prediction_window._live_context_key = self._strategy_context_key(
                        self.strategy_prediction_window
                    )
                except RuntimeError:
                    self.strategy_prediction_window = None
        except Exception as exc:
            self._show_start_error(exc)

    def _prepare_start(self) -> None:
        self._strategy_resume_context = None
        self._active_source_mode = None
        self._final_display = False
        self.connect_button.setEnabled(False)
        self.replay_button.setEnabled(False)
        self._driver_signature = ()
        self._tyre_event_signature = ()
        self._track_map_signature = None
        self.track_map.clear()
        self.race_control_events.clear()
        if self.track_map_window is not None:
            self.track_map_window.track_map.clear()
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
        final_display = _is_final_display(update.snapshot.session_status, update.statuses)
        presentation_changed = final_display and not self._final_display
        self._final_display |= final_display
        for status in update.statuses:
            self.connection_status.setText(status.message)
            if status.state in {"disconnected", "error"}:
                self.connect_button.setEnabled(True)
                self.replay_button.setEnabled(True)
        snapshot = update.snapshot
        # Recovery restores the store before queue draining, so there may be
        # no new topic notification. The widget skips unchanged snapshots.
        self.race_control_events.set_events(snapshot.race_control_events)
        if (
            self.strategy_prediction_window is not None
            and update.changes.intersection({"drivers", "timing", "tyres", "lap_count", "track_status", "session"})
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
        if presentation_changed or update.changes.intersection(PLANNER_VISUAL_CHANGES):
            self._redraw()
        track_snapshot = self.service.latest_track_map_snapshot()
        track_signature = (
            track_snapshot.generation,
            track_snapshot.position_visual_version,
            signature,
        )
        if track_signature != self._track_map_signature:
            if self.track_map.isVisible():
                self.track_map.update_snapshot(track_snapshot, drivers)
            if (
                self.track_map_window is not None
                and self.track_map_window.isVisible()
            ):
                self.track_map_window.track_map.update_snapshot(track_snapshot, drivers)
            self._track_map_signature = track_signature

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
        incoming_identity = {}
        if identity.event_name:
            incoming_identity["event_name"] = identity.event_name
            self.setWindowTitle(f"woPlanner Live — {identity.event_name}")
        for key, value in (
            ("official_event_name", _metadata_text(meeting.get("OfficialName"))),
            ("country", _metadata_text(meeting.get("Country"), "Name", "Code")),
            ("location", _metadata_text(meeting.get("Location"), "Name")),
            ("circuit", _metadata_text(meeting.get("Circuit"), "ShortName", "Name")),
            ("circuit_id", _metadata_text(meeting.get("Circuit"), "Key")),
        ):
            if value:
                incoming_identity[key] = value
        metadata_key = (
            (identity.year, identity.round_number)
            if identity.year is not None and identity.round_number is not None
            else None
        )
        if (
            metadata_key is not None
            and self._event_identity_event_key == metadata_key
            and not _stable_identity_conflicts(self._event_identity, incoming_identity)
        ):
            self._event_identity.update(incoming_identity)
        else:
            conflict = (
                metadata_key is not None
                and self._event_identity_event_key == metadata_key
                and _stable_identity_conflicts(self._event_identity, incoming_identity)
            )
            self._event_identity = incoming_identity
            self._event_identity_event_key = None if conflict else metadata_key

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
            include_provisional=not self._final_display,
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
        self.track_map.set_selected_driver(driver)
        if self.track_map_window is not None:
            self.track_map_window.track_map.set_selected_driver(driver)
        self._update_pit_window()

    def _clear_selection(self) -> None:
        self.relative_plot.set_selected_driver(None)
        self.track_map.set_selected_driver(None)
        if self.track_map_window is not None:
            self.track_map_window.track_map.set_selected_driver(None)
        self._update_pit_window()

    def _update_pit_window(self) -> None:
        windows = resolve_effective_windows(
            green_pit_loss_s=self._pit_loss_value(self.normal_pit_loss),
            sc_pit_loss_s=self._pit_loss_value(self.sc_pit_loss),
            vsc_pit_loss_s=self._pit_loss_value(self.vsc_pit_loss),
        )
        self.pit_window.update(
            self.service._last_plot_data,
            self.relative_plot.selected_driver,
            normal_pit_loss_s=self._pit_loss_value(self.normal_pit_loss),
            sc_pit_loss_s=self._pit_loss_value(self.sc_pit_loss),
            vsc_pit_loss_s=self._pit_loss_value(self.vsc_pit_loss),
            x_column="x_lap",
        )
        self.track_map.set_windows(windows)
        if self.track_map_window is not None:
            self.track_map_window.track_map.set_windows(windows)

    def _set_embedded_map_visible(self, visible: bool) -> None:
        self.track_map.setVisible(visible)
        if visible:
            self.track_map.update_snapshot(
                self.service.latest_track_map_snapshot(),
                self.service.available_drivers(),
            )

    def _refresh_active_tab(self, index: int) -> None:
        if index == 0:
            self._set_embedded_map_visible(self.track_map_collapse_button.isChecked())
        else:
            self.race_control_events.table.resizeRowsToContents()

    def _show_track_map_window(self) -> None:
        if self.track_map_window is None:
            self.track_map_window = TrackMapWindow(self)
            self.track_map_window.track_map.driver_selected.connect(self._select_driver)
        # Re-synchronise on every open. Hidden windows deliberately skip replay
        # redraws, but never own a separate snapshot, selection or window state.
        self.track_map_window.show()
        self.track_map_window.track_map.update_snapshot(
            self.service.latest_track_map_snapshot(),
            self.service.available_drivers(),
        )
        self.track_map_window.track_map.set_windows(self.track_map._windows)
        self.track_map_window.track_map.set_selected_driver(
            self.track_map.selected_driver
        )
        self.track_map_window.raise_()
        self.track_map_window.activateWindow()

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

    def _strategy_context_key(self, window=None) -> tuple:
        return (
            window.year if window is not None else self.year_input.value(),
            window.round_number if window is not None else self.round_input.value(),
            self._active_source_mode, self._strategy_state_directory(),
        )

    def _show_strategy_prediction_window(self) -> None:
        from ..analysis.gui import StrategyPredictionMode, StrategyPredictionWindow

        if self.strategy_prediction_window is not None:
            try:
                if self.strategy_prediction_window.isVisible():
                    self.strategy_prediction_window.raise_()
                    self.strategy_prediction_window.activateWindow()
                    return
            except RuntimeError:
                self.strategy_prediction_window = None
        context_key = self._strategy_context_key()
        resume = self._strategy_resume_context
        resume_options = (
            {"service": resume[1], "initial_model_summary": resume[2]}
            if resume is not None and resume[0] == context_key else {}
        )
        self.strategy_prediction_window = StrategyPredictionWindow(
            year=self.year_input.value(),
            round_number=self.round_input.value(),
            mode=(
                StrategyPredictionMode.HISTORICAL_WHAT_IF
                if self._active_source_mode == "replay"
                else StrategyPredictionMode.CURRENT_EVENT
            ),
            state_directory=self._strategy_state_directory(),
            persistence_mode=(
                "temporary" if self._active_source_mode == "replay" else "durable"
            ),
            auto_save_history=self._active_source_mode == "live",
            team_scope_enabled=True,
            live_operational_controls=True,
            telemetry_worker=self.service.telemetry_worker,
            **resume_options,
            parent=self,
        )
        # Closing a dialog does not restart the race/replay. Keep its current
        # model available before saved Team selection is restored on reopening.
        self.strategy_prediction_window._live_context_key = context_key
        self.strategy_prediction_window.session_closing.connect(
            lambda service, summary, dialog=self.strategy_prediction_window: setattr(
                self, "_strategy_resume_context", (dialog._live_context_key, service, summary)
            )
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
        if self.track_map_window is not None:
            self.track_map_window.hide()
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
