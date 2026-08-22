from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import json
import time
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
import numpy as np
import pyqtgraph as pg

from wodata.contracts import StrategySearchState
from wodata.artifacts import weekend_model_root

from ..common.visual_styles import COMPOUND_COLOURS
from .service import OfflineAnalysisService
from .worker import AnalysisQueuePolicy, LiveAnalysisProcessService
from .tyre_inputs import TYRE_PARAMETERS, tyre_snapshot_from_overrides


WORKSPACE_DATA_ROOT = Path(__file__).resolve().parents[4] / "woData"
LIVE_DEFAULT_SAMPLE_COUNT = 5000


def _version_text(version: int | None) -> str:
    return "—" if version is None else f"V{version}"


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
        degradation_row = next(
            (
                row for row in rows
                if str(row.get("source_scope", "")).upper() == "AGGREGATE"
                and int(float(row.get("as_of_leader_lap") or 0)) == int(leader_lap)
                and str(row.get("parameter") or "") == "degradation"
                and str(row.get("compound") or "").upper() == compound
            ),
            {},
        )
        source_text = (
            " (Relative degradation assumption)"
            if degradation_row.get("degradation_source")
            == "relative_degradation_assumption"
            else ""
        )
        pace_text = (
            f"{deltas[compound] - reference_value:+.3f} s"
            if compound in deltas
            else "—"
        )
        lines.append(
            f"{compound}: degradation {degradation_text} | pace Δ {pace_text}{source_text}"
        )
    return "\n".join(lines)


def format_quality_hover_text(rows, *, leader_lap: int) -> str:
    selected = [row for row in rows if int(float(row.get("as_of_leader_lap") or 0)) == leader_lap]
    quality = next((row for row in selected if row.get("sampler_ess") is not None), None)
    if quality is None:
        return f"Leader lap {leader_lap} | model-quality data unavailable"
    evidence = quality.get("compound_evidence") or {}
    lines = [
        f"Leader lap {leader_lap}",
        f"ESS {float(quality['sampler_ess']):,.1f} / {int(quality['sample_count']):,} "
        f"({float(quality['ess_fraction']):.2%})",
        f"Max weight {float(quality['max_weight']):.3g}",
        f"Sampler status {quality.get('numerical_quality_status', 'unknown')}",
        f"Clean Race laps {int(quality['clean_race_laps'])}; runs {int(quality['clean_race_runs'])}",
    ]
    for compound in ("SOFT", "MEDIUM", "HARD"):
        item = evidence.get(compound, {})
        width = next((row.get("p10_p90_width") for row in selected
                      if row.get("parameter") == "degradation" and row.get("compound") == compound), None)
        width_text = f"; width {float(width):.3f} s/lap" if width is not None else ""
        lines.append(
            f"{compound}: {item.get('status', 'unknown')}; "
            f"{int(item.get('clean_laps', 0))} laps/{int(item.get('clean_runs', 0))} runs{width_text}"
        )
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
        telemetry_worker=None,
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
        self._telemetry_worker = telemetry_worker
        self._telemetry_build_options: dict[int, tuple[int, dict[str, object]]] = {}
        state_root = (
            Path(state_directory).expanduser()
            if state_directory is not None
            else weekend_model_root(self.year, self.round_number, self.service.data_root)
        )
        self._state_path = state_root / "strategy_prediction_state.json"
        self._state_ready = False
        self._restored_driver_code: str | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="strategy-search")
        self._fp_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fp-tyre-evidence")
        self._analysis_worker = LiveAnalysisProcessService(
            self.service.data_root,
            policy=self._analysis_queue_policy(),
        )
        self._sync_bounded_replay_pipeline_flow_control()
        self._future: Future | None = None
        self._fp_future: Future | None = None
        self._resolved_tyre_inputs = None
        self._cached_fp_evidence: dict[str, object] | None = None
        self._tyre_input_loading = False
        self._strategy_started_at: float | None = None
        self._reported_analysis_error: str | None = None
        self._current_model_summary = None
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
        self._race_informed_degradation_compounds: set[str] = set()
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
            "Show advisory tyre-order warnings"
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
        self.tyre_inputs_tab = self._build_tyre_inputs_tab()
        self.strategy_tab = self._build_strategy_tab()
        self.history_tab = self._build_history_tab()
        self.live_tab = self._build_live_tab()
        self.tabs.addTab(self.model_table, "Model summary")
        self.tabs.addTab(self.tyre_inputs_tab, "Tyre Inputs")
        self.tabs.addTab(self.strategy_tab, "Strategy predictions")
        self.tabs.addTab(self.history_tab, "Update history")
        self.tabs.addTab(self.live_tab, "Live recalculation")
        self.tabs.currentChanged.connect(self._strategy_tab_changed)
        layout.addWidget(self.status_label)
        layout.addWidget(self.tabs)
        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.setInterval(100)
        self._poll_timer.timeout.connect(self._poll_future)
        self._fp_poll_timer = QtCore.QTimer(self)
        self._fp_poll_timer.setInterval(100)
        self._fp_poll_timer.timeout.connect(self._poll_fp_evidence)
        if self.persistence_mode == "temporary":
            self.service.activate_temporary_replay_model(self.year, self.round_number)
        self._load_model()
        self._load_history()
        self._load_model_config()
        self._refresh_tyre_inputs()
        self._refresh_fp_evidence()
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

    def _build_tyre_inputs_tab(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)

        input_group = QtWidgets.QGroupBox("Strategy tyre inputs")
        input_layout = QtWidgets.QVBoxLayout(input_group)
        input_help = QtWidgets.QLabel(
            "Manual values are event-specific and authoritative. Blank means no override; "
            "use Clear to return a field to the Legacy FP-derived fallback."
        )
        input_help.setWordWrap(True)
        self.tyre_input_table = QtWidgets.QTableWidget()
        self.tyre_input_table.setColumnCount(6)
        self.tyre_input_table.setHorizontalHeaderLabels(
            ["Parameter", "Selected", "Source", "Manual", "Legacy", "Clear"]
        )
        self.tyre_input_table.itemChanged.connect(self._manual_tyre_input_changed)
        self.clear_all_tyre_inputs_button = QtWidgets.QPushButton("Clear all manual overrides")
        self.clear_all_tyre_inputs_button.clicked.connect(self._clear_all_manual_tyre_inputs)
        self.tyre_warning_label = QtWidgets.QLabel("")
        self.tyre_warning_label.setWordWrap(True)
        input_layout.addWidget(input_help)
        input_layout.addWidget(self.tyre_input_table)
        input_layout.addWidget(self.tyre_warning_label)
        input_layout.addWidget(self.clear_all_tyre_inputs_button, 0, QtCore.Qt.AlignmentFlag.AlignLeft)

        evidence_group = QtWidgets.QGroupBox("FP LongRun Evidence")
        evidence_layout = QtWidgets.QVBoxLayout(evidence_group)
        self.fp_evidence_status_label = QtWidgets.QLabel("FP evidence not calculated")
        self.fp_evidence_status_label.setWordWrap(True)
        self.fp_evidence_summary = QtWidgets.QPlainTextEdit()
        self.fp_evidence_summary.setReadOnly(True)
        self.fp_evidence_summary.setMaximumHeight(230)
        fp_controls = QtWidgets.QHBoxLayout()
        self.calculate_fp_evidence_button = QtWidgets.QPushButton("Calculate FP Evidence")
        self.recalculate_fp_evidence_button = QtWidgets.QPushButton("Recalculate FP Evidence")
        self.view_fp_evidence_button = QtWidgets.QPushButton("View LongRun Evidence…")
        self.recalculate_fp_evidence_button.setVisible(False)
        self.view_fp_evidence_button.setEnabled(False)
        self.calculate_fp_evidence_button.setToolTip(
            "Explicitly analyse available FP1–FP3 data. This may load/download data and take time."
        )
        self.recalculate_fp_evidence_button.setToolTip(
            "Explicitly reload available FP data and replace the cache only after success."
        )
        self.calculate_fp_evidence_button.clicked.connect(
            lambda: self._start_fp_evidence_calculation(force_refresh=False)
        )
        self.recalculate_fp_evidence_button.clicked.connect(
            lambda: self._start_fp_evidence_calculation(force_refresh=True)
        )
        self.view_fp_evidence_button.clicked.connect(self._view_longrun_evidence)
        fp_controls.addWidget(self.calculate_fp_evidence_button)
        fp_controls.addWidget(self.recalculate_fp_evidence_button)
        fp_controls.addWidget(self.view_fp_evidence_button)
        fp_controls.addStretch(1)
        evidence_layout.addWidget(self.fp_evidence_status_label)
        evidence_layout.addWidget(self.fp_evidence_summary)
        evidence_layout.addLayout(fp_controls)

        sign_help = QtWidgets.QLabel(
            "Performance convention: HARD vs MEDIUM = HARD pace − MEDIUM pace; "
            "SOFT vs MEDIUM = SOFT pace − MEDIUM pace. Positive means the first "
            "compound is slower; negative means it is faster."
        )
        sign_help.setWordWrap(True)
        layout.addWidget(input_group)
        layout.addWidget(sign_help)
        layout.addWidget(evidence_group)
        layout.addStretch(1)
        return widget

    @staticmethod
    def _tyre_parameter_label(parameter: str) -> str:
        return {
            "Deg_HARD": "HARD degradation",
            "Deg_MEDIUM": "MEDIUM degradation",
            "Deg_SOFT": "SOFT degradation",
            "Perf_HARD_vs_MEDIUM": "HARD vs MEDIUM pace",
            "Perf_SOFT_vs_MEDIUM": "SOFT vs MEDIUM pace",
        }[parameter]

    def _refresh_tyre_inputs(self, *, apply_to_strategy: bool = False) -> None:
        try:
            resolved = self.service.resolve_tyre_inputs(self.year, self.round_number)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._resolved_tyre_inputs = None
            self.tyre_input_table.setRowCount(0)
            self.tyre_warning_label.setText(f"Tyre inputs unavailable: {exc}")
            return
        self._resolved_tyre_inputs = resolved
        self._render_tyre_input_table()
        warnings = self.service.tyre_inputs.physical_order_warnings(resolved)
        self.tyre_warning_label.setText(
            "Advisory: " + " ".join(warnings) if warnings else "No physical-order warning."
        )
        if apply_to_strategy and self._current_model_summary is not None:
            self._manual_baseline_compounds.clear()
            self._manual_degradation_compounds.clear()
            self._load_strategy_inputs(self._current_model_summary, resolved=resolved)

    def _render_tyre_input_table(self) -> None:
        if self._resolved_tyre_inputs is None:
            return
        self._tyre_input_loading = True
        rows = self._resolved_tyre_inputs.by_parameter()
        self.tyre_input_table.setRowCount(len(TYRE_PARAMETERS))
        for row_index, parameter in enumerate(TYRE_PARAMETERS):
            row = rows[parameter]
            values = [
                self._tyre_parameter_label(parameter),
                f"{row.selected_value:+.4f}",
                "Manual" if row.selected_source == "manual" else "Legacy FP-derived fallback",
                "" if row.manual_value is None else f"{row.manual_value:.6g}",
                f"{row.legacy_value:+.4f}",
            ]
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                if column == 3:
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, parameter)
                    item.setBackground(QtGui.QColor("#fff4c2"))
                    item.setToolTip("Enter a finite number. Use Clear to remove the override.")
                else:
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                self.tyre_input_table.setItem(row_index, column, item)
            clear = QtWidgets.QPushButton("Clear")
            clear.setEnabled(row.manual_value is not None)
            clear.clicked.connect(
                lambda _checked=False, name=parameter: self._clear_manual_tyre_input(name)
            )
            self.tyre_input_table.setCellWidget(row_index, 5, clear)
        self.tyre_input_table.resizeColumnsToContents()
        self._tyre_input_loading = False

    def _manual_tyre_input_changed(self, item) -> None:
        if self._tyre_input_loading or item.column() != 3:
            return
        parameter = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if parameter not in TYRE_PARAMETERS:
            return
        text = item.text().strip()
        if not text:
            self.tyre_warning_label.setText(
                "Use the Clear button to explicitly remove a manual override."
            )
            self._render_tyre_input_table()
            return
        try:
            self.service.set_manual_tyre_input(
                self.year, self.round_number, str(parameter), float(text)
            )
        except (OSError, TypeError, ValueError) as exc:
            self.tyre_warning_label.setText(str(exc))
            self._render_tyre_input_table()
            return
        self._append_log(f"Manual tyre input saved: {parameter} = {text}")
        self._refresh_tyre_inputs(apply_to_strategy=True)

    def _clear_manual_tyre_input(self, parameter: str) -> None:
        try:
            self.service.clear_manual_tyre_input(
                self.year, self.round_number, parameter
            )
        except (OSError, TypeError, ValueError) as exc:
            self.tyre_warning_label.setText(str(exc))
            return
        self._append_log(f"Manual tyre input cleared: {parameter}; Legacy restored")
        self._refresh_tyre_inputs(apply_to_strategy=True)

    def _clear_all_manual_tyre_inputs(self) -> None:
        try:
            self.service.tyre_inputs.clear_all_manual_values(
                self.year, self.round_number
            )
        except (OSError, TypeError, ValueError) as exc:
            self.tyre_warning_label.setText(str(exc))
            return
        self._append_log("All manual tyre inputs cleared; Legacy restored")
        self._refresh_tyre_inputs(apply_to_strategy=True)

    def _refresh_fp_evidence(self) -> None:
        try:
            cache = self.service.get_cached_fp_tyre_evidence(
                self.year, self.round_number
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.fp_evidence_status_label.setText(f"FP evidence cache error: {exc}")
            return
        evidence = cache.evidence
        self._cached_fp_evidence = evidence
        if evidence is None:
            self.fp_evidence_status_label.setText("FP evidence not calculated")
            self.fp_evidence_summary.setPlainText(
                "Strategy Prediction remains available through Manual / Legacy inputs."
            )
            self.calculate_fp_evidence_button.setVisible(True)
            self.recalculate_fp_evidence_button.setVisible(False)
            self.view_fp_evidence_button.setEnabled(False)
            return
        status = (
            f"Calculated {evidence.get('calculated_at', '—')} using "
            + " + ".join(evidence.get("sessions_used") or ())
        )
        if cache.stale:
            status += f"\n⚠ FP data have changed since calculation: {cache.stale_reason}"
        self.fp_evidence_status_label.setText(status)
        contrasts = {
            str(row.get("parameter")): row
            for row in evidence.get("degradation_contrasts") or ()
        }
        performance = {
            str(row.get("parameter")): row
            for row in evidence.get("performance_support") or ()
        }
        lines = []
        for name, label in (("Delta_HM", "H − M"), ("Delta_SM", "S − M"), ("Delta_SH", "S − H")):
            row = contrasts.get(name)
            if not row:
                lines.append(f"{label}: unavailable")
                continue
            lines.extend(
                [
                    f"{label}: strict {float(row['strict_central']):+.3f} s/lap; "
                    f"P10–P90 {float(row['strict_p10']):+.3f} … {float(row['strict_p90']):+.3f}",
                    f"  one-gap {float(row['one_gap_central']):+.3f}; robust variability "
                    f"{float(row['robust_variability_central']):+.3f}; support "
                    f"{int(row['supporting_runs'])} runs / {int(row['supporting_teams'])} teams / "
                    f"{int(row['supporting_clean_laps'])} laps",
                ]
            )
        for name, label in (
            ("Perf_HARD_vs_MEDIUM", "HARD vs MEDIUM performance"),
            ("Perf_SOFT_vs_MEDIUM", "SOFT vs MEDIUM performance"),
        ):
            row = performance.get(name, {})
            if row.get("support_status") == "fp_longrun_direct":
                lines.append(f"{label}: {float(row['fp_direct_value']):+.3f} s")
            else:
                lines.append(f"{label}: No direct LongRun evidence")
        self.fp_evidence_summary.setPlainText("\n".join(lines))
        self.calculate_fp_evidence_button.setVisible(False)
        self.recalculate_fp_evidence_button.setVisible(True)
        self.view_fp_evidence_button.setEnabled(True)

    def _start_fp_evidence_calculation(self, *, force_refresh: bool) -> None:
        if self._fp_future is not None:
            return
        self.calculate_fp_evidence_button.setEnabled(False)
        self.recalculate_fp_evidence_button.setEnabled(False)
        self.fp_evidence_status_label.setText(
            "FP evidence calculation running… this may load/download FP data."
        )
        self._fp_future = self._fp_executor.submit(
            self.service.calculate_fp_tyre_evidence,
            self.year,
            self.round_number,
            force_refresh=force_refresh,
        )
        self._fp_poll_timer.start()

    def _poll_fp_evidence(self) -> None:
        if self._fp_future is None or not self._fp_future.done():
            return
        future, self._fp_future = self._fp_future, None
        self._fp_poll_timer.stop()
        self.calculate_fp_evidence_button.setEnabled(True)
        self.recalculate_fp_evidence_button.setEnabled(True)
        try:
            future.result()
        except Exception as exc:
            self._append_log(f"FP evidence calculation failed: {exc}")
            # A failed recalculation never deletes the last successful cache.
            if self._cached_fp_evidence is not None:
                self._refresh_fp_evidence()
                cached_status = self.fp_evidence_status_label.text()
                self.fp_evidence_status_label.setText(
                    f"FP evidence calculation failed: {exc}\n{cached_status}"
                )
            else:
                self.fp_evidence_status_label.setText(
                    f"FP evidence calculation failed: {exc}"
                )
            return
        self._append_log("FP evidence calculation completed")
        self._refresh_fp_evidence()
        self._refresh_tyre_inputs()

    def _view_longrun_evidence(self) -> None:
        evidence = self._cached_fp_evidence
        if not evidence:
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle("FP LongRun Evidence Details")
        dialog.resize(1100, 560)
        layout = QtWidgets.QVBoxLayout(dialog)
        table = QtWidgets.QTableWidget()
        columns = [
            "session", "team", "driver", "compound", "run_id", "stint",
            "lap_number", "tyre_age", "relative_pace", "strict_clean_lap",
            "exclusion_reason",
        ]
        rows = list(evidence.get("longrun_laps") or ())
        table.setColumnCount(len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column_index, column in enumerate(columns):
                item = QtWidgets.QTableWidgetItem(str(row.get(column, "")))
                item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                table.setItem(row_index, column_index, item)
        table.resizeColumnsToContents()
        layout.addWidget(table)
        dialog.show()
        self._longrun_evidence_dialog = dialog

    def _strategy_tab_changed(self, _index: int) -> None:
        if self.tabs.currentWidget() is self.tyre_inputs_tab:
            self._refresh_tyre_inputs(apply_to_strategy=True)
            self._refresh_fp_evidence()

    def _build_strategy_tab(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        assumptions_header = QtWidgets.QHBoxLayout()
        assumptions_header.addWidget(QtWidgets.QLabel("Tyre inputs used by strategy search"))
        self.load_tyre_file_button = QtWidgets.QPushButton("Load tyre info from file…")
        self.load_tyre_file_button.setVisible(False)
        assumptions_header.addStretch(1)
        assumptions_header.addWidget(self.load_tyre_file_button)
        self.strategy_input_table = QtWidgets.QTableWidget()
        self.strategy_input_table.setColumnCount(9)
        self.strategy_input_table.setHorizontalHeaderLabels(
            [
                "Compound", "Selected pre-race baseline\n(s vs fastest)",
                "Latest baseline\n(s vs fastest)", "Used baseline",
                "Used delta\n(vs MEDIUM)", "Selected pre-race degradation",
                "Latest degradation", "Used degradation", "Used source",
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
            "Edit event-specific engineering values in the Tyre Inputs tab. "
            "This table shows the exact current values used by Strategy search."
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
        self.model_quality_plot = pg.PlotWidget()
        self.model_quality_plot.setTitle("Model quality — sampler adequacy, not inference confidence")
        self.model_quality_plot.setLabel("left", "log10(ESS)")
        self.model_quality_plot.setLabel("bottom", "Leader lap")
        for plot in (
            self.degradation_history_plot,
            self.compound_delta_history_plot,
            self.model_quality_plot,
        ):
            plot.getAxis("left").setWidth(65)
        self.compound_delta_history_plot.setXLink(self.degradation_history_plot)
        self.model_quality_plot.setXLink(self.degradation_history_plot)
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
                self.model_quality_plot,
            )
        }
        self._history_hover_labels = {
            self.degradation_history_plot: None,
            self.compound_delta_history_plot: None,
            self.model_quality_plot: None,
        }
        self.history_table = QtWidgets.QTableWidget()
        layout.addLayout(save_row)
        layout.addWidget(self.history_header)
        layout.addWidget(self.degradation_history_plot, 1)
        layout.addWidget(self.compound_delta_history_plot, 1)
        layout.addWidget(self.model_quality_plot, 1)
        layout.addWidget(self.history_table, 1)
        self.save_snapshot_button.clicked.connect(self._save_long_term_snapshot)
        self.history_snapshot_combo.currentIndexChanged.connect(
            self._history_selection_changed
        )
        for plot in self._history_cursor_lines:
            plot.viewport().installEventFilter(self)
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

    def _load_model(self, *, summary=None, record_history: bool = False) -> None:
        try:
            if summary is None:
                summary = self.service.load_model(self.year, self.round_number)
        except Exception as exc:
            self.status_label.setText(f"Model unavailable: {exc}")
            self.generate_button.setEnabled(False)
            return
        self._current_model_summary = summary
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
        # Cross-process source of truth: reload the event artifact immediately
        # before calculation.  This is a local read and never triggers FP work.
        self._refresh_tyre_inputs(apply_to_strategy=True)
        if self._resolved_tyre_inputs is None:
            self.status_label.setText(
                "Strategy calculation blocked: resolve all required Manual / Legacy tyre inputs."
            )
            return
        if self.total_laps.value() <= self.completed_lap.value():
            QtWidgets.QMessageBox.warning(self, "Strategy", "Total laps must exceed completed lap.")
            return
        self._sync_strategy_overrides()
        tyre_input_snapshot = tyre_snapshot_from_overrides(
            self._manual_overrides, self._resolved_tyre_inputs
        )
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
            tyre_input_snapshot=tyre_input_snapshot,
            persist=self.persistence_mode == "durable",
            model_summary=self._current_model_summary,
        )
        self._strategy_started_at = time.perf_counter()
        self._poll_timer.start()

    def _poll_future(self) -> None:
        self._poll_telemetry_inputs()
        self._poll_analysis_worker()
        if self._future is None or not self._future.done():
            if self._future is None and not self._analysis_worker.analysis_running:
                if not self._telemetry_build_options:
                    self._poll_timer.stop()
            return
        future, self._future = self._future, None
        elapsed_ms = (
            (time.perf_counter() - self._strategy_started_at) * 1000.0
            if self._strategy_started_at is not None
            else 0.0
        )
        self._strategy_started_at = None
        self.generate_button.setEnabled(True)
        try:
            results = future.result()
        except Exception as exc:
            self.status_label.setText(f"Strategy calculation failed: {exc}")
            self._append_log(f"Strategy calculation failed after {elapsed_ms:.0f} ms: {exc}")
            return
        self._append_log(f"Strategy calculation completed in {elapsed_ms:.0f} ms")
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
                "model_version": self._analysis_worker.current_result_version,
            }
            for result in results
        ]
        self._render_strategy_rows()
        self._save_state()

    def _poll_analysis_worker(self) -> None:
        completed = self._analysis_worker.poll()
        if completed is not None:
            result = completed.result
            source_text = ", ".join(
                f"{session}={count}" for session, count in result.clean_lap_sources
            ) or "none"
            self._append_log(
                f"Analysis V{completed.model_version}, leader lap {result.leader_lap}: "
                f"clean laps {result.previous_clean_lap_count} → {result.eligible_lap_count}; "
                f"sources [{source_text}]; {result.status}; samples={result.sample_count}; "
                f"time={result.elapsed_seconds:.2f}s; "
                f"IPC={completed.ipc_elapsed_seconds * 1000.0:.0f}ms"
                + (f"; model={result.analysis_id}" if result.analysis_id else "")
            )
            if result.status == "updated":
                if self.persistence_mode == "temporary":
                    self.service.publish_temporary_worker_snapshot(
                        self.year, self.round_number, completed
                    )
                self._load_model(summary=completed.summary, record_history=True)
                self.status_label.setText(
                    f"Current model: V{completed.model_version} (leader lap {result.leader_lap})"
                )
            self._reported_analysis_error = None
        error = self._analysis_worker.last_error
        if error and error != self._reported_analysis_error:
            self._reported_analysis_error = error
            current = self._analysis_worker.current_result_version
            current_text = f"; retaining V{current}" if current is not None else ""
            self.status_label.setText(f"Live analysis failed{current_text}: {error}")
            self._append_log(f"Live analysis failed{current_text}: {error}")
        if self._analysis_worker.analysis_running:
            current = self._analysis_worker.current_result_version
            current_text = f"Current model V{current}. " if current is not None else ""
            if self._analysis_worker.policy is AnalysisQueuePolicy.FIFO_ALL:
                pending_text = (
                    f"; replay queue {self._analysis_worker.queue_depth} "
                    f"({_version_text(self._analysis_worker.oldest_queued_version)}–"
                    f"{_version_text(self._analysis_worker.newest_queued_version)})"
                    if self._analysis_worker.queue_depth
                    else ""
                )
            else:
                pending = self._analysis_worker.pending_input_version
                pending_text = f"; latest pending V{pending}" if pending is not None else ""
            self.status_label.setText(
                f"{current_text}Analysis: updating V"
                f"{self._analysis_worker.running_input_version}{pending_text}…"
            )

    def _poll_telemetry_inputs(self) -> None:
        if self._telemetry_worker is None:
            return
        for prepared in self._telemetry_worker.poll_analysis_inputs():
            pending = self._telemetry_build_options.pop(prepared.request_id, None)
            if pending is None:
                continue
            leader_lap, options = pending
            if prepared.error or prepared.analysis_input is None:
                self._append_log(
                    f"MC input preparation failed at leader lap {leader_lap}: "
                    f"{prepared.error or 'no input'}"
                )
                continue
            version = self._analysis_worker.submit(
                year=self.year,
                round_number=self.round_number,
                snapshot=prepared.analysis_input,
                options=options,
            )
            cut_text = (
                f"; cut=g{prepared.cut.session_generation}/"
                f"i{prepared.cut.ingress_sequence}/"
                f"t{prepared.cut.leader_lap_end_session_time:.3f}"
                if prepared.cut is not None
                else ""
            )
            self._append_log(
                f"Prepared MC input V{version} for leader lap {leader_lap} in "
                f"{prepared.elapsed_seconds * 1000.0:.0f} ms; "
                f"queue={self._analysis_worker.queue_depth}{cut_text}."
            )

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
        queued_laps = (
            list(range(max(previous_leader_lap + 1, 1), leader_lap + 1))
            if self._analysis_worker.policy is AnalysisQueuePolicy.FIFO_ALL
            else [leader_lap]
        )
        if self._telemetry_worker is not None:
            request_ids = [
                self._telemetry_worker.request_analysis_input(
                    leader_lap=queued_lap,
                    use_sector_boundary_traffic=bool(
                        options["use_sector_boundary_traffic"]
                    ),
                )
                for queued_lap in queued_laps
            ]
            self._telemetry_build_options.update(
                {
                    request_id: (queued_lap, dict(options))
                    for request_id, queued_lap in zip(request_ids, queued_laps)
                }
            )
            self._append_log(
                f"Queued {len(request_ids)} telemetry-to-MC input build(s) through "
                f"leader lap {leader_lap}."
            )
            self._poll_timer.start()
            return
        queued_snapshots = (
            self._leader_lap_snapshots(
                snapshot,
                first_lap=previous_leader_lap + 1,
                last_lap=leader_lap,
            )
            if self._analysis_worker.policy is AnalysisQueuePolicy.FIFO_ALL
            else [(leader_lap, snapshot)]
        )
        versions = [
            self._analysis_worker.submit(
                year=self.year,
                round_number=self.round_number,
                snapshot=queued_snapshot,
                options=options,
            )
            for _queued_lap, queued_snapshot in queued_snapshots
        ]
        if self._analysis_worker.policy is AnalysisQueuePolicy.FIFO_ALL:
            self._append_log(
                f"MC replay queue: requested V{versions[0]}–V{versions[-1]}; "
                f"running=V{self._analysis_worker.running_input_version}; "
                f"pending={self._analysis_worker.queue_depth}; "
                f"oldest={_version_text(self._analysis_worker.oldest_queued_version)}; "
                f"newest={_version_text(self._analysis_worker.newest_queued_version)}."
            )
        else:
            pending = self._analysis_worker.pending_input_version
            self._append_log(
                f"Requested analysis V{versions[-1]} for leader lap {leader_lap}"
                + (f"; latest pending is V{pending}." if pending else ".")
            )
        self._poll_timer.start()

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
                telemetry_samples = tuple(
                    sample for sample in snapshot.telemetry_samples
                    if int(sample.lap_number) <= leader_lap
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
                telemetry_samples = tuple(
                    sample for sample in snapshot.telemetry_samples
                    if float(sample.session_time_seconds) <= cutoff
                )
            snapshots.append(
                (
                    leader_lap,
                    replace(
                        snapshot,
                        completed_laps=completed_laps,
                        completed_sectors=completed_sectors,
                        telemetry_samples=telemetry_samples,
                        position_samples=(),
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

    def reset_replay_history(self) -> None:
        """Start each replay with a fresh in-memory value-lap plot."""
        if self.persistence_mode != "temporary":
            return
        self._restart_analysis_worker()
        self.service.activate_temporary_replay_model(self.year, self.round_number)
        self._working_plot_rows.clear()
        self._race_informed_degradation_compounds.clear()
        self._last_strategy_rows.clear()
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
        self._working_plot_rows.clear()
        self._show_plot_rows([])
        if persistence_mode == "temporary":
            self.reset_replay_history()
        else:
            self._restart_analysis_worker()
            self.service.reset_live_tracking()
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

    @staticmethod
    def _resolved_assumption_rows(resolved) -> list[dict[str, object]]:
        by_parameter = resolved.by_parameter()
        deltas = {
            "HARD": by_parameter["Perf_HARD_vs_MEDIUM"].selected_value,
            "MEDIUM": 0.0,
            "SOFT": by_parameter["Perf_SOFT_vs_MEDIUM"].selected_value,
        }
        fastest = min(deltas.values())
        rows = []
        for compound in ("SOFT", "MEDIUM", "HARD"):
            degradation = by_parameter[f"Deg_{compound}"]
            performance = (
                by_parameter.get(f"Perf_{compound}_vs_MEDIUM")
                if compound != "MEDIUM" else None
            )
            source = degradation.selected_source
            if performance is not None and performance.selected_source != source:
                source = f"D:{source}; P:{performance.selected_source}"
            rows.append(
                {
                    "compound": compound,
                    "baseline_pace": deltas[compound] - fastest,
                    "compound_delta": deltas[compound],
                    "compound_delta_available": True,
                    "degradation": degradation.selected_value,
                    "degradation_source": degradation.selected_source,
                    "compound_delta_source": (
                        performance.selected_source
                        if performance is not None else "reference"
                    ),
                    "degradation_race_informed": False,
                    "degradation_support_status": "pre_race_resolved",
                    "sessions": source,
                    "aggregation": "manual > legacy field resolver",
                }
            )
        return rows

    def _load_strategy_inputs(self, summary, *, resolved=None) -> None:
        if resolved is None:
            try:
                resolved = self.service.resolve_tyre_inputs(self.year, self.round_number)
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                self._append_log(f"Resolved tyre inputs unavailable: {exc}")
                # Keep the model visible for diagnosis, but do not invent a
                # missing required pre-race baseline.
                self.generate_button.setEnabled(False)
                return
        self._resolved_tyre_inputs = resolved
        self._reference_compound = "MEDIUM"
        initial = self._resolved_assumption_rows(resolved)
        self._initial_strategy_inputs = {
            str(row["compound"]): dict(row) for row in initial
        }
        current_informed = {
            str(row.get("compound") or "").upper()
            for row in summary.parameters
            if str(row.get("parameter") or "") == "degradation"
            and str(row.get("support_status") or "").lower() == "race_informed"
        }
        self._race_informed_degradation_compounds.update(current_informed)
        as_of_lap = int(float(summary.manifest.get("as_of_leader_lap") or 0))
        if as_of_lap == 0:
            assumptions = initial
        else:
            assumptions = self.service.strategy_assumptions(
                summary,
                baseline_degradation={
                    compound: float(row["degradation"])
                    for compound, row in self._initial_strategy_inputs.items()
                    if row.get("degradation") is not None
                },
                baseline_compound_delta={
                    compound: float(row["compound_delta"])
                    for compound, row in self._initial_strategy_inputs.items()
                    if row.get("compound_delta") is not None
                },
                race_informed_compounds=self._race_informed_degradation_compounds,
            )
        self._race_informed_degradation_compounds.update(
            str(row["compound"]).upper()
            for row in assumptions
            if row.get("degradation_race_informed")
        )
        self._estimated_strategy_inputs = {
            str(row["compound"]): dict(row) for row in assumptions
        }
        for compound, row in self._estimated_strategy_inputs.items():
            if compound not in self._manual_baseline_compounds:
                self._baseline_values[compound] = float(row["baseline_pace"])
            if compound not in self._manual_degradation_compounds:
                self._degradation_values[compound] = float(row["degradation"])
        # Model/history values are already the resolved source of truth. Do not
        # reorder them through the optional manual-input convenience control.
        self._sync_strategy_overrides()
        self._render_strategy_inputs()
        self.generate_button.setEnabled(True)

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
        # Manual engineering input is authoritative.  Physical ordering is a
        # warning only; never reorder, clamp, or silently replace values.
        if self.enforce_compound_order.isChecked() and self._resolved_tyre_inputs is not None:
            warnings = self.service.tyre_inputs.physical_order_warnings(
                self._resolved_tyre_inputs
            )
            if warnings:
                self.tyre_warning_label.setText("Advisory: " + " ".join(warnings))
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
                item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                self.strategy_input_table.setItem(row_index, column, item)
        self.strategy_input_table.resizeColumnsToContents()
        self._strategy_input_loading = False

    def _plot_parameter_history(self, rows=None) -> None:
        plots = {
            "degradation": self.degradation_history_plot,
            "compound_delta": self.compound_delta_history_plot,
        }
        for plot in (*plots.values(), self.model_quality_plot):
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
        grouped: dict[tuple[str, str], dict[int, tuple[float, str]]] = {}
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
            source = str(row.get("degradation_source") or "race_evidence")
            if source != "relative_degradation_assumption":
                source = "race_evidence"
            grouped.setdefault((parameter, compound), {})[lap] = (median, source)
        colours = {"SOFT": "#e10600", "MEDIUM": "#d9a900", "HARD": "#777777"}
        for (parameter, compound), values in sorted(grouped.items()):
            points = sorted(values.items())
            if not points:
                continue
            if parameter != "degradation":
                plots[parameter].plot(
                    [lap for lap, _ in points], [value[0] for _, value in points],
                    pen=pg.mkPen(colours.get(compound, "#3366cc"), width=2),
                    symbol="o", name=compound,
                )
                continue
            labelled = False
            for source, style in (
                ("relative_degradation_assumption", QtCore.Qt.PenStyle.DashLine),
                ("race_evidence", QtCore.Qt.PenStyle.SolidLine),
            ):
                selected = [(lap, value) for lap, value in points if value[1] == source]
                if not selected:
                    continue
                plots[parameter].plot(
                    [lap for lap, _ in selected], [value[0] for _, value in selected],
                    pen=pg.mkPen(colours.get(compound, "#3366cc"), width=2, style=style),
                    symbol="o",
                    symbolBrush=(None if source == "relative_degradation_assumption"
                                 else pg.mkBrush(colours.get(compound, "#3366cc"))),
                    symbolPen=pg.mkPen(colours.get(compound, "#3366cc"), width=1.5),
                    name=compound if not labelled else None,
                )
                labelled = True
        quality_points = {}
        for row in rows:
            try:
                lap = int(float(row.get("as_of_leader_lap") or 0))
                ess = float(row["sampler_ess"])
            except (KeyError, TypeError, ValueError):
                continue
            if ess > 0:
                quality_points[lap] = ess
        if quality_points:
            points = sorted(quality_points.items())
            self.model_quality_plot.plot(
                [point[0] for point in points], [np.log10(point[1]) for point in points],
                pen=pg.mkPen("#3366cc", width=2), symbol="o", name="ESS",
            )
        history_laps = [
            int(float(row.get("as_of_leader_lap") or 0))
            for row in rows if row.get("as_of_leader_lap") is not None
        ]
        if history_laps:
            x_min, x_max = min(history_laps), max(history_laps)
            if x_min == x_max:
                x_max = x_min + 1
            for plot in (*plots.values(), self.model_quality_plot):
                plot.setXRange(x_min, x_max, padding=0)

    def _history_mouse_moved(self, plot, scene_position) -> None:
        if not plot.plotItem.vb.sceneBoundingRect().contains(scene_position):
            self._clear_history_hover()
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
        if plot is self.model_quality_plot:
            return None
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
            text=(
                format_quality_hover_text(self._displayed_history_rows, leader_lap=leader_lap)
                if plot is self.model_quality_plot
                else format_history_hover_text(
                    self._displayed_history_rows,
                    leader_lap=leader_lap,
                    reference_compound=reference_compound,
                )
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

    def _clear_history_hover(self) -> None:
        """Clear every transient artist when the pointer leaves the data axes."""
        had_hover = self._hovered_history_lap is not None or any(
            label is not None for label in self._history_hover_labels.values()
        )
        for plot in self._history_hover_labels:
            self._clear_history_hover_label(plot)
        for cursor in self._history_cursor_lines.values():
            cursor.hide()
        self._hovered_history_lap = None
        if had_hover:
            self.history_header.setText(
                "Tyre model update history — hover to select a lap; left-click for actions"
            )

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Type.Leave and any(
            watched is plot.viewport() for plot in self._history_cursor_lines
        ):
            self._clear_history_hover()
        return super().eventFilter(watched, event)

    def _history_mouse_clicked(self, plot, event) -> None:
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return
        position = event.scenePos()
        if not plot.plotItem.vb.sceneBoundingRect().contains(position):
            return
        self._history_mouse_moved(plot, position)
        if self._hovered_history_lap is None:
            return
        if plot is self.model_quality_plot:
            return
        # Opening the context menu can emit a Leave event for the plot viewport.
        # That clears the transient hover state, so retain the selected lap locally
        # before the menu starts its nested event loop.
        selected_lap = self._hovered_history_lap
        menu = QtWidgets.QMenu(self)
        action = menu.addAction(
            f"Use lap {selected_lap} tyre values for strategy calculation"
        )
        selected = menu.exec(event.screenPos().toPoint())
        if selected is action:
            self._apply_history_lap_to_strategy(selected_lap)

    def _apply_history_lap_to_strategy(self, leader_lap: int | None) -> bool:
        try:
            selected_lap = int(float(leader_lap))
        except (TypeError, ValueError, OverflowError):
            return False
        selected_rows = [
            row for row in self._displayed_history_rows
            if str(row.get("source_scope", "")).upper() == "AGGREGATE"
            and int(float(row.get("as_of_leader_lap") or 0)) == selected_lap
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
            self._external_tyre_sources[compound] = f"Update history lap {selected_lap}"
        self._sync_strategy_overrides()
        self._render_strategy_inputs()
        self.tabs.setCurrentWidget(self.strategy_tab)
        self._save_state()
        self._append_log(
            f"Applied tyre degradation and compound delta from leader lap {selected_lap} "
            "to Strategy Prediction used values."
        )
        return True

    def _restart_analysis_worker(self) -> None:
        self._analysis_worker.shutdown(wait=False)
        self._telemetry_build_options.clear()
        self._analysis_worker = LiveAnalysisProcessService(
            self.service.data_root,
            policy=self._analysis_queue_policy(),
        )
        self._sync_bounded_replay_pipeline_flow_control()
        self._reported_analysis_error = None

    def _sync_bounded_replay_pipeline_flow_control(self) -> None:
        if self._telemetry_worker is None or not hasattr(
            self._telemetry_worker,
            "set_bounded_replay_mc_pipeline_capacity_probe",
        ):
            return
        probe = (
            self._bounded_replay_mc_pipeline_has_capacity
            if self.persistence_mode == "temporary"
            else None
        )
        self._telemetry_worker.set_bounded_replay_mc_pipeline_capacity_probe(probe)

    def _bounded_replay_mc_pipeline_has_capacity(self) -> bool:
        # This is bounded FIFO replay backpressure, not coalescing: every leader-lap
        # request is preserved, with one running and (by default) one queued behind it.
        config = getattr(self._telemetry_worker, "replay_pipeline_config", None)
        max_queued = int(getattr(config, "replay_mc_max_queued_requests", 1))
        return self._analysis_worker.queue_depth < max_queued

    def _analysis_queue_policy(self) -> AnalysisQueuePolicy:
        return (
            AnalysisQueuePolicy.FIFO_ALL
            if self.persistence_mode == "temporary"
            else AnalysisQueuePolicy.LATEST_WINS
        )

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
            initial = []
            for value in self._initial_strategy_inputs.values():
                row = dict(value)
                row["degradation_source"] = "relative_degradation_assumption"
                row["degradation_race_informed"] = False
                initial.append(row)
            self._working_plot_rows.extend(
                self._plot_rows_from_assumptions(initial, 0)
            )
        self._working_plot_rows = [
            row for row in self._working_plot_rows
            if int(float(row.get("as_of_leader_lap") or 0)) != leader_lap
        ]
        # _load_strategy_inputs resolved the current Race-informed and assumed
        # degradations once. Persist exactly those same values for plotting and
        # later Strategy Prediction selection.
        new_rows = self._plot_rows_from_assumptions(
            list(self._estimated_strategy_inputs.values()), leader_lap
        )
        quality = dict(summary.manifest.get("sampler_quality") or {})
        if quality:
            evidence = quality.get("compound_evidence") or {}
            common = {
                "sampler_ess": quality.get("ess"),
                "sample_count": quality.get("sample_count"),
                "ess_fraction": quality.get("ess_fraction"),
                "max_weight": quality.get("max_weight"),
                "top5_weight": quality.get("top5_weight"),
                "numerical_quality_status": quality.get("numerical_quality_status"),
                "clean_race_laps": quality.get("clean_race_laps"),
                "clean_race_runs": quality.get("clean_race_runs"),
                "compound_evidence": evidence,
            }
            for row in new_rows:
                row.update(common)
                compound = str(row.get("compound") or "").upper()
                parameter = str(row.get("parameter") or "")
                match = next((item for item in summary.parameters
                              if str(item.get("parameter")) == parameter
                              and str(item.get("compound") or "").upper() == compound), None)
                if match:
                    row["p10"] = match.get("p10")
                    row["p90"] = match.get("p90")
                    row["p10_p90_width"] = float(match["p90"]) - float(match["p10"])
        self._working_plot_rows.extend(new_rows)
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
            rows.append(
                {
                    "as_of_leader_lap": int(leader_lap),
                    "source_scope": "aggregate",
                    "parameter": "degradation",
                    "compound": compound,
                    "median": float(assumption["degradation"]),
                    "degradation_source": str(
                        assumption.get("degradation_source") or "model"
                    ),
                    "degradation_race_informed": bool(
                        assumption.get("degradation_race_informed", False)
                    ),
                    "degradation_support_status": str(
                        assumption.get("degradation_support_status") or ""
                    ),
                }
            )
            if assumption.get("compound_delta_available", True):
                rows.append(
                    {
                        "as_of_leader_lap": int(leader_lap),
                        "source_scope": "aggregate",
                        "parameter": "compound_delta",
                        "compound": compound,
                        "median": float(assumption["compound_delta"]),
                        "compound_delta_source": str(
                            assumption.get("compound_delta_source") or "model"
                        ),
                    }
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
            # Event-specific manual tyre values live only in
            # manual_tyre_inputs_v1.json.  Older dialog-local copies are
            # deliberately ignored to prevent Planner/live divergence.
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
        self._poll_timer.stop()
        self._fp_poll_timer.stop()
        if self._telemetry_worker is not None and hasattr(
            self._telemetry_worker,
            "set_bounded_replay_mc_pipeline_capacity_probe",
        ):
            self._telemetry_worker.set_bounded_replay_mc_pipeline_capacity_probe(None)
        self._analysis_worker.shutdown(wait=False)
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._fp_executor.shutdown(wait=False, cancel_futures=True)
        super().closeEvent(event)
