"""Reusable Qt widgets for qualifying planning and monitoring."""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from PySide6 import QtCore, QtGui, QtWidgets

from ..common.session_info import is_sprint_qualifying_session
from .analysis import QualiAnalysisService
from .planning import (
    DEFAULT_SESSION_DURATIONS_S,
    SPRINT_QUALI_PARTS,
    STANDARD_QUALI_PARTS,
    QualiArrangement,
    QualiPlanModel,
    QualiRunPlan,
    QualiTimingValues,
)
from .service import QualiLiveService
from .track_evolution import TrackEvolutionDiagnostic, render_track_evolution_diagnostic
from .track_evolution_presentation import (
    display_messages, fallback_label, SOURCE_LABELS, STATUS_LABELS,
)


PREP_COOL_COLOUR = "#ffe69a"
ROLE_COLOURS = {
    "OUT": PREP_COOL_COLOUR,
    "IN": "#c7b3ff",
    "PREP": PREP_COOL_COLOUR,
    "COOL": PREP_COOL_COLOUR,
    "PUSH": "#a7e8a7",
    "OTHER": "#dddddd",
}


def format_time(value: object, *, gap: bool = False) -> str:
    if value is None:
        return "—"
    try:
        seconds_value = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(seconds_value):
        return "—"
    if gap:
        return f"+{seconds_value:.3f}"
    minutes, seconds = divmod(seconds_value, 60)
    return f"{int(minutes)}:{seconds:06.3f}"


def format_duration_words(value: float) -> str:
    sign = "-" if value < 0 else ""
    minutes, seconds = divmod(abs(float(value)), 60.0)
    seconds_text = f"{seconds:.1f}".rstrip("0").rstrip(".")
    return f"{sign}{int(minutes)} min {seconds_text} s"


class CutoffTable(QtWidgets.QTableWidget):
    """Timing table that paints a real elimination boundary."""

    def __init__(self) -> None:
        super().__init__()
        # Rendered elimination rank; independent of numeric cutoff support.
        self.cutoff_rank = 0

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        row = self.cutoff_row()
        if row is None:
            return
        item = self.item(row, 0)
        if item is None:
            return
        rect = self.visualItemRect(item)
        painter = QtGui.QPainter(self.viewport())
        painter.setPen(
            QtGui.QPen(QtGui.QColor("#d00000"), 3, QtCore.Qt.PenStyle.DashLine)
        )
        painter.drawLine(
            0,
            rect.bottom() + 1,
            self.viewport().width(),
            rect.bottom() + 1,
        )

    def cutoff_row(self) -> int | None:
        """Return the boundary row, or hide the line until that row exists."""
        if not 0 < self.cutoff_rank <= self.rowCount():
            return None
        row = self.cutoff_rank - 1
        return row if self.item(row, 0) is not None else None


class TrackEvolutionDiagnosticView(QtWidgets.QWidget):
    """Qt consumer of the shared track-evolution diagnostic view model."""

    image_ready = QtCore.Signal(object, bytes)
    render_failed = QtCore.Signal(object, str)

    def __init__(self) -> None:
        super().__init__()
        self.diagnostic: TrackEvolutionDiagnostic | None = None
        self._plot = QtGui.QPixmap()
        self._render_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quali-plot")
        self._render_future = None
        self._render_request = 0
        self._render_error: str | None = None
        self._closed = False
        self.image_ready.connect(self._accept_image)
        self.render_failed.connect(self._accept_render_error)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addStretch(1)
        self._support_status = QtWidgets.QLabel(self)
        self._support_status.setWordWrap(True)
        self._support_status.hide()
        layout.addWidget(self._support_status)
        self.retry_button = QtWidgets.QPushButton("Retry plot", self)
        self.retry_button.clicked.connect(self.retry_render)
        self.retry_button.hide()
        layout.addWidget(self.retry_button, alignment=QtCore.Qt.AlignmentFlag.AlignHCenter)
        self.setMinimumHeight(450)

    def set_diagnostic(self, diagnostic: TrackEvolutionDiagnostic) -> None:
        if diagnostic == self.diagnostic:
            self.diagnostic = diagnostic
            return
        self.diagnostic = diagnostic
        self._support_status.setText(
            "Track Correction unsupported: no supported driver fits."
            if not diagnostic.series else ""
        )
        self._support_status.setVisible(not diagnostic.series)
        self._start_render()

    def retry_render(self) -> None:
        """Retry the current immutable diagnostic after a rendering failure."""
        if self.diagnostic is not None and self._render_error is not None:
            self._start_render()

    def _start_render(self) -> None:
        if self._closed or self.diagnostic is None:
            return
        self._render_request += 1
        request = self._render_request
        self._render_error = None
        self.retry_button.hide()
        self._plot = QtGui.QPixmap()
        if self._render_future is not None:
            self._render_future.cancel()
        try:
            self._render_future = self._render_pool.submit(
                render_track_evolution_diagnostic, self.diagnostic
            )
        except Exception as exc:
            self._accept_render_error(request, f"{type(exc).__name__}: {exc}")
            return
        self._render_future.add_done_callback(
            lambda future, current=request: self._render_finished(current, future)
        )
        self.update()

    def _render_finished(self, request, future) -> None:
        if future.cancelled():
            return
        try:
            data = future.result()
            if not isinstance(data, bytes):
                raise TypeError("Plot renderer did not return PNG bytes")
        except Exception as exc:
            try:
                self.render_failed.emit(request, f"{type(exc).__name__}: {exc}")
            except RuntimeError:
                # The view can be destroyed while its worker finishes.
                pass
            return
        try:
            self.image_ready.emit(request, data)
        except RuntimeError:
            # The view may close while a previously requested image finishes.
            pass

    def _accept_image(self, request, data: bytes) -> None:
        if self._closed or request != self._render_request:
            return
        plot = QtGui.QPixmap()
        if not plot.loadFromData(data, "PNG") or plot.isNull():
            self._accept_render_error(request, "The rendered PNG could not be decoded.")
            return
        self._plot = plot
        self._render_error = None
        self.retry_button.hide()
        self.update()

    def _accept_render_error(self, request, error: str) -> None:
        if self._closed or request != self._render_request:
            return
        self._render_error = error
        self._plot = QtGui.QPixmap()
        self.retry_button.show()
        self.update()

    def closeEvent(self, event) -> None:
        self._closed = True
        self._render_request += 1
        if self._render_future is not None:
            self._render_future.cancel()
        self._render_pool.shutdown(wait=False, cancel_futures=True)
        super().closeEvent(event)

    def paintEvent(self, event) -> None:
        painter = QtGui.QPainter(self)
        plot_rect = self.rect().adjusted(0, 0, 0, -self.layout().contentsMargins().bottom())
        for footer in (self._support_status, self.retry_button):
            if not footer.isHidden():
                plot_rect.setBottom(min(plot_rect.bottom(), footer.geometry().top() - 8))
        if self.diagnostic is None:
            painter.drawText(plot_rect, QtCore.Qt.AlignmentFlag.AlignCenter, "No track-evolution evidence")
            return
        if not self._plot.isNull():
            painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawPixmap(plot_rect, self._plot)
        else:
            message = (
                f"Track-evolution plot could not be rendered.\n{self._render_error}\n"
                "Use Retry plot to try again."
                if self._render_error is not None else "Rendering track-evolution plot…"
            )
            painter.drawText(
                plot_rect.adjusted(12, 12, -12, -12),
                QtCore.Qt.AlignmentFlag.AlignCenter | QtCore.Qt.TextFlag.TextWordWrap,
                message,
            )


class TimelineWidget(QtWidgets.QWidget):
    """Paint an arrangement against session, margin, and zero boundaries."""

    def __init__(self) -> None:
        super().__init__()
        self.timing = None
        self.session_duration_s = 1.0
        self.margin_s = 0.0
        self.setMinimumHeight(100)

    def set_timing(self, timing, session_duration_s: float, margin_s: float) -> None:
        self.timing = timing
        self.session_duration_s = session_duration_s
        self.margin_s = margin_s
        self.update()

    def paintEvent(self, event) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        if self.timing is None:
            painter.drawText(
                self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter,
                "Timing unavailable — enter Cool / Prep duration",
            )
            return

        minimum = min(0.0, self.timing.latest_start_s)
        maximum = max(
            self.session_duration_s,
            max(segment.end_s for segment in self.timing.segments),
        )
        span = max(maximum - minimum, 1.0)
        left, right, y, height = 55.0, self.width() - 15.0, 32.0, 34.0
        scale = (right - left) / span

        def x_position(seconds: float) -> float:
            return left + (seconds - minimum) * scale

        colours = {
            **ROLE_COLOURS,
            "TURNAROUND": "#f4b183",
            "PIT EXIT": "#222222",
        }
        for segment in self.timing.segments:
            start = x_position(segment.start_s)
            end = x_position(segment.end_s)
            if segment.label == "PIT EXIT":
                painter.setPen(QtGui.QPen(QtGui.QColor("black"), 2))
                painter.drawLine(
                    int(start), int(y - 8), int(start), int(y + height + 8)
                )
                continue
            painter.fillRect(
                QtCore.QRectF(start, y, max(end - start, 2), height),
                QtGui.QColor(colours[segment.label]),
            )
            if end - start > 28:
                painter.drawText(
                    QtCore.QRectF(start, y, end - start, height),
                    QtCore.Qt.AlignmentFlag.AlignCenter,
                    segment.label,
                )

        boundaries = (
            (0.0, "START", "#111111"),
            (self.session_duration_s, "END", "#111111"),
            (self.session_duration_s - self.margin_s, "MARGIN", "#cc0000"),
        )
        for seconds, label, colour in boundaries:
            x = x_position(seconds)
            painter.setPen(
                QtGui.QPen(QtGui.QColor(colour), 2, QtCore.Qt.PenStyle.DashLine)
            )
            painter.drawLine(int(x), 12, int(x), int(y + height + 15))
            painter.drawText(
                int(x) - 25,
                10,
                50,
                20,
                QtCore.Qt.AlignmentFlag.AlignCenter,
                label,
            )
        latest_colour = "#cc0000" if self.timing.latest_start_s < 0 else "#111111"
        painter.setPen(QtGui.QColor(latest_colour))
        painter.drawText(
            5,
            self.height() - 12,
            f"Latest pit exit: {format_duration_words(self.timing.latest_start_s)}",
        )


class ArrangementEditor(QtWidgets.QGroupBox):
    changed = QtCore.Signal()

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.runs: list[
            tuple[QtWidgets.QCheckBox, QtWidgets.QSpinBox, QtWidgets.QWidget]
        ] = []
        layout = QtWidgets.QVBoxLayout(self)
        settings = QtWidgets.QHBoxLayout()
        self.turnaround = QtWidgets.QDoubleSpinBox()
        self.turnaround.setRange(0, 900)
        self.turnaround.setValue(120)
        self.turnaround.setSuffix(" s")
        self.margin = QtWidgets.QDoubleSpinBox()
        self.margin.setRange(0, 300)
        self.margin.setValue(30)
        self.margin.setSuffix(" s")
        self.double_yellow = QtWidgets.QCheckBox("DY")
        self.double_yellow.setToolTip(
            "Allow one worst-case Double Yellow per arrangement: OUT + PUSH + IN, "
            "plus PREP if the first run requires it."
        )
        self.add_run_button = QtWidgets.QPushButton("Add run")
        for label, widget in (
            ("Turnaround", self.turnaround),
            ("Margin", self.margin),
        ):
            settings.addWidget(QtWidgets.QLabel(label))
            settings.addWidget(widget)
        settings.addWidget(self.double_yellow)
        settings.addWidget(self.add_run_button)
        settings.addStretch(1)
        layout.addLayout(settings)
        self.run_layout = QtWidgets.QVBoxLayout()
        layout.addLayout(self.run_layout)
        self.timeline = TimelineWidget()
        layout.addWidget(self.timeline)
        self.release_summary = QtWidgets.QLabel()
        self.release_summary.setWordWrap(True)
        layout.addWidget(self.release_summary)
        self.add_run_button.clicked.connect(self.add_run)
        self.turnaround.valueChanged.connect(self.changed)
        self.margin.valueChanged.connect(self.changed)
        self.double_yellow.toggled.connect(self.changed)
        self.add_run()

    def add_run(self) -> None:
        row = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        prep = QtWidgets.QCheckBox("Prep required")
        pushes = QtWidgets.QSpinBox()
        pushes.setRange(1, 10)
        pushes.setValue(1)
        remove = QtWidgets.QPushButton("Remove")
        layout.addWidget(QtWidgets.QLabel(f"Run {len(self.runs) + 1}"))
        layout.addWidget(prep)
        layout.addWidget(QtWidgets.QLabel("Push laps"))
        layout.addWidget(pushes)
        layout.addWidget(remove)
        layout.addStretch(1)
        self.run_layout.addWidget(row)
        self.runs.append((prep, pushes, row))
        prep.toggled.connect(self.changed)
        pushes.valueChanged.connect(self.changed)
        remove.clicked.connect(lambda: self.remove_run(row))
        self.changed.emit()

    def remove_run(self, row: QtWidgets.QWidget) -> None:
        if len(self.runs) == 1:
            return
        self.runs = [entry for entry in self.runs if entry[2] is not row]
        row.deleteLater()
        self.changed.emit()

    def arrangement(self) -> QualiArrangement:
        runs = tuple(
            QualiRunPlan(prep.isChecked(), pushes.value())
            for prep, pushes, _ in self.runs
        )
        return QualiArrangement(
            runs,
            self.turnaround.value(),
            self.margin.value(),
            self.title(),
            double_yellow=self.double_yellow.isChecked(),
        )


class QualiPlanDialog(QtWidgets.QDialog):
    """Editable planning, track-correction, and FP-debug dialog."""

    def __init__(
        self,
        analysis_service: QualiAnalysisService,
        live_service: QualiLiveService | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.analysis_service = analysis_service
        self.live_service = live_service
        self.result = analysis_service.result
        self.setWindowTitle("Quali Plan")
        self.resize(1200, 800)
        self.model = QualiPlanModel()
        self._manual_timing_values: dict[str, float] = {}
        self.planning_parts = (
            SPRINT_QUALI_PARTS
            if live_service is not None
            and is_sprint_qualifying_session(live_service.received_session_name)
            else STANDARD_QUALI_PARTS
        )
        layout = QtWidgets.QVBoxLayout(self)
        self.tabs = QtWidgets.QTabWidget()
        layout.addWidget(self.tabs)
        self._build_planning_tab()
        self._build_track_tab()
        self._build_debug_tab()
        self.load_analysis(self.result)

    def _build_planning_tab(self) -> None:
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        timing_box = QtWidgets.QGroupBox("Common timing values")
        timing = QtWidgets.QHBoxLayout(timing_box)
        self.timing_inputs = {}
        timing_fields = (
            ("out_lap_s", "Out lap"),
            ("in_lap_s", "In lap"),
            ("cool_lap_s", "Cool / Prep lap"),
            ("push_lap_s", "Push lap"),
        )
        for key, label in timing_fields:
            spin = QtWidgets.QDoubleSpinBox()
            spin.setRange(0 if key == "cool_lap_s" else 1, 600)
            spin.setDecimals(1)
            spin.setSuffix(" s")
            if key == "cool_lap_s":
                spin.setSpecialValueText("Manual required")
            source = QtWidgets.QLabel("built-in fallback")
            timing.addWidget(QtWidgets.QLabel(label))
            timing.addWidget(spin)
            timing.addWidget(source)
            self.timing_inputs[key] = (spin, source)
            spin.valueChanged.connect(lambda value, category=key: self._timing_edited(category, value))
        reset = QtWidgets.QPushButton("Reset / reload")
        reset.clicked.connect(self._reset_timings)
        timing.addWidget(reset)
        outer.addWidget(timing_box)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        body = QtWidgets.QWidget()
        body_layout = QtWidgets.QVBoxLayout(body)
        self.part_editors = {}
        for part in self.planning_parts:
            seconds = DEFAULT_SESSION_DURATIONS_S[part]
            if self.live_service is not None and self.live_service.raw_snapshot.session_part == part:
                seconds = self.live_service.raw_snapshot.segment_duration_s or seconds
            minutes = seconds / 60.0
            box = QtWidgets.QGroupBox(part)
            box.setCheckable(True)
            box.setChecked(True)
            section = QtWidgets.QVBoxLayout(box)
            header = QtWidgets.QHBoxLayout()
            duration = QtWidgets.QDoubleSpinBox()
            duration.setRange(1, 60)
            duration.setValue(minutes)
            duration.setSuffix(" min")
            add = QtWidgets.QPushButton("Add arrangement")
            header.addWidget(QtWidgets.QLabel("Session duration"))
            header.addWidget(duration)
            header.addWidget(add)
            header.addStretch(1)
            section.addLayout(header)
            arrangements = QtWidgets.QVBoxLayout()
            section.addLayout(arrangements)
            self.part_editors[part] = (duration, arrangements, [])
            add.clicked.connect(lambda _=False, p=part: self._add_arrangement(p))
            duration.valueChanged.connect(self._refresh_timelines)
            body_layout.addWidget(box)
            self._add_arrangement(part)
        body_layout.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        self.tabs.addTab(page, "Planning")

    def _build_track_tab(self) -> None:
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        self.track_estimated = QtWidgets.QLabel("—")
        self.track_override_enabled = QtWidgets.QCheckBox("Use override")
        self.track_override = QtWidgets.QDoubleSpinBox()
        self.track_override.setRange(-10, 10)
        self.track_override.setDecimals(4)
        self.track_override.setSuffix(" s / field push")
        if self.live_service is not None:
            existing_override = self.live_service.track_override_s_per_field_push
            if existing_override is not None:
                self.track_override.setValue(existing_override)
                self.track_override_enabled.setChecked(True)
        self.track_effective = QtWidgets.QLabel("—")
        self.track_source = QtWidgets.QLabel("—")
        self.track_quality = QtWidgets.QLabel("—")
        self.track_messages = QtWidgets.QLabel()
        self.track_messages.setWordWrap(True)
        self.track_messages.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.track_result_detail = QtWidgets.QPlainTextEdit()
        self.track_result_detail.setReadOnly(True)
        self.track_result_detail.setMaximumHeight(180)
        self.track_detail_toggle = QtWidgets.QCheckBox("Show track-evolution details")
        self.track_result_detail.hide()
        self.track_detail_toggle.toggled.connect(self.track_result_detail.setVisible)
        self.track_diagnostic_summary = QtWidgets.QLabel("—")
        self.track_telemetry_warning = QtWidgets.QLabel()
        self.track_telemetry_warning.setObjectName("trackTelemetryDelayWarning")
        self.track_telemetry_warning.setStyleSheet("color: #b26a00; font-weight: 600;")
        self.track_telemetry_warning.hide()
        self.track_diagnostic = TrackEvolutionDiagnosticView()
        self.clean_air_gap = QtWidgets.QDoubleSpinBox()
        self.clean_air_gap.setRange(0.1, 30.0)
        self.clean_air_gap.setDecimals(1)
        self.clean_air_gap.setSuffix(" s ahead gap")
        self.clean_air_gap.setValue(
            self.live_service.clean_air_gap_s if self.live_service is not None else 3.0
        )
        override_row = QtWidgets.QHBoxLayout()
        override_row.addWidget(self.track_override_enabled)
        override_row.addWidget(self.track_override)
        override_row.addStretch(1)
        form.addRow("Automatic track rate", self.track_estimated)
        self.track_estimated.setToolTip(
            "Signed lap-time change per global field push: negative improves, positive worsens."
        )
        form.addRow("User override", override_row)
        form.addRow("Effective value", self.track_effective)
        form.addRow("Source", self.track_source)
        form.addRow("Status / support", self.track_quality)
        form.addRow("", self.track_messages)
        form.addRow("", self.track_detail_toggle)
        form.addRow("", self.track_result_detail)
        form.addRow("Track Evolution", self.track_diagnostic_summary)
        form.addRow("", self.track_telemetry_warning)
        form.addRow("", self.track_diagnostic)
        form.addRow("Traffic context threshold", self.clean_air_gap)
        self.clean_air_gap.setToolTip("Auxiliary physical traffic only; does not control track-evolution admission or status.")
        self.track_override.valueChanged.connect(self._update_effective_track)
        self.track_override_enabled.toggled.connect(self._update_effective_track)
        self.clean_air_gap.valueChanged.connect(self._update_clean_air_gap)
        self.tabs.addTab(page, "Track Correction")

    def _build_debug_tab(self) -> None:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        top = QtWidgets.QHBoxLayout()
        self.debug_session = QtWidgets.QComboBox()
        self.debug_session.addItems(["FP1", "FP2", "FP3"])
        self.debug_session.currentTextChanged.connect(self._refresh_debug)
        top.addWidget(QtWidgets.QLabel("Session"))
        top.addWidget(self.debug_session)
        for role in ("OUT", "IN", "COOL/PREP", "PUSH", "OTHER/INVALID"):
            if role == "COOL/PREP":
                base = "COOL"
            elif role == "OTHER/INVALID":
                base = "OTHER"
            else:
                base = role
            label = QtWidgets.QLabel(role)
            label.setStyleSheet(
                f"background:{ROLE_COLOURS[base]}; padding:4px"
            )
            top.addWidget(label)
        top.addStretch(1)
        layout.addLayout(top)
        self.debug_table = QtWidgets.QTableWidget()
        layout.addWidget(self.debug_table)
        self.tabs.addTab(page, "Debug")

    def load_analysis(self, result) -> None:
        self.result = result
        if self.live_service is not None:
            timing = self.live_service.operational_timings
            self._analysis_timing_source = self.live_service.operational_timing_sources[
                "cool_lap_s"
            ]
        else:
            offline_policy = QualiLiveService()
            if result is not None:
                offline_policy.load_operational_analysis(result)
            timing = offline_policy.operational_timings
            self._offline_timing_sources = offline_policy.operational_timing_sources
            self._analysis_timing_source = self._offline_timing_sources["cool_lap_s"]
        self.model.analysis_timings = timing
        self.model.timings = timing
        self._display_timings()
        rate, source, samples = self.analysis_service.effective_fp_track_rate()
        self._estimated_track_rate = rate
        self.track_estimated.setText(f"{rate:+.4f} s / field push")
        self.track_source.setText(source)
        self.track_quality.setText(f"{samples} usable push laps")
        if self.live_service is not None:
            self.refresh_live_track()
        else:
            self._display_effective_track()
        self._refresh_debug()

    def _reset_timings(self) -> None:
        if self.live_service is not None:
            self.live_service.clear_manual_operational_timings()
            self.model.analysis_timings = self.live_service.operational_timings
        else:
            self._manual_timing_values.clear()
        self._display_timings()

    def _display_timings(self) -> None:
        self.model.timings = self._current_timings()
        for key in vars(self.model.analysis_timings):
            spin = self.timing_inputs[key][0]
            with QtCore.QSignalBlocker(spin):
                value = getattr(self.model.timings, key)
                spin.setValue(0 if value is None else value)
        if self.live_service is not None:
            for key, (_, source) in self.timing_inputs.items():
                source.setText(self.live_service.operational_timing_sources[key])
        else:
            for key, (_, source) in self.timing_inputs.items():
                source.setText(self._offline_timing_sources[key])
        if self.live_service is None:
            for key in self._manual_timing_values:
                self.timing_inputs[key][1].setText("manual override")
        self._refresh_timelines()

    def _timing_edited(self, category: str, value: float) -> None:
        if category == "cool_lap_s" and value <= 0:
            return
        if self.live_service is not None:
            self.live_service.set_manual_operational_timing(category, value)
            self.timing_inputs[category][1].setText(
                self.live_service.operational_timing_sources[category]
            )
        else:
            self._manual_timing_values[category] = value
            self.timing_inputs[category][1].setText("manual override")
        self._refresh_timelines()

    def _current_timings(self) -> QualiTimingValues:
        if self.live_service is not None:
            return self.live_service.operational_timings
        return replace(self.model.analysis_timings, **self._manual_timing_values)

    def _add_arrangement(self, part: str) -> None:
        _, layout, editors = self.part_editors[part]
        editor = ArrangementEditor(f"Arrangement {len(editors) + 1}")
        editor.changed.connect(self._refresh_timelines)
        editors.append(editor)
        layout.addWidget(editor)
        self._refresh_timelines()

    def _refresh_timelines(self) -> None:
        if not hasattr(self, "part_editors"):
            return
        self.model.timings = self._current_timings()
        for part, (duration, _, editors) in self.part_editors.items():
            seconds = duration.value() * 60.0
            self.model.session_durations_s[part] = seconds
            for editor in editors:
                arrangement = editor.arrangement()
                timing = (
                    None if self.model.timings.cool_lap_s is None
                    else self.model.timing(part, arrangement)
                )
                editor.timeline.set_timing(timing, seconds, arrangement.margin_s)
                if timing is None:
                    editor.release_summary.setText(
                        "Cool / Prep timing unavailable — manual override required"
                    )
                    continue
                summary = (
                    "Latest-safe release: leave with at least "
                    f"{format_duration_words(seconds - timing.latest_start_s)} remaining. "
                )
                if arrangement.double_yellow:
                    baseline = timing.latest_start_s + timing.double_yellow_allowance_s
                    prep = " + PREP" if arrangement.runs[0].prep_required else ""
                    summary += (
                        f"DY: one OUT{prep} + PUSH + IN allowance, "
                        f"{format_duration_words(timing.double_yellow_allowance_s)} earlier; "
                        "without DY, leave with at least "
                        f"{format_duration_words(seconds - baseline)} remaining."
                    )
                else:
                    summary += "DY off."
                editor.release_summary.setText(summary)

    def _update_effective_track(self) -> None:
        if self.live_service is not None:
            self.live_service.set_track_override(
                self.track_override.value()
                if self.track_override_enabled.isChecked()
                else None
            )
            self.refresh_live_track()
            return
        self._display_effective_track()

    def _display_effective_track(self) -> None:
        value = (
            self.track_override.value()
            if self.track_override_enabled.isChecked()
            else getattr(self, "_estimated_track_rate", 0.0)
        )
        self.track_effective.setText(f"{value:+.4f} s / field push")

    def _update_clean_air_gap(self, value: float) -> None:
        if self.live_service is not None:
            self.live_service.set_clean_air_gap_s(value)
            self.refresh_live_track()

    def refresh_live_track(self, snapshot=None) -> None:
        if self.live_service is None:
            return
        snapshot = snapshot or self.live_service.snapshot
        self.track_estimated.setText(
            f"{snapshot.automatic_track_rate_s_per_field_push:+.4f} s / field push"
        )
        self.track_effective.setText(
            f"{snapshot.cutoff.track_rate_s_per_field_push:+.4f} s / field push"
        )
        self.track_source.setText(snapshot.track_source)
        warning = getattr(snapshot, "telemetry_delay_warning", "")
        self.track_telemetry_warning.setText(warning)
        self.track_telemetry_warning.setVisible(bool(warning))
        diagnostic = snapshot.track_evolution_diagnostic
        result = snapshot.track_evolution_result
        if result is not None:
            import json
            self.track_source.setText(SOURCE_LABELS[result['estimate_source']])
            self.track_estimated.setText(
                f"{snapshot.automatic_track_rate_s_per_field_push:+.4f} s / field push "
                f"({SOURCE_LABELS[result['automatic_source']] if result['live_estimate_s_per_global_push'] is not None else fallback_label(result)})"
            )
            self.track_quality.setText(
                f"{STATUS_LABELS[result['status']]} · {result['support']['fit_observations']} fitted push laps / "
                f"{result['support']['drivers']} drivers"
            )
            messages = display_messages(result)
            self.track_messages.setText("\n".join(
                ("Information: " if m['severity'] == 'information' else "") +
                f"{m['category']} — {m['explanation']}" for m in messages
            ))
            self.track_result_detail.setPlainText(json.dumps(result, indent=2))
        else:
            self.track_quality.setText("Track-evolution status unavailable")
            self.track_messages.clear()
            self.track_result_detail.clear()
        if diagnostic is not None:
            aggregate = (
                "—" if diagnostic.aggregate_slope_s_per_push is None
                else f"{diagnostic.aggregate_slope_s_per_push:+.4f} s/push"
            )
            sigma = (
                "—" if diagnostic.weighted_slope_std_s_per_push is None
                else f"{diagnostic.weighted_slope_std_s_per_push:.4f} s/push"
            )
            self.track_diagnostic_summary.setText(
                f"Aggregate {aggregate} · σ {sigma} · "
                f"{diagnostic.driver_count} drivers / {diagnostic.lap_count} laps"
            )
            self.track_diagnostic.set_diagnostic(diagnostic)

    def _refresh_debug(self) -> None:
        if self.result is None:
            self.debug_table.clear()
            self.debug_table.setRowCount(0)
            return
        data = self.result.laps.loc[
            self.result.laps["session"].eq(self.debug_session.currentText())
        ]
        drivers = sorted(data["driver"].unique())
        laps = sorted(data["lap_number"].dropna().unique())
        self.debug_table.setRowCount(len(laps))
        self.debug_table.setColumnCount(len(drivers))
        self.debug_table.setHorizontalHeaderLabels(drivers)
        self.debug_table.setVerticalHeaderLabels([str(int(value)) for value in laps])
        for row_index, lap in enumerate(laps):
            for column_index, driver in enumerate(drivers):
                rows = data.loc[
                    data["driver"].eq(driver) & data["lap_number"].eq(lap)
                ]
                if rows.empty:
                    continue
                value = rows.iloc[0]
                item = QtWidgets.QTableWidgetItem(
                    format_time(value["raw_lap_time_s"])
                )
                role = str(value["lap_role"])
                colour = ROLE_COLOURS.get(role, ROLE_COLOURS["OTHER"])
                if not bool(value["is_valid_run"]):
                    colour = ROLE_COLOURS["OTHER"]
                item.setBackground(QtGui.QColor(colour))
                item.setToolTip(
                    f"Role: {role}\n"
                    f"Run: {value['run_id']}\n"
                    f"Valid: {value['is_valid_run']}\n"
                    f"Reason: {value['invalid_reason']}"
                )
                self.debug_table.setItem(row_index, column_index, item)
