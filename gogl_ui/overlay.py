"""Capas dentro de la ventana principal para usar con el mando.

En el modo juego de SteamOS, gamescope no da el foco a las ventanas aparte que
abre Qt (diálogos, menús, la lista de un desplegable): se ven, pero el mando no
puede moverse por ellas ni se ve dónde está. Con el mando, esas ventanas se
sustituyen por capas dentro de la ventana principal, que es la que sí tiene el
foco:

  embed(dlg)            un QDialog que se abre con exec() pasa a dibujarse encima
  choose(…), menu(…)    un desplegable o un menú se muestran como una lista de botones

La ventana que los acepta lo indica con el atributo `gamepad_overlays = True`."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QLabel, QMenu, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from . import theme

QSS = f"""
#GamepadOverlay {{ background: rgba(0, 0, 0, 170); }}
#OverlayScroll, #OverlayScroll > QWidget, #OverlayBody {{ background: transparent; border: none; }}
QDialog[overlay="true"] {{ background: {theme.BG}; border: 1px solid {theme.BORDER}; border-radius: 12px; }}
#Sheet {{ background: {theme.SURFACE}; border: 1px solid {theme.BORDER}; border-radius: 12px; }}
#Sheet QLabel {{ background: transparent; }}
QPushButton#SheetItem {{ text-align: left; padding: 10px 18px; background: transparent; border: none; }}
QPushButton#SheetItem:focus {{ background: {theme.ACCENT}; color: white; border: none; padding: 10px 18px; }}
QPushButton#SheetItem:checked {{ color: {theme.ACCENT_HOVER}; }}
QPushButton#SheetItem:checked:focus {{ color: white; }}
"""


def host_for(w: QWidget | None) -> QWidget | None:
    win = w.window() if w is not None else None
    return win if win is not None and getattr(win, "gamepad_overlays", False) and win.isVisible() else None


def top(win: QWidget | None) -> "Overlay | None":
    """La capa visible de más arriba de la ventana."""
    if win is None:
        return None
    layers = [o for o in win.findChildren(Overlay) if o.isVisible()]
    return layers[-1] if layers else None


class Overlay(QWidget):
    """Oscurece la ventana y muestra encima el contenido, centrado y con desplazamiento
    si no cabe. Mientras está, el mando solo se mueve por ella."""

    def __init__(self, host: QWidget):
        super().__init__(host)
        self.setObjectName("GamepadOverlay")
        self.setAttribute(Qt.WA_StyledBackground)
        self.setStyleSheet(QSS)
        self.on_back: Callable[[], None] = self.close_layer
        self.restore: QWidget | None = None  # adónde vuelve el foco al cerrarla
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("OverlayScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("OverlayBody")
        self.body = QVBoxLayout(body)
        self.body.setContentsMargins(24, 24, 24, 24)
        self.body.setAlignment(Qt.AlignCenter)
        self.scroll.setWidget(body)
        lay.addWidget(self.scroll)
        self._follow = _Follow(self)
        host.installEventFilter(self._follow)  # solo los eventos de esa ventana, no un filtro global
        self.setGeometry(host.rect())

    def set_content(self, w: QWidget):
        self.body.addWidget(w, 0, Qt.AlignCenter)
        self.show()
        self.raise_()

    def default_focus(self) -> QWidget | None:
        return None

    def close_layer(self):
        self.hide()
        self.parentWidget().removeEventFilter(self._follow)
        if self.restore is not None and self.restore.isVisible():
            self.restore.setFocus(Qt.TabFocusReason)
        self.deleteLater()


class _Follow(QObject):
    def __init__(self, layer: Overlay):
        super().__init__(layer)
        self.layer = layer

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Resize:
            self.layer.setGeometry(obj.rect())
        return False


# --- diálogos ----------------------------------------------------------------

def embed(dlg: QDialog) -> bool:
    """Si su ventana lo admite, el diálogo se dibuja dentro de ella en vez de abrirse
    aparte. Al cerrarse vuelve a su sitio (oculto), así que se puede seguir leyendo
    después de exec()."""
    parent = dlg.parentWidget()
    host = host_for(parent)
    if host is None or dlg.isWindow() is False or not getattr(dlg, "embeddable", True):
        return False
    from PySide6.QtWidgets import QApplication
    layer = Overlay(host)
    layer.restore = QApplication.focusWidget()
    flags = dlg.windowFlags()
    dlg.setWindowFlags(Qt.Widget)
    dlg.setProperty("overlay", True)
    dlg.setAttribute(Qt.WA_StyledBackground)
    layer.on_back = dlg.reject

    def done(_result):
        dlg.finished.disconnect(done)
        dlg.setParent(parent, flags)  # fuera de la capa antes de borrarla
        dlg.hide()
        layer.close_layer()

    dlg.finished.connect(done)
    layer.set_content(dlg)
    return True


# --- desplegables y menús ------------------------------------------------------

class Sheet(QWidget):
    def __init__(self, title: str):
        super().__init__()
        self.setObjectName("Sheet")
        self.setAttribute(Qt.WA_StyledBackground)
        self.setMinimumWidth(420)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(8, 12, 8, 12)
        self.lay.setSpacing(2)
        if title:
            t = QLabel(title)
            t.setStyleSheet("font-weight: 800; padding: 4px 18px 8px 18px;")
            self.lay.addWidget(t)
        self.items: list[QPushButton] = []


def choose(origin: QWidget, title: str, options: list[tuple[str, bool]], current: int | None,
           picked: Callable[[int], None]) -> bool:
    """Lista de opciones [(texto, activa)] encima de la ventana de `origin`."""
    host = host_for(origin)
    if host is None or not options:
        return False
    layer = Overlay(host)
    layer.restore = origin
    sheet = Sheet(title)
    for i, (text, enabled) in enumerate(options):
        b = QPushButton(text)
        b.setObjectName("SheetItem")
        b.setCheckable(i == current)
        b.setChecked(i == current)
        b.setEnabled(enabled)

        def click(_c=False, i=i):
            layer.close_layer()
            picked(i)

        b.clicked.connect(click)
        sheet.items.append(b)
        sheet.lay.addWidget(b)
    first = next((b for b in sheet.items if b.isEnabled()), None)
    layer.default_focus = lambda: (sheet.items[current] if current is not None
                                   and sheet.items[current].isEnabled() else first)
    layer.set_content(sheet)
    target = layer.default_focus()
    if target is not None:
        target.setFocus(Qt.TabFocusReason)
    return True


def combo(box: QComboBox) -> bool:
    return choose(box, "", [(box.itemText(i), True) for i in range(box.count())],
                  box.currentIndex() if box.currentIndex() >= 0 else None, box.setCurrentIndex)


def menu(button: QPushButton) -> bool:
    m: QMenu = button.menu()
    actions = [a for a in m.actions() if a.isVisible() and not a.isSeparator()]
    title = button.text().rstrip(" ▾")
    return choose(button, title, [(a.text(), a.isEnabled()) for a in actions], None,
                  lambda i: actions[i].trigger())
