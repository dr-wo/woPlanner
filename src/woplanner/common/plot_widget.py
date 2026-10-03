from __future__ import annotations

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from .visual_styles import COMPOUND_COLOURS
from .plot_interactions import HoverLabelOverlay, bind_driver_curve_click


TYRE_MARKER_SIZE = 20


def tyre_age_label(value: object) -> str | None:
    age = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(age):
        return None
    return str(max(int(round(float(age))) - 1, 0))


def format_relative_delta_hover_text(row: pd.Series) -> str:
    driver_value = row.get("driver")
    driver = "" if pd.isna(driver_value) else str(driver_value)
    lap_value = pd.to_numeric(pd.Series([row.get("lap")]), errors="coerce").iloc[0]
    lap = "?" if pd.isna(lap_value) else str(int(lap_value))
    sector = str(row.get("sector") or "")
    delta_value = pd.to_numeric(pd.Series([row.get("delta_s")]), errors="coerce").iloc[0]
    delta = "—" if pd.isna(delta_value) else f"{float(delta_value):+.3f} s"
    role_value = row.get("plot_role")
    role = "" if pd.isna(role_value) else str(role_value).strip().lower()
    role_label = "  Simulation" if role == "simulation" else ""
    lines = [
        f"{driver}{role_label}  Lap {lap}{f' {sector}' if sector else ''}",
        f"Delta {delta}",
    ]
    compound = row.get("compound")
    age = tyre_age_label(row.get("tyre_age"))
    if compound is not None and str(compound) not in {"", "nan", "<NA>"}:
        tyre = str(compound).upper()
        if age is not None:
            tyre += f"  tyre age {age}"
        lines.append(tyre)
    return "\n".join(lines)


# Backwards-compatible public name retained for callers and tests.
format_live_hover_text = format_relative_delta_hover_text


def nearest_relative_delta_row(
    rows: pd.DataFrame,
    x_position: float,
    y_position: float,
    *,
    x_span: float,
    y_span: float,
    driver: str | None = None,
    max_distance: float | None = None,
) -> pd.Series | None:
    """Return the visually nearest plotted row using view-normalized distance."""
    if rows is None or rows.empty:
        return None
    candidates = rows
    if driver is not None:
        candidates = candidates.loc[candidates["driver"].astype(str).eq(str(driver))]
    if candidates.empty:
        return None
    if "_plot_x" in candidates.columns:
        x = pd.to_numeric(candidates["_plot_x"], errors="coerce")
    elif "segment_index" in candidates.columns:
        x = pd.to_numeric(candidates["segment_index"], errors="coerce")
    elif "x_lap" in candidates.columns:
        x = pd.to_numeric(candidates["x_lap"], errors="coerce")
    else:
        x = pd.to_numeric(candidates["lap"], errors="coerce")
    y_column = "_plot_y" if "_plot_y" in candidates.columns else "delta_s"
    y = pd.to_numeric(candidates[y_column], errors="coerce")
    valid = x.notna() & y.notna() & np.isfinite(x) & np.isfinite(y)
    if not valid.any():
        return None
    candidates = candidates.loc[valid]
    distances = (
        ((x.loc[valid] - float(x_position)) / max(abs(float(x_span)), 1.0)) ** 2
        + ((y.loc[valid] - float(y_position)) / max(abs(float(y_span)), 1.0)) ** 2
    )
    index = distances.idxmin()
    if max_distance is not None and float(distances.loc[index]) > float(max_distance):
        return None
    return candidates.loc[index]


class RelativeDeltaPlotWidget(QtWidgets.QWidget):
    driver_selected = QtCore.Signal(str)
    selection_cleared = QtCore.Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._drivers = pd.DataFrame()
        self._line_width = 3.0
        self._hover_enabled = False
        self._hover_driver: str | None = None
        self._selected_driver: str | None = None
        self._hover_label = None
        self._curve_points: dict[str, pd.DataFrame] = {}
        self._styles: dict[str, dict] = {}
        self._tyre_items = []
        self.curves = {}
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.start_rank_list = QtWidgets.QListWidget()
        self.latest_rank_list = QtWidgets.QListWidget()
        for rank_list in (self.start_rank_list, self.latest_rank_list):
            rank_list.setFixedWidth(190)
            rank_list.setIconSize(QtCore.QSize(28, 28))
            rank_list.setAlternatingRowColors(True)
            rank_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        start_panel = QtWidgets.QVBoxLayout()
        self.start_rank_label = QtWidgets.QLabel("Start")
        start_panel.addWidget(self.start_rank_label)
        start_panel.addWidget(self.start_rank_list)
        latest_panel = QtWidgets.QVBoxLayout()
        self.latest_rank_label = QtWidgets.QLabel("Current/Result")
        latest_panel.addWidget(self.latest_rank_label)
        latest_panel.addWidget(self.latest_rank_list)
        self.plot = pg.PlotWidget()
        self.plot.setLabel("bottom", "Lap", **{"font-size": "17pt"})
        self.plot.setLabel("left", "Cumulative delta", units="s", **{"font-size": "17pt"})
        self.plot.setBackground("w")
        self.plot.getPlotItem().invertY(True)
        self.plot.scene().sigMouseMoved.connect(self._mouse_moved)
        self.plot.scene().sigMouseClicked.connect(self._plot_clicked)
        self._hover_overlay = HoverLabelOverlay(self.plot)
        layout.addLayout(start_panel)
        layout.addWidget(self.plot, stretch=1)
        layout.addLayout(latest_panel)

    def set_session_drivers(self, drivers: pd.DataFrame) -> None:
        self._drivers = drivers.copy()

    def set_plot_data(self, plot_data: pd.DataFrame) -> None:
        self.plot.clear()
        self.curves = {}
        self._curve_points = {}
        self._tyre_items = []
        self._hover_label = None
        self._hover_driver = None
        if plot_data.empty:
            return
        self._styles = (
            self._drivers.set_index("driver").to_dict("index")
            if not self._drivers.empty else {}
        )
        for driver, data in plot_data.groupby("driver", sort=False):
            driver = str(driver)
            style = self._styles.get(driver, {})
            x = pd.to_numeric(data.get("x_lap", data["lap"]), errors="coerce")
            y = pd.to_numeric(data["delta_s"], errors="coerce")
            valid = x.notna() & y.notna() & np.isfinite(x) & np.isfinite(y)
            points = data.loc[valid].copy()
            points["_plot_x"] = x.loc[valid].to_numpy()
            points["_plot_y"] = y.loc[valid].to_numpy()
            self._curve_points[driver] = points
            # A line alone cannot show the first provisional observation.
            singleton_provisional = (
                len(points) == 1 and "timing_state" in points
                and points.iloc[0]["timing_state"] == "provisional"
            )
            curve = self.plot.plot(
                x.loc[valid].to_numpy(), y.loc[valid].to_numpy(),
                pen=self._curve_pen(driver),
                clickable=True,
                symbol="o" if singleton_provisional else None,
                symbolSize=7,
                symbolBrush=style.get("colour", "#333333"),
                symbolPen=style.get("colour", "#333333"),
            )
            bind_driver_curve_click(curve, driver, self._curve_clicked)
            self.curves[driver] = curve

    def set_selected_driver(self, driver: str | None) -> None:
        normalized = None if driver is None else str(driver)
        if normalized == self._selected_driver:
            return
        self._selected_driver = normalized
        for curve_driver, curve in self.curves.items():
            curve.setPen(self._curve_pen(curve_driver))

    @property
    def selected_driver(self) -> str | None:
        return self._selected_driver

    def set_hover_enabled(self, enabled: bool) -> None:
        self._hover_enabled = bool(enabled)
        if not enabled:
            self._set_hover_driver(None)
            self._clear_hover_label()

    def set_tyre_event_data(self, tyre_events: pd.DataFrame) -> None:
        for item in self._tyre_items:
            try:
                self.plot.removeItem(item)
            except Exception:
                pass
        self._tyre_items = []
        if tyre_events.empty:
            return
        spots = []
        labels = []
        for _, row in tyre_events.iterrows():
            x_value = _live_plot_x(row)
            y_value = row.get("delta_s")
            if pd.isna(x_value) or pd.isna(y_value):
                continue
            compound_value = row.get("compound")
            compound = "" if pd.isna(compound_value) else str(compound_value).upper()
            colour = COMPOUND_COLOURS.get(compound, "#777777")
            spots.append(
                {
                    "pos": (float(x_value), float(y_value)),
                    "symbol": "o",
                    "size": TYRE_MARKER_SIZE,
                    "brush": pg.mkBrush(0, 0, 0, 0),
                    "pen": pg.mkPen(colour, width=3.0),
                }
            )
            labels.append(
                (
                    float(x_value),
                    float(y_value),
                    str(row.get("tyre_age_label", "?")),
                    colour,
                )
            )
        if not spots:
            return
        marker_layer = pg.ScatterPlotItem(spots=spots, pxMode=True)
        marker_layer.setZValue(30)
        self.plot.addItem(marker_layer)
        self._tyre_items.append(marker_layer)
        for x_value, y_value, label_value, colour in labels:
            label = pg.TextItem(text=label_value, color=colour, anchor=(0.5, 0.5))
            font = QtGui.QFont()
            font.setPointSize(12)
            font.setBold(True)
            label.setFont(font)
            label.setPos(x_value, y_value)
            label.setZValue(31)
            self.plot.addItem(label)
            self._tyre_items.append(label)

    def set_rank_data(self, start: pd.DataFrame, current: pd.DataFrame) -> None:
        self._fill_rank_list(self.start_rank_list, start, tyre_prefix="start")
        self._fill_rank_list(self.latest_rank_list, current, tyre_prefix="latest")

    @staticmethod
    def _fill_rank_list(widget, ranks: pd.DataFrame, *, tyre_prefix: str) -> None:
        widget.clear()
        if ranks.empty:
            return
        for _, row in ranks.sort_values(["rank", "driver"], kind="mergesort").iterrows():
            text = f"{int(row['rank']):>2}  {row['driver']}"
            compound = row.get(f"{tyre_prefix}_tyre_compound")
            age = row.get(f"{tyre_prefix}_tyre_age_label")
            item = QtWidgets.QListWidgetItem(text)
            item.setForeground(QtGui.QColor(str(row.get("colour", "#333333"))))
            if pd.notna(compound) and pd.notna(age):
                item.setIcon(RelativeDeltaPlotWidget._tyre_icon(str(compound), str(age)))
                item.setToolTip(f"{str(compound).upper()}, tyre age {age}")
            widget.addItem(item)

    @staticmethod
    def _tyre_icon(compound: str, age: str) -> QtGui.QIcon:
        colour = QtGui.QColor(COMPOUND_COLOURS.get(compound.upper(), "#777777"))
        pixmap = QtGui.QPixmap(28, 28)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(colour, 2.0))
        painter.drawEllipse(2, 2, 24, 24)
        font = QtGui.QFont()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(colour)
        painter.drawText(pixmap.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, age)
        painter.end()
        return QtGui.QIcon(pixmap)

    def set_line_width(self, width: float) -> None:
        self._line_width = float(width)

    def clear(self) -> None:
        self.plot.clear()
        self.start_rank_list.clear()
        self.latest_rank_list.clear()
        self.curves = {}
        self._curve_points = {}
        self._tyre_items = []
        self._hover_label = None

    def _curve_pen(self, driver: str):
        style = self._styles.get(driver, {})
        pen_style = (
            QtCore.Qt.PenStyle.DashLine
            if style.get("line_style") == "dash"
            else QtCore.Qt.PenStyle.SolidLine
        )
        width = self._line_width
        if driver == self._hover_driver:
            width = max(width * 1.7, width + 1.2)
        if driver == self._selected_driver:
            width = max(width * 2.2, width + 2.0)
        return pg.mkPen(style.get("colour", "#555555"), width=width, style=pen_style)

    def _curve_clicked(self, driver: str, event) -> None:
        self.set_selected_driver(driver)
        self.driver_selected.emit(driver)
        if hasattr(event, "accept"):
            event.accept()

    def _plot_clicked(self, event) -> None:
        if (
            event.button() == QtCore.Qt.MouseButton.LeftButton
            and not event.isAccepted()
        ):
            self.set_selected_driver(None)
            self.selection_cleared.emit()

    def _mouse_moved(self, scene_position) -> None:
        if not self._hover_enabled or not self._curve_points:
            return
        if not self.plot.sceneBoundingRect().contains(scene_position):
            self._set_hover_driver(None)
            self._clear_hover_label()
            return
        point = self.plot.getPlotItem().vb.mapSceneToView(scene_position)
        nearest = self._nearest_point(float(point.x()), float(point.y()))
        if nearest is None:
            self._set_hover_driver(None)
            self._clear_hover_label()
            return
        driver, row = nearest
        self._set_hover_driver(driver)
        self._draw_hover_label(row)

    def _nearest_point(self, x_position: float, y_position: float):
        view_range = self.plot.getPlotItem().vb.viewRange()
        x_span = max(view_range[0][1] - view_range[0][0], 1.0)
        y_span = max(abs(view_range[1][1] - view_range[1][0]), 1.0)
        points = pd.concat(self._curve_points.values(), ignore_index=False)
        row = nearest_relative_delta_row(
            points,
            x_position,
            y_position,
            x_span=x_span,
            y_span=y_span,
            max_distance=0.0015,
        )
        return None if row is None else (str(row["driver"]), row)

    def _set_hover_driver(self, driver: str | None) -> None:
        if driver == self._hover_driver:
            return
        self._hover_driver = driver
        for curve_driver, curve in self.curves.items():
            curve.setPen(self._curve_pen(curve_driver))

    def _draw_hover_label(self, row: pd.Series) -> None:
        self._hover_overlay.show(
            format_relative_delta_hover_text(row),
            float(row["_plot_x"]),
            float(row["_plot_y"]),
        )
        self._hover_label = self._hover_overlay.item

    def _clear_hover_label(self) -> None:
        self._hover_overlay.clear()
        self._hover_label = None


def _live_plot_x(row: pd.Series) -> float:
    lap = float(row["lap"])
    sector_value = row.get("sector")
    sector = "" if pd.isna(sector_value) else str(sector_value)
    sector_number = {"S1": 1, "S2": 2, "S3": 3}.get(sector)
    return lap if sector_number is None else lap + (sector_number - 2) / 3.0
