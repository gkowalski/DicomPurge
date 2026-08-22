"""Offscreen check: Cmd+S / Ctrl+S commits the series being reviewed."""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QEventLoop, QTimer, Qt  # noqa: E402
from PySide6.QtGui import QKeySequence  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from deid_app.logging_setup import configure_logging  # noqa: E402
from deid_app.main_window import MainWindow  # noqa: E402

IN = Path("/tmp/fixtures")
failures = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


def pump(ms=200):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def press_save(widget):
    seq = QKeySequence(QKeySequence.StandardKey.Save)
    combo = seq[0]
    QTest.keyClick(widget, Qt.Key(combo.key().value), combo.keyboardModifiers())
    pump(200)


app = QApplication.instance() or QApplication(sys.argv)
win = MainWindow(configure_logging())
win.show()
pump(100)

seq_text = win.commit_action.shortcut().toString(QKeySequence.NativeText)
check(bool(seq_text), f"shortcut bound: {seq_text!r}")
check(seq_text.lower().endswith("s"), f"shortcut is the Save key ({seq_text})")
check("(" in win.commit_button.text(), f"button advertises it: {win.commit_button.text()!r}")

# 1. no series selected: must not crash, must not commit anything
press_save(win)
check(True, "pressing it with nothing selected does not crash")

win._start_scan(IN)
for _ in range(60):
    pump(100)
    if win.series_map:
        break
check(bool(win.series_map), f"{len(win.series_map)} series scanned")

uid, item = next(iter(win.series_items.items()))
series = win.series_map[uid]
win.tree.setCurrentItem(item)
pump(400)

# 2. place a box -> pending, then Cmd+S from the Image review tab
win.tabs.setCurrentIndex(0)
win._on_box_added(0.1, 0.1, 0.3, 0.2)
pump(100)
check(series.status == "pending", f"status after adding a box: {series.status}")
check(win.commit_button.isEnabled(), "Commit button is enabled")

press_save(win)
check(series.status == "committed", f"Cmd+S committed it (status={series.status})")
check(series.boxes and len(series.boxes) == 1, f"the box survived ({len(series.boxes)} box(es))")
check(win.series_items[uid].text(1).startswith("committed"),
      f"tree row updated: {win.series_items[uid].text(1)!r}")

# 3. pressing it again is a no-op with a message, not a second commit
press_save(win)
check(series.status == "committed", "pressing it again leaves it committed")

# 4. a new box re-opens the series; Cmd+S from another tab still commits
win._on_box_added(0.5, 0.5, 0.2, 0.2)
pump(100)
check(series.status == "pending", f"a new box re-opens it ({series.status})")
win.tabs.setCurrentIndex(1)  # Metadata tab
press_save(win)
check(series.status == "committed", f"Cmd+S works from the {win.tabs.tabText(1)} tab too")

# 5. reviewed-but-no-boxes series commits too (same as the button)
uid2, item2 = list(win.series_items.items())[1]
series2 = win.series_map[uid2]
win.tree.setCurrentItem(item2)
pump(400)
check(series2.status == "reviewed", f"second series is {series2.status}")
press_save(win)
check(series2.status == "committed", f"Cmd+S commits a box-free series ({series2.status})")

win.close()
print()
print("ALL SHORTCUT CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S): {failures}")
sys.exit(1 if failures else 0)
