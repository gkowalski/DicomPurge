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
print("ALL XNAT WORKER CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S): {failures}")
sys.exit(1 if failures else 0)
