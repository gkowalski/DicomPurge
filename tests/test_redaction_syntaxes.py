"""Headless check: redaction across transfer syntaxes and photometric types.

Big Endian, Deflated and RLE input must come back with the box blanked AND
every other sample intact; compressed input must stay compressed (RLE as RLE,
JPEG 2000 as JPEG 2000 Lossless) and not balloon to the uncompressed size; PALETTE COLOR must blank with the darkest entry,
not blindly with index 0.
"""
from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

import numpy as np
import pydicom
from pydicom.pixels import apply_color_lut, compress
from pydicom.uid import (
    DeflatedExplicitVRLittleEndian,
    ExplicitVRBigEndian,
    ExplicitVRLittleEndian,
    JPEG2000,
    JPEG2000Lossless,
    RLELossless,
    generate_uid,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from deid_app.redaction import redact_file  # noqa: E402
from make_fixtures import _base, save  # noqa: E402

logging.disable(logging.WARNING)
OUT = Path("/tmp/fixtures_syntaxes")
BOX = [(0.25, 0.25, 0.5, 0.5)]
failures = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


def mono16(ts, name):
    ds = _base(32, 32, generate_uid(), 1, name)
    ds.InstanceNumber = 1
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 12, 11, 0
    # Samples in the byte order the syntax demands: pydicom does not swap OW
    # bytes on write, and it masks reads to BitsStored, which would hide a
    # wrongly-ordered fixture.
    order = ">u2" if ts == ExplicitVRBigEndian else "<u2"
    ds.PixelData = np.full((32, 32), 1000, dtype=order).tobytes()
    ds.file_meta.TransferSyntaxUID = ts
    path = OUT / f"{name}.dcm"
    save(ds, path)
    return path


def roundtrip(src):
    dst = OUT / (src.stem + "_out.dcm")
    redact_file(src, dst, BOX)
    ds = pydicom.dcmread(dst)
    return ds, ds.pixel_array


if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir()

for ts, name in ((ExplicitVRBigEndian, "big_endian"),
                 (DeflatedExplicitVRLittleEndian, "deflated"),
                 (ExplicitVRLittleEndian, "little_endian")):
    print(name)
    ds, arr = roundtrip(mono16(ts, name))
    check(int(arr[8:24, 8:24].max()) == 0, f"{name}: box blanked")
    check(int(arr[0, 0]) == 1000 and int(arr[31, 31]) == 1000,
          f"{name}: samples outside the box intact (got {arr[0, 0]})")
    if ts == ExplicitVRBigEndian:
        check(ds.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian,
              "big endian input is rewritten as Explicit VR Little Endian")
    else:
        check(ds.file_meta.TransferSyntaxUID == ts, f"{name}: transfer syntax kept")

def smooth_multiframe(name, frames=6, size=128):
    """A compressible 16-bit multiframe image: a smooth ramp, different per frame."""
    ds = _base(size, size, generate_uid(), 1, name)
    ds.InstanceNumber = 1
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 12, 11, 0
    ds.NumberOfFrames = frames
    ramp = np.add.outer(np.arange(size), np.arange(size)).astype("<u2") * 8 + 100
    ds.PixelData = np.stack([ramp + 20 * f for f in range(frames)]).tobytes()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    return ds


for label, target in (("rle", RLELossless),
                      ("jpeg2000 lossless", JPEG2000Lossless)):
    print(label)
    src = smooth_multiframe(label.replace(" ", "_"))
    expected = src.pixel_array.copy()
    uncompressed = len(src.PixelData)
    compress(src, target)
    path = OUT / f"{label.replace(' ', '_')}.dcm"
    src.save_as(path, enforce_file_format=True)
    ds, arr = roundtrip(path)
    check(ds.file_meta.TransferSyntaxUID == target, f"{label}: written as {target.name}")
    box = arr[:, 32:96, 32:96]
    check(int(box.max()) == 0, f"{label}: box blanked in every frame")
    rest = arr.copy()
    rest[:, 32:96, 32:96] = expected[:, 32:96, 32:96]
    check(np.array_equal(rest, expected), f"{label}: samples outside the box bit-exact")
    size = path.with_name(path.stem + "_out.dcm").stat().st_size
    check(size < uncompressed * 0.75, f"{label}: output ({size} B) well below uncompressed ({uncompressed} B)")
    check(ds.pixel_array.shape == expected.shape, f"{label}: frame count kept")

print("jpeg2000 (lossy source)")
src = smooth_multiframe("j2k_lossy")
compress(src, JPEG2000, j2k_cr=[5])
path = OUT / "j2k_lossy.dcm"
src.save_as(path, enforce_file_format=True)
ds, arr = roundtrip(path)
check(ds.file_meta.TransferSyntaxUID == JPEG2000Lossless, "lossy JPEG 2000 comes back as JPEG 2000 Lossless")
check(int(arr[:, 32:96, 32:96].max()) == 0, "j2k lossy: box blanked")

print("palette color")
ds = _base(32, 32, generate_uid(), 1, "palette")
ds.InstanceNumber = 1
ds.SamplesPerPixel = 1
ds.PhotometricInterpretation = "PALETTE COLOR"
ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 8, 8, 7, 0
descriptor = [256, 0, 8]
# Entry 0 is white, entry 5 mid-grey, entry 9 black, everything else light:
# the darkest is 9, not 0.
table = bytearray([200] * 256)
table[0], table[5], table[9] = 255, 120, 0
for colour in ("Red", "Green", "Blue"):
    setattr(ds, f"{colour}PaletteColorLookupTableDescriptor", descriptor)
    setattr(ds, f"{colour}PaletteColorLookupTableData", bytes(table))
ds.PixelData = np.full((32, 32), 5, dtype=np.uint8).tobytes()
pal = OUT / "palette.dcm"
save(ds, pal)
ds, arr = roundtrip(pal)
check(int(arr[16, 16]) == 9, f"box uses the darkest palette entry (index {arr[16, 16]})")
rgb = apply_color_lut(arr, ds)
check(tuple(int(v) for v in rgb[16, 16]) == (0, 0, 0), f"which displays black (got {rgb[16, 16]})")
check(int(arr[0, 0]) == 5, "pixels outside the box untouched")
check(str(ds.PhotometricInterpretation) == "PALETTE COLOR", "palette kept, not converted")

print()
print("ALL SYNTAX CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
