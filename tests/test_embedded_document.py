"""Headless check: embedded documents never survive an export.

An embedded document (0042,0011) is a second, independent copy of the report
that redaction boxes cannot reach and that nothing in the UI displays. Two
distinct leaks are covered here, because a test that exercised only the
redaction path would miss the more likely one:

  * a hybrid file with NO boxes used to take the shutil.copy2 fast path, which
    copies the document through byte for byte;
  * a hybrid file WITH boxes went through save_as, which preserves every
    non-pixel tag.
"""
from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

import pydicom

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from deid_app.export import ExportWorker  # noqa: E402
from deid_app.model import ScanWorker  # noqa: E402
from make_fixtures import build_documents  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)-8s %(message)s")

IN = Path("/tmp/fixtures_docs")
OUT = Path("/tmp/fixtures_doc_out")
DOC_TAG, MIME_TAG = 0x00420011, 0x00420012

failures = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


def scan(root: Path):
    """Run ScanWorker synchronously.

    Deliberately NOT start_scan(): that moves the worker to a QThread, which
    makes its signals queued, so nothing would be delivered without an event
    loop running.
    """
    result = {}
    worker = ScanWorker(root)
    worker.finished.connect(lambda m: result.update(m=m))
    worker.run()
    return result.get("m", {})


def run_export(series_map, strip: bool):
    if OUT.exists():
        shutil.rmtree(OUT)
    done = {}
    worker = ExportWorker(list(series_map.values()), IN, OUT, strip_documents=strip)
    worker.finished.connect(
        lambda w, r, e, st, sk: done.update(
            written=w, redacted=r, errors=e, stripped=st, skipped=list(sk)
        )
    )
    worker.run()
    return done


def tags_of(path: Path):
    ds = pydicom.dcmread(str(path))
    return ds, (DOC_TAG in ds), (MIME_TAG in ds)


if IN.exists():
    shutil.rmtree(IN)
build_documents(IN)

series_map = scan(IN)
hybrid = doc_only = None
for s in series_map.values():
    for inst in s.instances:
        if inst.path.name == "hybrid_pdf.dcm": hybrid = (s, inst)
        if inst.path.name == "doc_only.dcm":   doc_only = (s, inst)

print("classification (from the header read the scan already does)")
check(hybrid is not None and doc_only is not None, "both fixtures were scanned")
check(hybrid[1].has_embedded_document, "hybrid flagged as carrying a document")
check(not hybrid[1].is_document_only, "hybrid NOT flagged document-only (it has pixels)")
check(doc_only[1].has_embedded_document, "doc-only flagged as carrying a document")
check(doc_only[1].is_document_only, "doc-only flagged document-only")
others = [i for s in series_map.values() for i in s.instances
          if i.path.name not in ("hybrid_pdf.dcm", "doc_only.dcm")]
check(others and not any(i.has_embedded_document for i in others),
      f"no false positives on the {len(others)} ordinary instance(s)")

print()
print("export with stripping ON, and NO boxes drawn (the shutil.copy2 leak)")
for s in series_map.values():
    s.boxes.clear()
res = run_export(series_map, strip=True)
out_hybrid = OUT / hybrid[1].path.relative_to(IN)
out_doc = OUT / doc_only[1].path.relative_to(IN)

check(out_hybrid.is_file(), "the hybrid was still exported")
ds, has_doc, has_mime = tags_of(out_hybrid)
check(not has_doc, "(0042,0011) EncapsulatedDocument is GONE")
check(not has_mime, "(0042,0012) MIMEType is GONE")
check("PixelData" in ds and ds.pixel_array.shape == (64, 64),
      "pixel data survived and still decodes")
check(res.get("stripped") == 1, f"reported 1 strip (got {res.get('stripped')})")

check(not out_doc.exists(), "the document-only file was NOT written")
check(res.get("skipped") == ["patientB/doc_only.dcm"],
      f"and is named in the skipped list (got {res.get('skipped')})")

leaks = [p for p in OUT.rglob("*") if p.is_file() and b"%PDF" in p.read_bytes()]
check(not leaks, f"no %PDF bytes anywhere in the export tree (found {leaks})")

print()
print("export with stripping ON and boxes drawn (the save_as path)")
hybrid[0].boxes.append((0.1, 0.1, 0.3, 0.3))
hybrid[0].committed = True
res2 = run_export(series_map, strip=True)
ds2, has_doc2, has_mime2 = tags_of(OUT / hybrid[1].path.relative_to(IN))
check(not has_doc2, "document still stripped when the file is also redacted")
check(not has_mime2, "MIME type still stripped")
check(res2.get("redacted", 0) >= 1, "and the redaction itself still happened")
leaks2 = [p for p in OUT.rglob("*") if p.is_file() and b"%PDF" in p.read_bytes()]
check(not leaks2, "still no %PDF bytes in the export tree")
hybrid[0].boxes.clear()
hybrid[0].committed = False

print()
print("export with stripping OFF - the opt-out must really opt out")
res3 = run_export(series_map, strip=False)
_, has_doc3, has_mime3 = tags_of(OUT / hybrid[1].path.relative_to(IN))
check(has_doc3 and has_mime3, "hybrid keeps its document when the setting is off")
check((OUT / doc_only[1].path.relative_to(IN)).is_file(),
      "document-only file is exported when the setting is off")
check(res3.get("stripped") == 0 and res3.get("skipped") == [],
      "nothing reported as stripped or skipped")
src_bytes = doc_only[1].path.read_bytes()
out_bytes = (OUT / doc_only[1].path.relative_to(IN)).read_bytes()
check(src_bytes == out_bytes, "and it is byte-identical to the input")

if OUT.exists():
    shutil.rmtree(OUT)
print()
print("ALL EMBEDDED-DOCUMENT CHECKS PASSED" if not failures
      else f"{len(failures)} FAILURE(S): {failures}")
sys.exit(1 if failures else 0)
