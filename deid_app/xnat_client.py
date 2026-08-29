"""Connecting to an XNAT server, with no Qt in sight.

Deliberately free of Qt so the error mapping can be unit-tested without a
QApplication. Everything thread-related lives in xnat_worker.py.
"""
from __future__ import annotations

import logging

import requests
import xnat
from xnat.exceptions import XNATAuthError, XNATLoginFailedError

from .xnat_settings import XnatSettings

log = logging.getLogger(__name__)

# xnatpy logs through whatever logger it is handed. Passing one explicitly stops
# it installing a StreamHandler of its own, so its output joins ours in the Log
# tab and ~/dicompurge.log.
xnat_log = logging.getLogger("deid_app.xnat.xnatpy")

# Wording lifted from the XNAT Desktop Client (assets/js/helpers.js), which has
# already had these messages in front of users.
CONNECT_ERRORS = {
    401: "Invalid username or password!",
    403: "Invalid username or password!",
    404: "Invalid XNAT server address!",
}
CONNECT_FAILED = (
    "Please check the XNAT server address (and your internet connection)."
)
SSL_FAILED = "The server's SSL certificate could not be verified."


def _status_code(exc: Exception) -> int | None:
    """The HTTP status behind an exception, if it carries one."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def _classify_by_probe(cfg: XnatSettings) -> str | None:
    """Ask the server directly what was wrong, returning None if that fails too.

    xnatpy collapses every HTTP failure into one opaque
    "Encountered a problem logging in: unknown error" carrying no status code,
    so a wrong password and a wrong URL are indistinguishable from the
    exception alone. This repeats the XNAT Desktop Client's own login request
    (GET /data/auth with basic auth, services/auth.js) purely to read the
    status off it.

    Only ever called after a connection that actually reached the server, so
    this costs one quick round trip and cannot hang on an unreachable host.
    """
    try:
        response = requests.get(
            f"{cfg.server}/data/auth",
            auth=(cfg.user, cfg.password),
            timeout=15.0,
            allow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001 - the probe is best-effort
        log.debug("Login probe failed: %s", exc)
        return None
    return CONNECT_ERRORS.get(response.status_code)


def describe_connect_error(exc: Exception, cfg: XnatSettings | None = None) -> str:
    """Turn an xnatpy/requests exception into something worth showing a user.

    Pass `cfg` to allow the probe above; without it an opaque xnatpy login
    failure can only be reported as bad credentials, which is the likelier of
    the two causes but not always the right one.
    """
    status = _status_code(exc)
    if status in CONNECT_ERRORS:
        return CONNECT_ERRORS[status]
    if isinstance(exc, requests.exceptions.SSLError):
        return SSL_FAILED
    # These carry a real distinction, so check them before the probe: the host
    # never answered, and probing it again would just wait out a second timeout.
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return CONNECT_FAILED
    if isinstance(exc, (XNATLoginFailedError, XNATAuthError)):
        if cfg is not None:
            probed = _classify_by_probe(cfg)
            if probed is not None:
                return probed
        return CONNECT_ERRORS[401]
    # Some xnatpy errors report the status only in their message text.
    text = str(exc)
    for code, message in CONNECT_ERRORS.items():
        if str(code) in text:
            return message
    return CONNECT_FAILED


def open_session(cfg: XnatSettings):
    """Authenticate and return a live XNATSession. Raises on failure.

    Callers must check `cfg.is_complete` first - see XnatSettings.is_complete
    for why an empty password would otherwise hang.
    """
    return xnat.connect(
        cfg.server,
        user=cfg.user,
        password=cfg.password,
        logger=xnat_log,
        default_timeout=300.0,
    )
