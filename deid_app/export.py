"""Background export: applies each series' boxes and mirrors the input tree."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from .redaction import redact_file

log = logging.getLogger(__name__)


class ExportWorker(QObject):
    progress = Signal(int, int, str)        # done, total, current file
    # written, redacted, errors, stripped, skipped
    finished = Signal(int, int, int, int, object)
    failed = Signal(str)

    def __init__(
        self,
        series_list,
        input_root: Path,
        output_root: Path,
        strip_documents: bool = True,
        skip_structured_reports: bool = False,
    ) -> None:
        super().__init__()
        self.series_list = list(series_list)
        self.input_root = Path(input_root)
        self.output_root = Path(output_root)
        self.strip_documents = bool(strip_documents)
        self.skip_structured_reports = bool(skip_structured_reports)
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True
        log.warning("Export cancellation requested")

    def run(self) -> None:
        written = redacted = errors = stripped = 0
        skipped: list[str] = []
        try:
            total = sum(len(s.instances) for s in self.series_list)
            log.info(
                "Export started: %d file(s) from %s -> %s",
                total,
                self.input_root,
                self.output_root,
            )
            done = 0
            for series in self.series_list:
                boxes = list(series.boxes)
                log.info(
                    "Exporting series %s (%s) with %d redaction box(es)",
                    series.series_uid,
                    series.series_description or "no description",
                    len(boxes),
                )
                for instance in series.instances:
                    if self._cancelled:
                        log.warning("Export cancelled after %d file(s)", done)
                        self.finished.emit(
                            written, redacted, errors, stripped, skipped
                        )
                        return
                    done += 1
                    src = instance.path
                    try:
                        relative = src.relative_to(self.input_root)
                    except ValueError:
                        relative = Path(src.name)
                        log.warning(
                            "%s is outside the input root; writing to the output root",
                            src,
                        )
                    dst = self.output_root / relative
                    self.progress.emit(done, total, str(relative))
                    try:
                        # A document-only object is entirely a PDF (or CDA, ...)
                        # with no pixel data, so there is nothing the box editor
                        # could ever redact. Exporting it would ship the report
                        # verbatim, so it is left behind and reported instead.
                        if self.strip_documents and instance.is_document_only:
                            skipped.append(str(relative))
                            log.warning(
                                "Skipped %s: contains only an embedded document "
                                "and cannot be de-identified",
                                relative,
                            )
                            continue

                        # A Structured Report is text in a ContentSequence, so
                        # there are no pixels for a redaction box to act on.
                        if self.skip_structured_reports and instance.is_structured_report:
                            skipped.append(str(relative))
                            log.warning(
                                "Skipped %s: contains only a Structured Report "
                                "and cannot be de-identified",
                                relative,
                            )
                            continue

                        dst.parent.mkdir(parents=True, exist_ok=True)
                        # The embedded document survives a byte-for-byte copy,
                        # so a file carrying one must be rewritten even when it
                        # has no boxes - that copy is the leak.
                        strip = self.strip_documents and instance.has_embedded_document
                        if boxes or strip:
                            summary = redact_file(src, dst, boxes, strip_documents=strip)
                            if boxes:
                                redacted += 1
                                log.info(
                                    "Redacted %s (%d frame(s)%s)",
                                    relative,
                                    summary.get("frames", 0),
                                    ", overlays " + ",".join(summary["overlays"])
                                    if summary.get("overlays")
                                    else "",
                                )
                            if summary.get("stripped"):
                                stripped += 1
                                log.info("Removed the embedded document from %s", relative)
                        else:
                            shutil.copy2(src, dst)
                            log.debug("Copied unmodified %s", relative)
                        written += 1
                    except Exception as exc:  # noqa: BLE001
                        errors += 1
                        log.exception("Failed to export %s: %s", src, exc)

            log.info(
                "Export finished: %d written, %d redacted, %d embedded document(s) "
                "removed, %d skipped, %d error(s)",
                written,
                redacted,
                stripped,
                len(skipped),
                errors,
            )
            self.finished.emit(written, redacted, errors, stripped, skipped)
        except Exception as exc:  # noqa: BLE001
            log.exception("Export failed: %s", exc)
            self.failed.emit(str(exc))


def start_export(series_list, input_root, output_root, on_progress, on_finished,
                 on_failed, strip_documents: bool = True,
                 skip_structured_reports: bool = False):
    thread = QThread()
    worker = ExportWorker(
        series_list, input_root, output_root, strip_documents, skip_structured_reports
    )
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.progress.connect(on_progress)
    worker.finished.connect(on_finished)
    worker.failed.connect(on_failed)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    thread.finished.connect(worker.deleteLater)
    return thread, worker
