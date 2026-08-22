"""Offscreen smoke test: build the window, scan fixtures, place a box, commit, export."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from deid_app.logging_setup import configure_logging  # noqa: E402
from deid_app.main_window import MainWindow  # noqa: E402

IN = Path("/tmp/fixtures")
OUT = Path("/tmp/fixtures_gui_out")

failures = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


def win_frames_for(win, series):
    return [f for f in win._frames] if win.current_series is series else series.instances


def pump(ms=300):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    app = QApplication(sys.argv)
    bridge = configure_logging()
    win = MainWindow(bridge)
    win.show()

    # Toolbar logo
    from deid_app.resources import LOGO_PATH
    check(LOGO_PATH.is_file(), f"logo present at {LOGO_PATH}")
    pm = win.logo_label.pixmap()
    check(pm is not None and not pm.isNull(), "logo pixmap loaded into the top toolbar")
    check(pm.height() == 36, f"logo scaled to 36px high (got {pm.height() if pm else '-'})")
    check(not win.windowIcon().isNull(), "window icon set from the logo")

    win._start_scan(IN)
    for _ in range(40):
        pump(100)
        if win.series_map:
            break
    check(len(win.series_map) == 3, f"3 series found (got {len(win.series_map)})")

    # Tree: only series items are selectable.
    selectable = []
    def walk(item):
        for i in range(item.childCount()):
            child = item.child(i)
            selectable.append(bool(child.flags() & child.flags().ItemIsSelectable))
            walk(child)
    for i in range(win.tree.topLevelItemCount()):
        top = win.tree.topLevelItem(i)
        selectable.append(bool(top.flags() & top.flags().ItemIsSelectable))
        walk(top)
    check(sum(selectable) == 3, f"only the 3 series nodes are selectable (got {sum(selectable)})")

    check(
        all(s.status == "clean" for s in win.series_map.values()),
        "every series starts as clean",
    )

    # Select each series, place a box, verify the indicator states, commit.
    for uid, series in win.series_map.items():
        item = win.series_items[uid]
        win.tree.setCurrentItem(item)
        pump(150)
        check(win.current_series is series, f"series {series.series_number} selected")
        check(series.reviewed and series.status == "reviewed",
              f"series {series.series_number} auto-marked reviewed on selection")
        check(win.canvas._pixmap is not None, f"series {series.series_number} rendered a frame")
        expected_frames = sum(1 for _ in series.instances)
        check(len(win._frames) >= expected_frames, f"series {series.series_number} slider has >= {expected_frames} positions")

        win._on_box_added(0.2, 0.2, 0.3, 0.3)
        check(series.status == "pending", "indicator turns red after first box")
        win.commit_series()
        check(series.status == "committed", "indicator turns green after commit")

    # Slider movement
    last = win.slider.maximum()
    win.slider.setValue(last)
    pump(150)
    check(win.frame_label.text() == f"{last+1} / {last+1}", "slider moves to the last frame")

    # Mouse wheel over the image cycles frames just like the slider
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent

    def wheel(delta):
        pos = QPointF(win.canvas.width() / 2, win.canvas.height() / 2)
        ev = QWheelEvent(pos, win.canvas.mapToGlobal(pos.toPoint()).toPointF(),
                         QPoint(0, 0), QPoint(0, delta), Qt.NoButton,
                         Qt.NoModifier, Qt.NoScrollPhase, False)
        QApplication.sendEvent(win.canvas, ev)
        pump(60)

    # Pick the 3-image series so there is something to scroll through.
    multi = next(s for s in win.series_map.values() if len(win_frames_for(win, s)) > 1)
    win.tree.setCurrentItem(win.series_items[multi.series_uid])
    pump(200)
    win.slider.setValue(0)
    pump(60)
    n = len(win._frames)
    wheel(-120)
    check(win.slider.value() == 1, f"wheel down advances one image (got {win.slider.value()})")
    wheel(120)
    check(win.slider.value() == 0, f"wheel up goes back one image (got {win.slider.value()})")
    wheel(120)
    check(win.slider.value() == 0, "wheel up at the first image clamps")
    for _ in range(n + 3):
        wheel(-120)
    check(win.slider.value() == n - 1, f"wheel down clamps at the last image ({win.slider.value()} of {n-1})")
    check(win.frame_label.text() == f"{n} / {n}", "frame counter follows the wheel")

    # Fine-grained trackpad deltas accumulate to exactly one step
    win.slider.setValue(0); pump(60)
    for _ in range(11):
        wheel(-10)
    check(win.slider.value() == 0, "sub-notch trackpad deltas do not step early")
    wheel(-10)
    check(win.slider.value() == 1, f"accumulated trackpad deltas step once (got {win.slider.value()})")

    # Reset drops boxes but keeps 'reviewed'
    s = win.current_series
    win.reset_series_boxes()
    check(s.status == "reviewed" and not s.boxes,
          f"reset drops boxes and falls back to reviewed (got {s.status})")
    check(s.export_ready, "a reviewed series with no boxes is export-ready")

    # Context-menu action: set back to clean
    win._set_series_clean(s)
    check(s.status == "clean", "set-to-clean returns the series to clean")
    check(not s.export_ready, "a clean series is NOT export-ready")
    check(not win.export_button.text().endswith("..."),
          "export button reports the series that are not ready")

    # And a clean series blocks export until reviewed again
    win.tree.setCurrentItem(win.series_items[s.series_uid])
    pump(150)
    check(s.status == "reviewed", "re-selecting a clean series marks it reviewed again")
    win._on_box_added(0.2, 0.2, 0.3, 0.3)
    check(s.status == "pending", "boxes outrank reviewed (pending)")
    check(not s.export_ready, "a pending series is NOT export-ready")
    win.commit_series()

    check(all(x.export_ready for x in win.series_map.values()), "all series export-ready")

    # Export (bypassing the file dialog).
    from deid_app.export import start_export
    done = {}
    thread, worker = start_export(
        list(win.series_map.values()), IN, OUT,
        lambda *a: None,
        lambda w, r, e: done.update(written=w, redacted=r, errors=e),
        lambda m: done.update(error=m),
    )
    thread.start()
    for _ in range(60):
        pump(100)
        if done:
            break
    thread.quit(); thread.wait(3000)
    check(done.get("errors") == 0, f"export completed without errors ({done})")
    check(done.get("written") == 5, f"5 files written (got {done.get('written')})")
    src_rel = {p.relative_to(IN) for p in IN.rglob('*.dcm')}
    out_rel = {p.relative_to(OUT) for p in OUT.rglob('*.dcm')}
    check(src_rel == out_rel, "exported tree mirrors the input tree")

    # Log pane received records and filters.
    check(len(win.log_pane._records) > 0, f"log pane captured {len(win.log_pane._records)} record(s)")
    win.log_pane.level_box.setCurrentText("ERROR")
    pump(50)
    win.log_pane.level_box.setCurrentText("INFO")
    pump(50)
    check(True, "log level filter re-renders without error")

    win.close()
    print()
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED")
        return 1
    print("ALL GUI CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
