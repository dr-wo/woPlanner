from __future__ import annotations

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets


class RelativeDeltaPlotWidget(QtWidgets.QWidget):
    driver_selected = QtCore.Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._drivers = pd.DataFrame()
        self._line_width = 3.0
        self.curves = {}
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.start_rank_list = QtWidgets.QListWidget()
        self.latest_rank_list = QtWidgets.QListWidget()
        for rank_list in (self.start_rank_list, self.latest_rank_list):
            rank_list.setFixedWidth(190)
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
        layout.addLayout(start_panel)
        layout.addWidget(self.plot, stretch=1)
        layout.addLayout(latest_panel)

    def set_session_drivers(self, drivers: pd.DataFrame) -> None:
        self._drivers = drivers.copy()

    def set_plot_data(self, plot_data: pd.DataFrame) -> None:
        self.plot.clear()
        self.curves = {}
        if plot_data.empty:
            return
        styles = (
            self._drivers.set_index("driver").to_dict("index")
            if not self._drivers.empty else {}
        )
        for driver, data in plot_data.groupby("driver", sort=False):
            style = styles.get(driver, {})
            x = pd.to_numeric(data.get("x_lap", data["lap"]), errors="coerce")
            y = pd.to_numeric(data["delta_s"], errors="coerce")
            valid = x.notna() & y.notna() & np.isfinite(x) & np.isfinite(y)
            pen_style = (
                QtCore.Qt.PenStyle.DashLine
                if style.get("line_style") == "dash"
                else QtCore.Qt.PenStyle.SolidLine
            )
            curve = self.plot.plot(
                x.loc[valid].to_numpy(), y.loc[valid].to_numpy(),
                pen=pg.mkPen(style.get("colour", "#555555"), width=self._line_width, style=pen_style),
                clickable=True,
            )
            curve.sigClicked.connect(lambda _curve, _event, drv=str(driver): self.driver_selected.emit(drv))
            self.curves[str(driver)] = curve
        self.plot.enableAutoRange()

    def set_rank_data(self, start: pd.DataFrame, current: pd.DataFrame) -> None:
        self._fill_rank_list(self.start_rank_list, start)
        self._fill_rank_list(self.latest_rank_list, current)

    @staticmethod
    def _fill_rank_list(widget, ranks: pd.DataFrame) -> None:
        widget.clear()
        if ranks.empty:
            return
        for _, row in ranks.sort_values(["rank", "driver"], kind="mergesort").iterrows():
            item = QtWidgets.QListWidgetItem(f"{int(row['rank']):>2}  {row['driver']}")
            item.setForeground(QtGui.QColor(str(row.get("colour", "#333333"))))
            widget.addItem(item)

    def set_line_width(self, width: float) -> None:
        self._line_width = float(width)

    def clear(self) -> None:
        self.plot.clear()
        self.start_rank_list.clear()
        self.latest_rank_list.clear()
        self.curves = {}
