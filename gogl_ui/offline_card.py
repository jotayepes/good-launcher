"""Tarjeta «Extras e instaladores» de la ficha: goodies e instaladores offline."""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from gogl import languages
from gogl.i18n import _, n_
from gogl.content.offline import (Bundle, Offline, bundle_dir, downloaded_files, downloads_root, list_downloads,
                                  safe_name)

from . import theme
from .workers import run_task

OS_NAMES = {"windows": "Windows", "linux": "Linux", "mac": "macOS"}
EXTRAS_PAGE = 6


def human(n: int) -> str:
    from .downloads import human as h
    return h(n)


def _open(path) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


class OfflinePanel(QWidget):
    def __init__(self, downloads):
        super().__init__()
        self.downloads = downloads
        self.game_id: str | None = None
        self.title = ""
        self.data: Offline | None = None
        self.os = ""
        self.lang = ""
        self.expanded = False
        self.buttons: dict[str, QPushButton] = {}
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(8)
        if downloads:
            downloads.progressed.connect(self._on_progress)
            downloads.changed.connect(self._refresh_buttons)

    def show_game(self, game_id: str, title: str, preferred_os: str):
        if game_id == self.game_id and self.data is not None:
            self._render()
            return
        self.game_id, self.title, self.data = game_id, title, None
        self.os, self.lang, self.expanded = preferred_os, "", False
        self._message(_("Buscando extras e instaladores…"))
        run_task(lambda b: list_downloads(b.api, game_id), lambda d: self._loaded(game_id, d),
                 lambda e: self._failed(game_id, e))

    def _loaded(self, game_id: str, data: Offline):
        if game_id != self.game_id:
            return
        self.data = data
        oses = sorted({b.os for b in data.installers})
        if self.os not in oses and oses:
            self.os = "windows" if "windows" in oses else oses[0]
        self._render()

    def _failed(self, game_id: str, e: Exception):
        if game_id == self.game_id:
            self._message(_("No se pudo consultar GOG: {error}").format(error=e))

    def _clear(self):
        from .game_page import clear
        clear(self.lay)
        self.buttons = {}

    def _message(self, text: str):
        self._clear()
        l = QLabel(text)
        l.setObjectName("Muted")
        l.setWordWrap(True)
        self.lay.addWidget(l)

    def _section(self, text: str):
        l = QLabel(text)
        l.setStyleSheet(f"color: {theme.MUTED}; font-size: 8pt; font-weight: 800; letter-spacing: 1px;")
        self.lay.addWidget(l)

    def _render(self):
        self._clear()
        d = self.data
        if not d.installers and not d.extras:
            self._message(_("Este juego no tiene extras ni instaladores offline."))
            return
        if d.installers:
            self._render_installers(d.installers)
        if d.extras:
            self._render_extras(d.extras)
        foot = QHBoxLayout()
        where = QLabel(_("Se guardan en {path}").format(path=downloads_root()))
        where.setObjectName("Muted")
        where.setWordWrap(True)
        foot.addWidget(where, 1)
        folder = downloads_root() / safe_name(self.title)
        open_ = QPushButton(_("Abrir carpeta"))
        open_.setObjectName("Ghost")
        open_.setEnabled(folder.exists())
        open_.clicked.connect(lambda: _open(folder))
        foot.addWidget(open_)
        self.lay.addLayout(foot)

    def _render_installers(self, installers: list[Bundle]):
        self._section(_("INSTALADORES OFFLINE"))
        oses = sorted({b.os for b in installers}, key=lambda o: list(OS_NAMES).index(o) if o in OS_NAMES else 9)
        langs = sorted({b.language for b in installers if b.os == self.os})
        if self.lang not in langs:
            mine = languages.preferred().split("-")[0]
            self.lang = next((l for l in langs if l.startswith(mine)), "en" if "en" in langs else langs[0])
        filters = QHBoxLayout()
        os_box = QComboBox()
        for o in oses:
            os_box.addItem(OS_NAMES.get(o, o), o)
        os_box.setCurrentIndex(max(os_box.findData(self.os), 0))
        os_box.currentIndexChanged.connect(lambda _i: self._set_filter(os_box.currentData(), self.lang))
        filters.addWidget(os_box)
        lang_box = QComboBox()
        names = {b.language: b.language_name for b in installers}
        for l in langs:
            lang_box.addItem(names.get(l) or languages.name(l), l)
        lang_box.setCurrentIndex(max(lang_box.findData(self.lang), 0))
        lang_box.currentIndexChanged.connect(lambda _i: self._set_filter(self.os, lang_box.currentData()))
        filters.addWidget(lang_box)
        filters.addStretch()
        self.lay.addLayout(filters)
        for b in installers:
            if b.os == self.os and b.language == self.lang:
                parts = len(b.files)
                desc = f"{b.version or _('sin versión')} · {human(b.total_size)}"
                if parts > 1:
                    desc += " · " + n_("{n} archivo", "{n} archivos", parts).format(n=parts)
                self._row(b, b.name if b.product_id == self.game_id else f"DLC: {b.name}", desc)

    def _render_extras(self, extras: list[Bundle]):
        self._section(_("EXTRAS") + f" ({len(extras)})")
        limit = len(extras) if self.expanded else EXTRAS_PAGE
        for b in extras[:limit]:
            self._row(b, b.name[:1].upper() + b.name[1:], f"{b.type_label} · {human(b.total_size)}")
        row = QHBoxLayout()
        if len(extras) > limit:
            more = QPushButton(_("Ver todos ({n})").format(n=len(extras)))
            more.setObjectName("Ghost")
            more.clicked.connect(self._expand)
            row.addWidget(more)
        row.addStretch()
        pending = [b for b in extras if not downloaded_files(b)]
        if len(pending) > 1:
            total = sum(b.total_size for b in pending)
            all_ = QPushButton(_("Descargar todos ({size})").format(size=human(total)))
            all_.clicked.connect(lambda: [self._download(b) for b in pending])
            row.addWidget(all_)
        self.lay.addLayout(row)

    def _row(self, b: Bundle, name: str, desc: str):
        row = QHBoxLayout()
        text = QVBoxLayout()
        text.setSpacing(0)
        n = QLabel(name)
        n.setStyleSheet("font-weight: 700;")
        n.setWordWrap(True)
        text.addWidget(n)
        dl = QLabel(desc)
        dl.setObjectName("Muted")
        text.addWidget(dl)
        row.addLayout(text, 1)
        btn = QPushButton()
        btn.setMinimumWidth(110)
        btn.clicked.connect(lambda _=False, bb=b: self._action(bb))
        row.addWidget(btn, 0, Qt.AlignVCenter)
        self.buttons[b.key] = btn
        self._update_button(b)
        self.lay.addLayout(row)

    def _bundle(self, key: str) -> Bundle | None:
        if not self.data:
            return None
        return next((b for b in self.data.installers + self.data.extras if b.key == key), None)

    def _update_button(self, b: Bundle):
        btn = self.buttons.get(b.key)
        if btn is None:
            return
        job = self.downloads.active(b.key) if self.downloads else None
        if job:
            btn.setText(_("En cola") if job.status == "queued" else f"{job.progress.fraction * 100:.0f}%")
            btn.setToolTip(_("Ver en Descargas"))
        elif downloaded_files(b):
            btn.setText(_("Abrir"))
            btn.setToolTip(str(bundle_dir(b)))
        else:
            btn.setText(_("Descargar"))
            btn.setToolTip(str(bundle_dir(b)))
        btn.setEnabled(self.downloads is not None)

    def _action(self, b: Bundle):
        job = self.downloads.active(b.key)
        if job:
            self.window().navigate("downloads")
        elif downloaded_files(b):
            files = downloaded_files(b)
            _open(files[0] if b.kind == "extra" and len(files) == 1 and files[0].suffix.lower() == ".pdf"
                  else bundle_dir(b))
        else:
            self._download(b)

    def _download(self, b: Bundle):
        from .downloads import Job
        if self.downloads.active(b.key) or downloaded_files(b):
            return
        label = (_("Extra · {name}").format(name=b.name) if b.kind == "extra" else
                 _("Instalador {os} ({language})").format(os=OS_NAMES.get(b.os, b.os),
                                                          language=b.language_name or b.language))
        self.downloads.enqueue(Job(self.game_id, self.title, "offline", {"label": label}, key=b.key))
        self._update_button(b)

    def _set_filter(self, os_: str, lang: str):
        self.os, self.lang = os_, lang
        self._render()

    def _expand(self):
        self.expanded = True
        self._render()

    def _on_progress(self, key: str):
        b = self._bundle(key) if key in self.buttons else None
        if b:
            try:
                self._update_button(b)
            except RuntimeError:
                pass

    def _refresh_buttons(self):
        for key in list(self.buttons):
            b = self._bundle(key)
            if b:
                try:
                    self._update_button(b)
                except RuntimeError:
                    pass


__all__ = ["OfflinePanel"]
