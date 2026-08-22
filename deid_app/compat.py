"""pydicom 2.x / 3.x compatibility shims."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

try:  # pydicom >= 3.0
    from pydicom.pixels import apply_modality_lut, apply_voi_lut, convert_color_space
except ImportError:  # pragma: no cover - pydicom 2.x
    from pydicom.pixel_data_handlers.util import (  # type: ignore
        apply_modality_lut,
        apply_voi_lut,
        convert_color_space,
    )

__all__ = ["apply_modality_lut", "apply_voi_lut", "convert_color_space"]
