"""Entry point for DicomPurge, the DICOM de-identification tool."""
from __future__ import annotations

import logging
import sys
import traceback

from PySide6.QtWidgets import QApplication, QMessageBox

from deid_app import APP_NAME
from deid_app.logging_setup import configure_logging
from deid_app.main_window import MainWindow
from deid_app.resources import app_icon


def _install_excepthook() -> None:
    log = logging.getLogger("deid_app.unhandled")

    def hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        log.critical(
            "Unhandled exception:\n%s",
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        )
        try:
            QMessageBox.critical(
                None, "Unexpected error", f"{exc_type.__name__}: {exc_value}"
            )
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = hook


def main() -> int:
    # macOS builds the application menu ("About X", "Hide X", "Quit X") from
    # arguments()[0], not from setApplicationName, so argv[0] has to carry the
    # product name - otherwise the menu reads "About main.py". Real arguments
    # are kept so Qt still honours -style, -platform and friends.
    app = QApplication([APP_NAME] + sys.argv[1:])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("de-id")
    app.setWindowIcon(app_icon())

    bridge = configure_logging()
    _install_excepthook()

    window = MainWindow(bridge)
    # closeEvent covers the window-close, Cmd+Q and quit() paths; aboutToQuit
    # also covers QApplication.exit(), which never closes windows. The handler
    # is idempotent, so both firing is harmless.
    app.aboutToQuit.connect(window.shutdown_xnat)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
