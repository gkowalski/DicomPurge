"""Log tab: level-filtered view of every log record, with a clear button."""
from __future__ import annotations

import logging
from collections import deque

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .logging_setup import LOG_PATH, LogBridge, truncate_log_file

LEVELS = [
    ("DEBUG", logging.DEBUG),
    ("INFO", logging.INFO),
    ("WARNING", logging.WARNING),
    ("ERROR", logging.ERROR),
    ("CRITICAL", logging.CRITICAL),
]

LEVEL_COLORS = {
    logging.DEBUG: QColor("#7f8c8d"),
    logging.INFO: QColor("#2c3e50"),
    logging.WARNING: QColor("#c87f0a"),
    logging.ERROR: QColor("#c0392b"),
    logging.CRITICAL: QColor("#8e2020"),
}

MAX_RECORDS = 20000


class LogPane(QWidget):
    def __init__(self, bridge: LogBridge, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._records: deque[tuple[int, str]] = deque(maxlen=MAX_RECORDS)
        self._min_level = logging.INFO

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Minimum level:"))
        self.level_box = QComboBox()
        for name, value in LEVELS:
            self.level_box.addItem(name, value)
        self.level_box.setCurrentText("INFO")
        self.level_box.currentIndexChanged.connect(self._on_level_changed)
        controls.addWidget(self.level_box)

        self.autoscroll = QCheckBox("Auto-scroll")
        self.autoscroll.setChecked(True)
        controls.addWidget(self.autoscroll)
        controls.addStretch(1)

        self.path_label = QLabel(f"Log file: {LOG_PATH}")
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        controls.addWidget(self.path_label)

        self.clear_button = QPushButton("Clear log")
        self.clear_button.setToolTip("Clears this pane and empties the log file on disk")
        self.clear_button.clicked.connect(self.clear_log)
        controls.addWidget(self.clear_button)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(MAX_RECORDS + 100)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        font = self.view.font()
        font.setFamily("Menlo")
        font.setStyleHint(font.StyleHint.Monospace)
        self.view.setFont(font)

        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(self.view, 1)

        bridge.record.connect(self.append_record)

    # -- slots -----------------------------------------------------------
    def append_record(self, levelno: int, _levelname: str, text: str) -> None:
        self._records.append((levelno, text))
        if levelno >= self._min_level:
            self._append_line(levelno, text)

    def _append_line(self, levelno: int, text: str) -> None:
        fmt = QTextCharFormat()
        fmt.setForeground(LEVEL_COLORS.get(levelno, QColor("#2c3e50")))
        cursor = self.view.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text + "\n", fmt)
        if self.autoscroll.isChecked():
            self.view.verticalScrollBar().setValue(
                self.view.verticalScrollBar().maximum()
            )

    def _on_level_changed(self) -> None:
        self._min_level = int(self.level_box.currentData())
        self._rerender()

    def _rerender(self) -> None:
        self.view.clear()
        for levelno, text in self._records:
            if levelno >= self._min_level:
                self._append_line(levelno, text)

    def clear_log(self) -> None:
        self._records.clear()
        self.view.clear()
        truncate_log_file()
        logging.getLogger(__name__).info("Log cleared by user (%s truncated)", LOG_PATH)
