from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from enum import Enum
import json
import logging
import time
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
import numpy as np
import pyqtgraph as pg

from wodata.contracts import StrategySearchState
from wodata import get_data_root
from wodata.artifacts import weekend_model_root
from wodata.colours import TEAM_COLOURS, get_team_colour

from ..common.visual_styles import COMPOUND_COLOURS
from ..operational_inputs import load_operational_race_inputs
from .service import OfflineAnalysisService, leader_lap_snapshots
from .worker import AnalysisQueuePolicy, LiveAnalysisProcessService
from .tyre_inputs import TYRE_PARAMETERS, tyre_snapshot_from_overrides
from .live_sensitivity import live_calculation_tyre_inputs, validate_degradation_sensitivity
from ..live.undercut_widget import ObservedUndercutWidget


WORKSPACE_DATA_ROOT = Path(__file__).resolve().parents[4] / "woData"
LIVE_DEFAULT_SAMPLE_COUNT = 5000
STRATEGY_UNAVAILABLE_TEAM_COLOUR = "#9CA3AF"


def strategy_team_roster(data_root: Path, year: int, round_number: int) -> tuple[tuple[str, ...], str]:
    """Read local pre-event WCC order for display; never supply model inputs."""
    roster = tuple(TEAM_COLOURS)
    previous_round = int(round_number) - 1
    event = f"{int(year):04d}-{previous_round:02d}"
    runs = Path(data_root) / "woweekend" / "runs" / "schema_v1" / f"event={event}" / "workflow=post_race" / "runs"
    candidates = []
    for manifest_path in runs.glob("*/manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                not isinstance(manifest, dict)
                or not isinstance(manifest.get("outputs"), dict)
                or manifest["outputs"].get("standings") != "SUCCESS"
                or manifest.get("event", event) != event
            ):
                continue
            generated = datetime.fromisoformat(str(manifest["generated_at"]).replace("Z", "+00:00"))
            if generated.tzinfo is None:
                generated = generated.replace(tzinfo=timezone.utc)
            candidates.append((generated.timestamp(), manifest_path.parent.name, manifest_path.parent))
        except (OSError, KeyError, TypeError, ValueError):
            continue
    for _generated, _run_id, run in sorted(candidates, reverse=True):
        standings_path = run / "standings.json"
        try:
            payload = json.loads(standings_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("constructors"), dict):
                continue
            rows = payload["constructors"].get("actual")
            if not isinstance(rows, list) or not rows:
                continue
            latest_round = max(int(row["RoundNumber"]) for row in rows)
            if latest_round != previous_round:
                continue
            points = {}
            for row in rows:
                if int(row["RoundNumber"]) != latest_round:
                    continue
                team = row["Team"]
                value = float(row["CumulativePoints"])
                if not isinstance(team, str) or not team.strip() or not np.isfinite(value):
                    raise ValueError("Invalid constructors standings row")
                points[team.strip()] = value
            if not points:
                continue
            ordered = tuple(sorted(points, key=lambda team: -points[team]))
            ordered += tuple(team for team in roster if team not in points)
            return ordered, (
                f"Pre-race constructors standings: {year} round {previous_round}. "
                f"Source: {standings_path}. Unlisted teams follow roster order; display only."
            )
        except (OSError, KeyError, TypeError, ValueError, OverflowError):
            continue
    return roster, (
        f"Prior-round constructors standings unavailable for {year} round {previous_round}; "
        "using TEAM_COLOURS roster order (display only)."
    )


class StrategyPredictionMode(str, Enum):
    CURRENT_EVENT = "current_event"
    HISTORICAL_WHAT_IF = "historical_what_if"


def _version_text(version: int | None) -> str:
    return "—" if version is None else f"V{version}"


def _cut_text(cut) -> str:
    if cut is None:
        return "snapshot"
    return (
        f"g{cut.session_generation}/i{cut.ingress_sequence}/"
        f"t{cut.leader_lap_end_session_time:.3f}"
    )


def tyre_history_source_segments(rows) -> dict[tuple[str, str], tuple[dict, ...]]:
    """Build coordinate segments with explicit base/derived/direct states."""
    grouped: dict[tuple[str, str], dict[int, tuple[float, str]]] = {}
    maximum_lap = 0
    for row in rows:
        if str(row.get("source_scope", "")).upper() != "AGGREGATE":
            continue
        parameter = str(row.get("parameter") or "")
        compound = str(row.get("compound") or "").upper()
        if parameter not in {"degradation", "compound_delta"} or not compound:
            continue
        try:
            lap = int(float(row.get("as_of_leader_lap") or 0))
            value = float(row["median"])
        except (KeyError, TypeError, ValueError):
            continue
        source_key = (
            "degradation_source" if parameter == "degradation"
            else "compound_delta_source"
        )
        state = _history_source_state(parameter, row, source_key, lap)
        grouped.setdefault((parameter, compound), {})[lap] = (value, state)
        maximum_lap = max(maximum_lap, lap)
    output: dict[tuple[str, str], tuple[dict, ...]] = {}
    for coordinate, by_lap in grouped.items():
        points = sorted(by_lap.items())
        segments: list[dict[str, object]] = []
        start = 0
        while start < len(points):
            state = points[start][1][1]
            end = start + 1
            while end < len(points) and points[end][1][1] == state:
                end += 1
            selected = [(lap, value) for lap, (value, _state) in points[start:end]]
            if end < len(points):
                # The old source owns the connector into the first point of the
                # new source. This gives base→direct and derived→direct their
                # required dashed transition without extending a stale flat line.
                selected.append((points[end][0], points[end][1][0]))
            elif state == "base" and selected[-1][0] < maximum_lap:
                selected.append((maximum_lap, selected[-1][1]))
            segments.append({"source": state, "points": tuple(selected)})
            start = end
        output[coordinate] = tuple(segments)
    return output


def _history_source_state(parameter: str, row, source_key: str, lap: int) -> str:
    source = str(row.get(source_key) or "").lower()
    direct_sources = {"live_direct", "race_evidence", "race_model", "live_mc"}
    if parameter == "degradation":
        if source == "live_derived" or (
            source == "relative_degradation_assumption" and lap > 0
        ):
            return "live_derived"
        if source in direct_sources or bool(row.get("degradation_race_informed", False)):
            return "live_direct"
        return "base"
    return "live_direct" if source in direct_sources else "base"


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
            f" [{degradation_row.get('degradation_source')}]"
            if degradation_row.get("degradation_source") else ""
        )
        delta_row = next(
            (
                row for row in rows
                if str(row.get("source_scope", "")).upper() == "AGGREGATE"
                and int(float(row.get("as_of_leader_lap") or 0)) == int(leader_lap)
                and str(row.get("parameter") or "") == "compound_delta"
                and str(row.get("compound") or "").upper() == compound
            ),
            {},
        )
        delta_source = (
            f" [{delta_row.get('compound_delta_source')}]"
            if delta_row.get("compound_delta_source") else ""
        )
        pace_text = (
            f"{deltas[compound] - reference_value:+.3f} s"
            if compound in deltas
            else "—"
        )
        lines.append(
            f"{compound}: degradation {degradation_text}{source_text} | "
            f"pace Δ {pace_text}{delta_source}"
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
    session_closing = QtCore.Signal(object, object)

    def __init__(
        self,
        *,
        year: int,
        round_number: int,
        mode: StrategyPredictionMode,
        service: OfflineAnalysisService | None = None,
        state_directory: str | Path | None = None,
        persistence_mode: str = "durable",
        auto_save_history: bool = False,
        team_scope_enabled: bool = False,
        live_operational_controls: bool = False,
        initial_model_summary=None,
        telemetry_worker=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.year = int(year)
        self.round_number = int(round_number)
        self.strategy_mode = StrategyPredictionMode(mode)
        self.team_scope_enabled = bool(team_scope_enabled)
        self.live_operational_controls = bool(live_operational_controls)
        self._inactive_live_state: dict[str, object] = {}
        self._strategy_tyre_input_snapshot = None
        self._last_calculation_tyre_input_snapshot = None
        self._observed_undercut_signature = None
        self._operational_input_error: str | None = None
        self._operational_race_inputs = None
        if self.strategy_mode is StrategyPredictionMode.CURRENT_EVENT:
            try:
                operational = load_operational_race_inputs()
            except Exception as exc:
                self._operational_input_error = (
                    f"Current-event operational config could not be read: {exc}. "
                    "Restore a readable race.json and reopen Strategy Prediction."
                )
            else:
                if (
                    operational.season != self.year
                    or operational.round_number != self.round_number
                ):
                    self._operational_input_error = (
                        f"Current-event config is {operational.event_id or 'unidentified'}, "
                        f"but Strategy Prediction is {self.year:04d}-{self.round_number:02d}. "
                        "Select the matching event or update race.json, then reopen this window."
                    )
                else:
                    self._operational_race_inputs = operational
        self.service = service or OfflineAnalysisService(
            WORKSPACE_DATA_ROOT if WORKSPACE_DATA_ROOT.is_dir() else None
        )
        self._team_roster, self.team_standings_source = (
            strategy_team_roster(get_data_root(self.service.data_root), self.year, self.round_number)
            if self.team_scope_enabled else ((), "")
        )
        self._available_strategy_teams: set[str] = set()
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
        self._strategy_request_context: tuple[object, ...] | None = None
        self._reported_analysis_error: str | None = None
        self._current_model_summary = None
        self._live_snapshot = None
        # A reopened live dialog must not enqueue earlier cuts and replace the
        # restored model with lap-zero/early-lap evidence again.
        self._last_seen_leader_lap = (
            int(initial_model_summary.manifest.get("as_of_leader_lap") or 0)
            if initial_model_summary is not None else 0
        )
        self._manual_overrides: dict[tuple[str, str], float] = {}
        self._selected_team: str | None = None
        self._scope_manual_values: dict[str, dict[tuple[str, str], float]] = {}
        self._inactive_scope_state: dict[str, object] = {}
        self._scope_initial_inputs: dict[str, dict[str, dict[str, object]]] = {}
        self._scope_initial_degradation: dict[str, dict[str, float]] = {}
        self._loading_model = False
        self._strategy_input_loading = False
        self._manual_baseline_compounds: set[str] = set()
        self._manual_degradation_compounds: set[str] = set()
        self._baseline_values: dict[str, float] = {}
        self._degradation_values: dict[str, float] = {}
        self._estimated_strategy_inputs: dict[str, dict[str, object]] = {}
        self._initial_strategy_inputs: dict[str, dict[str, object]] = {}
        self._initial_degradation: dict[str, float] = {}
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
        self.history_info_tab = self._build_history_info_tab()
        self.tabs.addTab(self.history_info_tab, "History Info")
        self.tabs.currentChanged.connect(self._strategy_tab_changed)
        layout.addWidget(self.status_label)
        self.team_scope_widget = QtWidgets.QWidget(self)
        self.team_scope_widget.setToolTip(self.team_standings_source)
        scope_layout = QtWidgets.QVBoxLayout(self.team_scope_widget)
        scope_layout.setContentsMargins(0, 0, 0, 0)
        scope_row = QtWidgets.QHBoxLayout()
        scope_row.addWidget(QtWidgets.QLabel("Strategy tyre scope"))
        self.team_scope_label = QtWidgets.QLabel("Performance model: Global")
        scope_row.addWidget(self.team_scope_label)
        scope_row.addStretch(1)
        scope_layout.addLayout(scope_row)
        self.team_scope_grid = QtWidgets.QGridLayout()
        self.team_button_group = QtWidgets.QButtonGroup(self)
        self.team_button_group.setExclusive(True)
        self.team_button_group.buttonClicked.connect(
            lambda button: self._team_scope_changed(button.property("team"))
        )
        self.team_buttons: dict[str | None, QtWidgets.QPushButton] = {}
        self._build_team_scope_buttons(())
        scope_layout.addLayout(self.team_scope_grid)
        self.team_scope_widget.setVisible(self.team_scope_enabled)
        layout.addWidget(self.team_scope_widget)
        layout.addWidget(self.tabs)
        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.setInterval(100)
        self._poll_timer.timeout.connect(self._poll_future)
        self._fp_poll_timer = QtCore.QTimer(self)
        self._fp_poll_timer.setInterval(100)
        self._fp_poll_timer.timeout.connect(self._poll_fp_evidence)
        if self.persistence_mode == "temporary" and initial_model_summary is None:
            self.service.activate_temporary_replay_model(self.year, self.round_number)
        self._load_model(summary=initial_model_summary)
        self._load_history()
        self._load_model_config()
        self._refresh_tyre_inputs()
        self._refresh_fp_evidence()
        self._load_saved_state()
        self._state_ready = True
        self._connect_state_persistence()
        self._apply_operational_availability()
        if self.persistence_mode == "temporary":
            self._append_log(
                "Replay persistence: settings only; calculations, results, and new "
                "history remain temporary until Save snapshot long-term is used."
            )
        else:
            self._append_log("Durable persistence: live calculations and results are retained.")

    def _build_history_info_tab(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        help_label = QtWidgets.QLabel(
            "Latest five prior races at the same venue. Safety Car and Red Flag context only; "
            "pure VSC excluded. Approximate SC ranges cover laps active for any portion; "
            "withdrawal at a lap boundary excludes the next lap. Red Flags use entry laps."
        )
        help_label.setWordWrap(True)
        self.history_info_status = QtWidgets.QLabel()
        self.history_info_status.setWordWrap(True)
        self.history_info_table = QtWidgets.QTableWidget(0, 4)
        self.history_info_table.setHorizontalHeaderLabels(
            ["Edition", "Safety Car periods / laps", "Red Flags / laps", "Source coverage"]
        )
        self.history_info_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_info_table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        refresh = QtWidgets.QPushButton("Reload prepared history")
        refresh.clicked.connect(self._reload_history_info)
        layout.addWidget(help_label)
        layout.addWidget(self.history_info_status)
        layout.addWidget(self.history_info_table)
        layout.addWidget(refresh)
        self._reload_history_info()
        return widget

    def _reload_history_info(self):
        from wodata.interruption_history import load_history
        from wostrategy.analysis.interruption_history import history_rows
        self.historical_interruptions = load_history(
            self.year, self.round_number, self.service.data_root
        )
        payload = self.historical_interruptions
        rows = history_rows(payload)
        warnings = payload.get("selection", {}).get("warnings", [])
        self.history_info_status.setText(
            payload.get("reason") or "; ".join(warnings) or
            ("Prepared historical context; source coverage shown per edition." if rows else
             "No prior same-venue races found in the inspected schedules.")
        )
        self.history_info_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column, key in enumerate(("edition", "safety_car", "red_flag", "coverage")):
                self.history_info_table.setItem(
                    row_index, column, QtWidgets.QTableWidgetItem(row[key])
                )

    def _build_tyre_inputs_tab(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)

        input_group = QtWidgets.QGroupBox("Strategy tyre inputs")
        input_layout = QtWidgets.QVBoxLayout(input_group)
        input_help = QtWidgets.QLabel(
            "Manual values apply to the selected Strategy tyre scope for this event. "
            "Blank means no override; "
            "use Clear to return a field to the loaded pre-race/default value."
        )
        input_help.setWordWrap(True)
        self.tyre_input_table = QtWidgets.QTableWidget()
        self.tyre_input_table.setColumnCount(6)
        self.tyre_input_table.setHorizontalHeaderLabels(
            ["Parameter", "Selected", "Source", "Manual", "Default", "Clear"]
        )
        self.tyre_input_table.itemChanged.connect(self._manual_tyre_input_changed)
        self.clear_all_tyre_inputs_button = QtWidgets.QPushButton("Clear all Global manual overrides")
        self.clear_all_tyre_inputs_button.clicked.connect(self._clear_all_manual_tyre_inputs)
        self.tyre_warning_label = QtWidgets.QLabel("")
        self.tyre_warning_label.setWordWrap(True)
        input_layout.addWidget(input_help)
        self.import_pre_race_button = QtWidgets.QPushButton("Import tyre inputs from pre-race run…")
        self.import_pre_race_button.setToolTip(
            "Explicitly copy a successful woWeekend run into Global Planner manual inputs."
        )
        self.import_pre_race_button.clicked.connect(self._import_pre_race_tyre_inputs)
        self.tyre_import_origin_label = QtWidgets.QLabel("")
        self.tyre_import_origin_label.setWordWrap(True)
        input_layout.addWidget(self.import_pre_race_button)
        input_layout.addWidget(self.tyre_import_origin_label)
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
        self.import_pre_race_button.setEnabled(
            self._selected_team is None
            and not getattr(self, "live_operational_controls", self.team_scope_enabled)
        )
        self.tyre_import_origin_label.clear()
        self.strategy_import_origin_label.clear()
        try:
            resolved = self.service.resolve_tyre_inputs(
                self.year, self.round_number, team=self._selected_team
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._resolved_tyre_inputs = None
            self.tyre_input_table.setRowCount(0)
            self.tyre_warning_label.setText(f"Tyre inputs unavailable: {exc}")
            return
        self._resolved_tyre_inputs = resolved
        origin = resolved.semantics.get("manual_import_origin")
        if origin:
            description = (
                f"Persisted Planner manual baseline imported from woWeekend pre-race run: "
                f"{origin['event']} / {origin['session']} / {origin['run_id']}. "
                f"Still imported: {', '.join(origin['fields'])}. "
                f"Effective input SHA256: {origin['effective_input_sha256']}"
            )
            self.tyre_import_origin_label.setText(description)
            self.strategy_import_origin_label.setText(description)
        self._render_tyre_input_table()
        warnings = self.service.tyre_inputs.physical_order_warnings(resolved)
        self.tyre_warning_label.setText(
            "Advisory: " + " ".join(warnings) if warnings else "No physical-order warning."
        )
        if apply_to_strategy and self._current_model_summary is not None:
            self._load_strategy_inputs(self._current_model_summary, resolved=resolved)

    def _render_tyre_input_table(self) -> None:
        if self._resolved_tyre_inputs is None:
            return
        self._tyre_input_loading = True
        rows = self._resolved_tyre_inputs.by_parameter()
        imported = self._resolved_tyre_inputs.semantics.get("manual_import_origin", {}).get("fields", {})
        self.tyre_input_table.setRowCount(len(TYRE_PARAMETERS))
        for row_index, parameter in enumerate(TYRE_PARAMETERS):
            row = rows[parameter]
            values = [
                self._tyre_parameter_label(parameter),
                "—" if row.selected_value is None else f"{row.selected_value:+.4f}",
                {
                    "manual": "Manual",
                    "pre_race_prediction": "Pre-race/default",
                    "legacy_fp_derived": "Legacy fallback",
                    "missing": "Missing",
                }.get(row.selected_source, row.selected_source),
                "" if row.manual_value is None else f"{row.manual_value:.6g}",
                "—" if row.legacy_value is None else f"{row.legacy_value:+.4f}",
            ]
            if parameter in imported:
                values[2] = f"Manual · woWeekend ({imported[parameter]['source']})"
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

    def _import_pre_race_tyre_inputs(self) -> None:
        if self._selected_team is not None or getattr(self, "live_operational_controls", self.team_scope_enabled):
            return
        event = (self.year, self.round_number)
        try:
            runs = self.service.tyre_inputs.available_pre_race_runs(*event)
            if not runs:
                self.tyre_warning_label.setText("No complete, valid pre-race runs for this event.")
                return
            labels = [
                f"{run.run_id} | S/M/H degradation = "
                + " / ".join(f"{run.values['Deg_' + c]:.6g}" for c in ("SOFT", "MEDIUM", "HARD"))
                for run in runs
            ]
            selected, accepted = QtWidgets.QInputDialog.getItem(
                self, "Import pre-race tyre inputs",
                f"Select an immutable woWeekend run for {event[0]} R{event[1]} (R).\n"
                "This replaces all five Global manual tyre inputs in Planner.",
                labels, 0, False,
            )
            if not accepted:
                return
            if event != (self.year, self.round_number) or self._selected_team is not None or getattr(self, "live_operational_controls", self.team_scope_enabled):
                raise ValueError("Planner event/scope changed; select the run again.")
            run_id = runs[labels.index(selected)].run_id
            self.service.tyre_inputs.import_pre_race_run(*event, run_id)
        except (OSError, TypeError, ValueError) as exc:
            self.tyre_warning_label.setText(f"Pre-race import failed: {exc}")
            return
        self._active_manual_values().clear()
        self._restore_scope_manual_sets()
        self._refresh_tyre_inputs(apply_to_strategy=True)
        self._save_state()
        self._append_log(f"Imported woWeekend pre-race tyre inputs: {run_id}")

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
                self.year, self.round_number, str(parameter), float(text),
                team=self._selected_team,
            )
        except (OSError, TypeError, ValueError) as exc:
            self.tyre_warning_label.setText(str(exc))
            self._render_tyre_input_table()
            return
        self._append_log(f"Manual tyre input saved: {parameter} = {text}")
        self._discard_local_manual(str(parameter))
        self._refresh_tyre_inputs(apply_to_strategy=True)
        self._save_state()

    def _clear_manual_tyre_input(self, parameter: str) -> None:
        try:
            self.service.clear_manual_tyre_input(
                self.year, self.round_number, parameter, team=self._selected_team
            )
        except (OSError, TypeError, ValueError) as exc:
            self.tyre_warning_label.setText(str(exc))
            return
        self._append_log(f"Manual tyre input cleared: {parameter}; loaded default restored")
        self._discard_local_manual(parameter)
        self._refresh_tyre_inputs(apply_to_strategy=True)
        self._save_state()

    def _clear_all_manual_tyre_inputs(self) -> None:
        try:
            self.service.tyre_inputs.clear_all_manual_values(
                self.year, self.round_number, team=self._selected_team
            )
        except (OSError, TypeError, ValueError) as exc:
            self.tyre_warning_label.setText(str(exc))
            return
        self._append_log("All manual tyre inputs cleared; loaded defaults restored")
        self._active_manual_values().clear()
        self._restore_scope_manual_sets()
        self._refresh_tyre_inputs(apply_to_strategy=True)
        self._save_state()

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
                "Strategy Prediction remains available through Manual / pre-race inputs."
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
        self.strategy_input_table.setColumnCount(10 if self.live_operational_controls else 9)
        self.strategy_input_table.setHorizontalHeaderLabels(
            [
                "Compound", "Selected pre-race baseline\n(s vs fastest)",
                "Latest baseline\n(s vs fastest)", "Used baseline",
                "Used delta\n(vs MEDIUM)", "Selected pre-race degradation",
                "Latest degradation", "Used degradation", "Used source",
            ] + (["Final degradation\n(after sensitivity)"] if self.live_operational_controls else [])
        )
        self.strategy_input_table.setEditTriggers(
            (
                QtWidgets.QAbstractItemView.EditTrigger.DoubleClicked
                | QtWidgets.QAbstractItemView.EditTrigger.EditKeyPressed
            ) if self.team_scope_enabled
            else QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.strategy_input_table.setMaximumHeight(170)
        if self.team_scope_enabled:
            # Keep the consumed values and their provenance together in the
            # initial viewport; comparison columns remain available by scrolling.
            header = self.strategy_input_table.horizontalHeader()
            order = (0, 3, 4, 7, 9, 8, 1, 2, 5, 6) if self.live_operational_controls else (0, 3, 4, 7, 8, 1, 2, 5, 6)
            for position, column in enumerate(order):
                header.moveSection(header.visualIndex(column), position)
        input_help = QtWidgets.QLabel(
            (
                "Edit persistent values in Tyre Inputs, or edit Used values for this scope. "
                "Used values are the exact Strategy search inputs. Performance model: Global."
            ) if self.team_scope_enabled else (
                "Edit event-specific engineering values in the Tyre Inputs tab. "
                "This table shows the exact current values used by Strategy search."
            )
        )
        input_help.setWordWrap(True)
        if self.live_operational_controls:
            input_help.setText(
                "Edit persistent values in Tyre Inputs, or edit Used values for this scope. "
                "Final degradation = Used degradation × Live sensitivity. Performance deltas stay unchanged."
            )
        self.degradation_sensitivity = QtWidgets.QDoubleSpinBox()
        self.degradation_sensitivity.setRange(0.0, 10.0)
        self.degradation_sensitivity.setDecimals(3)
        self.degradation_sensitivity.setSingleStep(0.05)
        self.degradation_sensitivity.setSuffix(" ×")
        self.degradation_sensitivity.setValue(1.0)
        self.degradation_sensitivity.setToolTip(
            "Multiply final effective degradation in Global and Team modes, including manual Used values. "
            "This Live sensitivity does not change Tyre Inputs or MC fitting."
        )
        self.reset_degradation_sensitivity = QtWidgets.QPushButton("Reset to 1.0")
        self.reset_degradation_sensitivity.clicked.connect(lambda: self.degradation_sensitivity.setValue(1.0))
        self.degradation_sensitivity_status = QtWidgets.QLabel()
        sensitivity_widget = QtWidgets.QWidget()
        sensitivity_layout = QtWidgets.QHBoxLayout(sensitivity_widget)
        sensitivity_layout.setContentsMargins(0, 0, 0, 0)
        sensitivity_layout.addWidget(QtWidgets.QLabel("Live degradation sensitivity"))
        sensitivity_layout.addWidget(self.degradation_sensitivity)
        sensitivity_layout.addWidget(self.reset_degradation_sensitivity)
        sensitivity_layout.addWidget(self.degradation_sensitivity_status, 1)
        sensitivity_widget.setVisible(self.live_operational_controls)
        self.degradation_sensitivity.valueChanged.connect(self._degradation_sensitivity_changed)
        self.operational_status_label = QtWidgets.QLabel()
        self.operational_status_label.setWordWrap(True)
        controls = QtWidgets.QGridLayout()
        self.completed_lap = QtWidgets.QSpinBox()
        self.completed_lap.setRange(0, 200)
        self.completed_lap_override_enabled = QtWidgets.QCheckBox("Use override")
        self.completed_lap_override_enabled.setToolTip(
            "Use the entered completed lap for Strategy Prediction while retaining "
            "the latest available tyre and performance information."
        )
        self.total_laps = QtWidgets.QSpinBox()
        if self.strategy_mode is StrategyPredictionMode.CURRENT_EVENT:
            self.total_laps.setRange(0, 200)
            self.total_laps.setSpecialValueText("Unresolved")
            self.total_laps.setValue(
                self._operational_race_inputs.total_laps
                if self._operational_race_inputs is not None
                and self._operational_race_inputs.total_laps is not None
                else 0
            )
            self.total_laps.setReadOnly(True)
            self.total_laps.setButtonSymbols(
                QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons
            )
        else:
            # Historical analysis retains its established editable what-if.
            self.total_laps.setRange(1, 200)
            self.total_laps.setValue(57)
        self.compound = QtWidgets.QComboBox()
        self.compound.addItems(["SOFT", "MEDIUM", "HARD"])
        self.compound.setCurrentText("MEDIUM")
        self.tyre_age = QtWidgets.QSpinBox()
        self.tyre_age.setRange(0, 100)
        self.pit_loss = QtWidgets.QDoubleSpinBox()
        if self.strategy_mode is StrategyPredictionMode.CURRENT_EVENT:
            self.pit_loss.setRange(-1, 120)
            self.pit_loss.setSpecialValueText("Unresolved")
            self.pit_loss.setValue(
                self._operational_race_inputs.green_pit_loss
                if self._operational_race_inputs is not None
                and self._operational_race_inputs.green_pit_loss is not None
                else -1
            )
            self.pit_loss.setReadOnly(True)
            self.pit_loss.setButtonSymbols(
                QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons
            )
        else:
            self.pit_loss.setRange(0, 120)
            self.pit_loss.setValue(20)
        self.pit_loss.setSuffix(" s")
        self.max_stops = QtWidgets.QSpinBox()
        self.max_stops.setRange(0, 5)
        self.max_stops.setValue(2)
        self.rules_compliant = QtWidgets.QCheckBox("Rules compliant")
        self.rules_compliant.setChecked(False)
        self.rules_compliant.setToolTip(
            "Require at least two distinct dry compounds. Unchecked keeps unrestricted search."
        )
        self.manual_override = QtWidgets.QCheckBox("Manual Override")
        self.manual_override.setToolTip(
            "Ignore live MC tyre overlays and use Manual / pre-race values only."
        )
        self.driver = QtWidgets.QComboBox()
        self.driver.addItem("Pre-race / manual", None)
        self.result_count = QtWidgets.QSpinBox()
        self.result_count.setRange(1, 100)
        self.result_count.setValue(10)
        self.generate_button = QtWidgets.QPushButton("Calculate top 10")
        for index, (label, control) in enumerate((
            ("Completed lap", self.completed_lap), ("Total laps", self.total_laps),
            ("Compound", self.compound), ("Tyre age", self.tyre_age),
            ("Pit loss", self.pit_loss), ("Max stops", self.max_stops),
            ("Driver", self.driver), ("Results", self.result_count),
        )):
            row, column = divmod(index, 4)
            controls.addWidget(QtWidgets.QLabel(label), row, column * 2)
            controls.addWidget(control, row, column * 2 + 1)
        controls.addWidget(self.completed_lap_override_enabled, 0, 9)
        controls.addWidget(self.rules_compliant, 0, 8)
        controls.addWidget(self.manual_override, 1, 8)
        controls.addWidget(self.generate_button, 1, 9)
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
        self.strategy_import_origin_label = QtWidgets.QLabel("")
        self.strategy_import_origin_label.setWordWrap(True)
        layout.addWidget(self.strategy_import_origin_label)
        layout.addWidget(input_help)
        layout.addWidget(self.operational_status_label)
        layout.addWidget(self.strategy_input_table)
        layout.addWidget(sensitivity_widget)
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
        self.completed_lap_override_enabled.toggled.connect(
            self._completed_lap_override_changed
        )
        self.strategy_input_table.itemChanged.connect(self._strategy_input_changed)
        self.enforce_compound_order.toggled.connect(self._apply_strategy_input_order)
        self.manual_override.toggled.connect(self._manual_override_changed)
        self.load_tyre_file_button.clicked.connect(self._load_tyre_information_from_file)
        self._completed_lap_override_changed(False)
        self._degradation_sensitivity_changed()
        return widget

    def _live_sensitivity_factor(self) -> float:
        return self.degradation_sensitivity.value() if self.live_operational_controls else 1.0

    def _degradation_sensitivity_changed(self, *_args) -> None:
        factor = self._live_sensitivity_factor()
        active = factor != 1.0
        self.degradation_sensitivity_status.setText(
            f"ACTIVE: final degradation × {factor:.3f} in every scope"
            if active else "Baseline: final degradation unchanged"
        )
        self.degradation_sensitivity_status.setStyleSheet(
            "color: #92400e; background: #fef3c7; font-weight: bold; padding: 4px;"
            if active else ""
        )
        self._render_strategy_inputs()
        self._save_state()

    def _current_operational_block_reason(self) -> str | None:
        if self.strategy_mode is not StrategyPredictionMode.CURRENT_EVENT:
            return None
        if self._operational_input_error:
            return self._operational_input_error
        if self._operational_race_inputs is None:
            return "Current-event operational inputs are unavailable."
        missing = []
        if self._operational_race_inputs.total_laps is None:
            missing.append("race distance")
        if self._operational_race_inputs.green_pit_loss is None:
            missing.append("GREEN pit loss")
        if missing:
            return "Resolve the current-event " + " and ".join(missing) + " in race.json."
        return None

    def _apply_operational_availability(self) -> None:
        reason = self._current_operational_block_reason()
        if self.strategy_mode is StrategyPredictionMode.HISTORICAL_WHAT_IF:
            self.operational_status_label.setText(
                "Mode: Historical / what-if — race distance and pit loss are editable assumptions."
            )
            return
        if reason is None:
            self.operational_status_label.setText(
                "Mode: Current event — race distance and GREEN pit loss come from race.json."
            )
            return
        self.operational_status_label.setText(f"Mode: Current event — unavailable: {reason}")
        self.generate_button.setEnabled(False)

    def _manual_override_changed(self, _checked: bool) -> None:
        if self._current_model_summary is not None:
            self._load_strategy_inputs(self._current_model_summary)
        self._save_state()

    def _scope_key(self) -> str:
        return self._selected_team or "Global"

    def _active_manual_values(self) -> dict[tuple[str, str], float]:
        return self._scope_manual_values.setdefault(self._scope_key(), {})

    def _restore_scope_manual_sets(self) -> None:
        manual = self._active_manual_values()
        self._manual_baseline_compounds = {
            compound for parameter, compound in manual if parameter == "compound_delta"
        }
        self._manual_degradation_compounds = {
            compound for parameter, compound in manual if parameter == "degradation"
        }

    def _discard_local_manual(self, parameter: str) -> None:
        coordinate = (
            ("degradation", parameter.removeprefix("Deg_"))
            if parameter.startswith("Deg_") else ("compound_delta", parameter.split("_")[1])
        )
        self._active_manual_values().pop(coordinate, None)
        self._restore_scope_manual_sets()

    def _team_scope_changed(self, team: str | None) -> None:
        if self.team_scope_enabled and team is not None and team not in self._available_strategy_teams:
            return
        self._selected_team = team if self.team_scope_enabled else None
        if self._selected_team in self.team_buttons:
            self.team_buttons[self._selected_team].setChecked(True)
        self.clear_all_tyre_inputs_button.setText(f"Clear all {self._scope_key()} manual overrides")
        self._restore_scope_manual_sets()
        self._external_tyre_sources = {}
        self._refresh_tyre_inputs(apply_to_strategy=True)
        self._append_log(f"Strategy tyre scope: {self._scope_key()}; performance model: Global")
        self._save_state()

    def _build_team_scope_buttons(self, teams: tuple[str, ...]) -> None:
        if not self.team_scope_enabled:
            return
        while self.team_scope_grid.count():
            button = self.team_scope_grid.takeAt(0).widget()
            self.team_button_group.removeButton(button)
            button.deleteLater()
        self.team_buttons = {}
        self._available_strategy_teams = set(teams)
        ordered_teams = self._team_roster + tuple(sorted(set(teams).difference(self._team_roster)))
        scopes = (None, *ordered_teams)
        columns = (len(scopes) + 1) // 2
        for index, team in enumerate(scopes):
            button = QtWidgets.QPushButton(team or "Global")
            button.setCheckable(True)
            button.setProperty("team", team)
            button.setChecked(team == self._selected_team)
            available = team is None or team in self._available_strategy_teams
            button.setEnabled(available)
            colour = (
                (get_team_colour(team) if team is not None else "#475569") or "#475569"
                if available else STRATEGY_UNAVAILABLE_TEAM_COLOUR
            )
            tint = QtGui.QColor(colour)
            button.setStyleSheet(
                f"QPushButton {{ color: {colour}; border: {2 if available else 1}px solid {colour}; "
                "border-radius: 4px; padding: 3px 8px; } "
                f"QPushButton:checked {{ border: 3px solid {colour}; font-weight: bold; "
                f"background-color: rgba({tint.red()}, {tint.green()}, {tint.blue()}, 35); }}"
            )
            availability = (
                "Global is always available." if team is None else
                "Available: this snapshot contains team_degradation." if available else
                "Unavailable: this snapshot has no team_degradation; no team calculation is available."
            )
            button.setToolTip(f"{availability}\n{self.team_standings_source}")
            self.team_button_group.addButton(button)
            self.team_buttons[team] = button
            row, column = divmod(index, columns)
            self.team_scope_grid.addWidget(button, row, column)

    def _refresh_strategy_teams(self, summary) -> None:
        if not self.team_scope_enabled:
            return
        teams = self.service.strategy_teams(summary)
        selected = self._selected_team
        if selected is not None and selected not in teams:
            self._selected_team = None
            self.clear_all_tyre_inputs_button.setText("Clear all Global manual overrides")
            self._restore_scope_manual_sets()
            self._external_tyre_sources = {}
            self._append_log(
                f"Team {selected} is absent from the current model; Strategy scope reset to Global."
            )
            self._save_state()
        self._build_team_scope_buttons(teams)

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
            "Automatic Race traffic unavailable (model retained)", "automatic"
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
            "Automatic Race traffic is quarantined: gap decisions are not validated. "
            "Existing model state is retained without a new Race update. "
            "Sector-boundary mode remains available only when explicitly selected."
        )
        self.live_note.setWordWrap(True)
        self.calculation_log = QtWidgets.QPlainTextEdit()
        self.calculation_log.setReadOnly(True)
        layout.addLayout(controls)
        layout.addWidget(self.clean_definition_label)
        layout.addWidget(self.live_note)
        self.observed_undercut = ObservedUndercutWidget()
        self.observed_undercut.setVisible(self.live_operational_controls)
        layout.addWidget(self.observed_undercut)
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
            "and stint metadata. Automatic Race traffic is unavailable while position gaps "
            "remain unvalidated. Existing model state is retained. "
            "Sector-boundary traffic requires explicit selection; there is no automatic fallback."
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
            "exclusions → minimum run length. Automatic traffic remains quarantined. "
            "Explicit sector-boundary mode requires "
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
                    "automatic Race traffic unavailable; existing model retained, "
                    "no new Race update and no automatic fallback."
                )
            )
        )
        self._refresh_observed_undercut()

    def _refresh_observed_undercut(self) -> None:
        if not self.live_operational_controls or not hasattr(self, "observed_undercut"):
            return
        snapshot = self._live_snapshot
        if snapshot is None:
            return
        from .observed_undercut import build_observed_undercut_estimates
        from wodata.live.session_identity import parse_session_identity

        options = {
            "quick_lap_threshold": self.quick_threshold.value(),
            "allowed_compounds": self._selected_clean_compounds(),
            "use_sector_boundary_traffic": self._force_sector_traffic(),
            "min_gap_ahead_seconds": self.min_gap_ahead.value(),
            "min_gap_behind_seconds": self.min_gap_behind.value(),
        }
        summary = self._current_model_summary
        manifest = summary.manifest if summary is not None else {}
        signature = (
            snapshot.completed_laps, getattr(snapshot, "completed_sectors", ()), tuple(options.items()),
            parse_session_identity(getattr(snapshot, "session_info", {})).session_name.upper(),
            manifest.get("analysis_id"), manifest.get("status"),
            manifest.get("as_of_leader_lap"),
        )
        if signature == self._observed_undercut_signature:
            return
        try:
            estimates = build_observed_undercut_estimates(
                snapshot, model_summary=summary, **options,
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            self.observed_undercut.unavailable(str(exc))
        else:
            self.observed_undercut.render(estimates)
        self._observed_undercut_signature = signature
        self._save_state()

    def _load_model(self, *, summary=None, record_history: bool = False) -> None:
        try:
            if summary is None:
                summary = self.service.load_model(self.year, self.round_number)
        except Exception as exc:
            self.status_label.setText(f"Model unavailable: {exc}")
            self.generate_button.setEnabled(False)
            return
        self._current_model_summary = summary
        self._refresh_strategy_teams(summary)
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
        self._observed_undercut_signature = None
        self._refresh_observed_undercut()
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
        operational_block = self._current_operational_block_reason()
        if operational_block is not None:
            self.status_label.setText(
                f"Strategy calculation blocked: {operational_block}"
            )
            return
        # Consume any MC result that completed since the last timer tick before
        # freezing the model/input context for this search.
        self._poll_telemetry_inputs()
        self._poll_analysis_worker()
        # Cross-process source of truth: reload the event artifact immediately
        # before calculation.  This is a local read and never triggers FP work.
        self._refresh_tyre_inputs(apply_to_strategy=True)
        if self._resolved_tyre_inputs is None:
            self.status_label.setText(
                "Strategy calculation blocked: resolve all required Manual / pre-race tyre inputs."
            )
            return
        self._apply_latest_race_state()
        effective_completed_lap = self._effective_completed_lap()
        if self.total_laps.value() <= effective_completed_lap:
            QtWidgets.QMessageBox.warning(self, "Strategy", "Total laps must exceed completed lap.")
            return
        if self.pit_loss.value() < 0.0:
            QtWidgets.QMessageBox.warning(
                self, "Strategy", "Current-event GREEN pit loss is unresolved."
            )
            return
        self._sync_strategy_overrides()
        calculation_overrides = self._manual_overrides
        if self.live_operational_controls:
            calculation_overrides, tyre_input_snapshot = live_calculation_tyre_inputs(
                self._manual_overrides, self._resolved_tyre_inputs,
                factor=self._live_sensitivity_factor(),
                assumptions=list(self._estimated_strategy_inputs.values()),
                manual_values=self._active_manual_values(),
            )
        else:
            tyre_input_snapshot = tyre_snapshot_from_overrides(
                self._manual_overrides, self._resolved_tyre_inputs,
                assumptions=list(self._estimated_strategy_inputs.values()),
                manual_values=self._active_manual_values(),
            )
        state = StrategySearchState(
            effective_completed_lap, self.total_laps.value(), self.compound.currentText(),
            self.tyre_age.value(), selected_driver=self.driver.currentData(),
            max_future_stops=self.max_stops.value()
        )
        self.generate_button.setEnabled(False)
        self.status_label.setText("Strategy calculation running…")
        self._future = self._executor.submit(
            self.service.generate_continuations,
            year=self.year, round_number=self.round_number, state=state,
            pit_loss=self.pit_loss.value(), k=self.result_count.value(),
            overrides=calculation_overrides,
            tyre_input_snapshot=tyre_input_snapshot,
            persist=self.persistence_mode == "durable",
            model_summary=self._current_model_summary,
            rules_compliant=self.rules_compliant.isChecked(),
        )
        self._strategy_request_context = self._current_strategy_context()
        self._strategy_tyre_input_snapshot = tyre_input_snapshot
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
        self.generate_button.setEnabled(self._current_operational_block_reason() is None)
        try:
            results = future.result()
        except Exception as exc:
            self._strategy_request_context = None
            self.status_label.setText(f"Strategy calculation failed: {exc}")
            self._append_log(f"Strategy calculation failed after {elapsed_ms:.0f} ms: {exc}")
            return
        if self._strategy_request_context != self._current_strategy_context():
            self.status_label.setText(
                "Strategy result discarded: live race state or MC model advanced."
            )
            self._append_log(
                "Discarded stale Strategy result because its snapshot/model context changed."
            )
            self._strategy_request_context = None
            return
        self._strategy_request_context = None
        self._last_calculation_tyre_input_snapshot = self._strategy_tyre_input_snapshot
        self._append_log(f"Strategy calculation completed in {elapsed_ms:.0f} ms")
        self._last_strategy_rows = [
            {
                "rank": result.rank,
                "formatted_strategy": result.formatted_strategy,
                "compounds": list(result.plan.compounds),
                "pit_laps": list(result.plan.pit_laps),
                "remaining_cost": result.remaining_cost,
                "delta_to_best": result.delta_to_best,
                "current_lap": self._effective_completed_lap(),
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
            evidence_text = (
                f"new eligible laps {result.eligible_lap_count}; "
                f"retained evidence {result.previous_clean_lap_count}"
                if result.model_state_retained
                else f"clean laps {result.previous_clean_lap_count} → {result.eligible_lap_count}"
            )
            self._append_log(
                f"Analysis V{completed.model_version}: leader lap "
                f"{completed.prepared_leader_lap if completed.prepared_leader_lap is not None else result.leader_lap}, "
                f"cut={_cut_text(completed.prepared_cut)}; "
                f"{evidence_text}; "
                f"sources [{source_text}]; {result.status}; samples={result.sample_count}; "
                f"time={result.elapsed_seconds:.2f}s; "
                f"IPC={completed.ipc_elapsed_seconds * 1000.0:.0f}ms"
                + (f"; runtime={dict(result.runtime_breakdown)}" if result.runtime_breakdown else "")
                + (f"; model={result.analysis_id}" if result.analysis_id else "")
            )
            if result.exclusion_diagnostics:
                self._append_log(
                    "Clean-lap exclusions: "
                    + ", ".join(
                        f"{name}={count}"
                        for name, count in result.exclusion_diagnostics
                    )
                )
            if result.status == "traffic-unavailable":
                self.status_label.setText(
                    "Automatic Race traffic unavailable; existing model retained (no Race update)."
                )
                self._append_log(result.traffic_unavailable_reason or "Traffic unavailable; model retained.")
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
            self._analysis_worker.cancel_pending()
            self._telemetry_build_options.clear()
            if self._telemetry_worker is not None:
                self._telemetry_worker.cancel_analysis_inputs()
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
                f"{self._analysis_worker.running_input_version}{pending_text}; "
                f"historical cuts waiting {len(self._telemetry_build_options)}…"
            )
        elif self._telemetry_build_options:
            current = self._analysis_worker.current_result_version
            current_text = f"Current model V{current}. " if current is not None else ""
            self.status_label.setText(
                f"{current_text}Analysis: preparing historical inputs; "
                f"cuts waiting {len(self._telemetry_build_options)}…"
            )

    def _poll_telemetry_inputs(self) -> None:
        if self._telemetry_worker is None:
            return
        config = getattr(self._telemetry_worker, "replay_pipeline_config", None)
        max_queued = int(getattr(config, "replay_mc_max_queued_requests", 1))
        available = max(max_queued - self._analysis_worker.queue_depth, 0)
        if not self._analysis_worker.analysis_running:
            available = max(available, 1)
        for prepared in self._telemetry_worker.poll_analysis_inputs(
            max_results=available
        ):
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
                prepared_leader_lap=leader_lap,
                prepared_cut=prepared.cut,
            )
            self._append_log(
                f"Prepared V{version}: leader lap {leader_lap}, cut={_cut_text(prepared.cut)}; "
                f"{prepared.elapsed_seconds * 1000.0:.0f} ms; "
                f"queue={self._analysis_worker.queue_depth}."
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
        self._refresh_observed_undercut()
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
        if self._reported_analysis_error is not None:
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
        return leader_lap_snapshots(
            snapshot, first_lap=first_lap, last_lap=last_lap
        )

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
        self._observed_undercut_signature = None
        self.observed_undercut.render(())

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
        self._apply_observed_completed_lap(int(latest.lap_number))
        index = self.compound.findText(str(latest.compound).upper())
        if index >= 0:
            self.compound.setCurrentIndex(index)
        if latest.tyre_life is not None:
            self.tyre_age.setValue(max(int(round(float(latest.tyre_life))) - 1, 0))

    def _apply_latest_race_state(self) -> None:
        if self._live_snapshot is None:
            return
        driver = self.driver.currentData()
        candidates = [
            lap for lap in self._live_snapshot.completed_laps
            if lap.compound is not None and (driver is None or lap.driver_code == driver)
        ]
        if driver is None:
            leaders = [lap for lap in candidates if lap.position == 1]
            candidates = leaders or candidates
        if candidates:
            latest = max(candidates, key=lambda lap: (lap.lap_number, lap.driver_code))
            self._apply_observed_completed_lap(int(latest.lap_number))
            self.compound.setCurrentText(str(latest.compound).upper())
            if latest.tyre_life is not None:
                self.tyre_age.setValue(max(int(round(float(latest.tyre_life))) - 1, 0))
        scheduled = getattr(self._live_snapshot, "scheduled_total_laps", None)
        if (
            self.strategy_mode is StrategyPredictionMode.HISTORICAL_WHAT_IF
            and scheduled is not None
            and int(scheduled) > 0
        ):
            self.total_laps.setValue(int(scheduled))

    def _apply_observed_completed_lap(self, completed_lap: int) -> None:
        if not self.completed_lap_override_enabled.isChecked():
            self.completed_lap.setValue(int(completed_lap))

    def _observed_completed_lap(self) -> int | None:
        if self._live_snapshot is None:
            return None
        driver = self.driver.currentData()
        candidates = [
            lap for lap in self._live_snapshot.completed_laps
            if lap.compound is not None and (driver is None or lap.driver_code == driver)
        ]
        if driver is None:
            leaders = [lap for lap in candidates if lap.position == 1]
            candidates = leaders or candidates
        if not candidates:
            return None
        latest = max(candidates, key=lambda lap: (lap.lap_number, lap.driver_code))
        return int(latest.lap_number)

    def _effective_completed_lap(self) -> int:
        if self.completed_lap_override_enabled.isChecked():
            return self.completed_lap.value()
        observed = self._observed_completed_lap()
        return self.completed_lap.value() if observed is None else observed

    def _completed_lap_override_changed(self, enabled: bool) -> None:
        self.completed_lap.setReadOnly(not enabled)
        self.completed_lap.setButtonSymbols(
            QtWidgets.QAbstractSpinBox.ButtonSymbols.UpDownArrows
            if enabled
            else QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons
        )
        if not enabled:
            observed = self._observed_completed_lap()
            if observed is not None:
                self.completed_lap.setValue(observed)
        self._save_state()

    def _current_strategy_context(self) -> tuple[object, ...]:
        summary = self._current_model_summary
        manifest = summary.manifest if summary is not None else {}
        selected_driver = self.driver.currentData()
        leader_lap = 0
        if self._live_snapshot is not None:
            leader_lap = max(
                (int(lap.lap_number) for lap in self._live_snapshot.completed_laps if lap.position == 1),
                default=max(
                    (int(lap.lap_number) for lap in self._live_snapshot.completed_laps),
                    default=0,
                ),
            )
        return (
            None if self.completed_lap_override_enabled.isChecked() else leader_lap,
            self._effective_completed_lap(),
            self.total_laps.value(),
            self.compound.currentText() if selected_driver is not None else None,
            # Top-10 sequence search replaces the supplied tyre age with the
            # model start age; it is display/persistence state, not a consumed
            # optimisation input.
            None,
            selected_driver,
            self._selected_team,
            manifest.get("analysis_id"),
            manifest.get("as_of_leader_lap"),
            bool(self.manual_override.isChecked()),
            self._live_sensitivity_factor(),
        )

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
                    "aggregation": "manual > pre-race/default resolver",
                }
            )
        return rows

    def _load_strategy_inputs(self, summary, *, resolved=None) -> None:
        if resolved is None:
            try:
                resolved = self.service.resolve_tyre_inputs(
                    self.year, self.round_number, team=self._selected_team
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                self._append_log(f"Resolved tyre inputs unavailable: {exc}")
                # Keep the model visible for diagnosis, but do not invent a
                # missing required pre-race baseline.
                self.generate_button.setEnabled(False)
                return
        self._resolved_tyre_inputs = resolved
        self._render_tyre_input_table()
        try:
            resolved.strategy_overrides()
        except ValueError as exc:
            self._append_log(str(exc))
            self.generate_button.setEnabled(False)
            return
        self._reference_compound = "MEDIUM"
        initial = self._resolved_assumption_rows(resolved)
        scope = self._scope_key()
        if scope not in self._scope_initial_inputs:
            self._scope_initial_inputs[scope] = {
                str(row["compound"]): dict(row) for row in initial
            }
            ratio_values = {
                str(row["compound"]): row["degradation"] for row in initial
            }
            if self._selected_team is not None:
                ratio_values = {
                    name.removeprefix("Deg_"): item.legacy_value
                    for name, item in resolved.by_parameter().items()
                    if name.startswith("Deg_")
                }
            self._scope_initial_degradation[scope] = {
                compound: float(value) for compound, value in ratio_values.items()
                if value is not None
            }
        self._initial_strategy_inputs = self._scope_initial_inputs[scope]
        self._initial_degradation = self._scope_initial_degradation[scope]
        effective, assumptions = self.service.effective_tyre_inputs(
            summary,
            resolved,
            manual_override=self.manual_override.isChecked(),
            # Team ratios must use the automatic baseline, never Global manual.
            initial_degradation=self._initial_degradation,
            team=self._selected_team,
            manual_values=self._active_manual_values(),
        )
        self._estimated_strategy_inputs = {
            str(row["compound"]): dict(row) for row in assumptions
        }
        for compound, row in self._estimated_strategy_inputs.items():
            self._baseline_values[compound] = float(row["baseline_pace"])
            self._degradation_values[compound] = float(row["degradation"])
        self._manual_overrides = effective
        self._render_strategy_inputs()
        self.generate_button.setEnabled(self._current_operational_block_reason() is None)

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
            if not np.isfinite(value):
                raise ValueError("non-finite tyre input")
        except ValueError:
            self.status_label.setText("Tyre input must be numeric.")
            self._render_strategy_inputs()
            return
        field, compound = key
        if field == "baseline":
            self._baseline_values[compound] = value
            self._manual_baseline_compounds.add(compound)
            if compound == self._reference_compound:
                self._manual_baseline_compounds.update(self._baseline_values)
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
        manual = self._active_manual_values()
        for compound in self._manual_baseline_compounds:
            manual[("compound_delta", compound)] = (
                float(self._baseline_values[compound]) - float(reference_baseline)
            )
        for compound in self._manual_degradation_compounds:
            manual[("degradation", compound)] = float(self._degradation_values[compound])
        if self._current_model_summary is not None:
            self._load_strategy_inputs(self._current_model_summary, resolved=self._resolved_tyre_inputs)

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
                f"D: {estimated.get('degradation_value_source', estimated.get('degradation_source', 'base'))}"
                + ("\n" if self.team_scope_enabled else "; ") +
                f"P: {estimated.get('compound_delta_source', 'reference')} (Global model)",
            ]
            if self.live_operational_controls:
                values.append(f"{self._degradation_values[compound] * self._live_sensitivity_factor():.4f}")
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(str(value))
                field = {3: "baseline", 4: "delta", 7: "degradation"}.get(column)
                if (
                    self.team_scope_enabled and field is not None
                    and not (field == "delta" and compound == "MEDIUM")
                ):
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, (field, compound))
                    item.setBackground(QtGui.QColor("#fff4c2"))
                else:
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                self.strategy_input_table.setItem(row_index, column, item)
        self.strategy_input_table.resizeColumnsToContents()
        if self.team_scope_enabled:
            self.strategy_input_table.resizeRowsToContents()
            # A maximum alone lets the results table squeeze this down to one
            # visible row. Reserve all three compounds plus the horizontal bar.
            table = self.strategy_input_table
            height = (
                table.horizontalHeader().height() + table.verticalHeader().length()
                + 2 * table.frameWidth() + table.horizontalScrollBar().sizeHint().height()
            )
            table.setFixedHeight(height)
        self._strategy_input_loading = False

    def _plot_parameter_history(self, rows=None) -> None:
        started = time.perf_counter()
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
        grouped = tyre_history_source_segments(rows)
        colours = {"SOFT": "#e10600", "MEDIUM": "#d9a900", "HARD": "#777777"}
        for (parameter, compound), segments in sorted(grouped.items()):
            for segment in segments:
                selected = segment["points"]
                if not selected:
                    continue
                source = str(segment["source"])
                direct = source == "live_direct"
                derived = parameter == "degradation" and source == "live_derived"
                source_label = {
                    "base": "base",
                    "live_derived": "live-derived",
                    "live_direct": "live-direct",
                }.get(source, source)
                plots[parameter].plot(
                    [lap for lap, _ in selected], [value for _, value in selected],
                    pen=pg.mkPen(
                        colours.get(compound, "#3366cc"), width=2,
                        style=(QtCore.Qt.PenStyle.SolidLine if direct
                               else QtCore.Qt.PenStyle.DashLine),
                    ),
                    symbol=("o" if direct or derived else None),
                    symbolBrush=(
                        pg.mkBrush(colours.get(compound, "#3366cc"))
                        if direct else None
                    ),
                    symbolPen=pg.mkPen(colours.get(compound, "#3366cc"), width=1.5),
                    name=f"{compound} — {source_label}",
                )
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
        logging.getLogger(__name__).debug(
            "Tyre history redraw: rows=%d coordinates=%d time=%.1fms",
            len(rows), len(grouped), (time.perf_counter() - started) * 1000.0,
        )

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
        # MC backlog must not stop replay-state reduction and race-state plots.
        # Prepared immutable cuts wait in the telemetry result queue and enter
        # the existing bounded process FIFO incrementally.
        self._telemetry_worker.set_bounded_replay_mc_pipeline_capacity_probe(None)

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
        # History remains a Global model consumer. Dialog-local Used edits
        # belong only to Strategy, regardless of the currently selected scope.
        resolved = self.service.resolve_tyre_inputs(self.year, self.round_number)
        initial_inputs = self._scope_initial_inputs.get("Global", {})
        _, assumptions = self.service.effective_tyre_inputs(
            summary, resolved, manual_override=self.manual_override.isChecked(),
            initial_degradation={
                compound: float(row["degradation"])
                for compound, row in initial_inputs.items()
                if row.get("degradation") is not None
            },
        )
        # Keep current-main history in the same normalized coordinates as
        # effective inputs; scope-local Used edits remain excluded.
        if not self._working_plot_rows and leader_lap > 0:
            initial = []
            for value in initial_inputs.values():
                row = dict(value)
                row["degradation_source"] = "base"
                row["degradation_race_informed"] = False
                row["degradation_support_status"] = "base"
                initial.append(row)
            self._working_plot_rows.extend(
                self._plot_rows_from_assumptions(initial, 0)
            )
        self._working_plot_rows = [
            row for row in self._working_plot_rows
            if int(float(row.get("as_of_leader_lap") or 0)) != leader_lap
        ]
        # Keep the existing Global history independent of the Strategy selector.
        new_rows = self._plot_rows_from_assumptions(
            assumptions, leader_lap
        )
        quality = dict(summary.manifest.get("sampler_quality") or {})
        race_delta_count = sum(
            row.get("compound_delta_source") == "race_evidence" for row in assumptions
        )
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
                if any(
                    str(row.get(source, "")).startswith("manual")
                    for source in (
                        "degradation_value_source", "degradation_source", "compound_delta_source"
                    )
                ):
                    continue
                compound = str(row.get("compound") or "").upper()
                parameter = str(row.get("parameter") or "")
                match = next((item for item in summary.parameters
                              if str(item.get("parameter")) == parameter
                              and str(item.get("compound") or "").upper() == compound), None)
                if match is None:
                    continue
                shift = 0.0
                if parameter == "compound_delta":
                    if row.get("compound_delta_source") != "race_evidence" or race_delta_count < 2:
                        continue
                    # These are model marginal diagnostics translated by a
                    # fixed offset, not quantiles of a new compound contrast.
                    shift = float(row["median"]) - float(match["median"])
                    row["model_reference_shift"] = shift
                    row["bounds_basis"] = "fixed-shift model marginal diagnostics"
                elif row.get("degradation_source") != "live_direct" or not np.isclose(
                    float(row["median"]), float(match["median"]), rtol=0., atol=1e-12
                ):
                    continue
                for bound in ("p10", "p90"):
                    row[bound] = (
                        None if match.get(bound) is None else float(match[bound]) + shift
                    )
                if row["p10"] is not None and row["p90"] is not None:
                    row["p10_p90_width"] = row["p90"] - row["p10"]
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
                    "degradation_value_source": str(
                        assumption.get("degradation_value_source")
                        or assumption.get("degradation_source") or "model"
                    ),
                    "degradation_race_informed": bool(
                        assumption.get("degradation_race_informed", False)
                    ),
                    "degradation_support_status": str(
                        assumption.get("degradation_support_status") or ""
                    ),
                    "degradation_base_source": str(
                        assumption.get("degradation_base_source") or ""
                    ),
                    "degradation_anchor_compound": assumption.get(
                        "degradation_anchor_compound"
                    ),
                    "degradation_initial_ratio_to_medium": assumption.get(
                        "degradation_initial_ratio_to_medium"
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
        for row in self._displayed_history_rows:
            row["source"] = row.get(
                "degradation_source" if row.get("parameter") == "degradation"
                else "compound_delta_source",
                "",
            )
        columns = ["as_of_leader_lap", "parameter", "compound", "median", "source"]
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
        controls = [
            self.completed_lap, self.completed_lap_override_enabled,
            self.compound, self.tyre_age,
            self.max_stops, self.driver, self.result_count,
            self.rules_compliant, self.manual_override,
            self.enforce_compound_order, self.auto_recalculate, self.sample_count,
            self.quick_threshold, self.min_clean_laps,
            self.exclude_stint_start_laps, self.exclude_stint_end_laps,
            self.traffic_source, self.min_gap_ahead, self.min_gap_behind,
            *self.clean_compound_checks.values(), self.tabs,
        ]
        if self.strategy_mode is StrategyPredictionMode.HISTORICAL_WHAT_IF:
            controls.extend((self.total_laps, self.pit_loss))
        if self.live_operational_controls:
            controls.append(self.degradation_sensitivity)
        for control in controls:
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
            if not isinstance(state, dict):
                raise ValueError("saved state must be an object")
            if int(state.get("year")) != self.year or int(state.get("round_number")) != self.round_number:
                raise ValueError("state belongs to a different event")
            controls = state.get("controls", {})
            if not isinstance(controls, dict):
                raise ValueError("saved controls must be an object")
            if self.live_operational_controls:
                self.degradation_sensitivity.setValue(validate_degradation_sensitivity(
                    controls.get("live_degradation_sensitivity", 1.0)
                ))
                self._last_calculation_tyre_input_snapshot = state.get("last_live_tyre_input_snapshot")
            else:
                for key in ("last_live_tyre_input_snapshot", "live_observed_undercut"):
                    if key in state:
                        self._inactive_live_state[key] = state[key]
                if "live_degradation_sensitivity" in controls:
                    self._inactive_live_state["live_degradation_sensitivity"] = controls["live_degradation_sensitivity"]
            if self.team_scope_enabled:
                self._restore_scope_manual_values(state.get("scope_manual_values", {}))
            else:
                # The standard window must neither apply nor erase live-only
                # scope state when both entry points use the same state file.
                if "team" in controls:
                    self._inactive_scope_state["team"] = controls["team"]
                if "scope_manual_values" in state:
                    self._inactive_scope_state["scope_manual_values"] = state["scope_manual_values"]
            saved_team = controls.get("team") if self.team_scope_enabled else None
            if self.team_scope_enabled and (
                not isinstance(saved_team, (str, type(None))) or saved_team not in self.team_buttons
                or not self.team_buttons[saved_team].isEnabled()
            ):
                self._append_log(
                    f"Saved team {saved_team} is absent from the current model; Strategy scope reset to Global."
                )
                saved_team = None
            self._team_scope_changed(saved_team)
            self.completed_lap.setValue(
                int(controls.get("completed_lap", self.completed_lap.value()))
            )
            self.completed_lap_override_enabled.setChecked(
                bool(controls.get("completed_lap_override_enabled", False))
            )
            if self.strategy_mode is StrategyPredictionMode.HISTORICAL_WHAT_IF:
                self.total_laps.setValue(int(controls.get("total_laps", self.total_laps.value())))
            self.compound.setCurrentText(str(controls.get("compound", self.compound.currentText())).upper())
            self.tyre_age.setValue(int(controls.get("tyre_age", self.tyre_age.value())))
            if self.strategy_mode is StrategyPredictionMode.HISTORICAL_WHAT_IF:
                self.pit_loss.setValue(float(controls.get("pit_loss", self.pit_loss.value())))
            self.max_stops.setValue(int(controls.get("max_stops", self.max_stops.value())))
            self.result_count.setValue(int(controls.get("result_count", self.result_count.value())))
            self.rules_compliant.setChecked(bool(controls.get("rules_compliant", False)))
            self.manual_override.setChecked(bool(controls.get("manual_override", False)))
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
            # Persisted Tyre Inputs remain in manual_tyre_inputs_v1.json;
            # scope_manual_values stores only this window's explicit Used edits.
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

    def _restore_scope_manual_values(self, scoped) -> None:
        self._scope_manual_values = {}
        if not isinstance(scoped, dict):
            self._append_log("Ignored invalid saved scope manual values: expected an object.")
            return
        invalid = False
        for scope, rows in scoped.items():
            if not isinstance(scope, str) or not isinstance(rows, list):
                invalid = True
                continue
            values = {}
            for row in rows:
                try:
                    if not isinstance(row, dict):
                        raise ValueError("expected an object")
                    parameter, compound = row.get("parameter"), row.get("compound")
                    if (
                        not isinstance(parameter, str) or not isinstance(compound, str)
                        or parameter not in {"degradation", "compound_delta"}
                        or compound not in {"HARD", "MEDIUM", "SOFT"}
                        or isinstance(row.get("value"), bool)
                    ):
                        raise ValueError("invalid parameter or compound")
                    value = float(row["value"])
                    if not np.isfinite(value):
                        raise ValueError("non-finite value")
                    if parameter == "compound_delta" and compound == "MEDIUM" and value != 0.:
                        raise ValueError("MEDIUM is the fixed performance reference")
                    values[(parameter, compound)] = value
                except (KeyError, TypeError, ValueError, OverflowError):
                    invalid = True
            self._scope_manual_values[scope] = values
        if invalid:
            self._append_log("Ignored invalid saved scope manual entries.")

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
                "completed_lap_override_enabled": (
                    self.completed_lap_override_enabled.isChecked()
                ),
                "compound": self.compound.currentText(),
                "tyre_age": self.tyre_age.value(),
                "max_stops": self.max_stops.value(),
                "driver": self.driver.currentData(),
                "team": self._selected_team,
                "result_count": self.result_count.value(),
                "rules_compliant": self.rules_compliant.isChecked(),
                "manual_override": self.manual_override.isChecked(),
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
            "scope_manual_values": {
                scope: [
                    {"parameter": parameter, "compound": compound, "value": value}
                    for (parameter, compound), value in sorted(values.items())
                ]
                for scope, values in self._scope_manual_values.items()
            },
        }
        if not self.team_scope_enabled:
            state["controls"].pop("team")
            state.pop("scope_manual_values")
            if "team" in self._inactive_scope_state:
                state["controls"]["team"] = self._inactive_scope_state["team"]
            if "scope_manual_values" in self._inactive_scope_state:
                state["scope_manual_values"] = self._inactive_scope_state["scope_manual_values"]
        if self.live_operational_controls:
            state["controls"]["live_degradation_sensitivity"] = self._live_sensitivity_factor()
            state["last_live_tyre_input_snapshot"] = self._last_calculation_tyre_input_snapshot
            from wodata.live.session_identity import parse_session_identity

            session_name = parse_session_identity(
                getattr(self._live_snapshot, "session_info", {})
            ).session_name.upper()
            session_code = {"RACE": "R", "SPRINT": "S"}.get(session_name, session_name or "R")
            state["live_observed_undercut"] = {
                "schema_version": 1,
                "event": {"year": self.year, "round_number": self.round_number, "session": session_code},
                "estimates": list(self.observed_undercut.records),
            }
        else:
            if "live_degradation_sensitivity" in self._inactive_live_state:
                state["controls"]["live_degradation_sensitivity"] = self._inactive_live_state["live_degradation_sensitivity"]
            if "last_live_tyre_input_snapshot" in self._inactive_live_state:
                state["last_live_tyre_input_snapshot"] = self._inactive_live_state["last_live_tyre_input_snapshot"]
            if "live_observed_undercut" in self._inactive_live_state:
                state["live_observed_undercut"] = self._inactive_live_state["live_observed_undercut"]
        if self.strategy_mode is StrategyPredictionMode.HISTORICAL_WHAT_IF:
            state["controls"].update({
                "total_laps": self.total_laps.value(),
                "pit_loss": self.pit_loss.value(),
            })
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
        self.session_closing.emit(self.service, self._current_model_summary)
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
