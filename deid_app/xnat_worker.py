"""The XNAT session, living on a thread of its own.

Both xnat.connect() and XNATSession.disconnect() are blocking network calls, so
neither may run on the GUI thread. The session also has to outlive the login
call - the window keeps showing who is logged in, and the session has to be
closed properly on the way out - so one long-lived thread owns it for its whole
life, the same arrangement as FrameWorker and _frame_thread in main_window.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal, Slot

from .xnat_client import describe_connect_error, open_session

log = logging.getLogger(__name__)


class XnatWorker(QObject):
    """Owns the XNATSession. Only ever touched from its own thread."""

    loggedIn = Signal(str)       # username, as confirmed by the server
    loginFailed = Signal(str)    # message already fit to show a user
    loggedOut = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._session = None

    @property
    def connected(self) -> bool:
        return self._session is not None

    @Slot(object)
    def login(self, cfg) -> None:
        # Guards xnatpy's console password prompt, which would hang this thread
        # forever with nothing on screen to say why.
        if not cfg.is_complete:
            self.loginFailed.emit(
                "Set the XNAT server, user and password before logging in."
            )
            return

        # A second login replaces the first rather than leaking the old session.
        if self._session is not None:
            self._close_session()

        try:
            log.info("Connecting to XNAT at %s as %s", cfg.server, cfg.user)
            self._session = open_session(cfg)
            # The server's idea of who we are, which is not always what was
            # typed (case, aliases). That is what the button should show.
            user = getattr(self._session, "logged_in_user", None) or cfg.user
            log.info("XNAT login succeeded as %s", user)
            self.loggedIn.emit(str(user))
        except Exception as exc:  # noqa: BLE001 - every failure is reportable
            message = describe_connect_error(exc, cfg)
            # The exception text may echo the URL, never the password.
            log.error("XNAT login failed: %s (%s)", message, exc)
            self._session = None
            self.loginFailed.emit(message)

    @Slot()
    def logout(self) -> None:
        self._close_session()
        self.loggedOut.emit()

    def _close_session(self) -> None:
        """DELETE /data/JSESSION and join xnatpy's keep-alive thread."""
        if self._session is None:
            return
        try:
            self._session.disconnect()
            log.info("XNAT session closed")
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            log.warning("Error while closing the XNAT session: %s", exc)
        finally:
            self._session = None
