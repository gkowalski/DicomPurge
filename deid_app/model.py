"""Scanning of an input tree and the in-memory series model."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import pydicom
from PySide6.QtCore import QObject, QThread, Signal

from .sr_render import SR_SOP_CLASS_UIDS

log = logging.getLogger(__name__)

# A redaction box, stored as fractions (0..1) of the series' reference image so
# that it survives differing instance sizes and any display scaling.
Box = tuple[float, float, float, float]  # x, y, w, h

# Tag (0042,0011) EncapsulatedDocument - a whole document (usually a PDF)
# carried inside the instance, which redaction boxes never touch.
ENCAPSULATED_DOCUMENT_TAG = 0x00420011

# SOP Classes whose ENTIRE payload is that document: no pixel data exists, so
# there is nothing for the box editor to redact (PS3.6 Annex A).
ENCAPSULATED_DOCUMENT_SOP_CLASS_UIDS = {
    "1.2.840.10008.5.1.4.1.1.104.1",  # Encapsulated PDF Storage
    "1.2.840.10008.5.1.4.1.1.104.2",  # Encapsulated CDA Storage
    "1.2.840.10008.5.1.4.1.1.104.3",  # Encapsulated STL Storage
    "1.2.840.10008.5.1.4.1.1.104.4",  # Encapsulated OBJ Storage
    "1.2.840.10008.5.1.4.1.1.104.5",  # Encapsulated MTL Storage
}


@dataclass
class Instance:
    path: Path
    instance_number: int
    sop_uid: str
    rows: int
    columns: int
    # Carries (0042,0011): an embedded document that redaction cannot reach.
    has_embedded_document: bool = False
    # ...and that document is the whole payload, so there are no pixels at all.
    is_document_only: bool = False
    # A Structured Report: text in a ContentSequence, no pixels for boxes to hit.
    is_structured_report: bool = False

    def sort_key(self) -> tuple:
        return (self.instance_number, self.path.name)


@dataclass
class Series:
    series_uid: str
    patient_id: str
    patient_name: str
    study_uid: str
    study_description: str
    series_number: int
    series_description: str
    modality: str
    instances: list[Instance] = field(default_factory=list)
    boxes: list[Box] = field(default_factory=list)
    reviewed: bool = False
    committed: bool = False
    # Withheld from export by the current settings. Owned by MainWindow, which
    # is the only place that knows what those settings are.
    skipped: bool = False

    @property
    def is_structured_report(self) -> bool:
        return any(i.is_structured_report for i in self.instances)

    @property
    def has_document_only(self) -> bool:
        return any(i.is_document_only for i in self.instances)

    @property
    def rows(self) -> int:
        return self.instances[0].rows if self.instances else 0

    @property
    def columns(self) -> int:
        return self.instances[0].columns if self.instances else 0

    @property
    def status(self) -> str:
        """One of 'skipped', 'clean', 'reviewed', 'pending' or 'committed'.

        'reviewed' is set automatically the first time the user selects the
        series (they have looked at the images). 'pending' means boxes have been
        placed but not committed, and always outranks 'reviewed'.

        'skipped' outranks everything: once a series is withheld from the export
        its review state is irrelevant, and showing 'committed' on a file that
        will never be written would be actively misleading.
        """
        if self.skipped:
            return "skipped"
        if self.committed:
            return "committed"
        if self.boxes:
            return "pending"
        if self.reviewed:
            return "reviewed"
        return "clean"

    @property
    def export_ready(self) -> bool:
        """Whether this series can stop blocking the export.

        A skipped series counts as ready: it is deliberately withheld, so it
        must not trip the not-ready gate the way an unreviewed image would.
        """
        return self.status in ("reviewed", "committed", "skipped")

    def set_clean(self) -> None:
        """Drop every box and both status flags."""
        self.boxes.clear()
        self.reviewed = False
        self.committed = False

    def label(self) -> str:
        desc = self.series_description or "(no description)"
        num = self.series_number if self.series_number is not None else "?"
        return f"Series {num}: {desc} [{self.modality}] - {len(self.instances)} image(s)"

    def sort_instances(self) -> None:
        self.instances.sort(key=Instance.sort_key)


def _get(ds, keyword, default=""):
    value = getattr(ds, keyword, default)
    if value is None:
        return default
    return value


class ScanWorker(QObject):
    """Recursively finds *.dcm files and groups them into series."""

    progress = Signal(int, int)          # scanned, total
    finished = Signal(object)            # dict[str, Series]
    failed = Signal(str)

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = Path(root)
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            log.info("Scanning %s for DICOM files (*.dcm)", self.root)
            candidates: list[Path] = []
            for dirpath, _dirnames, filenames in os.walk(self.root):
                for name in filenames:
                    if name.lower().endswith(".dcm"):
                        candidates.append(Path(dirpath) / name)
            candidates.sort()
            total = len(candidates)
            log.info("Found %d candidate file(s)", total)

            series_map: dict[str, Series] = {}
            unreadable = 0
            for index, path in enumerate(candidates, start=1):
                if self._cancelled:
                    log.warning("Scan cancelled by user")
                    self.finished.emit(series_map)
                    return
                try:
                    ds = pydicom.dcmread(
                        str(path), stop_before_pixels=True, force=False
                    )
                except Exception as exc:  # noqa: BLE001 - report and continue
                    unreadable += 1
                    log.warning("Skipping unreadable file %s: %s", path, exc)
                    self.progress.emit(index, total)
                    continue

                try:
                    uid = str(_get(ds, "SeriesInstanceUID", "")) or f"__no_uid__{path.parent}"
                    series = series_map.get(uid)
                    if series is None:
                        series = Series(
                            series_uid=uid,
                            patient_id=str(_get(ds, "PatientID", "(unknown id)")),
                            patient_name=str(_get(ds, "PatientName", "(unknown)")),
                            study_uid=str(_get(ds, "StudyInstanceUID", "(no study uid)")),
                            study_description=str(_get(ds, "StudyDescription", "")),
                            series_number=int(_get(ds, "SeriesNumber", 0) or 0),
                            series_description=str(_get(ds, "SeriesDescription", "")),
                            modality=str(_get(ds, "Modality", "??")),
                        )
                        series_map[uid] = series
                        log.debug("New series %s (%s)", uid, series.series_description)

                    # Classified from the header alone. Note that
                    # stop_before_pixels drops PixelData whether or not the file
                    # has any, so "document only" must come from the SOP Class,
                    # never from a has_pixel_data() check here.
                    has_doc = ENCAPSULATED_DOCUMENT_TAG in ds
                    sop_uid = str(_get(ds, "SOPClassUID", ""))
                    doc_only = sop_uid in ENCAPSULATED_DOCUMENT_SOP_CLASS_UIDS
                    # sr_render.is_structured_report() cannot be used here: its
                    # fallback calls has_pixel_data(), which is meaningless on a
                    # stop_before_pixels read. Absence of Rows stands in for it.
                    is_sr = sop_uid in SR_SOP_CLASS_UIDS or (
                        "ContentSequence" in ds and "Rows" not in ds
                    )
                    if is_sr:
                        log.info(
                            "%s is a Structured Report; redaction boxes cannot "
                            "de-identify it",
                            path.name,
                        )
                    if has_doc or doc_only:
                        log.info(
                            "%s carries an embedded document (%s)%s",
                            path.name,
                            _get(ds, "MIMETypeOfEncapsulatedDocument", "unknown type"),
                            " and has no pixel data" if doc_only else "",
                        )

                    series.instances.append(
                        Instance(
                            path=path,
                            instance_number=int(_get(ds, "InstanceNumber", 0) or 0),
                            sop_uid=str(_get(ds, "SOPInstanceUID", path.name)),
                            rows=int(_get(ds, "Rows", 0) or 0),
                            columns=int(_get(ds, "Columns", 0) or 0),
                            has_embedded_document=bool(has_doc or doc_only),
                            is_document_only=bool(doc_only),
                            is_structured_report=bool(is_sr),
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    unreadable += 1
                    log.exception("Failed to index %s: %s", path, exc)

                self.progress.emit(index, total)

            for series in series_map.values():
                series.sort_instances()

            log.info(
                "Scan complete: %d series, %d indexed file(s), %d skipped",
                len(series_map),
                sum(len(s.instances) for s in series_map.values()),
                unreadable,
            )
            self.finished.emit(series_map)
        except Exception as exc:  # noqa: BLE001
            log.exception("Scan failed: %s", exc)
            self.failed.emit(str(exc))


def start_scan(root: Path, on_progress, on_finished, on_failed) -> tuple[QThread, ScanWorker]:
    """Run a ScanWorker on its own QThread; returns (thread, worker)."""
    thread = QThread()
    worker = ScanWorker(root)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.progress.connect(on_progress)
    worker.finished.connect(on_finished)
    worker.failed.connect(on_failed)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    thread.finished.connect(worker.deleteLater)
    return thread, worker
