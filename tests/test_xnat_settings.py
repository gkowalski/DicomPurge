"""Headless checks: XNAT URL normalisation, completeness, and error wording."""
from __future__ import annotations

import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xnat.exceptions import XNATLoginFailedError  # noqa: E402

from deid_app.xnat_client import (  # noqa: E402
    CONNECT_FAILED,
    SSL_FAILED,
    describe_connect_error,
)
from deid_app.xnat_settings import XnatSettings  # noqa: E402

failures = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


def http_error(status):
    exc = requests.exceptions.HTTPError(f"{status} error")
    exc.response = FakeResponse(status)
    return exc


print("normalise_server")
norm = XnatSettings.normalise_server
check(norm("https://x.org/xnat/") == "https://x.org/xnat", "strips a trailing slash")
check(norm("https://x.org/xnat///") == "https://x.org/xnat", "strips several")
check(norm("  https://x.org/xnat  ") == "https://x.org/xnat", "strips whitespace")
check(norm("x.org/xnat") == "https://x.org/xnat", "bare hostname becomes https")
check(norm("http://x.org/xnat") == "http://x.org/xnat", "an explicit http is kept")
check(norm("") == "", "empty stays empty")
check(norm(None) == "", "None is tolerated")

print()
print("is_complete")
check(not XnatSettings().is_complete, "blank settings are incomplete")
check(
    not XnatSettings(server="https://x.org", user="bob").is_complete,
    "a missing password is incomplete (this is what blocks xnatpy's console prompt)",
)
check(
    not XnatSettings(server="https://x.org", password="pw").is_complete,
    "a missing user is incomplete",
)
check(
    not XnatSettings(user="bob", password="pw").is_complete,
    "a missing server is incomplete",
)
check(
    XnatSettings(server="https://x.org", user="bob", password="pw").is_complete,
    "all three present is complete",
)

print()
print("describe_connect_error")
check(
    describe_connect_error(http_error(401)) == "Invalid username or password!",
    "401 blames the credentials",
)
check(
    describe_connect_error(http_error(403)) == "Invalid username or password!",
    "403 blames the credentials",
)
check(
    describe_connect_error(http_error(404)) == "Invalid XNAT server address!",
    "404 blames the address",
)
check(
    describe_connect_error(XNATLoginFailedError("nope"))
    == "Invalid username or password!",
    "xnatpy's own login failure blames the credentials",
)
check(
    describe_connect_error(requests.exceptions.SSLError("bad cert")) == SSL_FAILED,
    "an SSL error names the certificate",
)
check(
    describe_connect_error(requests.exceptions.ConnectionError("no route"))
    == CONNECT_FAILED,
    "a connection error suggests checking the address",
)
check(
    describe_connect_error(requests.exceptions.Timeout("slow")) == CONNECT_FAILED,
    "a timeout suggests checking the address",
)
check(
    describe_connect_error(RuntimeError("something odd")) == CONNECT_FAILED,
    "an unrecognised error still yields usable advice",
)
check(
    describe_connect_error(RuntimeError("server returned 401"))
    == "Invalid username or password!",
    "a status embedded in the message is still recognised",
)

print()
print("describe_connect_error: probe classification")
# xnatpy collapses 401 and 404 into one opaque XNATLoginFailedError with no
# status attached, so without a probe a wrong URL reads as a wrong password.
import deid_app.xnat_client as xc  # noqa: E402

cfg = XnatSettings(server="https://x.org/xnat", user="bob", password="pw")
original = xc._classify_by_probe
try:
    xc._classify_by_probe = lambda c: "Invalid XNAT server address!"
    check(
        xc.describe_connect_error(XNATLoginFailedError("unknown error"), cfg)
        == "Invalid XNAT server address!",
        "the probe can correct an opaque login failure to a bad address",
    )

    xc._classify_by_probe = lambda c: None
    check(
        xc.describe_connect_error(XNATLoginFailedError("unknown error"), cfg)
        == "Invalid username or password!",
        "an inconclusive probe falls back to bad credentials",
    )

    # Ordering guard: an unreachable host must be answered from the exception
    # alone. Probing it would sit through a second connection timeout.
    probed = []
    xc._classify_by_probe = lambda c: probed.append(c) or "Invalid XNAT server address!"
    check(
        xc.describe_connect_error(requests.exceptions.ConnectTimeout("timed out"), cfg)
        == CONNECT_FAILED,
        "a connect timeout is reported without probing",
    )
    check(not probed, "and the probe is genuinely not called for it")
finally:
    xc._classify_by_probe = original

check(
    describe_connect_error(XNATLoginFailedError("unknown error")) 
    == "Invalid username or password!",
    "with no settings to probe with, an opaque failure blames the credentials",
)

print()
print("ALL XNAT SETTINGS CHECKS PASSED" if not failures else f"{len(failures)} FAILURE(S): {failures}")
sys.exit(1 if failures else 0)
