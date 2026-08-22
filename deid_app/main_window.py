"""Main window: series tree on the left, image review + log tabs on the right."""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, Slot
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QMenu,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QMainWindow,
)

from .export import start_export
from .image_view import ImageCanvas
from .log_pane import LogPane
from .logging_setup import LogBridge
from .metadata_pane import MetadataPane
from .model import Series, start_scan
from .resources import LOGO_PATH, app_icon, logo_pixmap
from .render import dataset_to_qimage, frame_count, read_dataset

log = logging.getLogger(__name__)

SERIES_UID_ROLE = Qt.UserRole + 1
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
        self._cache_path: Path | None = None
        self._cache_ds = None
        # Path whose tags the Metadata tab is currently showing.
        self._metadata_path: Path | None = None

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
        splitter.addWidget(self.tabs)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([420, 980])
        outer.addWidget(splitter, 1)

        self.setCentralWidget(central)

        status = QStatusBar()
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(240)
        self.progress.setVisible(False)
        status.addPermanentWidget(self.progress)
        self.setStatusBar(status)
        status.showMessage("Idle")

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
        layout.addWidget(self.canvas, 1)

        slider_row = QHBoxLayout()
        slider_row.addWidget(QLabel("Image:"))
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setEnabled(False)
        self.slider.setMinimum(0)
        self.slider.setMaximum(0)
        self.slider.valueChanged.connect(self._on_slider_changed)
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
        self.series_map = {}
        self.series_items = {}
        self.current_series = None
        self._frames = []
        self._cache_path = None
        self._cache_ds = None
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
        self.current_series = None
        self._frames = []
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

        self._frames = []
        for index, instance in enumerate(series.instances):
            try:
                ds = self._dataset_for(instance.path)
                frames = frame_count(ds)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not read %s: %s", instance.path, exc)
                frames = 1
            self._frames.extend((index, f) for f in range(frames))

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
        self._show_frame(0)

    def _dataset_for(self, path: Path):
        if self._cache_path == path and self._cache_ds is not None:
            return self._cache_ds
        ds = read_dataset(path)
        self._cache_path = path
        self._cache_ds = ds
        return ds

    def _show_frame(self, position: int) -> None:
        series = self.current_series
        if series is None or not self._frames:
            return
        position = max(0, min(position, len(self._frames) - 1))
        instance_index, frame_index = self._frames[position]
        instance = series.instances[instance_index]
        try:
            ds = self._dataset_for(instance.path)
            self._show_metadata(series, instance, ds)
            image = dataset_to_qimage(ds, frame_index)
            self.canvas.set_image(image)
        except Exception as exc:  # noqa: BLE001
            log.exception("Failed to render %s: %s", instance.path, exc)
            self.canvas.set_image(None)
            self.canvas.set_placeholder(
                f"Could not render {instance.path.name}:\n{exc}\n"
                "(the pixel data may need pylibjpeg / gdcm)"
            )

        self.frame_label.setText(f"{position + 1} / {len(self._frames)}")
        self.header_label.setText(
            f"{series.patient_name} - {series.label()}   |   {instance.path.name}"
            + (f"  (frame {frame_index + 1})" if frame_count_safe(self._cache_ds) > 1 else "")
        )

    def _show_metadata(self, series: Series, instance, ds) -> None:
        """Refresh the Metadata tab, but only when the instance actually changed."""
        if self._metadata_path == instance.path:
            return
        self._metadata_path = instance.path
        try:
            self.metadata_pane.show_dataset(
                ds, f"{series.label()}   |   {instance.path.name}"
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not show metadata for %s: %s", instance.path, exc)

    @Slot(int)
    def _on_slider_changed(self, value: int) -> None:
        self._show_frame(value)

    @Slot(int)
    def _step_frame(self, step: int) -> None:
        """Mouse-wheel scrolling over the image: same path as moving the slider."""
        if not self._frames or len(self._frames) < 2:
            return
        target = self.slider.value() + step
        target = max(0, min(target, self.slider.maximum()))
        if target != self.slider.value():
            self.slider.setValue(target)  # fires _on_slider_changed -> _show_frame

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
        if self.current_series is not None:
            self._commit(self.current_series)

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
        for worker, thread in (
            (self._scan_worker, self._scan_thread),
            (self._export_worker, self._export_thread),
        ):
            if thread is not None and thread.isRunning():
                if worker is not None:
                    worker.cancel()
                thread.quit()
                thread.wait(3000)
        log.info("Application closing")
        super().closeEvent(event)


def frame_count_safe(ds) -> int:
    try:
        return frame_count(ds)
    except Exception:  # noqa: BLE001
        return 1
