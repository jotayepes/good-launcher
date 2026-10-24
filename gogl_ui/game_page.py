"""Ficha de un juego: cabecera con arte, acciones y panel de partidas en la nube."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from PySide6.QtCore import QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QComboBox, QFrame, QMenu, QMessageBox, QHBoxLayout, QLabel, QPlainTextEdit,
                               QPushButton, QScrollArea, QSizePolicy, QTextBrowser, QVBoxLayout,
                               QWidget)

from gogl import APP_NAME, i18n, languages, session
from gogl.i18n import _
from gogl.library import product_details
from gogl.runner import proton as proton_mgr
from gogl.saves.engine import ConflictError
from gogl.selfcmd import cli_command
from gogl.state import State
from gogl.sync import SyncUnavailable, describe

from . import navigation, theme
from .achievements_card import AchievementsPanel
from .dialogs import ConflictDialog, ProtonDownloadDialog, pick_directory
from .images import ImageStore
from .library_view import Entry
from .offline_card import OfflinePanel
from .web import store_available
from .workers import run_task

HERO_H = 340


class Hero(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("Hero")
        self.setFixedHeight(HERO_H)
        self.pixmap: QPixmap | None = None

    def set_pixmap(self, pm: QPixmap | None):
        self.pixmap = pm
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        r = self.rect()
        p.fillRect(r, QColor(theme.SURFACE))
        if self.pixmap:
            s = self.pixmap.size().scaled(r.size(), Qt.KeepAspectRatioByExpanding)
            p.drawPixmap(int((r.width() - s.width()) / 2), 0, s.width(), s.height(), self.pixmap)
        g = QLinearGradient(0, 0, 0, r.height())
        g.setColorAt(0.0, QColor(18, 17, 22, 60))
        g.setColorAt(0.55, QColor(18, 17, 22, 150))
        g.setColorAt(1.0, QColor(theme.BG))
        p.fillRect(r, g)
        side = QLinearGradient(0, 0, r.width(), 0)
        side.setColorAt(0.0, QColor(18, 17, 22, 200))
        side.setColorAt(0.6, QColor(18, 17, 22, 0))
        p.fillRect(QRectF(r), side)


def card(title: str) -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setObjectName("Card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(20, 18, 20, 18)
    lay.setSpacing(10)
    t = QLabel(title)
    t.setObjectName("CardTitle")
    lay.addWidget(t)
    return f, lay


def pill(text: str, color: str) -> QLabel:
    c = QColor(color)
    l = QLabel(text)
    l.setObjectName("Pill")
    l.setStyleSheet(f"#Pill {{ color: {color}; background: rgba({c.red()},{c.green()},{c.blue()},40); }}")
    return l


class ElidedLabel(QLabel):
    """Ruta en una sola línea, recortada por el centro; completa en el tooltip."""

    def __init__(self, text: str):
        super().__init__()
        self.full = text
        self.setToolTip(text)
        self.setMinimumWidth(60)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.setText(self.fontMetrics().elidedText(self.full, Qt.ElideMiddle, self.width()))


def _drop(w: QWidget) -> None:
    # Ocultar y desenganchar ya: deleteLater solo libera la memoria más tarde.
    w.hide()
    w.setParent(None)
    w.deleteLater()


def clear(layout):
    while layout.count():
        it = layout.takeAt(0)
        if it.widget():
            _drop(it.widget())
        elif it.layout():
            clear(it.layout())


class GamePage(QWidget):
    back = Signal()
    link_requested = Signal(object)    # Entry
    unlink_requested = Signal(object)  # Entry
    uninstall_requested = Signal(object)  # Entry
    show_downloads = Signal()
    show_cloud = Signal(str)           # game_id: sus partidas en la nube
    changed = Signal(str)              # game_id: refrescar biblioteca

    def __init__(self, images: ImageStore, state: State, downloads=None):
        super().__init__()
        self.downloads = downloads
        self.updates: dict[str, tuple[str, str] | None] = {}
        self.images = images
        self.state = state
        self.entry: Entry | None = None
        self.details: dict = {}
        self.busy = False
        self._primary: QPushButton | None = None
        self._manage: QPushButton | None = None
        self.log: QPlainTextEdit | None = None
        self.running: dict[str, subprocess.Popen] = {}
        self.poll = QTimer(self)
        self.poll.setInterval(2000)
        self.poll.timeout.connect(self._poll_running)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        col = QVBoxLayout(body)
        col.setContentsMargins(0, 0, 0, 28)
        col.setSpacing(0)

        # --- cabecera ---
        self.hero = Hero()
        hl = QVBoxLayout(self.hero)
        hl.setContentsMargins(36, 18, 36, 26)
        back = QPushButton("←  " + _("Biblioteca"))
        back.setObjectName("Ghost")
        back.clicked.connect(self.back)
        hl.addWidget(back, 0, Qt.AlignLeft)
        hl.addStretch()
        self.title = QLabel()
        self.title.setObjectName("GameTitle")
        self.title.setWordWrap(True)
        hl.addWidget(self.title)
        self.meta = QLabel()
        self.meta.setObjectName("HeroMeta")
        hl.addWidget(self.meta)
        hl.addSpacing(14)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(10)
        hl.addLayout(self.actions)
        col.addWidget(self.hero)

        # --- contenido ---
        content = QHBoxLayout()
        content.setContentsMargins(36, 8, 36, 0)
        content.setSpacing(20)
        left = QVBoxLayout()
        about, al = card(_("Acerca del juego"))
        self.summary = QTextBrowser()
        self.summary.setOpenExternalLinks(True)
        self.summary.setMinimumHeight(160)
        al.addWidget(self.summary)
        left.addWidget(about)
        self.install_card, self.install_lay = card(_("Instalación"))
        left.addWidget(self.install_card)
        ach_card, ach_lay = card(_("Logros"))
        self.achievements = AchievementsPanel(images, state)
        ach_lay.addWidget(self.achievements)
        left.addWidget(ach_card)
        left.addStretch()
        content.addLayout(left, 3)

        right = QVBoxLayout()
        self.cloud_card, self.cloud_lay = card(_("Partidas en la nube"))
        self.cloud_card.setMinimumWidth(380)
        right.addWidget(self.cloud_card)
        off_card, off_lay = card(_("Extras e instaladores"))
        self.offline = OfflinePanel(downloads)
        off_lay.addWidget(self.offline)
        right.addWidget(off_card)
        right.addStretch()
        content.addLayout(right, 2)
        col.addLayout(content)
        col.addStretch()

        images.loaded.connect(self._on_image)

    # --- carga ---------------------------------------------------------

    def show_entry(self, entry: Entry):
        same = self.entry and self.entry.game_id == entry.game_id
        self.entry = entry
        self.title.setText(entry.title)
        if not same:
            self.details = {}
            self.hero.set_pixmap(None)
            self.summary.setPlainText("")
            gid = entry.game_id
            run_task(lambda _b: product_details(gid), self._on_details)
        if entry.game and entry.game.extra.get("managed") and entry.game_id not in self.updates:
            gid = entry.game_id
            self.updates[gid] = None
            from gogl.content import check_update
            run_task(lambda b: check_update(b.api, gid), lambda r: self._on_update_check(gid, r),
                     lambda e: None)
        self._render_meta()
        self._render_actions()
        self._render_install()
        self._render_cloud()
        self.achievements.show_game(entry.game_id, self._achievements_note())
        preferred = entry.game.platform if entry.game else (
            "linux" if "linux" in entry.platforms and "windows" not in entry.platforms else "windows")
        self.offline.show_game(entry.game_id, entry.title, preferred)

    def _achievements_note(self) -> str:
        from gogl.runner import comet
        g = self.entry.game
        if not g:
            return ""
        if g.platform != "windows":
            return _("La versión de Linux de los juegos de GOG no registra logros.")
        if not comet.uses_galaxy_sdk(g.install_path):
            return _("Este juego no trae el kit de Galaxy, así que no puede registrar logros nuevos "
                     "fuera de GOG Galaxy.")
        if not comet.enabled(g):
            return _("Registrar logros mientras juegas está desactivado para este juego.")
        return _("Se registran mientras juegas desde {app}, con aviso al conseguirlos.").format(app=APP_NAME)

    def _on_details(self, d: dict):
        if not self.entry or d.get("id") != self.entry.game_id:
            return
        self.details = d
        self.summary.setPlainText(d.get("summary") or _("Sin descripción."))
        self.hero.set_pixmap(self.images.image(d.get("background")))
        self._render_meta()
        if self.entry.game:
            self._render_install()

    def _on_image(self, key: str):
        bg = self.details.get("background")
        if bg and key == f"url:{bg}":
            self.hero.set_pixmap(self.images.image(bg))

    def _render_meta(self):
        e = self.entry
        bits = []
        if self.details.get("developers"):
            bits.append(", ".join(self.details["developers"][:2]))
        if self.details.get("release"):
            bits.append(self.details["release"][:4])
        bits.append(" · ".join({"windows": "Windows", "linux": "Linux", "mac": "macOS"}[p]
                               for p in e.platforms if p in ("windows", "linux", "mac")))
        self.meta.setText("   ·   ".join(b for b in bits if b))

    # --- acciones ------------------------------------------------------

    def primary_button(self) -> QPushButton | None:
        b = self._primary
        return b if b is not None and b.isVisible() else None

    def open_manage_menu(self) -> bool:
        if self._manage is None or not self._manage.isVisible():
            return False
        self._manage.setFocus()
        navigation.open_menu(self._manage)
        return True

    def _render_actions(self):
        # Si el foco estaba en estos botones (mando), se devuelve al principal al rehacerlos.
        fw = QApplication.focusWidget()
        had_focus = fw is not None and self.hero.isAncestorOf(fw)
        self._primary = self._manage = None
        clear(self.actions)
        self._render_actions_()
        if had_focus and self._primary is not None:
            self._primary.setFocus(Qt.OtherFocusReason)

    def _render_actions_(self):
        e = self.entry
        if e.installed:
            running = self._is_running(e.game_id)
            play = QPushButton(_("En ejecución…") if running else "▶  " + _("Jugar"))
            self._primary = play
            play.setObjectName("Primary")
            play.setEnabled(not running)
            play.clicked.connect(self._play)
            self.actions.addWidget(play)
            if running:
                stop = QPushButton(_("Cerrar el juego"))
                stop.setToolTip(_("Fuerza el cierre del juego y de sus procesos si no responde "
                                  "o no llega a abrir ventana"))
                stop.clicked.connect(self._stop)
                self.actions.addWidget(stop)
            steam_btn = QPushButton(_("Añadir a Steam"))
            steam_btn.setToolTip(_("Crea un acceso directo en Steam (con carátulas) para jugar desde "
                                   "el modo juego con sincronización de partidas"))
            steam_btn.clicked.connect(self._add_to_steam)
            self.actions.addWidget(steam_btn)
            job = self.downloads.active(e.game_id) if self.downloads else None
            if job:
                pct = f"{job.progress.fraction * 100:.0f}%"
                prog = QPushButton((_("Actualizando {pct}") if job.kind == "update" else
                                    _("Reparando {pct}")).format(pct=pct))
                prog.clicked.connect(self.show_downloads)
                self.actions.addWidget(prog)
            elif self.updates.get(e.game_id):
                old, new = self.updates[e.game_id]
                upd = QPushButton(_("Actualizar a {version}").format(version=new) if new else _("Actualizar"))
                upd.setToolTip(_("Versión instalada: {version}. Solo se descarga lo que ha cambiado.")
                               .format(version=old or "?"))
                upd.clicked.connect(lambda: self._enqueue("update"))
                self.actions.addWidget(upd)
            manage = QPushButton(_("Gestionar") + " ▾")
            manage.setObjectName("Ghost")
            menu = QMenu(manage)
            if e.game.extra.get("managed"):
                menu.addAction(_("Buscar actualizaciones"), self._check_update_now)
                menu.addAction(_("Verificar y reparar archivos"), lambda: self._enqueue("repair"))
            menu.addAction(_("Ver registro de la última partida"), self._open_log)
            if e.game.platform == "windows" and e.game.extra.get("managed"):
                menu.addAction(_("Repetir la preparación de Windows"), self._reset_setup)
            if e.game.platform == "windows":
                on = e.game.extra.get("achievements", True) is not False
                menu.addAction(_("No registrar logros en este juego") if on else
                               _("Registrar logros en este juego"),
                               lambda: self._toggle_achievements(not on))
            if e.game.extra.get("managed"):
                menu.addSeparator()
                menu.addAction(_("Desinstalar…"), lambda: self.uninstall_requested.emit(self.entry))
            manage.setMenu(menu)
            self._manage = manage
            self.actions.addWidget(manage)
        else:
            job = self.downloads.active(e.game_id) if self.downloads else None
            if job:
                inst = QPushButton(_("En cola…") if job.status == "queued" else
                                   _("Instalando {pct}").format(pct=f"{job.progress.fraction * 100:.0f}%"))
                inst.setObjectName("Primary")
                inst.clicked.connect(self.show_downloads)
            else:
                inst = QPushButton(_("Instalar"))
                inst.setObjectName("Primary")
                inst.setEnabled(bool({"windows", "linux"} & set(e.platforms)) and self.downloads is not None)
                inst.clicked.connect(self._install)
            self._primary = inst
            self.actions.addWidget(inst)
            link = QPushButton(_("Vincular instalación existente…"))
            link.clicked.connect(lambda: self.link_requested.emit(self.entry))
            self.actions.addWidget(link)
        store = QPushButton(_("Ver en GOG.com"))
        store.setObjectName("Ghost")
        url = e.product.get("url")
        store.setEnabled(bool(url))
        store.clicked.connect(lambda: self.window().show_store(url))
        store.setVisible(store_available())
        self.actions.addWidget(store)
        self.actions.addStretch()

    def _render_install(self):
        clear_after_title(self.install_lay)
        g = self.entry.game
        if not g:
            l = QLabel(_("No instalado."))
            l.setObjectName("Muted")
            self.install_lay.addWidget(l)
            return
        self._path_row(self.install_lay, _("Carpeta"), str(g.install_path))
        if g.prefix:
            self._path_row(self.install_lay, _("Prefijo"), str(g.prefix))
        if g.platform == "windows":
            prow = QHBoxLayout()
            lbl = QLabel("Proton")
            lbl.setStyleSheet("font-weight: 700;")
            combo = QComboBox()
            d = proton_mgr.default()
            combo.addItem(_("Predeterminado ({name})").format(name=d.name) if d else
                          _("Predeterminado (ninguno instalado)"), None)
            for v in proton_mgr.installed():
                combo.addItem(v.name, v.name)
            idx = combo.findData(g.extra.get("proton"))
            combo.setCurrentIndex(max(idx, 0))
            combo.currentIndexChanged.connect(lambda _i, c=combo: self._set_proton(c.currentData()))
            prow.addWidget(lbl)
            prow.addStretch()
            prow.addWidget(combo)
            self.install_lay.addLayout(prow)
        lrow = QHBoxLayout()
        lbl = QLabel(_("Idioma"))
        lbl.setStyleSheet("font-weight: 700;")
        lang = QComboBox()
        current = g.extra.get("language")
        codes = set(self.details.get("languages") or []) | {current or "en-US"}
        if not current:
            lang.addItem(_("El del sistema"), None)
        for code in sorted(codes, key=languages.name):
            lang.addItem(languages.name(code), code)
        lang.setCurrentIndex(max(lang.findData(current), 0))
        lang.setToolTip(_("Idioma que {app} indica al juego al arrancar (vía Wine/Proton). Si el juego "
                          "tiene paquetes de idioma propios, se ofrece descargarlos.").format(app=APP_NAME))
        lang.currentIndexChanged.connect(lambda _i, c=lang: self._set_language(c.currentData()))
        lrow.addWidget(lbl)
        lrow.addStretch()
        lrow.addWidget(lang)
        self.install_lay.addLayout(lrow)
        row = QHBoxLayout()
        opts = QPushButton(_("Opciones de lanzamiento…"))
        opts.setToolTip(_("Argumentos, variables de entorno y comando previo (MangoHud, GameMode…)"))
        opts.clicked.connect(self._launch_options)
        row.addWidget(opts)
        if g.extra.get("launch"):
            row.addWidget(pill(_("Personalizado"), theme.ACCENT))
        row.addStretch()
        unlink = QPushButton(_("Desvincular"))
        unlink.setObjectName("Ghost")
        unlink.setToolTip(_("Quita el juego del lanzador sin borrar ningún archivo"))
        unlink.clicked.connect(lambda: self.unlink_requested.emit(self.entry))
        row.addWidget(unlink)
        self.install_lay.addLayout(row)

    def _path_row(self, lay, label: str, path: str, extra: QPushButton | None = None):
        box = QVBoxLayout()
        box.setSpacing(0)
        head = QHBoxLayout()
        name = QLabel(label)
        name.setStyleSheet("font-weight: 700;")
        open_ = QPushButton(_("Abrir"))
        open_.setObjectName("Ghost")
        target = path.split("  (")[0]
        open_.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(target)))
        head.addWidget(name)
        head.addStretch()
        if extra:
            head.addWidget(extra)
        head.addWidget(open_)
        p = ElidedLabel(path)
        p.setObjectName("PathLabel")
        box.addLayout(head)
        box.addWidget(p)
        lay.addLayout(box)

    # --- instalar / actualizar ------------------------------------------

    def _install(self):
        from .dialogs import InstallDialog
        from .downloads import Job
        dlg = InstallDialog(self.entry.game_id, self.entry.title, self.entry.platforms, self)
        if dlg.exec():
            self.downloads.enqueue(Job(self.entry.game_id, self.entry.title, "install", dlg.params))
            self._render_actions()

    def _enqueue(self, kind: str):
        from .downloads import Job
        self.downloads.enqueue(Job(self.entry.game_id, self.entry.title, kind))
        self.updates.pop(self.entry.game_id, None)
        self._render_actions()

    def _on_update_check(self, gid: str, result):
        self.updates[gid] = result
        if self.entry and self.entry.game_id == gid:
            self._render_actions()

    def _check_update_now(self):
        from gogl.content import check_update
        gid = self.entry.game_id

        def done(r):
            self._on_update_check(gid, r)
            if not r:
                QMessageBox.information(self, _("Actualizaciones"), _("Tienes la última versión."))

        run_task(lambda b: check_update(b.api, gid), done,
                 lambda e: QMessageBox.warning(self, _("Actualizaciones"), str(e)))

    def on_download_progress(self, gid: str):
        if not (self.entry and self.entry.game_id == gid and self.downloads):
            return
        job = self.downloads.active(gid)
        key = (job.status, int(job.progress.fraction * 100)) if job else None
        if key != getattr(self, "_last_job_key", None):
            self._last_job_key = key
            self._render_actions()

    def _set_language(self, code: str | None):
        g = self.state.game(self.entry.game_id)
        if code:
            g.extra["language"] = code
        else:
            g.extra.pop("language", None)
        self.state.save_game(g)
        self.entry.game = g
        languages.apply_to_game_info(g.install_path, code)
        if not (code and g.extra.get("managed") and g.platform == "windows"):
            return
        # ¿Tiene ese idioma un paquete propio que no está descargado?
        from gogl.content import build_info, load_manifest
        gid = g.game_id

        def check(b):
            m = load_manifest(gid)
            info = build_info(b.api, gid, "windows")
            have = (m or {}).get("plan", {}).get("depot_language") or (m or {}).get("plan", {}).get("language")
            if info.depot_language(code) != have:
                old = info.estimate(have or "en-US", g.extra.get("dlcs") or [])[0]
                new = info.estimate(code, g.extra.get("dlcs") or [])[0]
                return max(new - old, 0), m
            return None

        def done(r):
            if not r:
                return
            extra_bytes, m = r
            from .downloads import Job, human
            if QMessageBox.question(
                    self, _("Archivos de idioma"),
                    _("{language} tiene archivos propios (voces o textos) que no están instalados. "
                      "¿Descargarlos ahora? (unos {size}; solo se baja lo que cambia)").format(
                        language=languages.name(code), size=human(extra_bytes))) == QMessageBox.Yes:
                self.downloads.enqueue(Job(gid, self.entry.title, "install", {
                    "platform": "windows", "language": code, "dlcs": g.extra.get("dlcs") or [],
                    "install_path": Path(m["install_path"])}))
                self._render_actions()

        run_task(check, done, lambda e: None)

    def _launch_options(self):
        from .dialogs import LaunchOptionsDialog
        g = self.state.game(self.entry.game_id)
        dlg = LaunchOptionsDialog(g.title, g.extra.get("launch") or {}, g.platform == "windows", self)
        if not dlg.exec():
            return
        if dlg.options:
            g.extra["launch"] = dlg.options
        else:
            g.extra.pop("launch", None)
        self.state.save_game(g)
        self.entry.game = g
        self._render_install()

    def _toggle_achievements(self, on: bool):
        g = self.state.game(self.entry.game_id)
        if on:
            g.extra.pop("achievements", None)
        else:
            g.extra["achievements"] = False
        self.state.save_game(g)
        self.entry.game = g
        self._render_actions()
        self.achievements.show_game(g.game_id, self._achievements_note())

    def _reset_setup(self):
        from gogl.runner.setup import reset
        reset(self.entry.game)
        QMessageBox.information(
            self, _("Preparación de Windows"),
            _("En la próxima partida se volverán a instalar los componentes que pide el juego "
              "(Visual C++, DirectX…) y a ejecutar sus scripts de instalación de GOG."))

    def _open_log(self):
        from gogl.runner.game import log_path
        p = log_path(self.entry.game_id)
        if not p.exists():
            QMessageBox.information(self, _("Registro"),
                                    _("Este juego todavía no se ha lanzado desde {app}.").format(app=APP_NAME))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def _set_proton(self, name: str | None):
        g = self.state.game(self.entry.game_id)
        if name:
            g.extra["proton"] = name
        else:
            g.extra.pop("proton", None)
        self.state.save_game(g)
        self.entry.game = g

    # --- jugar ---------------------------------------------------------

    def _is_running(self, gid: str) -> bool:
        p = self.running.get(gid)
        return p is not None and p.poll() is None

    def _play(self):
        g = self.entry.game
        if g.platform == "windows" and not proton_mgr.installed():
            if QMessageBox.question(
                    self, _("Falta Proton-GE"),
                    _("Para jugar a juegos de Windows hace falta Proton-GE.\n\n¿Descargar ahora la última "
                      "versión (unos 550 MB) a la carpeta de {app}?").format(app=APP_NAME)) != QMessageBox.Yes:
                return
            if not ProtonDownloadDialog(self).exec():
                return
            self._render_install()
        # Proceso aparte: la ventana de sesión (sync antes y después) es la de `gogl run`.
        self.running[g.game_id] = subprocess.Popen(
            [*cli_command(), "run", g.game_id], start_new_session=True)
        self._render_actions()
        self.poll.start()
        self._hide_for_game()

    def _hide_for_game(self):
        """En gamescope todas las ventanas son del mismo «juego» de Steam y la de la app, a
        pantalla completa, tapa la del juego (ScummVM, DOSBox…, que además es más pequeña).
        Mientras se juega se oculta; vuelve al terminar."""
        if not navigation.in_gamescope():
            return
        QApplication.setQuitOnLastWindowClosed(False)
        self.window().hide()

    def _show_after_game(self):
        w = self.window()
        if navigation.in_gamescope() and not w.isVisible():
            w.showFullScreen()
            w.raise_()
            w.activateWindow()

    def _stop(self):
        g = self.entry.game
        p = self.running.get(g.game_id)
        if p is None:
            return
        if QMessageBox.question(
                self, _("Cerrar el juego"),
                _("Se cerrará {title} a la fuerza; lo que no hayas guardado se perderá.\n\n¿Continuar?")
                .format(title=g.title)) != QMessageBox.Yes:
            return
        run_task(lambda _b: session.force_close(g, p),
                 lambda _r: self._poll_running(),
                 lambda e: QMessageBox.warning(self, _("No se pudo cerrar el juego"), str(e)))

    def _poll_running(self):
        for gid, p in list(self.running.items()):
            if p.poll() is not None:
                del self.running[gid]
                self.changed.emit(gid)
                if self.entry and self.entry.game_id == gid:
                    self._render_actions()
        if not self.running:
            self.poll.stop()
            self._show_after_game()

    def _add_to_steam(self, force: bool = False):
        from gogl import steam
        gid = self.entry.game_id

        def failed(e: Exception):
            if isinstance(e, steam.SteamError) and steam.steam_running():
                if QMessageBox.question(
                        self, _("Steam está abierto"),
                        _("Steam reescribe sus accesos directos al cerrarse, así que hay que cerrarlo "
                          "antes de añadir el juego.\n\n¿Cerrar Steam ahora y continuar?")) == QMessageBox.Yes:
                    run_task(lambda _b: _shutdown_steam(), lambda _r: self._add_to_steam())
                return
            QMessageBox.warning(self, _("No se pudo añadir a Steam"), str(e))

        def done(r):
            head = (_("«{title}» se ha actualizado en tu biblioteca de Steam, con sus carátulas.") if r.updated else
                    _("«{title}» ya está en tu biblioteca de Steam, con sus carátulas."))
            QMessageBox.information(
                self, _("Añadido a Steam"),
                head.format(title=self.entry.title) + "\n\n" +
                _("No hace falta forzar ninguna herramienta de compatibilidad: {app} usa su propio Proton-GE "
                  "y sincroniza las partidas antes y después de jugar.").format(app=APP_NAME))

        run_task(lambda b: steam.add_shortcut(b.state.game(gid)), done, failed)

    # --- partidas en la nube --------------------------------------------

    def _render_cloud(self):
        clear_after_title(self.cloud_lay)
        g = self.entry.game
        if not g:
            msg = (_("Vincula la instalación para gestionar sus partidas guardadas.")
                   if "windows" in self.entry.platforms else
                   _("GOG no ofrece partidas en la nube para juegos nativos de Linux."))
            self._muted(self.cloud_lay, msg)
            return
        if g.platform != "windows":
            self._muted(self.cloud_lay, _("Juego nativo de Linux: GOG no ofrece partidas en la nube para esta "
                                          "versión. Si las necesitas, usa la versión de Windows con Proton."))
            return

        st = self.state.status(g.game_id) or {}
        head = QHBoxLayout()
        if st.get("pending"):
            conflict = st.get("last_error") == "conflicto"
            head.addWidget(pill(_("Conflicto") if conflict else _("Pendiente"),
                                theme.DANGER if conflict else theme.WARN))
        elif st.get("last_ok"):
            head.addWidget(pill(_("Sincronizado"), theme.OK))
        else:
            head.addWidget(pill(_("Nunca sincronizado"), theme.MUTED))
        when = time.strftime(i18n.date_format(), time.localtime(st["last_ok"])) if st.get("last_ok") else "—"
        last = QLabel(_("Último sync correcto: {when}").format(when=when))
        last.setObjectName("Muted")
        head.addWidget(last)
        head.addStretch()
        self.cloud_lay.addLayout(head)
        if st.get("pending") and st.get("last_error") and st["last_error"] != "conflicto":
            err = QLabel(st["last_error"])
            err.setWordWrap(True)
            err.setStyleSheet(f"color: {theme.WARN};")
            self.cloud_lay.addWidget(err)

        self.locs_box = QVBoxLayout()
        self.cloud_lay.addLayout(self.locs_box)
        self._muted(self.locs_box, _("Buscando ubicaciones de partidas…"))
        gid = g.game_id
        run_task(lambda b: b.service.locations(b.state.game(gid), refresh=False),
                 lambda locs: self._on_locations(gid, locs),
                 lambda e: self._on_locations(gid, e))

        row = QHBoxLayout()
        self.sync_btn = QPushButton(_("Sincronizar ahora"))
        self.sync_btn.setObjectName("Primary")
        self.sync_btn.setStyleSheet("padding: 8px 20px; font-size: 10pt;")
        self.sync_btn.clicked.connect(lambda: self._sync())
        preview = QPushButton(_("Ver cambios"))
        preview.setToolTip(_("Muestra qué se subiría o descargaría sin tocar nada"))
        preview.clicked.connect(lambda: self._sync(dry_run=True))
        in_cloud = QPushButton(_("En la nube"))
        in_cloud.setObjectName("Ghost")
        in_cloud.setToolTip(_("Ver y borrar las partidas guardadas en la nube de GOG y su espacio usado"))
        in_cloud.clicked.connect(lambda: self.show_cloud.emit(self.entry.game_id))
        backups = QPushButton(_("Copias"))
        backups.setObjectName("Ghost")
        backups.setToolTip(_("Abrir la carpeta de copias de seguridad de este juego"))
        backups.clicked.connect(self._open_backups)
        row.addWidget(self.sync_btn)
        row.addWidget(preview)
        row.addStretch()
        row.addWidget(in_cloud)
        row.addWidget(backups)
        self.cloud_lay.addLayout(row)
        self.sync_btn.setEnabled(not self.busy)

        self.log = QPlainTextEdit()
        self.log.setObjectName("Log")
        self.log.setReadOnly(True)
        self.log.setFixedHeight(130)
        self.log.hide()
        self.cloud_lay.addWidget(self.log)

    def _on_locations(self, gid: str, result):
        if not self.entry or self.entry.game_id != gid:
            return
        clear(self.locs_box)
        if isinstance(result, Exception):
            self._muted(self.locs_box, str(result))
            if isinstance(result, SyncUnavailable):
                self.sync_btn.setEnabled(False)
            return
        for loc in result:
            change = QPushButton(_("Cambiar"))
            change.setObjectName("Ghost")
            change.clicked.connect(lambda _c=False, l=loc: self._change_location(l))
            exists = "" if loc.path.is_dir() else "  (" + _("aún no existe") + ")"
            self._path_row(self.locs_box, loc.name, str(loc.path) + exists, change)

    def _change_location(self, loc):
        d = pick_directory(self, _("Carpeta para «{name}»").format(name=loc.name), str(loc.path))
        if not d:
            return
        g = self.state.game(self.entry.game_id)
        g.extra.setdefault("save_overrides", {})[loc.name] = d
        self.state.save_game(g)
        self.entry.game = g
        self._render_cloud()

    def _open_backups(self):
        from gogl import paths
        d = paths.backups_dir() / self.entry.game_id
        d.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

    def _sync(self, prefer: str | None = None, dry_run: bool = False):
        gid = self.entry.game_id
        self.busy = True
        self.sync_btn.setEnabled(False)
        self.sync_btn.setText(_("Sincronizando…") if not dry_run else _("Analizando…"))
        self.log.show()
        self.log.clear()
        run_task(lambda b: b.service.sync(gid, prefer=prefer, dry_run=dry_run, wait_network=10),
                 lambda res: self._sync_done(gid, res),
                 lambda e: self._sync_failed(gid, e),
                 self._log)

    def _log(self, line: str):
        try:
            if self.log:
                self.log.appendPlainText(line)
        except RuntimeError:  # el widget se destruyó al cambiar de juego
            pass

    def _sync_done(self, gid: str, res):
        self.busy = False
        self.changed.emit(gid)
        if self.entry and self.entry.game_id == gid:
            log = self.log.toPlainText()
            self._render_cloud()
            self.log.show()
            self.log.setPlainText(log)
            if res.dry_run:
                self.log.appendPlainText("\n" + _("Cambios que se aplicarían:") + "\n" + describe(res))
            else:
                for w in res.plan.warnings:
                    self.log.appendPlainText(_("aviso: {warning}").format(warning=w))
                self.log.appendPlainText(f"\n✓ {describe(res)}")

    def _sync_failed(self, gid: str, e: Exception):
        self.busy = False
        self.changed.emit(gid)
        if not self.entry or self.entry.game_id != gid:
            return
        log = self.log.toPlainText()
        self._render_cloud()
        self.log.show()
        self.log.setPlainText(log)
        if isinstance(e, ConflictError):
            dlg = ConflictDialog(self.entry.title, e.plan, self)
            if dlg.exec() and dlg.prefer:
                self._sync(prefer=dlg.prefer)
            return
        self.log.appendPlainText(f"\n✗ {e}")

    @staticmethod
    def _muted(lay, text: str):
        l = QLabel(text)
        l.setWordWrap(True)
        l.setObjectName("Muted")
        lay.addWidget(l)


def clear_after_title(layout):
    """Vacía una tarjeta conservando su título (primer widget)."""
    while layout.count() > 1:
        it = layout.takeAt(1)
        if it.widget():
            _drop(it.widget())
        elif it.layout():
            clear(it.layout())


def _shutdown_steam() -> None:
    from gogl import steam
    subprocess.run(["steam", "-shutdown"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        if not steam.steam_running():
            return
        time.sleep(0.5)
