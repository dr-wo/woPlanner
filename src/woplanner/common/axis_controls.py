from __future__ import annotations

import pandas as pd
from PySide6 import QtCore, QtWidgets


class AxisRangeControls(QtWidgets.QWidget):
    """Shared axis-range UI and state for standard and live planners."""

    range_applied = QtCore.Signal()
    auto_requested = QtCore.Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.custom_ranges = False
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.x_min_input = self._spinbox()
        self.x_max_input = self._spinbox()
        self.y_min_input = self._spinbox()
        self.y_max_input = self._spinbox()
        self.apply_button = QtWidgets.QPushButton("Apply range")
        self.auto_button = QtWidgets.QPushButton("Auto range")
        for label, widget in (
            ("Lap min", self.x_min_input), ("Lap max", self.x_max_input),
            ("Y min", self.y_min_input), ("Y max", self.y_max_input),
        ):
            layout.addWidget(QtWidgets.QLabel(label))
            layout.addWidget(widget)
        layout.addWidget(self.apply_button)
        layout.addWidget(self.auto_button)
        layout.addStretch(1)
        self.apply_button.clicked.connect(self._apply)
        self.auto_button.clicked.connect(self._auto)

    @staticmethod
    def _spinbox() -> QtWidgets.QDoubleSpinBox:
        spinbox = QtWidgets.QDoubleSpinBox()
        spinbox.setRange(-10000.0, 10000.0)
        # Direct pyqtgraph ranges can land on fractional-sector coordinates;
        # retain enough precision for a redraw to reproduce the visible range.
        spinbox.setDecimals(6)
        spinbox.setSingleStep(1.0)
        spinbox.setMinimumWidth(86)
        return spinbox

    def update_from_data(self, plot_data: pd.DataFrame) -> None:
        if plot_data is None or plot_data.empty:
            return
        x = pd.to_numeric(plot_data["lap"], errors="coerce").dropna()
        y = pd.to_numeric(plot_data["delta_s"], errors="coerce").dropna()
        if x.empty or y.empty:
            return
        x_padding = max((float(x.max()) - float(x.min())) * 0.02, 1.0)
        y_padding = max((float(y.max()) - float(y.min())) * 0.08, 5.0)
        self.x_min_input.setValue(float(x.min()) - x_padding)
        self.x_max_input.setValue(float(x.max()) + x_padding)
        self.y_min_input.setValue(float(y.min()) - y_padding)
        self.y_max_input.setValue(float(y.max()) + y_padding)

    def lap_range(self) -> tuple[float, float]:
        return self._ordered(self.x_min_input.value(), self.x_max_input.value())

    def y_range(self) -> tuple[float, float]:
        return self._ordered(self.y_min_input.value(), self.y_max_input.value())

    @staticmethod
    def _ordered(first: float, second: float) -> tuple[float, float]:
        if first == second:
            second = first + 1.0
        return min(float(first), float(second)), max(float(first), float(second))

    def _apply(self) -> None:
        self.custom_ranges = True
        self.range_applied.emit()

    def _auto(self) -> None:
        self.custom_ranges = False
        self.auto_requested.emit()
