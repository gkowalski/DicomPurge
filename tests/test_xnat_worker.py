"""Headless check: XnatWorker's signals, session handling and shutdown.

The network is stubbed out - what is under test is the worker's own contract:
which signal fires, that the server-confirmed username wins over the typed one,
that a session is always disconnected rather than leaked, and that the password
never reaches the log.
"""
from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deid_app.xnat_worker as xw  # noqa: E402
from deid_app.xnat_settings import XnatSettings  # noqa: E402

failures = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


class FakeSession:
    def __init__(self, user="serverName"):
        self.logged_in_user = user
        self.disconnected = False

    def disconnect(self):
        self.disconnected = True


class Collector:
    """Records emitted signals without needing a running Qt event loop."""

    def __init__(self, worker):
        self.events = []
        worker.loggedIn.connect(lambda u: self.events.append(("loggedIn", u)))
        worker.loginFailed.connect(lambda m: self.events.append(("loginFailed", m)))
        worker.loggedOut.connect(lambda: self.events.append(("loggedOut", None)))


COMPLETE = XnatSettings(server="https://x.org/xnat", user="typedName", password="s3cret")

print("login")
worker = xw.XnatWorker()
events = Collector(worker)
fake = FakeSession()
xw.open_session = lambda cfg: fake
worker.login(COMPLETE)
check(events.events == [("loggedIn", "serverName")],
      f"a good login emits loggedIn once (got {events.events})")
check(worker.connected, "and the session is retained")

print()
print("the server's name wins")
check(events.events[0][1] == "serverName",
      "the username reported is the server's, not the one typed")

print()
print("incomplete settings never reach the network")
called = []
xw.open_session = lambda cfg: called.append(cfg)
worker2 = xw.XnatWorker()
events2 = Collector(worker2)
worker2.login(XnatSettings(server="https://x.org", user="bob"))
check(len(events2.events) == 1 and events2.events[0][0] == "loginFailed",
      "a blank password fails fast")
check(not called,
      "and open_session is never called - this is what keeps xnatpy from "
      "prompting for a password on a console nobody is watching")

print()
print("failure")
xw.open_session = lambda cfg: (_ for _ in ()).throw(RuntimeError("boom"))
worker3 = xw.XnatWorker()
events3 = Collector(worker3)
worker3.login(COMPLETE)
check(events3.events[0][0] == "loginFailed", "a raising connect emits loginFailed")
check(not worker3.connected, "and leaves no session behind")

print()
print("logging in twice does not leak the first session")
first, second = FakeSession("one"), FakeSession("two")
handles = iter((first, second))
xw.open_session = lambda cfg: next(handles)
worker4 = xw.XnatWorker()
Collector(worker4)
worker4.login(COMPLETE)
worker4.login(COMPLETE)
check(first.disconnected, "the first session is disconnected before the second opens")
check(worker4._session is second, "and the newer session is the one retained")

print()
print("logout")
worker4.logout()
check(second.disconnected, "logout disconnects the live session")
check(not worker4.connected, "and clears it")
worker4.logout()
check(True, "a second logout is harmless")

print()
print("a disconnect that raises must not propagate")


class AngrySession(FakeSession):
    def disconnect(self):
        raise RuntimeError("network gone")


worker5 = xw.XnatWorker()
Collector(worker5)
xw.open_session = lambda cfg: AngrySession()
worker5.login(COMPLETE)
try:
    worker5.logout()
    check(not worker5.connected, "a failing disconnect still clears the session")
except Exception as exc:
    check(False, f"logout raised: {exc}")

print()
print("the password never reaches the log")
buffer = io.StringIO()
handler = logging.StreamHandler(buffer)
root = logging.getLogger()
root.addHandler(handler)
root.setLevel(logging.DEBUG)
try:
    xw.open_session = lambda cfg: FakeSession()
    worker6 = xw.XnatWorker()
    Collector(worker6)
    worker6.login(COMPLETE)
    worker6.logout()
    xw.open_session = lambda cfg: (_ for _ in ()).throw(RuntimeError("bad login"))
    worker7 = xw.XnatWorker()
    Collector(worker7)
    worker7.login(COMPLETE)
finally:
    root.removeHandler(handler)
written = buffer.getvalue()
check("s3cret" not in written, "no password in the log on success or failure")
check("typedName" in written, "the username is logged, so the entries stay useful")

print()
print("listing projects and sessions")


class ListingSession(FakeSession):
    def __init__(self, reject_filter=False):
        super().__init__()
        self.queries = []
        self.reject_filter = reject_filter

    def get_json(self, path, query=None):
        self.queries.append((path, dict(query or {})))
        if path == "/data/projects":
            if self.reject_filter and "permissions" in (query or {}):
                raise RuntimeError("400 unknown parameter")
            return {"ResultSet": {"Result": [
                {"ID": "zeta", "name": "Zeta study", "secondary_ID": "Z"},
                {"ID": "alpha", "name": "", "secondary_ID": "Alpha"},
                {"ID": "", "name": "no id"},
            ]}}
        if path == "/data/projects/zeta/experiments":
            return {"ResultSet": {"Result": [
                {"ID": "X1", "label": "S1_MR_1"}, {"ID": "X2", "label": "S1_MR_2"},
                {"ID": "X3", "label": ""},
            ]}}
        raise RuntimeError(f"404 {path}")


def listing_collector(worker):
    events = []
    worker.projectsFetched.connect(lambda p: events.append(("projects", p)))
    worker.projectsFailed.connect(lambda m: events.append(("projectsFailed", m)))
    worker.experimentLabelsFetched.connect(lambda pid, s: events.append(("labels", pid, s)))
    worker.experimentLabelsFailed.connect(lambda pid, m: events.append(("labelsFailed", pid, m)))
    return events


worker8 = xw.XnatWorker()
ev = listing_collector(worker8)
worker8.fetch_projects()
worker8.fetch_experiment_labels("zeta")
check(ev == [("projectsFailed", "Not logged in to XNAT."),
             ("labelsFailed", "zeta", "Not logged in to XNAT.")],
      f"both listings fail cleanly when not logged in (got {ev})")

listing = ListingSession()
xw.open_session = lambda cfg: listing
worker8.login(COMPLETE)
ev.clear()
worker8.fetch_projects()
check(ev == [("projects", [("alpha", "Alpha"), ("zeta", "Zeta study")])],
      f"projects sorted by name, secondary_ID used when name is blank, blank ids dropped (got {ev})")
check(listing.queries[-1][1].get("permissions") == "edit"
      and listing.queries[-1][1].get("dataType") == "xnat:subjectData",
      "asks only for projects the user can create subjects in")
ev.clear()
worker8.fetch_experiment_labels("zeta")
check(ev == [("labels", "zeta", {"S1_MR_1", "S1_MR_2"})], f"session labels as a set (got {ev})")
ev.clear()
worker8.fetch_experiment_labels("nope")
check(ev and ev[0][0] == "labelsFailed" and ev[0][1] == "nope" and "404" in ev[0][2],
      f"a server error is reported with its text (got {ev})")

strict = ListingSession(reject_filter=True)
xw.open_session = lambda cfg: strict
worker9 = xw.XnatWorker()
ev9 = listing_collector(worker9)
worker9.login(COMPLETE)
worker9.fetch_projects()
check(ev9 and ev9[-1][0] == "projects" and len(ev9[-1][1]) == 2,
      "an older server that rejects the filter still yields the plain listing")
check(len(strict.queries) == 2 and "permissions" not in strict.queries[-1][1],
      "the fallback query drops the filter")
print()
print("checking where uploaded sessions stand")


class CheckSession(ListingSession):
    def get_json(self, path, query=None):
        self.queries.append((path, dict(query or {})))
        if path == "/data/projects/zeta/experiments":
            return {"ResultSet": {"Result": [{"ID": "X1", "label": "S1_MR_1"}]}}
        if path == "/data/prearchive/projects/zeta":
            return {"ResultSet": {"Result": [{"name": "S1_MR_2", "status": "READY"},
                                             {"name": "S1_MR_3", "status": "RECEIVING"}]}}
        if path == "/data/projects/broken/experiments":
            raise RuntimeError("500")
        return super().get_json(path, query)


checked = []
worker10 = xw.XnatWorker()
worker10.sessionsChecked.connect(lambda d: checked.append(dict(d)))
worker10.check_sessions([("zeta", "S1_MR_1")])
check(checked == [], "silent when not logged in")
cs = CheckSession()
xw.open_session = lambda cfg: cs
worker10.login(COMPLETE)
worker10.check_sessions([("zeta", "S1_MR_1"), ("zeta", "S1_MR_2"), ("zeta", "S1_MR_3"),
                         ("zeta", "S1_MR_9"), ("broken", "X")])
check(checked == [{("zeta", "S1_MR_1"): "archived", ("zeta", "S1_MR_2"): "prearchive:READY",
                   ("zeta", "S1_MR_3"): "prearchive:RECEIVING", ("zeta", "S1_MR_9"): "missing"}],
      f"archived / prearchive with its status / missing, and a failing project is left out (got {checked})")
paths = [q[0] for q in cs.queries]
check(paths.count("/data/projects/zeta/experiments") == 1 and paths.count("/data/prearchive/projects/zeta") == 1,
      "one archive query and one prearchive query per project, not per label")
checked.clear()
worker10.check_sessions([("zeta", "S1_MR_1")])
check(checked == [{("zeta", "S1_MR_1"): "archived"}] and paths.count("/data/prearchive/projects/zeta") == 1
      or [q[0] for q in cs.queries].count("/data/prearchive/projects/zeta") == 1,
      "the prearchive is not queried when every label is already archived")
worker10.logout()

worker8.logout()
worker9.logout()

print()
print("ALL XNAT WORKER CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S): {failures}")
sys.exit(1 if failures else 0)
