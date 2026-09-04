"""User-editable preferences, persisted in QSettings.

Everything here is tuning: the app runs correctly on the defaults, and each
field is applied live by `MainWindow._apply_settings` when the Settings
dialog is accepted.
"""
from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QSettings

log = logging.getLogger(__name__)

# Bounds for the concurrent-upload setting. Each running upload holds its own
# XNAT session, so the ceiling is deliberately low.
MIN_CONCURRENT_UPLOADS = 1
MAX_CONCURRENT_UPLOADS = 8

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
    # Some report objects carry a PDF copy of the report alongside the image.
    # Redaction boxes never touch it, so by default it is removed on export.
    strip_embedded_documents: bool = True
    # Structured Reports hold text rather than pixels, so boxes cannot alter
    # them either. Off by default: unlike an embedded PDF, which duplicates the
    # image beside it, an SR is often the only copy of the report.
    skip_structured_reports: bool = False
    # Instances with no pixel data at all - presentation states, waveforms, RT
    # objects and the like, shown as "Uneditable File" in the image pane. Off by
    # default: they hold no burned-in annotation to redact, but they may still
    # be data the user wants carried through.
    skip_files_without_image_data: bool = False
    # XNAT upload. Zip mode sends one DICOM-zip per study straight to the
    # archive; individual mode posts each file (gradual-DICOM) to the
    # prearchive. The temp directory holds the de-identified copies (and the
    # zip) while a study uploads; empty means the system temp directory.
    xnat_upload_zip: bool = True
    xnat_upload_temp_dir: str = ""
    xnat_upload_max_concurrent: int = 2

    def export_options(self):
        """The three export checkboxes as the value export_instance() takes."""
        from .export import ExportOptions

        return ExportOptions(
            strip_documents=self.strip_embedded_documents,
            skip_structured_reports=self.skip_structured_reports,
            skip_files_without_image_data=self.skip_files_without_image_data,
        )

    def upload_temp_root(self) -> Path:
        text = (self.xnat_upload_temp_dir or "").strip()
        return Path(text) if text else Path(tempfile.gettempdir())

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

        def as_bool(key: str, fallback: bool) -> bool:
            value = settings.value(key, fallback)
            if isinstance(value, bool):
                return value
            # QSettings hands back "true"/"false" strings on some platforms.
            return str(value).strip().lower() not in ("false", "0", "")

        def as_text(key: str, fallback: str) -> str:
            value = settings.value(key, fallback)
            return "" if value is None else str(value)

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
            strip_embedded_documents=as_bool(
                "export/strip_embedded_documents", defaults.strip_embedded_documents
            ),
            skip_structured_reports=as_bool(
                "export/skip_structured_reports", defaults.skip_structured_reports
            ),
            skip_files_without_image_data=as_bool(
                "export/skip_files_without_image_data",
                defaults.skip_files_without_image_data,
            ),
            xnat_upload_zip=as_bool("xnat_upload/zip_mode", defaults.xnat_upload_zip),
            xnat_upload_temp_dir=as_text(
                "xnat_upload/temp_dir", defaults.xnat_upload_temp_dir
            ),
            xnat_upload_max_concurrent=as_int(
                "xnat_upload/max_concurrent", defaults.xnat_upload_max_concurrent,
                MIN_CONCURRENT_UPLOADS, MAX_CONCURRENT_UPLOADS,
            ),
        )

    def save(self, settings: QSettings) -> None:
        settings.setValue("prefetch/ahead", self.prefetch_ahead)
        settings.setValue("prefetch/behind", self.prefetch_behind)
        settings.setValue("cache/frame_entries", self.frame_cache_entries)
        settings.setValue("cache/frame_mb", self.frame_cache_mb)
        settings.setValue("cache/dataset_entries", self.dataset_cache_entries)
        settings.setValue("log/gui_level", self.gui_log_level)
        settings.setValue(
            "export/strip_embedded_documents", self.strip_embedded_documents
        )
        settings.setValue(
            "export/skip_structured_reports", self.skip_structured_reports
        )
        settings.setValue(
            "export/skip_files_without_image_data",
            self.skip_files_without_image_data,
        )
        settings.setValue("xnat_upload/zip_mode", self.xnat_upload_zip)
        settings.setValue("xnat_upload/temp_dir", self.xnat_upload_temp_dir)
        settings.setValue(
            "xnat_upload/max_concurrent", self.xnat_upload_max_concurrent
        )
