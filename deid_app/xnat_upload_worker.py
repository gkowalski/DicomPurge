"""Upload jobs on threads of their own, and the queue that hands them out.

One UploadJob per study, each on its own QThread with its own XNAT session:
xnatpy shares a single requests.Session with a keep-alive thread and takes no
locks, so two uploads must never share one. UploadManager lives on the GUI
thread, keeps a queue of requests, and runs at most `max_concurrent` of them
at once - the rest wait until one finishes.
"""
from __future__ import annotations

import itertools
import logging
from collections import deque

from PySide6.QtCore import QObject, QThread, Signal, Slot

from .xnat_client import open_session
from .xnat_upload import UploadCancelled, UploadRequest, UploadResult, run_upload

log = logging.getLogger(__name__)


class UploadJob(QObject):
    """Runs one UploadRequest to completion on the thread it is moved to."""

    progress = Signal(int, int, int, str)   # job_id, done, total, phase
    finished = Signal(int, object)          # job_id, UploadResult
    failed = Signal(int, str)               # job_id, message
    cancelled = Signal(int)

    def __init__(self, request: UploadRequest) -> None:
        super().__init__()
        self.request = request
        self._cancel = False

    def cancel(self) -> None:
        # Read from the worker thread between files; a bool write is atomic.
        self._cancel = True

    def _is_cancelled(self) -> bool:
        return self._cancel

    def _progress(self, done: int, total: int, phase: str) -> None:
        self.progress.emit(self.request.job_id, done, total, phase)

    @Slot()
    def run(self) -> None:
        job_id = self.request.job_id
        try:
            log.info(
                "Upload %d started: %s -> project %s, subject %s, session %s (%s)",
                job_id, self.request.input_root, self.request.project,
                self.request.subject, self.request.session,
                "zip" if self.request.zip_mode else "individual files",
            )
            result = run_upload(self.request, open_session, self._progress, self._is_cancelled)
        except UploadCancelled:
            log.warning("Upload %d cancelled", job_id)
            self.cancelled.emit(job_id)
        except Exception as exc:  # noqa: BLE001 - every failure is reportable
            log.error("Upload %d failed: %s", job_id, exc)
            self.failed.emit(job_id, str(exc))
        else:
            self.finished.emit(job_id, result)


class UploadManager(QObject):
    """Queues UploadRequests and runs up to max_concurrent of them at once."""

    queued = Signal(int)
    started = Signal(int)
    progress = Signal(int, int, int, str)
    finished = Signal(int, object)
    failed = Signal(int, str)
    cancelled = Signal(int)

    def __init__(self, max_concurrent: int = 2, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._max_concurrent = max(1, int(max_concurrent))
        self._pending: deque[UploadRequest] = deque()
        self._running: dict[int, tuple[QThread, UploadJob]] = {}
        # Recently finished pairs, kept briefly so a thread is never freed
        # from inside its own finished handler.
        self._done: list = []
        self._ids = itertools.count(1)
        # The credentials each new job logs in with. None while logged out,
        # which also refuses new jobs.
        self.credentials = None

    # -- state ------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return bool(self._running or self._pending)

    @property
    def running_count(self) -> int:
        return len(self._running)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    def is_active(self, job_id: int) -> bool:
        return job_id in self._running or any(r.job_id == job_id for r in self._pending)

    # -- control ----------------------------------------------------------
    def enqueue(self, request: UploadRequest) -> int:
        if self.credentials is None:
            raise RuntimeError("Not logged in to XNAT")
        request.job_id = next(self._ids)
        request.credentials = self.credentials
        self._pending.append(request)
        log.info("Upload %d queued (%d running, %d waiting)", request.job_id,
                 len(self._running), len(self._pending))
        self.queued.emit(request.job_id)
        self._pump()
        return request.job_id

    def cancel(self, job_id: int) -> None:
        for request in list(self._pending):
            if request.job_id == job_id:
                self._pending.remove(request)
                log.info("Upload %d removed from the queue", job_id)
                self.cancelled.emit(job_id)
                return
        entry = self._running.get(job_id)
        if entry is not None:
            log.info("Cancelling upload %d (takes effect between files)", job_id)
            entry[1].cancel()

    def set_max_concurrent(self, value: int) -> None:
        """Raising it starts waiting jobs at once; lowering it never kills any."""
        self._max_concurrent = max(1, int(value))
        self._pump()

    def set_credentials(self, credentials) -> None:
        """On logout (None) the queue is dropped: a job that has not started
        must not log in as a user who has just logged out. Running jobs hold
        their own session and carry on."""
        self.credentials = credentials
        if credentials is not None:
            return
        dropped = list(self._pending)
        self._pending.clear()
        for request in dropped:
            log.warning("Upload %d dropped from the queue: logged out", request.job_id)
            self.cancelled.emit(request.job_id)
        if self._running:
            log.warning("%d upload(s) continue on their own session after logout",
                        len(self._running))

    def shutdown(self, timeout_ms: int = 3000) -> None:
        """Drop the queue, ask running jobs to stop, and join their threads."""
        self._pending.clear()
        for job_id, (thread, job) in list(self._running.items()):
            job.cancel()
        for job_id, (thread, job) in list(self._running.items()):
            thread.quit()
            if not thread.wait(timeout_ms):
                log.warning("Upload %d did not stop within %d ms", job_id, timeout_ms)
        self._running.clear()

    # -- internals --------------------------------------------------------
    def _pump(self) -> None:
        while self._pending and len(self._running) < self._max_concurrent:
            request = self._pending.popleft()
            self._launch(request)

    def _launch(self, request: UploadRequest) -> None:
        # No Qt parent and no deleteLater: the pair lives exactly as long as
        # the Python references in _running do. Mixing Qt ownership with
        # Python's here is what produces "shared QObject was deleted directly".
        thread = QThread()
        job = UploadJob(request)
        job.moveToThread(thread)
        thread.started.connect(job.run)
        job.progress.connect(self.progress)
        job.finished.connect(self.finished)
        job.failed.connect(self.failed)
        job.cancelled.connect(self.cancelled)
        for signal in (job.finished, job.failed, job.cancelled):
            signal.connect(thread.quit)
        thread.finished.connect(lambda jid=request.job_id: self._on_thread_finished(jid))
        self._running[request.job_id] = (thread, job)
        thread.start()
        log.debug("Upload %d running (%d of %d slots)", request.job_id,
                  len(self._running), self._max_concurrent)
        self.started.emit(request.job_id)

    def _on_thread_finished(self, job_id: int) -> None:
        # Dropping the references is what frees the thread and job - but only
        # after this slot returns, since the finished signal is still on the
        # stack. The thread has stopped, so nothing else touches them.
        self._done.append(self._running.pop(job_id, None))
        del self._done[:-4]
        # A slot has freed: this is what drains the queue.
        self._pump()
