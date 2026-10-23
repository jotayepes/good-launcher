"""Aviso de logro conseguido encima del juego.

En el escritorio basta con la notificación del sistema. En el modo juego de
SteamOS (gamescope) esas notificaciones no se ven, así que se muestra un banner
propio marcado con GAMESCOPE_EXTERNAL_OVERLAY, la propiedad de X11 con la que
gamescope compone una ventana encima del juego sin quitarle el foco (la usan
mangoapp y otros overlays)."""

from __future__ import annotations

import ctypes
import ctypes.util

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from gogl.i18n import _

from . import theme

SHOW_MS = 6000


def mark_gamescope_overlay(win_id: int) -> bool:
    """Pone GAMESCOPE_EXTERNAL_OVERLAY=1 en la ventana. Solo con la plataforma xcb."""
    if QGuiApplication.platformName() != "xcb":
        return False
    lib = ctypes.util.find_library("X11")
    if not lib:
        return False
    try:
        x11 = ctypes.CDLL(lib)
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XInternAtom.restype = ctypes.c_ulong
        x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x11.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        dpy = x11.XOpenDisplay(None)
        if not dpy:
            return False
        atom = x11.XInternAtom(dpy, b"GAMESCOPE_EXTERNAL_OVERLAY", 0)
        value = ctypes.c_long(1)  # con formato 32, Xlib espera «long»
        XA_CARDINAL, PROP_MODE_REPLACE = 6, 0
        x11.XChangeProperty(dpy, win_id, atom, XA_CARDINAL, 32, PROP_MODE_REPLACE,
                            ctypes.byref(value), 1)
        x11.XFlush(dpy)
        x11.XCloseDisplay(dpy)
        return True
    except (OSError, AttributeError):
        return False


class AchievementToast(QWidget):
    def __init__(self, name: str, description: str, icon: str | None, overlay: bool):
        flags = (Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                 | Qt.WindowDoesNotAcceptFocus)
        if overlay:
            flags |= Qt.X11BypassWindowManagerHint
        super().__init__(None, flags)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        box = QFrame()
        box.setObjectName("Toast")
        box.setStyleSheet(f"#Toast {{ background: rgba(24, 22, 30, 235); border: 1px solid {theme.ACCENT};"
                          " border-radius: 14px; } #Toast QLabel { background: transparent; }")
        outer.addWidget(box)
        row = QHBoxLayout(box)
        row.setContentsMargins(14, 12, 18, 12)
        row.setSpacing(14)
        pic = QLabel()
        pm = QPixmap(icon) if icon else QPixmap()
        if not pm.isNull():
            pic.setPixmap(pm.scaled(56, 56, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            pic.setText("🏆")
            pic.setStyleSheet("font-size: 32pt;")
        pic.setFixedSize(56, 56)
        row.addWidget(pic)
        text = QVBoxLayout()
        text.setSpacing(2)
        head = QLabel(_("LOGRO CONSEGUIDO"))
        head.setStyleSheet(f"color: {theme.ACCENT}; font-size: 8pt; font-weight: 800; letter-spacing: 1px;")
        title = QLabel(name)
        title.setStyleSheet("font-size: 12pt; font-weight: 800; color: white;")
        text.addWidget(head)
        text.addWidget(title)
        if description:
            d = QLabel(description)
            d.setWordWrap(True)
            d.setStyleSheet("color: #c9c6d3; font-size: 9pt;")
            text.addWidget(d)
        row.addLayout(text, 1)
        self.setFixedWidth(400)
        self.adjustSize()
        if overlay:
            mark_gamescope_overlay(int(self.winId()))
        QTimer.singleShot(SHOW_MS, self.close)

    def popup(self, index: int = 0):
        screen = QGuiApplication.primaryScreen().availableGeometry()
        y = screen.top() + 24 + index * (self.height() + 10)
        self.move(QPoint(screen.right() - self.width() - 24, y))
        self.show()


_alive: list[AchievementToast] = []


def _visible(t: AchievementToast) -> bool:
    try:
        return t.isVisible()
    except RuntimeError:  # ya destruido
        return False


def show_achievement(name: str, description: str, icon: str | None, overlay: bool) -> None:
    """Varios logros seguidos se apilan hacia abajo."""
    _alive[:] = [t for t in _alive if _visible(t)]
    t = AchievementToast(name, description, icon, overlay)
    t.popup(len(_alive))
    _alive.append(t)


__all__ = ["show_achievement", "mark_gamescope_overlay"]
