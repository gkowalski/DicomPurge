"""Offscreen check: the XNAT Login button needs BOTH settings and loaded images.

Nothing here talks to a server - the worker is never started. The point is the
gating and the labelling, which is what the user actually sees.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from deid_app.logging_setup import configure_logging  # noqa: E402
from deid_app.main_window import MainWindow  # noqa: E402
from deid_app.xnat_settings import XnatSettings  # noqa: E402

IN = Path("/tmp/fixtures")
failures = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


def pump(ms=200):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def main():
    app = QApplication(sys.argv)
    bridge = configure_logging()
    win = MainWindow(bridge)
    win.show()

    complete = XnatSettings(
        server="https://xnat.example.org/xnat", user="testUser", password="secret"
    )

    # 1. Neither precondition met.
    win.xnat_settings = XnatSettings()
    win._update_xnat_button()
    check(not win.xnat_login_button.isEnabled(), "greyed with no settings and no images")
    check(
        "XNAT Settings menu" in win.xnat_login_button.toolTip(),
        "the tooltip points at the menu when unconfigured",
    )

    # 2. Settings only - still greyed. The two preconditions are independent,
    #    so each has to be checked on its own.
    win.xnat_settings = complete
    win._update_xnat_button()
    check(
        not win.xnat_login_button.isEnabled(),
        "still greyed with settings but no images",
    )
    check(
        "input directory" in win.xnat_login_button.toolTip(),
        "the tooltip asks for a directory once configured",
    )

    # 3. Images only - still greyed.
    win.xnat_settings = XnatSettings()
    win._start_scan(IN)
    for _ in range(40):
        pump(100)
        if win.series_map:
            break
    check(bool(win.series_map), f"fixtures loaded ({len(win.series_map)} series)")
    win._update_xnat_button()
    check(
        not win.xnat_login_button.isEnabled(),
        "still greyed with images but no settings",
    )

    # 4. Both -> enabled.
    win.xnat_settings = complete
    win._update_xnat_button()
    check(win.xnat_login_button.isEnabled(), "enabled once both preconditions are met")
    check(
        win.xnat_login_button.text() == "XNAT Login",
        f"reads 'XNAT Login' before logging in (got {win.xnat_login_button.text()!r})",
    )

    # 5. An incomplete password must not enable it - that is the guard that
    #    keeps xnat.connect() away from its console password prompt.
    win.xnat_settings = XnatSettings(server="https://xnat.example.org", user="testUser")
    win._update_xnat_button()
    check(
        not win.xnat_login_button.isEnabled(),
        "a blank password leaves the button disabled",
    )

    # 6. The logged-in label, without touching the network.
    win.xnat_settings = complete
    win._on_xnat_logged_in("testUser")
    check(
        win.xnat_login_button.text() == "Logged in as testUser",
        f"shows the confirmed user (got {win.xnat_login_button.text()!r})",
    )
    check(
        not win.xnat_login_button.isEnabled(),
        "disabled while logged in - there is nothing left to click",
    )
    check(win.xnat_logout_action.isEnabled(), "Log Out becomes available")

    # 7. Logging out restores it.
    win._on_xnat_logged_out()
    check(win.xnat_login_button.text() == "XNAT Login", "logging out restores the label")
    check(win.xnat_login_button.isEnabled(), "and re-enables the button")
    check(not win.xnat_logout_action.isEnabled(), "and disables Log Out again")

    # 8. A rescan must drop the button back to disabled.
    win.xnat_settings = complete
    win._clear_series()
    check(
        not win.xnat_login_button.isEnabled(),
        "clearing the loaded series greys it again",
    )

    win.close()
    print()
    print("ALL XNAT BUTTON CHECKS PASSED" if not failures
          else f"{len(failures)} FAILURE(S): {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
