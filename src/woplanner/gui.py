from __future__ import annotations

import math
import sys

import numpy as np
import pandas as pd

from .model import (
    COMPARISON_MODE_LABELS,
    COMPARISON_MODE_MODELLED_REAL_STRATEGY,
    COMPARISON_MODE_PURE_SIM,
)
from .service import (
    REFERENCE_MODE_AVERAGED_PACE,
    REFERENCE_MODE_LABELS,
    RacePlannerService,
    finite_xy,
)
from .strategy_model import MissingStrategyModelError

COMPOUND_COLOURS = {
    "SOFT": "#e10600",
    "MEDIUM": "#c99a00",
    "HARD": "#6f7377",
    "INTERMEDIATE": "#43b02a",
    "WET": "#0067ad",
}
CONTROL_FONT_POINT_SIZE = 12
RANK_FONT_POINT_SIZE = 18
TYRE_MARKER_SIZE = 22
TYRE_FONT_POINT_SIZE = RANK_FONT_POINT_SIZE
SC_BAND_COLOUR = "#ffbf00"
VSC_BAND_COLOUR = "#ffeb3b"


class RacePlannerWindowMixin:
    def _init_state(self) -> None:
        self.service = RacePlannerService()
        self.curves = {}
        self.curve_data = {}
        self.selected_driver = None
        self.hover_driver = None
        self.hover_label = None
        self.pit_items = []
        self.tyre_items = []
        self.guide_items = []
        self.track_status_items = []
        self.pit_target_items = []
        self.pit_target = None
        self.custom_ranges = False


def main() -> int:
    from PySide6 import QtCore, QtGui, QtWidgets
    import pyqtgraph as pg

    pg.setConfigOption("background", "w")
    pg.setConfigOption("foreground", "k")

    class RacePlannerWindow(QtWidgets.QMainWindow, RacePlannerWindowMixin):
        def _font(self, point_size: int) -> QtGui.QFont:
            font = QtGui.QFont()
            font.setPointSize(point_size)
            return font

        def _bold_font(self, point_size: int) -> QtGui.QFont:
            font = self._font(point_size)
            font.setBold(True)
            return font

        def __init__(self) -> None:
            super().__init__()
            self._init_state()
            self.setWindowTitle("woPlanner")
            self.resize(1200, 760)
            self.setFont(self._font(14))

            central = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(central)
            controls = QtWidgets.QHBoxLayout()

            self.year_input = QtWidgets.QSpinBox()
            self.year_input.setRange(1950, 2100)
            self.year_input.setValue(2026)
            self.round_input = QtWidgets.QSpinBox()
            self.round_input.setRange(1, 30)
            self.round_input.setValue(1)
            self.session_input = QtWidgets.QLineEdit("R")
            self.session_input.setMaximumWidth(64)
            self.load_button = QtWidgets.QPushButton("Load")
            self.latest_button = QtWidgets.QPushButton("Latest")
            self.export_button = QtWidgets.QPushButton("Export")
            self.assumptions_button = QtWidgets.QPushButton("Assumptions")
            self.strategy_delta_button = QtWidgets.QPushButton("Strategy deltas")
            self.clear_sims_button = QtWidgets.QPushButton("Clear sims")
            self.export_menu = QtWidgets.QMenu(self.export_button)
            self.export_file_action = self.export_menu.addAction("PNG to file")
            self.export_clipboard_action = self.export_menu.addAction("PNG to clipboard")
            self.export_button.setMenu(self.export_menu)
            self.reference_driver = QtWidgets.QComboBox()
            self.reference_mode = QtWidgets.QComboBox()
            for mode, label in REFERENCE_MODE_LABELS.items():
                self.reference_mode.addItem(label, mode)
            averaged_index = self.reference_mode.findData(REFERENCE_MODE_AVERAGED_PACE)
            if averaged_index >= 0:
                self.reference_mode.setCurrentIndex(averaged_index)
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
                ("Year", self.year_input),
                ("Round", self.round_input),
                ("Session", self.session_input),
                ("Reference", self.reference_driver),
                ("Mode", self.reference_mode),
                ("Width", self.line_width),
                ("Pit", self.normal_pit_loss),
                ("SC/VSC", self.sc_pit_loss),
            ):
                controls.addWidget(QtWidgets.QLabel(label))
                controls.addWidget(widget)
            controls.addWidget(self.load_button)
            controls.addWidget(self.latest_button)
            controls.addWidget(self.export_button)
            controls.addWidget(self.assumptions_button)
            controls.addWidget(self.strategy_delta_button)
            controls.addWidget(self.clear_sims_button)
            controls.addStretch(1)

            range_controls = QtWidgets.QHBoxLayout()
            self.x_min_input = self._range_spinbox()
            self.x_max_input = self._range_spinbox()
            self.y_min_input = self._range_spinbox()
            self.y_max_input = self._range_spinbox()
            self.apply_range_button = QtWidgets.QPushButton("Apply range")
            self.auto_range_button = QtWidgets.QPushButton("Auto range")
            for label, widget in (
                ("Lap min", self.x_min_input),
                ("Lap max", self.x_max_input),
                ("Y min", self.y_min_input),
                ("Y max", self.y_max_input),
            ):
                range_controls.addWidget(QtWidgets.QLabel(label))
                range_controls.addWidget(widget)
            range_controls.addWidget(self.apply_range_button)
            range_controls.addWidget(self.auto_range_button)
            range_controls.addStretch(1)

            self.plot_row_widget = QtWidgets.QWidget()
            plot_row = QtWidgets.QHBoxLayout(self.plot_row_widget)
            plot_row.setContentsMargins(0, 0, 0, 0)
            self.start_rank_list = QtWidgets.QListWidget()
            self.latest_rank_list = QtWidgets.QListWidget()
            for rank_list in (self.start_rank_list, self.latest_rank_list):
                rank_list.setFixedWidth(190)
                rank_list.setAlternatingRowColors(True)
                rank_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
                rank_list.setFont(self._font(RANK_FONT_POINT_SIZE))

            start_panel = QtWidgets.QVBoxLayout()
            self.start_rank_label = QtWidgets.QLabel("Start")
            self.start_rank_label.setFont(self._font(RANK_FONT_POINT_SIZE))
            start_panel.addWidget(self.start_rank_label)
            start_panel.addWidget(self.start_rank_list)
            latest_panel = QtWidgets.QVBoxLayout()
            self.latest_rank_label = QtWidgets.QLabel("Current/Result")
            self.latest_rank_label.setFont(self._font(RANK_FONT_POINT_SIZE))
            latest_panel.addWidget(self.latest_rank_label)
            latest_panel.addWidget(self.latest_rank_list)

            self.plot = pg.PlotWidget()
            self.plot.setLabel("bottom", "Lap", **{"font-size": "17pt"})
            self.plot.setLabel("left", "Cumulative delta", units="s", **{"font-size": "17pt"})
            self.plot.getAxis("bottom").setTickFont(self._font(14))
            self.plot.getAxis("left").setTickFont(self._font(14))
            self.plot.setBackground("w")
            self.plot.getPlotItem().invertY(True)
            self.plot.scene().sigMouseClicked.connect(self._plot_clicked)
            self.plot.scene().sigMouseMoved.connect(self._plot_mouse_moved)

            plot_row.addLayout(start_panel)
            plot_row.addWidget(self.plot, stretch=1)
            plot_row.addLayout(latest_panel)

            layout.addLayout(controls)
            layout.addLayout(range_controls)
            layout.addWidget(self.plot_row_widget, stretch=1)
            self.setCentralWidget(central)

            self.load_button.clicked.connect(self._load)
            self.latest_button.clicked.connect(self._load_latest)
            self.export_file_action.triggered.connect(self._export_png)
            self.export_clipboard_action.triggered.connect(self._copy_png_to_clipboard)
            self.reference_driver.currentTextChanged.connect(self._redraw)
            self.reference_mode.currentTextChanged.connect(self._redraw)
            self.line_width.valueChanged.connect(self._redraw)
            self.normal_pit_loss.valueChanged.connect(self._update_pit_window)
            self.sc_pit_loss.valueChanged.connect(self._update_pit_window)
            self.assumptions_button.clicked.connect(self._show_assumptions)
            self.strategy_delta_button.clicked.connect(self._show_strategy_delta_details)
            self.clear_sims_button.clicked.connect(self._clear_all_simulations)
            self.apply_range_button.clicked.connect(self._apply_custom_ranges)
            self.auto_range_button.clicked.connect(self._reset_auto_ranges)

        def _range_spinbox(self):
            spinbox = QtWidgets.QDoubleSpinBox()
            spinbox.setRange(-10000.0, 10000.0)
            spinbox.setDecimals(1)
            spinbox.setSingleStep(1.0)
            spinbox.setMinimumWidth(86)
            return spinbox

        def keyReleaseEvent(self, event) -> None:
            super().keyReleaseEvent(event)
            if not self._pit_modifier_active():
                self._clear_pit_target()

        def _load(self) -> None:
            session = self._load_session(
                self.year_input.value(),
                self.round_input.value(),
                self.session_input.text().strip() or "R",
            )
            self._set_reference_to_default(session)

        def _load_latest(self) -> None:
            session_name = self.session_input.text().strip() or "R"
            session = self.service.load_latest_race(session_name=session_name)
            self.year_input.setValue(session.year)
            self.round_input.setValue(session.round_number)
            self.session_input.setText(session.session_name)
            self._populate_reference_drivers(session)
            self._set_reference_to_default(session)

        def _load_session(self, year: int, round_number: int, session_name: str):
            session = self.service.load_race(year, round_number, session_name)
            self._populate_reference_drivers(session)
            return session

        def _populate_reference_drivers(self, session) -> None:
            self.reference_driver.clear()
            self.reference_driver.addItems(session.drivers["driver"].astype(str).tolist())
            self.selected_driver = None
            self.hover_driver = None
            self._update_rank_lists()

        def _set_reference_to_default(self, session) -> None:
            reference = self.service.default_reference_driver()
            index = self.reference_driver.findText(reference)
            if index >= 0:
                self.reference_driver.setCurrentIndex(index)
            self._redraw()

        def _redraw(self) -> None:
            if self.service.session is None or not self.reference_driver.currentText():
                return
            self._clear_pit_target()
            self._clear_hover_label()
            self.plot.clear()
            self.curves = {}
            self.curve_data = {}
            self.pit_items = []
            self.tyre_items = []
            self.guide_items = []
            self.track_status_items = []
            plot_data = self.service.build_plot_data(
                self.reference_driver.currentText(),
                reference_mode=str(self.reference_mode.currentData() or REFERENCE_MODE_AVERAGED_PACE),
            )
            styles = self.service.session.drivers.set_index("driver").to_dict("index")
            for (driver, role, scenario_id, is_faded), driver_data in plot_data.groupby(
                ["driver", "plot_role", "scenario_id", "is_faded"],
                sort=False,
                dropna=False,
            ):
                style = styles.get(driver, {})
                x, y = finite_xy(driver_data, y_column="delta_s")
                pen = self._driver_pen(
                    driver,
                    style,
                    role=str(role),
                    is_faded=bool(is_faded),
                )
                curve = self.plot.plot(x, y, pen=pen, name=str(driver), clickable=True)
                curve.sigClicked.connect(
                    lambda _, event, drv=driver: self._curve_clicked(drv, event)
                )
                curve_key = (driver, str(role), str(scenario_id), bool(is_faded))
                self.curves[curve_key] = curve
                if driver in self.curve_data:
                    existing_x, existing_y = self.curve_data[driver]
                    self.curve_data[driver] = (
                        np.concatenate([existing_x, x]),
                        np.concatenate([existing_y, y]),
                    )
                else:
                    self.curve_data[driver] = (x, y)
            if not self.custom_ranges:
                self._update_range_controls_from_data(plot_data)
            self._update_lap_axis_ticks(plot_data)
            self._apply_plot_ranges()
            self._draw_track_status_bands()
            self._draw_lap_delta_guides()
            self._update_pit_window()
            self._draw_tyre_markers()
            self._update_rank_lists()

        def _driver_pen(
            self,
            driver: str,
            style: dict,
            *,
            role: str = "actual",
            is_faded: bool = False,
        ):
            base_width = self.line_width.value()
            width = base_width
            colour = QtGui.QColor(style.get("colour", "#555555"))
            if driver == self.hover_driver:
                width = max(width + 1.2, base_width * 1.7)
            if driver == self.selected_driver:
                width = max(width + 2.0, base_width * 2.2)
            pen_style = (
                QtCore.Qt.PenStyle.DashLine
                if style.get("line_style") == "dash"
                else QtCore.Qt.PenStyle.SolidLine
            )
            if role == "simulation":
                width = max(width, base_width + 1.0, 3.0)
                pen_style = QtCore.Qt.PenStyle.CustomDashLine
            if is_faded:
                width = max(base_width * 0.45, 1.0)
                colour.setAlpha(128)
            pen = pg.mkPen(colour, width=width, style=pen_style)
            if role == "simulation":
                pen.setDashPattern(
                    [1.0, 3.0, 4.0, 3.0]
                    if style.get("line_style") == "dash"
                    else [1.0, 2.0, 8.0, 2.0]
                )
                pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
            return pen

        def _select_driver(self, driver: str) -> None:
            self.selected_driver = driver
            self._update_selection_visuals()

        def _curve_clicked(self, driver: str, event) -> None:
            if (
                event.button() == QtCore.Qt.MouseButton.LeftButton
                and self._pit_modifier_active()
            ):
                self.selected_driver = driver
                self._update_selection_visuals()
                self._schedule_strategy_editor(event.scenePos(), preferred_driver=driver)
                event.accept()
                return
            self._select_driver(driver)

        def _plot_clicked(self, event) -> None:
            if (
                event.button() == QtCore.Qt.MouseButton.LeftButton
                and self._pit_modifier_active()
                and not event.isAccepted()
            ):
                self._schedule_strategy_editor(event.scenePos())
                event.accept()
                return
            if event.button() == QtCore.Qt.MouseButton.LeftButton and not event.isAccepted():
                self.selected_driver = None
                self._clear_pit_target()
                self._update_selection_visuals()

        def _schedule_strategy_editor(
            self,
            scene_pos,
            *,
            preferred_driver: str | None = None,
        ) -> None:
            copied_pos = QtCore.QPointF(float(scene_pos.x()), float(scene_pos.y()))
            QtCore.QTimer.singleShot(
                0,
                lambda pos=copied_pos, drv=preferred_driver: self._open_strategy_editor_from_position(
                    pos,
                    preferred_driver=drv,
                ),
            )

        def _open_strategy_editor_from_position(
            self,
            scene_pos,
            *,
            preferred_driver: str | None = None,
        ) -> None:
            if self.service.session is None:
                return
            point = self.plot.getPlotItem().vb.mapSceneToView(scene_pos)
            driver = (
                preferred_driver
                or self.hover_driver
                or self._nearest_driver(point.x(), point.y(), max_distance=0.0025)
                or self.selected_driver
            )
            if not driver:
                QtWidgets.QMessageBox.information(
                    self,
                    "Strategy simulation",
                    "Hover a driver curve while holding Command or Ctrl before creating a simulation.",
                )
                return
            stop_after_lap = self._stop_after_lap_from_x(driver, float(point.x()))
            if stop_after_lap is None:
                return
            self.selected_driver = driver
            self._update_selection_visuals()
            self._open_strategy_editor(driver, stop_after_lap)

        def _open_strategy_editor(self, driver: str, stop_after_lap: int) -> None:
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle("Pit stop simulation")
            layout = QtWidgets.QFormLayout(dialog)
            applied = False

            driver_label = QtWidgets.QLabel(driver)
            stop_label = QtWidgets.QLabel(str(stop_after_lap))
            compound_buttons = QtWidgets.QButtonGroup(dialog)
            compound_row = QtWidgets.QHBoxLayout()
            compound_map = {
                "S": "SOFT",
                "M": "MEDIUM",
                "H": "HARD",
                "I": "INTERMEDIATE",
                "W": "WET",
            }
            for label, compound in compound_map.items():
                button = QtWidgets.QPushButton(label)
                button.setCheckable(True)
                button.setProperty("compound", compound)
                if compound == "HARD":
                    button.setChecked(True)
                colour = COMPOUND_COLOURS.get(compound, "#777777")
                button.setStyleSheet(
                    "QPushButton {"
                    f"color: {colour}; border: 2px solid {colour};"
                    "border-radius: 14px; min-width: 28px; min-height: 28px;"
                    "background: transparent; font-weight: 700;"
                    "}"
                    "QPushButton:checked { background: rgba(0, 0, 0, 25); }"
                )
                compound_buttons.addButton(button)
                compound_row.addWidget(button)
            tyre_age = QtWidgets.QSpinBox()
            tyre_age.setRange(0, 100)
            tyre_age.setValue(0)
            pit_loss = QtWidgets.QDoubleSpinBox()
            pit_loss.setRange(0.0, 120.0)
            pit_loss.setDecimals(1)
            pit_loss.setSingleStep(0.5)
            pit_loss.setSuffix(" s")
            try:
                default_pit_loss = self.service.pit_loss_default_for_stop(
                    stop_after_lap=stop_after_lap,
                    normal_pit_loss_s=self.normal_pit_loss.value(),
                    sc_vsc_pit_loss_s=self.sc_pit_loss.value(),
                )
            except Exception:
                default_pit_loss = self.normal_pit_loss.value()
            pit_loss.setValue(default_pit_loss)
            comparison_mode = QtWidgets.QComboBox()
            comparison_mode.addItem(
                COMPARISON_MODE_LABELS[COMPARISON_MODE_PURE_SIM],
                COMPARISON_MODE_PURE_SIM,
            )
            comparison_mode.addItem(
                COMPARISON_MODE_LABELS[COMPARISON_MODE_MODELLED_REAL_STRATEGY],
                COMPARISON_MODE_MODELLED_REAL_STRATEGY,
            )
            comparison_mode.setCurrentIndex(
                comparison_mode.findData(COMPARISON_MODE_MODELLED_REAL_STRATEGY)
            )

            layout.addRow("Driver", driver_label)
            layout.addRow("Stop after lap", stop_label)
            layout.addRow("Compound", compound_row)
            layout.addRow("Tyre age", tyre_age)
            layout.addRow("Pit loss", pit_loss)
            layout.addRow("Lap-time mode", comparison_mode)

            buttons = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.StandardButton.Apply
                | QtWidgets.QDialogButtonBox.StandardButton.Cancel
            )
            layout.addRow(buttons)
            buttons.rejected.connect(dialog.reject)

            def apply() -> None:
                nonlocal applied
                checked = compound_buttons.checkedButton()
                compound = checked.property("compound") if checked is not None else "HARD"
                try:
                    self.service.create_pit_stop_simulation(
                        driver=driver,
                        stop_after_lap=stop_after_lap,
                        compound=str(compound),
                        initial_tyre_age=tyre_age.value(),
                        pit_loss_s=pit_loss.value(),
                        comparison_mode=str(comparison_mode.currentData()),
                        normal_pit_loss_s=self.normal_pit_loss.value(),
                        sc_vsc_pit_loss_s=self.sc_pit_loss.value(),
                    )
                except MissingStrategyModelError as exc:
                    QtWidgets.QMessageBox.critical(self, "Strategy model missing", str(exc))
                    return
                except Exception as exc:
                    QtWidgets.QMessageBox.warning(self, "Strategy simulation", str(exc))
                    return
                applied = True
                dialog.accept()

            buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Apply).clicked.connect(apply)
            dialog.exec()
            if applied:
                QtCore.QTimer.singleShot(0, self._redraw)

        def _plot_mouse_moved(self, position) -> None:
            if not self.curve_data:
                return
            if not self.plot.sceneBoundingRect().contains(position):
                self._set_hover_driver(None)
                self._clear_hover_label()
                self._clear_pit_target()
                return
            point = self.plot.getPlotItem().vb.mapSceneToView(position)
            driver = self._nearest_driver(point.x(), point.y())
            self._set_hover_driver(driver)
            self._update_hover_label(position, driver)
            self._update_pit_target(position, driver)

        def _pit_modifier_active(self) -> bool:
            modifiers = QtWidgets.QApplication.keyboardModifiers()
            return bool(
                modifiers
                & (
                    QtCore.Qt.KeyboardModifier.ControlModifier
                    | QtCore.Qt.KeyboardModifier.MetaModifier
                )
            )

        def _update_pit_target(self, scene_pos, driver: str | None) -> None:
            if not self._pit_modifier_active() or self.service.session is None:
                self._clear_pit_target()
                return
            point = self.plot.getPlotItem().vb.mapSceneToView(scene_pos)
            target_driver = driver or self.selected_driver
            if not target_driver:
                self._clear_pit_target()
                return
            stop_after_lap = self._stop_after_lap_from_x(target_driver, float(point.x()))
            if stop_after_lap is None:
                self._clear_pit_target()
                return
            target = (target_driver, stop_after_lap)
            if target == self.pit_target:
                return
            self._draw_pit_target(target_driver, stop_after_lap)

        def _update_hover_label(self, scene_pos, driver: str | None) -> None:
            if self.service.session is None:
                self._clear_hover_label()
                return
            point = self.plot.getPlotItem().vb.mapSceneToView(scene_pos)
            target_driver = driver or self.selected_driver
            selection = self._resolve_sector_selection(target_driver, float(point.x()))
            if selection is None:
                self._clear_hover_label()
                return
            pit_driver = target_driver if self._pit_modifier_active() else None
            if pit_driver:
                text = self._pit_target_label(pit_driver, int(selection["stop_after_lap"]))
            else:
                text = f"Lap {selection['lap']} {selection['sector']}"
            self._draw_hover_label(text, float(point.x()), float(point.y()))

        def _draw_hover_label(self, text: str, x_position: float, y_position: float) -> None:
            self._clear_hover_label()
            label = pg.TextItem(
                text=text,
                color="#111111",
                anchor=(0.0, 1.0),
                fill=QtGui.QBrush(QtGui.QColor(255, 255, 255, 225)),
                border=pg.mkPen("#111111", width=1.0),
            )
            label.setFont(self._bold_font(12))
            label.setPos(x_position, y_position)
            label.setZValue(50)
            self.plot.addItem(label)
            self.hover_label = label

        def _clear_hover_label(self) -> None:
            if self.hover_label is None:
                return
            try:
                self.plot.removeItem(self.hover_label)
            except Exception:
                pass
            self.hover_label = None

        def _draw_pit_target(self, driver: str, stop_after_lap: int) -> None:
            self._clear_pit_target()
            self.pit_target = (driver, stop_after_lap)
            x_position = stop_after_lap * 3.0
            y_min, y_max = self._ordered_range(self.y_min_input, self.y_max_input)
            x_min, x_max = self._plot_x_range_from_lap_inputs()
            top_y = max(y_min, y_max)
            label_x = min(max(x_position, x_min), x_max)

            pen = pg.mkPen("#111111", width=1.5, style=QtCore.Qt.PenStyle.DashLine)
            line = pg.InfiniteLine(pos=x_position, angle=90, pen=pen, movable=False)
            line.setZValue(40)
            self.plot.addItem(line)

            label = pg.TextItem(
                text=self._pit_target_label(driver, stop_after_lap),
                color="#111111",
                anchor=(0.5, 0.0),
                fill=QtGui.QBrush(QtGui.QColor(255, 255, 255, 220)),
                border=pg.mkPen("#111111", width=1.0),
            )
            label.setFont(self._bold_font(13))
            label.setPos(label_x, top_y)
            label.setZValue(41)
            self.plot.addItem(label)
            self.pit_target_items = [line, label]

        def _clear_pit_target(self) -> None:
            for item in self.pit_target_items:
                try:
                    self.plot.removeItem(item)
                except Exception:
                    pass
            self.pit_target_items = []
            self.pit_target = None

        def _stop_after_lap_from_x(self, driver: str, x_position: float) -> int | None:
            selection = self._resolve_sector_selection(driver, x_position)
            return None if selection is None else int(selection["stop_after_lap"])

        def _resolve_sector_selection(self, driver: str | None, x_position: float) -> dict | None:
            if self.service.session is None:
                return None
            if driver:
                driver_laps = self.service.session.laps.loc[
                    self.service.session.laps["driver"] == driver,
                    "lap",
                ]
            else:
                driver_laps = self.service.session.laps["lap"]
            laps = driver_laps.dropna().astype(int)
            if laps.empty:
                return None
            first_lap = int(laps.min())
            final_lap = int(laps.max())
            segment_index = int(round(float(x_position)))
            segment_index = min(max(segment_index, (first_lap - 1) * 3 + 1), final_lap * 3)
            lap = (segment_index - 1) // 3 + 1
            sector_number = ((segment_index - 1) % 3) + 1
            sector = f"S{sector_number}"
            if sector == "S1":
                stop_after_lap = lap - 1
            else:
                stop_after_lap = lap
            stop_after_lap = min(max(stop_after_lap, first_lap), final_lap - 1)
            if stop_after_lap < first_lap or stop_after_lap >= final_lap:
                return None
            return {
                "driver": driver,
                "lap": int(lap),
                "sector": sector,
                "segment_index": segment_index,
                "stop_after_lap": int(stop_after_lap),
            }

        def _pit_target_label(self, driver: str, stop_after_lap: int) -> str:
            return f"{driver} pit after Lap {stop_after_lap}"

        def _nearest_driver(
            self,
            x_pos: float,
            y_pos: float,
            *,
            max_distance: float = 0.0006,
        ) -> str | None:
            view_range = self.plot.getPlotItem().vb.viewRange()
            x_span = max(view_range[0][1] - view_range[0][0], 1.0)
            y_span = max(abs(view_range[1][1] - view_range[1][0]), 1.0)
            best_driver = None
            best_distance = float("inf")
            for driver, (x_values, y_values) in self.curve_data.items():
                if len(x_values) == 0:
                    continue
                distances = ((x_values - x_pos) / x_span) ** 2 + ((y_values - y_pos) / y_span) ** 2
                distance = float(distances.min())
                if distance < best_distance:
                    best_distance = distance
                    best_driver = driver
            if best_distance <= max_distance:
                return best_driver
            return None

        def _set_hover_driver(self, driver: str | None) -> None:
            if driver == self.hover_driver:
                return
            self.hover_driver = driver
            self._restyle_curves()
            self._update_rank_lists()

        def _update_selection_visuals(self) -> None:
            self._restyle_curves()
            self._update_pit_window()
            self._update_rank_lists()

        def _update_range_controls_from_data(self, plot_data) -> None:
            x = plot_data["lap"].dropna()
            y = plot_data["delta_s"].dropna()
            if x.empty or y.empty:
                return
            x_padding = max((float(x.max()) - float(x.min())) * 0.02, 1.0)
            y_padding = max((float(y.max()) - float(y.min())) * 0.08, 5.0)
            self.x_min_input.setValue(float(x.min()) - x_padding)
            self.x_max_input.setValue(float(x.max()) + x_padding)
            self.y_min_input.setValue(float(y.min()) - y_padding)
            self.y_max_input.setValue(float(y.max()) + y_padding)

        def _update_lap_axis_ticks(self, plot_data) -> None:
            if "segment_index" not in plot_data.columns or "lap" not in plot_data.columns:
                return
            ticks = (
                plot_data.loc[:, ["lap", "segment_index"]]
                .dropna()
                .sort_values(["lap", "segment_index"], kind="mergesort")
                .drop_duplicates(subset=["lap"], keep="last")
            )
            if ticks.empty:
                return
            max_ticks = 20
            step = max(int(math.ceil(len(ticks) / max_ticks)), 1)
            tick_values = [
                (float(row["segment_index"]), str(int(row["lap"])))
                for _, row in ticks.iloc[::step].iterrows()
            ]
            self.plot.getAxis("bottom").setTicks([tick_values])

        def _apply_custom_ranges(self) -> None:
            self.custom_ranges = True
            self._apply_plot_ranges()
            self._draw_lap_delta_guides()

        def _reset_auto_ranges(self) -> None:
            self.custom_ranges = False
            if self.service._last_plot_data is not None:
                self._update_range_controls_from_data(self.service._last_plot_data)
            self._apply_plot_ranges()
            self._draw_lap_delta_guides()

        def _apply_plot_ranges(self) -> None:
            x_min, x_max = self._plot_x_range_from_lap_inputs()
            y_min, y_max = self._ordered_range(self.y_min_input, self.y_max_input)
            self.plot.setXRange(x_min, x_max, padding=0)
            self.plot.setYRange(y_min, y_max, padding=0)

        def _plot_x_range_from_lap_inputs(self) -> tuple[float, float]:
            lap_min, lap_max = self._ordered_range(self.x_min_input, self.x_max_input)
            return self._lap_range_to_plot_x(lap_min, lap_max)

        def _lap_range_to_plot_x(self, lap_min: float, lap_max: float) -> tuple[float, float]:
            first_sector_x = (float(lap_min) - 1.0) * 3.0 + 1.0
            last_sector_x = float(lap_max) * 3.0
            return (first_sector_x, last_sector_x)

        def _ordered_range(self, min_input, max_input) -> tuple[float, float]:
            first = float(min_input.value())
            second = float(max_input.value())
            if first == second:
                second = first + 1.0
            return (min(first, second), max(first, second))

        def _draw_track_status_bands(self) -> None:
            for item in self.track_status_items:
                try:
                    self.plot.removeItem(item)
                except Exception:
                    pass
            self.track_status_items = []
            if self.service.session is None:
                return
            status_data = self.service.build_track_status_event_data()
            if status_data.empty:
                return

            for _, row in status_data.iterrows():
                status_type = str(row["track_status_type"])
                colour = SC_BAND_COLOUR if status_type == "sc" else VSC_BAND_COLOUR
                qcolour = QtGui.QColor(colour)
                qcolour.setAlpha(72)
                segment_index = row.get("segment_index")
                if segment_index is not None and not pd.isna(segment_index):
                    segment_start = float(segment_index) - 0.5
                    segment_end = float(segment_index) + 0.5
                else:
                    lap = float(row["lap"])
                    segment_start = (lap - 1.0) * 3.0 + 0.5
                    segment_end = lap * 3.0 + 0.5
                region = pg.LinearRegionItem(
                    values=(segment_start, segment_end),
                    orientation=pg.LinearRegionItem.Vertical,
                    brush=pg.mkBrush(qcolour),
                    movable=False,
                )
                region.setZValue(-100)
                region.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
                for line in getattr(region, "lines", []):
                    line.setPen(pg.mkPen(qcolour, width=0))
                self.plot.addItem(region)
                self.track_status_items.append(region)

        def _draw_lap_delta_guides(self) -> None:
            for item in self.guide_items:
                try:
                    self.plot.removeItem(item)
                except Exception:
                    pass
            self.guide_items = []
            if self.service.session is None or not self.reference_driver.currentText():
                return

            x_min, x_max = self._plot_x_range_from_lap_inputs()
            y_min, y_max = self._ordered_range(self.y_min_input, self.y_max_input)
            guides = self.service.build_lap_delta_guide_data(
                self.reference_driver.currentText(),
                y_min=y_min,
                y_max=y_max,
            )
            if guides.empty:
                return

            pen = pg.mkPen("#000000", width=1.0, style=QtCore.Qt.PenStyle.DashLine)
            for _, row in guides.iterrows():
                line = self.plot.plot([x_min, x_max], [row["y"], row["y"]], pen=pen)
                line.setZValue(-5)
                label = pg.TextItem(
                    text=str(row["label"]),
                    color="#000000",
                    anchor=(1.0, 0.5),
                )
                label.setFont(self._font(13))
                label.setPos(x_max, float(row["y"]))
                label.setZValue(-4)
                self.plot.addItem(label)
                self.guide_items.extend([line, label])

        def _restyle_curves(self) -> None:
            if self.service.session is None:
                return
            styles = self.service.session.drivers.set_index("driver").to_dict("index")
            for (driver, role, _, is_faded), curve in self.curves.items():
                curve.setPen(
                    self._driver_pen(
                        driver,
                        styles.get(driver, {}),
                        role=role,
                        is_faded=is_faded,
                    )
                )

        def _update_pit_window(self) -> None:
            for item in self.pit_items:
                try:
                    self.plot.removeItem(item)
                except Exception:
                    pass
            self.pit_items = []
            if not self.selected_driver:
                return
            pit_data = self.service.build_pit_window_data(
                self.selected_driver,
                normal_pit_loss_s=self.normal_pit_loss.value(),
                sc_vsc_pit_loss_s=self.sc_pit_loss.value(),
            )
            if pit_data.empty:
                return
            x, normal_lower = finite_xy(
                pit_data.rename(columns={"normal_lower_boundary_s": "boundary"}),
                y_column="boundary",
            )
            _, vsc_lower = finite_xy(
                pit_data.rename(columns={"vsc_lower_boundary_s": "boundary"}),
                y_column="boundary",
            )
            _, vsc_upper = finite_xy(
                pit_data.rename(columns={"vsc_upper_boundary_s": "boundary"}),
                y_column="boundary",
            )
            _, normal_upper = finite_xy(
                pit_data.rename(columns={"normal_upper_boundary_s": "boundary"}),
                y_column="boundary",
            )
            if not len(x) or not (
                len(normal_lower) == len(vsc_lower) == len(vsc_upper) == len(normal_upper)
            ):
                return
            pen = pg.mkPen("#666666", width=1.2, style=QtCore.Qt.PenStyle.DashDotLine)
            normal_lower_curve = self.plot.plot(x, normal_lower, pen=pen)
            vsc_lower_curve = self.plot.plot(x, vsc_lower, pen=pen)
            vsc_upper_curve = self.plot.plot(x, vsc_upper, pen=pen)
            normal_upper_curve = self.plot.plot(x, normal_upper, pen=pen)
            for curve in (
                normal_lower_curve,
                vsc_lower_curve,
                vsc_upper_curve,
                normal_upper_curve,
            ):
                curve.setZValue(5)
            lower_normal_fill = pg.FillBetweenItem(
                normal_lower_curve,
                vsc_lower_curve,
                brush=QtGui.QBrush(QtGui.QColor(95, 95, 95, 55)),
            )
            upper_normal_fill = pg.FillBetweenItem(
                vsc_upper_curve,
                normal_upper_curve,
                brush=QtGui.QBrush(QtGui.QColor(95, 95, 95, 55)),
            )
            vsc_fill = pg.FillBetweenItem(
                vsc_lower_curve,
                vsc_upper_curve,
                brush=QtGui.QBrush(QtGui.QColor(185, 185, 185, 60)),
            )
            lower_normal_fill.setZValue(-20)
            upper_normal_fill.setZValue(-20)
            vsc_fill.setZValue(-19)
            self.plot.addItem(lower_normal_fill)
            self.plot.addItem(upper_normal_fill)
            self.plot.addItem(vsc_fill)
            self.pit_items.extend(
                [
                    normal_lower_curve,
                    vsc_lower_curve,
                    vsc_upper_curve,
                    normal_upper_curve,
                    lower_normal_fill,
                    upper_normal_fill,
                    vsc_fill,
                ]
            )

        def _draw_tyre_markers(self) -> None:
            for item in self.tyre_items:
                try:
                    self.plot.removeItem(item)
                except Exception:
                    pass
            self.tyre_items = []
            tyre_events = self.service.build_tyre_event_data()
            if tyre_events.empty:
                return
            spots = []
            for _, row in tyre_events.iterrows():
                compound = str(row["compound"]).upper()
                colour = QtGui.QColor(COMPOUND_COLOURS.get(compound, "#d9d9d9"))
                if bool(row.get("is_faded", False)):
                    colour.setAlpha(128)
                simulated = bool(row.get("is_simulated", False))
                removed_observed_pit = bool(row.get("is_removed_observed_pit", False))
                scenario_id = row.get("scenario_id")
                scenario_id = None if not self._has_value(scenario_id) else str(scenario_id)
                marker_data = {
                    "scenario_id": scenario_id,
                    "driver": str(row.get("driver")),
                    "stop_after_lap": int(row["lap"]) - 1,
                    "first_lap_after_stop": int(row["lap"]),
                    "is_simulated": simulated,
                    "is_removed_observed_pit": removed_observed_pit,
                    "removed_pit_edit_id": (
                        None
                        if not self._has_value(row.get("removed_pit_edit_id"))
                        else str(row.get("removed_pit_edit_id"))
                    ),
                    "removed_pit_loss_s": row.get("removed_pit_loss_s"),
                    "removed_pit_loss_source": row.get("removed_pit_loss_source"),
                    "compound": compound,
                    "continued_compound": row.get("continued_compound"),
                    "continued_tyre_age": row.get("continued_tyre_age"),
                    "tyre_age_label": row.get("tyre_age_label"),
                }
                spots.append(
                    {
                        "pos": (
                            float(row["segment_index"])
                            if self._has_value(row.get("segment_index"))
                            else float(row["lap"]),
                            float(row["delta_s"]),
                        ),
                        "symbol": "o",
                        "size": TYRE_MARKER_SIZE,
                        "brush": pg.mkBrush(0, 0, 0, 0),
                        "pen": pg.mkPen(
                            colour,
                            width=3.0,
                            style=(
                                QtCore.Qt.PenStyle.DotLine
                                if simulated
                                else QtCore.Qt.PenStyle.SolidLine
                            ),
                        ),
                        "data": marker_data,
                    }
                )
                if removed_observed_pit:
                    spots.append(
                        {
                            "pos": (
                                float(row["segment_index"])
                                if self._has_value(row.get("segment_index"))
                                else float(row["lap"]),
                                float(row["delta_s"]),
                            ),
                            "symbol": "x",
                            "size": TYRE_MARKER_SIZE + 7,
                            "brush": pg.mkBrush(0, 0, 0, 0),
                            "pen": pg.mkPen(
                                QtGui.QColor("#111111"),
                                width=2.0,
                                style=QtCore.Qt.PenStyle.DashLine,
                            ),
                            "data": marker_data,
                        }
                    )
            marker_layer = pg.ScatterPlotItem(spots=spots, pxMode=True)
            marker_layer.setZValue(30)
            marker_layer.sigClicked.connect(self._tyre_marker_clicked)
            self.plot.addItem(marker_layer)
            self.tyre_items.append(marker_layer)

            for _, row in tyre_events.iterrows():
                text_colour = QtGui.QColor(
                    COMPOUND_COLOURS.get(str(row["compound"]).upper(), "#555555")
                )
                if bool(row.get("is_faded", False)):
                    text_colour.setAlpha(128)
                text = pg.TextItem(
                    text=str(row["tyre_age_label"]),
                    color=text_colour,
                    anchor=(0.5, 0.5),
                )
                text.setFont(self._bold_font(TYRE_FONT_POINT_SIZE))
                text_x = (
                    float(row["segment_index"])
                    if self._has_value(row.get("segment_index"))
                    else float(row["lap"])
                )
                text.setPos(text_x, float(row["delta_s"]))
                text.setZValue(31)
                self.plot.addItem(text)
                self.tyre_items.append(text)

        def _tyre_marker_clicked(self, _plot, points) -> None:
            if not points:
                return
            data = points[0].data() or {}
            data = dict(data)
            QtCore.QTimer.singleShot(0, lambda payload=data: self._handle_tyre_marker_clicked(payload))

        def _handle_tyre_marker_clicked(self, data: dict) -> None:
            scenario_id = data.get("scenario_id")
            if bool(data.get("is_removed_observed_pit", False)) and self._has_value(scenario_id):
                menu = QtWidgets.QMenu(self)
                edit_action = menu.addAction("Edit removed-pit-loss estimate")
                restore_action = menu.addAction("Restore real pit")
                delete_action = menu.addAction("Delete this edit")
                detail_action = menu.addAction("Strategy delta details")
                action = menu.exec(QtGui.QCursor.pos())
                if action == edit_action:
                    self._edit_removed_pit_loss(data)
                elif action in {restore_action, delete_action}:
                    edit_id = data.get("removed_pit_edit_id")
                    if self._has_value(edit_id):
                        self.service.remove_strategy_edit_by_id(str(scenario_id), str(edit_id))
                    else:
                        self.service.remove_strategy_edit(
                            str(scenario_id), int(data.get("stop_after_lap"))
                        )
                    QtCore.QTimer.singleShot(0, self._redraw)
                elif action == detail_action:
                    self._show_strategy_delta_details(str(scenario_id))
                return

            if not self._has_value(scenario_id):
                if bool(data.get("is_simulated", False)):
                    return
                menu = QtWidgets.QMenu(self)
                remove_action = menu.addAction("Remove this real pit from strategy")
                action = menu.exec(QtGui.QCursor.pos())
                if action == remove_action:
                    self._open_remove_observed_pit_editor(data)
                return
            menu = QtWidgets.QMenu(self)
            update_action = menu.addAction("Update simulation")
            detail_action = menu.addAction("Strategy delta details")
            delete_action = menu.addAction("Delete simulation")
            action = menu.exec(QtGui.QCursor.pos())
            if action == update_action:
                self._open_strategy_editor(str(data.get("driver")), int(data.get("stop_after_lap")))
            elif action == detail_action:
                self._show_strategy_delta_details(str(scenario_id))
            elif action == delete_action:
                self.service.remove_strategy_edit(str(scenario_id), int(data.get("stop_after_lap")))
                QtCore.QTimer.singleShot(0, self._redraw)

        def _edit_removed_pit_loss(self, data: dict) -> None:
            scenario_id = data.get("scenario_id")
            if not self._has_value(scenario_id):
                return
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle("Removed pit")
            layout = QtWidgets.QFormLayout(dialog)
            driver = str(data.get("driver"))
            first_lap = int(data.get("first_lap_after_stop"))
            stop_after_lap = int(data.get("stop_after_lap"))
            loss = QtWidgets.QDoubleSpinBox()
            loss.setRange(0.0, 120.0)
            loss.setDecimals(1)
            loss.setSingleStep(0.5)
            loss.setSuffix(" s")
            try:
                loss.setValue(float(data.get("removed_pit_loss_s")))
            except (TypeError, ValueError):
                loss.setValue(self.normal_pit_loss.value())
            layout.addRow("Driver", QtWidgets.QLabel(driver))
            layout.addRow("Removed pit after lap", QtWidgets.QLabel(str(stop_after_lap)))
            layout.addRow("First affected lap", QtWidgets.QLabel(str(first_lap)))
            layout.addRow("Original compound", QtWidgets.QLabel(str(data.get("compound", ""))))
            layout.addRow(
                "Continued compound",
                QtWidgets.QLabel(str(data.get("continued_compound") or "")),
            )
            layout.addRow("Pit-loss estimate", loss)
            layout.addRow(
                "Pit-loss source",
                QtWidgets.QLabel(str(data.get("removed_pit_loss_source") or "")),
            )
            buttons = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.StandardButton.Apply
                | QtWidgets.QDialogButtonBox.StandardButton.Cancel
            )
            layout.addRow(buttons)
            buttons.rejected.connect(dialog.reject)

            def apply() -> None:
                scenario = self.service.scenarios.get(str(scenario_id))
                comparison_mode = (
                    scenario.comparison_mode
                    if scenario is not None
                    else COMPARISON_MODE_MODELLED_REAL_STRATEGY
                )
                try:
                    self.service.create_remove_observed_pit_simulation(
                        driver=driver,
                        first_lap_after_stop=first_lap,
                        pit_loss_s=loss.value(),
                        pit_loss_source="manual",
                        comparison_mode=comparison_mode,
                        normal_pit_loss_s=self.normal_pit_loss.value(),
                        sc_vsc_pit_loss_s=self.sc_pit_loss.value(),
                    )
                except Exception as exc:
                    QtWidgets.QMessageBox.warning(self, "Strategy simulation", str(exc))
                    return
                dialog.accept()

            buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Apply).clicked.connect(apply)
            if dialog.exec():
                QtCore.QTimer.singleShot(0, self._redraw)

        def _open_remove_observed_pit_editor(self, data: dict) -> None:
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle("Remove real pit")
            layout = QtWidgets.QFormLayout(dialog)
            driver = str(data.get("driver"))
            first_lap = int(data.get("first_lap_after_stop"))
            stop_after_lap = int(data.get("stop_after_lap"))
            loss = QtWidgets.QDoubleSpinBox()
            loss.setRange(0.0, 120.0)
            loss.setDecimals(1)
            loss.setSingleStep(0.5)
            loss.setSuffix(" s")
            try:
                loss.setValue(
                    self.service.pit_loss_default_for_stop(
                        stop_after_lap=stop_after_lap,
                        normal_pit_loss_s=self.normal_pit_loss.value(),
                        sc_vsc_pit_loss_s=self.sc_pit_loss.value(),
                    )
                )
            except Exception:
                loss.setValue(self.normal_pit_loss.value())
            comparison_mode = QtWidgets.QComboBox()
            comparison_mode.addItem(
                COMPARISON_MODE_LABELS[COMPARISON_MODE_PURE_SIM],
                COMPARISON_MODE_PURE_SIM,
            )
            comparison_mode.addItem(
                COMPARISON_MODE_LABELS[COMPARISON_MODE_MODELLED_REAL_STRATEGY],
                COMPARISON_MODE_MODELLED_REAL_STRATEGY,
            )
            comparison_mode.setCurrentIndex(
                comparison_mode.findData(COMPARISON_MODE_MODELLED_REAL_STRATEGY)
            )

            layout.addRow("Driver", QtWidgets.QLabel(driver))
            layout.addRow("Remove pit after lap", QtWidgets.QLabel(str(stop_after_lap)))
            layout.addRow("First affected lap", QtWidgets.QLabel(str(first_lap)))
            layout.addRow("Original compound", QtWidgets.QLabel(str(data.get("compound", ""))))
            layout.addRow("Pit-loss estimate", loss)
            layout.addRow("Lap-time mode", comparison_mode)
            buttons = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.StandardButton.Apply
                | QtWidgets.QDialogButtonBox.StandardButton.Cancel
            )
            layout.addRow(buttons)
            buttons.rejected.connect(dialog.reject)

            def apply() -> None:
                try:
                    self.service.create_remove_observed_pit_simulation(
                        driver=driver,
                        first_lap_after_stop=first_lap,
                        pit_loss_s=loss.value(),
                        pit_loss_source="manual",
                        comparison_mode=str(comparison_mode.currentData()),
                        normal_pit_loss_s=self.normal_pit_loss.value(),
                        sc_vsc_pit_loss_s=self.sc_pit_loss.value(),
                    )
                except MissingStrategyModelError as exc:
                    QtWidgets.QMessageBox.critical(self, "Strategy model missing", str(exc))
                    return
                except Exception as exc:
                    QtWidgets.QMessageBox.warning(self, "Strategy simulation", str(exc))
                    return
                dialog.accept()

            buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Apply).clicked.connect(apply)
            if dialog.exec():
                QtCore.QTimer.singleShot(0, self._redraw)

        def _clear_all_simulations(self) -> None:
            self.service.clear_all_simulations()
            self._redraw()

        def _show_assumptions(self) -> None:
            scenarios = self.service.scenarios.active_scenarios()
            if not scenarios:
                QtWidgets.QMessageBox.information(
                    self,
                    "Strategy assumptions",
                    "No active simulations.",
                )
                return
            rows = []
            for scenario in scenarios:
                edit_lines = []
                for edit in scenario.edits:
                    if edit.edit_type == "remove_observed_pit":
                        edit_lines.append(
                            f"Remove real pit after lap {edit.stop_after_lap}: "
                            f"continue {edit.compound}, age {edit.initial_tyre_age}, "
                            f"loss {float(edit.pit_loss_s or 0.0):.1f} s "
                            f"({edit.pit_loss_source or 'estimated'})"
                        )
                    else:
                        edit_lines.append(
                            f"Pit after lap {edit.stop_after_lap}: "
                            f"{edit.compound}, age {edit.initial_tyre_age}, "
                            f"pit loss {float(edit.pit_loss_s or 0.0):.1f} s"
                        )
                first_simulated_lap = min(
                    int(edit.stop_after_lap) for edit in scenario.edits
                ) + 1
                rows.append(
                    f"{scenario.driver} - {len(scenario.edits)} strategy edit(s)\n"
                    + "\n".join(edit_lines)
                    + "\nLap-time mode: "
                    f"{COMPARISON_MODE_LABELS.get(scenario.comparison_mode, scenario.comparison_mode)}\n"
                    f"First simulated lap: {first_simulated_lap}\n"
                    "Model source: woStrategy race-performance-review median"
                )
            warnings = []
            for comparison in self.service.scenarios.active_comparisons():
                warnings.extend(comparison.warnings or [])
            if warnings:
                rows.append("Warnings:\n" + "\n".join(dict.fromkeys(warnings)))
            QtWidgets.QMessageBox.information(
                self,
                "Strategy assumptions",
                "\n\n".join(rows),
            )

        def _show_strategy_delta_details(self, scenario_id: str | None = None) -> None:
            delta_data = self.service.build_strategy_delta_data(
                reference_driver=self.reference_driver.currentText(),
                reference_mode=str(self.reference_mode.currentData() or REFERENCE_MODE_AVERAGED_PACE),
            )
            if scenario_id:
                delta_data = delta_data.loc[delta_data["scenario_id"].astype(str) == str(scenario_id)]
            if delta_data.empty:
                QtWidgets.QMessageBox.information(
                    self,
                    "Strategy deltas",
                    "No active strategy delta data.",
                )
                return

            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle("Strategy delta details")
            dialog.resize(920, 560)
            layout = QtWidgets.QVBoxLayout(dialog)

            detail_plot = pg.PlotWidget()
            detail_plot.setLabel("bottom", "Lap", **{"font-size": "12pt"})
            detail_plot.setLabel("left", "Delta to reference", units="s", **{"font-size": "12pt"})
            detail_plot.setBackground("w")
            detail_plot.addLegend(offset=(8, 8))
            styles = (
                self.service.session.drivers.set_index("driver").to_dict("index")
                if self.service.session is not None
                else {}
            )
            for (_, driver), rows in delta_data.groupby(["scenario_id", "driver"], sort=False):
                x, y = finite_xy(rows, y_column="cumulative_delta_s")
                if not len(x):
                    continue
                colour = styles.get(driver, {}).get("colour", "#555555")
                label = COMPARISON_MODE_LABELS.get(
                    str(rows["comparison_mode"].iloc[0]),
                    str(rows["comparison_mode"].iloc[0]),
                )
                detail_plot.plot(
                    x,
                    y,
                    pen=pg.mkPen(QtGui.QColor(colour), width=2.2),
                    name=f"{driver} - {label}",
                )
            x_values = delta_data["lap"].dropna()
            if not x_values.empty:
                detail_plot.plot(
                    [float(x_values.min()), float(x_values.max())],
                    [0.0, 0.0],
                    pen=pg.mkPen("#777777", width=1.0, style=QtCore.Qt.PenStyle.DashLine),
                )
            layout.addWidget(detail_plot, stretch=1)

            columns = [
                ("lap", "Lap"),
                ("sector", "Sector"),
                ("observed_sector_time_s", "Observed sector"),
                ("baseline_model_sector_time_s", "Baseline sector"),
                ("sim_model_sector_time_s", "Sim model sector"),
                ("race_domain_residual_s", "Race residual"),
                ("display_residual_s", "Applied residual"),
                ("display_segment_time_s", "Display segment"),
                ("reference_segment_time_s", "Reference segment"),
                ("segment_delta_to_reference_s", "Segment vs ref"),
                ("delta_to_reference_s", "Delta to ref"),
                ("model_delta_sector_s", "Model delta"),
                ("cumulative_model_delta_s", "Cumulative model delta"),
                ("compound", "Compound"),
                ("tyre_age", "Tyre age"),
                ("pit_in_loss_s", "Pit in"),
                ("pit_out_loss_s", "Pit out"),
            ]
            table = QtWidgets.QTableWidget(len(delta_data), len(columns))
            table.setHorizontalHeaderLabels([label for _, label in columns])
            table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
            for row_index, (_, row) in enumerate(delta_data.reset_index(drop=True).iterrows()):
                for column_index, (column, _) in enumerate(columns):
                    value = row.get(column, "")
                    if column.endswith("_s") or column in {
                        "what_if_lap_time_s",
                        "baseline_lap_time_s",
                    }:
                        try:
                            text = f"{float(value):.3f}"
                        except (TypeError, ValueError):
                            text = str(value)
                    else:
                        text = str(value)
                    table.setItem(row_index, column_index, QtWidgets.QTableWidgetItem(text))
            table.resizeColumnsToContents()
            layout.addWidget(table, stretch=2)

            buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            dialog.exec()

        def _update_rank_lists(self) -> None:
            if self.service.session is None:
                self.start_rank_list.clear()
                self.latest_rank_list.clear()
                return
            drivers = self.service.session.drivers
            self._fill_rank_list(self.start_rank_list, drivers, "start_rank")
            self._fill_rank_list(self.latest_rank_list, drivers, "latest_rank")

        def _fill_rank_list(self, rank_list, drivers, rank_column: str) -> None:
            rank_list.clear()
            if rank_column not in drivers.columns:
                return
            prefix = "start" if rank_column == "start_rank" else "latest"
            ranked = drivers.dropna(subset=[rank_column]).sort_values(
                [rank_column, "driver"],
                kind="mergesort",
            )
            for _, row in ranked.iterrows():
                item = QtWidgets.QListWidgetItem()
                rank_list.addItem(item)
                widget = self._rank_row_widget(row, rank_column=rank_column, tyre_prefix=prefix)
                item.setSizeHint(widget.sizeHint())
                rank_list.setItemWidget(item, widget)

        def _rank_row_widget(self, row, *, rank_column: str, tyre_prefix: str):
            widget = QtWidgets.QWidget()
            layout = QtWidgets.QHBoxLayout(widget)
            layout.setContentsMargins(4, 2, 4, 2)
            layout.setSpacing(6)

            text = QtWidgets.QLabel(f"{int(row[rank_column]):>2}  {row['driver']}")
            text.setFont(self._rank_font(str(row["driver"])))
            text.setStyleSheet(f"color: {row.get('colour', '#333333')};")
            layout.addWidget(text)
            if rank_column == "start_rank":
                colour = str(row.get("colour", "#333333"))
                line_style = str(row.get("line_style", "solid"))
                layout.addWidget(
                    self._line_sample_label(
                        colour,
                        line_style=line_style,
                        role="actual",
                    )
                )
                if self._driver_has_simulation(str(row["driver"])):
                    comma = QtWidgets.QLabel(",")
                    comma.setFont(self._font(max(RANK_FONT_POINT_SIZE - 4, 10)))
                    comma.setStyleSheet("color: #666666;")
                    layout.addWidget(comma)
                    layout.addWidget(
                        self._line_sample_label(
                            colour,
                            line_style=line_style,
                            role="simulation",
                        )
                    )
            layout.addStretch(1)

            compound = row.get(f"{tyre_prefix}_tyre_compound")
            age_label = row.get(f"{tyre_prefix}_tyre_age_label")
            if self._has_value(age_label):
                layout.addWidget(self._tyre_badge(str(age_label), compound))
            return widget

        def _has_value(self, value) -> bool:
            return value is not None and str(value) not in {"", "nan", "<NA>", "NaT"}

        def _rank_font(self, driver: str):
            font = self._font(RANK_FONT_POINT_SIZE)
            if driver in {self.selected_driver, self.hover_driver}:
                font.setBold(True)
            return font

        def _driver_has_simulation(self, driver: str) -> bool:
            return any(
                scenario.driver == driver for scenario in self.service.scenarios.active_scenarios()
            )

        def _line_sample_label(
            self,
            colour: str,
            *,
            line_style: str,
            role: str,
        ) -> QtWidgets.QLabel:
            width = 28
            height = 12
            pixmap = QtGui.QPixmap(width, height)
            pixmap.fill(QtCore.Qt.GlobalColor.transparent)
            painter = QtGui.QPainter(pixmap)
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            if role == "simulation":
                pen_style = QtCore.Qt.PenStyle.CustomDashLine
            else:
                pen_style = (
                    QtCore.Qt.PenStyle.DashLine
                    if line_style == "dash"
                    else QtCore.Qt.PenStyle.SolidLine
                )
            pen = QtGui.QPen(QtGui.QColor(colour), 2.4, pen_style)
            if role == "simulation":
                pen.setDashPattern(
                    [1.0, 3.0, 4.0, 3.0]
                    if line_style == "dash"
                    else [1.0, 2.0, 8.0, 2.0]
                )
            pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(2, height // 2, width - 2, height // 2)
            painter.end()

            label = QtWidgets.QLabel()
            label.setPixmap(pixmap)
            label.setFixedSize(width, height)
            return label

        def _tyre_badge(self, label: str, compound) -> QtWidgets.QLabel:
            colour = COMPOUND_COLOURS.get(str(compound).upper(), "#777777")
            badge = QtWidgets.QLabel(str(label))
            badge.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            badge.setFont(self._bold_font(RANK_FONT_POINT_SIZE))
            badge.setFixedSize(28, 28)
            badge.setStyleSheet(
                "QLabel {"
                f"color: {colour};"
                f"border: 2px solid {colour};"
                "border-radius: 14px;"
                "background: transparent;"
                "}"
            )
            return badge

        def _export_png(self) -> None:
            if self.service.session is None:
                QtWidgets.QMessageBox.information(
                    self,
                    "Export PNG",
                    "Load a race before exporting.",
                )
                return
            default_name = (
                f"woplanner_{self.service.session.year}_"
                f"{self.service.session.round_number}_"
                f"{self.service.session.session_name}.png"
            )
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self,
                "Export PNG",
                default_name,
                "PNG images (*.png)",
            )
            if not path:
                return
            if not path.lower().endswith(".png"):
                path = f"{path}.png"

            pixmap = self._grab_export_pixmap()
            if not pixmap.save(path, "PNG"):
                QtWidgets.QMessageBox.warning(
                    self,
                    "Export PNG",
                    f"Could not save image to {path}",
                )

        def _copy_png_to_clipboard(self) -> None:
            if self.service.session is None:
                QtWidgets.QMessageBox.information(
                    self,
                    "Export PNG",
                    "Load a race before exporting.",
                )
                return
            pixmap = self._grab_export_pixmap()
            QtWidgets.QApplication.clipboard().setPixmap(pixmap)

        def _grab_export_pixmap(self) -> QtGui.QPixmap:
            hidden_items = []
            for item in self._transient_export_items():
                try:
                    was_visible = bool(item.isVisible())
                    item.setVisible(False)
                    hidden_items.append((item, was_visible))
                except Exception:
                    continue
            try:
                QtWidgets.QApplication.processEvents()
                return self.plot_row_widget.grab()
            finally:
                for item, was_visible in hidden_items:
                    try:
                        item.setVisible(was_visible)
                    except Exception:
                        pass
                QtWidgets.QApplication.processEvents()

        def _transient_export_items(self) -> list:
            items = []
            if self.hover_label is not None:
                items.append(self.hover_label)
            items.extend(self.pit_target_items)
            return items

    app = QtWidgets.QApplication(sys.argv)
    window = RacePlannerWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
