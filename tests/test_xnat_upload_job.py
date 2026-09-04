"""Headless check: one upload job against a fake session, no thread, no server.

What is under test is the contract of run_upload: the study is de-identified
into a temp directory (with the boxes really applied), the right import_ call
is made for each mode, the session is always disconnected, and the temp files
are gone afterwards whether the upload succeeded, failed or was cancelled.
"""
from __future__ import annotations

import logging
import shutil
import sys
import zipfile
from pathlib import Path

import numpy as np
import pydicom

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from deid_app.export import ExportOptions  # noqa: E402
from deid_app.model import ScanWorker  # noqa: E402
from deid_app.xnat_upload import (  # noqa: E402
    PHASE_ARCHIVING,
    PHASE_STAGING,
    PHASE_UPLOADING,
    PHASE_ZIPPING,
    UploadCancelled,
    UploadRequest,
    run_upload,
)
from make_fixtures import build, build_documents  # noqa: E402

logging.basicConfig(level=logging.ERROR, format="%(levelname)-8s %(message)s")

IN = Path("/tmp/fixtures_upload_job")
TMP = Path("/tmp/fixtures_upload_job_tmp")
failures = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


class FakeResponse:
    def __init__(self, text):
        self.text = text + "\r\n"
        self.status_code = 200


class FakeSession:
    """Stands in for an XNATSession: upload_stream, plus get_json for the
    archive poll. `archive_after` is how many polls until the label shows."""

    def __init__(self, version=(1, 8, 5), raise_on_call=None, routed_label=None,
                 archive_after=1):
        self.calls = []
        self.polls = 0
        self.archive_after = archive_after
        self.archived_labels = set()
        self.disconnected = False
        self.version = version
        self.xnat_version_tuple = version
        self.xnat_version = ".".join(map(str, version))
        self.raise_on_call = raise_on_call
        self.routed_label = routed_label

    def upload_stream(self, uri, stream, query=None, content_type=None, method="put",
                      timeout=None, **_):
        payload = b""
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            payload += chunk
        call = dict(query or {})
        call.update(_uri=uri, _method=method, _content_type=content_type, _timeout=timeout,
                    _bytes=payload, _len=len(stream) if hasattr(stream, "__len__") else None)
        self.calls.append(call)
        if self.raise_on_call is not None and len(self.calls) == self.raise_on_call:
            raise RuntimeError("server said no")
        q = query or {}
        label = self.routed_label or q.get("session", "")
        if q.get("Direct-Archive") == "true" and self.version >= (1, 8, 3):
            return FakeResponse(f"/xapi/direct-archive/{q['project']}/1.2.3.4/{label}")
        return FakeResponse(f"/data/prearchive/projects/{q['project']}/20260904_104707173/{label}")

    def get_json(self, path, query=None):
        self.polls += 1
        rows = []
        if self.polls >= self.archive_after:
            rows = [{"ID": "X", "label": lbl} for lbl in self.archived_labels]
        return {"ResultSet": {"Result": rows}}

    def disconnect(self):
        self.disconnected = True


# No real waiting in tests.
import deid_app.xnat_upload as xu  # noqa: E402
xu.ARCHIVE_POLL_SECONDS = 0
xu.time.sleep = lambda _s: None


def scan(root):
    result = {}
    w = ScanWorker(root)
    w.finished.connect(lambda m: result.update(m=m))
    w.run()
    return result["m"]


def request(series_map, zip_mode, job_id=1, options=ExportOptions()):
    return UploadRequest(job_id=job_id, project="PROJ", subject="TP001", session="TP001_US_1",
                         series=list(series_map.values()), input_root=IN, zip_mode=zip_mode,
                         temp_root=TMP, options=options, credentials="creds")


def run(req, session, cancel_after=None):
    phases = []
    count = {"n": 0}

    def progress(done, total, phase):
        if not phases or phases[-1] != phase:
            phases.append(phase)

    def is_cancelled():
        count["n"] += 1
        return cancel_after is not None and count["n"] > cancel_after

    result = run_upload(req, lambda creds: session, progress, is_cancelled)
    return result, phases


def temp_leftovers():
    return sorted(p.name for p in TMP.iterdir()) if TMP.exists() else []


if IN.exists():
    shutil.rmtree(IN)
if TMP.exists():
    shutil.rmtree(TMP)
uid1, uid2, uid3 = build(IN)
series_map = scan(IN)
mono = series_map[uid1]
mono.boxes.append((0.25, 0.25, 0.5, 0.5))
for s in series_map.values():
    s.reviewed = True

print("zip mode")
session = FakeSession(archive_after=3)
session.archived_labels.add("TP001_US_1")
result, phases = run(request(series_map, zip_mode=True), session)
check(session.polls == 3, f"waited for the session to appear in the project (polled {session.polls}x)")
check(result.where == xu.WHERE_ARCHIVED, f"reported as archived only once it appeared ({result.where})")
check(len(session.calls) == 1, f"exactly one import POST (got {len(session.calls)})")
call = session.calls[0]
check(call["_uri"] == "/data/services/import" and call["_method"] == "post", "POST /data/services/import")
check(call.get("import-handler") == "DICOM-zip", "DICOM-zip handler")
check(call.get("Direct-Archive") == "true" and call.get("Ignore-Unparsable") == "true",
      "Direct-Archive and Ignore-Unparsable requested")
check(call["_content_type"] == "application/zip", "sent as application/zip")
check((call.get("project"), call.get("subject"), call.get("session")) == ("PROJ", "TP001", "TP001_US_1"),
      "project/subject/session parameters set")
check(call.get("overwrite") == "none", "overwrite=none")
check("dest" not in call, "no dest: Direct-Archive decides")
check(call["_len"] == len(call["_bytes"]) and call["_len"] > 0,
      f"stream advertises its length so Content-Length is sent ({call['_len']} bytes)")
names = sorted(zipfile.ZipFile(__import__("io").BytesIO(call["_bytes"])).namelist())
check(names == ["patientA/study1/mono_0.dcm", "patientA/study1/mono_1.dcm",
                "patientA/study1/mono_2.dcm", "patientA/study1/sub/rgb_mf.dcm",
                "patientB/jpeg.dcm"], f"zip mirrors the input tree (got {names})")
zf = zipfile.ZipFile(__import__("io").BytesIO(call["_bytes"]))
ds = pydicom.dcmread(__import__("io").BytesIO(zf.read("patientA/study1/mono_0.dcm")))
arr = ds.pixel_array
inside = arr[16:48, 16:48]
check(int(inside.max()) == 0 and int(arr.max()) > 0,
      "the staged copy really has the box applied (pixels blanked inside, kept outside)")
check(str(ds.PatientName) == "TEST^PATIENT" and str(ds.PatientID) == "TP001",
      "zip mode leaves the headers as they are")
check(result.archived and result.uri.startswith("/xapi/direct-archive"),
      f"a direct-archive tracking URL is reported as archived, not as a failure ({result.uri})")
check(result.written == 5 and result.redacted == 3, f"counts: {result.written} written, {result.redacted} redacted")
check(session.disconnected, "the job's session is disconnected")
check(phases == [PHASE_STAGING, PHASE_ZIPPING, PHASE_UPLOADING, PHASE_ARCHIVING],
      f"phases in order (got {phases})")
check(temp_leftovers() == [], f"temp directory cleaned up (left {temp_leftovers()})")

print()
print("zip accepted but not yet archived")
xu.ARCHIVE_WAIT_SECONDS = 0
session = FakeSession(archive_after=99)
result, phases = run(request(series_map, zip_mode=True), session)
check(not result.archived and result.where == xu.WHERE_ACCEPTED,
      f"after the wait it is reported as accepted, not archived and not failed ({result.where})")
check(result.uri.startswith("/xapi/direct-archive"), "and still counts as finished with the URI")
xu.ARCHIVE_WAIT_SECONDS = 600

print()
print("zip mode on an old server")
session = FakeSession(version=(1, 7, 6))
result, _ = run(request(series_map, zip_mode=True), session)
check(not result.archived and result.uri.startswith("/data/prearchive"),
      "a pre-1.8.3 server leaves it in the prearchive and that is what is reported")

print()
print("individual mode")
session = FakeSession()
result, phases = run(request(series_map, zip_mode=False), session)
check(len(session.calls) == 5, f"one import POST per file (got {len(session.calls)})")
check(all(c.get("import-handler") == "gradual-DICOM" for c in session.calls), "gradual-DICOM handler")
check(all(c.get("dest") == "/prearchive/projects/PROJ" for c in session.calls),
      "dest=/prearchive/projects/PROJ (without it gradual-DICOM lands in Unassigned)")
check(all((c.get("project"), c.get("subject"), c.get("session")) == ("PROJ", "TP001", "TP001_US_1")
          for c in session.calls), "project/subject/session on every file")
check(all(c["_content_type"] == "application/dicom" for c in session.calls), "application/dicom")
check(all("Direct-Archive" not in c for c in session.calls), "no Direct-Archive")
check(all(c["_bytes"][128:132] == b"DICM" for c in session.calls), "each body is a DICOM file")
sent = [pydicom.dcmread(__import__("io").BytesIO(c["_bytes"])) for c in session.calls]
check(all(str(d.PatientName) == "TP001" and str(d.PatientID) == "TP001_US_1" for d in sent),
      "individual mode sets PatientName=subject and PatientID=session (XNAT's default routing) "
      f"(got {sorted({(str(d.PatientID), str(d.PatientName)) for d in sent})})")
check(all(str(pydicom.dcmread(str(i.path), stop_before_pixels=True).PatientName) == "TEST^PATIENT"
          for s in series_map.values() for i in s.instances), "the input files are untouched")
check(all(d.pixel_array is not None for d in sent), "the rewritten files still decode")
check(not result.archived and result.uri.startswith("/data/prearchive"), "reported as in prearchive")
check(phases == [PHASE_STAGING, PHASE_UPLOADING], f"phases (got {phases})")
check(session.disconnected and temp_leftovers() == [], "disconnected and cleaned up")

print()
print("failure part-way")
session = FakeSession(raise_on_call=2)
try:
    run(request(series_map, zip_mode=False), session)
    check(False, "import_ failure propagates")
except RuntimeError as exc:
    check("server said no" in str(exc), "import_ failure propagates with the server text")
check(session.disconnected, "disconnected even after a failure")
check(temp_leftovers() == [], f"temp directory removed after a failure (left {temp_leftovers()})")

print()
print("cancellation during staging")
session = FakeSession()
try:
    run(request(series_map, zip_mode=True), session, cancel_after=2)
    check(False, "cancel raises")
except UploadCancelled:
    check(True, "UploadCancelled raised")
check(session.calls == [] and not session.disconnected,
      "nothing was posted and no session was ever opened")
check(temp_leftovers() == [], "no temp files left behind by a cancelled job")

print()
print("nothing to upload")
docs = Path("/tmp/fixtures_upload_job_docs")
if docs.exists():
    shutil.rmtree(docs)
build_documents(docs)
doc_map = scan(docs)
only_sr = {k: v for k, v in doc_map.items() if v.is_structured_report}
req = UploadRequest(job_id=9, project="P", subject="S", session="L", series=list(only_sr.values()),
                    input_root=docs, zip_mode=True, temp_root=TMP,
                    options=ExportOptions(skip_structured_reports=True), credentials=None)
session = FakeSession()
try:
    run(req, session)
    check(False, "an all-skipped study fails before connecting")
except RuntimeError as exc:
    check("Nothing to upload" in str(exc), f"an all-skipped study fails before connecting ({exc})")
check(session.calls == [] and not session.disconnected, "no session opened for it")

print()
print("temp root inside the input tree")
req = request(series_map, zip_mode=True)
req.temp_root = IN / "tmp"
try:
    run(req, FakeSession())
    check(False, "refused")
except ValueError as exc:
    check("must not be the input directory" in str(exc), "refused with a clear message")

print()
print("routing mismatch is reported, not hidden")
import io as _io
buf = _io.StringIO()
h = logging.StreamHandler(buf)
logging.getLogger("deid_app.xnat_upload").addHandler(h)
logging.getLogger("deid_app.xnat_upload").setLevel(logging.WARNING)
session = FakeSession(routed_label="TP001")
result, _ = run(request(series_map, zip_mode=False), session)
logging.getLogger("deid_app.xnat_upload").removeHandler(h)
check(result.uri.endswith("/TP001"), "upload still reported as done, with the server's URI")
check("rather than the requested 'TP001_US_1'" in buf.getvalue(),
      "and a warning names both labels")

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED")
    sys.exit(1)
print("ALL UPLOAD-JOB CHECKS PASSED")
