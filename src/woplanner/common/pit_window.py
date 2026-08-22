from __future__ import annotations

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6 import QtCore, QtGui

from .plot_data import build_pit_window_data


class PitWindowOverlay:
    """Own and render the shared grey normal/SC-VSC pit-loss bands."""

    def __init__(self, plot) -> None:
        self.plot = plot
        self.items: list = []

    def clear(self) -> None:
        for item in self.items:
            try:
                self.plot.removeItem(item)
            except Exception:
                pass
        self.items = []

    def update(
        self,
        plot_data: pd.DataFrame | None,
        selected_driver: str | None,
        *,
        normal_pit_loss_s: float,
        sc_vsc_pit_loss_s: float,
        x_column: str | None = None,
    ) -> None:
        self.clear()
        if plot_data is None or plot_data.empty or not selected_driver:
            return
        selected = plot_data.loc[plot_data["driver"].astype(str).eq(str(selected_driver))].copy()
        if "plot_role" in selected.columns:
            selected = selected.loc[selected["plot_role"].astype(str).eq("actual")]
        data = build_pit_window_data(
            selected,
            normal_pit_loss_s=normal_pit_loss_s,
            sc_vsc_pit_loss_s=sc_vsc_pit_loss_s,
        )
        if x_column not in data.columns:
            x_column = next(
                (column for column in ("x_lap", "segment_index", "lap") if column in data.columns),
                None,
            )
        boundaries = [
            "normal_lower_boundary_s", "vsc_lower_boundary_s",
            "vsc_upper_boundary_s", "normal_upper_boundary_s",
        ]
        if x_column is None or data.empty:
            return
        numeric = data.loc[:, [x_column, *boundaries]].apply(pd.to_numeric, errors="coerce")
        valid = np.isfinite(numeric).all(axis=1)
        if not valid.any():
            return
        x = numeric.loc[valid, x_column].to_numpy(dtype="float64")
        values = [numeric.loc[valid, column].to_numpy(dtype="float64") for column in boundaries]
        pen = pg.mkPen("#666666", width=1.2, style=QtCore.Qt.PenStyle.DashDotLine)
        curves = [self.plot.plot(x, y, pen=pen) for y in values]
        for curve in curves:
            curve.setZValue(5)
        fills = [
            pg.FillBetweenItem(curves[0], curves[1], brush=QtGui.QBrush(QtGui.QColor(95, 95, 95, 55))),
            pg.FillBetweenItem(curves[2], curves[3], brush=QtGui.QBrush(QtGui.QColor(95, 95, 95, 55))),
            pg.FillBetweenItem(curves[1], curves[2], brush=QtGui.QBrush(QtGui.QColor(185, 185, 185, 60))),
        ]
        for index, fill in enumerate(fills):
            fill.setZValue(-19 if index == 2 else -20)
            self.plot.addItem(fill)
        self.items = [*curves, *fills]
