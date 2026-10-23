"""El logo dibujado con QPainter (mismo diseño que desktop.ICON_SVG), sin depender
del módulo SVG de Qt: icono de ventana y PNG de la AppImage."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPolygonF

BG = "#121116"
ACCENT = "#8b6cf0"


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 64, size / 64)  # el SVG usa una caja de 64×64
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(BG))
    p.drawRoundedRect(QRectF(0, 0, 64, 64), 14, 14)

    def diamond(r: float, color: str):
        p.setBrush(QColor(color))
        p.drawPolygon(QPolygonF([QPointF(32, 32 - r), QPointF(32 + r, 32), QPointF(32, 32 + r),
                                 QPointF(32 - r, 32)]))

    diamond(22, ACCENT)
    diamond(10, BG)
    p.end()
    return img


def app_icon() -> QIcon:
    from PySide6.QtGui import QPixmap
    icon = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(QPixmap.fromImage(render(s)))
    return icon


def save_png(path: str, size: int = 256) -> None:
    if not render(size).save(path, "PNG"):
        raise OSError(f"no se pudo guardar {path}")


__all__ = ["app_icon", "render", "save_png"]
