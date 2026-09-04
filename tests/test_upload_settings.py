"""Headless check: the XNAT upload settings survive a save/load round trip."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from deid_app.app_settings import MAX_CONCURRENT_UPLOADS, AppSettings  # noqa: E402
from deid_app.dialogs import SettingsDialog  # noqa: E402

failures = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


app = QApplication.instance() or QApplication(sys.argv)
ini = Path(tempfile.mkdtemp()) / "settings.ini"
store = QSettings(str(ini), QSettings.IniFormat)

defaults = AppSettings()
check(defaults.xnat_upload_zip is True and defaults.xnat_upload_temp_dir == ""
      and defaults.xnat_upload_max_concurrent == 2, "defaults: zip, system temp, 2 at a time")
check(defaults.upload_temp_root() == Path(tempfile.gettempdir()), "empty temp dir means the system one")

edited = AppSettings(xnat_upload_zip=False, xnat_upload_temp_dir="/tmp/deid_stage",
                     xnat_upload_max_concurrent=5)
edited.save(store)
store.sync()
loaded = AppSettings.load(QSettings(str(ini), QSettings.IniFormat))
check(loaded.xnat_upload_zip is False, "zip mode round-trips (as a bool, not a string)")
check(loaded.xnat_upload_temp_dir == "/tmp/deid_stage", "temp dir round-trips")
check(loaded.xnat_upload_max_concurrent == 5, "concurrency round-trips")
check(loaded.upload_temp_root() == Path("/tmp/deid_stage"), "and resolves to that path")

store.setValue("xnat_upload/max_concurrent", 99)
store.setValue("xnat_upload/zip_mode", "false")
store.sync()
loaded = AppSettings.load(QSettings(str(ini), QSettings.IniFormat))
check(loaded.xnat_upload_max_concurrent == MAX_CONCURRENT_UPLOADS, "concurrency clamped to the ceiling")
check(loaded.xnat_upload_zip is False, "a string 'false' from the ini reads as False")

dialog = SettingsDialog(edited)
values = dialog.values
check((values.xnat_upload_zip, values.xnat_upload_temp_dir, values.xnat_upload_max_concurrent)
      == (False, "/tmp/deid_stage", 5), "dialog shows the current values")
dialog._restore_defaults()
values = dialog.values
check((values.xnat_upload_zip, values.xnat_upload_temp_dir, values.xnat_upload_max_concurrent)
      == (True, "", 2), "Restore Defaults resets all three")

print()
print("ALL UPLOAD SETTINGS CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
