"""Ventana de `gogl run`: sincronización antes y después de jugar.

En el modo juego de SteamOS (gamescope) se muestra a pantalla completa con
botones grandes, manejable con la pantalla táctil o el trackpad."""

from __future__ import annotations

import os
import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QMessageBox, QProgressBar,
                               QVBoxLayout, QWidget)

from gogl import APP_NAME, session
from gogl.i18n import _
from gogl.library import cover_path
from gogl.state import State

from . import navigation, qt_i18n, theme
from .desktop import desktop_entry_installed
from .dialogs import ConflictDialog


def in_gamescope() -> bool:
    if os.environ.get("GAMESCOPE_WAYLAND_DISPLAY"):
        return True
    return os.environ.get("XDG_CURRENT_DESKTOP", "").lower() == "gamescope"


class _Bridge(QObject):
    """Ejecuta funciones en el hilo de la UI y devuelve su resultado al hilo de la sesión."""
    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(self._run, Qt.QueuedConnection)

    def _run(self, job):
        fn, box, ev = job
        try:
            box.append(fn())
        except Exception as e:  # noqa: BLE001
            box.append(e)
        ev.set()

    def invoke(self, fn: Callable[[], Any]) -> Any:
        box: list = []
        ev = threading.Event()
        self.call.emit((fn, box, ev))
        ev.wait()
        if isinstance(box[0], Exception):
            raise box[0]
        return box[0]


class SessionWindow(QWidget):
    def __init__(self, game_id: str, title: str):
        super().__init__()
        self.big = in_gamescope()
        self.setWindowTitle(f"{APP_NAME} · {title}")
        self.title_text = title
        lay = QVBoxLayout(self)
        lay.setContentsMargins(40, 36, 40, 36)
        lay.addStretch()
        row = QHBoxLayout()
        row.setSpacing(28)
        cover = QLabel()
        pm = QPixmap(str(cover_path(game_id)))
        if not pm.isNull():
            w = 220 if self.big else 120
            cover.setPixmap(pm.scaledToWidth(w, Qt.SmoothTransformation))
            row.addWidget(cover, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(10)
        t = QLabel(title)
        t.setWordWrap(True)
        t.setStyleSheet(f"font-size: {30 if self.big else 18}pt; font-weight: 800;")
        self.status_lbl = QLabel(_("Preparando…"))
        self.status_lbl.setStyleSheet(f"font-size: {16 if self.big else 11}pt;")
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setTextVisible(False)
        self.log_lbl = QLabel()
        self.log_lbl.setObjectName("Muted")
        self.log_lbl.setWordWrap(True)
        col.addWidget(t)
        col.addWidget(self.status_lbl)
        col.addWidget(self.bar)
        col.addWidget(self.log_lbl)
        col.addStretch()
        row.addLayout(col, 1)
        box = QWidget()
        box.setLayout(row)
        box.setMaximumWidth(1000)
        lay.addWidget(box, 0, Qt.AlignHCenter)
        lay.addStretch()
        if self.big:
            f = QFont(self.font())
            f.setPointSizeF(f.pointSizeF() * 1.4)
            self.setFont(f)
            self.showFullScreen()
        else:
            self.resize(620, 300)
            self.show()

    # --- SessionUI (llamado desde el hilo de la sesión vía _Bridge) -----------

    def status(self, text):
        self.status_lbl.setText(text)
        self.log_lbl.setText("")

    def log(self, text):
        self.log_lbl.setText(text)

    def ask_conflict(self, title, plan, phase):
        dlg = ConflictDialog(title, plan, self)
        if self.big:
            dlg.setFont(self.font())
            dlg.showFullScreen()
        return dlg.prefer if dlg.exec() else None

    def _choice(self, text: str, options: list[tuple[str, str]]) -> str:
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setText(text)
        buttons = {box.addButton(label, QMessageBox.AcceptRole): key for key, label in options}
        box.setDefaultButton(next(iter(buttons)))
        if self.big:
            box.setFont(self.font())
        box.exec()
        return buttons.get(box.clickedButton(), options[-1][0])

    def ask_offline(self, phase):
        if phase == session.PRE:
            return self._choice(_("No hay conexión con GOG.\n\nSi juegas ahora, al terminar se comparará "
                                  "con la nube y, si alguien cambió las partidas allí, se te preguntará."),
                                [("retry", _("Reintentar")), ("continue", _("Jugar sin sincronizar")),
                                 ("cancel", _("Salir"))])
        return self._choice(_("No hay conexión con GOG.\n\nTu partida queda guardada aquí y se subirá "
                              "automáticamente cuando vuelva la conexión."),
                            [("retry", _("Reintentar")), ("continue", _("Subir más tarde"))])

    def ask_error(self, message, phase):
        opts = [("retry", _("Reintentar")), ("continue", _("Jugar igualmente") if phase == session.PRE
                                                        else _("Subir más tarde"))]
        if phase == session.PRE:
            opts.append(("cancel", _("Salir")))
        return self._choice(_("No se pudieron sincronizar las partidas:") + f"\n\n{message}", opts)

    def error(self, message):
        QMessageBox.critical(self, APP_NAME, message)

    def achievement(self, name, description, icon):
        if self.big:
            from .toast import show_achievement
            show_achievement(name, description, icon, overlay=True)
        else:
            session.notify_achievement(name, description, icon)

    def hide_(self):
        self.hide()

    def show_(self):
        if self.big:
            self.showFullScreen()
        else:
            self.show()
        self.raise_()


class _ThreadUI:
    """Adaptador SessionUI que reenvía cada llamada al hilo de la interfaz."""

    def __init__(self, bridge: _Bridge, win: SessionWindow):
        self.b, self.w = bridge, win

    def status(self, text):
        self.b.invoke(lambda: self.w.status(text))

    def log(self, text):
        self.b.invoke(lambda: self.w.log(text))

    def ask_conflict(self, title, plan, phase):
        return self.b.invoke(lambda: self.w.ask_conflict(title, plan, phase))

    def ask_offline(self, phase):
        return self.b.invoke(lambda: self.w.ask_offline(phase))

    def ask_error(self, message, phase):
        return self.b.invoke(lambda: self.w.ask_error(message, phase))

    def error(self, message):
        self.b.invoke(lambda: self.w.error(message))

    def hide(self):
        self.b.invoke(self.w.hide_)

    def show(self):
        self.b.invoke(self.w.show_)

    def achievement(self, name, description, icon):
        self.b.invoke(lambda: self.w.achievement(name, description, icon))


def run_with_window(game_id: str) -> int:
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("gogl")
    if desktop_entry_installed():
        app.setDesktopFileName("gogl")
    app.setQuitOnLastWindowClosed(False)  # la ventana se oculta mientras se juega
    from .icon import app_icon
    app.setWindowIcon(app_icon())
    qt_i18n.install(app)
    theme.apply(app)
    nav = navigation.install(app)
    g = State().game(game_id)
    win = SessionWindow(game_id, g.title if g else game_id)
    if win.big:
        nav.set_active(True)  # en el modo juego, los diálogos se manejan con el mando
    bridge = _Bridge()
    ui = _ThreadUI(bridge, win)
    result: dict = {}

    session.install_signal_handlers()
    # Qt no devuelve el control a Python mientras espera eventos; este timer
    # permite que los manejadores de señales de Python se ejecuten.
    tick = QTimer()
    tick.start(300)
    tick.timeout.connect(lambda: None)

    def work():
        try:
            result["rc"] = session.run_game(game_id, ui)
        except Exception as e:  # noqa: BLE001
            result["rc"] = 1
            try:
                ui.error(str(e))
            except Exception:  # noqa: BLE001
                pass
        finally:
            bridge.call.emit((app.quit, [], threading.Event()))

    threading.Thread(target=work, daemon=True).start()
    app.exec()
    return result.get("rc", 1)


__all__ = ["run_with_window", "in_gamescope"]
