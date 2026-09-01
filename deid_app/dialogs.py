"""About and Settings dialogs, reached from the menu bar."""
from __future__ import annotations

import logging
import platform

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .app_settings import LOG_LEVELS, AppSettings
from .logging_setup import LOG_PATH
from .resources import logo_pixmap
from .xnat_settings import PASSWORD_WARNING, XnatSettings

log = logging.getLogger(__name__)


def _library_versions() -> list[tuple[str, str]]:
    """Versions of the libraries worth knowing about in a bug report."""
    rows = [("Python", platform.python_version())]
    for label, module in (("PySide6", "PySide6"), ("pydicom", "pydicom"), ("NumPy", "numpy")):
        try:
            mod = __import__(module)
        except Exception:  # noqa: BLE001 - a missing optional dep is not fatal here
            rows.append((label, "not available"))
            continue
        rows.append((label, getattr(mod, "__version__", "unknown")))
    rows.append(("Platform", f"{platform.system()} {platform.release()}"))
    return rows


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About DicomPurge")
        self.setModal(True)

        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        pixmap = logo_pixmap(72)
        if pixmap is not None:
            logo = QLabel()
            logo.setPixmap(pixmap)
            header.addWidget(logo, 0, Qt.AlignTop)

        title = QLabel(
            f"<h2 style='margin:0'>DicomPurge</h2>"
            f"<p style='margin:4px 0'>Version {__version__}</p>"
            "<p style='margin:0'>De-identification of burned-in annotations<br>"
            "in DICOM images.</p>"
        )
        title.setTextFormat(Qt.RichText)
        header.addWidget(title, 1)
        layout.addLayout(header)

        details = QGroupBox("Environment")
        form = QFormLayout(details)
        for label, value in _library_versions():
            form.addRow(f"{label}:", QLabel(value))
        log_label = QLabel(f"<a href='file://{LOG_PATH}'>{LOG_PATH}</a>")
        log_label.setTextFormat(Qt.RichText)
        log_label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        log_label.linkActivated.connect(self._open_log)
        form.addRow("Log file:", log_label)
        layout.addWidget(details)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        # Everything here is fixed-size content, so let the layout pin the
        # dialog to its size hint - that is also what drops the resize grip.
        # Done via the layout rather than setFixedSize() because the height
        # depends on how many rows _library_versions() produced.
        layout.setSizeConstraint(QLayout.SetFixedSize)

    def _open_log(self, url: str) -> None:
        from PySide6.QtCore import QUrl

        if not QDesktopServices.openUrl(QUrl(url)):
            log.warning("Could not open the log file at %s", LOG_PATH)


class SettingsDialog(QDialog):
    """Edits an AppSettings copy; read `values` after exec() returns Accepted."""

    def __init__(self, current: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        self._current = current

        layout = QVBoxLayout(self)

        # Prefetch ---------------------------------------------------------
        prefetch = QGroupBox("Frame prefetch")
        prefetch.setToolTip(
            "How many neighbouring frames are decoded in the background so that "
            "scrolling stays smooth. Higher values use more memory and CPU."
        )
        prefetch_form = QFormLayout(prefetch)
        self.ahead_spin = self._spin(0, 32, current.prefetch_ahead, " frames")
        self.behind_spin = self._spin(0, 32, current.prefetch_behind, " frames")
        prefetch_form.addRow("Read ahead:", self.ahead_spin)
        prefetch_form.addRow("Read behind:", self.behind_spin)
        layout.addWidget(prefetch)

        # Caches -----------------------------------------------------------
        caches = QGroupBox("Caches")
        caches.setToolTip(
            "Decoded frames and datasets kept in memory so a revisited image "
            "reappears instantly. Lower these if the app uses too much RAM."
        )
        cache_form = QFormLayout(caches)
        self.frame_entries_spin = self._spin(4, 1024, current.frame_cache_entries, " frames")
        self.frame_mb_spin = self._spin(32, 8192, current.frame_cache_mb, " MB")
        self.dataset_spin = self._spin(1, 256, current.dataset_cache_entries, " files")
        cache_form.addRow("Rendered frames:", self.frame_entries_spin)
        cache_form.addRow("Frame cache limit:", self.frame_mb_spin)
        cache_form.addRow("Decoded DICOM files:", self.dataset_spin)
        layout.addWidget(caches)

        # Logging ----------------------------------------------------------
        logging_box = QGroupBox("Logging")
        log_form = QFormLayout(logging_box)
        self.level_combo = QComboBox()
        for label, level in LOG_LEVELS:
            self.level_combo.addItem(label, level)
        index = self.level_combo.findData(current.gui_log_level)
        self.level_combo.setCurrentIndex(index if index >= 0 else 0)
        self.level_combo.setToolTip(
            "The minimum level the Log tab starts at. You can still change it "
            "on the tab itself at any time; the log file on disk always keeps "
            "everything regardless."
        )
        log_form.addRow("Log tab default level:", self.level_combo)
        layout.addWidget(logging_box)

        # Export -----------------------------------------------------------
        export_box = QGroupBox("Export")
        export_form = QVBoxLayout(export_box)
        self.strip_docs_check = QCheckBox(
            "Remove embedded documents (PDFs) from exported files"
        )
        self.strip_docs_check.setChecked(current.strip_embedded_documents)
        self.strip_docs_check.setToolTip(
            "Some report objects carry a PDF copy of the report alongside the "
            "image. Redaction boxes do not apply to that copy, so it is removed "
            "from the exported file.\n\nFiles that contain only a PDF and no "
            "image cannot be de-identified at all and are skipped; the export "
            "summary lists them."
        )
        export_form.addWidget(self.strip_docs_check)

        self.skip_sr_check = QCheckBox(
            "Skip Structured Reports (they cannot be de-identified with boxes)"
        )
        self.skip_sr_check.setChecked(current.skip_structured_reports)
        self.skip_sr_check.setToolTip(
            "A Structured Report holds text rather than pixels, so redaction "
            "boxes cannot alter it.\n\nWith this on, such series are withheld "
            "from the export and shown as skipped in the series tree."
        )
        export_form.addWidget(self.skip_sr_check)

        self.skip_no_image_check = QCheckBox(
            "Skip files with no image data (shown as \"Uneditable File\")"
        )
        self.skip_no_image_check.setChecked(current.skip_files_without_image_data)
        self.skip_no_image_check.setToolTip(
            "Presentation states, waveforms, RT objects and similar instances "
            "carry no pixel data, so there is no image to draw a box on.\n\n"
            "With this on, series made up entirely of such files are withheld "
            "from the export and shown as skipped."
        )
        export_form.addWidget(self.skip_no_image_check)
        layout.addWidget(export_box)

        # Recent directories ------------------------------------------------
        self.clear_recent_check = QCheckBox("Clear the list of recent input directories")
        self.clear_recent_check.setToolTip(
            "Empties the input-directory dropdown when you press OK. The "
            "directories themselves are untouched."
        )
        layout.addWidget(self.clear_recent_check)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.RestoreDefaults
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.RestoreDefaults).clicked.connect(self._restore_defaults)
        layout.addWidget(buttons)

    @staticmethod
    def _spin(low: int, high: int, value: int, suffix: str) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(low, high)
        spin.setValue(value)
        spin.setSuffix(suffix)
        return spin

    def _restore_defaults(self) -> None:
        defaults = AppSettings()
        self.ahead_spin.setValue(defaults.prefetch_ahead)
        self.behind_spin.setValue(defaults.prefetch_behind)
        self.frame_entries_spin.setValue(defaults.frame_cache_entries)
        self.frame_mb_spin.setValue(defaults.frame_cache_mb)
        self.dataset_spin.setValue(defaults.dataset_cache_entries)
        index = self.level_combo.findData(defaults.gui_log_level)
        self.level_combo.setCurrentIndex(index if index >= 0 else 0)
        self.strip_docs_check.setChecked(defaults.strip_embedded_documents)
        self.skip_sr_check.setChecked(defaults.skip_structured_reports)
        self.skip_no_image_check.setChecked(defaults.skip_files_without_image_data)

    @property
    def values(self) -> AppSettings:
        return AppSettings(
            prefetch_ahead=self.ahead_spin.value(),
            prefetch_behind=self.behind_spin.value(),
            frame_cache_entries=self.frame_entries_spin.value(),
            frame_cache_mb=self.frame_mb_spin.value(),
            dataset_cache_entries=self.dataset_spin.value(),
            gui_log_level=self.level_combo.currentData(),
            strip_embedded_documents=self.strip_docs_check.isChecked(),
            skip_structured_reports=self.skip_sr_check.isChecked(),
            skip_files_without_image_data=self.skip_no_image_check.isChecked(),
        )

    @property
    def clear_recent_requested(self) -> bool:
        return self.clear_recent_check.isChecked()


class XnatSettingsDialog(QDialog):
    """Edits XNAT credentials; read `values` after exec() returns Accepted."""

    def __init__(self, current: XnatSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("XNAT Server and Credentials")
        self.setModal(True)

        layout = QVBoxLayout(self)

        server_box = QGroupBox("XNAT server")
        form = QFormLayout(server_box)

        self.server_edit = QLineEdit(current.server)
        self.server_edit.setPlaceholderText("https://xnat.example.org/xnat")
        self.server_edit.setToolTip(
            "Base URL of the XNAT server. A bare hostname is assumed to be https."
        )
        self.server_edit.setMinimumWidth(360)

        self.user_edit = QLineEdit(current.user)
        self.user_edit.setToolTip("Your XNAT username.")

        self.password_edit = QLineEdit(current.password)
        self.password_edit.setEchoMode(QLineEdit.Password)

        form.addRow("Server URL:", self.server_edit)
        form.addRow("User ID:", self.user_edit)
        form.addRow("Password:", self.password_edit)
        layout.addWidget(server_box)

        warning = QLabel(PASSWORD_WARNING)
        warning.setWordWrap(True)
        warning.setStyleSheet("color: palette(mid);")
        layout.addWidget(warning)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        # OK stays disabled until all three fields are filled, so an incomplete
        # configuration can never be saved and then fail confusingly at login.
        for edit in (self.server_edit, self.user_edit, self.password_edit):
            edit.textChanged.connect(self._update_ok)
        self._update_ok()

    def _update_ok(self) -> None:
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(self.values.is_complete)

    @property
    def values(self) -> XnatSettings:
        return XnatSettings(
            server=XnatSettings.normalise_server(self.server_edit.text()),
            user=self.user_edit.text().strip(),
            password=self.password_edit.text(),
        )
