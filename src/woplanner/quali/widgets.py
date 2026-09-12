"""Reusable Qt widgets for qualifying planning and monitoring."""

from __future__ import annotations

import math

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


ROLE_COLOURS = {
    "OUT": "#b3d9ff",
    "IN": "#c7b3ff",
    "PREP": "#ffe69a",
    "COOL": "#ffe69a",
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
        self.cutoff_rank = 0

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.cutoff_rank <= 0 or self.cutoff_rank > self.rowCount():
            return
        item = self.item(self.cutoff_rank - 1, 0)
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
                self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "Add a run"
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
        self.add_run_button = QtWidgets.QPushButton("Add run")
        for label, widget in (
            ("Turnaround", self.turnaround),
            ("Margin", self.margin),
        ):
            settings.addWidget(QtWidgets.QLabel(label))
            settings.addWidget(widget)
        settings.addWidget(self.add_run_button)
        settings.addStretch(1)
        layout.addLayout(settings)
        self.run_layout = QtWidgets.QVBoxLayout()
        layout.addLayout(self.run_layout)
        self.timeline = TimelineWidget()
        layout.addWidget(self.timeline)
        self.add_run_button.clicked.connect(self.add_run)
        self.turnaround.valueChanged.connect(self.changed)
        self.margin.valueChanged.connect(self.changed)
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
            spin.setRange(1, 600)
            spin.setDecimals(1)
            spin.setSuffix(" s")
            count = QtWidgets.QLabel("n=0")
            timing.addWidget(QtWidgets.QLabel(label))
            timing.addWidget(spin)
            timing.addWidget(count)
            self.timing_inputs[key] = (spin, count)
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
        self.track_effective = QtWidgets.QLabel("—")
        self.track_source = QtWidgets.QLabel("—")
        self.track_quality = QtWidgets.QLabel("—")
        override_row = QtWidgets.QHBoxLayout()
        override_row.addWidget(self.track_override_enabled)
        override_row.addWidget(self.track_override)
        override_row.addStretch(1)
        form.addRow("Estimated value", self.track_estimated)
        form.addRow("User override", override_row)
        form.addRow("Effective value", self.track_effective)
        form.addRow("Source", self.track_source)
        form.addRow("Quality / samples", self.track_quality)
        self.track_override.valueChanged.connect(self._update_effective_track)
        self.track_override_enabled.toggled.connect(self._update_effective_track)
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
            if result is not None:
                self.live_service.load_operational_analysis(result)
            timing = self.live_service.operational_timings
            self._analysis_timing_source = self.live_service.operational_timing_sources[
                "cool_lap_s"
            ]
        else:
            values = vars(QualiTimingValues()).copy()
            if result is not None:
                values.update({key: value for key, value in result.planning_defaults.items() if value is not None})
            timing = QualiTimingValues(**values)
            self._analysis_timing_source = "FP analysis / built-in fallback"
        if result is not None:
            for _, row in result.durations.iterrows():
                if row["category"] in self.timing_inputs:
                    self.timing_inputs[row["category"]][1].setText(f"n={int(row['count'])}")
        self.model.analysis_timings = timing
        self.model.timings = timing
        self._reset_timings()
        rate, source, samples = self.analysis_service.effective_fp_track_rate()
        self._estimated_track_rate = rate
        self.track_estimated.setText(f"{rate:+.4f} s / field push")
        self.track_source.setText(source)
        self.track_quality.setText(f"{samples} usable push laps")
        self._update_effective_track()
        self._refresh_debug()

    def _reset_timings(self) -> None:
        for key, value in vars(self.model.analysis_timings).items():
            spin = self.timing_inputs[key][0]
            with QtCore.QSignalBlocker(spin):
                spin.setValue(value)
        if self.live_service is not None:
            self.live_service.clear_manual_operational_timings()
        self._refresh_timelines()

    def _timing_edited(self, category: str, value: float) -> None:
        if self.live_service is not None:
            self.live_service.set_manual_operational_timing(category, value)
        self._refresh_timelines()

    def _current_timings(self) -> QualiTimingValues:
        return QualiTimingValues(
            **{
                key: spin.value()
                for key, (spin, _) in self.timing_inputs.items()
            }
        )

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
                timing = self.model.timing(part, arrangement)
                editor.timeline.set_timing(timing, seconds, arrangement.margin_s)

    def _update_effective_track(self) -> None:
        if self.live_service is not None:
            self.live_service.set_track_override(
                self.track_override.value()
                if self.track_override_enabled.isChecked()
                else None
            )
            self.refresh_live_track()
            return
        value = (
            self.track_override.value()
            if self.track_override_enabled.isChecked()
            else getattr(self, "_estimated_track_rate", 0.0)
        )
        self.track_effective.setText(f"{value:+.4f} s / field push")

    def refresh_live_track(self) -> None:
        if self.live_service is None:
            return
        self.track_effective.setText(
            f"{self.live_service.track_rate_s_per_field_push:+.4f} s / field push"
        )
        self.track_source.setText(self.live_service.track_source)
        estimate = self.live_service.automatic_track_estimate
        if estimate is not None:
            _, samples, quality = estimate
            self.track_quality.setText(
                f"{samples} live push laps ({quality.replace('_', ' ')})"
            )

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
