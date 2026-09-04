"""Offscreen check: the upload queue honours its concurrency limit.

A fake session whose import_ sleeps stands in for the server; the jobs still
stage the real fixture files. Under test: never more than max_concurrent jobs
run at once, waiting jobs start as slots free up, raising the limit starts
more at once, logging out drops what is waiting, and shutdown leaves no
threads behind.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import deid_app.xnat_upload_worker as uw  # noqa: E402
from deid_app.model import ScanWorker  # noqa: E402
from deid_app.xnat_upload import UploadRequest  # noqa: E402
from make_fixtures import build  # noqa: E402

logging.basicConfig(level=logging.ERROR, format="%(levelname)-8s %(message)s")

IN = Path("/tmp/fixtures_upload_mgr")
TMP = Path("/tmp/fixtures_upload_mgr_tmp")
failures = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


def pump(ms=100):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_until(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        pump(20)
    return pred()


class Tally:
    """Counts how many fake uploads are inside import_ at the same time."""

    def __init__(self):
        self.lock = threading.Lock()
        self.inside = 0
        self.peak = 0

    def enter(self):
        with self.lock:
            self.inside += 1
            self.peak = max(self.peak, self.inside)

    def leave(self):
        with self.lock:
            self.inside -= 1


tally = Tally()


class FakeResponse:
    text = "/xapi/direct-archive/P/1.2.3/L\r\n"
    status_code = 200


class FakeSession:
    xnat_version_tuple = (1, 8, 5)
    xnat_version = "1.8.5"

    def __init__(self, delay=0.3):
        self.delay = delay

    def upload_stream(self, uri, stream, **kw):
        tally.enter()
        try:
            stream.read()
            time.sleep(self.delay)
        finally:
            tally.leave()
        return FakeResponse()

    def get_json(self, path, query=None):
        label = path.rsplit("/", 1)[-1]
        return {"ResultSet": {"Result": [{"ID": "X", "label": "TP001_US_2"}, {"ID": "Y", "label": "L"}]
                              + [{"ID": "Z", "label": f"L{i}"} for i in range(50)]}}

    def disconnect(self):
        pass


app = QApplication.instance() or QApplication(sys.argv)
if IN.exists():
    shutil.rmtree(IN)
if TMP.exists():
    shutil.rmtree(TMP)
build(IN)
result = {}
w = ScanWorker(IN)
w.finished.connect(lambda m: result.update(m=m))
w.run()
series_map = result["m"]
for s in series_map.values():
    s.reviewed = True


def request(n):
    return UploadRequest(job_id=0, project="P", subject="S", session=f"L{n}",
                         series=list(series_map.values()), input_root=IN,
                         zip_mode=True, temp_root=TMP)


class Events:
    def __init__(self, manager):
        self.log = []
        manager.queued.connect(lambda j: self.log.append(("queued", j)))
        manager.started.connect(lambda j: self.log.append(("started", j)))
        manager.finished.connect(lambda j, r: self.log.append(("finished", j)))
        manager.failed.connect(lambda j, m: self.log.append(("failed", j, m)))
        manager.cancelled.connect(lambda j: self.log.append(("cancelled", j)))

    def of(self, kind):
        return [e[1] for e in self.log if e[0] == kind]


uw.open_session = lambda creds: FakeSession(delay=0.3)
before = set(threading.enumerate())

print("not logged in")
manager = uw.UploadManager(max_concurrent=2)
ev = Events(manager)
try:
    manager.enqueue(request(0))
    check(False, "enqueue refused when logged out")
except RuntimeError:
    check(True, "enqueue refused when logged out")

print()
print("five jobs, two at a time")
manager.set_credentials("creds")
ids = [manager.enqueue(request(i)) for i in range(1, 6)]
check(ids == [1, 2, 3, 4, 5], f"job ids assigned in order (got {ids})")
pump(50)
check(manager.running_count == 2 and manager.pending_count == 3,
      f"two running, three waiting (got {manager.running_count}/{manager.pending_count})")
check(ev.of("queued") == ids, "every job was announced as queued")
check(ev.of("started") == [1, 2], f"only the first two started (got {ev.of('started')})")
check(manager.busy, "manager reports busy")
ok = wait_until(lambda: len(ev.of("finished")) == 5, timeout=20)
check(ok, f"all five finish (got {ev.log})")
check(tally.peak == 2, f"never more than two uploads inside the POST at once (peak {tally.peak})")
check(ev.of("started") == ids, "the waiting jobs started as slots freed, in order")
check(not manager.busy and manager.running_count == 0, "idle afterwards")
pump(100)
check(sorted(p.name for p in TMP.iterdir()) == [], "no temp files left")

print()
print("raising the limit starts waiting jobs at once")
tally.peak = 0
ev.log.clear()
uw.open_session = lambda creds: FakeSession(delay=0.6)
ids = [manager.enqueue(request(i)) for i in range(10, 14)]
pump(50)
check(manager.running_count == 2, "two running at the old limit")
manager.set_max_concurrent(4)
pump(50)
check(manager.running_count == 4 and manager.pending_count == 0,
      f"all four running after the raise (got {manager.running_count}/{manager.pending_count})")
manager.set_max_concurrent(1)
pump(50)
check(manager.running_count == 4, "lowering the limit does not stop running jobs")
wait_until(lambda: len(ev.of("finished")) == 4, timeout=20)
check(tally.peak == 4, f"peak concurrency followed the raised limit ({tally.peak})")

print()
print("logging out drops the queue but not the running jobs")
ev.log.clear()
manager.set_max_concurrent(1)
ids = [manager.enqueue(request(i)) for i in range(20, 23)]
pump(50)
manager.set_credentials(None)
check(ev.of("cancelled") == ids[1:], f"the two waiting jobs are cancelled (got {ev.of('cancelled')})")
check(manager.running_count == 1, "the running one carries on")
wait_until(lambda: len(ev.of("finished")) == 1, timeout=20)
check(ev.of("finished") == [ids[0]], "and finishes")

print()
print("cancelling a waiting job, then a running one")
ev.log.clear()
manager.set_credentials("creds")
uw.open_session = lambda creds: FakeSession(delay=0.5)
a = manager.enqueue(request(30))
b = manager.enqueue(request(31))
pump(20)
manager.cancel(b)
check(ev.of("cancelled") == [b] and manager.pending_count == 0, "waiting job removed")
manager.cancel(a)
wait_until(lambda: any(e[1] == a for e in ev.log if e[0] in ("finished", "cancelled")), timeout=20)
kinds = [e[0] for e in ev.log if e[1] == a]
check("cancelled" in kinds or "finished" in kinds,
      f"a running job either stops early or completes its request ({kinds})")

print()
print("shutdown joins every thread")
manager.set_max_concurrent(2)
uw.open_session = lambda creds: FakeSession(delay=1.0)
manager.enqueue(request(40))
manager.enqueue(request(41))
manager.enqueue(request(42))
pump(50)
t0 = time.monotonic()
manager.shutdown(timeout_ms=5000)
check(manager.running_count == 0 and manager.pending_count == 0, "nothing running or waiting")
pump(200)
strays = [t for t in threading.enumerate() if t not in before and t.is_alive()
          and not t.daemon]
check(strays == [], f"no non-daemon threads left behind (got {[t.name for t in strays]})")
check(time.monotonic() - t0 < 5.0, "shutdown returned in bounded time")

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED")
    sys.exit(1)
print("ALL UPLOAD-MANAGER CHECKS PASSED")
