"""Live presentation of unmodelled lap observations and separate MC terms."""

from __future__ import annotations

import json

from PySide6 import QtCore, QtWidgets


class ObservedUndercutWidget(QtWidgets.QGroupBox):
    def __init__(self, parent=None):
        super().__init__("Observed pace delta / Observed undercut estimate", parent)
        layout = QtWidgets.QVBoxLayout(self)
        explanation = QtWidgets.QLabel(
            "Clean full laps: positive observed delta means the post-stop lap was faster. "
            "MC track change is separate: positive means the later reference is modelled slower."
        )
        explanation.setWordWrap(True)
        self.status = QtWidgets.QLabel("Waiting for racing pit-in/out evidence and clean full laps.")
        self.status.setWordWrap(True)
        self.table = QtWidgets.QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels([
            "Driver", "Pit in / out", "Old → replacement", "Pre lap", "Post lap",
            "Separation", "Observed delta (s)", "MC track change (s)", "Status",
        ])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMinimumHeight(135)
        self.table.setMaximumHeight(260)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(explanation)
        layout.addWidget(self.status)
        layout.addWidget(self.table)
        self.records: tuple[dict, ...] = ()

    def render(self, estimates) -> None:
        self.records = tuple(estimate.to_record() for estimate in estimates)
        self.table.setRowCount(len(estimates))
        for row_index, estimate in enumerate(estimates):
            track = estimate.track
            observed = estimate.observed_pace_delta_s
            values = [
                estimate.driver,
                f"{estimate.pit_in_lap} / {estimate.pit_out_lap}",
                f"{estimate.old_compound or '—'} → {estimate.replacement_compound or '—'}",
                estimate.pre_reference_lap, estimate.post_reference_lap,
                estimate.reference_separation,
                f"{observed:+.3f}" if observed is not None else "—",
                f"{track.track_term_change_s:+.3f}" if track.status == "available" else "Unavailable",
                "Available" if estimate.status == "available" else estimate.pending_reason,
            ]
            provenance = json.dumps(self.records[row_index], indent=2, default=str)
            for column_index, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem("—" if value is None else str(value))
                item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                item.setToolTip(provenance)
                self.table.setItem(row_index, column_index, item)
        self.table.resizeColumnsToContents()
        available = sum(estimate.status == "available" for estimate in estimates)
        self.status.setText(
            f"{available} available / {len(estimates)} racing stops. Hover a row for references and model provenance."
            if estimates else "Waiting for racing pit-in/out evidence and clean full laps."
        )

    def unavailable(self, reason: str) -> None:
        self.records = ()
        self.table.setRowCount(0)
        self.status.setText(f"Observation unavailable: {reason}")
