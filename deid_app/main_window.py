"""Main window: series tree on the left, image review + log tabs on the right."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QSettings,
    Qt,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QMenu,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QSlider,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QTabWidget,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QMainWindow,
)

from .decode_cache import DatasetCache, FrameCache
from .export import start_export
from .frame_worker import (
    FrameRenderRequest,
    FrameRenderResult,
    FrameWorker,
    PrefetchRequest,
    PrefetchWorker,
)
from .image_view import ImageCanvas, WheelAccumulator
from .log_pane import LogPane
from .logging_setup import LogBridge
from .metadata_pane import MetadataPane
from .model import Series, start_scan
from .resources import LOGO_PATH, app_icon, logo_pixmap
from .render import frame_count, read_dataset_header

log = logging.getLogger(__name__)

SERIES_UID_ROLE = Qt.UserRole + 1
# How far the prefetcher runs ahead of / behind the frame on screen. Ahead is
# deeper because a scroll keeps going the way it started; behind covers the
# small back-step people make after overshooting.
PREFETCH_AHEAD = 6
PREFETCH_BEHIND = 2
# How long the scroll has to settle before the Metadata tab is rebuilt.
METADATA_DEBOUNCE_MS = 250
STATUS_COLORS = {
    "clean": QColor("#9e9e9e"),
    "reviewed": QColor("#1e7fd4"),
    "pending": QColor("#e53935"),
    "committed": QColor("#2e9e4f"),
}
STATUS_TEXT = {
    "clean": "not reviewed",
    "reviewed": "reviewed",
    "pending": "boxes placed - NOT committed",
    "committed": "committed",
}
MAX_RECENT_DIRS = 10


def _status_icon(status: str) -> QIcon:
    pixmap = QPixmap(14, 14)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setBrush(STATUS_COLORS.get(status, QColor("#9e9e9e")))
    painter.setPen(QColor("#404040"))
    painter.drawEllipse(1, 1, 12, 12)
    painter.end()
    return QIcon(pixmap)


class MainWindow(QMainWindow):
    renderRequested = Signal(object)
    prefetchRequested = Signal(object)

    def __init__(self, bridge: LogBridge) -> None:
        super().__init__()
        self.setWindowTitle("Scuppernong - DICOM De-identification")
        self.setWindowIcon(app_icon())
        self.resize(1400, 880)

        self.settings = QSettings("de-id", "dicom-deid")
        self.series_map: dict[str, Series] = {}
        self.series_items: dict[str, QTreeWidgetItem] = {}
        self.current_series: Series | None = None
        self.input_root: Path | None = None

        # (instance_index, frame_index) pairs for the selected series.
        self._frames: list[tuple[int, int]] = []
        # Path whose tags the Metadata tab is currently showing.
        self._metadata_path: Path | None = None

        # Background frame decoding: bumping _render_epoch invalidates any
        # in-flight/pending render belonging to a since-abandoned series or
        # selection. Only one render is ever in flight; a newer request
        # while one is running just overwrites _render_pending so a fast
        # scrub skips straight to the position the user settles on.
        self._render_epoch = 0
        self._render_inflight = False
        # Modal "loading series" popup, alive from the click on a series until
        # its first frame is on screen. None whenever nothing is loading.
        self._load_dialog: QProgressDialog | None = None
        # Notch accumulation for wheel events over the frame slider.
        self._slider_wheel = WheelAccumulator()
        # Metadata tab is rebuilt lazily: this holds what it *should* show, and
        # _metadata_path what it is actually showing.
        self._pending_metadata: tuple[Series, object, object] | None = None
        self._metadata_timer = QTimer(self)
        self._metadata_timer.setSingleShot(True)
        self._metadata_timer.setInterval(METADATA_DEBOUNCE_MS)
        self._metadata_timer.timeout.connect(self._flush_metadata)
        self._render_pending: FrameRenderRequest | None = None
        # Decoded datasets and rendered frames, shared with both workers. The
        # frame cache is what makes a re-visited image appear instantly; the
        # dataset cache mostly serves multi-frame instances.
        self._dataset_cache = DatasetCache(capacity=12)
        self._frame_cache = FrameCache(capacity=64)
        # Position the user is actually on. A worker result for anything else
        # is stale - it can be outrun by a cache hit displayed while it was
        # still decoding - and must not be painted over the current image.
        self._render_target: int | None = None
        # Last position handed to _prefetch_neighbors, for direction of travel.
        self._last_position: int | None = None

        self._frame_thread = QThread()
        self._frame_worker = FrameWorker(self._dataset_cache, self._frame_cache)
        self._frame_worker.moveToThread(self._frame_thread)
        self.renderRequested.connect(self._frame_worker.render)
        self._frame_worker.rendered.connect(self._on_frame_rendered)
        self._frame_thread.start()

        # Opportunistic neighbor-instance prefetch, on its own thread so a
        # slow prefetch decode never delays a real user-requested render.
        self._prefetch_thread = QThread()
        self._prefetch_worker = PrefetchWorker(self._dataset_cache, self._frame_cache)
        self._prefetch_worker.moveToThread(self._prefetch_thread)
        self.prefetchRequested.connect(self._prefetch_worker.prefetch)
        self._prefetch_thread.start()

        self._scan_thread = None
        self._scan_worker = None
        self._export_thread = None
        self._export_worker = None

        self._build_ui(bridge)
        self._load_recent_dirs()
        self._update_export_button()
        log.info("Application ready. Choose an input directory to begin.")

    # -- construction ----------------------------------------------------
    def _build_ui(self, bridge: LogBridge) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)

        # Top bar -------------------------------------------------------
        top = QHBoxLayout()

        self.logo_label = QLabel()
        pixmap = logo_pixmap(36)
        if pixmap is not None:
            self.logo_label.setPixmap(pixmap)
            self.logo_label.setToolTip(str(LOGO_PATH.name))
        self.logo_label.setContentsMargins(2, 0, 8, 0)
        top.addWidget(self.logo_label, 0, Qt.AlignVCenter)

        top.addWidget(QLabel("Input directory:"))
        self.dir_combo = QComboBox()
        self.dir_combo.setMinimumWidth(520)
        self.dir_combo.setToolTip("Recently used input directories")
        self.dir_combo.activated.connect(self._on_dir_combo_activated)
        top.addWidget(self.dir_combo, 1)

        self.browse_button = QPushButton("Browse...")
        self.browse_button.clicked.connect(self.choose_input_directory)
        top.addWidget(self.browse_button)

        self.rescan_button = QPushButton("Rescan")
        self.rescan_button.setEnabled(False)
        self.rescan_button.clicked.connect(self._rescan)
        top.addWidget(self.rescan_button)

        top.addStretch(1)
        self.export_button = QPushButton("Export de-identified files...")
        self.export_button.clicked.connect(self.export)
        top.addWidget(self.export_button)
        outer.addLayout(top)

        # Splitter ------------------------------------------------------
        splitter = QSplitter(Qt.Horizontal)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Patient / Study / Series", "Status"])
        self.tree.setToolTip(
            "Grey = not reviewed, blue = reviewed, red = boxes not committed, "
            "green = committed.\nRight-click a series for status actions."
        )
        self.tree.setColumnWidth(0, 300)
        self.tree.setColumnWidth(1, 175)
        self.tree.itemSelectionChanged.connect(self._on_tree_selection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        splitter.addWidget(self.tree)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_image_tab(), "Image review")
        self.metadata_pane = MetadataPane()
        self.tabs.addTab(self.metadata_pane, "Metadata")
        self.log_pane = LogPane(bridge)
        self.tabs.addTab(self.log_pane, "Log")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        splitter.addWidget(self.tabs)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([420, 980])
        outer.addWidget(splitter, 1)

        self.setCentralWidget(central)
        self._build_shortcuts()

        status = QStatusBar()
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(240)
        self.progress.setVisible(False)
        status.addPermanentWidget(self.progress)
        self.setStatusBar(status)
        status.showMessage("Idle")

    def _build_shortcuts(self) -> None:
        """Cmd+S (Ctrl+S off macOS) commits the series being reviewed."""
        self.commit_action = QAction("Commit series", self)
        self.commit_action.setShortcut(QKeySequence.StandardKey.Save)
        self.commit_action.setShortcutContext(Qt.WindowShortcut)
        self.commit_action.triggered.connect(self.commit_series)
        self.addAction(self.commit_action)

        shortcut = self.commit_action.shortcut().toString(QKeySequence.NativeText)
        self.commit_button.setText(f"Commit series ({shortcut})")
        self.commit_button.setToolTip(
            self.commit_button.toolTip() + f"\n\nShortcut: {shortcut}"
        )

    def _build_image_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.header_label = QLabel("No series selected")
        self.header_label.setStyleSheet("font-weight: 600;")
        layout.addWidget(self.header_label)

        self.canvas = ImageCanvas()
        self.canvas.boxAdded.connect(self._on_box_added)
        self.canvas.boxDiscarded.connect(self._on_box_discarded)
        self.canvas.stepRequested.connect(self._step_frame)

        self.report_view = QTextBrowser()
        self.report_view.setReadOnly(True)
        self.report_view.setOpenExternalLinks(False)

        self.view_stack = QStackedWidget()
        self.view_stack.addWidget(self.canvas)
        self.view_stack.addWidget(self.report_view)
        layout.addWidget(self.view_stack, 1)

        slider_row = QHBoxLayout()
        slider_row.addWidget(QLabel("Image:"))
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setEnabled(False)
        self.slider.setMinimum(0)
        self.slider.setMaximum(0)
        self.slider.valueChanged.connect(self._on_slider_changed)
        # Qt's own wheel handling on a horizontal slider runs the opposite way
        # to the image canvas, so the filter below owns the wheel instead.
        self.slider.installEventFilter(self)
        slider_row.addWidget(self.slider, 1)
        self.frame_label = QLabel("- / -")
        self.frame_label.setMinimumWidth(90)
        self.frame_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        slider_row.addWidget(self.frame_label)
        layout.addLayout(slider_row)

        button_row = QHBoxLayout()
        self.boxes_label = QLabel("0 box(es)")
        button_row.addWidget(self.boxes_label)
        button_row.addStretch(1)
        self.reset_button = QPushButton("Reset boxes")
        self.reset_button.setToolTip("Drop every redaction box on this series")
        self.reset_button.setEnabled(False)
        self.reset_button.clicked.connect(self.reset_series_boxes)
        button_row.addWidget(self.reset_button)

        self.commit_button = QPushButton("Commit series")
        self.commit_button.setToolTip(
            "Lock in the redaction boxes for this series. A series with boxes must be "
            "committed before export; a series with no boxes only needs to be reviewed."
        )
        self.commit_button.setEnabled(False)
        self.commit_button.clicked.connect(self.commit_series)
        button_row.addWidget(self.commit_button)
        layout.addLayout(button_row)

        return page

    # -- input directory -------------------------------------------------
    def _load_recent_dirs(self) -> None:
        recent = self.settings.value("recent_dirs", []) or []
        if isinstance(recent, str):
            recent = [recent]
        self.dir_combo.blockSignals(True)
        self.dir_combo.clear()
        self.dir_combo.addItem("<select an input directory...>", None)
        for path in recent:
            self.dir_combo.addItem(path, path)
        self.dir_combo.addItem("Browse for another directory...", "__browse__")
        self.dir_combo.blockSignals(False)

    def _remember_dir(self, path: str) -> None:
        recent = self.settings.value("recent_dirs", []) or []
        if isinstance(recent, str):
            recent = [recent]
        recent = [p for p in recent if p != path]
        recent.insert(0, path)
        self.settings.setValue("recent_dirs", recent[:MAX_RECENT_DIRS])
        self._load_recent_dirs()
        index = self.dir_combo.findData(path)
        if index >= 0:
            self.dir_combo.setCurrentIndex(index)

    @Slot(int)
    def _on_dir_combo_activated(self, index: int) -> None:
        data = self.dir_combo.itemData(index)
        if data is None:
            return
        if data == "__browse__":
            self.choose_input_directory()
            return
        self._start_scan(Path(data))

    def choose_input_directory(self) -> None:
        start_dir = str(self.input_root) if self.input_root else str(Path.home())
        directory = QFileDialog.getExistingDirectory(
            self, "Select the input DICOM directory", start_dir
        )
        if not directory:
            log.debug("Input directory selection cancelled")
            return
        self._start_scan(Path(directory))

    def _rescan(self) -> None:
        if self.input_root:
            self._start_scan(self.input_root)

    def _start_scan(self, root: Path) -> None:
        if self._scan_thread is not None and self._scan_thread.isRunning():
            log.warning("A scan is already running")
            return
        if not root.is_dir():
            log.error("Not a directory: %s", root)
            QMessageBox.warning(self, "Invalid directory", f"{root} is not a directory.")
            return

        if self.series_map and any(s.boxes for s in self.series_map.values()):
            answer = QMessageBox.question(
                self,
                "Discard current work?",
                "Loading a new directory discards the redaction boxes placed so far.\n"
                "Continue?",
            )
            if answer != QMessageBox.Yes:
                log.info("Directory change cancelled by user")
                return

        self.input_root = root
        self._remember_dir(str(root))
        self._clear_series()
        self.rescan_button.setEnabled(True)
        self.browse_button.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.statusBar().showMessage(f"Scanning {root}...")

        self._scan_thread, self._scan_worker = start_scan(
            root, self._on_scan_progress, self._on_scan_finished, self._on_scan_failed
        )
        self._scan_thread.start()

    @Slot(int, int)
    def _on_scan_progress(self, done: int, total: int) -> None:
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        self.statusBar().showMessage(f"Scanning... {done}/{total}")

    @Slot(object)
    def _on_scan_finished(self, series_map) -> None:
        self.series_map = series_map
        self.progress.setVisible(False)
        self.browse_button.setEnabled(True)
        self._populate_tree()
        count = len(series_map)
        files = sum(len(s.instances) for s in series_map.values())
        self.statusBar().showMessage(f"{count} series / {files} image(s) loaded")
        if count == 0:
            QMessageBox.information(
                self,
                "No DICOM files",
                f"No *.dcm files were found under:\n{self.input_root}",
            )
        self._update_export_button()

    @Slot(str)
    def _on_scan_failed(self, message: str) -> None:
        self.progress.setVisible(False)
        self.browse_button.setEnabled(True)
        self.statusBar().showMessage("Scan failed")
        QMessageBox.critical(self, "Scan failed", message)

    # -- tree ------------------------------------------------------------
    def _clear_series(self) -> None:
        self._close_load_dialog()
        self._frame_cache.clear()
        self.series_map = {}
        self.series_items = {}
        self.current_series = None
        self._frames = []
        self._render_epoch += 1
        self._render_target = None
        self._last_position = None
        self._metadata_timer.stop()
        self._pending_metadata = None
        self._metadata_path = None
        self.tree.clear()
        self.metadata_pane.clear()
        self.canvas.set_image(None)
        self.canvas.set_boxes([])
        self.canvas.set_editable(False)
        self.canvas.set_placeholder("Select a series in the tree to display it.")
        self.header_label.setText("No series selected")
        self.slider.setEnabled(False)
        self.slider.setMaximum(0)
        self.frame_label.setText("- / -")
        self.reset_button.setEnabled(False)
        self.commit_button.setEnabled(False)
        self.boxes_label.setText("0 box(es)")

    def _populate_tree(self) -> None:
        self.tree.clear()
        self.series_items = {}

        patients: dict[str, QTreeWidgetItem] = {}
        studies: dict[tuple[str, str], QTreeWidgetItem] = {}

        for series in sorted(
            self.series_map.values(),
            key=lambda s: (s.patient_name, s.study_uid, s.series_number),
        ):
            patient_key = f"{series.patient_name} [{series.patient_id}]"
            patient_item = patients.get(patient_key)
            if patient_item is None:
                patient_item = QTreeWidgetItem(self.tree, [patient_key, ""])
                patient_item.setFlags(Qt.ItemIsEnabled)  # not selectable
                patients[patient_key] = patient_item

            study_key = (patient_key, series.study_uid)
            study_item = studies.get(study_key)
            if study_item is None:
                label = series.study_description or "(no study description)"
                study_item = QTreeWidgetItem(patient_item, [f"Study: {label}", ""])
                study_item.setFlags(Qt.ItemIsEnabled)  # not selectable
                studies[study_key] = study_item

            series_item = QTreeWidgetItem(study_item, [series.label(), ""])
            series_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            series_item.setData(0, SERIES_UID_ROLE, series.series_uid)
            self.series_items[series.series_uid] = series_item
            self._refresh_item(series)

        self.tree.expandAll()
        log.info("Tree populated with %d series", len(self.series_items))

    def _refresh_item(self, series: Series) -> None:
        item = self.series_items.get(series.series_uid)
        if item is None:
            return
        status = series.status
        item.setIcon(0, _status_icon(status))
        suffix = f" ({len(series.boxes)} box(es))" if series.boxes else ""
        item.setText(1, STATUS_TEXT[status] + suffix)
        item.setForeground(1, STATUS_COLORS[status])

    @Slot()
    def _on_tree_selection(self) -> None:
        items = self.tree.selectedItems()
        if not items:
            return
        uid = items[0].data(0, SERIES_UID_ROLE)
        if not uid:
            return
        series = self.series_map.get(uid)
        if series is None:
            return
        self._select_series(series)

    @Slot(object)
    def _on_tree_context_menu(self, position) -> None:
        item = self.tree.itemAt(position)
        if item is None:
            return
        uid = item.data(0, SERIES_UID_ROLE)
        if not uid:
            return  # patient / study nodes have no actions
        series = self.series_map.get(uid)
        if series is None:
            return

        menu = QMenu(self)
        menu.addAction(f"Status: {STATUS_TEXT[series.status]}").setEnabled(False)
        menu.addSeparator()

        clean_action = menu.addAction("Set status to Clean")
        clean_action.setToolTip("Clear reviewed/committed and discard any redaction boxes")
        clean_action.setEnabled(series.status != "clean")

        commit_action = menu.addAction("Commit series")
        commit_action.setEnabled(not series.committed)

        reset_action = menu.addAction("Reset boxes")
        reset_action.setEnabled(bool(series.boxes))

        chosen = menu.exec(self.tree.viewport().mapToGlobal(position))
        if chosen is None:
            return
        if chosen is clean_action:
            self._set_series_clean(series)
        elif chosen is commit_action:
            self._commit(series)
        elif chosen is reset_action:
            self._reset_boxes(series)

    def _set_series_clean(self, series: Series) -> None:
        if series.boxes:
            answer = QMessageBox.question(
                self,
                "Discard redaction boxes?",
                f"Setting this series back to Clean also discards "
                f"{len(series.boxes)} redaction box(es).\n\nContinue?",
            )
            if answer != QMessageBox.Yes:
                log.info(
                    "Set-to-clean cancelled for series %s",
                    series.series_description or series.series_uid,
                )
                return
        previous = series.status
        series.set_clean()
        log.info(
            "Series %s status reset to clean (was %s)",
            series.series_description or series.series_uid,
            previous,
        )
        if series is self.current_series:
            # Deselect it: nothing is on screen being reviewed, so a later click
            # on the same series re-triggers the automatic 'reviewed' status.
            self.tree.clearSelection()
            self.tree.setCurrentItem(None)
            self._clear_selection_view()
        self._refresh_item(series)
        self._update_export_button()

    def _clear_selection_view(self) -> None:
        """Return the review tab to its empty state without touching the model."""
        self._close_load_dialog()
        self.current_series = None
        self._frames = []
        self._render_epoch += 1
        self._render_target = None
        self._last_position = None
        self._metadata_timer.stop()
        self._pending_metadata = None
        self._metadata_path = None
        self.metadata_pane.clear()
        self.canvas.set_image(None)
        self.canvas.set_boxes([])
        self.canvas.set_editable(False)
        self.canvas.set_placeholder("Select a series in the tree to display it.")
        self.header_label.setText("No series selected")
        self.slider.blockSignals(True)
        self.slider.setMaximum(0)
        self.slider.setValue(0)
        self.slider.blockSignals(False)
        self.slider.setEnabled(False)
        self.frame_label.setText("- / -")
        self.reset_button.setEnabled(False)
        self.commit_button.setEnabled(False)
        self.boxes_label.setText("0 box(es)")

    # -- series display --------------------------------------------------
    def _select_series(self, series: Series) -> None:
        self.current_series = series
        self._render_epoch += 1
        log.info(
            "Series selected: %s (%d image(s))",
            series.series_description or series.series_uid,
            len(series.instances),
        )

        # Selecting a series counts as reviewing it.
        if not series.reviewed:
            series.reviewed = True
            log.info(
                "Series %s marked as reviewed",
                series.series_description or series.series_uid,
            )
            self._refresh_item(series)
            self._update_export_button()

        self._last_position = None
        # Called directly, not through a queued signal: a signal would be
        # delivered *behind* the stale prefetch requests it is meant to cancel.
        # Assigning an int is atomic, so the worker thread sees it at once.
        self._prefetch_worker.set_epoch(self._render_epoch)

        self._open_load_dialog(series)
        self._frames = self._scan_series_frames(series)

        self.slider.blockSignals(True)
        self.slider.setMinimum(0)
        self.slider.setMaximum(max(0, len(self._frames) - 1))
        self.slider.setValue(0)
        self.slider.setEnabled(len(self._frames) > 1)
        self.slider.blockSignals(False)

        self.canvas.set_editable(True)
        self.canvas.set_boxes(series.boxes)
        self.reset_button.setEnabled(True)
        self.commit_button.setEnabled(True)
        self._update_boxes_label()
        if not self._frames:
            self._close_load_dialog()
            return
        self._update_load_dialog("Decoding first image...")
        self._request_frame(0)

    # -- loading popup ---------------------------------------------------
    def _open_load_dialog(self, series: Series) -> None:
        """Arm the modal load popup for `series`.

        The dialog is created hidden and only shown if the load is still
        running 300 ms later, so a small series never flashes a window. It is
        never `exec()`ed: it stays up across the async first-frame decode
        without blocking the event loop, and `_close_load_dialog` retires it.
        """
        self._close_load_dialog()
        dialog = QProgressDialog(
            f"Loading {series.label()}...", "", 0, max(1, len(series.instances)), self
        )
        dialog.setWindowTitle("Loading series")
        dialog.setCancelButton(None)
        dialog.setWindowModality(Qt.ApplicationModal)
        # A huge minimum duration disables Qt's own auto-show heuristic, which
        # is what failed before: with the GUI thread pinned in the header loop
        # it only fired near the end. The QTimer below shows the dialog instead.
        dialog.setMinimumDuration(24 * 60 * 60 * 1000)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setMinimumWidth(420)
        self._load_dialog = dialog
        QTimer.singleShot(300, self._show_load_dialog)

    def _show_load_dialog(self) -> None:
        dialog = self._load_dialog
        if dialog is None:
            return
        dialog.show()
        dialog.raise_()
        QCoreApplication.processEvents()

    def _update_load_dialog(self, text: str, value: int | None = None) -> None:
        dialog = self._load_dialog
        if dialog is None:
            return
        dialog.setLabelText(text)
        if value is None:
            dialog.setRange(0, 0)  # indeterminate
        else:
            dialog.setValue(value)
        QCoreApplication.processEvents()

    def _close_load_dialog(self) -> None:
        dialog = self._load_dialog
        self._load_dialog = None
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()

    def _scan_series_frames(self, series: Series) -> list[tuple[int, int]]:
        """Build the (instance_index, frame_index) list for `series`.

        Reading every header is the first half of the stall the user sees after
        clicking a series, so it drives the load popup as it goes. The second
        half - decoding the first frame - happens on the worker thread after
        this returns, which is why the popup outlives this call.
        """
        total = len(series.instances)
        frames: list[tuple[int, int]] = []
        started = time.perf_counter()
        for index, instance in enumerate(series.instances):
            self._update_load_dialog(
                f"Reading headers...\n{instance.path.name}  ({index + 1} of {total})",
                index,
            )
            try:
                ds = read_dataset_header(instance.path)
                count = frame_count(ds)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not read %s: %s", instance.path, exc)
                count = 1
            frames.extend((index, f) for f in range(count))
        log.info(
            "Header scan of %d instance(s) took %.2fs (%d frame(s))",
            total,
            time.perf_counter() - started,
            len(frames),
        )
        return frames

    def _request_frame(self, position: int) -> None:
        """Ask the background worker to decode `position`.

        Only one render is ever in flight; a request that arrives while one
        is running just replaces `_render_pending` so a fast scrub skips
        straight to wherever the user settles, instead of blocking the GUI
        thread or queuing up every intermediate frame.
        """
        series = self.current_series
        if series is None or not self._frames:
            return
        position = max(0, min(position, len(self._frames) - 1))
        self._render_target = position
        instance_index, frame_index = self._frames[position]
        instance = series.instances[instance_index]

        # Already rendered (by an earlier visit or by the prefetcher): paint it
        # now rather than queueing behind whatever the worker is chewing on.
        cached = self._frame_cache.get(instance.path, frame_index)
        if cached is not None:
            kind, payload, ds = cached
            self._apply_frame_result(
                FrameRenderResult(
                    self._render_epoch, position, instance, frame_index,
                    ds, kind, payload,
                )
            )
            return

        request = FrameRenderRequest(self._render_epoch, position, instance, frame_index)
        if self._render_inflight:
            self._render_pending = request
            return
        self._render_inflight = True
        self.renderRequested.emit(request)

    @Slot(object)
    def _on_frame_rendered(self, result: FrameRenderResult) -> None:
        self._render_inflight = False
        stale = self._render_target is not None and result.position != self._render_target
        if result.epoch == self._render_epoch and not stale:
            # Whatever came back - image, report, no-pixel or error - the load
            # this popup was covering is over. A stale epoch means a newer
            # selection owns the current dialog, so leave it alone.
            self._close_load_dialog()
            self._apply_frame_result(result)
        if self._render_pending is not None:
            pending = self._render_pending
            self._render_pending = None
            self._render_inflight = True
            self.renderRequested.emit(pending)

    def _apply_frame_result(self, result: FrameRenderResult) -> None:
        series = self.current_series
        if series is None:
            return
        instance = result.instance
        if result.kind == "error":
            log.exception("Failed to render %s: %s", instance.path, result.payload)
            self.canvas.set_image(None)
            self.canvas.set_placeholder(
                f"Could not render {instance.path.name}:\n{result.payload}\n"
                "(the pixel data may need pylibjpeg / gdcm)"
            )
            self.view_stack.setCurrentWidget(self.canvas)
        else:
            self._show_metadata(series, instance, result.ds)
            if result.kind == "report":
                self.report_view.setHtml(result.payload)
                self.view_stack.setCurrentWidget(self.report_view)
            elif result.kind == "no_pixel":
                sop_uid = getattr(result.ds, "SOPClassUID", None)
                sop_name = sop_uid.name if sop_uid is not None else "Unknown SOP Class"
                log.info("No pixel data for %s (%s)", instance.path, sop_name)
                self.canvas.set_image(None)
                self.canvas.set_placeholder(
                    f"Uneditable File : No Pixel Data for image {instance.path.name}\n "
                    f"of image type '{sop_name}'"
                )
                self.view_stack.setCurrentWidget(self.canvas)
            else:
                started = time.perf_counter()
                self.canvas.set_image(result.payload)
                self.view_stack.setCurrentWidget(self.canvas)
                log.debug(
                    "Displayed %s in %.0f ms",
                    instance.path.name,
                    (time.perf_counter() - started) * 1000.0,
                )

        self.frame_label.setText(f"{result.position + 1} / {len(self._frames)}")
        self.header_label.setText(
            f"{series.patient_name} - {series.label()}   |   {instance.path.name}"
            + (f"  (frame {result.frame_index + 1})" if frame_count_safe(result.ds) > 1 else "")
        )
        self._prefetch_neighbors(result.position)

    def _prefetch_neighbors(self, position: int) -> None:
        """Render the frames the user is heading towards into the caches.

        Direction is inferred from the previous position, so a steady scroll
        warms what is coming rather than what has just been passed. Purely
        best-effort: a stale request (series changed before the prefetch
        thread got to it) is dropped by the worker's epoch check, and a
        request that never lands only costs one on-demand decode later.
        """
        series = self.current_series
        if series is None or not self._frames:
            return
        previous = self._last_position
        self._last_position = position
        forward = previous is None or position >= previous
        # Where the user actually is, so the worker can drop requests that a
        # fast scrub has already left behind.
        self._prefetch_worker.set_focus(position)

        offsets = [o for o in range(1, PREFETCH_AHEAD + 1)]
        offsets += [-o for o in range(1, PREFETCH_BEHIND + 1)]
        if not forward:
            offsets = [-o for o in offsets]

        for offset in offsets:
            neighbor = position + offset
            if not (0 <= neighbor < len(self._frames)):
                continue
            instance_index, frame_index = self._frames[neighbor]
            instance = series.instances[instance_index]
            if self._frame_cache.has(instance.path, frame_index):
                continue
            self.prefetchRequested.emit(
                PrefetchRequest(self._render_epoch, instance, frame_index, neighbor)
            )

    def _show_metadata(self, series: Series, instance, ds) -> None:
        """Note which instance the Metadata tab owes, and rebuild it lazily.

        Building the tag tree walks (and re-encodes) every element, which is
        far too much to do per frame on the GUI thread while someone scrolls.
        The debounce means it happens once, when the scroll settles, instead of
        once per image; switching to that tab flushes it immediately.
        """
        if self._metadata_path == instance.path:
            return
        self._pending_metadata = (series, instance, ds)
        self._metadata_timer.start()

    def _flush_metadata(self) -> None:
        """Rebuild the Metadata tab from whatever _show_metadata last noted."""
        pending = self._pending_metadata
        if pending is None:
            return
        series, instance, ds = pending
        if self._metadata_path == instance.path:
            return
        self._metadata_path = instance.path
        started = time.perf_counter()
        try:
            self.metadata_pane.show_dataset(
                ds, f"{series.label()}   |   {instance.path.name}"
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not show metadata for %s: %s", instance.path, exc)
        log.debug(
            "Metadata tree for %s built in %.0f ms",
            instance.path.name,
            (time.perf_counter() - started) * 1000.0,
        )

    @Slot(int)
    def _on_tab_changed(self, _index: int) -> None:
        """Don't make someone who just opened the Metadata tab wait out the
        debounce - build it now."""
        if self.tabs.currentWidget() is self.metadata_pane:
            self._metadata_timer.stop()
            self._flush_metadata()

    @Slot(int)
    def _on_slider_changed(self, value: int) -> None:
        if self._frames:
            self.frame_label.setText(f"{value + 1} / {len(self._frames)}")
        self._request_frame(value)

    def eventFilter(self, obj, event):
        """Give the frame slider the same wheel direction as the image.

        Wheel up moves toward the start of the series in both places, which
        means swallowing the slider's built-in handling (it runs the other
        way for a horizontal slider) and stepping it ourselves.
        """
        if obj is self.slider and event.type() == QEvent.Wheel:
            delta = event.angleDelta().y() or event.angleDelta().x()
            steps = self._slider_wheel.steps(delta) if delta else 0
            if steps:
                self._step_frame(-steps)
            return True
        return super().eventFilter(obj, event)

    @Slot(int)
    def _step_frame(self, step: int) -> None:
        """Mouse-wheel scrolling over the image: same path as moving the slider."""
        if not self._frames or len(self._frames) < 2:
            return
        target = self.slider.value() + step
        target = max(0, min(target, self.slider.maximum()))
        if target != self.slider.value():
            self.slider.setValue(target)  # fires _on_slider_changed -> _request_frame

    # -- boxes -----------------------------------------------------------
    @Slot(float, float, float, float)
    def _on_box_added(self, x: float, y: float, w: float, h: float) -> None:
        series = self.current_series
        if series is None:
            return
        series.boxes.append((x, y, w, h))
        if series.committed:
            series.committed = False
            log.warning(
                "Series %s was already committed; a new box re-opens it for review",
                series.series_description or series.series_uid,
            )
        log.info(
            "Series %s now has %d redaction box(es)",
            series.series_description or series.series_uid,
            len(series.boxes),
        )
        self.canvas.set_boxes(series.boxes)
        self._refresh_item(series)
        self._update_boxes_label()
        self._update_export_button()

    @Slot()
    def _on_box_discarded(self) -> None:
        self.statusBar().showMessage("Selection discarded (released outside the image)", 4000)

    def reset_series_boxes(self) -> None:
        if self.current_series is not None:
            self._reset_boxes(self.current_series)

    def _reset_boxes(self, series: Series) -> None:
        count = len(series.boxes)
        series.boxes.clear()
        series.committed = False  # reviewed is kept: they have still seen the images
        log.info(
            "Reset %d redaction box(es) on series %s",
            count,
            series.series_description or series.series_uid,
        )
        if series is self.current_series:
            self.canvas.set_boxes(series.boxes)
            self._update_boxes_label()
        self._refresh_item(series)
        self._update_export_button()

    def commit_series(self) -> None:
        """Commit the displayed series. Also the Cmd+S / Ctrl+S handler."""
        series = self.current_series
        if series is None:
            self.statusBar().showMessage(
                "Nothing to commit - select a series in the tree first", 4000
            )
            log.debug("Commit requested with no series selected")
            return
        if series.committed:
            self.statusBar().showMessage(
                f"{series.label()} is already committed", 4000
            )
            return
        self._commit(series)
        self.statusBar().showMessage(
            f"Committed {series.label()} ({len(series.boxes)} box(es))", 5000
        )

    def _commit(self, series: Series) -> None:
        series.reviewed = True
        series.committed = True
        log.info(
            "Committed series %s with %d redaction box(es)",
            series.series_description or series.series_uid,
            len(series.boxes),
        )
        self._refresh_item(series)
        if series is self.current_series:
            self._update_boxes_label()
        self._update_export_button()

    def _update_boxes_label(self) -> None:
        series = self.current_series
        if series is None:
            self.boxes_label.setText("0 box(es)")
            return
        self.boxes_label.setText(
            f"{len(series.boxes)} box(es) - {STATUS_TEXT[series.status]}"
        )

    def _update_export_button(self) -> None:
        self.export_button.setEnabled(bool(self.series_map))
        remaining = sum(1 for s in self.series_map.values() if not s.export_ready)
        if not self.series_map or remaining == 0:
            self.export_button.setText("Export de-identified files...")
        else:
            self.export_button.setText(
                f"Export de-identified files... ({remaining} series not ready)"
            )

    # -- export ----------------------------------------------------------
    def export(self) -> None:
        if not self.series_map or self.input_root is None:
            return
        not_ready = [s for s in self.series_map.values() if not s.export_ready]
        if not_ready:
            names = "\n".join(
                f"  - {s.label()}  [{STATUS_TEXT[s.status]}]" for s in not_ready[:15]
            ) + ("\n  ..." if len(not_ready) > 15 else "")
            log.warning(
                "Export blocked: %d series are neither reviewed nor committed",
                len(not_ready),
            )
            QMessageBox.warning(
                self,
                "Series not ready",
                f"{len(not_ready)} series must be reviewed or committed before export.\n"
                "A series with redaction boxes must be committed.\n\n"
                f"{names}",
            )
            return

        output = QFileDialog.getExistingDirectory(
            self, "Select the output directory", str(Path.home())
        )
        if not output:
            log.info("Export cancelled: no output directory selected")
            return
        output_root = Path(output)
        try:
            if output_root == self.input_root or self.input_root in output_root.parents:
                QMessageBox.warning(
                    self,
                    "Invalid output directory",
                    "The output directory must not be the input directory or inside it.",
                )
                log.error("Export blocked: output %s is inside input %s", output_root, self.input_root)
                return
        except Exception:  # noqa: BLE001
            pass

        self.export_button.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.statusBar().showMessage("Exporting...")

        self._export_thread, self._export_worker = start_export(
            list(self.series_map.values()),
            self.input_root,
            output_root,
            self._on_export_progress,
            self._on_export_finished,
            self._on_export_failed,
        )
        self._export_thread.start()

    @Slot(int, int, str)
    def _on_export_progress(self, done: int, total: int, name: str) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.statusBar().showMessage(f"Exporting {done}/{total}: {name}")

    @Slot(int, int, int)
    def _on_export_finished(self, written: int, redacted: int, errors: int) -> None:
        self.progress.setVisible(False)
        self.export_button.setEnabled(True)
        self.statusBar().showMessage(
            f"Export complete: {written} written, {redacted} redacted, {errors} error(s)"
        )
        if errors:
            QMessageBox.warning(
                self,
                "Export finished with errors",
                f"{written} file(s) written, {redacted} redacted, {errors} failed.\n"
                "See the Log tab for details.",
            )
        else:
            QMessageBox.information(
                self,
                "Export complete",
                f"{written} file(s) written ({redacted} redacted).",
            )

    @Slot(str)
    def _on_export_failed(self, message: str) -> None:
        self.progress.setVisible(False)
        self.export_button.setEnabled(True)
        self.statusBar().showMessage("Export failed")
        QMessageBox.critical(self, "Export failed", message)

    # -- lifecycle -------------------------------------------------------
    def closeEvent(self, event) -> None:
        self._close_load_dialog()
        for worker, thread in (
            (self._scan_worker, self._scan_thread),
            (self._export_worker, self._export_thread),
        ):
            if thread is not None and thread.isRunning():
                if worker is not None:
                    worker.cancel()
                thread.quit()
                thread.wait(3000)
        if self._frame_thread.isRunning():
            self._frame_thread.quit()
            self._frame_thread.wait(3000)
        if self._prefetch_thread.isRunning():
            self._prefetch_thread.quit()
            self._prefetch_thread.wait(3000)
        log.info("Application closing")
        super().closeEvent(event)


def frame_count_safe(ds) -> int:
    try:
        return frame_count(ds)
    except Exception:  # noqa: BLE001
        return 1
