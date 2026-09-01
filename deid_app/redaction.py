"""Pixel-level de-identification: blanks regions of (7FE0,0010) and (60xx,3000).

Boxes arrive as normalised fractions of the image (x, y, w, h in 0..1), which
makes them independent of instance size and of any display scaling.

Only Graphics overlays are redacted: an overlay whose Overlay Type (60xx,0040)
is ``R`` (Region of Interest) carries no burned-in content, so it is left
byte-for-byte unchanged.
"""
from __future__ import annotations

import logging

import numpy as np
import pydicom
from pydicom.dataset import Dataset
from pydicom.uid import ExplicitVRLittleEndian

from .compat import convert_color_space
from .overlays import overlay_groups, read_overlay

log = logging.getLogger(__name__)

# pydicom 3 already returns RGB for YBR source data; pydicom 2 does not.
PYDICOM_MAJOR = int(str(pydicom.__version__).split(".")[0])



def _boxes_to_pixels(boxes, rows: int, columns: int) -> list[tuple[int, int, int, int]]:
    """Normalised boxes -> integer (r0, r1, c0, c1) half-open pixel ranges."""
    out: list[tuple[int, int, int, int]] = []
    for x, y, w, h in boxes:
        c0 = int(np.floor(x * columns))
        r0 = int(np.floor(y * rows))
        c1 = int(np.ceil((x + w) * columns))
        r1 = int(np.ceil((y + h) * rows))
        c0 = max(0, min(c0, columns))
        r0 = max(0, min(r0, rows))
        c1 = max(0, min(c1, columns))
        r1 = max(0, min(r1, rows))
        if c1 > c0 and r1 > r0:
            out.append((r0, r1, c0, c1))
    return out


def _fill_value(ds: Dataset, dtype) -> int:
    """The sample value that renders as black for this photometric interpretation."""
    photometric = str(getattr(ds, "PhotometricInterpretation", "MONOCHROME2")).upper()
    if photometric == "MONOCHROME1":
        # In MONOCHROME1 the maximum stored value displays as black.
        bits_stored = int(getattr(ds, "BitsStored", getattr(ds, "BitsAllocated", 8)) or 8)
        value = (1 << bits_stored) - 1
        if np.issubdtype(dtype, np.signedinteger):
            value = (1 << (bits_stored - 1)) - 1
        return int(value)
    return 0


def _normalise_colour(ds: Dataset, arr: np.ndarray) -> np.ndarray:
    """Make sure the array is RGB (so that 0,0,0 is black) and record that in the tags."""
    photometric = str(getattr(ds, "PhotometricInterpretation", "")).upper()
    if photometric.startswith("YBR"):
        if PYDICOM_MAJOR >= 3:
            # The pydicom 3 pixel backend has already converted to RGB for us.
            log.debug("%s decoded as RGB by pydicom %s", photometric, pydicom.__version__)
            ds.PhotometricInterpretation = "RGB"
        else:
            try:
                arr = convert_color_space(arr, "YBR_FULL", "RGB")
                ds.PhotometricInterpretation = "RGB"
                log.debug("Converted %s -> RGB before redaction", photometric)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Could not convert %s to RGB (%s); blanking in the native colour space",
                    photometric,
                    exc,
                )
    if "PlanarConfiguration" in ds:
        ds.PlanarConfiguration = 0
    return arr


def _apply_to_pixel_data(ds: Dataset, boxes) -> int:
    """Blank the boxes in (7FE0,0010). Returns the number of frames touched."""
    if "PixelData" not in ds:
        log.warning("No PixelData (7FE0,0010) in dataset - nothing to blank")
        return 0

    was_compressed = bool(ds.file_meta.TransferSyntaxUID.is_compressed)
    arr = ds.pixel_array
    arr = np.array(arr, copy=True)
    arr = _normalise_colour(ds, arr)

    rows = int(ds.Rows)
    columns = int(ds.Columns)
    samples = int(getattr(ds, "SamplesPerPixel", 1) or 1)
    frames = int(getattr(ds, "NumberOfFrames", 1) or 1)

    # Normalise shape to (frames, rows, columns, samples).
    view = arr.reshape(frames, rows, columns, samples)

    fill = _fill_value(ds, arr.dtype)
    regions = _boxes_to_pixels(boxes, rows, columns)
    for r0, r1, c0, c1 in regions:
        view[:, r0:r1, c0:c1, :] = fill

    arr = view.reshape(arr.shape)

    if was_compressed:
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        log.info(
            "Transfer syntax was compressed; re-encoding as Explicit VR Little Endian"
        )
    ds.PixelData = arr.tobytes()
    ds["PixelData"].is_undefined_length = False
    ds.BitsAllocated = int(ds.BitsAllocated)
    return frames


def _apply_to_overlay(ds: Dataset, group: int, boxes) -> bool:
    """Blank the boxes in one (60xx,3000) overlay plane. Returns True if changed.

    Region-of-Interest overlays are skipped; only Graphics overlays are redacted.
    """
    overlay = read_overlay(ds, group)
    if overlay is None:
        return False

    if not overlay.is_graphics:
        log.info(
            "Overlay %s: Overlay Type %r is not G; left unchanged",
            overlay.label,
            overlay.overlay_type,
        )
        return False

    # Boxes are expressed against the image grid; map them onto the overlay grid
    # using the (1-based) overlay origin.
    image_rows = int(ds.Rows)
    image_cols = int(ds.Columns)
    changed = False
    for r0, r1, c0, c1 in _boxes_to_pixels(boxes, image_rows, image_cols):
        or0 = max(0, r0 - (overlay.origin_row - 1))
        or1 = min(overlay.rows, r1 - (overlay.origin_row - 1))
        oc0 = max(0, c0 - (overlay.origin_col - 1))
        oc1 = min(overlay.columns, c1 - (overlay.origin_col - 1))
        if or1 > or0 and oc1 > oc0:
            overlay.planes[:, or0:or1, oc0:oc1] = 0
            changed = True

    if not changed:
        log.debug("Overlay %s: no box intersects the overlay plane", overlay.label)
        return False

    ds[(group, 0x3000)].value = overlay.packed()
    log.info("Overlay %s redacted (%d frame(s))", overlay.label, overlay.frames)
    return True


def redact_dataset(ds: Dataset, boxes) -> dict:
    """Apply the boxes to pixel data and every Graphics overlay. Mutates `ds`."""
    summary = {"frames": 0, "overlays": []}
    if not boxes:
        return summary

    summary["frames"] = _apply_to_pixel_data(ds, boxes)

    for group in overlay_groups(ds):
        try:
            if _apply_to_overlay(ds, group, boxes):
                summary["overlays"].append(f"{group:04X}")
        except Exception as exc:  # noqa: BLE001
            log.exception("Failed to redact overlay (%04X,3000): %s", group, exc)

    ds.BurnedInAnnotation = "NO"
    return summary


# (0042,0011) EncapsulatedDocument and (0042,0012) its MIME type. An embedded
# document is a second, independent copy of the report that redaction boxes
# never reach and that no part of this app displays - so a reviewer has no way
# to notice it before the file leaves the machine.
ENCAPSULATED_DOC_TAGS = (0x00420011, 0x00420012)


def strip_embedded_document(ds: Dataset) -> bool:
    """Delete the encapsulated document. True if anything was removed.

    Leaves a conformant object: these tags are a vendor addition to the
    Secondary Capture IOD rather than a required part of it, and pixel data is
    untouched, so the redacted image survives intact.
    """
    removed = False
    for tag in ENCAPSULATED_DOC_TAGS:
        if tag in ds:
            del ds[tag]
            removed = True
    return removed


def _save(ds: Dataset, dst) -> None:
    """save_as() across pydicom 2.x / 3.x keyword differences."""
    import inspect

    params = inspect.signature(Dataset.save_as).parameters
    if "enforce_file_format" in params:
        ds.save_as(str(dst), enforce_file_format=True)
    else:  # pragma: no cover - pydicom 2.x
        ds.save_as(str(dst), write_like_original=False)


def redact_file(src, dst, boxes, strip_documents: bool = False) -> dict:
    """Read `src`, apply `boxes`, write the result to `dst`.

    With `strip_documents` the embedded document is removed as well, which is
    why this is called even for files with no boxes at all - copying such a file
    byte-for-byte is exactly what would leak the document.
    """
    ds = pydicom.dcmread(str(src))
    summary = redact_dataset(ds, boxes)
    summary["stripped"] = bool(strip_documents and strip_embedded_document(ds))
    _save(ds, dst)
    return summary
