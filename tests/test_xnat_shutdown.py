"""Offscreen check: closing the window closes the XNAT session.

Two regression guards. First, a plain `signal.emit()` races QThread.quit() and
loses, so the session's DELETE /data/JSESSION never goes out and the server is
left holding a session until it times out - closing must block on the logout.
Second, the same teardown has to work from QApplication.aboutToQuit, which is
the only path that covers a quit that never closes a window.
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

    # Calling it again is what happens for real: closeEvent runs, then
    # aboutToQuit fires and calls the same handler.
    try:
        win.shutdown_xnat()
        check(True, "a second shutdown_xnat() is harmless")
    except Exception as exc:
        check(False, f"second shutdown_xnat() raised: {exc}")

    # -- the aboutToQuit path on its own, with no window close at all --------
    # QApplication.exit() never closes windows, so closeEvent would not run and
    # the session would be left for the server to expire.
    second = FakeSession()
    xw.open_session = lambda cfg: second
    win2 = MainWindow(configure_logging())
    win2.show()
    win2.xnat_settings = XnatSettings(
        server="https://x.org/xnat", user="testUser", password="pw"
    )
    win2._update_xnat_button()
    win2.xnat_login()
    for _ in range(20):
        pump(100)
        if win2._xnat_user:
            break
    check(win2._xnat_user == "testUser", "second window logged in")

    # Exactly what main.py wires aboutToQuit to - no close() anywhere.
    win2.shutdown_xnat()
    check(second.disconnected,
          "the aboutToQuit path disconnects without any window close")
    check(not win2._xnat_thread.isRunning(), "and stops that thread too")

    # The XNAT assertions above are done; close now only to stop win2's frame
    # and prefetch threads, which Qt aborts over if left running at exit.
    win2.close()
    pump(300)

    print()
    print("ALL XNAT SHUTDOWN CHECKS PASSED" if not failures
          else f"{len(failures)} FAILURE(S): {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
