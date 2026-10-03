from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from ..common.track_position import ReferenceLapMapper
from ..common.windows import WindowDescriptor
from .telemetry import TrackMapSnapshot


@dataclass(frozen=True)
class RenderedWindowMarker:
    key: str
    direction: str
    seconds: float
    x: float
    y: float
    label: str


@dataclass(frozen=True)
class RenderedWindowConnector:
    """A low-priority line from the selected car to a displayed window mark."""

    key: str
    direction: str
    x1: float
    y1: float
    x2: float
    y2: float
    colour: str
    line_style: str


class TrackMapWidget(QtWidgets.QWidget):
    """Render real Position.z markers and approximate operational windows."""

    driver_selected = QtCore.Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._snapshot: TrackMapSnapshot | None = None
        self._drivers = pd.DataFrame()
        self._selected_driver: str | None = None
        self._windows: tuple[WindowDescriptor, ...] = ()
        self._mapper: ReferenceLapMapper | None = None
        self.rendered_window_markers: tuple[RenderedWindowMarker, ...] = ()
        self.rendered_window_connectors: tuple[RenderedWindowConnector, ...] = ()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.status = QtWidgets.QLabel(
            "Track Map: waiting for a complete clean-lap Position.z reference"
        )
        self.status.setWordWrap(True)
        self.plot = pg.PlotWidget()
        self.plot.setBackground("w")
        self.plot.setAspectLocked(True)
        # Curves include marker endpoints; this extra breathing room keeps the
        # unshifted marker labels from being clipped at the map edge.
        self.plot.getViewBox().setDefaultPadding(0.12)
        self.plot.hideAxis("left")
        self.plot.hideAxis("bottom")
        layout.addWidget(self.status)
        layout.addWidget(self.plot, stretch=1)

    @property
    def selected_driver(self) -> str | None:
        return self._selected_driver

    def set_selected_driver(self, driver: str | None) -> None:
        normalized = None if driver is None else str(driver)
        if normalized == self._selected_driver:
            return
        self._selected_driver = normalized
        if self.isVisible():
            self._render()

    def set_windows(self, windows: tuple[WindowDescriptor, ...]) -> None:
        normalized = tuple(windows)
        if normalized == self._windows:
            return
        self._windows = normalized
        if self.isVisible():
            self._render()

    def update_snapshot(
        self, snapshot: TrackMapSnapshot, drivers: pd.DataFrame
    ) -> None:
        self._snapshot = snapshot
        self._drivers = drivers.copy()
        try:
            self._mapper = (
                None if snapshot.reference is None
                else ReferenceLapMapper(snapshot.reference.points)
            )
        except ValueError:
            self._mapper = None
        if self.isVisible():
            self._render()

    def clear(self) -> None:
        self._snapshot = None
        self._mapper = None
        self.rendered_window_markers = ()
        self.rendered_window_connectors = ()
        self.plot.clear()
        self.status.setText(
            "Track Map: waiting for a complete clean-lap Position.z reference"
        )

    def _render(self) -> None:
        self.plot.clear()
        self.rendered_window_markers = ()
        self.rendered_window_connectors = ()
        snapshot = self._snapshot
        if snapshot is None:
            return
        if snapshot.reference is not None:
            reference = snapshot.reference
            self.plot.plot(
                [point.x for point in reference.points],
                [point.y for point in reference.points],
                pen=pg.mkPen("#aeb4ba", width=4.0),
            ).setZValue(-20)
            self.status.setText(
                "Track Map — approximate mapping from actual Position.z reference: "
                f"{reference.driver_code or reference.driver_id} lap {reference.lap_number} "
                f"({reference.lap_time_s:.1f} s)"
            )
        else:
            self.status.setText(
                "Track Map: real positions available; waiting for a complete clean-lap "
                "Position.z reference before drawing time windows"
            )

        styles = (
            self._drivers.set_index("driver").to_dict("index")
            if not self._drivers.empty and "driver" in self._drivers.columns
            else {}
        )
        spots = []
        labels = []
        by_code = {}
        for driver in snapshot.drivers:
            code = driver.driver_code or driver.driver_id
            by_code[code] = driver
            style = styles.get(code, {})
            colour = str(style.get("colour", "#555555"))
            selected = code == self._selected_driver
            spots.append({
                "pos": (driver.x, driver.y),
                "data": code,
                "symbol": "o",
                "size": 19 if selected else 11,
                "brush": pg.mkBrush(colour if driver.valid else "#d0d0d0"),
                "pen": pg.mkPen("#111111" if selected else colour, width=4 if selected else 1),
            })
            labels.append((driver.x, driver.y, code, colour, selected, driver.valid))
        if spots:
            layer = pg.ScatterPlotItem(spots=spots, pxMode=True)
            layer.setZValue(20)
            layer.sigClicked.connect(self._marker_clicked)
            self.plot.addItem(layer)
        for x, y, code, colour, selected, valid in labels:
            label = pg.TextItem(
                text=code,
                color=("#111111" if selected else colour if valid else "#888888"),
                anchor=(0.5, 1.35),
            )
            font = QtGui.QFont()
            font.setBold(selected)
            font.setPointSize(11 if selected else 9)
            label.setFont(font)
            label.setPos(float(x), float(y))
            label.setZValue(21)
            self.plot.addItem(label)

        selected = by_code.get(self._selected_driver or "")
        if selected is not None:
            self._render_windows(selected)

    def _render_windows(self, driver) -> None:
        if not driver.valid:
            self.status.setText(self.status.text() + " — selected position is stale")
            return
        if driver.in_pit is True or driver.on_track is False:
            self.status.setText(self.status.text() + " — selected car is pit-side/off-track")
            return
        if self._mapper is None:
            return
        markers = []
        connectors = []
        pen_styles = {
            "dash": QtCore.Qt.PenStyle.DashLine,
            "dot": QtCore.Qt.PenStyle.DotLine,
            "dashdot": QtCore.Qt.PenStyle.DashDotLine,
        }
        for window in self._windows:
            colour = QtGui.QColor(window.colour)
            interval = self._mapper.interval_from_xy(
                driver.x, driver.y, window.value_s
            )
            curve = self.plot.plot(
                [point.x for point in interval],
                [point.y for point in interval],
                pen=pg.mkPen(
                    colour, width=2.0,
                    style=pen_styles.get(
                        window.line_style, QtCore.Qt.PenStyle.SolidLine
                    ),
                ),
            )
            curve.setZValue(5)
            for direction, seconds in (("behind", -window.value_s), ("ahead", window.value_s)):
                point = self._mapper.offset_from_xy(driver.x, driver.y, seconds)
                # This deliberately joins the selected car, rather than the pace
                # reference at the top of the planner, to its own mapped markers.
                connector = self.plot.plot(
                    [driver.x, point.x], [driver.y, point.y],
                    pen=pg.mkPen(
                        colour,
                        width=1.0,
                        style=QtCore.Qt.PenStyle.DashLine,
                    ),
                )
                connector.setZValue(2)
                connectors.append(RenderedWindowConnector(
                    window.key, direction, float(driver.x), float(driver.y),
                    float(point.x), float(point.y), window.colour, "dash",
                ))
                title = window.label.split(" ±", 1)[0]
                sign = "−" if seconds < 0.0 else "+"
                marker_label = f"{title} {sign}{abs(float(seconds)):.1f} s"
                marker = pg.ScatterPlotItem(
                    [point.x], [point.y], symbol="d", size=15,
                    brush=pg.mkBrush(255, 255, 255, 180),
                    pen=pg.mkPen(colour, width=3),
                )
                marker.setZValue(15)
                self.plot.addItem(marker)
                # Keep the coordinate exact and offset only the text box away
                # from the selected-car anchor.  A translucent background keeps
                # the label legible when the field is bunched at the marker.
                label_anchor = (
                    (-0.12, 0.5) if point.x >= driver.x else (1.12, 0.5)
                )
                text = pg.TextItem(
                    text=marker_label,
                    color=colour,
                    anchor=label_anchor,
                    border=pg.mkPen(colour, width=0.5),
                    fill=pg.mkBrush(255, 255, 255, 220),
                )
                text.setPos(point.x, point.y)
                text.setZValue(16)
                self.plot.addItem(text)
                markers.append(RenderedWindowMarker(
                    window.key, direction, abs(float(seconds)), point.x, point.y,
                    marker_label,
                ))
        self.rendered_window_markers = tuple(markers)
        self.rendered_window_connectors = tuple(connectors)

    def _marker_clicked(self, _layer, points, _event) -> None:
        if len(points) == 0:
            return
        driver = points[0].data()
        if driver is not None:
            self.driver_selected.emit(str(driver))


__all__ = ["RenderedWindowConnector", "RenderedWindowMarker", "TrackMapWidget"]
