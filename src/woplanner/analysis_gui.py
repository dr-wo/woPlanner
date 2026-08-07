from __future__ import annotations

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

from wodata.contracts import StrategySearchState
from wodata.artifacts import weekend_model_root

from .analysis_service import OfflineAnalysisService


COMPOUND_COLOURS = {"SOFT": "#e10600", "MEDIUM": "#f2c500", "HARD": "#d5d7da"}
WORKSPACE_DATA_ROOT = Path(__file__).resolve().parents[3] / "woData"
LIVE_DEFAULT_SAMPLE_COUNT = 5000


def history_values_at_lap(rows, leader_lap: int) -> dict[str, dict[str, float]]:
    """Return aggregate tyre readings indexed by parameter and compound."""
    values: dict[str, dict[str, float]] = {
        "degradation": {},
        "compound_delta": {},
    }
    for row in rows:
        if str(row.get("source_scope", "")).upper() != "AGGREGATE":
            continue
        try:
            row_lap = int(float(row.get("as_of_leader_lap") or 0))
            value = float(row["median"])
        except (KeyError, TypeError, ValueError):
            continue
        parameter = str(row.get("parameter") or "")
        compound = str(row.get("compound") or "").upper()
        if row_lap == int(leader_lap) and parameter in values and compound:
            values[parameter][compound] = value
    return values


def format_history_hover_text(
    rows,
    *,
    leader_lap: int,
    reference_compound: str,
) -> str:
    """Format all tyre readings, re-referencing pace to one compound."""
    values = history_values_at_lap(rows, leader_lap)
    degradation = values["degradation"]
    deltas = values["compound_delta"]
    compounds = sorted(
        set(degradation) | set(deltas),
        key=lambda compound: ({"SOFT": 0, "MEDIUM": 1, "HARD": 2}.get(compound, 99), compound),
    )
    reference = str(reference_compound).upper()
    if reference not in deltas and deltas:
        reference = min(
            deltas,
            key=lambda compound: ({"SOFT": 0, "MEDIUM": 1, "HARD": 2}.get(compound, 99), compound),
        )
    reference_value = deltas.get(reference, 0.0)
    lines = [f"Leader lap {int(leader_lap)} | pace reference: {reference}"]
    for compound in compounds:
        degradation_text = (
            f"{degradation[compound]:.3f} s/lap"
            if compound in degradation
            else "—"
        )
        pace_text = (
            f"{deltas[compound] - reference_value:+.3f} s"
            if compound in deltas
            else "—"
        )
        lines.append(f"{compound}: degradation {degradation_text} | pace Δ {pace_text}")
    return "\n".join(lines)


class StrategyBarWidget(QtWidgets.QWidget):
    def __init__(self, compounds, pit_laps, current_lap, total_laps, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(1)
        boundaries = (int(current_lap),) + tuple(int(value) for value in pit_laps) + (
            int(total_laps),
        )
        for index, compound in enumerate(compounds):
            length = max(boundaries[index + 1] - boundaries[index], 1)
            label = QtWidgets.QLabel(str(compound)[0])
            label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            label.setStyleSheet(
                f"background:{COMPOUND_COLOURS.get(str(compound).upper(), '#bbbbbb')};"
                "border:1px solid #777; font-weight:bold;"
            )
            layout.addWidget(label, length)


class StrategyPredictionWindow(QtWidgets.QDialog):
    def __init__(
        self,
        *,
        year: int,
        round_number: int,
        service: OfflineAnalysisService | None = None,
        state_directory: str | Path | None = None,
        persistence_mode: str = "durable",
        auto_save_history: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.year = int(year)
        self.round_number = int(round_number)
        self.service = service or OfflineAnalysisService(
            WORKSPACE_DATA_ROOT if WORKSPACE_DATA_ROOT.is_dir() else None
        )
        if persistence_mode not in {"temporary", "durable"}:
            raise ValueError("persistence_mode must be 'temporary' or 'durable'.")
        self.persistence_mode = persistence_mode
        self.auto_save_history = bool(auto_save_history)
        state_root = (
            Path(state_directory).expanduser()
            if state_directory is not None
            else weekend_model_root(self.year, self.round_number, self.service.data_root)
        )
        self._state_path = state_root / "strategy_prediction_state.json"
        self._state_ready = False
        self._restored_driver_code: str | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="strategy-search")
        self._future: Future | None = None
        self._future_mode: str | None = None
        self._live_recalculation_queue: deque[tuple[int, object, dict[str, object]]] = deque()
        self._strategy_refresh_pending = False
        self._live_snapshot = None
        self._last_seen_leader_lap = 0
        self._manual_overrides: dict[tuple[str, str], float] = {}
        self._loading_model = False
        self._strategy_input_loading = False
        self._manual_baseline_compounds: set[str] = set()
        self._manual_degradation_compounds: set[str] = set()
        self._baseline_values: dict[str, float] = {}
        self._degradation_values: dict[str, float] = {}
        self._estimated_strategy_inputs: dict[str, dict[str, object]] = {}
        self._initial_strategy_inputs: dict[str, dict[str, object]] = {}
        self._external_tyre_sources: dict[str, str] = {}
        self._last_strategy_rows: list[dict[str, object]] = []
        self._working_plot_rows: list[dict[str, object]] = []
        self._displayed_history_rows: list[dict[str, object]] = []
        self._hovered_history_lap: int | None = None
        self._auto_history_name = (
            f"Live {self.year} R{self.round_number} "
            + datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        )
        self._reference_compound = "HARD"
        self.enforce_compound_order = QtWidgets.QCheckBox(
            "Enforce SOFT quickest/highest degradation → MEDIUM → HARD"
        )
        self.enforce_compound_order.setChecked(True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowModality(QtCore.Qt.WindowModality.NonModal)
        self.setWindowTitle(f"Strategy Prediction — {year} round {round_number}")
        self.resize(980, 680)
        layout = QtWidgets.QVBoxLayout(self)
        self.status_label = QtWidgets.QLabel("Loading model…")
        self.tabs = QtWidgets.QTabWidget()
        self.model_table = QtWidgets.QTableWidget()
        self.strategy_tab = self._build_strategy_tab()
        self.history_tab = self._build_history_tab()
        self.live_tab = self._build_live_tab()
        self.tabs.addTab(self.model_table, "Model summary")
        self.tabs.addTab(self.strategy_tab, "Strategy predictions")
        self.tabs.addTab(self.history_tab, "Update history")
        self.tabs.addTab(self.live_tab, "Live recalculation")
        layout.addWidget(self.status_label)
        layout.addWidget(self.tabs)
        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.setInterval(100)
        self._poll_timer.timeout.connect(self._poll_future)
        if self.persistence_mode == "temporary":
            self.service.activate_temporary_replay_model(self.year, self.round_number)
        self._load_model()
        self._load_history()
        self._load_model_config()
        self._load_saved_state()
        self._state_ready = True
        self._connect_state_persistence()
        if self.persistence_mode == "temporary":
            self._append_log(
                "Replay persistence: settings only; calculations, results, and new "
                "history remain temporary until Save snapshot long-term is used."
            )
        else:
            self._append_log("Durable persistence: live calculations and results are retained.")

    def _build_strategy_tab(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        assumptions_header = QtWidgets.QHBoxLayout()
        assumptions_header.addWidget(QtWidgets.QLabel("Tyre inputs used by strategy search"))
        self.load_tyre_file_button = QtWidgets.QPushButton("Load tyre info from file…")
        assumptions_header.addStretch(1)
        assumptions_header.addWidget(self.load_tyre_file_button)
        self.strategy_input_table = QtWidgets.QTableWidget()
        self.strategy_input_table.setColumnCount(9)
        self.strategy_input_table.setHorizontalHeaderLabels(
            [
                "Compound", "Initial FP baseline\n(s vs fastest)",
                "Latest baseline\n(s vs fastest)", "Used baseline\n(editable)",
                "Used delta\n(editable, vs reference)", "Initial FP degradation",
                "Latest degradation", "Used degradation\n(editable)", "Used source",
            ]
        )
        self.strategy_input_table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.DoubleClicked
            | QtWidgets.QAbstractItemView.EditTrigger.SelectedClicked
            | QtWidgets.QAbstractItemView.EditTrigger.EditKeyPressed
            | QtWidgets.QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        self.strategy_input_table.setMaximumHeight(170)
        input_help = QtWidgets.QLabel(
            "Edit the highlighted Used cells, then calculate again. "
            "Lower baseline is quicker; degradation is seconds lost per tyre lap."
        )
        controls = QtWidgets.QHBoxLayout()
        self.completed_lap = QtWidgets.QSpinBox()
        self.completed_lap.setRange(0, 200)
        self.total_laps = QtWidgets.QSpinBox()
        self.total_laps.setRange(1, 200)
        self.total_laps.setValue(57)
        self.compound = QtWidgets.QComboBox()
        self.compound.addItems(["SOFT", "MEDIUM", "HARD"])
        self.compound.setCurrentText("MEDIUM")
        self.tyre_age = QtWidgets.QSpinBox()
        self.tyre_age.setRange(0, 100)
        self.pit_loss = QtWidgets.QDoubleSpinBox()
        self.pit_loss.setRange(0, 120)
        self.pit_loss.setValue(20)
        self.pit_loss.setSuffix(" s")
        self.max_stops = QtWidgets.QSpinBox()
        self.max_stops.setRange(0, 5)
        self.max_stops.setValue(2)
        self.driver = QtWidgets.QComboBox()
        self.driver.addItem("Pre-race / manual", None)
        self.result_count = QtWidgets.QSpinBox()
        self.result_count.setRange(1, 100)
        self.result_count.setValue(10)
        self.generate_button = QtWidgets.QPushButton("Calculate top 10")
        for label, control in (
            ("Completed lap", self.completed_lap), ("Total laps", self.total_laps),
            ("Compound", self.compound), ("Tyre age", self.tyre_age),
            ("Pit loss", self.pit_loss), ("Max stops", self.max_stops),
            ("Driver", self.driver), ("Results", self.result_count),
        ):
            controls.addWidget(QtWidgets.QLabel(label))
            controls.addWidget(control)
        controls.addWidget(self.generate_button)
        self.strategy_table = QtWidgets.QTableWidget()
        self.strategy_table.setColumnCount(5)
        self.strategy_table.setHorizontalHeaderLabels(
            ["Rank", "Strategy", "Stints", "Cost", "Delta"]
        )
        self.strategy_table.horizontalHeader().setSectionResizeMode(
            2, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        self.pin_button = QtWidgets.QPushButton("Pin selected (max 3)")
        self.pinned_label = QtWidgets.QLabel("Pinned: —")
        layout.addLayout(assumptions_header)
        layout.addWidget(input_help)
        layout.addWidget(self.strategy_input_table)
        layout.addLayout(controls)
        layout.addWidget(self.strategy_table)
        layout.addWidget(self.pin_button)
        layout.addWidget(self.pinned_label)
        self.generate_button.clicked.connect(self._start_search)
        self.result_count.valueChanged.connect(
            lambda value: self.generate_button.setText(f"Calculate top {value}")
        )
        self.pin_button.clicked.connect(self._pin_selected)
        self.driver.currentIndexChanged.connect(self._apply_selected_driver_state)
        self.strategy_input_table.itemChanged.connect(self._strategy_input_changed)
        self.enforce_compound_order.toggled.connect(self._apply_strategy_input_order)
        self.load_tyre_file_button.clicked.connect(self._load_tyre_information_from_file)
        return widget

    def _build_history_tab(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        save_row = QtWidgets.QHBoxLayout()
        self.history_snapshot_combo = QtWidgets.QComboBox()
        self.snapshot_name = QtWidgets.QLineEdit(
            datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        )
        self.save_snapshot_button = QtWidgets.QPushButton("Save snapshot long-term")
        save_row.addWidget(QtWidgets.QLabel("Load history"))
        save_row.addWidget(self.history_snapshot_combo, 1)
        save_row.addWidget(QtWidgets.QLabel("Snapshot name"))
        save_row.addWidget(self.snapshot_name, 1)
        save_row.addWidget(self.save_snapshot_button)
        self.history_header = QtWidgets.QLabel(
            "Tyre model update history — hover to select a lap; left-click for actions"
        )
        self.history_header.setStyleSheet("font-weight: bold;")
        self.degradation_history_plot = pg.PlotWidget()
        self.degradation_history_plot.setTitle("Tyre degradation")
        self.degradation_history_plot.setLabel("left", "Degradation", units="s/lap")
        self.degradation_history_plot.setLabel("bottom", "Leader lap")
        self.compound_delta_history_plot = pg.PlotWidget()
        self.compound_delta_history_plot.setTitle("Compound delta")
        self.compound_delta_history_plot.setLabel("left", "Delta", units="s")
        self.compound_delta_history_plot.setLabel("bottom", "Leader lap")
        # Compatibility alias for callers that previously accessed the single plot.
        self.history_plot = self.degradation_history_plot
        self._history_cursor_lines = {
            plot: pg.InfiniteLine(
                angle=90,
                movable=False,
                pen=pg.mkPen("#3366cc", width=2, style=QtCore.Qt.PenStyle.DashLine),
            )
            for plot in (
                self.degradation_history_plot,
                self.compound_delta_history_plot,
            )
        }
        self._history_hover_labels = {
            self.degradation_history_plot: None,
            self.compound_delta_history_plot: None,
        }
        self.history_table = QtWidgets.QTableWidget()
        layout.addLayout(save_row)
        layout.addWidget(self.history_header)
        layout.addWidget(self.degradation_history_plot, 1)
        layout.addWidget(self.compound_delta_history_plot, 1)
        layout.addWidget(self.history_table, 1)
        self.save_snapshot_button.clicked.connect(self._save_long_term_snapshot)
        self.history_snapshot_combo.currentIndexChanged.connect(
            self._history_selection_changed
        )
        for plot in self._history_cursor_lines:
            plot.scene().sigMouseMoved.connect(
                lambda position, source=plot: self._history_mouse_moved(source, position)
            )
            plot.scene().sigMouseClicked.connect(
                lambda event, source=plot: self._history_mouse_clicked(source, event)
            )
        return widget

    def _build_live_tab(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        controls = QtWidgets.QFormLayout()
        self.auto_recalculate = QtWidgets.QCheckBox("Recalculate when leader lap advances")
        self.auto_recalculate.setChecked(True)
        self.sample_count = QtWidgets.QSpinBox()
        self.sample_count.setRange(100, 1_000_000)
        self.sample_count.setValue(LIVE_DEFAULT_SAMPLE_COUNT)
        self.quick_threshold = QtWidgets.QDoubleSpinBox()
        self.quick_threshold.setRange(1.0, 2.0)
        self.quick_threshold.setDecimals(3)
        self.quick_threshold.setValue(1.10)
        self.min_clean_laps = QtWidgets.QSpinBox()
        self.min_clean_laps.setRange(2, 30)
        self.min_clean_laps.setValue(4)
        self.exclude_stint_start_laps = QtWidgets.QSpinBox()
        self.exclude_stint_start_laps.setRange(0, 10)
        self.exclude_stint_end_laps = QtWidgets.QSpinBox()
        self.exclude_stint_end_laps.setRange(0, 10)
        self.traffic_source = QtWidgets.QComboBox()
        self.traffic_source.addItem(
            "Automatic telemetry with sector fallback", "automatic"
        )
        self.traffic_source.addItem("Force sector-boundary only", "sector")
        self.min_gap_ahead = QtWidgets.QDoubleSpinBox()
        self.min_gap_ahead.setRange(0.0, 30.0)
        self.min_gap_ahead.setDecimals(1)
        self.min_gap_ahead.setValue(2.5)
        self.min_gap_ahead.setSuffix(" s")
        self.min_gap_behind = QtWidgets.QDoubleSpinBox()
        self.min_gap_behind.setRange(0.0, 30.0)
        self.min_gap_behind.setDecimals(1)
        self.min_gap_behind.setValue(1.0)
        self.min_gap_behind.setSuffix(" s")
        self.clean_compound_checks = {
            compound: QtWidgets.QCheckBox(compound)
            for compound in ("SOFT", "MEDIUM", "HARD")
        }
        for checkbox in self.clean_compound_checks.values():
            checkbox.setChecked(True)
        self.clean_details_button = QtWidgets.QPushButton("Clean-lap details…")
        self.clean_definition_label = QtWidgets.QLabel()
        self.clean_definition_label.setWordWrap(True)
        self.clean_details_dialog = self._build_clean_lap_details_dialog()
        controls.addRow(self.auto_recalculate)
        controls.addRow("Live recalculation samples", self.sample_count)
        controls.addRow("Compound constraints", self.enforce_compound_order)
        controls.addRow("Clean-lap definition", self.clean_details_button)
        controls.addRow("Traffic source", self.traffic_source)
        self.live_note = QtWidgets.QLabel(
            "Traffic uses rolling car telemetry when coverage is sufficient and "
            "falls back to sector-boundary crossing times automatically."
        )
        self.live_note.setWordWrap(True)
        self.calculation_log = QtWidgets.QPlainTextEdit()
        self.calculation_log.setReadOnly(True)
        layout.addLayout(controls)
        layout.addWidget(self.clean_definition_label)
        layout.addWidget(self.live_note)
        layout.addWidget(self.calculation_log)
        self.clean_details_button.clicked.connect(self.clean_details_dialog.show)
        for control in (
            self.quick_threshold, self.min_clean_laps,
            self.exclude_stint_start_laps, self.exclude_stint_end_laps,
            self.traffic_source, self.min_gap_ahead, self.min_gap_behind,
            *self.clean_compound_checks.values(),
        ):
            if isinstance(control, QtWidgets.QCheckBox):
                signal = control.toggled
            elif isinstance(control, QtWidgets.QComboBox):
                signal = control.currentIndexChanged
            else:
                signal = control.valueChanged
            signal.connect(self._update_clean_definition_label)
        self._update_clean_definition_label()
        return widget

    def _build_clean_lap_details_dialog(self):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Live clean-lap definition")
        layout = QtWidgets.QVBoxLayout(dialog)
        fixed = QtWidgets.QLabel(
            "A lap must have a positive completed lap time plus compound, tyre-life, "
            "and stint metadata. Traffic proximity uses rolling car telemetry around "
            "the lap when coverage is sufficient. Sector-boundary crossing times are "
            "used automatically when telemetry is unavailable or fails coverage checks."
        )
        fixed.setWordWrap(True)
        layout.addWidget(fixed)
        form = QtWidgets.QFormLayout()
        form.addRow("Maximum vs driver's fastest", self.quick_threshold)
        form.addRow("Minimum consecutive laps after exclusions", self.min_clean_laps)
        form.addRow("Exclude laps at run start", self.exclude_stint_start_laps)
        form.addRow("Exclude laps at run end", self.exclude_stint_end_laps)
        form.addRow("Clear ahead by at least", self.min_gap_ahead)
        form.addRow("Clear behind by at least", self.min_gap_behind)
        compounds = QtWidgets.QHBoxLayout()
        for checkbox in self.clean_compound_checks.values():
            compounds.addWidget(checkbox)
        compounds.addStretch(1)
        form.addRow("Allowed compounds", compounds)
        layout.addLayout(form)
        explanation = QtWidgets.QLabel(
            "Filtering order: required metadata → allowed compound → lap-time threshold "
            "→ selected traffic gaps → consecutive driver/stint run → start/end "
            "exclusions → minimum run length. Normally, physical gaps come from rolling "
            "telemetry with automatic sector fallback. Forcing sector fallback requires "
            "all four boundaries: lap start, S1, S2 and lap end."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.close)
        layout.addWidget(buttons)
        return dialog

    def _selected_clean_compounds(self) -> tuple[str, ...]:
        return tuple(
            compound for compound, checkbox in self.clean_compound_checks.items()
            if checkbox.isChecked()
        )

    def _force_sector_traffic(self) -> bool:
        return self.traffic_source.currentData() == "sector"

    def _update_clean_definition_label(self, *_args) -> None:
        compounds = "/".join(self._selected_clean_compounds()) or "none"
        self.clean_definition_label.setText(
            f"Current: ≤{self.quick_threshold.value():.3f}× driver fastest; "
            f"minimum run {self.min_clean_laps.value()}; exclude "
            f"{self.exclude_stint_start_laps.value()} at start / "
            f"{self.exclude_stint_end_laps.value()} at end; compounds {compounds}; "
            + (
                f"forced sector gaps ≥{self.min_gap_ahead.value():.1f}s ahead / "
                f"≥{self.min_gap_behind.value():.1f}s behind."
                if self._force_sector_traffic()
                else (
                    f"automatic telemetry traffic ≥{self.min_gap_ahead.value():.1f}s "
                    f"ahead / ≥{self.min_gap_behind.value():.1f}s behind, with sector "
                    "fallback when required."
                )
            )
        )

    def _load_model(self, *, record_history: bool = False) -> None:
        try:
            summary = self.service.load_model(self.year, self.round_number)
        except Exception as exc:
            self.status_label.setText(f"Model unavailable: {exc}")
            self.generate_button.setEnabled(False)
            return
        columns = [
            "parameter", "compound", "session", "median", "p10", "p90",
            "unit", "ess", "source_scope",
        ]
        self._loading_model = True
        self.model_table.clear()
        self.model_table.setColumnCount(len(columns))
        self.model_table.setHorizontalHeaderLabels(columns)
        self.model_table.setRowCount(len(summary.parameters))
        for row_index, row in enumerate(summary.parameters):
            parameter = str(row.get("parameter") or "")
            compound = str(row.get("compound") or "").upper()
            for column_index, column in enumerate(columns):
                value = row.get(column, "")
                item = QtWidgets.QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                self.model_table.setItem(row_index, column_index, item)
        self.model_table.resizeColumnsToContents()
        self._loading_model = False
        self._load_strategy_inputs(summary)
        if record_history:
            self._record_history_point(summary)
        reason = summary.manifest.get("stale_reason")
        self.status_label.setText(
            f"Model: {summary.status}" + (f" — {reason}" if reason else "")
        )

    def _load_history(self) -> None:
        selected = (
            self.history_snapshot_combo.currentData()
            if self.history_snapshot_combo.count()
            else "clear"
        )
        self._refresh_history_entries(selected_key=selected)

    def _load_model_config(self) -> None:
        config = self.service.live_model_config(self.year, self.round_number, "R")
        if not config:
            self._append_log("No model_config.json found; run pre_race_analysis first.")
            return
        self.sample_count.setValue(int(config.get("sample_count", self.sample_count.value())))
        self.quick_threshold.setValue(float(config.get("quick_lap_threshold", 1.10)))
        self.min_clean_laps.setValue(int(config.get("min_clean_air_laps", 4)))
        self.min_gap_ahead.setValue(
            float(config.get("clean_mean_time_delta_seconds", 2.5))
        )
        self.min_gap_behind.setValue(
            float(config.get("clean_mean_time_delta_behind_seconds", 1.0))
        )
        self._reference_compound = str(config.get("reference_compound", "HARD")).upper()
        self.enforce_compound_order.setChecked(
            bool(config.get("enforce_compound_order", True))
        )
        self._sync_strategy_overrides()
        self._render_strategy_inputs()
        self._append_log(
            f"Effective fitting settings ({config.get('settings_source', 'unknown')}) use "
            f"{config.get('sample_count', '—')} Monte Carlo samples, "
            f"seed {config.get('random_seed', '—')}. Live recalculation is set to "
            f"{self.sample_count.value()} samples (editable above)."
        )
        if config.get("settings_path"):
            self._append_log(f"Fitting settings source: {config['settings_path']}")

    def _start_search(self) -> None:
        if self._future is not None:
            return
        if self.total_laps.value() <= self.completed_lap.value():
            QtWidgets.QMessageBox.warning(self, "Strategy", "Total laps must exceed completed lap.")
            return
        self._sync_strategy_overrides()
        state = StrategySearchState(
            self.completed_lap.value(), self.total_laps.value(), self.compound.currentText(),
            self.tyre_age.value(), selected_driver=self.driver.currentData(),
            max_future_stops=self.max_stops.value()
        )
        self.generate_button.setEnabled(False)
        self.status_label.setText("Strategy calculation running…")
        self._future = self._executor.submit(
            self.service.generate_continuations,
            year=self.year, round_number=self.round_number, state=state,
            pit_loss=self.pit_loss.value(), k=self.result_count.value(),
            overrides=self._manual_overrides,
            persist=self.persistence_mode == "durable",
        )
        self._future_mode = "strategy"
        self._poll_timer.start()

    def _poll_future(self) -> None:
        if self._future is None or not self._future.done():
            return
        self._poll_timer.stop()
        future, self._future = self._future, None
        mode = self._future_mode
        self._future_mode = None
        self.generate_button.setEnabled(True)
        try:
            results = future.result()
        except Exception as exc:
            label = "Live recalculation" if mode == "live-model" else "Strategy calculation"
            self.status_label.setText(f"{label} failed: {exc}")
            self._append_log(f"{label} failed: {exc}")
            self._start_next_live_recalculation()
            return
        if mode == "live-model":
            source_text = ", ".join(
                f"{session}={count}"
                for session, count in results.clean_lap_sources
            ) or "none"
            self._append_log(
                f"Leader lap {results.leader_lap}: clean laps "
                f"{results.previous_clean_lap_count} → {results.eligible_lap_count} "
                f"({results.eligible_lap_count - results.previous_clean_lap_count:+d}); "
                f"sources [{source_text}]; {results.status}; "
                f"samples={results.sample_count}; time={results.elapsed_seconds:.2f}s; "
                f"queued={len(self._live_recalculation_queue)}"
                + (f"; model={results.analysis_id}" if results.analysis_id else "")
            )
            if results.status == "updated":
                self._load_model(record_history=True)
                self.status_label.setText(f"Live model updated at leader lap {results.leader_lap}")
                self._strategy_refresh_pending = True
            if self._start_next_live_recalculation():
                return
            if self._strategy_refresh_pending:
                self._strategy_refresh_pending = False
                self._start_search()
            return
        self._last_strategy_rows = [
            {
                "rank": result.rank,
                "formatted_strategy": result.formatted_strategy,
                "compounds": list(result.plan.compounds),
                "pit_laps": list(result.plan.pit_laps),
                "remaining_cost": result.remaining_cost,
                "delta_to_best": result.delta_to_best,
                "current_lap": self.completed_lap.value(),
                "total_laps": self.total_laps.value(),
            }
            for result in results
        ]
        self._render_strategy_rows()
        self._save_state()
        self._start_next_live_recalculation()

    def _render_strategy_rows(self) -> None:
        self.status_label.setText(
            f"Strategy results current — {len(self._last_strategy_rows)} distinct compound sequences"
        )
        self.strategy_table.setRowCount(len(self._last_strategy_rows))
        for row, result in enumerate(self._last_strategy_rows):
            values = [
                result["rank"], result["formatted_strategy"], "",
                f"{float(result['remaining_cost']):.3f}",
                f"+{float(result['delta_to_best']):.3f}",
            ]
            for column, value in enumerate(values):
                self.strategy_table.setItem(row, column, QtWidgets.QTableWidgetItem(str(value)))
            self.strategy_table.setCellWidget(
                row, 2, StrategyBarWidget(
                    result["compounds"], result["pit_laps"],
                    int(result.get("current_lap", self.completed_lap.value())),
                    int(result.get("total_laps", self.total_laps.value())),
                )
            )
            self.strategy_table.item(row, 0).setData(
                QtCore.Qt.ItemDataRole.UserRole,
                f"rank-{result['rank']}:{result['formatted_strategy']}"
            )

    def _pin_selected(self) -> None:
        row = self.strategy_table.currentRow()
        if row < 0:
            return
        item = self.strategy_table.item(row, 0)
        strategy_id = str(item.data(QtCore.Qt.ItemDataRole.UserRole))
        try:
            pinned = self.service.pin(strategy_id)
        except ValueError as exc:
            QtWidgets.QMessageBox.information(self, "Pinned strategies", str(exc))
            return
        self.pinned_label.setText("Pinned: " + ", ".join(pinned))

    def consume_live_snapshot(self, snapshot) -> None:
        """Receive the same canonical snapshot that drives the live timing plot."""
        self._live_snapshot = snapshot
        current_driver = self.driver.currentData()
        if current_driver is None and self._restored_driver_code:
            current_driver = self._restored_driver_code
        codes = [driver.driver_code for driver in snapshot.drivers]
        existing = [self.driver.itemData(index) for index in range(self.driver.count())]
        if existing[1:] != codes:
            self.driver.blockSignals(True)
            self.driver.clear()
            self.driver.addItem("Pre-race / manual", None)
            for driver in snapshot.drivers:
                self.driver.addItem(f"{driver.driver_code} — {driver.team}", driver.driver_code)
            target = self.driver.findData(current_driver)
            self.driver.setCurrentIndex(max(target, 0))
            self.driver.blockSignals(False)
            if target >= 0:
                self._restored_driver_code = None
        self._apply_selected_driver_state()
        leader_laps = [lap.lap_number for lap in snapshot.completed_laps if lap.position == 1]
        all_laps = [lap.lap_number for lap in snapshot.completed_laps]
        leader_lap = int(max(leader_laps or all_laps or [0]))
        if leader_lap <= self._last_seen_leader_lap:
            return
        previous_leader_lap = self._last_seen_leader_lap
        self._last_seen_leader_lap = leader_lap
        self._append_log(f"Leader completed lap {leader_lap}; checking for new clean laps.")
        if not self.auto_recalculate.isChecked():
            self._append_log("Automatic recalculation is disabled.")
            return
        if self.persistence_mode != "temporary" and self._future is not None:
            self._append_log(
                "Live calculation already running; this update is not queued and "
                "the newest data will be covered on the next leader-lap trigger."
            )
            return
        options = {
            "sample_count": self.sample_count.value(),
            "quick_lap_threshold": self.quick_threshold.value(),
            "min_clean_laps": self.min_clean_laps.value(),
            "allowed_compounds": self._selected_clean_compounds(),
            "exclude_stint_start_laps": self.exclude_stint_start_laps.value(),
            "exclude_stint_end_laps": self.exclude_stint_end_laps.value(),
            "use_sector_boundary_traffic": self._force_sector_traffic(),
            "min_gap_ahead_seconds": self.min_gap_ahead.value(),
            "min_gap_behind_seconds": self.min_gap_behind.value(),
            "enforce_compound_order": self.enforce_compound_order.isChecked(),
            "persist": self.persistence_mode == "durable",
        }
        queued_snapshots = (
            self._leader_lap_snapshots(
                snapshot,
                first_lap=previous_leader_lap + 1,
                last_lap=leader_lap,
            )
            if self.persistence_mode == "temporary"
            else [(leader_lap, snapshot)]
        )
        self._live_recalculation_queue.extend(
            (queued_lap, queued_snapshot, dict(options))
            for queued_lap, queued_snapshot in queued_snapshots
        )
        self._append_log(
            f"Queued {len(queued_snapshots)} recalculation request(s) through "
            f"leader lap {leader_lap}; "
            f"pending={len(self._live_recalculation_queue)}."
        )
        self._start_next_live_recalculation()

    @staticmethod
    def _leader_lap_snapshots(snapshot, *, first_lap: int, last_lap: int):
        snapshots = []
        for leader_lap in range(max(int(first_lap), 1), int(last_lap) + 1):
            lap_candidates = [
                lap for lap in snapshot.completed_laps
                if int(lap.lap_number) == leader_lap and lap.position == 1
            ]
            if not lap_candidates:
                lap_candidates = [
                    lap for lap in snapshot.completed_laps
                    if int(lap.lap_number) == leader_lap
                ]
            clocks = [
                StrategyPredictionWindow._completion_clock(lap)
                for lap in lap_candidates
            ]
            clocks = [value for value in clocks if value is not None]
            cutoff = min(clocks) if clocks else None
            if cutoff is None:
                completed_laps = tuple(
                    lap for lap in snapshot.completed_laps
                    if int(lap.lap_number) <= leader_lap
                )
                completed_sectors = tuple(
                    sector for sector in snapshot.completed_sectors
                    if int(sector.lap_number) <= leader_lap
                )
            else:
                completed_laps = tuple(
                    lap for lap in snapshot.completed_laps
                    if (
                        StrategyPredictionWindow._completion_clock(lap) is not None
                        and StrategyPredictionWindow._completion_clock(lap) <= cutoff
                    )
                )
                completed_sectors = tuple(
                    sector for sector in snapshot.completed_sectors
                    if (
                        StrategyPredictionWindow._completion_clock(sector) is not None
                        and StrategyPredictionWindow._completion_clock(sector) <= cutoff
                    )
                )
            snapshots.append(
                (
                    leader_lap,
                    replace(
                        snapshot,
                        completed_laps=completed_laps,
                        completed_sectors=completed_sectors,
                    ),
                )
            )
        return snapshots

    @staticmethod
    def _completion_clock(value) -> float | None:
        session_time = getattr(value, "session_time_seconds", None)
        if session_time is not None:
            return float(session_time)
        timestamp = getattr(value, "source_timestamp_utc", None)
        return timestamp.timestamp() if timestamp is not None else None

    def _start_next_live_recalculation(self) -> bool:
        if self._future is not None or not self._live_recalculation_queue:
            return False
        leader_lap, snapshot, options = self._live_recalculation_queue.popleft()
        self._future = self._executor.submit(
            self.service.recalculate_from_live_snapshot,
            year=self.year,
            round_number=self.round_number,
            snapshot=snapshot,
            **options,
        )
        self._future_mode = "live-model"
        self.status_label.setText(
            f"Recalculating model at leader lap {leader_lap} "
            f"({len(self._live_recalculation_queue)} queued)…"
        )
        self._append_log(
            f"Started recalculation for leader lap {leader_lap}; "
            f"remaining queue={len(self._live_recalculation_queue)}."
        )
        self.generate_button.setEnabled(False)
        self._poll_timer.start()
        return True

    def reset_replay_history(self) -> None:
        """Start each replay with a fresh in-memory value-lap plot."""
        if self.persistence_mode != "temporary":
            return
        self.service.activate_temporary_replay_model(self.year, self.round_number)
        self._working_plot_rows.clear()
        self._last_strategy_rows.clear()
        self._live_recalculation_queue.clear()
        self._strategy_refresh_pending = False
        self.strategy_table.setRowCount(0)
        self._last_seen_leader_lap = 0
        self._load_model()
        self._refresh_history_entries(selected_key="clear")
        self._append_log("Replay restarted: temporary value-lap plot cleared.")

    def configure_session_context(
        self,
        *,
        persistence_mode: str,
        state_directory: str | Path | None,
        auto_save_history: bool,
    ) -> None:
        self.persistence_mode = persistence_mode
        self.auto_save_history = bool(auto_save_history)
        if state_directory is not None:
            self._state_path = (
                Path(state_directory).expanduser() / "strategy_prediction_state.json"
            )
        self.service.reset_live_tracking()
        self._live_recalculation_queue.clear()
        self._strategy_refresh_pending = False
        self._working_plot_rows.clear()
        self._show_plot_rows([])
        if persistence_mode == "temporary":
            self.reset_replay_history()
        else:
            self._auto_history_name = (
                f"Live {self.year} R{self.round_number} "
                + datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            )
            self._refresh_history_entries(selected_key="clear")
            self._append_log("Live session started: durable value-lap plot reset.")

    def _apply_selected_driver_state(self) -> None:
        if self._live_snapshot is None:
            return
        driver = self.driver.currentData()
        if not driver:
            return
        laps = [
            lap for lap in self._live_snapshot.completed_laps
            if lap.driver_code == driver and lap.compound is not None
        ]
        if not laps:
            return
        latest = max(laps, key=lambda lap: lap.lap_number)
        self.completed_lap.setValue(int(latest.lap_number))
        index = self.compound.findText(str(latest.compound).upper())
        if index >= 0:
            self.compound.setCurrentIndex(index)
        if latest.tyre_life is not None:
            self.tyre_age.setValue(max(int(round(float(latest.tyre_life))) - 1, 0))

    def _load_strategy_inputs(self, summary) -> None:
        assumptions = self.service.strategy_assumptions(summary)
        self._estimated_strategy_inputs = {
            str(row["compound"]): dict(row) for row in assumptions
        }
        if not self._initial_strategy_inputs:
            initial = self.service.initial_fp_assumptions(self.year, self.round_number)
            if not initial and int(float(summary.manifest.get("as_of_leader_lap") or 0)) == 0:
                initial = assumptions
            self._initial_strategy_inputs = {
                str(row["compound"]): dict(row) for row in initial
            }
        for compound, row in self._estimated_strategy_inputs.items():
            if compound not in self._manual_baseline_compounds:
                self._baseline_values[compound] = float(row["baseline_pace"])
            if compound not in self._manual_degradation_compounds:
                self._degradation_values[compound] = float(row["degradation"])
        self._apply_strategy_input_order()

    def _load_tyre_information_from_file(self) -> None:
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            "Load woStrategy tyre information",
            str(self.service.data_root or WORKSPACE_DATA_ROOT),
            "CSV files (*.csv)",
        )
        if not paths:
            return
        try:
            rows, reference = self.service.load_tyre_information_files(paths)
        except (OSError, ValueError) as exc:
            QtWidgets.QMessageBox.warning(self, "Load tyre information", str(exc))
            return
        if reference:
            self._reference_compound = reference
        updated = []
        for row in rows:
            compound = str(row["compound"]).upper()
            if compound not in self._estimated_strategy_inputs:
                continue
            if "baseline_pace" in row:
                self._baseline_values[compound] = float(row["baseline_pace"])
                self._manual_baseline_compounds.add(compound)
                updated.append(f"{compound} delta")
            if "degradation" in row:
                self._degradation_values[compound] = float(row["degradation"])
                self._manual_degradation_compounds.add(compound)
                updated.append(f"{compound} degradation")
            self._external_tyre_sources[compound] = str(row.get("source") or "file")
        if not updated:
            QtWidgets.QMessageBox.warning(
                self,
                "Load tyre information",
                "The selected files contain no compounds available in the current model.",
            )
            return
        self._apply_strategy_input_order()
        self._save_state()
        self._append_log(
            f"Loaded tyre inputs from {len(paths)} file(s): " + ", ".join(updated)
        )

    def _strategy_input_changed(self, item) -> None:
        if self._strategy_input_loading:
            return
        key = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if not key:
            return
        try:
            value = float(item.text())
        except ValueError:
            self.status_label.setText("Tyre input must be numeric.")
            self._render_strategy_inputs()
            return
        field, compound = key
        if field == "baseline":
            self._baseline_values[compound] = value
            self._manual_baseline_compounds.add(compound)
        elif field == "delta":
            reference_baseline = self._baseline_values.get(
                self._reference_compound,
                max(self._baseline_values.values(), default=0.0),
            )
            self._baseline_values[compound] = float(reference_baseline) + value
            self._manual_baseline_compounds.add(compound)
        else:
            self._degradation_values[compound] = value
            self._manual_degradation_compounds.add(compound)
        self._append_log(f"Manual tyre input: {compound} {field} = {value:g}")
        self._apply_strategy_input_order()
        self._save_state()

    def _apply_strategy_input_order(self, *_args) -> None:
        compounds = [
            compound for compound in ("SOFT", "MEDIUM", "HARD")
            if compound in self._baseline_values and compound in self._degradation_values
        ]
        if self.enforce_compound_order.isChecked() and len(compounds) > 1:
            baseline = sorted(self._baseline_values[compound] for compound in compounds)
            degradation = sorted(
                (self._degradation_values[compound] for compound in compounds),
                reverse=True,
            )
            for compound, value in zip(compounds, baseline):
                self._baseline_values[compound] = float(value)
            for compound, value in zip(compounds, degradation):
                self._degradation_values[compound] = float(value)
        self._sync_strategy_overrides()
        self._render_strategy_inputs()

    def _sync_strategy_overrides(self) -> None:
        if not self._baseline_values:
            return
        reference = self._reference_compound
        reference_baseline = self._baseline_values.get(reference)
        if reference_baseline is None:
            reference_baseline = max(self._baseline_values.values())
        self._manual_overrides = {}
        for compound, baseline in self._baseline_values.items():
            self._manual_overrides[("compound_delta", compound)] = (
                float(baseline) - float(reference_baseline)
            )
        for compound, degradation in self._degradation_values.items():
            self._manual_overrides[("degradation", compound)] = float(degradation)

    def _render_strategy_inputs(self) -> None:
        if not hasattr(self, "strategy_input_table"):
            return
        compounds = [
            compound for compound in ("SOFT", "MEDIUM", "HARD")
            if compound in self._estimated_strategy_inputs
        ]
        self._strategy_input_loading = True
        self.strategy_input_table.setRowCount(len(compounds))
        reference_baseline = self._baseline_values.get(
            self._reference_compound,
            max(self._baseline_values.values(), default=0.0),
        )
        for row_index, compound in enumerate(compounds):
            estimated = self._estimated_strategy_inputs[compound]
            initial = self._initial_strategy_inputs.get(compound, {})
            initial_baseline = initial.get("baseline_pace")
            initial_degradation = initial.get("degradation")
            values = [
                compound,
                "—" if initial_baseline is None else f"{float(initial_baseline):.4f}",
                f"{float(estimated['baseline_pace']):.4f}",
                f"{self._baseline_values[compound]:.4f}",
                f"{self._baseline_values[compound] - reference_baseline:+.4f}",
                "—" if initial_degradation is None else f"{float(initial_degradation):.4f}",
                f"{float(estimated['degradation']):.4f}",
                f"{self._degradation_values[compound]:.4f}",
                self._external_tyre_sources.get(
                    compound,
                    f"{estimated['sessions']} ({estimated['aggregation']})",
                ),
            ]
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(str(value))
                if column == 3:
                    item.setData(
                        QtCore.Qt.ItemDataRole.UserRole, ("baseline", compound)
                    )
                elif column == 4 and compound != self._reference_compound:
                    item.setData(
                        QtCore.Qt.ItemDataRole.UserRole, ("delta", compound)
                    )
                elif column == 7:
                    item.setData(
                        QtCore.Qt.ItemDataRole.UserRole, ("degradation", compound)
                    )
                else:
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                if item.data(QtCore.Qt.ItemDataRole.UserRole):
                    item.setBackground(QtGui.QColor("#fff4c2"))
                    item.setToolTip("Editable value used by the next strategy calculation")
                self.strategy_input_table.setItem(row_index, column, item)
        self.strategy_input_table.resizeColumnsToContents()
        self._strategy_input_loading = False

    def _plot_parameter_history(self, rows=None) -> None:
        plots = {
            "degradation": self.degradation_history_plot,
            "compound_delta": self.compound_delta_history_plot,
        }
        for plot in plots.values():
            plot.clear()
            self._history_hover_labels[plot] = None
            plot.addLegend()
            cursor = self._history_cursor_lines[plot]
            plot.addItem(cursor)
            cursor.hide()
        if rows is None:
            rows = self.service.parameter_history(self.year, self.round_number)
        if not rows:
            self._hovered_history_lap = None
            self.history_header.setText(
                "Tyre model update history — hover to select a lap; left-click for actions"
            )
            return
        grouped: dict[tuple[str, str], dict[int, float]] = {}
        for row in rows:
            if str(row.get("source_scope", "")).upper() != "AGGREGATE":
                continue
            parameter = str(row.get("parameter") or "")
            compound = str(row.get("compound") or "").upper()
            if parameter not in {"degradation", "compound_delta"} or not compound:
                continue
            try:
                lap = int(float(row.get("as_of_leader_lap") or 0))
                median = float(row["median"])
            except (KeyError, TypeError, ValueError):
                continue
            grouped.setdefault((parameter, compound), {})[lap] = median
        colours = {"SOFT": "#e10600", "MEDIUM": "#d9a900", "HARD": "#777777"}
        for (parameter, compound), values in sorted(grouped.items()):
            points = sorted(values.items())
            if not points:
                continue
            pen = pg.mkPen(colours.get(compound, "#3366cc"), width=2)
            plots[parameter].plot(
                [point[0] for point in points], [point[1] for point in points],
                pen=pen, symbol="o", name=compound,
            )

    def _history_mouse_moved(self, plot, scene_position) -> None:
        if not plot.sceneBoundingRect().contains(scene_position):
            self._clear_history_hover_label(plot)
            return
        laps = sorted(
            {
                int(float(row.get("as_of_leader_lap") or 0))
                for row in self._displayed_history_rows
                if row.get("as_of_leader_lap") is not None
            }
        )
        if not laps:
            return
        x_value = float(plot.plotItem.vb.mapSceneToView(scene_position).x())
        selected_lap = min(laps, key=lambda lap: abs(lap - x_value))
        hovered_compound = self._history_compound_near_pointer(
            plot, scene_position, selected_lap
        )
        reference = hovered_compound or self._reference_compound
        self._select_history_lap(selected_lap, reference_compound=reference)
        self._show_history_hover_label(
            plot,
            scene_position,
            leader_lap=selected_lap,
            reference_compound=reference,
        )

    def _select_history_lap(
        self, leader_lap: int, *, reference_compound: str | None = None
    ) -> None:
        self._hovered_history_lap = int(leader_lap)
        for plot, cursor in self._history_cursor_lines.items():
            cursor.setPos(self._hovered_history_lap)
            cursor.show()
        reference_text = (
            f"; pace reference {str(reference_compound).upper()}"
            if reference_compound
            else ""
        )
        self.history_header.setText(
            f"Selected leader lap {self._hovered_history_lap}{reference_text} — "
            "left-click a chart to use its tyre values"
        )

    def _history_compound_near_pointer(
        self, plot, scene_position, leader_lap: int
    ) -> str | None:
        parameter = (
            "degradation"
            if plot is self.degradation_history_plot
            else "compound_delta"
        )
        readings = history_values_at_lap(
            self._displayed_history_rows, leader_lap
        )[parameter]
        nearest: tuple[float, str] | None = None
        for compound, value in readings.items():
            point = plot.plotItem.vb.mapViewToScene(
                QtCore.QPointF(float(leader_lap), float(value))
            )
            distance = (
                (float(point.x()) - float(scene_position.x())) ** 2
                + (float(point.y()) - float(scene_position.y())) ** 2
            ) ** 0.5
            if nearest is None or distance < nearest[0]:
                nearest = (distance, compound)
        return nearest[1] if nearest is not None and nearest[0] <= 14.0 else None

    def _show_history_hover_label(
        self,
        plot,
        scene_position,
        *,
        leader_lap: int,
        reference_compound: str,
    ) -> None:
        self._clear_history_hover_label(plot)
        view_position = plot.plotItem.vb.mapSceneToView(scene_position)
        scene_bounds = plot.sceneBoundingRect()
        anchor = (
            1 if float(scene_position.x()) > float(scene_bounds.center().x()) else 0,
            0 if float(scene_position.y()) < float(scene_bounds.center().y()) else 1,
        )
        label = pg.TextItem(
            text=format_history_hover_text(
                self._displayed_history_rows,
                leader_lap=leader_lap,
                reference_compound=reference_compound,
            ),
            color="#111111",
            fill=pg.mkBrush(255, 255, 255, 235),
            border=pg.mkPen("#666666"),
            anchor=anchor,
        )
        plot.addItem(label, ignoreBounds=True)
        label.setPos(float(view_position.x()), float(view_position.y()))
        self._history_hover_labels[plot] = label

    def _clear_history_hover_label(self, plot) -> None:
        label = self._history_hover_labels.get(plot)
        if label is None:
            return
        try:
            plot.removeItem(label)
        except RuntimeError:
            pass
        self._history_hover_labels[plot] = None

    def _history_mouse_clicked(self, plot, event) -> None:
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return
        position = event.scenePos()
        if not plot.sceneBoundingRect().contains(position):
            return
        self._history_mouse_moved(plot, position)
        if self._hovered_history_lap is None:
            return
        menu = QtWidgets.QMenu(self)
        action = menu.addAction(
            f"Use lap {self._hovered_history_lap} tyre values for strategy calculation"
        )
        selected = menu.exec(event.screenPos().toPoint())
        if selected is action:
            self._apply_history_lap_to_strategy(self._hovered_history_lap)

    def _apply_history_lap_to_strategy(self, leader_lap: int) -> bool:
        selected_rows = [
            row for row in self._displayed_history_rows
            if str(row.get("source_scope", "")).upper() == "AGGREGATE"
            and int(float(row.get("as_of_leader_lap") or 0)) == int(leader_lap)
        ]
        degradation: dict[str, float] = {}
        deltas: dict[str, float] = {}
        for row in selected_rows:
            compound = str(row.get("compound") or "").upper()
            try:
                value = float(row["median"])
            except (KeyError, TypeError, ValueError):
                continue
            if str(row.get("parameter")) == "degradation":
                degradation[compound] = value
            elif str(row.get("parameter")) == "compound_delta":
                deltas[compound] = value
        fastest_delta = min(deltas.values(), default=0.0)
        updated_compounds = set()
        for compound, value in deltas.items():
            if compound not in self._estimated_strategy_inputs:
                continue
            self._baseline_values[compound] = value - fastest_delta
            self._manual_baseline_compounds.add(compound)
            updated_compounds.add(compound)
        for compound, value in degradation.items():
            if compound not in self._estimated_strategy_inputs:
                continue
            self._degradation_values[compound] = value
            self._manual_degradation_compounds.add(compound)
            updated_compounds.add(compound)
        if not updated_compounds:
            return False
        for compound in updated_compounds:
            self._external_tyre_sources[compound] = f"Update history lap {leader_lap}"
        self._apply_strategy_input_order()
        self.tabs.setCurrentWidget(self.strategy_tab)
        self._save_state()
        self._append_log(
            f"Applied tyre degradation and compound delta from leader lap {leader_lap} "
            "to Strategy Prediction used values."
        )
        return True

    def _append_log(self, message: str) -> None:
        stamp = QtCore.QTime.currentTime().toString("HH:mm:ss")
        self.calculation_log.appendPlainText(f"[{stamp}] {message}")

    def _save_long_term_snapshot(self) -> None:
        if not self._working_plot_rows:
            QtWidgets.QMessageBox.information(
                self, "Save snapshot", "There is no current value-lap plot to save."
            )
            return
        try:
            self.service.save_named_snapshot(
                self.year,
                self.round_number,
                self.snapshot_name.text(),
                self._last_strategy_rows,
            )
            destination = self.service.save_history_plot(
                self.year,
                self.round_number,
                self.snapshot_name.text(),
                self._working_plot_rows,
            )
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Save snapshot", str(exc))
            return
        self._append_log(f"Saved long-term snapshot to {destination}")
        self._refresh_history_entries(selected_key=f"plot:{destination}")

    def _record_history_point(self, summary) -> None:
        leader_lap = int(float(summary.manifest.get("as_of_leader_lap") or 0))
        if not self._working_plot_rows and leader_lap > 0:
            self._working_plot_rows.extend(
                self._plot_rows_from_assumptions(
                    list(self._initial_strategy_inputs.values()), 0
                )
            )
        self._working_plot_rows = [
            row for row in self._working_plot_rows
            if int(float(row.get("as_of_leader_lap") or 0)) != leader_lap
        ]
        self._working_plot_rows.extend(
            self._plot_rows_from_assumptions(
                self.service.strategy_assumptions(summary), leader_lap
            )
        )
        self.history_snapshot_combo.blockSignals(True)
        self.history_snapshot_combo.setCurrentIndex(0)
        self.history_snapshot_combo.blockSignals(False)
        self._show_plot_rows(self._working_plot_rows)
        if self.auto_save_history and self._working_plot_rows:
            destination = self.service.save_history_plot(
                self.year,
                self.round_number,
                self._auto_history_name,
                self._working_plot_rows,
            )
            self._refresh_history_entries(
                selected_key="clear", preserve_display=True
            )
            self._append_log(f"Auto-saved live value-lap plot to {destination}")

    @staticmethod
    def _plot_rows_from_assumptions(assumptions, leader_lap: int):
        rows = []
        for assumption in assumptions:
            compound = str(assumption.get("compound") or "").upper()
            if not compound:
                continue
            rows.extend(
                [
                    {
                        "as_of_leader_lap": int(leader_lap),
                        "source_scope": "aggregate",
                        "parameter": "degradation",
                        "compound": compound,
                        "median": float(assumption["degradation"]),
                    },
                    {
                        "as_of_leader_lap": int(leader_lap),
                        "source_scope": "aggregate",
                        "parameter": "compound_delta",
                        "compound": compound,
                        "median": float(assumption["compound_delta"]),
                    },
                ]
            )
        return rows

    def _show_plot_rows(self, rows) -> None:
        self._displayed_history_rows = [dict(row) for row in rows]
        columns = ["as_of_leader_lap", "parameter", "compound", "median"]
        self._fill_table(self.history_table, self._displayed_history_rows, columns)
        self._plot_parameter_history(self._displayed_history_rows)

    def _refresh_history_entries(
        self, selected_key: str = "clear", *, preserve_display: bool = False
    ) -> None:
        if not hasattr(self, "history_snapshot_combo"):
            return
        self.history_snapshot_combo.blockSignals(True)
        self.history_snapshot_combo.clear()
        self.history_snapshot_combo.addItem("— Select saved plot —", "clear")
        selected_index = 0
        for plot in self.service.list_saved_history_plots(self.year, self.round_number):
            path = str(plot["path"])
            key = f"plot:{path}"
            label = str(plot["name"])
            if plot.get("saved_at"):
                label += f" — {plot['saved_at']}"
            self.history_snapshot_combo.addItem(label, key)
            if selected_key == key:
                selected_index = self.history_snapshot_combo.count() - 1
        self.history_snapshot_combo.setCurrentIndex(selected_index)
        self.history_snapshot_combo.blockSignals(False)
        if not preserve_display:
            self._history_selection_changed()

    def _history_selection_changed(self, *_args) -> None:
        selection = self.history_snapshot_combo.currentData()
        if selection == "clear" or selection is None:
            self._show_plot_rows([])
            return
        if str(selection).startswith("plot:"):
            try:
                payload = self.service.load_history_plot(
                    str(selection).split(":", 1)[1]
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._append_log(f"Could not load saved history snapshot: {exc}")
                return
            parameter_rows = payload.get("parameter_history", [])
        else:
            parameter_rows = []
        self._show_plot_rows(parameter_rows)

    def _connect_state_persistence(self) -> None:
        for control in (
            self.completed_lap, self.total_laps, self.compound, self.tyre_age,
            self.pit_loss, self.max_stops, self.driver, self.result_count,
            self.enforce_compound_order, self.auto_recalculate, self.sample_count,
            self.quick_threshold, self.min_clean_laps,
            self.exclude_stint_start_laps, self.exclude_stint_end_laps,
            self.traffic_source, self.min_gap_ahead, self.min_gap_behind,
            *self.clean_compound_checks.values(), self.tabs,
        ):
            if isinstance(control, QtWidgets.QAbstractButton):
                control.toggled.connect(self._save_state)
            elif isinstance(control, QtWidgets.QComboBox):
                control.currentIndexChanged.connect(self._save_state)
            elif isinstance(control, QtWidgets.QTabWidget):
                control.currentChanged.connect(self._save_state)
            else:
                control.valueChanged.connect(self._save_state)

    def _load_saved_state(self) -> None:
        if not self._state_path.is_file():
            self._append_log(f"State will be saved to {self._state_path}")
            return
        try:
            with self._state_path.open(encoding="utf-8") as handle:
                state = json.load(handle)
            if int(state.get("year")) != self.year or int(state.get("round_number")) != self.round_number:
                raise ValueError("state belongs to a different event")
            controls = state.get("controls", {})
            self.completed_lap.setValue(int(controls.get("completed_lap", self.completed_lap.value())))
            self.total_laps.setValue(int(controls.get("total_laps", self.total_laps.value())))
            self.compound.setCurrentText(str(controls.get("compound", self.compound.currentText())).upper())
            self.tyre_age.setValue(int(controls.get("tyre_age", self.tyre_age.value())))
            self.pit_loss.setValue(float(controls.get("pit_loss", self.pit_loss.value())))
            self.max_stops.setValue(int(controls.get("max_stops", self.max_stops.value())))
            self.result_count.setValue(int(controls.get("result_count", self.result_count.value())))
            self.sample_count.setValue(int(controls.get("live_sample_count", self.sample_count.value())))
            self.quick_threshold.setValue(float(controls.get("quick_threshold", self.quick_threshold.value())))
            self.min_clean_laps.setValue(int(controls.get("min_clean_laps", self.min_clean_laps.value())))
            self.exclude_stint_start_laps.setValue(
                int(controls.get("exclude_stint_start_laps", 0))
            )
            self.exclude_stint_end_laps.setValue(
                int(controls.get("exclude_stint_end_laps", 0))
            )
            traffic_source = controls.get("traffic_source")
            if traffic_source is None:
                traffic_source = (
                    "sector" if controls.get("sector_traffic_filter", False) else "automatic"
                )
            traffic_source_index = self.traffic_source.findData(str(traffic_source))
            self.traffic_source.setCurrentIndex(max(traffic_source_index, 0))
            self.min_gap_ahead.setValue(
                float(controls.get("min_gap_ahead_seconds", self.min_gap_ahead.value()))
            )
            self.min_gap_behind.setValue(
                float(controls.get("min_gap_behind_seconds", self.min_gap_behind.value()))
            )
            selected_compounds = {
                str(value).upper()
                for value in controls.get(
                    "clean_compounds", list(self.clean_compound_checks)
                )
            }
            for compound, checkbox in self.clean_compound_checks.items():
                checkbox.setChecked(compound in selected_compounds)
            self.auto_recalculate.setChecked(bool(controls.get("auto_recalculate", self.auto_recalculate.isChecked())))
            self.enforce_compound_order.setChecked(bool(controls.get("enforce_compound_order", self.enforce_compound_order.isChecked())))
            self.tabs.setCurrentIndex(int(controls.get("tab_index", self.tabs.currentIndex())))
            driver = controls.get("driver")
            self._restored_driver_code = str(driver) if driver else None
            baseline = state.get("manual_baseline", {})
            degradation = state.get("manual_degradation", {})
            for compound, value in baseline.items():
                compound = str(compound).upper()
                if compound in self._estimated_strategy_inputs:
                    self._baseline_values[compound] = float(value)
                    self._manual_baseline_compounds.add(compound)
            for compound, value in degradation.items():
                compound = str(compound).upper()
                if compound in self._estimated_strategy_inputs:
                    self._degradation_values[compound] = float(value)
                    self._manual_degradation_compounds.add(compound)
            self._external_tyre_sources = {
                str(compound).upper(): str(source)
                for compound, source in dict(state.get("external_tyre_sources", {})).items()
                if str(compound).upper() in self._estimated_strategy_inputs
            }
            saved_results = state.get("last_strategy_results", [])
            if self.persistence_mode == "durable" and isinstance(saved_results, list):
                self._last_strategy_rows = [
                    dict(row) for row in saved_results if isinstance(row, dict)
                ]
            self._apply_strategy_input_order()
            if self._last_strategy_rows:
                self._render_strategy_rows()
            self._append_log(f"Restored Strategy Prediction state from {self._state_path}")
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._append_log(f"Could not restore saved state: {exc}")

    def _save_state(self, *_args) -> None:
        if not self._state_ready:
            return
        state = {
            "schema_version": 1,
            "year": self.year,
            "round_number": self.round_number,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "controls": {
                "completed_lap": self.completed_lap.value(),
                "total_laps": self.total_laps.value(),
                "compound": self.compound.currentText(),
                "tyre_age": self.tyre_age.value(),
                "pit_loss": self.pit_loss.value(),
                "max_stops": self.max_stops.value(),
                "driver": self.driver.currentData(),
                "result_count": self.result_count.value(),
                "enforce_compound_order": self.enforce_compound_order.isChecked(),
                "auto_recalculate": self.auto_recalculate.isChecked(),
                "live_sample_count": self.sample_count.value(),
                "quick_threshold": self.quick_threshold.value(),
                "min_clean_laps": self.min_clean_laps.value(),
                "exclude_stint_start_laps": self.exclude_stint_start_laps.value(),
                "exclude_stint_end_laps": self.exclude_stint_end_laps.value(),
                "traffic_source": str(self.traffic_source.currentData()),
                "min_gap_ahead_seconds": self.min_gap_ahead.value(),
                "min_gap_behind_seconds": self.min_gap_behind.value(),
                "clean_compounds": list(self._selected_clean_compounds()),
                "tab_index": self.tabs.currentIndex(),
            },
            "manual_baseline": {
                compound: self._baseline_values[compound]
                for compound in sorted(self._manual_baseline_compounds)
                if compound in self._baseline_values
            },
            "manual_degradation": {
                compound: self._degradation_values[compound]
                for compound in sorted(self._manual_degradation_compounds)
                if compound in self._degradation_values
            },
            "external_tyre_sources": dict(self._external_tyre_sources),
        }
        if self.persistence_mode == "durable":
            state["last_strategy_results"] = self._last_strategy_rows
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._state_path.with_suffix(".json.tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
            temporary.replace(self._state_path)
        except OSError as exc:
            self._append_log(f"Could not save Strategy Prediction state: {exc}")

    @staticmethod
    def _fill_table(table, rows, columns):
        table.setColumnCount(len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column_index, column in enumerate(columns):
                table.setItem(
                    row_index, column_index,
                    QtWidgets.QTableWidgetItem(str(row.get(column, ""))),
                )
        table.resizeColumnsToContents()

    def closeEvent(self, event) -> None:
        self._save_state()
        self._executor.shutdown(wait=False, cancel_futures=True)
        super().closeEvent(event)
