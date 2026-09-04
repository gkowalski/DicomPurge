"""Uploading one de-identified study to XNAT, with no Qt in sight.

Everything here runs on a worker thread of its own (xnat_upload_worker.py) but
is written as plain functions so it can be tested against a fake session with
no event loop and no server.

Each upload de-identifies on the fly: the study's files go through exactly the
same export_instance() call as a local export, into a fresh directory under
the temp root, and it is that staged copy that is sent. Nothing under the
input tree is ever uploaded directly.
"""
from __future__ import annotations

import logging
import re
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pydicom

from .export import ExportOptions, export_instance, relative_path
from .model import Series

log = logging.getLogger(__name__)

# XNAT rejects anything outside this set in a project, subject or session
# label, so it is what the sanitiser keeps.
LABEL_RE = re.compile(r"[^A-Za-z0-9_-]+")
LABEL_MAX_LEN = 64

# Direct-Archive and Ignore-Unparsable arrived in this XNAT release; xnatpy
# silently drops both flags on anything older.
DIRECT_ARCHIVE_MIN_VERSION = (1, 8, 3)

# Progress signature shared by every phase: (done, total, phase). A total of 0
# means "indeterminate" - the server is working and there is nothing to count.
ProgressFn = Callable[[int, int, str], None]
CancelFn = Callable[[], bool]

PHASE_STAGING = "Staging"
PHASE_ZIPPING = "Zipping"
PHASE_UPLOADING = "Uploading"
PHASE_ARCHIVING = "Archiving"

# Direct-Archive is asynchronous: the server answers as soon as it has the zip
# and builds the session in the background - two to three minutes on XNAT
# 1.9.2 at MCW. The job waits this long for the session to show up in the
# project before reporting; after that it reports "accepted" rather than
# "archived", which is the truth at that point.
ARCHIVE_WAIT_SECONDS = 600
ARCHIVE_POLL_SECONDS = 10

WHERE_ARCHIVED = "archived"
WHERE_ACCEPTED = "accepted (archiving in background)"
WHERE_PREARCHIVE = "in prearchive"


class UploadCancelled(Exception):
    """Raised inside a job when is_cancelled() turns true between steps."""


def sanitise_label(text: str) -> str:
    """Reduce free text to something XNAT accepts as a label."""
    cleaned = LABEL_RE.sub("_", (text or "").strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_-")
    cleaned = cleaned[:LABEL_MAX_LEN].rstrip("_-")
    return cleaned or "unknown"


# -- what is on screen --------------------------------------------------------
@dataclass
class StudyRow:
    """One uploadable unit: every series sharing a patient and a study."""

    patient_id: str
    patient_name: str
    study_uid: str
    study_description: str
    modality: str
    series: list[Series] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return bool(self.series) and all(s.export_ready for s in self.series)

    @property
    def not_ready_count(self) -> int:
        return sum(1 for s in self.series if not s.export_ready)

    @property
    def file_count(self) -> int:
        return sum(len(s.instances) for s in self.series)

    @property
    def subject_label(self) -> str:
        return sanitise_label(self.patient_id)


def group_studies(series_map: dict[str, Series]) -> list[StudyRow]:
    """Group the scanned series into studies, in the order the tree shows them.

    The modality comes from the series with the lowest series number - "the
    first images in that study" - so the default label is stable no matter in
    which order the files were scanned.
    """
    rows: dict[tuple[str, str], StudyRow] = {}
    for series in sorted(
        series_map.values(),
        key=lambda s: (s.patient_name, s.patient_id, s.study_uid, s.series_number),
    ):
        key = (series.patient_id, series.study_uid)
        row = rows.get(key)
        if row is None:
            row = StudyRow(
                patient_id=series.patient_id,
                patient_name=series.patient_name,
                study_uid=series.study_uid,
                study_description=series.study_description,
                modality=series.modality,
                series=[],
            )
            rows[key] = row
        row.series.append(series)
    for row in rows.values():
        first = min(row.series, key=lambda s: (s.series_number, s.series_uid))
        row.modality = first.modality
    return list(rows.values())


def default_session_labels(rows: list[StudyRow], existing: set[str] | None = None) -> dict[str, str]:
    """`<PatientID>_<modality>_<N>` per study, N counting from 1 per subject.

    `existing` holds the session labels already in the chosen project; those
    are skipped over, as are labels handed out earlier in this same batch, so
    two studies of one subject never collide with each other or the server.
    """
    taken = set(existing or ())
    labels: dict[str, str] = {}
    counters: dict[str, int] = {}
    for row in rows:
        base = f"{row.subject_label}_{sanitise_label(row.modality)}_"
        n = counters.get(row.patient_id, 1)
        while f"{base}{n}" in taken:
            n += 1
        label = f"{base}{n}"
        taken.add(label)
        labels[row.study_uid] = label
        counters[row.patient_id] = n + 1
    return labels


# -- one job ----------------------------------------------------------------
@dataclass
class UploadRequest:
    job_id: int
    project: str
    subject: str
    session: str
    series: list[Series]
    input_root: Path
    zip_mode: bool
    temp_root: Path
    options: ExportOptions = field(default_factory=ExportOptions)
    # Rewrite PatientName/PatientID in the staged copies to the subject and
    # session labels. Needed for gradual-DICOM, which files by the headers
    # and ignores the subject/session parameters; the zip path honours the
    # parameters, so it defaults to off there. Never touches the input files.
    relabel_headers: bool | None = None
    # Read by the worker, never by the functions here: the credentials to
    # open this job's own session with.
    credentials: object = None

    @property
    def relabel(self) -> bool:
        return (not self.zip_mode) if self.relabel_headers is None else self.relabel_headers

    @property
    def file_count(self) -> int:
        return sum(len(s.instances) for s in self.series)


@dataclass
class UploadResult:
    uri: str = ""
    archived: bool = False
    # One of the WHERE_* strings: what the server has done with it so far.
    where: str = WHERE_PREARCHIVE
    written: int = 0
    redacted: int = 0
    stripped: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: int = 0

    def describe(self) -> str:
        where = self.where
        extra = []
        if self.redacted:
            extra.append(f"{self.redacted} redacted")
        if self.stripped:
            extra.append(f"{self.stripped} document(s) removed")
        if self.skipped:
            extra.append(f"{len(self.skipped)} skipped")
        if self.errors:
            extra.append(f"{self.errors} error(s)")
        tail = f" ({', '.join(extra)})" if extra else ""
        return f"{where}: {self.written} file(s){tail}"


def _check_temp_root(temp_root: Path, input_root: Path) -> None:
    """Refuse a temp root at or under the input tree, exactly as export does."""
    try:
        temp_root = temp_root.resolve()
        input_root = input_root.resolve()
    except OSError:
        return
    if temp_root == input_root or input_root in temp_root.parents:
        raise ValueError(
            "The upload temporary directory must not be the input directory "
            "or inside it (Settings > XNAT upload)."
        )


def make_stage_dir(req: UploadRequest) -> Path:
    """A fresh, empty directory under the temp root for this job alone.

    Created by the caller of stage_study rather than inside it, so that the
    caller holds the path - and can remove it - even when staging is cut short
    by a cancel or an error.
    """
    _check_temp_root(req.temp_root, req.input_root)
    req.temp_root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"dicompurge_upload_{req.job_id}_",
                                 dir=req.temp_root))


def stage_study(req: UploadRequest, stage_dir: Path, progress: ProgressFn,
                is_cancelled: CancelFn, result: UploadResult) -> None:
    """De-identify the study into `stage_dir` (from make_stage_dir).

    Counts go into `result` so a failure part-way still reports what had
    been done.
    """
    total = req.file_count
    done = 0
    progress(0, total, PHASE_STAGING)
    for series in req.series:
        for instance in series.instances:
            if is_cancelled():
                raise UploadCancelled()
            relative = relative_path(instance.path, req.input_root)
            try:
                outcome = export_instance(
                    series, instance, stage_dir / relative, req.options, str(relative)
                )
            except Exception as exc:  # noqa: BLE001 - counted, not fatal
                result.errors += 1
                log.exception("Failed to stage %s: %s", instance.path, exc)
            else:
                if outcome.written and req.relabel:
                    relabel_headers(stage_dir / relative, req.subject, req.session)
                if outcome.written:
                    result.written += 1
                    result.redacted += int(outcome.redacted)
                    result.stripped += int(outcome.stripped)
                else:
                    result.skipped.append(str(relative))
            done += 1
            progress(done, total, PHASE_STAGING)
    if result.written == 0:
        raise RuntimeError(
            f"Nothing to upload: all {total} file(s) were skipped or failed"
        )
    log.info(
        "Staged %d file(s) for %s/%s/%s in %s (%d skipped, %d error(s))%s",
        result.written, req.project, req.subject, req.session, stage_dir,
        len(result.skipped), result.errors,
        "; PatientName/PatientID rewritten to the labels" if req.relabel else "",
    )


def relabel_headers(path: Path, subject: str, session: str) -> None:
    """Set PatientName := subject and PatientID := session in one staged file.

    That is XNAT's default DICOM routing: the receiver behind gradual-DICOM
    takes the subject label from PatientName and the session label from
    PatientID (confirmed on XNAT 1.8 at MCW - the first attempt had them the
    other way round and the prearchive showed the labels swapped). It is the
    only handle that receiver offers for choosing where a file is filed.
    Applied to the staged copy only, never to the input tree.
    """
    ds = pydicom.dcmread(str(path), force=True)
    ds.PatientName = subject
    ds.PatientID = session
    ds.save_as(str(path))
    log.debug("Relabelled %s: PatientName=%s PatientID=%s", path.name, subject, session)


def staged_files(stage_dir: Path) -> list[Path]:
    return sorted(p for p in stage_dir.rglob("*") if p.is_file())


def zip_staged(stage_dir: Path, progress: ProgressFn) -> Path:
    """Zip the staged tree next to it. DICOM is already compressed, so store."""
    zip_path = stage_dir.with_name(stage_dir.name + ".zip")
    files = staged_files(stage_dir)
    progress(0, len(files), PHASE_ZIPPING)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        for index, path in enumerate(files, start=1):
            zf.write(path, arcname=str(path.relative_to(stage_dir)))
            progress(index, len(files), PHASE_ZIPPING)
    log.info("Zipped %d file(s) into %s (%d bytes)", len(files), zip_path,
             zip_path.stat().st_size)
    return zip_path


class ProgressFile:
    """A seekable read-only stream that reports how much has been read.

    xnatpy's upload_stream does not forward a progress callback, so byte
    progress for a zip upload has to come from the stream itself. __len__ is
    what makes requests send a Content-Length rather than a chunked body, and
    seek(0) resets the count because upload_stream rewinds before each try.
    """

    def __init__(self, fh, size: int, on_bytes: Callable[[int, int], None],
                 step: int | None = None) -> None:
        self._fh = fh
        self._size = size
        self._on_bytes = on_bytes
        self._sent = 0
        self._reported = 0
        # Report roughly every 1% (at least every 256 KB) - a signal per
        # read() would swamp the GUI queue on a fast link.
        self._step = step if step is not None else max(size // 100, 256 * 1024)

    def __len__(self) -> int:
        return self._size

    def read(self, size: int = -1) -> bytes:
        data = self._fh.read(size)
        if not data:
            # EOF: requests reads once more after the last byte. Reporting
            # again here would announce the end of the upload twice.
            return data
        self._sent += len(data)
        if self._sent - self._reported >= self._step or self._sent >= self._size:
            self._reported = self._sent
            self._on_bytes(self._sent, self._size)
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        pos = self._fh.seek(offset, whence)
        self._sent = pos
        self._reported = pos
        return pos

    def tell(self) -> int:
        return self._fh.tell()

    @property
    def mode(self) -> str:  # requests looks for this when guessing a body type
        return "rb"


def _supports_direct_archive(session) -> bool:
    try:
        version = tuple(session.xnat_version_tuple)
    except Exception:  # noqa: BLE001 - dev builds report odd strings
        log.warning("Could not read the XNAT version; assuming Direct-Archive "
                    "is unsupported")
        return False
    return version >= DIRECT_ARCHIVE_MIN_VERSION


IMPORT_URI = "/data/services/import"


def _post_import(session, stream, content_type: str, query: dict, timeout) -> str:
    """POST one body to the import service; returns the response text.

    Deliberately not services.import_(): that helper turns the response into
    an xnatpy object, and a Direct-Archive upload answers with an
    /xapi/direct-archive/... tracking URL that xnatpy cannot model - it raised
    KeyError('items') on a perfectly good upload. The text is all that is
    needed here.
    """
    response = session.upload_stream(
        IMPORT_URI, stream, query=query, content_type=content_type,
        method="post", timeout=timeout,
    )
    return (getattr(response, "text", "") or "").strip()


def _describe_import_response(text: str) -> tuple[str, bool, str]:
    """(uri, archived, label the server used) from the import response.

    Seen in practice:
      /data/prearchive/projects/P/20260904_104707173/LABEL   -> prearchive
      /xapi/direct-archive/P/<StudyInstanceUID>/LABEL        -> archived
      /data/archive/projects/P/subjects/S/experiments/LABEL  -> archived
    """
    uri = text.strip().splitlines()[0].strip() if text.strip() else ""
    archived = not uri.startswith("/data/prearchive")
    label = uri.rstrip("/").rsplit("/", 1)[-1] if "/" in uri else ""
    return uri, archived, label


def upload_zip(session, req: UploadRequest, zip_path: Path, progress: ProgressFn,
               is_cancelled: CancelFn = lambda: False) -> tuple[str, bool]:
    """One POST of the whole study: DICOM-zip straight to the archive."""
    size = zip_path.stat().st_size
    if not _supports_direct_archive(session):
        log.warning(
            "XNAT %s is older than %s: Direct-Archive is unavailable, so %s will "
            "land in the prearchive", getattr(session, "xnat_version", "?"),
            ".".join(map(str, DIRECT_ARCHIVE_MIN_VERSION)), req.session,
        )

    def on_bytes(sent: int, total: int) -> None:
        progress(sent, total, PHASE_UPLOADING)
        if sent >= total:
            # The last byte is out; now the server unpacks and archives, which
            # can take a while for a large study and cannot be measured.
            progress(0, 0, PHASE_ARCHIVING)

    progress(0, size, PHASE_UPLOADING)
    log.info("Uploading %s (%d bytes) to project %s as %s/%s", zip_path.name,
             size, req.project, req.subject, req.session)
    query = {
        "import-handler": "DICOM-zip",
        "Direct-Archive": "true",
        "Ignore-Unparsable": "true",
        "project": req.project,
        "subject": req.subject,
        "session": req.session,
        "overwrite": "none",
    }
    with open(zip_path, "rb") as fh:
        stream = ProgressFile(fh, size, on_bytes)
        text = _post_import(session, stream, "application/zip", query, (30, 3600))
    uri, archived, label = _describe_import_response(text)
    _warn_if_relabelled(req, label)
    if archived and label:
        archived = _wait_for_archive(session, req.project, label, progress, is_cancelled)
    return uri, archived


def _wait_for_archive(session, project: str, label: str, progress: ProgressFn,
                      is_cancelled: CancelFn) -> bool:
    """Poll the project's session list until `label` appears. True if it did."""
    progress(0, 0, PHASE_ARCHIVING)
    deadline = time.monotonic() + ARCHIVE_WAIT_SECONDS
    polls = 0
    while True:
        try:
            payload = session.get_json(f"/data/projects/{project}/experiments",
                                       query={"columns": "ID,label"})
            labels = {str(r.get("label", "")) for r in payload["ResultSet"]["Result"]}
        except Exception as exc:  # noqa: BLE001 - keep waiting, the upload is done
            log.debug("Archive poll failed: %s", exc)
            labels = set()
        polls += 1
        if label in labels:
            log.info("%s is in the %s archive (after %d poll(s))", label, project, polls)
            return True
        if time.monotonic() >= deadline or is_cancelled():
            log.warning(
                "%s was accepted by %s but has not appeared in the archive after "
                "%d s; XNAT is still processing it in the background",
                label, project, ARCHIVE_WAIT_SECONDS,
            )
            return False
        time.sleep(ARCHIVE_POLL_SECONDS)


def upload_files(session, req: UploadRequest, stage_dir: Path, progress: ProgressFn,
                 is_cancelled: CancelFn) -> tuple[str, bool]:
    """One POST per file: gradual-DICOM into the project's prearchive."""
    files = staged_files(stage_dir)
    progress(0, len(files), PHASE_UPLOADING)
    log.info("Uploading %d file(s) individually to the prearchive of %s as %s/%s",
             len(files), req.project, req.subject, req.session)
    # gradual-DICOM is XNAT's DICOM receiver behind an HTTP door: it takes the
    # project from dest= and everything else from the DICOM headers. Seen on
    # XNAT 1.8 at MCW: with dest= the subject and session came from PatientID;
    # without it even the project was ignored and the session landed in
    # "Unassigned". So dest= stays, and the subject/session parameters are sent
    # for servers that do honour them - the headers decide otherwise.
    query = {
        "import-handler": "gradual-DICOM",
        "dest": f"/prearchive/projects/{req.project}",
        "project": req.project,
        "subject": req.subject,
        "session": req.session,
        "overwrite": "none",
    }
    text = ""
    for index, path in enumerate(files, start=1):
        if is_cancelled():
            raise UploadCancelled()
        with open(path, "rb") as fh:
            text = _post_import(session, fh, "application/dicom", query, (30, 600))
        progress(index, len(files), PHASE_UPLOADING)
    uri, archived, label = _describe_import_response(text)
    _warn_if_relabelled(req, label)
    return uri, archived


def _warn_if_relabelled(req: UploadRequest, label: str) -> None:
    """gradual-DICOM follows the DICOM receiver's routing rules, which can key
    off the headers rather than the parameters sent. Say so, loudly, if the
    session did not land under the label that was asked for."""
    if label and label != req.session:
        log.warning(
            "XNAT filed the upload as session %r rather than the requested %r - "
            "the server routed it by the DICOM headers, not the parameters",
            label, req.session,
        )


def cleanup(stage_dir: Path | None, zip_path: Path | None) -> None:
    for target in (zip_path, stage_dir):
        if target is None or not target.exists():
            continue
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            log.debug("Removed %s", target)
        except OSError as exc:
            log.warning("Could not remove %s: %s", target, exc)


def run_upload(req: UploadRequest, open_session: Callable[[object], object],
               progress: ProgressFn, is_cancelled: CancelFn) -> UploadResult:
    """The whole job: stage, (zip), connect, post, disconnect, clean up.

    The session is opened only after staging so that a slow redaction never
    holds an idle server session; and it is closed in a finally, as is the
    removal of the staged copy, so neither survives a failure.
    """
    result = UploadResult()
    stage_dir: Path | None = None
    zip_path: Path | None = None
    session = None
    try:
        stage_dir = make_stage_dir(req)
        stage_study(req, stage_dir, progress, is_cancelled, result)
        if req.zip_mode:
            zip_path = zip_staged(stage_dir, progress)
        if is_cancelled():
            raise UploadCancelled()
        session = open_session(req.credentials)
        try:
            if req.zip_mode:
                result.uri, result.archived = upload_zip(
                    session, req, zip_path, progress, is_cancelled
                )
                result.where = WHERE_ARCHIVED if result.archived else (
                    WHERE_PREARCHIVE if result.uri.startswith("/data/prearchive")
                    else WHERE_ACCEPTED
                )
            else:
                result.uri, result.archived = upload_files(
                    session, req, stage_dir, progress, is_cancelled
                )
                result.where = WHERE_ARCHIVED if result.archived else WHERE_PREARCHIVE
        finally:
            try:
                session.disconnect()
            except Exception as exc:  # noqa: BLE001 - never mask the real error
                log.warning("Error closing the upload session: %s", exc)
        log.info("Upload %d finished: %s -> %s", req.job_id, result.describe(),
                 result.uri or "(no URI returned)")
        return result
    finally:
        cleanup(stage_dir, zip_path)
