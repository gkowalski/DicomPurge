"""Background rendering of DICOM frames so the GUI thread never blocks."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from .decode_cache import DatasetCache, FrameCache
from .model import Instance
from .render import dataset_to_qimage, has_pixel_data, read_dataset
from .sr_render import dataset_to_html, is_structured_report

log = logging.getLogger(__name__)


@dataclass
class FrameRenderRequest:
    epoch: int
    position: int
    instance: Instance
    frame_index: int


@dataclass
class FrameRenderResult:
    epoch: int
    position: int
    instance: Instance
    frame_index: int
    ds: Any
    kind: str  # "image" | "report" | "no_pixel" | "error"
    payload: Any  # QImage | html str | None | error str


def render_frame(
    datasets: DatasetCache,
    frames: FrameCache,
    instance: Instance,
    frame_index: int,
) -> tuple[str, Any, Any]:
    """Produce the (kind, payload, ds) triple for one frame.

    Shared by the render worker and the prefetch worker so a prefetched frame
    is byte-for-byte what the user would otherwise wait for, and lands in the
    same `FrameCache`. Both caches are thread-safe; both workers use them.
    """
    cached = frames.get(instance.path, frame_index)
    if cached is not None:
        return cached

    started = time.perf_counter()
    ds = datasets.get(instance.path)
    ds_hit = ds is not None
    if ds is None:
        ds = read_dataset(instance.path)
        datasets.put(instance.path, ds)
    read_ms = (time.perf_counter() - started) * 1000.0

    decoded = time.perf_counter()
    if is_structured_report(ds):
        entry = ("report", dataset_to_html(ds), ds)
    elif not has_pixel_data(ds):
        entry = ("no_pixel", None, ds)
    else:
        entry = ("image", dataset_to_qimage(ds, frame_index), ds)
    render_ms = (time.perf_counter() - decoded) * 1000.0

    frames.put(instance.path, frame_index, entry)
    log.debug(
        "Rendered %s frame %d: dataset %s in %.0f ms, %s in %.0f ms",
        instance.path.name,
        frame_index,
        "cache hit" if ds_hit else "read",
        read_ms,
        entry[0],
        render_ms,
    )
    return entry


class FrameWorker(QObject):
    """Decodes one DICOM frame per request, on its own thread.

    Uses the `DatasetCache` / `FrameCache` pair shared with `PrefetchWorker`,
    so decoding never blocks the main event loop and a frame the prefetcher
    already rendered comes back immediately.
    """

    rendered = Signal(object)

    def __init__(self, datasets: DatasetCache, frames: FrameCache) -> None:
        super().__init__()
        self._datasets = datasets
        self._frames = frames

    @Slot(object)
    def render(self, request: FrameRenderRequest) -> None:
        instance = request.instance
        try:
            kind, payload, ds = render_frame(
                self._datasets, self._frames, instance, request.frame_index
            )
            result = FrameRenderResult(
                request.epoch, request.position, instance, request.frame_index,
                ds, kind, payload,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Failed to render %s: %s", instance.path, exc)
            result = FrameRenderResult(
                request.epoch, request.position, instance, request.frame_index,
                None, "error", str(exc),
            )
        self.rendered.emit(result)


# A prefetch request more than this many positions from the frame on screen is
# thrown away when it reaches the worker: during a fast scrub the queue fills
# faster than it drains, and decoding frames the user has already scrolled past
# would leave the prefetcher permanently behind them.
PREFETCH_WINDOW = 12


@dataclass
class PrefetchRequest:
    epoch: int
    instance: Instance
    frame_index: int = 0
    position: int = 0


class PrefetchWorker(QObject):
    """Renders frames the user is heading towards into the shared caches.

    Runs on its own QThread so a slow prefetch can never delay an in-flight
    user-requested render, and is fire-and-forget: no signal back to the GUI
    thread. Requests from an abandoned selection are dropped rather than
    decoded, so a queue built up before a series change does not squat on the
    thread while the new series needs it.
    """

    def __init__(self, datasets: DatasetCache, frames: FrameCache) -> None:
        super().__init__()
        self._datasets = datasets
        self._frames = frames
        # Both are written from the GUI thread and read here. They are plain
        # int assignments (atomic), deliberately not queued signals: a signal
        # would arrive behind the very requests it is meant to invalidate.
        self._epoch = 0
        self._focus = 0

    def set_epoch(self, epoch: int) -> None:
        self._epoch = epoch

    def set_focus(self, position: int) -> None:
        self._focus = position

    @Slot(object)
    def prefetch(self, request: PrefetchRequest) -> None:
        if request.epoch < self._epoch:
            return
        if abs(request.position - self._focus) > PREFETCH_WINDOW:
            return
        instance = request.instance
        if self._frames.has(instance.path, request.frame_index):
            return
        try:
            render_frame(self._datasets, self._frames, instance, request.frame_index)
        except Exception as exc:  # noqa: BLE001
            log.info("Prefetch skipped for %s: %s", instance.path, exc)
