"""Ventana principal: barra lateral + páginas (bienvenida, biblioteca, juego, ajustes)."""

from __future__ import annotations

import json
import time

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel,
                               QMainWindow, QMessageBox, QPushButton, QScrollArea, QStackedWidget,
                               QVBoxLayout, QWidget)

from gogl import APP_NAME, APP_TITLE, config, i18n, paths, services
from gogl.auth import AuthManager
from gogl.i18n import _
from gogl.games import register_installed
from gogl.library import fetch_owned, fetch_user
from gogl.runner import proton as proton_mgr
from gogl.state import State

from . import navigation, theme
from .dialogs import LinkInstallDialog, LoginDialog, ProtonDownloadDialog, pick_directory
from .downloads import DownloadManager, DownloadsPage
from .cloud_page import CloudPage
from .game_page import ElidedLabel, GamePage, card, clear
from .images import ImageStore
from .library_view import Entry, LibraryPage, build_entries
from .profile_page import ProfilePage
from .store_page import StorePage
from .workers import run_task

USER_CACHE = "user.json"
SECTIONS = ["all", "installed", "pending", "cloud", "downloads", "store", "profile", "settings"]


def _key(letter: str, color: str) -> str:
    return f"<span style='color:{color}; font-weight:900; font-size:12pt'>{letter}</span>&nbsp; "


class GamepadHints(QLabel):
    """Barra inferior con los botones del mando (solo mientras se usa)."""

    def __init__(self):
        super().__init__()
        self.setObjectName("Muted")
        self.setStyleSheet(f"background: {theme.SIDEBAR}; border-top: 1px solid {theme.BORDER};"
                           "padding: 8px 20px;")
        self.set_context("library")
        self.hide()

    def set_context(self, ctx: str):
        a, b = _key("A", theme.OK), _key("B", theme.DANGER)
        x, y = _key("X", "#5aa9f0"), _key("Y", theme.WARN)
        sep = "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
        items = {
            "library": [a + _("Abrir"), b + _("Atrás"), y + _("Buscar"), "<b>LB RB</b> " + _("Sección"),
                        "<b>LT RT</b> " + _("Página")],
            "game": [a + _("Seleccionar"), b + _("Biblioteca"), x + _("Gestionar"),
                     "<b>☰</b> " + _("Jugar / Instalar"), "<b>" + _("Stick dcho.") + "</b> " + _("Desplazar")],
            "other": [a + _("Seleccionar"), b + _("Biblioteca"), "<b>LB RB</b> " + _("Sección"),
                      "<b>" + _("Stick dcho.") + "</b> " + _("Desplazar")],
        }[ctx]
        self.setText(sep.join(items))


class Sidebar(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("Sidebar")
        self.setFixedWidth(232)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 20, 16, 16)
        lay.setSpacing(4)

        brand = QLabel(f"<span style='color:{theme.ACCENT}'>◆</span> {APP_NAME}")
        brand.setObjectName("Brand")
        lay.addWidget(brand)
        lay.addSpacing(14)

        user = QHBoxLayout()
        self.avatar = QLabel("?")
        self.avatar.setFixedSize(36, 36)
        self.avatar.setAlignment(Qt.AlignCenter)
        self.avatar.setStyleSheet(f"background: {theme.ACCENT}; color: white; border-radius: 18px;"
                                  "font-weight: 800; font-size: 12pt;")
        names = QVBoxLayout()
        names.setSpacing(0)
        self.user_name = QLabel(_("Sin sesión"))
        self.user_name.setObjectName("UserName")
        self.user_sub = QLabel("GOG")
        self.user_sub.setObjectName("UserSub")
        names.addWidget(self.user_name)
        names.addWidget(self.user_sub)
        user.addWidget(self.avatar)
        user.addSpacing(6)
        user.addLayout(names, 1)
        lay.addLayout(user)
        lay.addSpacing(10)

        section = QLabel(_("JUEGOS"))
        section.setObjectName("SectionLabel")
        lay.addWidget(section)
        self.group = QButtonGroup(self)
        self.buttons: dict[str, QPushButton] = {}
        for key, label in (("all", _("Biblioteca")), ("installed", _("Instalados")),
                           ("pending", _("Sincronización pendiente")), ("cloud", _("Partidas en la nube")),
                           ("downloads", _("Descargas"))):
            self._nav(lay, key, label)
        section = QLabel("GOG")
        section.setObjectName("SectionLabel")
        lay.addWidget(section)
        self._nav(lay, "store", _("Tienda"))
        self._nav(lay, "profile", _("Perfil"))
        section = QLabel(_("LANZADOR"))
        section.setObjectName("SectionLabel")
        lay.addWidget(section)
        self._nav(lay, "settings", _("Ajustes"))
        lay.addStretch()

        self.status = QLabel()
        self.status.setObjectName("Muted")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

    def _nav(self, lay, key, label):
        b = QPushButton(label)
        b.setObjectName("NavButton")
        b.setCheckable(True)
        b.setCursor(Qt.PointingHandCursor)
        self.group.addButton(b)
        self.buttons[key] = b
        lay.addWidget(b)

    def set_user(self, name: str | None):
        self.user_name.setText(name or _("Sin sesión"))
        self.avatar.setText((name or "?")[:1].upper())
        self.user_sub.setText(_("Conectado a GOG") if name else _("Inicia sesión"))

    def set_downloads(self, n: int):
        self.buttons["downloads"].setText(_("Descargas") + (f"  ({n})" if n else ""))

    def set_pending(self, n: int):
        self.buttons["pending"].setText(_("Sincronización pendiente") + (f"  ({n})" if n else ""))


class WelcomePage(QWidget):
    def __init__(self, on_login):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.addStretch()
        t = QLabel(f"<span style='color:{theme.ACCENT}'>◆</span> {APP_NAME}")
        t.setStyleSheet("font-size: 40pt; font-weight: 900;")
        t.setAlignment(Qt.AlignCenter)
        lay.addWidget(t)
        s = QLabel(_("Tus juegos de GOG en Linux, con las partidas siempre sincronizadas."))
        s.setObjectName("Muted")
        s.setStyleSheet("font-size: 12pt;")
        s.setAlignment(Qt.AlignCenter)
        lay.addWidget(s)
        lay.addSpacing(26)
        b = QPushButton(_("Iniciar sesión con GOG"))
        b.setObjectName("Primary")
        b.clicked.connect(on_login)
        self.login_btn = b
        lay.addWidget(b, 0, Qt.AlignCenter)
        lay.addStretch()


class SettingsPage(QWidget):
    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window = window
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        self.lay = QVBoxLayout(body)
        self.lay.setContentsMargins(28, 22, 28, 28)
        self.lay.setSpacing(16)

    def refresh(self):
        clear(self.lay)
        t = QLabel(_("Ajustes"))
        t.setStyleSheet("font-size: 20pt; font-weight: 800;")
        self.lay.addWidget(t)

        c, l = card(_("Cuenta"))
        name = self.window.sidebar.user_name.text()
        l.addWidget(QLabel(name))
        row = QHBoxLayout()
        row.addStretch()
        out = QPushButton(_("Cerrar sesión"))
        out.clicked.connect(self.window.logout)
        row.addWidget(out)
        l.addLayout(row)
        self.lay.addWidget(c)

        self._language_card()

        c, l = card(_("Sincronización de partidas"))
        info = QLabel(_("Las partidas se comparan por contenido, no por fecha. Nunca se borra nada en la "
                        "nube por tener una carpeta local vacía, y antes de sobrescribir partidas locales "
                        "se guarda una copia de seguridad (se conservan las 10 últimas por juego)."))
        info.setWordWrap(True)
        info.setObjectName("Muted")
        l.addWidget(info)
        row = QHBoxLayout()
        self.pending_btn = QPushButton(_("Sincronizar todos los pendientes"))
        self.pending_btn.clicked.connect(self._sync_pending)
        row.addWidget(self.pending_btn)
        row.addStretch()
        l.addLayout(row)
        self.pending_out = QLabel()
        self.pending_out.setObjectName("Muted")
        self.pending_out.setWordWrap(True)
        l.addWidget(self.pending_out)
        self.lay.addWidget(c)

        c, l = card(_("Instalación de juegos"))
        from gogl.content import install_root
        from gogl import languages
        row = QHBoxLayout()
        n = QLabel(_("Carpeta por defecto"))
        n.setFixedWidth(150)
        n.setObjectName("Muted")
        root_lbl = ElidedLabel(str(install_root()))
        root_lbl.setObjectName("PathLabel")
        ch = QPushButton(_("Cambiar…"))
        ch.setObjectName("Ghost")
        ch.clicked.connect(self._choose_root)
        row.addWidget(n)
        row.addWidget(root_lbl, 1)
        row.addWidget(ch)
        l.addLayout(row)
        row = QHBoxLayout()
        n = QLabel(_("Idioma de los juegos"))
        n.setFixedWidth(150)
        n.setObjectName("Muted")
        lang = QComboBox()
        lang.setToolTip(_("Idioma que se descarga al instalar, si el juego lo tiene"))
        for code, name in sorted(languages.NAMES.items(), key=lambda kv: kv[1]):
            lang.addItem(name, code)
        lang.setCurrentIndex(max(lang.findData(languages.preferred()), 0))
        lang.currentIndexChanged.connect(lambda _i: config.set_value("language", lang.currentData()))
        row.addWidget(n)
        row.addWidget(lang)
        row.addStretch()
        l.addLayout(row)
        row = QHBoxLayout()
        from gogl.content.offline import downloads_root
        n = QLabel(_("Extras e instaladores"))
        n.setFixedWidth(150)
        n.setObjectName("Muted")
        off_lbl = ElidedLabel(str(downloads_root()))
        off_lbl.setObjectName("PathLabel")
        off_lbl.setToolTip(_("Dónde se guardan los extras y los instaladores offline (una carpeta por juego)"))
        ch = QPushButton(_("Cambiar…"))
        ch.setObjectName("Ghost")
        ch.clicked.connect(self._choose_offline_root)
        row.addWidget(n)
        row.addWidget(off_lbl, 1)
        row.addWidget(ch)
        l.addLayout(row)
        self.lay.addWidget(c)

        c, l = card(_("Logros"))
        info = QLabel(_("En los juegos que traen el kit de Galaxy, los logros se registran mientras juegas "
                        "(con Comet, una implementación libre del servicio de Galaxy que se descarga la "
                        "primera vez) y aparece un aviso al conseguirlos, también en el modo juego. "
                        "Sin conexión se guardan y se suben la próxima vez que juegues con red."))
        info.setWordWrap(True)
        info.setObjectName("Muted")
        l.addWidget(info)
        ach = QCheckBox(_("Registrar logros mientras juegas"))
        ach.setChecked(config.load().get("achievements", True))
        ach.toggled.connect(lambda on: config.set_value("achievements", on))
        l.addWidget(ach)
        self.lay.addWidget(c)

        c, l = card("Proton-GE")
        info = QLabel(_("{app} descarga y usa sus propias versiones de Proton-GE (en su carpeta de datos), "
                        "independientes de Steam.").format(app=APP_NAME))
        info.setWordWrap(True)
        info.setObjectName("Muted")
        l.addWidget(info)
        d = proton_mgr.default()
        vers = proton_mgr.installed()
        if not vers:
            m = QLabel(_("No hay ninguna versión instalada todavía."))
            l.addWidget(m)
        for v in vers:
            row = QHBoxLayout()
            n = QLabel(v.name)
            n.setStyleSheet("font-weight: 700;")
            row.addWidget(n)
            if d and v.name == d.name:
                tag = QLabel(_("Predeterminada"))
                tag.setStyleSheet(f"color: {theme.ACCENT}; font-weight: 700;")
                row.addWidget(tag)
            row.addStretch()
            if not d or v.name != d.name:
                mk = QPushButton(_("Usar por defecto"))
                mk.setObjectName("Ghost")
                mk.clicked.connect(lambda _=False, x=v.name: (proton_mgr.set_default(x), self.refresh()))
                row.addWidget(mk)
            rm = QPushButton(_("Borrar"))
            rm.setObjectName("Ghost")
            rm.clicked.connect(lambda _=False, x=v.name: self._remove_proton(x))
            row.addWidget(rm)
            l.addLayout(row)
        row = QHBoxLayout()
        dl = QPushButton(_("Descargar la última versión"))
        dl.clicked.connect(self._download_proton)
        row.addWidget(dl)
        row.addStretch()
        l.addLayout(row)
        self.lay.addWidget(c)

        c, l = card(_("Subidas pendientes en segundo plano"))
        info = QLabel(_("Un timer de systemd de tu usuario reintenta cada 15 minutos las partidas que no "
                        "se pudieron subir (por ejemplo, si apagaste la consola sin conexión)."))
        info.setWordWrap(True)
        info.setObjectName("Muted")
        l.addWidget(info)
        row = QHBoxLayout()
        on = services.installed()
        state_lbl = QLabel(_("Activado") if on else _("Desactivado"))
        state_lbl.setStyleSheet(f"color: {theme.OK if on else theme.MUTED}; font-weight: 700;")
        tog = QPushButton(_("Desactivar") if on else _("Activar"))
        tog.clicked.connect(lambda: (services.uninstall() if on else services.install(), self.refresh()))
        row.addWidget(state_lbl)
        row.addStretch()
        row.addWidget(tog)
        l.addLayout(row)
        self.lay.addWidget(c)

        c, l = card(_("Modo juego de SteamOS"))
        info = QLabel(_("Añade {app} a Steam como «juego no de Steam» para abrirlo desde el modo juego y "
                        "manejarlo con el mando (cruceta o stick para moverte, A para aceptar, B para "
                        "volver, LB/RB para cambiar de sección).").format(app=APP_NAME))
        info.setWordWrap(True)
        info.setObjectName("Muted")
        l.addWidget(info)
        row = QHBoxLayout()
        row.addStretch()
        b = QPushButton(_("Añadir {app} a Steam").format(app=APP_NAME))
        b.clicked.connect(self._add_launcher_to_steam)
        row.addWidget(b)
        l.addLayout(row)
        self.lay.addWidget(c)

        c, l = card(_("Menú de aplicaciones"))
        from .desktop import desktop_entry_installed, install_desktop_entry, remove_desktop_entry
        on = desktop_entry_installed()
        row = QHBoxLayout()
        info = QLabel((_("{app} aparece en el menú de aplicaciones de tu escritorio.") if on else
                       _("Añade {app} al menú de aplicaciones para abrirlo sin terminal.")).format(app=APP_NAME))
        info.setObjectName("Muted")
        row.addWidget(info, 1)
        b = QPushButton(_("Quitar del menú") if on else _("Añadir al menú"))
        b.clicked.connect(lambda: (remove_desktop_entry() if on else install_desktop_entry(), self.refresh()))
        row.addWidget(b)
        l.addLayout(row)
        self.lay.addWidget(c)

        c, l = card(_("Carpetas del lanzador"))
        for label, p in ((_("Datos"), paths.data_dir()), (_("Configuración"), paths.config_dir()),
                         (_("Copias de seguridad"), paths.backups_dir()), (_("Caché"), paths.cache_dir())):
            row = QHBoxLayout()
            n = QLabel(label)
            n.setFixedWidth(150)
            n.setObjectName("Muted")
            v = ElidedLabel(str(p))
            v.setObjectName("PathLabel")
            o = QPushButton(_("Abrir"))
            o.setObjectName("Ghost")
            o.clicked.connect(lambda _=False, x=p: QDesktopServices.openUrl(QUrl.fromLocalFile(str(x))))
            row.addWidget(n)
            row.addWidget(v, 1)
            row.addWidget(o)
            l.addLayout(row)
        self.lay.addWidget(c)
        self.lay.addStretch()

    def _language_card(self):
        c, l = card(_("Idioma"))
        row = QHBoxLayout()
        n = QLabel(_("Interfaz"))
        n.setFixedWidth(150)
        n.setObjectName("Muted")
        combo = QComboBox()
        combo.addItem(_("Automático ({lang})").format(lang=i18n.native_name(i18n.system_language())), None)
        for code in i18n.available():
            combo.addItem(i18n.native_name(code), code)
        combo.setCurrentIndex(max(combo.findData(config.load().get("ui_language")), 0))
        note = QLabel()
        note.setObjectName("Muted")
        note.setWordWrap(True)

        def changed(_i):
            code = combo.currentData()
            config.set_value("ui_language", code)
            # Lo que se verá al volver a abrir; el aviso, en el idioma elegido si se puede.
            if i18n.resolve(code) != i18n.current:
                note.setText(_("Se aplicará la próxima vez que abras {app}.").format(app=APP_NAME))
            else:
                note.clear()

        combo.currentIndexChanged.connect(changed)
        row.addWidget(n)
        row.addWidget(combo)
        row.addStretch()
        l.addLayout(row)
        l.addWidget(note)
        self.lay.addWidget(c)

    def _add_launcher_to_steam(self):
        from gogl import steam
        try:
            r = steam.add_launcher_shortcut()
        except steam.SteamError as e:
            if steam.steam_running() and QMessageBox.question(
                    self, _("Steam está abierto"),
                    _("Steam reescribe sus accesos directos al cerrarse, así que hay que cerrarlo "
                      "antes.\n\n¿Cerrar Steam ahora y continuar?")) == QMessageBox.Yes:
                from .game_page import _shutdown_steam
                run_task(lambda _b: _shutdown_steam(), lambda _r: self._add_launcher_to_steam())
                return
            QMessageBox.warning(self, _("No se pudo añadir a Steam"), str(e))
            return
        QMessageBox.information(self, _("Añadido a Steam"), (
            _("{app} ya estaba en tu biblioteca de Steam, en «Juegos no de Steam». Ábrelo desde el modo "
              "juego y manéjalo con el mando.") if r.updated else
            _("{app} está en tu biblioteca de Steam, en «Juegos no de Steam». Ábrelo desde el modo juego "
              "y manéjalo con el mando.")).format(app=APP_NAME))

    def _choose_root(self):
        from gogl.content import install_root
        d = pick_directory(self, _("Carpeta de instalación"), str(install_root()))
        if d:
            config.set_value("install_root", d)
            self.refresh()

    def _choose_offline_root(self):
        from gogl.content.offline import downloads_root
        d = pick_directory(self, _("Carpeta para extras e instaladores"), str(downloads_root()))
        if d:
            config.set_value("offline_root", d)
            self.refresh()

    def _download_proton(self):
        ProtonDownloadDialog(self).exec()
        self.refresh()

    def _remove_proton(self, name: str):
        if QMessageBox.question(self, _("Borrar Proton"), _("¿Borrar {name}?").format(name=name)) == QMessageBox.Yes:
            proton_mgr.remove(name)
            self.refresh()

    def _sync_pending(self):
        self.pending_btn.setEnabled(False)
        self.pending_out.setText(_("Sincronizando…"))

        def done(results):
            self.pending_btn.setEnabled(True)
            if not results:
                self.pending_out.setText(_("No hay juegos pendientes."))
            else:
                from gogl.sync import describe
                lines = []
                for gid, r in results.items():
                    g = self.window.state.game(gid)
                    title = g.title if g else gid
                    lines.append(f"{title}: {r if isinstance(r, Exception) else describe(r)}")
                self.pending_out.setText("\n".join(lines))
            self.window.reload_entries()

        run_task(lambda b: b.service.sync_pending(wait_network=10), done,
                 lambda e: (self.pending_btn.setEnabled(True), self.pending_out.setText(str(e))))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1360, 860)
        self.setMinimumSize(980, 620)
        self.state = State()
        self.auth = AuthManager()
        self.images = ImageStore()
        self.downloads = DownloadManager()

        central = QWidget()
        self.setCentralWidget(central)
        v = QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        v.addLayout(h, 1)
        self.hints = GamepadHints()
        v.addWidget(self.hints)
        self.sidebar = Sidebar()
        h.addWidget(self.sidebar)
        self.stack = QStackedWidget()
        h.addWidget(self.stack, 1)

        self.welcome = WelcomePage(self.login)
        self.library = LibraryPage(self.images, self.downloads)
        self.game = GamePage(self.images, self.state, self.downloads)
        self.settings = SettingsPage(self)
        self.downloads_page = DownloadsPage(self.downloads)
        self.cloud = CloudPage(self)
        self.store = StorePage(self.owned_slug)
        self.profile = ProfilePage(self, self.images)
        for w in (self.welcome, self.library, self.game, self.settings, self.downloads_page, self.cloud,
                  self.store, self.profile):
            self.stack.addWidget(w)
        self.store.open_game.connect(self.open_game_id)
        self.store.purchase_finished.connect(self._after_purchase)
        self.profile.open_store.connect(self.show_store)
        self.profile.open_game.connect(self.open_game_id)

        self.library.open_game.connect(self.open_game)
        self.library.refresh_requested.connect(self.refresh_library)
        self.game.back.connect(lambda: self.stack.setCurrentWidget(self.library))
        self.game.link_requested.connect(self.link_install)
        self.game.unlink_requested.connect(self.unlink)
        self.game.changed.connect(lambda _gid: self.reload_entries())
        self.game.uninstall_requested.connect(self.uninstall)
        self.game.show_downloads.connect(lambda: self.navigate("downloads"))
        self.game.show_cloud.connect(self.show_cloud)
        self.downloads.changed.connect(
            lambda: self.sidebar.set_downloads(self.downloads.pending_count()))
        self.downloads.progressed.connect(self.game.on_download_progress)
        self.downloads.progressed.connect(lambda _g: self.library.view.viewport().update())
        self.downloads.finished.connect(self._download_finished)
        for key, b in self.sidebar.buttons.items():
            b.clicked.connect(lambda _=False, k=key: self.navigate(k))
        self.stack.currentChanged.connect(self._page_changed)
        nav = navigation.instance()
        if nav is not None:
            nav.mode_changed.connect(self._gamepad_mode)

        self.entries: dict[str, Entry] = {}
        if self.auth.is_logged_in():
            self.sidebar.set_user(self._cached_user())
            self.reload_entries()
            self.navigate("all")
            self.refresh_library()
        else:
            self.show_welcome()

    # --- navegación ----------------------------------------------------

    def navigate(self, key: str):
        if not self.auth.is_logged_in():
            self.show_welcome()
            return
        self.sidebar.buttons[key].setChecked(True)
        if key == "settings":
            self.settings.refresh()
            self.stack.setCurrentWidget(self.settings)
        elif key == "downloads":
            self.stack.setCurrentWidget(self.downloads_page)
        elif key == "cloud":
            self.cloud.refresh()
            self.stack.setCurrentWidget(self.cloud)
        elif key == "store":
            self.store.ensure_view()
            self.stack.setCurrentWidget(self.store)
        elif key == "profile":
            self.profile.refresh()
            self.stack.setCurrentWidget(self.profile)
        else:
            self.library.set_mode(key)
            self.stack.setCurrentWidget(self.library)

    def show_cloud(self, game_id: str):
        self.sidebar.buttons["cloud"].setChecked(True)
        self.cloud.refresh(game_id)
        self.stack.setCurrentWidget(self.cloud)

    def show_welcome(self):
        self.sidebar.set_user(None)
        for b in self.sidebar.buttons.values():
            b.setEnabled(False)
        self.stack.setCurrentWidget(self.welcome)

    def open_game(self, entry: Entry):
        self.game.show_entry(entry)
        self.stack.setCurrentWidget(self.game)

    def open_game_id(self, game_id: str):
        e = self.entries.get(game_id)
        if e:
            self.sidebar.buttons["all"].setChecked(True)
            self.open_game(e)

    def show_store(self, path_or_url: str):
        self.sidebar.buttons["store"].setChecked(True)
        self.store.go(path_or_url)
        self.stack.setCurrentWidget(self.store)

    def owned_slug(self, slug: str) -> tuple[str, str] | None:
        """(game_id, título) si la página /game/<slug> de la tienda es de un juego tuyo."""
        for e in self.entries.values():
            if (e.product.get("url") or "").rstrip("/").endswith(f"/game/{slug}"):
                return e.game_id, e.title
        return None

    def _after_purchase(self):
        """Tras pasar por el checkout: refresca la biblioteca y enseña lo nuevo."""
        before = set(self.entries)

        def done():
            new = [self.entries[g] for g in set(self.entries) - before]
            if not new:
                return
            names = ", ".join(e.title for e in new[:3]) + ("…" if len(new) > 3 else "")
            first = new[0].game_id
            self.store.show_banner("🎉 " + _("Ya está en tu biblioteca: {names}").format(names=f"<b>{names}</b>"),
                                   _("Abrir su ficha"), lambda: self.open_game_id(first), kind="purchase")
            self.sidebar.status.setText(_("Nuevo en tu biblioteca: {names}").format(names=names))

        self.refresh_library(done)

    # --- mando ---------------------------------------------------------

    def _gamepad_mode(self, on: bool):
        self.hints.setVisible(on)
        if on:
            self._page_changed()

    def _page_changed(self, *_):
        page = self.stack.currentWidget()
        if page is not self.store:
            self.store.leaving()
        self.hints.set_context("library" if page is self.library else
                               "game" if page is self.game else "other")
        if navigation.gamepad_active():
            # Tras construirse la página (p. ej. los botones de la ficha del juego).
            QTimer.singleShot(0, self._focus_page)

    def _focus_page(self):
        w = self.gamepad_default_focus()
        if w is not None:
            navigation.focus_widget(w)

    def gamepad_default_focus(self):
        page = self.stack.currentWidget()
        if page is self.library:
            return self.library.view if self.library.view.isVisible() else self.library.search
        if page is self.game:
            return self.game.primary_button()
        if page is self.welcome:
            return self.welcome.login_btn
        if page is self.cloud and self.cloud.first_button is not None:
            return self.cloud.first_button
        cands = navigation.focusable(page)
        return min(cands, key=lambda w: (navigation.global_rect(w).top(),
                                         navigation.global_rect(w).left())) if cands else None

    def gamepad_back(self) -> bool:
        page = self.stack.currentWidget()
        focus = QApplication.focusWidget()
        if page is self.game:
            self.stack.setCurrentWidget(self.library)
            return True
        if page is self.library:
            if focus is not self.library.view:
                self._focus_page()
            return True
        if page is self.cloud and self.cloud.back():
            return True
        if page is self.store and self.store.view is not None and self.store.view.history().canGoBack():
            self.store.view.back()
            return True
        if page in (self.settings, self.downloads_page, self.cloud, self.store, self.profile):
            self.navigate("all")
            return True
        return False

    def gamepad_button(self, name: str) -> bool:
        if not self.auth.is_logged_in():
            return False
        if name in ("lb", "rb"):
            checked = next((k for k, b in self.sidebar.buttons.items() if b.isChecked()), "all")
            i = SECTIONS.index(checked) if checked in SECTIONS else 0
            self.navigate(SECTIONS[(i + (1 if name == "rb" else -1)) % len(SECTIONS)])
            QTimer.singleShot(0, self._focus_page)  # aunque la página no cambie (filtros)
            return True
        if name == "y":
            if self.stack.currentWidget() is not self.library:
                self.navigate("all")
            navigation.focus_widget(self.library.search)
            navigation.open_keyboard()
            return True
        if self.stack.currentWidget() is self.game:
            if name == "start":
                b = self.game.primary_button()
                if b is not None and b.isEnabled():
                    b.animateClick()
                return True
            if name == "x":
                return self.game.open_manage_menu()
        return False

    # --- sesión --------------------------------------------------------

    def login(self):
        dlg = LoginDialog(self)
        if not dlg.exec() or not dlg.code:
            return
        code = dlg.code
        self.sidebar.status.setText(_("Iniciando sesión…"))

        def ok(_user_id):
            for b in self.sidebar.buttons.values():
                b.setEnabled(True)
            self.navigate("all")
            self.refresh_library()

        run_task(lambda b: b.auth.login_with_code(code), ok,
                 lambda e: self._error(_("No se pudo iniciar sesión"), e))

    def logout(self):
        if QMessageBox.question(self, _("Cerrar sesión"),
                                _("¿Cerrar sesión? Los juegos vinculados y sus partidas no se tocan.")) != QMessageBox.Yes:
            return
        self.auth.logout()
        from .web import HAS_WEBENGINE, clear_session
        if HAS_WEBENGINE:
            clear_session()  # que la tienda tampoco siga con tu sesión
        self.show_welcome()

    # --- biblioteca ----------------------------------------------------

    def refresh_library(self, then=None):
        self.sidebar.status.setText(_("Actualizando biblioteca…"))

        def fetch(b):
            return fetch_owned(b.api), fetch_user(b.api)

        def done(res):
            products, user = res
            self.state.replace_library(products)
            name = user.get("username")
            (paths.cache_dir() / USER_CACHE).write_text(json.dumps({"username": name}))
            self.sidebar.set_user(name)
            self.reload_entries()
            self.sidebar.status.setText(_("Biblioteca actualizada · {time}").format(time=time.strftime("%H:%M")))
            if then:
                then()

        run_task(fetch, done, lambda e: self.sidebar.status.setText(_("No se pudo actualizar: {error}").format(error=e)))

    def reload_entries(self):
        games = self.state.games()
        statuses = {g.game_id: s for g in games if (s := self.state.status(g.game_id))}
        entries = build_entries(self.state.library(), games, statuses)
        self.entries = {e.game_id: e for e in entries}
        bar = self.library.view.verticalScrollBar()
        pos = bar.value()
        self.library.set_entries(entries)
        bar.setValue(pos)
        self.sidebar.set_pending(sum(1 for e in entries if e.sync in ("pending", "conflict")))
        cur = self.game.entry
        if cur and cur.game_id in self.entries and self.stack.currentWidget() is self.game:
            self.game.show_entry(self.entries[cur.game_id])

    def _cached_user(self) -> str | None:
        try:
            return json.loads((paths.cache_dir() / USER_CACHE).read_text()).get("username")
        except (OSError, ValueError):
            return None

    # --- instalación ---------------------------------------------------

    def link_install(self, entry: Entry):
        dlg = LinkInstallDialog(entry.title, "windows" in entry.platforms, self)
        if not dlg.exec():
            return
        install, prefix = dlg.install.path(), dlg.prefix.path()
        self.sidebar.status.setText(_("Vinculando {title}…").format(title=entry.title))

        def work(b):
            game = register_installed(install, prefix, b.api)
            old = b.state.game(game.game_id)
            if old:
                game.extra = old.extra
            b.state.save_game(game)
            return game

        def done(game):
            self.sidebar.status.setText(_("{title} vinculado").format(title=game.title))
            if game.game_id != entry.game_id:
                QMessageBox.information(self, _("Juego distinto"), _(
                    "Esa carpeta corresponde a «{found}» ({id}), no a «{expected}». Se ha vinculado "
                    "igualmente a su juego.").format(found=game.title, id=game.game_id, expected=entry.title))
            self.reload_entries()
            if game.game_id in self.entries:
                self.open_game(self.entries[game.game_id])

        run_task(work, done, lambda e: self._error(_("No se pudo vincular la instalación"), e))

    def unlink(self, entry: Entry):
        if QMessageBox.question(self, _("Desvincular"), _(
                "¿Quitar «{title}» del lanzador?\nNo se borra ningún archivo del juego ni de sus "
                "partidas.").format(title=entry.title)) != QMessageBox.Yes:
            return
        self.state.remove_game(entry.game_id)
        self.reload_entries()
        if entry.game_id in self.entries:
            self.open_game(self.entries[entry.game_id])
        else:
            self.navigate("all")

    def _download_finished(self, game_id: str, ok: bool, message: str):
        self.reload_entries()
        e = self.entries.get(game_id)
        title = e.title if e else game_id
        self.sidebar.status.setText(f"{title}: {message}")
        if self.game.entry and self.game.entry.game_id == game_id and e:
            self.game.updates.pop(game_id, None)
            self.game.show_entry(e)

    def uninstall(self, entry: Entry):
        from PySide6.QtWidgets import QCheckBox
        from gogl.content import uninstall
        box = QMessageBox(self)
        box.setWindowTitle(_("Desinstalar"))
        box.setText(_("¿Desinstalar «{title}»?").format(title=entry.title))
        box.setInformativeText(_(
            "Se borrará {path}.\n\nAntes se guarda una copia de seguridad de las partidas locales, y las "
            "de la nube de GOG no se tocan.").format(path=entry.game.install_path))
        rm_prefix = QCheckBox(_("Borrar también el prefijo de Proton (configuración del juego en Wine)"))
        box.setCheckBox(rm_prefix)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.button(QMessageBox.Yes).setText(_("Desinstalar"))
        if box.exec() != QMessageBox.Yes:
            return
        gid, remove_prefix = entry.game_id, rm_prefix.isChecked()
        self.sidebar.status.setText(_("Desinstalando {title}…").format(title=entry.title))

        def done(_r):
            self.sidebar.status.setText(_("{title} desinstalado").format(title=entry.title))
            self.reload_entries()
            if gid in self.entries:
                self.open_game(self.entries[gid])

        run_task(lambda b: uninstall(b.state, gid, remove_prefix), done,
                 lambda e: self._error(_("No se pudo desinstalar"), e))

    def _error(self, title: str, e: Exception):
        self.sidebar.status.setText(title)
        QMessageBox.warning(self, title, str(e))
