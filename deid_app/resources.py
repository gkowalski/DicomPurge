"""Bundled image resources (logo / window icon)."""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
IMAGES_DIR = PROJECT_ROOT / "images"
LOGO_PATH = IMAGES_DIR / "Scuppernong_logo_512x512.png"

_cache: dict[int, QPixmap] = {}


def logo_pixmap(height: int = 36) -> QPixmap | None:
    """The toolbar logo, smoothly scaled to `height`. None if the file is missing."""
    if height in _cache:
        return _cache[height]
    if not LOGO_PATH.is_file():
        log.warning("Logo not found at %s; the toolbar will show text only", LOGO_PATH)
        return None
    pixmap = QPixmap(str(LOGO_PATH))
    if pixmap.isNull():
        log.warning("Logo at %s could not be loaded as an image", LOGO_PATH)
        return None
    scaled = pixmap.scaledToHeight(height, Qt.SmoothTransformation)
    _cache[height] = scaled
    log.debug("Loaded logo from %s (%dx%d)", LOGO_PATH, scaled.width(), scaled.height())
    return scaled


def app_icon() -> QIcon:
    """Window / dock icon. Empty QIcon if the logo is missing."""
    if not LOGO_PATH.is_file():
        return QIcon()
    return QIcon(str(LOGO_PATH))
