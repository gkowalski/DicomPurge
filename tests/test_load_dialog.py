"""Offscreen check: the "Loading series" popup never outlives the load.

The popup is armed on every series selection and must be retired by whatever
finishes that load - including the path where the first frame is already in the
frame cache and no worker is involved at all.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QProgressDialog  # noqa: E402

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


def visible_dialogs():
    return [
        w for w in QApplication.topLevelWidgets()
        if isinstance(w, QProgressDialog) and w.isVisible()
    ]


def select(win, item, settle=500):
    """Select a series and wait past the popup's 300 ms show delay."""
    win.tree.setCurrentItem(item)
    pump(settle)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow(configure_logging())
    win.show()

    win._start_scan(IN)
    for _ in range(60):
        pump(100)
        if win.series_map:
            break
    check(len(win.series_map) >= 2, f"{len(win.series_map)} series scanned")

    items = list(win.series_items.items())
    uids = [uid for uid, _ in items]

    # A -> B -> A. The revisit is the all-cached path: no worker round-trip, so
    # nothing but _apply_frame_result is left to close the popup.
    print("\n--- revisiting a series whose frames are already cached ---")
    for label, (uid, item) in (
        ("first visit to A", items[0]),
        ("first visit to B", items[1]),
        ("revisit of A", items[0]),
    ):
        select(win, item)
        series = win.series_map[uid]
        check(win._load_dialog is None, f"[{label}] popup retired ({series.label()})")
        check(not visible_dialogs(), f"[{label}] no progress dialog left on screen")
        check(win.frame_label.text() == f"1 / {len(win._frames)}",
              f"[{label}] first image displayed ({win.frame_label.text()})")

    # Fast switching: fire every series with no event pumping in between, the
    # way an impatient click-through does.
    print("\n--- switching series as fast as the tree will take it ---")
    for _round in range(3):
        for _uid, item in items:
            win.tree.setCurrentItem(item)
    pump(800)

    last_uid = uids[-1]
    last_series = win.series_map[last_uid]
    check(win._load_dialog is None, "popup retired after rapid switching")
    check(not visible_dialogs(), "no progress dialog left on screen")
    check(win.current_series is last_series,
          f"showing the last series selected ({win.current_series.label()})")

    expected = sum(
        max(1, int(getattr(__import__("pydicom").dcmread(str(inst.path), stop_before_pixels=True),
                           "NumberOfFrames", 1) or 1))
        for inst in last_series.instances
    )
    check(len(win._frames) == expected,
          f"frame list belongs to the displayed series ({len(win._frames)} of {expected})")
    check(win.frame_label.text() == f"1 / {len(win._frames)}",
          f"first image of that series displayed ({win.frame_label.text()})")

    # The other half of the contract: a load slow enough to cross the 300 ms
    # threshold must still put the popup on screen.
    print("\n--- a load that outlasts the show delay still shows the popup ---")
    win._open_load_dialog(last_series)
    pump(500)
    check(bool(visible_dialogs()), "popup appears once the load passes 300 ms")
    win._close_load_dialog()
    pump(100)
    check(not visible_dialogs(), "popup closes when the load ends")

    win.close()
    print()
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED")
        return 1
    print("ALL LOAD-DIALOG CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
