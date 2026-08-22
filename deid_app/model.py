"""Scanning of an input tree and the in-memory series model."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import pydicom
from PySide6.QtCore import QObject, QThread, Signal

log = logging.getLogger(__name__)

# A redaction box, stored as fractions (0..1) of the series' reference image so
# that it survives differing instance sizes and any display scaling.
Box = tuple[float, float, float, float]  # x, y, w, h


@dataclass
class Instance:
    path: Path
    instance_number: int
    sop_uid: str
    rows: int
    columns: int

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

    @property
    def rows(self) -> int:
        return self.instances[0].rows if self.instances else 0

    @property
    def columns(self) -> int:
        return self.instances[0].columns if self.instances else 0

    @property
    def status(self) -> str:
        """One of 'clean', 'reviewed', 'pending' or 'committed'.

        'reviewed' is set automatically the first time the user selects the
        series (they have looked at the images). 'pending' means boxes have been
        placed but not committed, and always outranks 'reviewed'.
        """
        if self.committed:
            return "committed"
        if self.boxes:
            return "pending"
        if self.reviewed:
            return "reviewed"
        return "clean"

    @property
    def export_ready(self) -> bool:
        """A series may be exported once it is reviewed or committed."""
        return self.status in ("reviewed", "committed")

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

                    series.instances.append(
                        Instance(
                            path=path,
                            instance_number=int(_get(ds, "InstanceNumber", 0) or 0),
                            sop_uid=str(_get(ds, "SOPInstanceUID", path.name)),
                            rows=int(_get(ds, "Rows", 0) or 0),
                            columns=int(_get(ds, "Columns", 0) or 0),
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
