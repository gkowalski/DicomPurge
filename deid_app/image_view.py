"""Interactive image canvas: displays a frame and lets the user draw redaction boxes."""
from __future__ import annotations

import logging

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

log = logging.getLogger(__name__)

MIN_BOX_PIXELS = 3.0  # ignore accidental click-drags smaller than this (screen px)
WHEEL_NOTCH = 120     # Qt reports one mouse-wheel detent as 120 eighths-of-a-degree

COMMITTED_FILL = QColor(0, 0, 0, 190)
COMMITTED_PEN = QColor(0, 200, 0)
DRAFT_FILL = QColor(0, 0, 0, 120)
DRAFT_PEN = QColor(60, 160, 255)
INVALID_PEN = QColor(220, 40, 40)
INVALID_FILL = QColor(220, 40, 40, 70)


class WheelAccumulator:
    """Turns a stream of wheel deltas into whole notches.

    High-resolution trackpads send many small deltas per flick; accumulating
    them means one flick advances one image instead of flying past.
    """

    def __init__(self) -> None:
        self._delta = 0

    def steps(self, delta: int) -> int:
        self._delta += delta
        steps = int(self._delta / WHEEL_NOTCH)
        self._delta -= steps * WHEEL_NOTCH
        return steps

    def reset(self) -> None:
        self._delta = 0


class ImageCanvas(QWidget):
    """Aspect-correct image display with rubber-band box selection.

    Boxes are emitted in *normalised* coordinates (fractions of the image), so
    they can be applied to any instance in the series regardless of its size.
    """

    boxAdded = Signal(float, float, float, float)
    boxDiscarded = Signal()
    stepRequested = Signal(int)  # +1 = next image, -1 = previous

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(320, 320)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)

        self._pixmap: QPixmap | None = None
        self._boxes: list[tuple[float, float, float, float]] = []
        self._editable = False
        self._placeholder = "Select a series in the tree to display it."

        self._drag_origin: QPointF | None = None
        self._drag_current: QPointF | None = None
        self._drag_valid = True
        self._wheel = WheelAccumulator()

        # The displayed image, already scaled to the widget. Rescaling a large
        # image smoothly costs real time, and paintEvent runs on every
        # mouse-move during a box drag, so the result is kept until the image
        # or the widget size changes.
        self._scaled: QPixmap | None = None
        self._scaled_key: tuple | None = None

    # -- public API ------------------------------------------------------
    def set_image(self, image: QImage | None) -> None:
        self._pixmap = QPixmap.fromImage(image) if image is not None else None
        self._scaled = None
        self._scaled_key = None
        self._cancel_drag()
        self.update()

    def set_placeholder(self, text: str) -> None:
        self._placeholder = text
        self.update()

    def set_boxes(self, boxes) -> None:
        self._boxes = list(boxes)
        self.update()

    def set_editable(self, editable: bool) -> None:
        self._editable = editable
        self.setCursor(Qt.CrossCursor if editable else Qt.ArrowCursor)
        if not editable:
            self._cancel_drag()
        self.update()

    def _scaled_pixmap(self, target: QRect) -> QPixmap:
        """The image scaled to `target`, reused across repaints."""
        size = target.size()
        key = (self._pixmap.cacheKey(), size.width(), size.height())
        if self._scaled is None or self._scaled_key != key:
            self._scaled = self._pixmap.scaled(
                size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation
            )
            self._scaled_key = key
        return self._scaled

    def resizeEvent(self, event) -> None:
        self._scaled = None
        self._scaled_key = None
        super().resizeEvent(event)

    # -- geometry --------------------------------------------------------
    def _image_rect(self) -> QRectF | None:
        """Where the image is painted inside the widget, preserving aspect ratio."""
        if self._pixmap is None or self._pixmap.isNull():
            return None
        widget_w = max(1, self.width())
        widget_h = max(1, self.height())
        img_w = self._pixmap.width()
        img_h = self._pixmap.height()
        scale = min(widget_w / img_w, widget_h / img_h)
        draw_w = img_w * scale
        draw_h = img_h * scale
        return QRectF(
            (widget_w - draw_w) / 2.0, (widget_h - draw_h) / 2.0, draw_w, draw_h
        )

    def _to_normalised(self, rect: QRectF) -> tuple[float, float, float, float]:
        image_rect = self._image_rect()
        assert image_rect is not None
        x = (rect.left() - image_rect.left()) / image_rect.width()
        y = (rect.top() - image_rect.top()) / image_rect.height()
        w = rect.width() / image_rect.width()
        h = rect.height() / image_rect.height()
        x = min(max(x, 0.0), 1.0)
        y = min(max(y, 0.0), 1.0)
        w = min(w, 1.0 - x)
        h = min(h, 1.0 - y)
        return (x, y, w, h)

    def _from_normalised(self, box) -> QRectF:
        image_rect = self._image_rect()
        assert image_rect is not None
        x, y, w, h = box
        return QRectF(
            image_rect.left() + x * image_rect.width(),
            image_rect.top() + y * image_rect.height(),
            w * image_rect.width(),
            h * image_rect.height(),
        )

    # -- mouse -----------------------------------------------------------
    def mousePressEvent(self, event) -> None:
        if not self._editable or event.button() != Qt.LeftButton:
            return
        image_rect = self._image_rect()
        pos = QPointF(event.position())
        if image_rect is None or not image_rect.contains(pos):
            log.debug("Ignoring press outside image area at %s", pos)
            return
        self._drag_origin = pos
        self._drag_current = pos
        self._drag_valid = True
        self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_origin is None:
            return
        image_rect = self._image_rect()
        pos = QPointF(event.position())
        self._drag_current = pos
        # Requirement: dragging outside the image turns the selection red.
        self._drag_valid = image_rect is not None and image_rect.contains(pos)
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._drag_origin is None or event.button() != Qt.LeftButton:
            return
        origin = self._drag_origin
        current = self._drag_current or origin
        valid = self._drag_valid
        self._cancel_drag()

        if not valid:
            log.info("Redaction box discarded: released outside the image area")
            self.boxDiscarded.emit()
            self.update()
            return

        rect = QRectF(origin, current).normalized()
        if rect.width() < MIN_BOX_PIXELS or rect.height() < MIN_BOX_PIXELS:
            log.debug("Redaction box discarded: too small (%.1fx%.1f)", rect.width(), rect.height())
            self.boxDiscarded.emit()
            self.update()
            return

        box = self._to_normalised(rect)
        if box[2] <= 0 or box[3] <= 0:
            self.boxDiscarded.emit()
            return
        log.info(
            "Redaction box added at x=%.4f y=%.4f w=%.4f h=%.4f (normalised)", *box
        )
        self.boxAdded.emit(*box)

    def wheelEvent(self, event) -> None:
        """Scroll through the series, exactly like dragging the slider below."""
        if self._pixmap is None:
            super().wheelEvent(event)
            return
        if self._drag_origin is not None:
            # Mid-selection: don't move the image out from under the box.
            event.accept()
            return

        delta = event.angleDelta().y() or event.angleDelta().x()
        if not delta:
            event.ignore()
            return

        steps = self._wheel.steps(delta)
        if steps:
            # Wheel up moves toward the start of the series; the slider below
            # the image is filtered to match (see MainWindow.eventFilter).
            self.stepRequested.emit(-steps)
        event.accept()

    def leaveEvent(self, event) -> None:
        if self._drag_origin is not None:
            self._drag_valid = False
            self.update()
        super().leaveEvent(event)

    def _cancel_drag(self) -> None:
        self._wheel.reset()
        self._drag_origin = None
        self._drag_current = None
        self._drag_valid = True

    # -- painting --------------------------------------------------------
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#1e1e1e"))

        image_rect = self._image_rect()
        if self._pixmap is None or image_rect is None:
            painter.setPen(QColor("#b0b0b0"))
            painter.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap, self._placeholder)
            return

        target = image_rect.toRect()
        painter.drawPixmap(target.topLeft(), self._scaled_pixmap(target))

        # Existing boxes for this series.
        painter.setPen(QPen(COMMITTED_PEN, 1.5))
        for box in self._boxes:
            rect = self._from_normalised(box)
            painter.fillRect(rect, COMMITTED_FILL)
            painter.drawRect(rect)

        # In-progress selection.
        if self._drag_origin is not None and self._drag_current is not None:
            rect = QRectF(self._drag_origin, self._drag_current).normalized()
            if self._drag_valid:
                painter.fillRect(rect, DRAFT_FILL)
                painter.setPen(QPen(DRAFT_PEN, 1.5, Qt.DashLine))
            else:
                painter.fillRect(rect, INVALID_FILL)
                painter.setPen(QPen(INVALID_PEN, 2.0, Qt.DashLine))
            painter.drawRect(rect)

        # Thin frame around the image extents.
        painter.setPen(QPen(QColor("#555555"), 1))
        painter.drawRect(image_rect.adjusted(0, 0, -1, -1))
