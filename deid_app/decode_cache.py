"""Thread-safe bounded LRU of path -> decoded pydicom.Dataset, shared
between the render worker and the prefetch worker."""
from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path

import pydicom


class DatasetCache:
    def __init__(self, capacity: int = 4) -> None:
        self._capacity = max(1, capacity)
        self._lock = threading.Lock()
        self._entries: "OrderedDict[Path, pydicom.Dataset]" = OrderedDict()

    def get(self, path: Path) -> pydicom.Dataset | None:
        with self._lock:
            ds = self._entries.get(path)
            if ds is not None:
                self._entries.move_to_end(path)
            return ds

    def put(self, path: Path, ds: pydicom.Dataset) -> None:
        with self._lock:
            self._entries[path] = ds
            self._entries.move_to_end(path)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)

    def set_capacity(self, capacity: int) -> None:
        """Resize, evicting straight away rather than at the next put."""
        with self._lock:
            self._capacity = max(1, capacity)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)

    def __contains__(self, path: Path) -> bool:
        with self._lock:
            return path in self._entries

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class FrameCache:
    """Thread-safe bounded LRU of (path, frame_index) -> rendered frame.

    Holds what `frame_worker.render_frame` produced - the (kind, payload, ds)
    triple - so a frame that has already been rendered once, by the render
    worker or ahead of time by the prefetch worker, is redisplayed without
    touching pixel data again.
    """

    def __init__(self, capacity: int = 48, max_bytes: int = 256 * 1024 * 1024) -> None:
        self._capacity = max(1, capacity)
        # Frames vary hugely in size (a 512x512 CT slice is 256 KB, a digital
        # radiograph tens of MB), so the cache is bounded by bytes as well as
        # by entry count - otherwise a big series could hold hundreds of MB.
        self._max_bytes = max(1, max_bytes)
        self._bytes = 0
        self._lock = threading.Lock()
        self._entries: "OrderedDict[tuple[Path, int], tuple]" = OrderedDict()
        self._sizes: "dict[tuple[Path, int], int]" = {}

    def get(self, path: Path, frame_index: int) -> tuple | None:
        with self._lock:
            entry = self._entries.get((path, frame_index))
            if entry is not None:
                self._entries.move_to_end((path, frame_index))
            return entry

    def put(self, path: Path, frame_index: int, entry: tuple) -> None:
        with self._lock:
            key = (path, frame_index)
            if key in self._entries:
                self._bytes -= self._sizes.pop(key, 0)
            size = _entry_bytes(entry)
            self._entries[key] = entry
            self._sizes[key] = size
            self._bytes += size
            self._entries.move_to_end(key)
            self._evict_locked()

    def set_limits(self, capacity: int, max_bytes: int) -> None:
        """Resize, evicting straight away rather than at the next put."""
        with self._lock:
            self._capacity = max(1, capacity)
            self._max_bytes = max(1, max_bytes)
            self._evict_locked()

    def _evict_locked(self) -> None:
        """Drop least-recently-used frames until both limits hold. Caller holds the lock."""
        while self._entries and (
            len(self._entries) > self._capacity or self._bytes > self._max_bytes
        ):
            oldest, _ = self._entries.popitem(last=False)
            self._bytes -= self._sizes.pop(oldest, 0)

    def has(self, path: Path, frame_index: int) -> bool:
        with self._lock:
            return (path, frame_index) in self._entries

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._sizes.clear()
            self._bytes = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def _entry_bytes(entry: tuple) -> int:
    """Rough footprint of a cached (kind, payload, ds) triple.

    Counts the rendered payload *and* the dataset it came from: a cached entry
    keeps that dataset alive (the Metadata tab needs it), pixel data included,
    and on a large radiograph the dataset dwarfs the QImage.
    """
    payload = entry[1] if len(entry) > 1 else None
    ds = entry[2] if len(entry) > 2 else None
    return _payload_bytes(payload) + _dataset_bytes(ds)


def _payload_bytes(payload) -> int:
    size = getattr(payload, "sizeInBytes", None)  # QImage
    if callable(size):
        try:
            return int(size())
        except Exception:  # noqa: BLE001
            return 0
    if isinstance(payload, str):
        return len(payload)
    return 0


def _dataset_bytes(ds) -> int:
    if ds is None:
        return 0
    total = 0
    try:
        elem = ds.get("PixelData")
        value = getattr(elem, "value", None)
        if isinstance(value, (bytes, bytearray, memoryview)):
            total += len(bytes(value))
    except Exception:  # noqa: BLE001
        pass
    # pydicom keeps the decoded array on the dataset once pixel_array is
    # touched; that copy is usually the larger half.
    arr = getattr(ds, "_pixel_array", None)
    nbytes = getattr(arr, "nbytes", None)
    if isinstance(nbytes, int):
        total += nbytes
    return total
