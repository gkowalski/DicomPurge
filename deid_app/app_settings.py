"""User-editable preferences, persisted in QSettings.

Everything here is tuning: the app runs correctly on the defaults, and each
field is applied live by `MainWindow._apply_settings` when the Settings
dialog is accepted.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import QSettings

log = logging.getLogger(__name__)

# Level names offered for the Log tab, coarsest last.
LOG_LEVELS: list[tuple[str, int]] = [
    ("Debug (everything)", logging.DEBUG),
    ("Info", logging.INFO),
    ("Warnings only", logging.WARNING),
    ("Errors only", logging.ERROR),
]


@dataclass
class AppSettings:
    prefetch_ahead: int = 6
    prefetch_behind: int = 2
    frame_cache_entries: int = 64
    frame_cache_mb: int = 256
    dataset_cache_entries: int = 12
    # Matches LogPane's own starting level, so the default is a no-op.
    gui_log_level: int = logging.INFO

    @classmethod
    def load(cls, settings: QSettings) -> "AppSettings":
        defaults = cls()

        def as_int(key: str, fallback: int, low: int, high: int) -> int:
            try:
                value = int(settings.value(key, fallback))
            except (TypeError, ValueError):
                log.warning("Ignoring unreadable setting %s=%r", key, settings.value(key))
                return fallback
            return max(low, min(high, value))

        return cls(
            prefetch_ahead=as_int("prefetch/ahead", defaults.prefetch_ahead, 0, 32),
            prefetch_behind=as_int("prefetch/behind", defaults.prefetch_behind, 0, 32),
            frame_cache_entries=as_int(
                "cache/frame_entries", defaults.frame_cache_entries, 4, 1024
            ),
            frame_cache_mb=as_int("cache/frame_mb", defaults.frame_cache_mb, 32, 8192),
            dataset_cache_entries=as_int(
                "cache/dataset_entries", defaults.dataset_cache_entries, 1, 256
            ),
            gui_log_level=as_int("log/gui_level", defaults.gui_log_level, 0, 50),
        )

    def save(self, settings: QSettings) -> None:
        settings.setValue("prefetch/ahead", self.prefetch_ahead)
        settings.setValue("prefetch/behind", self.prefetch_behind)
        settings.setValue("cache/frame_entries", self.frame_cache_entries)
        settings.setValue("cache/frame_mb", self.frame_cache_mb)
        settings.setValue("cache/dataset_entries", self.dataset_cache_entries)
        settings.setValue("log/gui_level", self.gui_log_level)
