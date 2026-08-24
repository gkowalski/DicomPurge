"""Headless check: redaction blanks pixel data and Graphics overlay bits (never ROI),
and export mirrors the tree."""
from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

import numpy as np
import pydicom

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deid_app.redaction import redact_file  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")

BOX = (0.25, 0.25, 0.25, 0.25)  # centre-ish quarter
OUT = Path("/tmp/fixtures_out")
IN = Path("/tmp/fixtures")

failures = []


def check(condition, message):
    if condition:
        print(f"  PASS  {message}")
    else:
        print(f"  FAIL  {message}")
        failures.append(message)


def overlay_planes(ds, group):
    rows = int(ds[(group, 0x0010)].value)
    cols = int(ds[(group, 0x0011)].value)
    frames = int(ds[(group, 0x0015)].value) if (group, 0x0015) in ds else 1
    bits = np.unpackbits(
        np.frombuffer(bytes(ds[(group, 0x3000)].value), dtype=np.uint8), bitorder="little"
    )[: frames * rows * cols]
    return bits.reshape(frames, rows, cols)


def main():
    if OUT.exists():
        shutil.rmtree(OUT)

    for src in sorted(IN.rglob("*.dcm")):
        rel = src.relative_to(IN)
        dst = OUT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        print(f"\n--- {rel} ---")
        summary = redact_file(src, dst, [BOX])
        print(f"  summary: {summary}")

        ds = pydicom.dcmread(str(dst))
        rows, cols = int(ds.Rows), int(ds.Columns)
        samples = int(getattr(ds, "SamplesPerPixel", 1) or 1)
        frames = int(getattr(ds, "NumberOfFrames", 1) or 1)
        arr = np.asarray(ds.pixel_array).reshape(frames, rows, cols, samples)

        r0, r1 = int(0.25 * rows), int(0.5 * rows)
        c0, c1 = int(0.25 * cols), int(0.5 * cols)

        inside = arr[:, r0:r1, c0:c1, :]
        check(int(inside.max()) == 0, f"pixel data blanked inside box (max={inside.max()})")

        outside = arr.copy()
        outside[:, r0:r1, c0:c1, :] = 0
        check(int(outside.max()) > 0, "pixel data outside the box preserved")

        check(str(ds.BurnedInAnnotation) == "NO", "BurnedInAnnotation set to NO")
        check(
            not ds.file_meta.TransferSyntaxUID.is_compressed,
            f"written uncompressed ({ds.file_meta.TransferSyntaxUID.name})",
        )
        if str(getattr(ds, "PhotometricInterpretation", "")).startswith("YBR"):
            check(False, "photometric interpretation left as YBR")

        original = pydicom.dcmread(str(src))
        for group in (0x6000, 0x6002, 0x6004):
            if (group, 0x3000) not in ds:
                continue
            if str(ds[(group, 0x0040)].value).strip().upper() != "G":
                check(
                    bytes(ds[(group, 0x3000)].value)
                    == bytes(original[(group, 0x3000)].value),
                    f"ROI overlay ({group:04X},3000) left untouched",
                )
                continue
            planes = overlay_planes(ds, group)
            o_rows, o_cols = planes.shape[1], planes.shape[2]
            orr0, orr1 = min(r0, o_rows), min(r1, o_rows)
            occ0, occ1 = min(c0, o_cols), min(c1, o_cols)
            region = planes[:, orr0:orr1, occ0:occ1]
            check(
                region.size == 0 or int(region.max()) == 0,
                f"overlay ({group:04X},3000) bits cleared inside box",
            )
            rest = planes.copy()
            rest[:, orr0:orr1, occ0:occ1] = 0
            check(int(rest.max()) == 1, f"overlay ({group:04X},3000) bits outside box kept")

    # Directory structure mirrored?
    src_rel = {p.relative_to(IN) for p in IN.rglob("*.dcm")}
    out_rel = {p.relative_to(OUT) for p in OUT.rglob("*.dcm")}
    print("\n--- structure ---")
    check(src_rel == out_rel, f"output mirrors input tree ({len(out_rel)} file(s))")

    print()
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
