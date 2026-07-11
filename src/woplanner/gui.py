from __future__ import annotations

import math
import sys

from .service import RacePlannerService, finite_xy
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


class RacePlannerWindowMixin:
    def _init_state(self) -> None:
        self.service = RacePlannerService()
        self.curves = {}
        self.curve_data = {}
        self.selected_driver = None
        self.hover_driver = None
        self.pit_items = []
        self.tyre_items = []
        self.guide_items = []
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
            self.clear_sims_button = QtWidgets.QPushButton("Clear sims")
            self.export_menu = QtWidgets.QMenu(self.export_button)
            self.export_file_action = self.export_menu.addAction("PNG to file")
            self.export_clipboard_action = self.export_menu.addAction("PNG to clipboard")
            self.export_button.setMenu(self.export_menu)
            self.reference_driver = QtWidgets.QComboBox()
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
                ("X min", self.x_min_input),
                ("X max", self.x_max_input),
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
            self.line_width.valueChanged.connect(self._redraw)
            self.normal_pit_loss.valueChanged.connect(self._update_pit_window)
            self.sc_pit_loss.valueChanged.connect(self._update_pit_window)
            self.assumptions_button.clicked.connect(self._show_assumptions)
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
            self.plot.clear()
            self.curves = {}
            self.curve_data = {}
            self.pit_items = []
            self.tyre_items = []
            self.guide_items = []
            plot_data = self.service.build_plot_data(self.reference_driver.currentText())
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
                if str(role) == "actual":
                    self.curve_data[driver] = (x, y)
            if not self.custom_ranges:
                self._update_range_controls_from_data(plot_data)
            self._apply_plot_ranges()
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
                width = max(base_width + 1.0, 3.0)
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
            driver = preferred_driver or self.hover_driver or self.selected_driver
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
            pit_loss.setValue(self.normal_pit_loss.value())

            layout.addRow("Driver", driver_label)
            layout.addRow("Stop after lap", stop_label)
            layout.addRow("Compound", compound_row)
            layout.addRow("Tyre age", tyre_age)
            layout.addRow("Pit loss", pit_loss)

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
                self._clear_pit_target()
                return
            point = self.plot.getPlotItem().vb.mapSceneToView(position)
            driver = self._nearest_driver(point.x(), point.y())
            self._set_hover_driver(driver)
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

        def _draw_pit_target(self, driver: str, stop_after_lap: int) -> None:
            self._clear_pit_target()
            self.pit_target = (driver, stop_after_lap)
            x_position = stop_after_lap + 0.5
            y_min, y_max = self._ordered_range(self.y_min_input, self.y_max_input)
            x_min, x_max = self._ordered_range(self.x_min_input, self.x_max_input)
            top_y = max(y_min, y_max)
            label_x = min(max(x_position, x_min), x_max)

            pen = pg.mkPen("#111111", width=1.5, style=QtCore.Qt.PenStyle.DashLine)
            line = pg.InfiniteLine(pos=x_position, angle=90, pen=pen, movable=False)
            line.setZValue(40)
            self.plot.addItem(line)

            label = pg.TextItem(
                text=f"{driver}  pit after lap {stop_after_lap}",
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
            if self.service.session is None:
                return None
            driver_laps = self.service.session.laps.loc[
                self.service.session.laps["driver"] == driver,
                "lap",
            ]
            laps = driver_laps.dropna().astype(int)
            if laps.empty:
                return None
            stop_after_lap = int(math.floor(x_position))
            first_lap = int(laps.min())
            final_lap = int(laps.max())
            stop_after_lap = min(max(stop_after_lap, first_lap), final_lap - 1)
            if stop_after_lap < first_lap or stop_after_lap >= final_lap:
                return None
            return stop_after_lap

        def _nearest_driver(self, x_pos: float, y_pos: float) -> str | None:
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
            if best_distance <= 0.0006:
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
            x_min, x_max = self._ordered_range(self.x_min_input, self.x_max_input)
            y_min, y_max = self._ordered_range(self.y_min_input, self.y_max_input)
            self.plot.setXRange(x_min, x_max, padding=0)
            self.plot.setYRange(y_min, y_max, padding=0)

        def _ordered_range(self, min_input, max_input) -> tuple[float, float]:
            first = float(min_input.value())
            second = float(max_input.value())
            if first == second:
                second = first + 1.0
            return (min(first, second), max(first, second))

        def _draw_lap_delta_guides(self) -> None:
            for item in self.guide_items:
                try:
                    self.plot.removeItem(item)
                except Exception:
                    pass
            self.guide_items = []
            if self.service.session is None or not self.reference_driver.currentText():
                return

            x_min, x_max = self._ordered_range(self.x_min_input, self.x_max_input)
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
                scenario_id = row.get("scenario_id")
                scenario_id = None if not self._has_value(scenario_id) else str(scenario_id)
                marker_data = {
                    "scenario_id": scenario_id,
                    "driver": str(row.get("driver")),
                    "stop_after_lap": int(row["lap"]) - 1,
                }
                spots.append(
                    {
                        "pos": (float(row["lap"]), float(row["delta_s"])),
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
                text.setPos(float(row["lap"]), float(row["delta_s"]))
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
            if not self._has_value(scenario_id):
                return
            menu = QtWidgets.QMenu(self)
            update_action = menu.addAction("Update simulation")
            delete_action = menu.addAction("Delete simulation")
            action = menu.exec(QtGui.QCursor.pos())
            if action == update_action:
                self._open_strategy_editor(str(data.get("driver")), int(data.get("stop_after_lap")))
            elif action == delete_action:
                self.service.clear_simulation(str(scenario_id))
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
            rows = [
                (
                    f"{scenario.driver}: stop after lap {scenario.stop_after_lap}, "
                    f"{scenario.compound} age {scenario.initial_tyre_age}, "
                    f"pit loss {scenario.pit_loss_s:.1f} s"
                )
                for scenario in scenarios
            ]
            QtWidgets.QMessageBox.information(
                self,
                "Strategy assumptions",
                "\n".join(rows),
            )

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

            pixmap = self.plot_row_widget.grab()
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
            pixmap = self.plot_row_widget.grab()
            QtWidgets.QApplication.clipboard().setPixmap(pixmap)

    app = QtWidgets.QApplication(sys.argv)
    window = RacePlannerWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
