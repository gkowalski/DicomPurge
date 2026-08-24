"""Headless check: the viewer composites 60xx overlays on top of the pixel data,
and only Overlay Type G is redacted."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QImage  # noqa: E402

from deid_app.overlays import read_overlay, read_overlays  # noqa: E402
from deid_app.redaction import redact_dataset  # noqa: E402
from deid_app.render import ROI_COLOUR, dataset_to_qimage  # noqa: E402
from make_fixtures import _base, add_overlay  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)-8s %(message)s")

ROWS = COLS = 64
failures: list[str] = []


def check(condition, message):
    if condition:
        print(f"  PASS  {message}")
    else:
        print(f"  FAIL  {message}")
        failures.append(message)


def mono(frames: int = 1, value: int = 0):
    ds = _base(ROWS, COLS, "1.2.3.9", 9, "overlay display", "US")
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    if frames > 1:
        ds.NumberOfFrames = frames
    ds.PixelData = np.full((frames, ROWS, COLS), value, dtype=np.uint8).tobytes()
    return ds


def banner(rows, cols, height=8):
    """A plane with the top `height` rows set - like burned-in annotation."""
    plane = np.zeros((1, rows, cols), dtype=np.uint8)
    plane[0, :height, :] = 1
    return plane


def rgb_at(image: QImage, x: int, y: int):
    c = image.pixelColor(x, y)
    return (c.red(), c.green(), c.blue())


def main():
    print("\n--- graphics overlay on a grayscale frame ---")
    ds = mono()
    add_overlay(ds, 0x6000, ROWS, COLS, plane=banner(ROWS, COLS))
    image = dataset_to_qimage(ds, 0)
    check(image.format() == QImage.Format_Grayscale8, "stays 8-bit grayscale")
    check(rgb_at(image, 10, 2) == (255, 255, 255), "overlay bits drawn white")
    check(rgb_at(image, 10, 40) == (0, 0, 0), "pixels outside the overlay untouched")

    print("\n--- ROI overlay forces colour and uses a distinct shade ---")
    ds = mono()
    add_overlay(ds, 0x6000, ROWS, COLS, plane=banner(ROWS, COLS))
    add_overlay(ds, 0x6004, 16, 16, origin=(33, 33), overlay_type="R")
    image = dataset_to_qimage(ds, 0)
    check(image.format() == QImage.Format_RGB888, "promoted to RGB for the ROI colour")
    check(rgb_at(image, 10, 2) == (255, 255, 255), "graphics overlay still white")
    check(rgb_at(image, 40, 40) == ROI_COLOUR, "ROI overlay drawn in its own colour")
    check(rgb_at(image, 10, 40) == (0, 0, 0), "pixels outside both overlays untouched")

    print("\n--- overlay origin is honoured ---")
    ds = mono()
    add_overlay(ds, 0x6000, 16, 16, origin=(33, 33))
    image = dataset_to_qimage(ds, 0)
    check(rgb_at(image, 40, 40) == (255, 255, 255), "overlay drawn at its origin")
    check(rgb_at(image, 10, 10) == (0, 0, 0), "nothing drawn above the origin")

    print("\n--- multi-frame overlay follows the image frame ---")
    ds = mono(frames=3)
    planes = np.zeros((3, ROWS, COLS), dtype=np.uint8)
    planes[1, :8, :] = 1  # only the middle frame carries the banner
    add_overlay(ds, 0x6000, ROWS, COLS, frames=3, plane=planes, image_frame_origin=1)
    check(rgb_at(dataset_to_qimage(ds, 0), 10, 2) == (0, 0, 0), "frame 0 has no overlay")
    check(
        rgb_at(dataset_to_qimage(ds, 1), 10, 2) == (255, 255, 255),
        "frame 1 shows its overlay",
    )
    check(rgb_at(dataset_to_qimage(ds, 2), 10, 2) == (0, 0, 0), "frame 2 has no overlay")

    print("\n--- redaction blanks G and leaves R alone ---")
    ds = mono(value=200)
    add_overlay(ds, 0x6000, ROWS, COLS)  # graphics, every bit set
    add_overlay(ds, 0x6002, ROWS, COLS, overlay_type="R")  # ROI, every bit set
    before_roi = bytes(ds[(0x6002, 0x3000)].value)
    summary = redact_dataset(ds, [(0.25, 0.25, 0.25, 0.25)])
    print(f"  summary: {summary}")
    check(summary["overlays"] == ["6000"], "only the G overlay reported as redacted")

    g = read_overlay(ds, 0x6000)
    r0, r1 = int(0.25 * ROWS), int(0.5 * ROWS)
    c0, c1 = int(0.25 * COLS), int(0.5 * COLS)
    check(int(g.planes[:, r0:r1, c0:c1].max()) == 0, "G overlay bits cleared inside box")
    rest = g.planes.copy()
    rest[:, r0:r1, c0:c1] = 0
    check(int(rest.max()) == 1, "G overlay bits outside the box kept")
    check(
        bytes(ds[(0x6002, 0x3000)].value) == before_roi,
        "R overlay bytes identical after redaction",
    )

    print("\n--- a redacted box hides both pixel data and overlay on screen ---")
    image = dataset_to_qimage(ds, 0)
    inside = [rgb_at(image, x, y) for x in range(c0 + 1, c1 - 1) for y in range(r0 + 1, r1 - 1)]
    check(
        all(px == (0, 0, 0) or px == ROI_COLOUR for px in inside),
        "nothing but the untouched ROI overlay is visible inside the box",
    )

    print("\n--- overlay type defaults to G when the tag is missing ---")
    ds = mono()
    add_overlay(ds, 0x6000, ROWS, COLS)
    del ds[(0x6000, 0x0040)]
    overlays = read_overlays(ds)
    check(overlays and overlays[0].is_graphics, "missing Overlay Type treated as G")

    print()
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
