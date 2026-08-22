"""Turn a DICOM instance into a QImage for on-screen review."""
from __future__ import annotations

import logging

import numpy as np
import pydicom
from PySide6.QtGui import QImage

from .compat import apply_modality_lut, apply_voi_lut

log = logging.getLogger(__name__)


def _to_8bit(ds, arr: np.ndarray) -> np.ndarray:
    """Window/level a grayscale frame down to 8 bits."""
    try:
        arr = apply_modality_lut(arr, ds)
    except Exception as exc:  # noqa: BLE001
        log.debug("Modality LUT not applied: %s", exc)
    try:
        arr = apply_voi_lut(arr, ds, prefer_lut=True)
    except Exception as exc:  # noqa: BLE001
        log.debug("VOI LUT not applied: %s", exc)

    arr = np.asarray(arr, dtype=np.float64)
    lo = float(np.min(arr))
    hi = float(np.max(arr))
    if hi <= lo:
        return np.zeros(arr.shape, dtype=np.uint8)
    scaled = (arr - lo) / (hi - lo) * 255.0

    if str(getattr(ds, "PhotometricInterpretation", "")).upper() == "MONOCHROME1":
        scaled = 255.0 - scaled
    return scaled.astype(np.uint8)


def frame_count(ds) -> int:
    return max(1, int(getattr(ds, "NumberOfFrames", 1) or 1))


def read_dataset(path) -> pydicom.Dataset:
    return pydicom.dcmread(str(path))


def dataset_to_qimage(ds, frame_index: int = 0) -> QImage:
    """Render frame `frame_index` of `ds` as an 8-bit QImage (deep copy)."""
    arr = ds.pixel_array
    frames = frame_count(ds)
    rows = int(ds.Rows)
    columns = int(ds.Columns)
    samples = int(getattr(ds, "SamplesPerPixel", 1) or 1)

    view = np.asarray(arr).reshape(frames, rows, columns, samples)
    frame_index = max(0, min(frame_index, frames - 1))
    frame = view[frame_index]

    if samples >= 3:
        rgb = np.ascontiguousarray(frame[:, :, :3])
        if rgb.dtype != np.uint8:
            rgb = _scale_colour(rgb)
        image = QImage(rgb.data, columns, rows, 3 * columns, QImage.Format_RGB888)
    else:
        gray = _to_8bit(ds, frame[:, :, 0])
        gray = np.ascontiguousarray(gray)
        image = QImage(gray.data, columns, rows, columns, QImage.Format_Grayscale8)

    return image.copy()  # detach from the numpy buffer


def _scale_colour(rgb: np.ndarray) -> np.ndarray:
    rgb = np.asarray(rgb, dtype=np.float64)
    hi = float(rgb.max()) or 1.0
    return np.ascontiguousarray((rgb / hi * 255.0).astype(np.uint8))
