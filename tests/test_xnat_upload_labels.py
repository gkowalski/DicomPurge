"""Headless check: label sanitising, study grouping and default session labels.

No Qt and no server: this is the pure logic behind the XNAT server tab.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from deid_app.model import Instance, ScanWorker, Series  # noqa: E402
from deid_app.xnat_upload import (  # noqa: E402
    default_session_labels,
    group_studies,
    sanitise_label,
)
from make_fixtures import build  # noqa: E402

IN = Path("/tmp/fixtures_upload_labels")
failures = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


print("sanitise_label")
check(sanitise_label("TP001") == "TP001", "plain id unchanged")
check(sanitise_label(" TP 001^SMITH ") == "TP_001_SMITH", "spaces and ^ become single underscores")
check(sanitise_label("__a--") == "a", "leading/trailing punctuation dropped")
check(sanitise_label("") == "unknown", "empty becomes 'unknown'")
check(sanitise_label("José Müller") == "Jos_M_ller", "non-ASCII replaced")
check(len(sanitise_label("x" * 100)) == 64, "capped at 64 characters")
check(sanitise_label("a" * 63 + "_b") == "a" * 63, "cap never leaves a trailing underscore")

print()
print("group_studies on the shared fixtures")
if IN.exists():
    shutil.rmtree(IN)
build(IN)
result = {}
w = ScanWorker(IN)
w.finished.connect(lambda m: result.update(m=m))
w.run()
series_map = result["m"]
rows = group_studies(series_map)
check(len(rows) == 1, f"one study from the fixtures (got {len(rows)})")
row = rows[0]
check(row.patient_id == "TP001" and row.subject_label == "TP001", "subject label from PatientID")
check(row.modality == "US", f"modality from the lowest series number (got {row.modality})")
check(len(row.series) == 3 and row.file_count == 5, "all series and files grouped")
check(not row.ready and row.not_ready_count == 3, "unreviewed study is not ready")
for s in row.series:
    s.reviewed = True
check(row.ready, "ready once every series is reviewed")
row.series[0].reviewed = False
row.series[0].auto_skipped = True
check(row.ready, "a skipped series counts as ready, like export")


def fake_series(pid, name, study, num, modality):
    s = Series(series_uid=f"{pid}-{study}-{num}", patient_id=pid, patient_name=name,
               study_uid=study, study_description="", series_number=num,
               series_description="", modality=modality)
    s.instances.append(Instance(path=Path(f"/x/{s.series_uid}.dcm"), instance_number=1,
                                sop_uid="1", rows=1, columns=1))
    return s


print()
print("default_session_labels")
sm = {s.series_uid: s for s in [
    fake_series("P2", "B", "s1", 2, "CT"),
    fake_series("P2", "B", "s1", 1, "MR"),   # lower number -> MR wins
    fake_series("P2", "B", "s2", 1, "CT"),
    fake_series("P1", "A", "s3", 1, "US"),
    fake_series("P1", "A", "s4", 1, "US"),
    fake_series("P1", "A", "s5", 1, "US"),
]}
rows = group_studies(sm)
check([r.study_uid for r in rows] == ["s3", "s4", "s5", "s1", "s2"],
      f"studies ordered by patient name then study (got {[r.study_uid for r in rows]})")
check(rows[3].modality == "MR", "study s1 takes the modality of series 1, not 2")
labels = default_session_labels(rows)
check(labels == {"s3": "P1_US_1", "s4": "P1_US_2", "s5": "P1_US_3",
                 "s1": "P2_MR_1", "s2": "P2_CT_2"},
      f"per-subject counter, one sequence per subject (got {labels})")
labels = default_session_labels(rows, existing={"P1_US_1", "P1_US_3", "P2_MR_1"})
check(labels["s3"] == "P1_US_2" and labels["s4"] == "P1_US_4" and labels["s5"] == "P1_US_5",
      f"labels already on the server are skipped over (got {labels})")
check(labels["s1"] == "P2_MR_2" and labels["s2"] == "P2_CT_3",
      "the counter keeps going after a skipped label")
sm2 = {s.series_uid: s for s in [fake_series("A B", "n", "s1", 1, "X Y")]}
check(default_session_labels(group_studies(sm2)) == {"s1": "A_B_X_Y_1"},
      "both halves of the label are sanitised")

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED")
    sys.exit(1)
print("ALL LABEL CHECKS PASSED")
