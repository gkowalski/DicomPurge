"""Offscreen check: the XNAT server tab's gating, labels and job rows.

Runs the real MainWindow with a fake XNAT session (the worker and its thread
are real) and a fake upload session, so what is exercised is the wiring:
login fills the project list, readiness follows the tree, the Upload button
is enabled only when everything lines up, a click queues a job whose row
follows the manager's signals, and quitting mid-upload asks first.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtGui import QCloseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import deid_app.main_window as mw  # noqa: E402
import deid_app.xnat_upload_worker as uw  # noqa: E402
import deid_app.xnat_worker as xw  # noqa: E402
from deid_app.logging_setup import configure_logging  # noqa: E402
from deid_app.xnat_settings import XnatSettings  # noqa: E402
from make_fixtures import build  # noqa: E402

IN = Path("/tmp/fixtures_pane")
TMP = Path("/tmp/fixtures_pane_tmp")
failures = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


def pump(ms=150):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_until(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        pump(30)
    return pred()


class FakeLoginSession:
    """What XnatWorker holds: login + the two listings."""

    logged_in_user = "tester"
    archive_labels = {"TP001_US_1"}

    def __init__(self):
        self.disconnected = False

    def get_json(self, path, query=None):
        if path == "/data/projects":
            return {"ResultSet": {"Result": [
                {"ID": "P2", "name": "Second"}, {"ID": "P1", "name": "First"}]}}
        if path == "/data/projects/P1/experiments":
            return {"ResultSet": {"Result": [{"ID": "x", "label": l} for l in self.archive_labels]}}
        return {"ResultSet": {"Result": []}}

    def disconnect(self):
        self.disconnected = True


class FakeResponse:
    text = "/xapi/direct-archive/P1/1.2.3/TP001_US_2\r\n"
    status_code = 200


class FakeUploadSession:
    xnat_version_tuple = (1, 8, 5)
    xnat_version = "1.8.5"
    delay = 0.5
    calls = []

    def upload_stream(self, uri, stream, query=None, **kw):
        stream.read()
        FakeUploadSession.calls.append(dict(query or {}))
        time.sleep(FakeUploadSession.delay)
        return FakeResponse()

    def get_json(self, path, query=None):
        label = path.rsplit("/", 1)[-1]
        return {"ResultSet": {"Result": [{"ID": "X", "label": "TP001_US_2"}, {"ID": "Y", "label": "L"}]
                              + [{"ID": "Z", "label": f"L{i}"} for i in range(50)]}}

    def disconnect(self):
        pass


app = QApplication.instance() or QApplication(sys.argv)
bridge = configure_logging(logging.ERROR)
if IN.exists():
    shutil.rmtree(IN)
if TMP.exists():
    shutil.rmtree(TMP)
build(IN)

xw.open_session = lambda cfg: FakeLoginSession()
uw.open_session = lambda creds: FakeUploadSession()

win = mw.MainWindow(bridge)
win.app_settings.xnat_upload_temp_dir = str(TMP)
# Pin the mode: MainWindow loads the real QSettings, and the user's choice
# there must not decide whether this test sees one POST or five.
win.app_settings.xnat_upload_zip = True
win.app_settings.xnat_upload_max_concurrent = 1
win._upload_manager.set_max_concurrent(1)
pane = win.xnat_pane
tabs = [win.tabs.tabText(i) for i in range(win.tabs.count())]

print("tab and initial state")
check(tabs == ["Image review", "Metadata", "Log", "XNAT server"], f"tab after Log (got {tabs})")
check(not pane.upload_button.isEnabled(), "Upload disabled before anything is loaded")
check("Log in" in pane.upload_button.toolTip(), f"tooltip says why: {pane.upload_button.toolTip()}")

from PySide6.QtWidgets import QHeaderView  # noqa: E402
for name, table in (("studies", pane.studies_table), ("uploads", pane.uploads_table)):
    header = table.horizontalHeader()
    modes = {header.sectionResizeMode(i) for i in range(table.columnCount())}
    check(modes == {QHeaderView.Interactive}, f"{name} table: every column is user-resizable")
    check(header.stretchLastSection(), f"{name} table: last column fills the remaining width")
    table.setColumnWidth(1, 333)
    check(table.columnWidth(1) == 333, f"{name} table: a column keeps the width it is dragged to")

print()
print("scan, then log in")
win._start_scan(IN)
wait_until(lambda: bool(win.series_map))
pump(200)
check(pane.studies_table.rowCount() == 1, "one study row from the fixtures")
check(pane.studies_table.item(0, 6).text() == "3 series not ready", "status shows how many series block it")
win.xnat_settings = XnatSettings(server="https://x.org", user="u", password="p")
win.xnat_login()
wait_until(lambda: win._xnat_user is not None)
wait_until(lambda: pane.project_combo.count() == 2)
pump(200)
check(pane.status_label.text() == "Logged in as tester", "pane shows the server-confirmed user")
check([pane.project_combo.itemData(i) for i in range(2)] == ["P1", "P2"],
      "projects listed by name, first one selected")
check(pane.current_project() == "P1", "first project selected")
check(pane.label_edit.text() == "TP001_US_2",
      f"default label skips the one already in P1 (got {pane.label_edit.text()})")
check(not pane.upload_button.isEnabled() and "reviewed" in pane.upload_button.toolTip(),
      "still disabled: series not reviewed")

print()
print("readiness follows the tree")
for s in win.series_map.values():
    s.reviewed = True
win._update_export_button()
check(pane.studies_table.item(0, 6).text() == "ready", "status turns ready")
check(pane.upload_button.isEnabled(), "Upload enabled once every series is reviewed")
pane.label_edit.setText("TP001_US_1")
pane._on_label_edited("TP001_US_1")
check(not pane.upload_button.isEnabled() and "already exists" in pane.upload_button.toolTip(),
      "a label already on the server is refused")
pane.default_button.click()
check(pane.label_edit.text() == "TP001_US_2" and pane.upload_button.isEnabled(), "Default restores it")
pane.project_combo.setCurrentIndex(1)
pump(200)
check(pane.label_edit.text() == "TP001_US_1", "switching to a project without that label makes _1 the default")
pane.project_combo.setCurrentIndex(0)
pump(200)
check(pane.label_edit.text() == "TP001_US_2", "and back")

print()
print("type-to-filter project selection")
edit = pane.project_combo.lineEdit()
edit.setText("sec")
edit.textEdited.emit("sec")
check(pane.current_project() is None, "a partial filter is not a selection")
check(not pane.upload_button.isEnabled() and "filtered list" in pane.upload_button.toolTip(),
      "Upload waits for a real pick and says so")
pane._on_project_typed("Second (P2)")
check(pane.current_project() == "P2", "picking a completion selects that project")
pump(100)
check(pane.label_edit.text() == "TP001_US_1", "and the label defaults follow the new project")
edit.setText("nonsense")
pane._on_project_edit_finished()
check(edit.text() == "Second (P2)" and pane.current_project() == "P2",
      "unknown text on Enter falls back to the selected project")
edit.setText("first (p1)")
pane._on_project_edit_finished()
check(pane.current_project() == "P1" and edit.text() == "First (P1)",
      "an exact name, any case, selects on Enter")
pump(200)
check(pane.label_edit.text() == "TP001_US_2", "back on P1 the existing label is skipped again")
completions = []
model = pane.project_completer.completionModel()
pane.project_completer.setCompletionPrefix("p2")
for i in range(model.rowCount()):
    completions.append(model.index(i, 0).data())
check(completions == ["Second (P2)"], f"completer matches inside the text, case-insensitively (got {completions})")

print()
print("upload")
FakeUploadSession.calls = []
pane.upload_button.click()
pump(100)
check(pane.uploads_table.rowCount() == 1, "a job row appears")
check(win._upload_manager.busy, "the manager has the job")
check(not pane.upload_button.isEnabled() and "already queued" in pane.upload_button.toolTip(),
      "the same study cannot be queued twice")
check(pane.studies_table.item(0, 6).text() == "uploading", "study status shows uploading")
wait_until(lambda: pane.job_status_text(1) == "archived", timeout=20)
check(pane.job_status_text(1) == "archived", f"row ends as archived (got {pane.job_status_text(1)!r})")
check(len(FakeUploadSession.calls) == 1 and FakeUploadSession.calls[0]["session"] == "TP001_US_2"
      and FakeUploadSession.calls[0]["project"] == "P1" and FakeUploadSession.calls[0]["subject"] == "TP001",
      f"the request carried project/subject/session from the tab (got {FakeUploadSession.calls})")
check(pane.studies_table.item(0, 6).text() == "archived", "study status remembers where it went")
check(pane.label_edit.text() == "TP001_US_3" or "already exists" in pane.upload_button.toolTip()
      or not pane.upload_button.isEnabled(),
      "the used label is not offered again")
pump(200)
check(not TMP.exists() or list(TMP.iterdir()) == [], "temp files gone")

print()
print("periodic status refresh of finished rows")
from deid_app.xnat_upload import UploadResult, WHERE_PREARCHIVE  # noqa: E402
pane.add_job(77, "fake-study", "TP009", "TP009_US_1", "P1")
pane.set_job_started(77)
pane.set_job_finished(77, UploadResult(uri="/data/prearchive/projects/P1/2026/TP009_US_1",
                                       archived=False, where=WHERE_PREARCHIVE, written=3))
check(pane.job_status_text(77) == "in prearchive", "a prearchive row starts as 'in prearchive'")
check(pane._refresh_timer.isActive(), "the refresh timer runs while such a row exists and we are logged in")
asked = []
pane.statusCheckRequested.connect(lambda lst: asked.append(list(lst)))
pane.request_status_check()
check(asked == [[("P1", "TP009_US_1")]], f"the check asks about exactly that row (got {asked})")
pane.apply_session_status({("P1", "TP009_US_1"): "prearchive:RECEIVING"})
check(pane.job_status_text(77) == "in prearchive (receiving)", f"prearchive status shown ({pane.job_status_text(77)!r})")
check(pane._refresh_timer.isActive(), "still watching")
pane.apply_session_status({("P1", "TP009_US_1"): "missing"})
check(pane.job_status_text(77) == "in prearchive (receiving)", "'missing' (mid-build) leaves the row alone")
pane.apply_session_status({("P1", "TP009_US_1"): "archived"})
check(pane.job_status_text(77) == "archived", "and it becomes archived when the server says so")
check(not pane._refresh_timer.isActive() and 77 not in pane._watching, "no longer watched; timer stopped")
# Wiring through MainWindow to the real worker and back.
pane.add_job(78, "fake-study-2", "TP010", "TP010_US_1", "P1")
pane.set_job_started(78)
pane.set_job_finished(78, UploadResult(uri="/x", archived=False, where=WHERE_PREARCHIVE))
FakeLoginSession.archive_labels = {"TP001_US_1", "TP010_US_1"}
pane.request_status_check()
wait_until(lambda: pane.job_status_text(78) == "archived", timeout=5)
check(pane.job_status_text(78) == "archived", "a check round-trips through the XNAT worker thread")
pane.set_logged_in(None)
check(not pane._refresh_timer.isActive(), "logging out stops the timer")
win._on_xnat_logged_in("tester")
pump(100)

print()
print("quit prompt while uploading")
FakeUploadSession.delay = 2.0
pane.default_button.click()
pane._user_edited.discard(pane.selected_row().study_uid)
pane._recompute_defaults()
pane._update_controls()
pane.upload_button.click()
pump(100)
asked = []
real_question = QMessageBox.question
QMessageBox.question = lambda *a, **k: (asked.append(a[1]), QMessageBox.No)[1]
try:
    ev = QCloseEvent()
    win.closeEvent(ev)
    check(asked and "Uploads in progress" in asked[0], "closing asks about running uploads")
    check(not ev.isAccepted(), "and answering No keeps the window open")
finally:
    QMessageBox.question = real_question
QMessageBox.question = lambda *a, **k: QMessageBox.Yes
try:
    win.close()
finally:
    QMessageBox.question = real_question
pump(300)
check(not win._upload_manager.busy, "Yes stops the uploads and closes")
check(win._xnat_thread is None or not win._xnat_thread.isRunning(), "XNAT thread stopped")

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED")
    sys.exit(1)
print("ALL XNAT PANE CHECKS PASSED")
