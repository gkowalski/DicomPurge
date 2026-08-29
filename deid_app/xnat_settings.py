"""XNAT server connection settings, persisted in QSettings.

Kept apart from AppSettings on purpose: everything in AppSettings is a live
performance knob that `MainWindow._apply_settings` pushes onto running objects,
whereas these are credentials that are only ever read when a login is attempted.

The password is stored unencrypted, by explicit choice. On macOS that means a
readable plist under ~/Library/Preferences. Two rules follow from that and both
matter: the settings dialog says so out loud, and nothing here (or anywhere
else) ever hands the password to the logger.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import QSettings

log = logging.getLogger(__name__)

PASSWORD_WARNING = (
    "The password is stored unencrypted in this application's settings file."
)


@dataclass
class XnatSettings:
    server: str = ""
    user: str = ""
    password: str = ""

    @staticmethod
    def normalise_server(value: str) -> str:
        """Tidy a typed-in server URL: trim, drop trailing slashes, add a scheme.

        A bare hostname becomes https://. The XNAT Desktop Client does the same
        (renderer-process/login.js), but then silently retries over plain http://
        when https fails - which would put the password on the wire in the
        clear, so that half is deliberately not copied here.
        """
        value = (value or "").strip().rstrip("/")
        if value and not value.startswith(("http://", "https://")):
            value = "https://" + value
        return value

    @property
    def is_complete(self) -> bool:
        """Whether a login may be attempted at all.

        This gates the login button, but it is also a hard safety check: given a
        username and an empty password, xnat.connect() prompts for one on the
        console, which in a GUI would hang the worker thread with nothing on
        screen to explain why.
        """
        return bool(self.server and self.user and self.password)

    @classmethod
    def load(cls, settings: QSettings) -> "XnatSettings":
        defaults = cls()

        def as_text(key: str, fallback: str) -> str:
            value = settings.value(key, fallback)
            return str(value) if value is not None else fallback

        return cls(
            server=cls.normalise_server(as_text("xnat/server", defaults.server)),
            user=as_text("xnat/user", defaults.user).strip(),
            password=as_text("xnat/password", defaults.password),
        )

    def save(self, settings: QSettings) -> None:
        settings.setValue("xnat/server", self.server)
        settings.setValue("xnat/user", self.user)
        settings.setValue("xnat/password", self.password)
        # User and server only - never the password.
        log.info("XNAT settings saved for %s@%s", self.user or "(no user)", self.server)
