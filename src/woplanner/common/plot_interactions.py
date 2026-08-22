from __future__ import annotations

import pyqtgraph as pg
from PySide6 import QtGui


def bind_driver_curve_click(curve, driver: str, callback) -> None:
    """Bind a pyqtgraph curve to the shared driver-click callback contract."""
    curve.sigClicked.connect(
        lambda _curve, event, drv=str(driver): callback(drv, event)
    )


class HoverLabelOverlay:
    """Shared hover-label rendering and lifecycle."""

    def __init__(self, plot, *, point_size: int = 12) -> None:
        self.plot = plot
        self.point_size = point_size
        self.item = None

    def show(self, text: str, x_position: float, y_position: float) -> None:
        self.clear()
        label = pg.TextItem(
            text=text,
            color="#111111",
            anchor=(0.0, 1.0),
            fill=QtGui.QBrush(QtGui.QColor(255, 255, 255, 225)),
            border=pg.mkPen("#111111", width=1.0),
        )
        font = QtGui.QFont()
        font.setPointSize(self.point_size)
        font.setBold(True)
        label.setFont(font)
        label.setPos(float(x_position), float(y_position))
        label.setZValue(50)
        self.plot.addItem(label)
        self.item = label

    def clear(self) -> None:
        if self.item is None:
            return
        try:
            self.plot.removeItem(self.item)
        except Exception:
            pass
        self.item = None
