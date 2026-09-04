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

from .xnat_client import (
    EXPERIMENTS_COLUMNS,
    PROJECTS_QUERY,
    describe_connect_error,
    open_session,
    parse_experiment_labels,
    parse_projects,
)

log = logging.getLogger(__name__)


class XnatWorker(QObject):
    """Owns the XNATSession. Only ever touched from its own thread."""

    loggedIn = Signal(str)       # username, as confirmed by the server
    loginFailed = Signal(str)    # message already fit to show a user
    loggedOut = Signal()
    # Listings for the XNAT server tab. The session never leaves this thread;
    # only plain lists and sets cross to the GUI.
    projectsFetched = Signal(object)            # list[(id, name)]
    projectsFailed = Signal(str)
    experimentLabelsFetched = Signal(str, object)   # project id, set[str]
    experimentLabelsFailed = Signal(str, str)       # project id, message
    # Where uploaded sessions stand now: {(project, label): status}, status
    # being "archived", "prearchive:<XNAT status>" or "missing".
    sessionsChecked = Signal(object)

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
    def fetch_projects(self) -> None:
        """Projects the user can upload into, for the project drop-down."""
        if self._session is None:
            self.projectsFailed.emit("Not logged in to XNAT.")
            return
        try:
            try:
                payload = self._session.get_json("/data/projects", query=PROJECTS_QUERY)
            except Exception as exc:  # noqa: BLE001 - older servers reject the filter
                log.info("Filtered project listing failed (%s); listing all", exc)
                payload = self._session.get_json(
                    "/data/projects", query={"columns": PROJECTS_QUERY["columns"]}
                )
            projects = parse_projects(payload)
            log.info("XNAT lists %d project(s) available for upload", len(projects))
            self.projectsFetched.emit(projects)
        except Exception as exc:  # noqa: BLE001
            log.error("Could not list XNAT projects: %s", exc)
            self.projectsFailed.emit(f"Could not list projects: {exc}")

    @Slot(str)
    def fetch_experiment_labels(self, project_id: str) -> None:
        """Session labels already in a project, so defaults can avoid them."""
        if self._session is None:
            self.experimentLabelsFailed.emit(project_id, "Not logged in to XNAT.")
            return
        try:
            payload = self._session.get_json(
                f"/data/projects/{project_id}/experiments", query=EXPERIMENTS_COLUMNS
            )
            labels = parse_experiment_labels(payload)
            log.info("Project %s has %d session(s)", project_id, len(labels))
            self.experimentLabelsFetched.emit(project_id, labels)
        except Exception as exc:  # noqa: BLE001
            log.error("Could not list sessions in %s: %s", project_id, exc)
            self.experimentLabelsFailed.emit(project_id, f"Could not list sessions: {exc}")

    @Slot(object)
    def check_sessions(self, wanted) -> None:
        """Look up each (project, label) in the archive, then the prearchive.

        Feeds the periodic refresh of the uploads table. Silent on failure -
        a missed poll is nothing to bother the user about; the next one runs
        a minute later.
        """
        if self._session is None or not wanted:
            return
        wanted = [(str(p), str(l)) for p, l in wanted]
        result: dict[tuple[str, str], str] = {}
        for project in sorted({p for p, _ in wanted}):
            labels = {l for p, l in wanted if p == project}
            try:
                payload = self._session.get_json(
                    f"/data/projects/{project}/experiments", query=EXPERIMENTS_COLUMNS
                )
                archived = parse_experiment_labels(payload)
            except Exception as exc:  # noqa: BLE001
                log.debug("Archive check for %s failed: %s", project, exc)
                continue
            prearchive: dict[str, str] = {}
            if labels - archived:
                try:
                    payload = self._session.get_json(f"/data/prearchive/projects/{project}")
                    for row in payload["ResultSet"]["Result"]:
                        prearchive[str(row.get("name", ""))] = str(row.get("status", ""))
                except Exception as exc:  # noqa: BLE001
                    log.debug("Prearchive check for %s failed: %s", project, exc)
            for label in labels:
                if label in archived:
                    result[(project, label)] = "archived"
                elif label in prearchive:
                    result[(project, label)] = f"prearchive:{prearchive[label]}"
                else:
                    result[(project, label)] = "missing"
        log.debug("Session check: %s", result)
        self.sessionsChecked.emit(result)

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
