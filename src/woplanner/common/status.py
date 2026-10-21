"""Keep the shared padded status labels readable without consuming spare space."""
from PySide6 import QtCore


def fit_status_label(label) -> None:
    width = max(label.width(), 300) - 20
    bounds = label.fontMetrics().boundingRect(
        QtCore.QRect(0, 0, width, 10000),
        int(QtCore.Qt.TextFlag.TextWordWrap), label.text(),
    )
    label.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop)
    label.setFixedHeight(max(38, bounds.height() + 16))
