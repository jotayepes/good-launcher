"""Navegación con mando por la interfaz (la lectura del mando está en gamepad.py).

Botones:
  cruceta / stick izq.   mover el foco          A   activar     B   atrás
  stick dcho.            desplazar (en diálogos y menús, mover el foco)
  LB/RB  secciones
  LT/RT                  página arriba/abajo    X, Y, Start: según la ventana

Las ventanas pueden ofrecer, si les interesa:
  gamepad_back() -> bool          qué hacer con B cuando no hay diálogo ni menú
  gamepad_button(acción) -> bool  X, Y, LB, RB, Start, Select
  gamepad_default_focus() -> QWidget | None
"""

from __future__ import annotations

import os
import shutil
import subprocess

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSocketNotifier, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QKeyEvent
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QAbstractScrollArea,
                               QAbstractSpinBox, QApplication, QComboBox, QLineEdit, QMenu,
                               QPlainTextEdit, QPushButton, QScrollArea, QScrollBar,
                               QStackedWidget, QTextEdit, QWidget)

from . import gamepad, theme

REPEAT_DELAY, REPEAT_RATE = 380, 90  # ms
SCROLL_SPEED = 28  # px por tick a fondo

KEYS = {"up": Qt.Key_Up, "down": Qt.Key_Down, "left": Qt.Key_Left, "right": Qt.Key_Right}


def in_gamescope() -> bool:
    """Modo juego de SteamOS."""
    return bool(os.environ.get("GAMESCOPE_WAYLAND_DISPLAY")
                or os.environ.get("XDG_CURRENT_DESKTOP", "").lower() == "gamescope")


def in_steam() -> bool:
    """Lanzado desde Steam (modo juego o escritorio) o dentro de gamescope."""
    return bool(os.environ.get("SteamGameId") or os.environ.get("SteamAppId") or in_gamescope())


def open_keyboard() -> None:
    """Teclado en pantalla de Steam (solo existe si Steam está detrás)."""
    if in_steam() and shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", "steam://open/keyboard"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


def _send_key(w: QWidget, key) -> None:
    QApplication.sendEvent(w, QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))
    QApplication.sendEvent(w, QKeyEvent(QEvent.KeyRelease, key, Qt.NoModifier))


def _select_first(menu: QMenu) -> None:
    """Marca la primera opción, para que con el mando se vea dónde se está."""
    if menu.isVisible() and menu.activeAction() is None:
        first = next((a for a in menu.actions() if a.isEnabled() and not a.isSeparator() and a.isVisible()), None)
        if first is not None:
            menu.setActiveAction(first)


def global_rect(w: QWidget) -> QRect:
    if isinstance(w, QAbstractItemView) and w.hasFocus() and w.currentIndex().isValid():
        r = w.visualRect(w.currentIndex())
        if r.isValid():
            return QRect(w.viewport().mapToGlobal(r.topLeft()), r.size())
    return QRect(w.mapToGlobal(QPoint(0, 0)), w.size())


def focusable(win: QWidget) -> list[QWidget]:
    out = []
    for w in win.findChildren(QWidget):
        if not (w.isVisible() and w.isEnabled()):
            continue
        policy = w.focusPolicy()
        # Qt quita el foco por tabulador a los botones no marcados de un grupo
        # exclusivo (la barra lateral, los filtros); con el mando sí se visitan.
        if not (policy & Qt.TabFocus or (isinstance(w, QAbstractButton) and policy & Qt.ClickFocus)):
            continue
        if isinstance(w, QScrollBar) or w.window() is not win:
            continue
        if isinstance(w, QAbstractScrollArea) and not isinstance(w, (QAbstractItemView, QTextEdit,
                                                                    QPlainTextEdit)):
            continue  # el contenedor de una página: se visita lo de dentro
        if isinstance(w, (QTextEdit, QPlainTextEdit)) and not w.isReadOnly():
            continue
        p = w.parentWidget()
        if isinstance(p, (QComboBox, QAbstractSpinBox)):
            continue  # el editor interno de un desplegable
        out.append(w)
    return out


def pick(cur: QRect, cands: list[tuple[QWidget, QRect]], d: str,
         near=lambda w: True) -> QWidget | None:
    """El candidato más cercano en la dirección d (como el FocusFinder de Android):
    primero los que están «en el haz» (solapan en el otro eje), luego los de la
    zona `near`, luego la distancia."""
    best, best_key = None, None
    for w, r in cands:
        if d == "down":
            ok = r.top() > cur.top() and r.bottom() > cur.bottom()
            major, beam = r.top() - cur.bottom(), (r.left() <= cur.right() and r.right() >= cur.left())
            minor = abs(r.center().x() - cur.center().x())
        elif d == "up":
            ok = r.bottom() < cur.bottom() and r.top() < cur.top()
            major, beam = cur.top() - r.bottom(), (r.left() <= cur.right() and r.right() >= cur.left())
            minor = abs(r.center().x() - cur.center().x())
        elif d == "right":
            ok = r.left() > cur.left() and r.right() > cur.right()
            major, beam = r.left() - cur.right(), (r.top() <= cur.bottom() and r.bottom() >= cur.top())
            minor = abs(r.center().y() - cur.center().y())
        else:
            ok = r.right() < cur.right() and r.left() < cur.left()
            major, beam = cur.left() - r.right(), (r.top() <= cur.bottom() and r.bottom() >= cur.top())
            minor = abs(r.center().y() - cur.center().y())
        if not ok:
            continue
        major = max(major, 0)
        key = (0 if beam else 1, 0 if near(w) else 1, 13 * major * major + minor * minor)
        if best_key is None or key < best_key:
            best, best_key = w, key
    return best


def region(w: QWidget) -> QWidget | None:
    """Zona de la ventana a la que pertenece: una página de un QStackedWidget o un
    panel con nombre (la barra lateral)."""
    for p in [w, *_ancestors(w)]:
        parent = p.parentWidget()
        if p.objectName() == "Sidebar" or isinstance(parent, QStackedWidget):
            return p
    return None


def pick_in_window(w: QWidget, cur: QRect, cands: list[tuple[QWidget, QRect]], d: str) -> QWidget | None:
    """Arriba/abajo no salen de la zona (la barra lateral está a la izquierda, no
    «encima» de nada); izquierda/derecha prefieren la misma zona salvo que en la
    otra haya algo justo al lado."""
    reg = region(w)
    if reg is None:
        return pick(cur, cands, d)
    inside = lambda c: c is reg or reg.isAncestorOf(c)  # noqa: E731
    if d in ("up", "down"):
        return pick(cur, [(c, r) for c, r in cands if inside(c)], d)
    target = pick(cur, cands, d, inside)
    if target is not None and not inside(target) and isinstance(target, QAbstractButton):
        # Al entrar en la barra lateral, a la sección activa, no a la que caiga al lado.
        group = target.group()
        checked = group.checkedButton() if group is not None and group.exclusive() else None
        if checked is not None and checked.isVisible() and checked.isEnabled():
            return checked
    return target


def focus_widget(w: QWidget, origin: QRect | None = None) -> None:
    win = w.window()
    if win is not QApplication.activeWindow():
        # En gamescope un diálogo puede abrirse sin quedar activo: sin esto el foco
        # no llega a verse y cada pulsación volvía al control inicial.
        win.activateWindow()
    w.setFocus(Qt.TabFocusReason)
    if isinstance(w, QAbstractItemView):
        _enter_view(w, origin)
    for p in _ancestors(w):
        if isinstance(p, QScrollArea) and p.widget() is not None and p.widget().isAncestorOf(w):
            p.ensureWidgetVisible(w, 40, 60)
            break


def _ancestors(w: QWidget):
    p = w.parentWidget()
    while p is not None:
        yield p
        p = p.parentWidget()


def _enter_view(v: QAbstractItemView, origin: QRect | None) -> None:
    """Al entrar en una lista: el elemento visible más cercano a donde venía el foco."""
    model = v.model()
    if model is None or model.rowCount() == 0:
        return
    vp = v.viewport()
    cur = v.currentIndex()
    if cur.isValid() and v.visualRect(cur).intersects(vp.rect()):
        v.scrollTo(cur)
        return
    idx = None
    if origin is not None:
        p = vp.mapFromGlobal(origin.center())
        p = QPoint(min(max(p.x(), 4), vp.width() - 4), min(max(p.y(), 4), vp.height() - 4))
        idx = v.indexAt(p)
        if not idx.isValid():
            idx = v.indexAt(QPoint(min(max(p.x(), 4), vp.width() - 4), 8))
    if idx is None or not idx.isValid():
        idx = model.index(0, 0)
    v.setCurrentIndex(idx)
    v.scrollTo(idx)


FOCUS_QSS = f"""
QPushButton:focus {{ border: 2px solid {theme.ACCENT}; padding: 7px 15px; background: {theme.BORDER}; }}
QPushButton#Primary:focus {{ border: 3px solid white; padding: 8px 31px; background: {theme.ACCENT_HOVER}; }}
QPushButton#Ghost:focus {{ border: 2px solid {theme.ACCENT}; padding: 4px 8px; color: {theme.TEXT}; }}
QPushButton#NavButton:focus {{ border: 2px solid {theme.ACCENT}; padding: 7px 12px; color: {theme.TEXT};
                               background: {theme.SURFACE_2}; }}
QPushButton#Chip:focus {{ border: 2px solid {theme.ACCENT}; padding: 4px 13px; color: {theme.TEXT}; }}
QLineEdit:focus {{ border: 2px solid {theme.ACCENT}; padding: 6px 13px; }}
QComboBox:focus {{ border: 2px solid {theme.ACCENT}; padding: 5px 9px; background: {theme.SURFACE_2}; }}
QComboBox QAbstractItemView {{ selection-background-color: {theme.ACCENT}; selection-color: white; }}
QCheckBox:focus, QRadioButton:focus {{ color: white; background: {theme.SURFACE_2}; border-radius: 6px; }}
QTableWidget:focus {{ border: 2px solid {theme.ACCENT}; }}
QTextBrowser:focus, QPlainTextEdit:focus {{ border: 2px solid {theme.ACCENT}; border-radius: 8px; }}
"""


class Navigator(QObject):
    """Une los mandos al bucle de Qt. Uno por proceso: `install()`."""
    mode_changed = Signal(bool)  # True mientras se usa el mando (foco visible, ayudas)

    def __init__(self, app: QApplication, require_active: bool = True):
        super().__init__(app)
        self.app = app
        self.require_active = require_active
        self.devices: dict[str, tuple[gamepad.Device, QSocketNotifier]] = {}
        self.rejected: set[str] = set()
        self.active = False
        self.base_qss = app.styleSheet()
        self.repeat = QTimer(self)
        self.repeat.timeout.connect(self._repeat)
        self.repeating: str | None = None
        self.scroller = QTimer(self)
        self.scroller.setInterval(16)
        self.scroller.timeout.connect(self._scroll_tick)
        self.scan = QTimer(self)
        self.scan.setInterval(2000)
        self.scan.timeout.connect(self.rescan)
        self.scan.start()
        self.rescan()
        # Salir del modo mando al usar el ratón o el trackpad. Se mira la posición del
        # puntero en vez de filtrar los eventos de toda la aplicación: un filtro global
        # en Python hace caer Qt WebEngine (la página de inicio de sesión).
        self.cursor_at = QCursor.pos()
        self.mouse_watch = QTimer(self)
        self.mouse_watch.setInterval(400)
        self.mouse_watch.timeout.connect(self._mouse_moved)

    # --- dispositivos --------------------------------------------------------

    def rescan(self):
        paths = set(gamepad.candidates())
        for p in list(self.devices):
            if p not in paths:
                self._drop(p)
        self.rejected &= paths
        for p in paths - set(self.devices) - self.rejected:
            dev = gamepad.open_gamepad(p)
            if dev is None:
                self.rejected.add(p)
                continue
            n = QSocketNotifier(dev.fd, QSocketNotifier.Read, self)
            n.activated.connect(lambda *_a, path=p: self._readable(path))
            self.devices[p] = (dev, n)

    def _drop(self, path: str):
        dev, n = self.devices.pop(path)
        n.setEnabled(False)
        n.deleteLater()
        dev.close()
        self._stop_repeat()

    def _readable(self, path: str):
        if path not in self.devices:
            return
        dev = self.devices[path][0]
        actions = dev.read()
        if actions is None:
            self._drop(path)
            return
        # Un menú desplegable abierto también cuenta como «en primer plano»: en
        # gamescope, al abrirse (es otra ventana) la aplicación puede dejar de
        # figurar como activa y se perdían las pulsaciones dentro del menú.
        # Lo mismo con los diálogos modales.
        popup_open = QApplication.activePopupWidget() is not None or QApplication.activeModalWidget() is not None
        if self.require_active and not popup_open and self.app.applicationState() != Qt.ApplicationActive:
            dev.tr.reset()  # p. ej. mientras se juega: el mando es del juego
            self._stop_repeat()
            return
        for name, pressed in actions:
            self.handle(name, pressed)
        if any(abs(d[0].tr.scroll) > 0 for d in self.devices.values()):
            if not self.scroller.isActive():
                self.scroller.start()

    # --- modo mando ----------------------------------------------------------

    def set_active(self, on: bool):
        if on == self.active:
            return
        self.active = on
        self.app.setStyleSheet(self.base_qss + (FOCUS_QSS if on else ""))
        if on:
            self.cursor_at = QCursor.pos()
            self.mouse_watch.start()
        else:
            self.mouse_watch.stop()
        self.mode_changed.emit(on)

    def _mouse_moved(self):
        p = QCursor.pos()
        if (p - self.cursor_at).manhattanLength() > 12:
            self.set_active(False)

    # --- acciones ------------------------------------------------------------

    def handle(self, name: str, pressed: bool):
        if name in gamepad.RIGHT_STICK and pressed and not self._stick_moves_focus():
            return  # aquí el stick derecho desplaza (_scroll_tick)
        if name in gamepad.DIRECTIONS or name in gamepad.RIGHT_STICK:
            if pressed:
                self.set_active(True)
                self.move(gamepad.RIGHT_STICK.get(name, name))
                self.repeating = name
                self.repeat.start(REPEAT_DELAY)
            elif self.repeating == name:
                self._stop_repeat()
            return
        if not pressed:
            return
        first = not self.active
        self.set_active(True)
        if first and name == "accept" and self._focus_missing():
            self._default_focus()  # la primera pulsación solo muestra dónde está el foco
            return
        getattr(self, f"_on_{name}", lambda: self._window_button(name))()

    def _stop_repeat(self):
        self.repeat.stop()
        self.repeating = None

    def _repeat(self):
        if self.repeating:
            self.repeat.setInterval(REPEAT_RATE)
            self.move(gamepad.RIGHT_STICK.get(self.repeating, self.repeating))

    def _window(self) -> QWidget | None:
        return QApplication.activeModalWidget() or QApplication.activeWindow()

    def _current(self) -> QWidget | None:
        """El control con el foco en la ventana que manda. Si esa ventana no figura
        como activa (gamescope), Qt no lo da como foco de la aplicación pero sí lo
        recuerda la ventana."""
        w, win = QApplication.focusWidget(), self._window()
        if win is not None and (w is None or w.window() is not win):
            w = win.focusWidget()
        return w

    def _focus_missing(self) -> bool:
        w, win = self._current(), self._window()
        return win is not None and (w is None or not w.isVisible() or w.window() is not win)

    def _default_focus(self) -> bool:
        win = self._window()
        if win is None:
            return False
        target = None
        fn = getattr(win, "gamepad_default_focus", None)
        if fn:
            target = fn()
        if target is None:
            cands = focusable(win)
            default =next((w for w in cands if isinstance(w, QPushButton) and w.isDefault()), None)
            target = default or (min(cands, key=lambda w: (global_rect(w).top(), global_rect(w).left()))
                                 if cands else None)
        if target is not None:
            focus_widget(target)
            return True
        return False

    def move(self, d: str):
        popup = QApplication.activePopupWidget()
        if popup is not None:
            _send_key(popup.focusWidget() or popup, KEYS[d])
            return
        win = self._window()
        if win is None:
            return
        if self._focus_missing():
            self._default_focus()
            return
        w = self._current()
        if isinstance(w, QAbstractItemView) and w.model() is not None and w.model().rowCount():
            before = w.currentIndex()
            _send_key(w, KEYS[d])
            if w.currentIndex() != before:
                return  # se movió dentro de la lista; si no, estamos en su borde: salimos
        cur = global_rect(w)
        cands = [(c, global_rect(c)) for c in focusable(win) if c is not w and not w.isAncestorOf(c)]
        target = pick_in_window(w, cur, cands, d)
        if target is not None:
            focus_widget(target, cur)

    def _on_accept(self):
        popup = QApplication.activePopupWidget()
        if popup is not None:
            target = popup if isinstance(popup, QMenu) else (popup.focusWidget() or popup)
            _send_key(target, Qt.Key_Return)
            return
        if self._focus_missing():
            self._default_focus()
            return
        w = self._current()
        if w is None:
            return
        if isinstance(w, QComboBox):
            w.showPopup()
        elif isinstance(w, QPushButton) and w.menu() is not None:
            QTimer.singleShot(0, w.showMenu)  # showMenu es bloqueante: fuera de este manejador
            QTimer.singleShot(0, lambda m=w.menu(): _select_first(m))  # corre ya con el menú abierto
        elif isinstance(w, QAbstractButton):
            w.animateClick()
        elif isinstance(w, QAbstractItemView):
            _send_key(w, Qt.Key_Return)
        elif isinstance(w, QLineEdit):
            open_keyboard()
        else:
            _send_key(w, Qt.Key_Space)

    def _on_back(self):
        popup = QApplication.activePopupWidget()
        if popup is not None:
            _send_key(popup, Qt.Key_Escape)
            return
        modal = QApplication.activeModalWidget()
        if modal is not None:
            _send_key(modal, Qt.Key_Escape)
            return
        win = QApplication.activeWindow()
        fn = getattr(win, "gamepad_back", None)
        if fn is None or not fn():
            if win is not None:
                _send_key(self._current() or win, Qt.Key_Escape)

    def _on_lt(self):
        self._page(Qt.Key_PageUp, -1)

    def _on_rt(self):
        self._page(Qt.Key_PageDown, 1)

    def _page(self, key, sign: int):
        w = self._current()
        if isinstance(w, QAbstractItemView):
            _send_key(w, key)
            return
        area = self._scroll_area()
        if area is not None:
            bar = area.verticalScrollBar()
            bar.setValue(bar.value() + sign * bar.pageStep())

    def _window_button(self, name: str):
        if QApplication.activePopupWidget() is not None or QApplication.activeModalWidget() is not None:
            return
        fn = getattr(QApplication.activeWindow(), "gamepad_button", None)
        if fn:
            fn(name)

    # --- desplazamiento con el stick derecho ----------------------------------

    def _scroll_area(self, focused_only: bool = False) -> QAbstractScrollArea | None:
        w = self._current()
        chain = [w, *_ancestors(w)] if w is not None else []
        for p in chain:
            if isinstance(p, QAbstractScrollArea) and p.verticalScrollBar().maximum() > 0:
                return p
        if focused_only:
            return None
        win = self._window()
        if win is not None:
            for a in win.findChildren(QAbstractScrollArea):
                if a.isVisible() and a.verticalScrollBar().maximum() > 0 and not isinstance(a, QTextEdit):
                    return a
        return None

    def _stick_moves_focus(self) -> bool:
        """En un menú, o en un diálogo salvo que el foco esté en algo que se desplaza
        (un registro largo), el stick derecho cambia de opción como la cruceta."""
        if QApplication.activePopupWidget() is not None:
            return True
        return QApplication.activeModalWidget() is not None and self._scroll_area(focused_only=True) is None

    def _scroll_tick(self):
        v = next((d[0].tr.scroll for d in self.devices.values() if d[0].tr.scroll), 0.0)
        if not v or self._stick_moves_focus() or (self.require_active
                                                  and self.app.applicationState() != Qt.ApplicationActive):
            self.scroller.stop()
            return
        area = self._scroll_area()
        if area is not None:
            bar = area.verticalScrollBar()
            bar.setValue(bar.value() + int(v * abs(v) * SCROLL_SPEED))


_instance: Navigator | None = None


def install(app: QApplication, require_active: bool = True) -> Navigator:
    global _instance
    if _instance is None:
        _instance = Navigator(app, require_active)
    return _instance


def instance() -> Navigator | None:
    return _instance


def gamepad_active() -> bool:
    return _instance is not None and _instance.active
