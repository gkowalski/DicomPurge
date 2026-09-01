"""Headless check: series that boxes cannot de-identify are withheld and shown.

Three kinds of series can be withheld from an export: a Structured Report (text
in a ContentSequence, opt-in), an object that is entirely an embedded document
(on by default), and a file with no pixel data at all - a presentation state or
similar, shown as "Uneditable File" (opt-in). All three must report the same
"skipped" status so the tree tells one story, and none may block the export the
way an unreviewed image does.
"""
from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from deid_app.export import ExportWorker  # noqa: E402
from deid_app.main_window import STATUS_COLORS, STATUS_TEXT  # noqa: E402
from deid_app.model import ScanWorker  # noqa: E402
from make_fixtures import build_documents  # noqa: E402

logging.basicConfig(level=logging.ERROR, format="%(levelname)-8s %(message)s")

IN = Path("/tmp/fixtures_skip")
OUT = Path("/tmp/fixtures_skip_out")
failures = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


def scan(root: Path):
    result = {}
    worker = ScanWorker(root)
    worker.finished.connect(lambda m: result.update(m=m))
    worker.run()
    return result.get("m", {})


def mark_skipped(series_map, skip_sr: bool, strip_docs: bool, skip_no_image=False):
    """The same rule MainWindow._update_skipped_series applies."""
    for s in series_map.values():
        # auto_skipped, not skipped: the latter is now a read-only property
        # combining the automatic and manual reasons.
        s.auto_skipped = (
            (skip_sr and s.is_structured_report)
            or (strip_docs and s.has_document_only)
            or (skip_no_image and s.has_no_image_data)
        )


def run_export(series_map, skip_sr: bool, strip_docs: bool = True,
               skip_no_image: bool = False):
    if OUT.exists():
        shutil.rmtree(OUT)
    done = {}
    w = ExportWorker(list(series_map.values()), IN, OUT,
                     strip_documents=strip_docs, skip_structured_reports=skip_sr,
                     skip_files_without_image_data=skip_no_image)
    w.finished.connect(lambda wr, r, e, st, sk: done.update(
        written=wr, redacted=r, errors=e, stripped=st, skipped=list(sk)))
    w.run()
    return done


if IN.exists():
    shutil.rmtree(IN)
build_documents(IN)
series_map = scan(IN)

by_name = {}
for s in series_map.values():
    for i in s.instances:
        by_name[i.path.name] = s

print("status keys exist (direct dict lookups would KeyError otherwise)")
check("skipped" in STATUS_TEXT, "STATUS_TEXT has a 'skipped' entry")
check("skipped" in STATUS_COLORS, "STATUS_COLORS has a 'skipped' entry")
check(STATUS_TEXT["skipped"] == "skipped", "and it reads 'skipped'")
check(STATUS_COLORS["skipped"].name() == "#f57c00",
      f"and it is orange (got {STATUS_COLORS['skipped'].name()})")

print()
print("detection")
sr = by_name["structured_report.dcm"]
doc = by_name["doc_only.dcm"]
plain = by_name["plain.dcm"]
hybrid = by_name["hybrid_pdf.dcm"]
check(sr.is_structured_report, "the SR series is recognised")
check(doc.has_document_only, "the document-only series is recognised")
check(not plain.is_structured_report and not plain.has_document_only,
      "a plain image is neither")
ps = by_name["presentation_state.dcm"]
check(ps.has_no_image_data, "the presentation state has no image data")
check(not ps.is_structured_report and not ps.has_document_only,
      "and is neither an SR nor a document - it needs its own option")
check(not plain.has_no_image_data, "a plain image is not flagged as image-less")
check(not hybrid.has_no_image_data,
      "nor is the hybrid - it has real pixels behind the embedded document")
check(not hybrid.has_document_only,
      "a hybrid is NOT document-only - it has pixels and is still exportable")

print()
print("default settings: SR skipping OFF, document stripping ON")
mark_skipped(series_map, skip_sr=False, strip_docs=True)
check(not sr.skipped, "the SR is NOT withheld by default")
check(sr.status != "skipped", f"and its status is {sr.status!r}, not 'skipped'")
check(doc.skipped, "the document-only series IS withheld")
check(doc.status == "skipped", "and reports status 'skipped'")
check(doc.export_ready, "a withheld series counts as export-ready (allows Export)")
res = run_export(series_map, skip_sr=False)
check((OUT / sr.instances[0].path.relative_to(IN)).is_file(),
      "the SR is exported when the setting is off")
src = sr.instances[0].path.read_bytes()
out = (OUT / sr.instances[0].path.relative_to(IN)).read_bytes()
check(src == out, "and byte-identical to the input")

print()
print("SR skipping ON")
mark_skipped(series_map, skip_sr=True, strip_docs=True)
check(sr.skipped, "the SR is now withheld")
check(sr.status == "skipped", "and reports status 'skipped'")
check(sr.export_ready, "and no longer blocks the export")
res = run_export(series_map, skip_sr=True)
check(not (OUT / sr.instances[0].path.relative_to(IN)).exists(),
      "the SR file is NOT written")
check("patientB/structured_report.dcm" in res["skipped"],
      f"and is named in the skipped list (got {res['skipped']})")
check("patientB/doc_only.dcm" in res["skipped"],
      "the document-only file is still skipped too")
check((OUT / plain.instances[0].path.relative_to(IN)).is_file(),
      "the ordinary image is still exported")
check(res["stripped"] == 1, "and the hybrid still had its document stripped")

print()
print("files with no image data (the \"Uneditable File\" option)")
mark_skipped(series_map, skip_sr=False, strip_docs=True, skip_no_image=False)
check(not ps.skipped, "the presentation state is NOT withheld by default")
res = run_export(series_map, skip_sr=False, skip_no_image=False)
check((OUT / ps.instances[0].path.relative_to(IN)).is_file(),
      "and is exported when the option is off")

mark_skipped(series_map, skip_sr=False, strip_docs=True, skip_no_image=True)
check(ps.skipped, "the presentation state IS withheld when the option is on")
check(ps.status == "skipped", "and reports status 'skipped'")
check(ps.export_ready, "and does not block the export")
check(not sr.skipped,
      "the SR is untouched by this option - it has its own checkbox")
res = run_export(series_map, skip_sr=False, skip_no_image=True)
check(not (OUT / ps.instances[0].path.relative_to(IN)).exists(),
      "the presentation state file is NOT written")
check("patientB/presentation_state.dcm" in res["skipped"],
      f"and is named in the skipped list (got {res['skipped']})")
check((OUT / plain.instances[0].path.relative_to(IN)).is_file(),
      "the ordinary image is still exported")
check((OUT / sr.instances[0].path.relative_to(IN)).is_file(),
      "and so is the SR, since its own option is off")

print()
print("all three options together")
mark_skipped(series_map, skip_sr=True, strip_docs=True, skip_no_image=True)
res = run_export(series_map, skip_sr=True, skip_no_image=True)
check(sorted(res["skipped"]) == ["patientB/doc_only.dcm",
                                 "patientB/presentation_state.dcm",
                                 "patientB/structured_report.dcm"],
      f"exactly the three unredactable files are skipped (got {sorted(res['skipped'])})")
check(res["written"] == 2 and res["stripped"] == 1,
      f"the 2 real images are written and the hybrid stripped (got {res})")

print()
print("manual skip from the series context menu")
mark_skipped(series_map, skip_sr=False, strip_docs=True)   # settings-driven only
plain.boxes.clear(); plain.committed = False; plain.reviewed = True
plain.boxes.append((0.1, 0.1, 0.3, 0.3))
check(plain.status == "pending", f"the image series starts pending (got {plain.status})")

plain.set_skipped()
check(plain.manually_skipped, "set_skipped() marks it manually skipped")
check(plain.boxes == [], "and discards the boxes that were on it")
check(plain.status == "skipped", "status becomes 'skipped'")
check(plain.export_ready, "and it no longer blocks the export")

# Problem 1: the settings recompute must not clobber a manual decision.
mark_skipped(series_map, skip_sr=True, strip_docs=True, skip_no_image=True)
check(plain.manually_skipped and plain.status == "skipped",
      "a settings recompute does NOT clear the manual skip")
mark_skipped(series_map, skip_sr=False, strip_docs=True)

# Problem 2: export must actually withhold it, not just colour it orange.
res = run_export(series_map, skip_sr=False)
check(not (OUT / plain.instances[0].path.relative_to(IN)).exists(),
      "the manually skipped file is NOT written")
check("patientB/plain.dcm" in res["skipped"],
      f"and is named in the skipped list (got {res['skipped']})")

plain.clear_manual_skip()
check(not plain.manually_skipped, "clear_manual_skip() undoes it")
check(plain.status == "clean",
      f"and the series returns to 'clean' (got {plain.status})")
res = run_export(series_map, skip_sr=False)
check((OUT / plain.instances[0].path.relative_to(IN)).is_file(),
      "the file is exported again once un-skipped")

print()
print("the two reasons compose")
mark_skipped(series_map, skip_sr=True, strip_docs=True)   # SR auto-skipped
sr.set_skipped()
check(sr.auto_skipped and sr.manually_skipped, "the SR is skipped both ways")
sr.clear_manual_skip()
check(sr.skipped and sr.auto_skipped,
      "clearing the manual flag leaves the settings-driven skip in place")
check(not sr.manually_skipped, "and the manual flag really is gone")

print()
print("skipped outranks review state")
sr.committed = True
check(sr.status == "skipped",
      f"a committed-but-skipped series still reads 'skipped' (got {sr.status})")
sr.committed = False

print()
print("the export gate: a skipped series must not block export")
# Everything unreviewed; the only non-ready series are the withheld ones.
for s in series_map.values():
    s.reviewed = s.committed = False
mark_skipped(series_map, skip_sr=True, strip_docs=True)
not_ready = [s for s in series_map.values() if not s.export_ready]
check(all(not s.skipped for s in not_ready),
      "no withheld series appears in the not-ready list")
check({n.series_description for n in not_ready} == {"Report page with embedded PDF",
                                                     "Ordinary image",
                                                     "Visage Presentation State"},
      f"only the genuinely unreviewed series block - the presentation state "
      f"among them, because its own option is off here "
      f"(got {sorted(n.series_description for n in not_ready)})")

for p in (IN, OUT):
    if p.exists():
        shutil.rmtree(p)
print()
print("ALL SKIPPED-SERIES CHECKS PASSED" if not failures
      else f"{len(failures)} FAILURE(S): {failures}")
sys.exit(1 if failures else 0)
