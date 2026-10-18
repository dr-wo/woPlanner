"""Read-only Race Control notices; the source message remains authoritative."""
from __future__ import annotations

from PySide6 import QtWidgets


class RaceControlEventsWidget(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._events = ()
        layout = QtWidgets.QVBoxLayout(self)
        controls = QtWidgets.QHBoxLayout()
        self.filter = QtWidgets.QComboBox()
        self.filter.addItems(("All events", "Penalty", "Lap time deleted"))
        self.status = QtWidgets.QLabel("No Race Control events received")
        controls.addWidget(self.filter)
        controls.addWidget(self.status)
        controls.addStretch()
        layout.addLayout(controls)
        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(("Time (UTC)", "Race lap", "Event", "Car", "Details", "Original message"))
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        for column in range(4):
            header.setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(4, 170)
        header.setSectionResizeMode(5, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setWordWrap(True)
        layout.addWidget(self.table)
        note = QtWidgets.QLabel(
            "Race lap is the source notification lap; the affected lap and penalty details "
            "are in the original message. Unknown event times are shown last. "
            "These notices do not alter timing or strategy calculations."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.filter.currentTextChanged.connect(self._render)

    def set_events(self, events):
        events = tuple(events)
        if events == self._events:
            return
        self._events = events
        self._render()

    def clear(self):
        self._events = ()
        self._render()

    def _render(self, *_args):
        kind = self.filter.currentText()
        events = [event for event in self._events if kind == "All events" or event.kind == kind]
        scrollbar = self.table.verticalScrollBar()
        follow_latest = scrollbar.value() == scrollbar.maximum()
        previous_scroll = scrollbar.value()
        self.table.setRowCount(len(events))
        for row, event in enumerate(events):
            fields = event.fields
            details = "; ".join(
                f"{key}: {value}" for key, value in fields.items()
                if key not in {"Utc", "Message", "Category", "Lap", "RacingNumber"}
                and value is not None and str(value).strip()
            )
            values = (
                event.utc.strftime("%H:%M:%S") if event.utc else "—",
                str(fields.get("Lap") or "—"), event.kind,
                event.driver_number or "—", details or "—", event.message or "—",
            )
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, column, item)
        self.table.resizeRowsToContents()
        if follow_latest:
            self.table.scrollToBottom()
        else:
            scrollbar.setValue(previous_scroll)
        self.status.setText(
            f"{len(events)} shown / {len(self._events)} received"
            if self._events else "No Race Control events received (this feed or recording may not include them)"
        )
