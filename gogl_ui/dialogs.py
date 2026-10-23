"""Diálogos: inicio de sesión, vincular instalación y conflictos de partidas."""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QFileDialog, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from gogl import APP_NAME
from gogl.auth import REDIRECT_URI, login_url
from gogl.saves.cloud import parse_ts
from gogl.saves.engine import SyncPlan

from .web import HAS_WEBENGINE


class LoginDialog(QDialog):
    """Abre la página de login de GOG y captura el código de la redirección.

    Todo el proceso (contraseña, captcha y verificación en dos pasos) ocurre en
    las páginas de GOG; solo se lee la URL final on_login_success?code=…"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Iniciar sesión en GOG")
        self.code: str | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        if HAS_WEBENGINE:
            self.resize(520, 720)
            # Mismo perfil persistente que la tienda: al iniciar sesión aquí, gog.com
            # también queda con la sesión abierta (ver web.py).
            from .web import make_view
            self.view = make_view(self)
            self.view.urlChanged.connect(self._on_url)
            self.view.load(QUrl(login_url()))
            lay.addWidget(self.view, 1)
        manual = QWidget()
        self._manual(QVBoxLayout(manual))
        if HAS_WEBENGINE:
            manual.hide()
            alt = QPushButton("¿Problemas? Iniciar sesión en tu navegador")
            alt.setObjectName("Ghost")
            alt.clicked.connect(lambda: (alt.hide(), manual.show(),
                                         QDesktopServices.openUrl(QUrl(login_url()))))
            lay.addWidget(alt, 0, Qt.AlignCenter)
        else:
            self.resize(560, 240)
        lay.addWidget(manual)

    def _manual(self, lay):
        lay.setContentsMargins(24, 16, 24, 20)
        info = QLabel("Inicia sesión en el navegador (incluido el código de verificación en dos pasos "
                      "si lo tienes activado). Al terminar llegarás a una página en blanco de "
                      "embed.gog.com/on_login_success: copia su URL completa y pégala aquí.")
        info.setWordWrap(True)
        info.setObjectName("Muted")
        lay.addWidget(info)
        link = QLineEdit(login_url())
        link.setReadOnly(True)
        lay.addWidget(link)
        self.paste = QLineEdit()
        self.paste.setPlaceholderText("Pega aquí la URL final o el código")
        lay.addWidget(self.paste)
        ok = QPushButton("Continuar")
        ok.setObjectName("Primary")
        ok.clicked.connect(lambda: self._finish(self.paste.text()))
        lay.addWidget(ok, 0, Qt.AlignRight)

    def _on_url(self, url: QUrl):
        s = url.toString()
        if s.startswith(REDIRECT_URI.split("?")[0]) and "code=" in s:
            self._finish(s)

    def _finish(self, code_or_url: str):
        if code_or_url.strip():
            self.code = code_or_url.strip()
            self.accept()


def pick_directory(parent, title: str, start: str) -> str:
    """Elegir carpeta. Dentro de Steam/gamescope, el diálogo propio de Qt: el del
    escritorio (portal) ni se maneja con el mando ni siempre se ve en el modo juego."""
    from .navigation import in_steam
    opts = QFileDialog.ShowDirsOnly
    if in_steam():
        opts |= QFileDialog.DontUseNativeDialog
    return QFileDialog.getExistingDirectory(parent, title, start, opts)


class PathRow(QHBoxLayout):
    def __init__(self, placeholder: str, title: str, parent):
        super().__init__()
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        btn = QPushButton("Examinar…")
        btn.clicked.connect(lambda: self._browse(title, parent))
        self.addWidget(self.edit, 1)
        self.addWidget(btn)

    def _browse(self, title, parent):
        d = pick_directory(parent, title, self.edit.text() or str(Path.home()))
        if d:
            self.edit.setText(d)

    def path(self) -> Path | None:
        t = self.edit.text().strip()
        return Path(t).expanduser() if t else None


class LinkInstallDialog(QDialog):
    def __init__(self, title: str, windows: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Vincular instalación existente")
        self.resize(620, 0)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 22)
        lay.setSpacing(10)
        h = QLabel(f"<b>{title}</b>")
        h.setStyleSheet("font-size: 13pt;")
        lay.addWidget(h)
        info = QLabel("Indica la carpeta donde ya está instalado el juego (la que contiene "
                      "<code>goggame-*.info</code>). No se mueve ni se modifica nada.")
        info.setWordWrap(True)
        info.setObjectName("Muted")
        lay.addWidget(info)
        lay.addWidget(QLabel("Carpeta del juego"))
        self.install = PathRow("/home/…/Games/MiJuego", "Carpeta del juego", self)
        lay.addLayout(self.install)
        self.prefix = PathRow("…/compatdata/<id>  o  …/Prefixes/MiJuego", "Prefijo de Proton/Wine", self)
        if windows:
            lay.addWidget(QLabel(f"Prefijo de Proton/Wine existente (opcional: vacío = prefijo nuevo de {APP_NAME})"))
            lay.addLayout(self.prefix)
        self.error = QLabel()
        self.error.setStyleSheet("color: #f06c6c;")
        self.error.hide()
        lay.addWidget(self.error)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Vincular")
        ok.setObjectName("Primary")
        ok.clicked.connect(self._ok)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)

    def _ok(self):
        p = self.install.path()
        if not p or not p.is_dir():
            self.error.setText("Selecciona una carpeta de instalación válida")
            self.error.show()
            return
        self.accept()


class ConflictDialog(QDialog):
    """Pregunta una sola vez por juego qué versión conservar."""

    def __init__(self, title: str, plan: SyncPlan, parent=None):
        super().__init__(parent)
        self.prefer: str | None = None
        self.setWindowTitle("Conflicto de partidas")
        self.resize(720, 460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 22)
        lay.setSpacing(12)
        h = QLabel("Las partidas han cambiado en este equipo y en la nube")
        h.setStyleSheet("font-size: 14pt; font-weight: 800;")
        lay.addWidget(h)
        info = QLabel(f"<b>{title}</b> se ha jugado en otro equipo desde la última sincronización "
                      "y también aquí. Elige qué versión conservar. Antes de sobrescribir nada se "
                      "guarda una copia de seguridad de tus partidas locales.")
        info.setWordWrap(True)
        info.setObjectName("Muted")
        lay.addWidget(info)

        t = QTableWidget(len(plan.conflicts), 3)
        t.setHorizontalHeaderLabels(["Archivo", "En este equipo", "En la nube"])
        t.verticalHeader().hide()
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionMode(QAbstractItemView.NoSelection)
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for i, it in enumerate(plan.conflicts):
            loc = _fmt(it.local.mtime) + f"  ·  {_size(it.local.size)}" if it.local else "borrado"
            cl = _fmt(parse_ts(it.cloud.last_modified)) if it.cloud else "borrado"
            if it.cloud and it.cloud.size:
                cl += f"  ·  {_size(it.cloud.size)} (comprimido)"
            for c, txt in enumerate((it.key, loc, cl)):
                t.setItem(i, c, QTableWidgetItem(txt))
        t.resizeColumnsToContents()
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        lay.addWidget(t, 1)

        row = QHBoxLayout()
        cancel = QPushButton("Decidir más tarde")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        row.addStretch()
        local = QPushButton("Conservar la de este equipo")
        local.clicked.connect(lambda: self._pick("local"))
        cloud = QPushButton("Conservar la de la nube")
        cloud.setObjectName("Primary")
        cloud.clicked.connect(lambda: self._pick("cloud"))
        row.addWidget(local)
        row.addWidget(cloud)
        lay.addLayout(row)

    def _pick(self, prefer: str):
        self.prefer = prefer
        self.accept()


def _fmt(ts: float | None) -> str:
    return time.strftime("%d/%m/%Y %H:%M", time.localtime(ts)) if ts else "?"


def _size(n: int | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class ProtonDownloadDialog(QDialog):
    """Descarga la última versión de Proton-GE al directorio de gogl."""

    def __init__(self, parent=None, release_name: str | None = None):
        from PySide6.QtWidgets import QProgressBar

        from .workers import run_task
        super().__init__(parent)
        self.setWindowTitle("Descargar Proton-GE")
        self.resize(480, 0)
        self.installed_name: str | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 22)
        lay.setSpacing(12)
        self.label = QLabel("Buscando la última versión de Proton-GE…")
        self.label.setWordWrap(True)
        lay.addWidget(self.label)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        lay.addWidget(self.bar)
        note = QLabel(f"Se guarda en la carpeta de datos de {APP_NAME}; no se toca la instalación de Steam.")
        note.setObjectName("Muted")
        note.setWordWrap(True)
        lay.addWidget(note)
        self.close_btn = QPushButton("Cancelar")
        self.close_btn.clicked.connect(self.reject)
        lay.addWidget(self.close_btn, 0, Qt.AlignRight)

        def work(b):
            from gogl.runner import proton
            rels = proton.available()
            rel = next((r for r in rels if r.name == release_name), None) if release_name else rels[0]
            if rel is None:
                raise proton.ProtonError(f"No se encuentra {release_name}")
            b.log(f"name:{rel.name}:{rel.size}")
            last = [-1]

            def progress(done, total):
                pct = int(done * 100 / total) if total else 0
                if pct != last[0]:
                    last[0] = pct
                    b.log(f"pct:{pct}")

            v = proton.install(rel, progress)
            if proton.default() is None or len(proton.installed()) == 1:
                proton.set_default(v.name)
            return v.name

        run_task(work, self._done, self._failed, self._progress)

    def _progress(self, msg: str):
        if msg.startswith("name:"):
            _, name, size = msg.split(":")
            self.label.setText(f"Descargando {name} ({int(size) / 2**20:.0f} MB)…")
            self.bar.setRange(0, 100)
        elif msg.startswith("pct:"):
            pct = int(msg[4:])
            self.bar.setValue(pct)
            if pct >= 100:
                self.label.setText("Verificando y descomprimiendo…")
                self.bar.setRange(0, 0)

    def _done(self, name: str):
        self.installed_name = name
        self.accept()

    def _failed(self, e: Exception):
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.label.setText(f"No se pudo descargar: {e}")
        self.close_btn.setText("Cerrar")


class InstallDialog(QDialog):
    """Elegir plataforma, idioma, DLC y carpeta antes de instalar."""

    def __init__(self, game_id: str, title: str, platforms: list[str], parent=None):
        from PySide6.QtWidgets import QButtonGroup, QCheckBox, QComboBox, QRadioButton

        from gogl.content import default_install_path, install_root
        from gogl.languages import NAMES as LANGUAGE_NAMES

        from .workers import run_task
        super().__init__(parent)
        self.setWindowTitle(f"Instalar {title}")
        self.setMinimumWidth(600)
        self.game_id, self.title = game_id, title
        self.info = None
        self.params: dict = {}
        self._names = LANGUAGE_NAMES
        self._default_path = default_install_path
        self._root = install_root()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 22)
        lay.setSpacing(10)
        h = QLabel(title)
        h.setStyleSheet("font-size: 15pt; font-weight: 800;")
        h.setWordWrap(True)
        lay.addWidget(h)

        self.plat_group = QButtonGroup(self)
        self.rb_win = QRadioButton("Windows con Proton-GE  ·  partidas en la nube de GOG")
        self.rb_lin = QRadioButton("Linux nativo  ·  sin partidas en la nube (GOG no las ofrece)")
        if "windows" in platforms and "linux" in platforms:
            lay.addWidget(QLabel("<b>Versión</b>"))
            for rb in (self.rb_win, self.rb_lin):
                self.plat_group.addButton(rb)
                lay.addWidget(rb)
            self.rb_win.setChecked(True)
            self.rb_win.toggled.connect(self._platform_changed)
        self.platform = "windows" if "windows" in platforms else "linux"

        self.win_box = QWidget()
        wl = QVBoxLayout(self.win_box)
        wl.setContentsMargins(0, 6, 0, 0)
        wl.setSpacing(8)
        wl.addWidget(QLabel("<b>Idioma</b>"))
        self.lang = QComboBox()
        self.lang.currentIndexChanged.connect(self._update_sizes)
        wl.addWidget(self.lang)
        self.lang_note = QLabel()
        self.lang_note.setObjectName("Muted")
        self.lang_note.setWordWrap(True)
        wl.addWidget(self.lang_note)
        self.dlc_title = QLabel("<b>Contenido adicional</b>")
        wl.addWidget(self.dlc_title)
        self.dlc_box = QVBoxLayout()
        wl.addLayout(self.dlc_box)
        self.dlc_checks: list[QCheckBox] = []
        self._QCheckBox = QCheckBox
        lay.addWidget(self.win_box)

        lay.addWidget(QLabel("<b>Carpeta de instalación</b>"))
        self.path = PathRow(str(self._root / "…"), "Carpeta de instalación", self)
        lay.addLayout(self.path)
        self.sizes = QLabel("Consultando a GOG…")
        self.sizes.setObjectName("Muted")
        self.sizes.setWordWrap(True)
        lay.addWidget(self.sizes)

        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        self.ok = QPushButton("Instalar")
        self.ok.setObjectName("Primary")
        self.ok.setEnabled(False)
        self.ok.clicked.connect(self._accept)
        row.addWidget(cancel)
        row.addWidget(self.ok)
        lay.addLayout(row)

        if self.platform == "windows":
            from gogl.content import build_info
            run_task(lambda b: build_info(b.api, game_id, "windows"), self._loaded, self._failed)
        else:
            self._platform_changed(False)

    def _loaded(self, info):
        self.info = info
        self.lang.blockSignals(True)
        for code in info.all_languages:
            self.lang.addItem(self._names.get(code, code), code)
        idx = self.lang.findData(info.default_language())
        self.lang.setCurrentIndex(max(idx, 0))
        self.lang.blockSignals(False)
        for pid, name in info.dlcs:
            cb = self._QCheckBox(name or pid)
            cb.setChecked(True)
            cb.setProperty("pid", pid)
            cb.toggled.connect(self._update_sizes)
            self.dlc_checks.append(cb)
            self.dlc_box.addWidget(cb)
            cb.show()
        self.dlc_title.setVisible(bool(info.dlcs))
        if not self.path.edit.text():
            self.path.edit.setText(str(self._default_path(info)))
        self.ok.setEnabled(True)
        self._update_sizes()
        self.adjustSize()

    def _failed(self, e: Exception):
        self.sizes.setText(f"No se pudo consultar el juego en GOG: {e}")
        self.sizes.setStyleSheet("color: #f06c6c;")

    def _platform_changed(self, windows: bool):
        self.platform = "windows" if windows else "linux"
        self.win_box.setVisible(windows)
        if self.platform == "linux":
            self.ok.setEnabled(True)
            if not self.path.edit.text() or self.info and self.path.edit.text() == str(self._default_path(self.info)):
                self.path.edit.setText(str(self._root / self.title))
            self.sizes.setText("Se descargará el instalador oficial de Linux de GOG.")
        else:
            if self.info:
                self.path.edit.setText(str(self._default_path(self.info)))
            self.ok.setEnabled(self.info is not None)
            self._update_sizes()
        self.adjustSize()

    def _update_sizes(self, *_):
        if not self.info or self.platform != "windows":
            return
        import shutil as _sh
        code = self.lang.currentData()
        shared = code not in self.info.languages and bool(self.info.languages)
        self.lang_note.setText(f"Este juego trae todos sus idiomas en el mismo paquete: {APP_NAME} hará que "
                               f"Windows (Wine) le indique {self._names.get(code, code).lower()} al "
                               f"arrancar. Si sale en otro idioma, cámbialo en sus opciones."
                               if shared else "")
        self.lang_note.setVisible(shared)
        down, disk = self.info.estimate(code, self._dlcs())
        p = Path(self.path.edit.text() or self._root)
        while not p.exists() and p != p.parent:
            p = p.parent
        free = _sh.disk_usage(p).free
        warn = "  ⚠ no hay espacio suficiente" if disk > free else ""
        self.sizes.setText(f"Descarga: {_size(down)}  ·  En disco: {_size(disk)}  ·  "
                           f"Libre: {_size(free)}{warn}   (versión {self.info.version or '?'})")
        self.sizes.setStyleSheet("color: #f5b942;" if warn else "")

    def _dlcs(self) -> list[str]:
        return [cb.property("pid") for cb in self.dlc_checks if cb.isChecked()]

    def _accept(self):
        path = self.path.path()
        self.params = {"platform": self.platform, "install_path": path}
        if self.platform == "windows":
            self.params.update(language=self.lang.currentData(), dlcs=self._dlcs())
        self.accept()


class LaunchOptionsDialog(QDialog):
    """Argumentos, variables de entorno y comando previo de un juego."""

    def __init__(self, title: str, options: dict, windows: bool, parent=None):
        from PySide6.QtWidgets import QPlainTextEdit
        super().__init__(parent)
        self.setWindowTitle(f"Opciones de lanzamiento · {title}")
        self.setMinimumWidth(620)
        self.options = dict(options)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 22)
        lay.setSpacing(8)

        def field(label: str, hint: str, widget):
            lay.addWidget(QLabel(f"<b>{label}</b>"))
            lay.addWidget(widget)
            h = QLabel(hint)
            h.setObjectName("Muted")
            h.setWordWrap(True)
            lay.addWidget(h)
            lay.addSpacing(6)

        self.args = QLineEdit(options.get("args", ""))
        self.args.setPlaceholderText("-windowed -skipintro")
        field("Argumentos", "Se añaden a los que GOG ya pasa al juego.", self.args)
        self.env = QPlainTextEdit(options.get("env", ""))
        self.env.setPlaceholderText("DXVK_HUD=fps\nPROTON_USE_WINED3D=1" if windows else "SDL_VIDEODRIVER=x11")
        self.env.setFixedHeight(96)
        field("Variables de entorno", "Una por línea, CLAVE=valor." + (
            " Útiles con Proton: PROTON_USE_WINED3D=1 (sin Vulkan), PROTON_NO_ESYNC=1, "
            "DXVK_HUD=fps." if windows else ""), self.env)
        self.wrapper = QLineEdit(options.get("wrapper", ""))
        self.wrapper.setPlaceholderText("mangohud  ·  gamemoderun")
        field("Comando previo", "Programa que envuelve al juego, p. ej. MangoHud o GameMode "
                                "(tienen que estar instalados en el sistema).", self.wrapper)

        self.error = QLabel()
        self.error.setStyleSheet("color: #f06c6c;")
        self.error.hide()
        lay.addWidget(self.error)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Guardar")
        ok.setObjectName("Primary")
        ok.clicked.connect(self._ok)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)

    def _ok(self):
        import shlex

        from gogl.runner.game import LaunchError, parse_env
        try:
            parse_env(self.env.toPlainText())
            shlex.split(self.wrapper.text())
            if self.args.text().count('"') % 2:
                raise ValueError
        except LaunchError as e:
            self.error.setText(str(e))
            self.error.show()
            return
        except ValueError:
            self.error.setText("Hay comillas sin cerrar en los argumentos o en el comando previo")
            self.error.show()
            return
        self.options = {k: v for k, v in (("args", self.args.text().strip()),
                                          ("env", self.env.toPlainText().strip()),
                                          ("wrapper", self.wrapper.text().strip())) if v}
        self.accept()
