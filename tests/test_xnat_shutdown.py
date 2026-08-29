"""Offscreen check: closing the window closes the XNAT session.

Regression guard. A plain `signal.emit()` here races QThread.quit() and loses,
so the session's DELETE /data/JSESSION never goes out and the server is left
holding a session until it times out. Closing must block on the logout.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import deid_app.xnat_worker as xw  # noqa: E402
from deid_app.logging_setup import configure_logging  # noqa: E402
from deid_app.main_window import MainWindow  # noqa: E402
from deid_app.xnat_settings import XnatSettings  # noqa: E402

IN = Path("/tmp/fixtures")
failures = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


def pump(ms=150):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class FakeSession:
    logged_in_user = "testUser"

    def __init__(self):
        self.disconnected = False

    def disconnect(self):
        self.disconnected = True


def main():
    app = QApplication(sys.argv)
    session = FakeSession()
    xw.open_session = lambda cfg: session

    win = MainWindow(configure_logging())
    win.show()
    win._start_scan(IN)
    for _ in range(30):
        pump(100)
        if win.series_map:
            break

    win.xnat_settings = XnatSettings(
        server="https://x.org/xnat", user="testUser", password="pw"
    )
    win._update_xnat_button()
    win.xnat_login()
    for _ in range(20):
        pump(100)
        if win._xnat_user:
            break

    check(win._xnat_user == "testUser", "logged in before closing")
    check(win._xnat_thread.isRunning(), "the XNAT thread is running")

    win.close()
    pump(300)

    check(session.disconnected, "closing the window disconnected the XNAT session")
    check(not win._xnat_thread.isRunning(), "and stopped the XNAT thread")
    strays = [t.name for t in threading.enumerate() if t is not threading.main_thread()]
    check(not strays, f"leaving no stray threads behind (saw {strays})")

    print()
    print("ALL XNAT SHUTDOWN CHECKS PASSED" if not failures
          else f"{len(failures)} FAILURE(S): {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
