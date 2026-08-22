"""Build a synthetic DICOM tree (mono, RGB, multiframe, overlays, JPEG) for testing."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import (
    ExplicitVRLittleEndian,
    JPEGBaseline8Bit,
    generate_uid,
)


def _base(rows, cols, series_uid, series_num, desc, modality="OT"):
    ds = Dataset()
    ds.PatientName = "TEST^PATIENT"
    ds.PatientID = "TP001"
    ds.StudyInstanceUID = "1.2.3.4.5"
    ds.StudyDescription = "Synthetic study"
    ds.SeriesInstanceUID = series_uid
    ds.SeriesNumber = series_num
    ds.SeriesDescription = desc
    ds.Modality = modality
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    ds.SOPInstanceUID = generate_uid()
    ds.Rows = rows
    ds.Columns = cols
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = ds.SOPClassUID
    fm.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta = fm
    return ds


def add_overlay(ds, group, rows, cols, origin=(1, 1), frames=1):
    plane = np.ones((frames, rows, cols), dtype=np.uint8)  # every bit set
    packed = np.packbits(plane.reshape(-1), bitorder="little").tobytes()
    if len(packed) % 2:
        packed += b"\x00"
    ds.add_new((group, 0x0010), "US", rows)
    ds.add_new((group, 0x0011), "US", cols)
    ds.add_new((group, 0x0040), "CS", "G")
    ds.add_new((group, 0x0050), "SS", list(origin))
    ds.add_new((group, 0x0100), "US", 1)
    ds.add_new((group, 0x0102), "US", 0)
    ds.add_new((group, 0x0015), "IS", frames)
    ds.add_new((group, 0x3000), "OW", packed)


def save(ds, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(str(path), enforce_file_format=True)


def build(root: Path):
    rows = cols = 64

    # Series 1: monochrome, 3 instances, two overlay planes.
    uid1 = generate_uid()
    for i in range(3):
        ds = _base(rows, cols, uid1, 1, "Mono with overlays", "US")
        ds.InstanceNumber = i + 1
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = 8
        ds.BitsStored = 8
        ds.HighBit = 7
        ds.PixelRepresentation = 0
        arr = np.full((rows, cols), 200, dtype=np.uint8)
        ds.PixelData = arr.tobytes()
        add_overlay(ds, 0x6000, rows, cols)
        add_overlay(ds, 0x6002, 32, 32, origin=(1, 1))
        save(ds, root / "patientA" / "study1" / f"mono_{i}.dcm")

    # Series 2: RGB multiframe.
    uid2 = generate_uid()
    ds = _base(rows, cols, uid2, 2, "RGB multiframe", "XC")
    ds.InstanceNumber = 1
    ds.SamplesPerPixel = 3
    ds.PhotometricInterpretation = "RGB"
    ds.PlanarConfiguration = 0
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.NumberOfFrames = 4
    arr = np.full((4, rows, cols, 3), 180, dtype=np.uint8)
    ds.PixelData = arr.tobytes()
    save(ds, root / "patientA" / "study1" / "sub" / "rgb_mf.dcm")

    # Series 3: JPEG baseline (compressed, YBR_FULL_422) with an overlay.
    uid3 = generate_uid()
    ds = _base(rows, cols, uid3, 3, "JPEG compressed", "XC")
    ds.InstanceNumber = 1
    ds.SamplesPerPixel = 3
    ds.PhotometricInterpretation = "YBR_FULL_422"
    ds.PlanarConfiguration = 0
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    rgb = np.full((rows, cols, 3), 200, dtype=np.uint8)
    rgb[:, :, 1] = 150
    try:
        import io

        from PIL import Image
        from pydicom.encaps import encapsulate

        buf = io.BytesIO()
        Image.fromarray(rgb).save(buf, format="JPEG", quality=95, subsampling=0)
        ds.PhotometricInterpretation = "YBR_FULL_422"
        ds.file_meta.TransferSyntaxUID = JPEGBaseline8Bit
        ds.PixelData = encapsulate([buf.getvalue()])
        ds["PixelData"].is_undefined_length = True
    except Exception as exc:
        print(f"  (JPEG fixture skipped: {exc})")
        return uid1, uid2, None
    add_overlay(ds, 0x6000, rows, cols)
    save(ds, root / "patientB" / "jpeg.dcm")

    return uid1, uid2, uid3


if __name__ == "__main__":
    root = Path(sys.argv[1])
    build(root)
    print("fixtures written to", root)
