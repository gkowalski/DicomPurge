"""Metadata tab: the DICOM tags of the instance currently on screen.

Shows Field Name / Tag / VR / Size / Content in a tree, with sequences and
multi-valued elements expandable, mirroring a DICOM dump viewer. Nothing here
modifies the dataset - it is a read-only view.
"""
from __future__ import annotations

import logging

from pydicom.filebase import DicomBytesIO
from pydicom.filewriter import writers
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)

# Values longer than this are elided in the Content column.
MAX_CONTENT_CHARS = 300
# Binary VRs whose content is never rendered as text.
BINARY_VRS = {"OB", "OW", "OF", "OD", "OL", "OV", "UN"}

GROUP_META_COLOR = QColor("#7f8c8d")


def human_size(num_bytes: int | None) -> str:
    if num_bytes is None:
        return ""
    value = float(num_bytes)
    if value < 1024:
        return f"{int(value)} B"
    for unit in ("KB", "MB", "GB"):
        value /= 1024.0
        if value < 1024 or unit == "GB":
            return f"{value:.2f} {unit}"
    return f"{value:.2f} GB"


def _element_length(elem) -> int | None:
    """Byte length of the element's value, best effort.

    Raw (unparsed) elements carry their on-disk length. Parsed DataElements do
    not in pydicom 3, so the value is re-encoded with the same writer the file
    writer uses - that is the only way to get the real padded byte count.
    """
    length = getattr(elem, "length", None)
    if isinstance(length, int) and 0 <= length < 0xFFFFFFFF:
        return length

    try:
        value = elem.value
    except Exception:  # noqa: BLE001
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(bytes(value))

    vr = str(getattr(elem, "VR", "") or "")
    if vr == "SQ" or vr in BINARY_VRS:
        return None
    try:
        entry = writers[vr]
        writer, param = entry if isinstance(entry, tuple) else (entry, None)
        fp = DicomBytesIO()
        fp.is_little_endian = True
        fp.is_implicit_VR = False
        if param is not None:
            writer(fp, elem, param)
        else:
            writer(fp, elem)
        return len(fp.getvalue())
    except Exception as exc:  # noqa: BLE001
        log.debug("Could not size %s: %s", getattr(elem, "tag", "?"), exc)
        return None


def _value_list(elem) -> list[str] | None:
    """The element's individual values when VM > 1, else None.

    VM is the only safe test: PersonName and str are iterable but single-valued,
    so iterating them would split a name into characters.
    """
    vr = str(getattr(elem, "VR", "") or "")
    if vr == "SQ" or vr in BINARY_VRS:
        return None
    try:
        if int(getattr(elem, "VM", 1) or 1) < 2:
            return None
        return [str(v) for v in elem.value]
    except Exception:  # noqa: BLE001
        return None


def _format_value(elem) -> str:
    """The Content column for a non-sequence element."""
    vr = str(getattr(elem, "VR", "") or "")
    if vr in BINARY_VRS:
        size = human_size(_element_length(elem))
        return f"<binary data, {size}>" if size else "<binary data>"
    try:
        value = elem.value
    except Exception as exc:  # noqa: BLE001
        return f"<unreadable: {exc}>"
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary data, {human_size(len(bytes(value)))}>"

    values = _value_list(elem)
    text = "\\".join(values) if values else str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    if len(text) > MAX_CONTENT_CHARS:
        text = text[:MAX_CONTENT_CHARS] + " ..."
    return text


def _multi_values(elem) -> list[str]:
    """Individual values for a VM > 1 element, else an empty list."""
    return _value_list(elem) or []


class MetadataPane(QWidget):
    """Read-only tag view for one dataset."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dataset = None
        self._title = ""

        controls = QHBoxLayout()
        self.header_label = QLabel("No series selected")
        self.header_label.setStyleSheet("font-weight: 600;")
        controls.addWidget(self.header_label, 1)

        controls.addWidget(QLabel("Filter:"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("field name, tag or content...")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setMaximumWidth(280)
        self.filter_edit.textChanged.connect(self._apply_filter)
        controls.addWidget(self.filter_edit)

        self.file_meta_box = QCheckBox("File meta")
        self.file_meta_box.setToolTip("Show the (0002,xxxx) file meta information group")
        self.file_meta_box.setChecked(True)
        self.file_meta_box.toggled.connect(self._repopulate)
        controls.addWidget(self.file_meta_box)

        self.expand_button = QPushButton("Expand all")
        self.expand_button.clicked.connect(lambda: self.tree.expandAll())
        controls.addWidget(self.expand_button)

        self.collapse_button = QPushButton("Collapse all")
        self.collapse_button.clicked.connect(self._collapse)
        controls.addWidget(self.collapse_button)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Field Name", "Tag", "VR", "Size", "Content"])
        self.tree.setColumnWidth(0, 320)
        self.tree.setColumnWidth(1, 100)
        self.tree.setColumnWidth(2, 50)
        self.tree.setColumnWidth(3, 90)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionBehavior(QTreeWidget.SelectRows)
        self.tree.setTextElideMode(Qt.ElideRight)
        font = self.tree.font()
        font.setStyleHint(font.StyleHint.Monospace)
        self.tree.setFont(font)

        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(self.tree, 1)

    # -- public API ------------------------------------------------------
    def show_dataset(self, dataset, title: str = "") -> None:
        """Display `dataset`. Passing None clears the pane."""
        self._dataset = dataset
        self._title = title
        self._repopulate()

    def clear(self) -> None:
        self.show_dataset(None, "")

    # -- population ------------------------------------------------------
    def _repopulate(self) -> None:
        self.tree.clear()
        ds = self._dataset
        if ds is None:
            self.header_label.setText("No series selected")
            return
        self.header_label.setText(self._title or "DICOM metadata")

        try:
            root = QTreeWidgetItem(self.tree, ["DICOMObject", "", "", self._object_size(ds), ""])
            root.setExpanded(True)

            if self.file_meta_box.isChecked():
                file_meta = getattr(ds, "file_meta", None)
                if file_meta is not None:
                    for elem in file_meta:
                        self._add_element(root, elem, meta=True)

            for elem in ds:
                self._add_element(root, elem)
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not build the metadata view: %s", exc)
            QTreeWidgetItem(self.tree, [f"<could not read tags: {exc}>", "", "", "", ""])
            return

        self.tree.expandItem(self.tree.topLevelItem(0))
        self._apply_filter(self.filter_edit.text())

    def _object_size(self, ds) -> str:
        path = getattr(ds, "filename", None)
        if path:
            try:
                from pathlib import Path

                return human_size(Path(path).stat().st_size)
            except Exception:  # noqa: BLE001
                pass
        return ""

    def _add_element(self, parent: QTreeWidgetItem, elem, meta: bool = False) -> None:
        vr = str(getattr(elem, "VR", "") or "")
        name = str(getattr(elem, "name", "") or getattr(elem, "keyword", "") or "")
        tag = getattr(elem, "tag", None)
        tag_text = f"{tag.group:04X},{tag.element:04X}" if tag is not None else ""
        size_text = human_size(_element_length(elem))

        if vr == "SQ":
            item = QTreeWidgetItem(parent, [name, tag_text, vr, size_text, ""])
            try:
                sequence = list(elem.value or [])
            except Exception:  # noqa: BLE001
                sequence = []
            item.setText(4, f"<sequence, {len(sequence)} item(s)>")
            for index, item_ds in enumerate(sequence, start=1):
                item_node = QTreeWidgetItem(item, [f"Item {index}", "", "", "", ""])
                for sub in item_ds:
                    self._add_element(item_node, sub)
        else:
            item = QTreeWidgetItem(
                parent, [name, tag_text, vr, size_text, _format_value(elem)]
            )
            for value in _multi_values(elem):
                QTreeWidgetItem(item, ["value", "", "", "", value])

        if meta:
            brush = QBrush(GROUP_META_COLOR)
            for column in range(5):
                item.setForeground(column, brush)

    # -- filtering -------------------------------------------------------
    def _collapse(self) -> None:
        self.tree.collapseAll()
        root = self.tree.topLevelItem(0)
        if root is not None:
            root.setExpanded(True)

    def _apply_filter(self, text: str) -> None:
        needle = (text or "").strip().lower()
        root = self.tree.topLevelItem(0)
        if root is None:
            return
        if not needle:
            self._set_visible(root, True)
            root.setExpanded(True)
            return
        self._filter_item(root, needle)
        root.setHidden(False)
        root.setExpanded(True)

    def _set_visible(self, item: QTreeWidgetItem, visible: bool) -> None:
        item.setHidden(not visible)
        for index in range(item.childCount()):
            self._set_visible(item.child(index), visible)

    def _filter_item(self, item: QTreeWidgetItem, needle: str) -> bool:
        """Hide items that neither match nor contain a match. Returns True if kept."""
        match = any(needle in item.text(column).lower() for column in (0, 1, 4))
        keep_child = False
        for index in range(item.childCount()):
            child = item.child(index)
            if self._filter_item(child, needle):
                keep_child = True
        keep = match or keep_child
        item.setHidden(not keep)
        if keep_child:
            item.setExpanded(True)
        return keep
