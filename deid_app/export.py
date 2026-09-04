"""Background export: applies each series' boxes and mirrors the input tree.

The per-instance decision - skip it, redact it, or copy it - lives in
export_instance() so that the XNAT upload stager (xnat_upload.py) makes exactly
the same call as a local export. Two copies of the four skip branches would
drift, and a drift here is a PHI leak.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from .redaction import redact_file
from .render import has_pixel_data, read_dataset

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExportOptions:
    """The Settings > Export checkboxes, as one value."""

    strip_documents: bool = True
    skip_structured_reports: bool = False
    skip_files_without_image_data: bool = False


@dataclass
class InstanceOutcome:
    """What export_instance did with one file."""

    action: str            # "written" or "skipped"
    reason: str = ""       # why it was skipped, already worded for the log
    redacted: bool = False
    stripped: bool = False

    @property
    def written(self) -> bool:
        return self.action == "written"


def relative_path(src: Path, input_root: Path) -> Path:
    """Where `src` sits under the input tree; bare name if it is outside it."""
    try:
        return src.relative_to(input_root)
    except ValueError:
        log.warning(
            "%s is outside the input root; writing to the output root", src
        )
        return Path(src.name)


def export_instance(series, instance, dst: Path, options: ExportOptions,
                    label: str | None = None) -> InstanceOutcome:
    """Skip, redact or copy one instance to `dst`. Raises if writing fails.

    `dst` is the full destination path; its parent is created here. `label`
    is how the file is named in the log lines (its input-relative path); it
    defaults to the bare file name.
    """
    relative = label or dst.name
    boxes = list(series.boxes)

    # Marked skipped by hand from the series context menu. Checked before the
    # settings-driven branches below, and keyed on manually_skipped rather
    # than skipped so the two reasons cannot both count the same file.
    if series.manually_skipped:
        log.warning("Skipped %s: series marked skipped by hand", relative)
        return InstanceOutcome("skipped", "series marked skipped by hand")

    # A document-only object is entirely a PDF (or CDA, ...) with no pixel
    # data, so there is nothing the box editor could ever redact. Exporting
    # it would ship the report verbatim, so it is left behind and reported.
    if options.strip_documents and instance.is_document_only:
        log.warning(
            "Skipped %s: contains only an embedded document and cannot be "
            "de-identified",
            relative,
        )
        return InstanceOutcome("skipped", "contains only an embedded document")

    # A Structured Report is text in a ContentSequence, so there are no
    # pixels for a redaction box to act on.
    if options.skip_structured_reports and instance.is_structured_report:
        log.warning(
            "Skipped %s: contains only a Structured Report and cannot be "
            "de-identified",
            relative,
        )
        return InstanceOutcome("skipped", "contains only a Structured Report")

    # No pixel data at all - a presentation state, waveform, RT object and
    # the like, shown as "Uneditable File" in the image pane. The scan infers
    # this from Rows, so confirm it against the real file before dropping
    # anything; these are rare, so the read costs little.
    if (
        options.skip_files_without_image_data
        and instance.is_uneditable_non_image
        and not has_pixel_data(read_dataset(instance.path))
    ):
        log.warning("Skipped %s: has no image data to redact", relative)
        return InstanceOutcome("skipped", "has no image data to redact")

    dst.parent.mkdir(parents=True, exist_ok=True)
    # The embedded document survives a byte-for-byte copy, so a file carrying
    # one must be rewritten even when it has no boxes - that copy is the leak.
    strip = options.strip_documents and instance.has_embedded_document
    if not boxes and not strip:
        shutil.copy2(instance.path, dst)
        log.debug("Copied unmodified %s", relative)
        return InstanceOutcome("written")

    summary = redact_file(instance.path, dst, boxes, strip_documents=strip)
    outcome = InstanceOutcome("written", redacted=bool(boxes),
                              stripped=bool(summary.get("stripped")))
    if boxes:
        log.info(
            "Redacted %s (%d frame(s)%s)",
            relative,
            summary.get("frames", 0),
            ", overlays " + ",".join(summary["overlays"])
            if summary.get("overlays")
            else "",
        )
    if outcome.stripped:
        log.info("Removed the embedded document from %s", relative)
    return outcome


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
        skip_files_without_image_data: bool = False,
    ) -> None:
        super().__init__()
        self.series_list = list(series_list)
        self.input_root = Path(input_root)
        self.output_root = Path(output_root)
        self.options = ExportOptions(
            strip_documents=bool(strip_documents),
            skip_structured_reports=bool(skip_structured_reports),
            skip_files_without_image_data=bool(skip_files_without_image_data),
        )
        self._cancelled = False

    # Kept so existing callers and tests that read these still work.
    @property
    def strip_documents(self) -> bool:
        return self.options.strip_documents

    @property
    def skip_structured_reports(self) -> bool:
        return self.options.skip_structured_reports

    @property
    def skip_files_without_image_data(self) -> bool:
        return self.options.skip_files_without_image_data

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
                log.info(
                    "Exporting series %s (%s) with %d redaction box(es)",
                    series.series_uid,
                    series.series_description or "no description",
                    len(series.boxes),
                )
                for instance in series.instances:
                    if self._cancelled:
                        log.warning("Export cancelled after %d file(s)", done)
                        self.finished.emit(
                            written, redacted, errors, stripped, skipped
                        )
                        return
                    done += 1
                    relative = relative_path(instance.path, self.input_root)
                    dst = self.output_root / relative
                    self.progress.emit(done, total, str(relative))
                    try:
                        outcome = export_instance(
                            series, instance, dst, self.options, str(relative)
                        )
                    except Exception as exc:  # noqa: BLE001
                        errors += 1
                        log.exception("Failed to export %s: %s", instance.path, exc)
                        continue
                    if not outcome.written:
                        skipped.append(str(relative))
                        continue
                    written += 1
                    redacted += int(outcome.redacted)
                    stripped += int(outcome.stripped)

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
                 skip_structured_reports: bool = False,
                 skip_files_without_image_data: bool = False):
    thread = QThread()
    worker = ExportWorker(
        series_list, input_root, output_root, strip_documents,
        skip_structured_reports, skip_files_without_image_data,
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
