"""Offscreen check: a zip over 2 GiB reports progress, and a worker-thread error shows a dialog.

Uploading a ~1 GB study built a 14.4 GiB zip. Its byte total overflowed the
C int behind Signal(int, ...) and killed the upload thread; the excepthook then
opened a QMessageBox from that thread, which aborts the process on macOS. So:

* the progress signals must carry Python ints of any size, and the progress bar
  (also 32-bit) must be fed scaled values;
* an exception escaping on a non-GUI thread must reach the user through a dialog
  built on the GUI thread.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QEventLoop, QThread, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import main as app_main  # noqa: E402
from deid_app.xnat_pane import XnatPane  # noqa: E402
from deid_app.xnat_upload import UploadRequest  # noqa: E402
from deid_app.xnat_upload_worker import UploadJob, UploadManager  # noqa: E402

logging.disable(logging.CRITICAL)
INT_MAX = 2**31 - 1
ZIP_BYTES = 15_482_081_936   # the zip that crashed the app
failures = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


def pump(ms=150):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


app = QApplication.instance() or QApplication([])

print("progress signal and bar with a >2 GiB total")
pane = XnatPane()
pane.add_job(1, "1.2.3", "SUBJ", "SESS", "P1")
manager = UploadManager()
received = []
manager.progress.connect(lambda *args: received.append(args))
manager.progress.connect(pane.set_job_progress)

job = UploadJob(UploadRequest.__new__(UploadRequest))
job.request.job_id = 1
job.progress.connect(manager.progress)

errors = []
try:
    job._progress(ZIP_BYTES // 2, ZIP_BYTES, "Uploading")
    pump()
except OverflowError as exc:  # pragma: no cover - the bug
    errors.append(exc)
check(not errors, "emitting a 15 GB total raises no OverflowError")
check(received == [(1, ZIP_BYTES // 2, ZIP_BYTES, "Uploading")],
      f"slot receives the exact values ({received})")

bar = pane._bar(1)
check(bar.maximum() <= INT_MAX, f"bar maximum fits a C int ({bar.maximum()})")
check(0 < bar.value() <= bar.maximum(), f"bar value is in range ({bar.value()})")
check(abs(bar.value() / bar.maximum() - 0.5) < 0.001, "bar is half full at the half-way point")
check("14764 MB" in bar.format(), f"format shows the real size in MB ({bar.format()!r})")

job._progress(ZIP_BYTES, ZIP_BYTES, "Uploading")
pump()
check(bar.value() == bar.maximum(), "bar reaches 100% at the end")
job._progress(ZIP_BYTES + 5, ZIP_BYTES, "Uploading")
pump()
check(bar.value() <= bar.maximum(), "a done count past the total is clamped")

print("progress from a worker thread")
received.clear()
worker = threading.Thread(target=lambda: job._progress(1, ZIP_BYTES, "Uploading"))
worker.start()
worker.join()
pump()
check(received == [(1, 1, ZIP_BYTES, "Uploading")], "emit from another thread is delivered")

print("small totals keep their exact range")
pane.add_job(2, "1.2.4", "SUBJ", "SESS2", "P1")
pane.set_job_progress(2, 3, 10, "Staging")
bar2 = pane._bar(2)
check((bar2.maximum(), bar2.value()) == (10, 3), "file-count phases are not scaled")

print("unhandled exception on a worker thread")
shown = []


class FakeMessageBox:
    @staticmethod
    def critical(parent, title, text):
        shown.append((QThread.currentThread(), title, text))


app_main.QMessageBox = FakeMessageBox
saved = sys.excepthook, threading.excepthook
app_main._install_excepthook()


def boom():
    raise OverflowError("value too large")


t = threading.Thread(target=boom)
t.start()
t.join()
check(not shown, "dialog is not built on the failing thread")
pump()
gui_thread = app.thread()
check(len(shown) == 1, f"one dialog shown ({len(shown)})")
if shown:
    check(shown[0][0] == gui_thread, "dialog was built on the GUI thread")
    check("OverflowError" in shown[0][2] and "value too large" in shown[0][2],
          f"dialog names the error ({shown[0][2]!r})")

shown.clear()
try:
    raise RuntimeError("main thread failure")
except RuntimeError:
    sys.excepthook(*sys.exc_info())
pump()
check(len(shown) == 1 and shown[0][0] == gui_thread, "sys.excepthook on the GUI thread also shows one dialog")
sys.excepthook, threading.excepthook = saved

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
