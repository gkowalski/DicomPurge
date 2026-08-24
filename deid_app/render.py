"""Turn a DICOM instance into a QImage for on-screen review."""
from __future__ import annotations

import logging

import numpy as np
import pydicom
from PySide6.QtGui import QImage

from .compat import apply_modality_lut, apply_voi_lut
from .overlays import mask_on_image_grid, read_overlays

log = logging.getLogger(__name__)

# How overlay bits are painted on top of the pixel data. Graphics overlays are
# drawn the way a modality would draw them (white); ROI overlays get a distinct
# colour because they are shown for context only and are never redacted.
GRAPHICS_SHADE = 255
GRAPHICS_COLOUR = (255, 255, 255)
ROI_COLOUR = (0, 255, 255)


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


def overlay_masks(ds, frame_index: int, rows: int, columns: int):
    """Every overlay plane that lands on this image frame, as image-grid masks.

    Returns a list of (OverlayPlanes, boolean mask) pairs, group order preserved.
    """
    out = []
    for overlay in read_overlays(ds):
        plane = overlay.plane_for_image_frame(frame_index)
        if plane is None:
            continue
        mask = mask_on_image_grid(overlay, plane, rows, columns)
        if mask.any():
            out.append((overlay, mask))
    return out


def _paint_overlays(rgb: np.ndarray, masks) -> np.ndarray:
    """Burn the overlay masks into an (rows, columns, 3) uint8 array."""
    for overlay, mask in masks:
        rgb[mask] = GRAPHICS_COLOUR if overlay.is_graphics else ROI_COLOUR
    return rgb


def dataset_to_qimage(ds, frame_index: int = 0) -> QImage:
    """Render frame `frame_index` of `ds` as an 8-bit QImage (deep copy).

    Overlay planes (60xx,3000) are composited on top of the pixel data so that
    what is on screen is everything the viewer of the exported file would see.
    """
    arr = ds.pixel_array
    frames = frame_count(ds)
    rows = int(ds.Rows)
    columns = int(ds.Columns)
    samples = int(getattr(ds, "SamplesPerPixel", 1) or 1)

    view = np.asarray(arr).reshape(frames, rows, columns, samples)
    frame_index = max(0, min(frame_index, frames - 1))
    frame = view[frame_index]

    try:
        masks = overlay_masks(ds, frame_index, rows, columns)
    except Exception as exc:  # noqa: BLE001
        log.exception("Could not build overlay masks: %s", exc)
        masks = []
    needs_colour = any(not overlay.is_graphics for overlay, _ in masks)

    if samples >= 3:
        rgb = np.ascontiguousarray(frame[:, :, :3])
        if rgb.dtype != np.uint8:
            rgb = _scale_colour(rgb)
        else:
            rgb = np.array(rgb, copy=True)
        rgb = np.ascontiguousarray(_paint_overlays(rgb, masks))
        image = QImage(rgb.data, columns, rows, 3 * columns, QImage.Format_RGB888)
    elif needs_colour:
        gray = _to_8bit(ds, frame[:, :, 0])
        rgb = np.repeat(gray[:, :, np.newaxis], 3, axis=2)
        rgb = np.ascontiguousarray(_paint_overlays(rgb, masks))
        image = QImage(rgb.data, columns, rows, 3 * columns, QImage.Format_RGB888)
    else:
        gray = np.ascontiguousarray(_to_8bit(ds, frame[:, :, 0]))
        for _overlay, mask in masks:
            gray[mask] = GRAPHICS_SHADE
        image = QImage(gray.data, columns, rows, columns, QImage.Format_Grayscale8)

    return image.copy()  # detach from the numpy buffer


def _scale_colour(rgb: np.ndarray) -> np.ndarray:
    rgb = np.asarray(rgb, dtype=np.float64)
    hi = float(rgb.max()) or 1.0
    return np.ascontiguousarray((rgb / hi * 255.0).astype(np.uint8))
