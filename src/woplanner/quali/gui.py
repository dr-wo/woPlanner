"""Qt entry point for the qualifying monitor."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from wodata.colours import get_team_colour
from wodata.live import LiveConfig, ReplayConfig

from ..common.visual_styles import COMPOUND_COLOURS
from ..common.session_info import (
    is_sprint_qualifying_session,
    resolve_session_identity,
)
from .analysis import QualiAnalysisService
from .experimental_cutoff import calculate_half_live
from .models import FinishedReason
from .runtime_paths import ArtifactError, RuntimePaths, resolve_runtime_paths
from .service import QualiLiveService
from .track_evolution_presentation import readiness_messages
from .widgets import CutoffTable, QualiPlanDialog, format_time


WORKSPACE_DATA_ROOT = Path(__file__).resolve().parents[4] / "woData"
SLOW_LAP_BACKGROUND = "#b26a00"
PIT_STATUS_BACKGROUNDS = {
    "NOT ACTIVE": "#e0e0e0",
    "FIRE-UP": "#f0ad00",
    "PIT-LANE": "#607d8b",
    "QUEUE": "#d00000",
    "OUT": SLOW_LAP_BACKGROUND,
    "FAST": "#2e7d32",
    "SLOW": SLOW_LAP_BACKGROUND,
    "IN": "#1976d2",
    "IN-PIT": "#ffffff",
}


class PresentationCountdown:
    """Locally advancing display clock; it never changes Qualifying domain state."""

    def __init__(self) -> None:
        self._token = None
        self._remaining_s: float | None = None
        self._anchor_monotonic_s: float | None = None
        self._running = False

    def update(self, snapshot, *, now_s: float | None = None) -> float | None:
        now_s = time.monotonic() if now_s is None else now_s
        stopped = (
            snapshot.normal_finished
            or str(snapshot.session_status or "").lower()
            in {"aborted", "finished", "finalised", "ends", "inactive"}
        )
        token = (
            snapshot.session_part,
            snapshot.remaining_time_s,
            snapshot.clock_running,
            snapshot.clock_anchor_session_time_s,
            stopped,
        )
        if token != self._token:
            self._token = token
            self._remaining_s = snapshot.remaining_time_s
            self._anchor_monotonic_s = now_s
            self._running = bool(snapshot.clock_running and not stopped)
        if self._remaining_s is None:
            return None
        if not self._running or self._anchor_monotonic_s is None:
            return max(0.0, self._remaining_s)
        return max(0.0, self._remaining_s - (now_s - self._anchor_monotonic_s))


def format_countdown(value: float | None) -> str:
    if value is None:
        return "—:—"
    minutes, seconds = divmod(max(0, int(value)), 60)
    return f"{minutes}:{seconds:02d}"




class QualiWindow(QtWidgets.QMainWindow):
    COLUMNS = (
        "Pos",
        "Driver",
        "Team",
        "Tyre age",
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

    def __init__(self, *, runtime_paths: RuntimePaths | None = None) -> None:
        paths = runtime_paths or resolve_runtime_paths(WORKSPACE_DATA_ROOT if WORKSPACE_DATA_ROOT.is_dir() else None)
        super().__init__()
        self.setWindowTitle("woPlanner Qualifying Monitor")
        self.resize(1500, 900)
        self.runtime_paths = paths
        self.analysis_service = QualiAnalysisService(runtime_paths=paths)
        self._analysis_process = None
        self._analysis_timeout = QtCore.QTimer(self)
        self._analysis_timeout.setSingleShot(True)
        self._analysis_timeout.timeout.connect(self._cancel_fp_analysis)
        self.service = QualiLiveService(runtime_paths=paths)
        self.plan_dialog = None
        self._session_metadata_signature: tuple[object, ...] | None = None
        self._non_quali_warning_shown = False
        self._presentation_countdown = PresentationCountdown()

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
        self.table.horizontalHeader().setSectionResizeMode(
            3, QtWidgets.QHeaderView.ResizeMode.Interactive
        )
        self.table.horizontalHeader().resizeSection(3, 190)
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
        self.elimination_labels = {}
        for part in ("Q1", "Q2", "Q3"):
            spin = QtWidgets.QSpinBox()
            spin.setRange(0, 20)
            spin.setValue(6)
            spin.valueChanged.connect(
                lambda value, p=part: self._set_eliminated_count(p, value)
            )
            label = QtWidgets.QLabel(f"{part} eliminated")
            controls.addWidget(label)
            controls.addWidget(spin)
            self.elimination[part] = spin
            self.elimination_labels[part] = label
        # Q3 is a P1 target, not an elimination calculation.
        self.elimination["Q3"].hide()
        self.elimination_labels["Q3"].hide()
        controls.addStretch(1)
        return controls

    def _build_status_row(self) -> QtWidgets.QHBoxLayout:
        status = QtWidgets.QHBoxLayout()
        self.connection = QtWidgets.QLabel("Idle")
        self.clock = QtWidgets.QLabel("Q1 —:—")
        self.normal_finish = QtWidgets.QLabel("")
        self.cutoff = QtWidgets.QLabel("Projected cutoff: —")
        self.current_prediction_label = QtWidgets.QLabel("Current (legacy projection)")
        self.experimental_cutoff = QtWidgets.QLabel()
        self.experimental_cutoff.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.experimental_cutoff.setWordWrap(True)
        self.experimental_cutoff.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        self._clear_experimental_cutoff("Waiting for a qualifying snapshot.")
        predictions = QtWidgets.QVBoxLayout()
        predictions.setSpacing(2)
        current = QtWidgets.QHBoxLayout()
        current.addStretch(1)
        current.addWidget(self.clock)
        current.addWidget(self.normal_finish)
        current.addWidget(self.current_prediction_label)
        current.addWidget(self.cutoff)
        predictions.addLayout(current)
        predictions.addWidget(self.experimental_cutoff)
        self.final_cutoff_forecast = QtWidgets.QLabel()
        self.final_cutoff_forecast.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.final_cutoff_forecast.setWordWrap(True)
        predictions.addWidget(self.final_cutoff_forecast)
        self.q_only_track = QtWidgets.QLabel()
        self.q_only_track.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.q_only_track.setWordWrap(True)
        predictions.addWidget(self.q_only_track)
        self.readiness = QtWidgets.QLabel()
        self.readiness.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.readiness.setWordWrap(True)
        self.readiness.hide()
        predictions.addWidget(self.readiness)
        status.addWidget(self.connection)
        status.addLayout(predictions, 1)
        return status

    def _connect(self) -> None:
        self._prepare_session_start()
        try:
            self.service.start_live(
                LiveConfig(
                    self.year.value(),
                    self.round.value(),
                    "Q",
                    data_root=self.runtime_paths.output_root if self.runtime_paths.isolated else WORKSPACE_DATA_ROOT,
                )
            )
            self.timer.start()
            self.connection.setText("Connecting…")
        except Exception as exc:
            self._restore_offline_controls()
            QtWidgets.QMessageBox.critical(self, "Live timing", str(exc))

    def _start_replay(self) -> None:
        path = Path(self.replay_path.text()).expanduser()
        if not path.is_file():
            QtWidgets.QMessageBox.warning(
                self, "Replay", "Choose an existing replay file."
            )
            return
        self._prepare_session_start()
        try:
            self.service.configure_event_geometry(
                self.year.value(), self.round.value(),
                self.runtime_paths.resource_root if self.runtime_paths.isolated else WORKSPACE_DATA_ROOT
            )
            self.service.start_replay(ReplayConfig(path, 0.0))
            self.timer.start()
            self.connection.setText("Replaying…")
        except Exception as exc:
            self._restore_offline_controls()
            QtWidgets.QMessageBox.critical(self, "Replay", str(exc))

    def _browse(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Replay file")
        if path:
            self.replay_path.setText(path)

    def _analyze(self) -> None:
        if self.service.session_is_qualifying is False:
            self._show_non_quali_warning()
            return
        if self._analysis_process is not None:
            return
        event = (self.year.value(), self.round.value())
        paths = self.runtime_paths
        request = {"year": event[0], "round_number": event[1],
                   "resource_root": str(paths.resource_root) if paths.resource_root else None,
                   "output_root": str(paths.output_root) if paths.output_root else None,
                   "isolated": paths.isolated}
        process = QtCore.QProcess(self)
        self._analysis_process = process
        process.setProgram(sys.executable)
        process.setArguments(["-m", "woplanner.quali.fp_worker", json.dumps(request)])
        process.readyReadStandardError.connect(lambda: process.readAllStandardError())
        process.started.connect(process.closeWriteChannel)
        process.finished.connect(lambda code, status: self._fp_analysis_finished(process, event, code))
        process.errorOccurred.connect(lambda error: self._fp_analysis_start_error(process, error))
        self.analyze.setEnabled(False)
        self.analyze.setText("Analysing FP…")
        self._analysis_timeout.start(15 * 60 * 1000)
        process.start()

    def _fp_analysis_start_error(self, process, error) -> None:
        if error == QtCore.QProcess.ProcessError.FailedToStart and process is self._analysis_process:
            self._finish_fp_process(process)
            QtWidgets.QMessageBox.critical(self, "Pre-qualifying analysis", "Unable to start the FP preparation process.")

    def _finish_fp_process(self, process) -> None:
        self._analysis_timeout.stop()
        self._analysis_process = None
        self.analyze.setText("Analyse FP1–FP3")
        self.analyze.setEnabled(self.service.session_is_qualifying is not False)
        if process.state() == QtCore.QProcess.ProcessState.NotRunning:
            process.deleteLater()
        else:
            process.finished.connect(process.deleteLater)

    def _cancel_fp_analysis(self) -> None:
        process = self._analysis_process
        if process is not None:
            process.kill()
            self._finish_fp_process(process)
            QtWidgets.QMessageBox.critical(self, "Pre-qualifying analysis",
                "FP preparation exceeded 15 minutes and was stopped. Qualifying updates continue; authenticate outside this window before retrying fresh acquisition.")

    def _fp_analysis_finished(self, process, event, code) -> None:
        if process is not self._analysis_process:
            return
        contents = bytes(process.readAllStandardOutput()).decode("utf-8")
        self._finish_fp_process(process)
        if event != (self.year.value(), self.round.value()):
            # Saved artifacts retain their requested identity; never apply a
            # late preparation result to a different active event.
            return
        try:
            reply = json.loads(contents)
            if not isinstance(reply, dict):
                raise ValueError("Invalid FP worker reply")
        except ValueError:
            QtWidgets.QMessageBox.critical(self, "Pre-qualifying analysis",
                f"FP worker failed to return a valid result for {event[0]}/R{event[1]} "
                f"(worker phase; exit {code}). Qualifying updates continue.")
            return
        try:
            self.analysis_service.last_diagnostics = reply.get("diagnostics", {})
            self.analysis_service.diagnostic_path = (
                Path(reply["diagnostic_path"]) if reply.get("diagnostic_path") else None)
            if code != 0 or not reply.get("ok"):
                QtWidgets.QMessageBox.critical(self, "Pre-qualifying analysis",
                    reply.get("message", "FP preparation failed; qualifying forecasts remain available."))
                return
            result = self.analysis_service.load(*event)
            self._apply_fp_analysis(result, *event)
        except (OSError, ValueError, KeyError) as exc:
            self._show_fp_analysis_failure(exc)

    def _show_fp_analysis_failure(self, exc) -> None:
        QtWidgets.QMessageBox.critical(self, "Pre-qualifying analysis",
            self.analysis_service.failure_message(exc)
            if hasattr(self.analysis_service, "failure_message") else str(exc))

    def _apply_fp_analysis(self, result, year, round_number) -> None:
        try:
            rate, source, samples = self.analysis_service.effective_fp_track_rate()
            self.service.set_fp_track_rate(
                rate, loaded_fp=samples > 0,
                reference=f"{year}/R{round_number}: {source}",
            )
            self.service.load_operational_analysis(result)
            if self.runtime_paths.isolated:
                self.service.reload_operational_artifacts(year, round_number)
            if self.plan_dialog:
                self.plan_dialog.load_analysis(result)
            message = f"Analysed {len(result.laps)} FP laps and saved planner artifacts."
            diagnostics = getattr(self.analysis_service, "last_diagnostics", {}) or {}
            unavailable = [name for name, detail in diagnostics.get("sessions", {}).items()
                           if detail.get("error")]
            if unavailable:
                message += "\nSessions unavailable: " + ", ".join(unavailable) + ". Available sessions were analysed."
            warnings = diagnostics.get("warnings", ())
            if warnings:
                message += f"\nAnalysis completed with {len(warnings)} reference-detail warnings; see diagnostics for details."
            diagnostic_path = getattr(self.analysis_service, "diagnostic_path", None)
            if diagnostic_path:
                message += f"\nDiagnostics: {diagnostic_path}"
            QtWidgets.QMessageBox.information(
                self,
                "Pre-qualifying analysis",
                message,
            )
        except Exception as exc:
            self._show_fp_analysis_failure(exc)

    def _show_plan(self) -> None:
        if self.service.session_is_qualifying is False:
            self._show_non_quali_warning()
            return
        if self.analysis_service.result is None:
            try:
                self.analysis_service.load(self.year.value(), self.round.value())
            except ArtifactError as exc:
                QtWidgets.QMessageBox.critical(self, "Pre-qualifying analysis", str(exc))
                return
            except FileNotFoundError:
                if self.runtime_paths.isolated:
                    QtWidgets.QMessageBox.warning(self, "Pre-qualifying analysis", "No saved FP artifacts available.")
            except Exception as exc:
                if self.runtime_paths.isolated:
                    QtWidgets.QMessageBox.critical(self, "Pre-qualifying analysis", str(exc))
                    return
        if self.plan_dialog is None:
            self.plan_dialog = QualiPlanDialog(
                self.analysis_service, self.service, self
            )
            self.plan_dialog.finished.connect(
                lambda: setattr(self, "plan_dialog", None)
            )
        self.plan_dialog.show()
        self.plan_dialog.refresh_live_track(self.service.snapshot)
        self.plan_dialog.raise_()

    def _poll(self) -> None:
        try:
            snapshot, statuses, _ = self.service.poll()
        except ArtifactError as exc:
            self.service.stop()
            self.timer.stop()
            self._restore_offline_controls()
            self.connection.setText(f"FP artifact error: {exc}")
            QtWidgets.QMessageBox.critical(self, "Pre-qualifying analysis", str(exc))
            return
        for status in statuses:
            self.connection.setText(status.message)
            if (
                status.state in {"disconnected", "error"}
                and self.service.session_is_qualifying is not False
            ):
                self._restore_offline_controls()
        self._sync_session_context(self.service.received_session_info)
        if self.service.session_is_qualifying is False:
            self._reject_non_quali_session()
            return
        if self.service.session_is_qualifying is True:
            self.analyze.setEnabled(self._analysis_process is None)
            self.plan.setEnabled(True)
        remaining_text = format_countdown(self._presentation_countdown.update(snapshot))
        self.clock.setText(f"{snapshot.session_part} {remaining_text}")
        self._render_normal_finish(snapshot)
        self._render_cutoff_predictions(snapshot)
        self._render_timing_table(snapshot)
        self._render_pit_status()
        if self.plan_dialog is not None:
            self.plan_dialog.refresh_live_track(snapshot)

    def _render_cutoff_predictions(self, snapshot) -> None:
        # Keep the production label's existing publication and missing-data rule.
        projected_cutoff = snapshot.cutoff.projected_cutoff_s
        target_name = "Projected P1" if snapshot.cutoff.target_kind == "P1" else "Projected cutoff"
        if snapshot.cutoff.current_cutoff_s is None:
            self.cutoff.setText(f"{target_name}: —")
        else:
            self.cutoff.setText(
                f"{target_name}: {format_time(projected_cutoff)}"
            )
        experimental = calculate_half_live(snapshot)
        status = {
            "LIVE_SUPPORTED": "Live supported",
            "LIVE_LIMITED": "Live limited",
            "FALLBACK": "Fallback · FP/default prior",
            "UNAVAILABLE": "Unavailable",
        }[experimental.status]
        self.experimental_cutoff.setText(
            f"R&D · Half-live [Experimental] · {target_name}: "
            f"{format_time(experimental.projected_cutoff_s)} · {status}"
            + (" · Zero fallback; target unchanged"
               if experimental.status == "FALLBACK" and experimental.rate_s_per_field_push == 0
               else "")
        )
        self.experimental_cutoff.setToolTip(
            "Candidate: half_live (experimental; limited historical evaluation, "
            "not validated prospective performance).\n"
            "Independent of manual overrides: uses half the automatic fitted live "
            "rate, or the configured FP/default prior unchanged when no live fit is available.\n"
            f"Source: {experimental.source}\nStatus: {experimental.status}\n"
            f"{experimental.reason}"
        )
        messages = readiness_messages(snapshot.track_evolution_result, inputs_only=True)
        self.readiness.setText("\n".join(messages))
        self.readiness.setVisible(bool(messages))
        self._render_resilient_predictions(snapshot)

    def _render_resilient_predictions(self, snapshot) -> None:
        """Publish the distinct field forecast and Q-only measurement sources."""
        forecast = getattr(snapshot, "final_cutoff_forecast", None)
        self.final_cutoff_forecast.setVisible(snapshot.session_part in {"Q1", "Q2"})
        def clock(value):
            minutes, seconds = divmod(round(float(value), 1), 60)
            return f"{int(minutes)}:{seconds:04.1f}"
        if forecast and forecast.get("status") == "OBSERVED_FINAL":
            self.final_cutoff_forecast.setText(
                f"Final P{forecast['target_rank']}: {clock(forecast['estimate_s'])} · observed final classification"
            )
        elif forecast and forecast.get("estimate_s") is not None:
            self.final_cutoff_forecast.setText(
                f"Final P{forecast['target_rank']} forecast [Experimental]: "
                f"{clock(forecast['estimate_s'])} · historical scenario range "
                f"{clock(forecast['lower_s'])}–{clock(forecast['upper_s'])} · Q pace + historical prior"
            )
        elif forecast and forecast.get("status") == "SCENARIO_RANGE":
            self.final_cutoff_forecast.setText(
                f"Final P{forecast['target_rank']} forecast [Experimental]: "
                f"{clock(forecast['lower_s'])}–{clock(forecast['upper_s'])} · "
                "partial-field scenario envelope; no point estimate"
            )
        else:
            self.final_cutoff_forecast.setText("Final cutoff forecast [Experimental]: unavailable")
        self.final_cutoff_forecast.setToolTip(
            "Forward-looking final classification from joint historical field scenarios. "
            "Independent of FP preparation and the legacy/Half-live correction.\n"
            + (f"Source: {forecast['source']}\nTraining: {', '.join(forecast['training_events'])}\n"
               + "\n".join(forecast["limitations"]) if forecast else "Waiting for a qualifying publication.")
        )
        result = getattr(snapshot, "q_only_track_evolution_result", None)
        if result:
            rate = result["live_estimate_s_per_global_push"]
            trend = f"{rate:+.4f} s/field push" if rate is not None else "slope not yet identifiable"
            self.q_only_track.setText(
                f"Q-only Track Evolution: {trend} · {result['accepted_observation_count']} accepted / "
                f"{result['fitted_observation_count']} fitted observations"
                + (" · limited evidence" if result["status"] == "LIVE_LIMITED" else "")
            )
            self.q_only_track.setToolTip(result["assumption"])
        else:
            self.q_only_track.setText("Q-only Track Evolution: waiting for qualifying observations")

    def _clear_experimental_cutoff(self, reason: str) -> None:
        self.experimental_cutoff.setText("R&D · Half-live [Experimental] · Unavailable")
        self.experimental_cutoff.setToolTip(f"Candidate: half_live\n{reason}")
        if hasattr(self, "readiness"):
            self.readiness.clear()
            self.readiness.hide()
        if hasattr(self, "final_cutoff_forecast"):
            self.final_cutoff_forecast.setText("Final cutoff forecast [Experimental]: unavailable")
            self.final_cutoff_forecast.setToolTip(reason)
            self.q_only_track.setText("Q-only Track Evolution: waiting for qualifying observations")

    def _clear_session_prediction_display(self, reason: str) -> None:
        self.cutoff.setText("Projected cutoff: —")
        self.clock.setText("—:—")
        self.normal_finish.clear()
        self.normal_finish.setToolTip("")
        self._presentation_countdown = PresentationCountdown()
        self._clear_experimental_cutoff(reason)

    def _render_normal_finish(self, snapshot) -> None:
        self.normal_finish.setText("SESSION CHEQUERED" if snapshot.normal_finished else "")
        self.normal_finish.setToolTip(
            f"Normal session finish: {snapshot.normal_finish_source}" if snapshot.normal_finished else ""
        )

    def _prepare_session_start(self) -> None:
        # A failed restart must not repaint the previous session's prediction.
        self.timer.stop()
        self._clear_session_prediction_display("Waiting for the new qualifying session.")
        self._session_metadata_signature = None
        self._non_quali_warning_shown = False
        self.analyze.setEnabled(False)
        self.plan.setEnabled(False)

    def _restore_offline_controls(self) -> None:
        self.analyze.setEnabled(self._analysis_process is None)
        self.plan.setEnabled(True)

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
            session_info, fallback_year=self.year.value()
        )
        if identity.year is not None:
            self.year.setValue(identity.year)
        if identity.round_number is not None:
            self.round.setValue(identity.round_number)
        if identity.event_name:
            self.setWindowTitle(
                f"woPlanner Qualifying Monitor — {identity.event_name}"
            )
        sprint = is_sprint_qualifying_session(identity.session_name)
        for part, label in self.elimination_labels.items():
            display_part = f"S{part}" if sprint else part
            label.setText(f"{display_part} eliminated")
        self.elimination["Q3"].setVisible(sprint)
        self.elimination_labels["Q3"].setVisible(sprint)

    def _set_eliminated_count(self, part: str, value: int) -> None:
        self.service.eliminated_counts[part] = value
        self.service.eliminated_counts[f"S{part}"] = value

    def _reject_non_quali_session(self) -> None:
        if self._non_quali_warning_shown:
            return
        self._non_quali_warning_shown = True
        self.timer.stop()
        self.service.stop()
        self.connection.setText("Not a qualifying session")
        self.analyze.setEnabled(False)
        self.plan.setEnabled(False)
        self.table.setRowCount(0)
        self.pit_table.clearContents()
        self._clear_session_prediction_display("The received session is not Qualifying.")
        self._show_non_quali_warning()

    def _show_non_quali_warning(self) -> None:
        session_name = self.service.received_session_name or "the received session"
        QtWidgets.QMessageBox.warning(
            self,
            "Not a qualifying session",
            f"{session_name} is not Qualifying. No qualifying results were loaded.",
        )

    def _render_timing_table(self, snapshot) -> None:
        current_item = self.table.currentItem()
        selected_driver_number = (
            self.table.item(current_item.row(), 0).data(QtCore.Qt.ItemDataRole.UserRole)
            if current_item is not None and self.table.item(current_item.row(), 0) is not None
            else None
        )
        selected_column = current_item.column() if current_item is not None else 0
        self.table.setRowCount(len(snapshot.drivers))
        self.table.cutoff_rank = {"Q1": 16, "Q2": 10, "SQ1": 16, "SQ2": 10}.get(
            getattr(snapshot, "session_part", None), 0
        )
        self.table.setColumnHidden(
            3, not any(state.tyre_history for state in snapshot.drivers)
        )
        for row, state in enumerate(snapshot.drivers):
            values = (
                state.official_position if state.official_position is not None else "—",
                state.driver,
                state.team,
                None,
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
                if column == 3:
                    self.table.setCellWidget(
                        row, column, self._tyre_history_widget(state.tyre_history)
                    )
                    continue
                item = QtWidgets.QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, state.driver_number)
                if column == 0 and state.finished_reason == FinishedReason.FINAL_LAP_COMPLETED:
                    item.setText(f"🏁 {value}")
                    item.setToolTip("Chequered: recognised final timed lap completed.")
                elif column == 0 and state.effective_finished:
                    item.setBackground(QtGui.QColor("#f4b183"))
                    if state.finished_reason == FinishedReason.IN_PIT_NO_OUTLAP_TIME:
                        opportunity = "OUT lap from the pit"
                    elif state.finished_reason == FinishedReason.NO_TIME_FOR_NEXT_PUSH:
                        opportunity = "COOL/PREP lap after completed PUSH"
                    else:
                        opportunity = "recovery lap"
                    item.setToolTip(
                        f"Effective finish: insufficient time for the {opportunity} "
                        f"before another PUSH ({state.effective_finish_remaining_s:g}s remaining, "
                        f"{state.effective_finish_recovery_s:g}s required). "
                        f"Source: {state.effective_finish_timing_source}. Not the session chequered flag."
                    )
                if column == 1 and colour:
                    item.setBackground(QtGui.QColor(colour))
                    item.setForeground(QtGui.QColor("white"))
                if column == 4 and not state.active and state.finished_reason == FinishedReason.ELIMINATED:
                    item.setToolTip(f"Eliminated after {state.frozen_part or 'previous segment'}")
                if column == 4 and state.active:
                    status_colour = PIT_STATUS_BACKGROUNDS.get(state.status)
                    if status_colour:
                        item.setBackground(QtGui.QColor(status_colour))
                        item.setForeground(
                            QtGui.QColor(
                                "black" if state.status in {"IN-PIT", "NOT ACTIVE", "FIRE-UP"} else "white"
                            )
                        )
                if not state.active:
                    item.setForeground(QtGui.QColor("#777777"))
                self.table.setItem(row, column, item)
        if selected_driver_number is not None:
            selected_row = next(
                (row for row in range(self.table.rowCount())
                 if self.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
                 == selected_driver_number),
                None,
            )
            if selected_row is not None:
                self.table.setCurrentCell(selected_row, selected_column)
        self.table.viewport().update()

    def _tyre_history_widget(self, tyre_history) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(widget)
        layout.setContentsMargins(2, 1, 2, 1)
        layout.setSpacing(3)
        if not tyre_history:
            empty = QtWidgets.QLabel("—")
            empty.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(empty)
        for stint in tyre_history:
            compound = str(stint.compound).upper()
            background = QtGui.QColor(COMPOUND_COLOURS.get(compound, "#999999"))
            foreground = "black" if background.lightness() > 150 else "white"
            circle = QtWidgets.QLabel(str(stint.start_laps))
            circle.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            circle.setFixedSize(26, 26)
            circle.setStyleSheet(
                f"background:{background.name()}; color:{foreground}; "
                "border:1px solid #555555; border-radius:13px; font-weight:bold;"
            )
            circle.setToolTip(
                f"{compound} tyre age at stint start: {stint.start_laps} laps"
            )
            layout.addWidget(circle)
        layout.addStretch(1)
        return widget

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
        if self._analysis_process is not None:
            process = self._analysis_process
            self._analysis_process = None
            process.kill()
            process.waitForFinished(1000)
        self._analysis_timeout.stop()
        self.timer.stop()
        self.service.stop()
        event.accept()


def main() -> int:
    paths = resolve_runtime_paths(WORKSPACE_DATA_ROOT if WORKSPACE_DATA_ROOT.is_dir() else None)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    window = QualiWindow(runtime_paths=paths)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
