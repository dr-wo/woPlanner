from __future__ import annotations

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6 import QtCore, QtGui

from .plot_data import build_pit_window_data


class PitWindowOverlay:
    """Own and render separate current-event GREEN, SC and VSC bands."""

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
        normal_pit_loss_s: float | None,
        sc_pit_loss_s: float | None = None,
        vsc_pit_loss_s: float | None = None,
        sc_vsc_pit_loss_s: float | None = None,
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
            sc_pit_loss_s=sc_pit_loss_s,
            vsc_pit_loss_s=vsc_pit_loss_s,
            sc_vsc_pit_loss_s=sc_vsc_pit_loss_s,
        )
        if x_column not in data.columns:
            x_column = next(
                (column for column in ("x_lap", "segment_index", "lap") if column in data.columns),
                None,
            )
        if x_column is None or data.empty:
            return
        colours = {
            "normal": QtGui.QColor(95, 95, 95),
            "sc": QtGui.QColor(244, 180, 0),
            "vsc": QtGui.QColor(155, 89, 182),
            "legacy_sc_vsc": QtGui.QColor(70, 130, 180),
        }
        labels = {
            "normal": "GREEN pit window",
            "sc": "SC pit window",
            "vsc": "VSC pit window",
            "legacy_sc_vsc": "SC/VSC combined (legacy)",
        }
        for index, state in enumerate(("normal", "sc", "vsc", "legacy_sc_vsc")):
            lower = f"{state}_lower_boundary_s"
            upper = f"{state}_upper_boundary_s"
            if lower not in data.columns or upper not in data.columns:
                continue
            numeric = data.loc[:, [x_column, lower, upper]].apply(pd.to_numeric, errors="coerce")
            valid = np.isfinite(numeric).all(axis=1)
            if not valid.any():
                continue
            x = numeric.loc[valid, x_column].to_numpy(dtype="float64")
            pen = pg.mkPen(colours[state].darker(180), width=1.2,
                           style=QtCore.Qt.PenStyle.DashDotLine)
            lower_curve = self.plot.plot(
                x, numeric.loc[valid, lower].to_numpy(dtype="float64"), pen=pen
            )
            upper_curve = self.plot.plot(
                x, numeric.loc[valid, upper].to_numpy(dtype="float64"),
                pen=pen, name=labels[state],
            )
            lower_curve.setZValue(5)
            upper_curve.setZValue(5)
            brush_colour = QtGui.QColor(colours[state])
            brush_colour.setAlpha(40)
            fill = pg.FillBetweenItem(
                lower_curve, upper_curve, brush=QtGui.QBrush(brush_colour)
            )
            fill.setZValue(-20 + index)
            self.plot.addItem(fill)
            upper_values = numeric.loc[valid, upper].to_numpy(dtype="float64")
            label = pg.TextItem(
                text=labels[state], color=colours[state].darker(220), anchor=(1, 1)
            )
            label.setPos(float(x[-1]), float(upper_values[-1]))
            label.setZValue(6)
            self.plot.addItem(label)
            self.items.extend([lower_curve, upper_curve, fill, label])
