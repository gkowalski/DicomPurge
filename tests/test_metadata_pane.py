"""Offscreen check of the Metadata tab against the fixture datasets."""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QTreeWidgetItem  # noqa: E402

from deid_app.logging_setup import LogBridge, configure_logging  # noqa: E402
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


def walk(item, depth=0):
    yield depth, item
    for i in range(item.childCount()):
        yield from walk(item.child(i), depth + 1)


app = QApplication.instance() or QApplication(sys.argv)
bridge = configure_logging() if not isinstance(configure_logging(), type(None)) else LogBridge()
win = MainWindow(bridge)
win.show()

win._start_scan(IN)
for _ in range(60):
    pump(100)
    if win.series_map:
        break
check(bool(win.series_map), f"{len(win.series_map)} series scanned")

tab_names = [win.tabs.tabText(i) for i in range(win.tabs.count())]
check("Metadata" in tab_names, f"Metadata tab present ({tab_names})")

pane = win.metadata_pane
check(pane.tree.topLevelItemCount() == 0, "pane empty before a series is selected")

for uid, item in win.series_items.items():
    win.tree.setCurrentItem(item)
    pump(300)
    series = win.series_map[uid]
    root = pane.tree.topLevelItem(0)
    check(root is not None and root.text(0) == "DICOMObject",
          f"[{series.series_description}] root row is DICOMObject")
    rows = list(walk(root))
    check(len(rows) > 10, f"[{series.series_description}] {len(rows)} rows built")

    headers = [pane.tree.headerItem().text(c) for c in range(pane.tree.columnCount())]
    check(headers == ["Field Name", "Tag", "VR", "Size", "Content"],
          f"columns are {headers}")

    texts = {i.text(0): i for _, i in rows}
    check("Transfer Syntax UID" in texts or "TransferSyntaxUID" in texts,
          f"[{series.series_description}] file meta included")
    check("Modality" in texts, f"[{series.series_description}] Modality row present")
    if "Modality" in texts:
        m = texts["Modality"]
        check(m.text(1) == "0008,0060" and m.text(2) == "CS" and m.text(4) == series.modality,
              f"  Modality row: tag={m.text(1)} vr={m.text(2)} size={m.text(3)} content={m.text(4)!r}")

    # Pixel Data must never be dumped as text
    pd = texts.get("Pixel Data")
    check(pd is not None and pd.text(4).startswith("<binary"),
          f"[{series.series_description}] Pixel Data shown as {pd.text(4)[:40] if pd else 'MISSING'!r}")

    # Multi-valued element expands into child 'value' rows
    it = texts.get("Image Type")
    if it is not None:
        kids = [it.child(i).text(4) for i in range(it.childCount())]
        check(it.childCount() >= 2, f"  Image Type expands into {kids}")

# filter
win.tree.setCurrentItem(next(iter(win.series_items.values())))
pump(300)
pane.filter_edit.setText("modality")
pump(200)
visible = [i.text(0) for _, i in walk(pane.tree.topLevelItem(0)) if not i.isHidden()]
check(any("Modality" in v for v in visible) and len(visible) < 15,
      f"filter narrowed to {visible}")
pane.filter_edit.setText("")
pump(200)
hidden = [i.text(0) for _, i in walk(pane.tree.topLevelItem(0)) if i.isHidden()]
check(not hidden, f"clearing the filter restores every row ({len(hidden)} still hidden)")

# file meta toggle
before = len(list(walk(pane.tree.topLevelItem(0))))
pane.file_meta_box.setChecked(False)
pump(200)
after = len(list(walk(pane.tree.topLevelItem(0))))
check(after < before, f"file-meta toggle drops rows ({before} -> {after})")
pane.file_meta_box.setChecked(True)
pump(200)

# deselecting clears
win._set_series_clean(win.current_series)
pump(200)
check(pane.tree.topLevelItemCount() == 0, "pane cleared when the series is deselected")

win.close()
print()
print("ALL METADATA CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S): {failures}")
sys.exit(1 if failures else 0)
