"""Shared reading/writing of DICOM overlay planes (60xx,3000).

Both the viewer (`deid_app.render`) and the redactor (`deid_app.redaction`) need
to know where the overlay bits sit on the image grid, so the parsing lives here
once.

Only overlays whose Overlay Type (60xx,0040) is ``G`` (Graphics) are redacted;
``R`` (Region of Interest) overlays are displayed for context but never altered.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

log = logging.getLogger(__name__)

OVERLAY_GROUP_START = 0x6000
OVERLAY_GROUP_END = 0x601E

TYPE_GRAPHICS = "G"
TYPE_ROI = "R"


@dataclass
class OverlayPlanes:
    """One decoded (60xx,3000) overlay, as 0/1 bits on the *overlay* grid."""

    group: int
    overlay_type: str
    rows: int
    columns: int
    frames: int
    origin_row: int  # (60xx,0050), 1-based, against the image grid
    origin_col: int
    image_frame_origin: int  # (60xx,0051), 1-based
    has_image_frame_origin: bool
    planes: np.ndarray  # (frames, rows, columns), uint8 of 0/1
    padding: np.ndarray  # trailing bits of the last byte(s), preserved verbatim

    @property
    def is_graphics(self) -> bool:
        return self.overlay_type == TYPE_GRAPHICS

    @property
    def label(self) -> str:
        return f"({self.group:04X},3000)"

    def plane_for_image_frame(self, frame_index: int) -> np.ndarray | None:
        """The overlay frame that belongs on image frame `frame_index` (0-based).

        A single-frame overlay with no Image Frame Origin applies to the whole
        image, which is by far the common case.
        """
        if self.frames == 1 and not self.has_image_frame_origin:
            return self.planes[0]
        index = frame_index - (self.image_frame_origin - 1)
        if 0 <= index < self.frames:
            return self.planes[index]
        return None

    def packed(self) -> bytes:
        """Re-pack `planes` (plus the original padding) into element bytes."""
        bits = np.concatenate([self.planes.reshape(-1), self.padding])
        data = np.packbits(bits, bitorder="little").tobytes()
        if len(data) % 2:
            data += b"\x00"
        return data


def overlay_groups(ds) -> list[int]:
    """Every even group in 6000-601E that carries Overlay Data."""
    groups = set()
    for elem in ds:
        group = elem.tag.group
        if OVERLAY_GROUP_START <= group <= OVERLAY_GROUP_END and group % 2 == 0:
            if elem.tag.element == 0x3000:
                groups.add(group)
    return sorted(groups)


def overlay_type(ds, group: int) -> str:
    """Overlay Type (60xx,0040), upper-cased.

    The tag is Type 1, but if it is missing or unreadable we fall back to ``G``:
    for a de-identification tool the safe default is "treat it as burned-in
    graphics and redact it".
    """
    if (group, 0x0040) not in ds:
        log.warning(
            "Overlay (%04X,3000): no Overlay Type (%04X,0040); assuming G", group, group
        )
        return TYPE_GRAPHICS
    value = ds[(group, 0x0040)].value
    text = str(value).strip().upper() if value is not None else ""
    if not text:
        log.warning("Overlay (%04X,3000): empty Overlay Type; assuming G", group)
        return TYPE_GRAPHICS
    return text


def _int_tag(ds, group: int, element: int, default: int) -> int:
    if (group, element) not in ds:
        return default
    try:
        return int(ds[(group, element)].value)
    except Exception:  # noqa: BLE001
        log.warning("Overlay (%04X,%04X): unreadable value; using %d", group, element, default)
        return default


def read_overlay(ds, group: int) -> OverlayPlanes | None:
    """Decode one overlay group, or return None if it cannot be used."""
    if (group, 0x3000) not in ds:
        return None
    raw = ds[(group, 0x3000)].value
    if raw is None:
        return None

    rows = _int_tag(ds, group, 0x0010, 0)
    columns = _int_tag(ds, group, 0x0011, 0)
    if rows <= 0 or columns <= 0:
        log.error("Overlay (%04X,3000): bad OverlayRows/Columns %sx%s", group, rows, columns)
        return None

    frames = max(1, _int_tag(ds, group, 0x0015, 1))
    bits_allocated = _int_tag(ds, group, 0x0100, 1)
    if bits_allocated != 1:
        log.warning(
            "Overlay (%04X,3000): OverlayBitsAllocated=%d is not supported; skipped",
            group,
            bits_allocated,
        )
        return None

    origin_row, origin_col = 1, 1
    if (group, 0x0050) in ds:
        origin = ds[(group, 0x0050)].value
        try:
            origin_row, origin_col = int(origin[0]), int(origin[1])
        except Exception:  # noqa: BLE001
            log.warning("Overlay (%04X,3000): unreadable OverlayOrigin %r", group, origin)

    has_frame_origin = (group, 0x0051) in ds
    image_frame_origin = _int_tag(ds, group, 0x0051, 1) if has_frame_origin else 1

    needed = frames * rows * columns
    bits = np.unpackbits(np.frombuffer(bytes(raw), dtype=np.uint8), bitorder="little")
    if bits.size < needed:
        log.error(
            "Overlay (%04X,3000): expected %d bits but only %d present; skipped",
            group,
            needed,
            bits.size,
        )
        return None

    return OverlayPlanes(
        group=group,
        overlay_type=overlay_type(ds, group),
        rows=rows,
        columns=columns,
        frames=frames,
        origin_row=origin_row,
        origin_col=origin_col,
        image_frame_origin=image_frame_origin,
        has_image_frame_origin=has_frame_origin,
        planes=bits[:needed].reshape(frames, rows, columns).astype(np.uint8),
        padding=bits[needed:],
    )


def read_overlays(ds) -> list[OverlayPlanes]:
    """Every usable overlay in the dataset, in group order."""
    out = []
    for group in overlay_groups(ds):
        try:
            overlay = read_overlay(ds, group)
        except Exception as exc:  # noqa: BLE001
            log.exception("Failed to read overlay (%04X,3000): %s", group, exc)
            continue
        if overlay is not None:
            out.append(overlay)
    return out


def mask_on_image_grid(
    overlay: OverlayPlanes, plane: np.ndarray, image_rows: int, image_columns: int
) -> np.ndarray:
    """Place one overlay plane onto the image grid, honouring Overlay Origin.

    Returns a boolean array of shape (image_rows, image_columns).
    """
    mask = np.zeros((image_rows, image_columns), dtype=bool)
    top = overlay.origin_row - 1
    left = overlay.origin_col - 1

    r0 = max(0, top)
    c0 = max(0, left)
    r1 = min(image_rows, top + overlay.rows)
    c1 = min(image_columns, left + overlay.columns)
    if r1 <= r0 or c1 <= c0:
        return mask

    mask[r0:r1, c0:c1] = plane[r0 - top : r1 - top, c0 - left : c1 - left].astype(bool)
    return mask
