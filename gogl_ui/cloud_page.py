"""Partidas en la nube de toda la cuenta: espacio por juego y gestión de partidas
(lo mismo que gog.com/account/cloud-saves, sin salir del lanzador)."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel, QMessageBox, QProgressBar,
                               QPushButton, QScrollArea, QVBoxLayout, QWidget)

from gogl import i18n
from gogl.i18n import _, n_
from gogl.saves.account import AccountCloud, Container, SaveGroup

from . import navigation, theme
from .downloads import human
from .game_page import clear, pill
from .workers import run_task


def quota_bar(c: Container) -> QProgressBar:
    bar = QProgressBar()
    bar.setRange(0, 1000)
    bar.setValue(min(1000, int(c.usage * 1000)))
    bar.setTextVisible(False)
    color = theme.DANGER if c.usage >= 0.95 else theme.WARN if c.usage >= 0.8 else theme.ACCENT
    bar.setStyleSheet(f"QProgressBar::chunk {{ background: {color}; border-radius: 3px; }}")
    return bar


def usage_text(c: Container) -> str:
    return (_("{used} de {quota}").format(used=human(c.size), quota=human(c.quota)) + "  ·  " +
            n_("{n} archivo", "{n} archivos", c.files).format(n=c.files))


def _when(ts: float | None) -> str:
    return time.strftime(i18n.date_format(), time.localtime(ts)) if ts else "—"


class CloudPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.containers: list[Container] = []
        self.current: Container | None = None
        self.groups: list[SaveGroup] = []
        self.checks: list[tuple[QCheckBox, SaveGroup]] = []
        self.busy = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(self.scroll)
        body = QWidget()
        self.scroll.setWidget(body)
        self.lay = QVBoxLayout(body)
        self.lay.setContentsMargins(28, 22, 28, 28)
        self.lay.setSpacing(12)
        self.first_button: QPushButton | None = None

    # --- carga ---------------------------------------------------------

    def _titles(self) -> dict[str, str]:
        return {str(p["id"]): p.get("title", "") for p in self.window.state.library()}

    def refresh(self, game_id: str | None = None):
        """Lista de juegos; con game_id, abre directamente las partidas de ese juego."""
        self.current = None
        self._message(_("Partidas en la nube"), _("Consultando la nube de GOG…"))
        titles = self._titles()

        def done(containers: list[Container]):
            self._show_list(containers)
            c = next((x for x in containers if game_id in x.game_ids), None) if game_id else None
            if c is not None:
                self.open(c)
            elif game_id:
                self.window.sidebar.status.setText(_("Este juego no tiene partidas en la nube"))

        run_task(lambda b: AccountCloud(b.auth).containers(titles), done,
                 lambda e: self._message(_("Partidas en la nube"),
                                         _("No se pudo consultar la nube: {error}").format(error=e),
                                         retry=self.refresh))

    def _message(self, title: str, text: str, retry=None):
        clear(self.lay)
        self.first_button = None
        self._title(title)
        m = QLabel(text)
        m.setObjectName("Muted")
        m.setWordWrap(True)
        self.lay.addWidget(m)
        if retry:
            b = QPushButton(_("Reintentar"))
            b.clicked.connect(retry)
            self.lay.addWidget(b, 0, Qt.AlignLeft)
            self.first_button = b
        self.lay.addStretch()

    def _title(self, text: str, back: bool = False):
        if back:
            b = QPushButton("←  " + _("Todas las partidas"))
            b.setObjectName("Ghost")
            b.clicked.connect(lambda: self._show_list(self.containers))
            self.lay.addWidget(b, 0, Qt.AlignLeft)
            self.back_button = b
        t = QLabel(text)
        t.setStyleSheet("font-size: 20pt; font-weight: 800;")
        t.setWordWrap(True)
        self.lay.addWidget(t)

    # --- lista de juegos --------------------------------------------------

    def _show_list(self, containers: list[Container]):
        self.containers, self.current = containers, None
        clear(self.lay)
        self.first_button = None
        self._title(_("Partidas en la nube"))
        total = sum(c.size for c in containers)
        near = sum(1 for c in containers if c.usage >= 0.8)
        n = len(containers)
        quota = f" ({human(containers[0].quota)})" if containers else ""
        info = QLabel(n_("{n} juego usa {size} en la nube de GOG.", "{n} juegos usan {size} en la nube de GOG.", n)
                      .format(n=n, size=human(total)) + " " +
                      _("Cada juego tiene su propia cuota{quota}: si se llena, GOG borra primero los archivos "
                        "que no reconoce como partidas y luego las partidas más antiguas.").format(quota=quota)
                      + ("\n" + n_("{n} cerca del límite.", "{n} cerca del límite.", near).format(n=near)
                         if near else ""))
        info.setObjectName("Muted")
        info.setWordWrap(True)
        self.lay.addWidget(info)
        if not containers:
            self.lay.addStretch()
            return
        for c in containers:
            self.lay.addWidget(self._row(c))
        self.lay.addStretch()
        self.scroll.verticalScrollBar().setValue(0)
        self._gamepad_focus()

    def _gamepad_focus(self):
        if navigation.gamepad_active() and self.first_button is not None:
            navigation.focus_widget(self.first_button)

    def _row(self, c: Container) -> QFrame:
        f = QFrame()
        f.setObjectName("Card")
        lay = QHBoxLayout(f)
        lay.setContentsMargins(18, 12, 18, 12)
        lay.setSpacing(16)
        col = QVBoxLayout()
        col.setSpacing(6)
        head = QHBoxLayout()
        t = QLabel(c.title)
        t.setObjectName("CardTitle")
        head.addWidget(t)
        if any(self.window.state.game(g) for g in c.game_ids):
            head.addWidget(pill(_("En este equipo"), theme.OK))
        if c.usage >= 0.95:
            head.addWidget(pill(_("Cuota casi llena") if c.usage < 1 else _("Cuota llena"), theme.DANGER))
        head.addStretch()
        col.addLayout(head)
        col.addWidget(quota_bar(c))
        u = QLabel(usage_text(c))
        u.setObjectName("Muted")
        col.addWidget(u)
        lay.addLayout(col, 1)
        b = QPushButton(_("Ver partidas"))
        b.clicked.connect(lambda _=False, x=c: self.open(x))
        lay.addWidget(b)
        if self.first_button is None:
            self.first_button = b
        return f

    # --- partidas de un juego ---------------------------------------------

    def open(self, c: Container):
        self.current = c
        self._message(c.title, _("Cargando partidas…"))
        sid = c.space_id
        run_task(lambda b: AccountCloud(b.auth).saves(sid), lambda g: self._show_saves(c, g),
                 lambda e: self._message(c.title, _("No se pudieron cargar las partidas: {error}").format(error=e),
                                         retry=lambda: self.open(c)))

    def _show_saves(self, c: Container, groups: list[SaveGroup]):
        if self.current is not c:
            return
        self.groups = groups
        self.checks = []
        clear(self.lay)
        self._title(c.title, back=True)
        self.first_button = self.back_button
        self.lay.addWidget(quota_bar(c))
        u = QLabel(usage_text(c))
        u.setObjectName("Muted")
        self.lay.addWidget(u)
        if any(self.window.state.game(g) for g in c.game_ids):
            note = QLabel(_("Este juego está en este equipo: lo que borres aquí también se quitará de tu "
                            "equipo en la próxima sincronización (antes se guarda una copia de seguridad). "
                            "Si borras todo, en cambio, se volverán a subir las partidas de este equipo."))
            note.setWordWrap(True)
            note.setStyleSheet(f"color: {theme.WARN};")
            self.lay.addWidget(note)

        row = QHBoxLayout()
        self.del_btn = QPushButton(_("Borrar seleccionadas"))
        self.del_btn.setEnabled(False)
        self.del_btn.clicked.connect(self._delete_selected)
        sel_all = QPushButton(_("Seleccionar todo"))
        sel_all.setObjectName("Ghost")
        sel_all.clicked.connect(self._toggle_all)
        auto = QPushButton(_("Limpieza de GOG…"))
        auto.setToolTip(_("Lo que GOG borraría por su cuenta: archivos que no reconoce como partidas y, "
                          "si se supera la cuota, las partidas más antiguas"))
        auto.clicked.connect(self._auto_clean)
        wipe = QPushButton(_("Borrar todo…"))
        wipe.setObjectName("Ghost")
        wipe.clicked.connect(self._delete_all)
        for w in (self.del_btn, sel_all):
            row.addWidget(w)
        row.addStretch()
        row.addWidget(auto)
        row.addWidget(wipe)
        self.lay.addLayout(row)

        if not groups:
            m = QLabel(_("No hay partidas en la nube para este juego."))
            m.setObjectName("Muted")
            self.lay.addWidget(m)
        box = QFrame()
        box.setObjectName("Card")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(16, 10, 16, 10)
        bl.setSpacing(2)
        for g in groups:
            bl.addWidget(self._group_row(g))
        if groups:
            self.lay.addWidget(box)
        self.lay.addStretch()
        self.scroll.verticalScrollBar().setValue(0)
        self._gamepad_focus()

    def _group_row(self, g: SaveGroup) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        name = _("Archivos que GOG no reconoce como partidas") if g.unrelated else g.name
        cb = QCheckBox(name)
        cb.setToolTip("\n".join(g.files[:40]) + ("\n…" if len(g.files) > 40 else ""))
        cb.toggled.connect(self._update_delete)
        lay.addWidget(cb, 1)
        if g.unrelated:
            lay.addWidget(pill(_("No reconocido"), theme.WARN))
        meta = QLabel(f"{_when(g.timestamp)}   ·   {human(g.size)}"
                      + ("   ·   " + n_("{n} archivo", "{n} archivos", len(g.files)).format(n=len(g.files))
                         if len(g.files) > 1 else ""))
        meta.setObjectName("Muted")
        lay.addWidget(meta)
        self.checks.append((cb, g))
        return w

    def _selected(self) -> list[SaveGroup]:
        return [g for cb, g in self.checks if cb.isChecked()]

    def _update_delete(self, *_args):
        sel = self._selected()
        size = sum(g.size for g in sel)
        self.del_btn.setEnabled(bool(sel) and not self.busy)
        self.del_btn.setText(_("Borrar seleccionadas") + (f" ({len(sel)} · {human(size)})" if sel else ""))

    def _toggle_all(self):
        on = not all(cb.isChecked() for cb, _ in self.checks)
        for cb, _ in self.checks:
            cb.blockSignals(True)
            cb.setChecked(on)
            cb.blockSignals(False)
        self._update_delete()

    # --- borrar ---------------------------------------------------------------

    def _confirm(self, text: str) -> bool:
        return QMessageBox.question(self, _("Borrar partidas en la nube"), text) == QMessageBox.Yes

    def _run_delete(self, fn, done_msg: str):
        c = self.current
        self.busy = True
        self._update_delete()

        def done(_r):
            self.busy = False
            self.window.sidebar.status.setText(done_msg)
            self._reload(c)

        def failed(e):
            self.busy = False
            QMessageBox.warning(self, _("No se pudo borrar"), str(e))
            self._reload(c)

        run_task(fn, done, failed)

    def _reload(self, c: Container):
        """Vuelve a leer el espacio usado y las partidas del juego tras borrar."""
        titles, sid = self._titles(), c.space_id

        def work(b):
            cloud = AccountCloud(b.auth)
            return cloud.containers(titles), cloud.saves(sid)

        def done(res):
            self.containers, groups = res
            fresh = next((x for x in self.containers if x.space_id == sid), None)
            if fresh is None:  # sin nada en la nube: el juego desaparece de la lista
                self._show_list(self.containers)
                return
            self.current = fresh
            self._show_saves(fresh, groups)

        run_task(work, done, lambda e: self._message(c.title, str(e), retry=lambda: self.open(c)))

    def _delete_selected(self):
        sel = self._selected()
        files = [f for g in sel for f in g.files]
        if not files or not self._confirm(
                n_("¿Borrar {n} partida ({files} archivos, {size}) de la nube de «{title}»?",
                   "¿Borrar {n} partidas ({files} archivos, {size}) de la nube de «{title}»?", len(sel))
                .format(n=len(sel), files=len(files), size=human(sum(g.size for g in sel)),
                        title=self.current.title) + "\n\n" + _("No se puede deshacer en la nube.")):
            return
        sid = self.current.space_id
        self._run_delete(lambda b: AccountCloud(b.auth).delete_files(sid, files),
                         n_("{n} archivo borrado de la nube", "{n} archivos borrados de la nube", len(files))
                         .format(n=len(files)))

    def _auto_clean(self):
        c = self.current
        sid = c.space_id

        def show(groups: list[SaveGroup]):
            if not groups:
                QMessageBox.information(self, _("Limpieza de GOG"), _("GOG no propone borrar nada en este juego."))
                return
            lines = [f"•  {_('Archivos no reconocidos') if g.unrelated else g.name}  ({human(g.size)})"
                     for g in groups[:12]]
            if len(groups) > 12:
                lines.append(_("…y {n} más").format(n=len(groups) - 12))
            if self._confirm(_("GOG borraría esto:") + "\n\n" + "\n".join(lines) + "\n\n" +
                             _("Ojo: «no reconocido» solo significa que GOG no lo identifica como partida; "
                               "algunos juegos sí lo necesitan. ¿Borrarlo?")):
                self._run_delete(lambda b: AccountCloud(b.auth).auto_clean(sid), _("Limpieza de GOG hecha"))

        run_task(lambda b: AccountCloud(b.auth).auto_clean_preview(sid), show,
                 lambda e: QMessageBox.warning(self, _("Limpieza de GOG"), str(e)))

    def _delete_all(self):
        c = self.current
        if not self._confirm(_("¿Borrar TODAS las partidas de «{title}» de la nube ({files} archivos, {size})?")
                             .format(title=c.title, files=c.files, size=human(c.size)) + "\n\n" +
                             _("No se puede deshacer en la nube.")):
            return
        sid = c.space_id
        self._run_delete(lambda b: AccountCloud(b.auth).delete_all(sid),
                         _("{title}: nube vaciada").format(title=c.title))

    # --- mando -------------------------------------------------------------

    def back(self) -> bool:
        if self.current is not None and self.containers:
            self._show_list(self.containers)
            return True
        return False
