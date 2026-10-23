"""Página «Perfil»: tu cuenta de GOG, logros y lista de deseos."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from gogl import profile as prof
from gogl.i18n import _, n_

from . import theme
from .game_page import card, clear
from .images import ImageStore
from .workers import run_task

AVATAR = 96


def _round(pm: QPixmap, size: int) -> QPixmap:
    pm = pm.scaled(size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    out = QPixmap(size, size)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addEllipse(0, 0, size, size)
    p.setClipPath(path)
    p.drawPixmap(0, 0, pm)
    p.end()
    return out


def tile(value: str, label: str) -> QFrame:
    f = QFrame()
    f.setObjectName("Card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(18, 14, 18, 14)
    v = QLabel(value)
    v.setStyleSheet("font-size: 20pt; font-weight: 800;")
    l = QLabel(label)
    l.setObjectName("Muted")
    lay.addWidget(v)
    lay.addWidget(l)
    return f


class ProfilePage(QWidget):
    open_store = Signal(str)     # ruta o URL de gog.com
    open_game = Signal(str)      # game_id

    def __init__(self, window, images: ImageStore):
        super().__init__()
        self.window_, self.images = window, images
        self.data: prof.Profile | None = None
        self.icons: list[tuple[QLabel, str, int]] = []
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
        images.loaded.connect(self._on_image)

    def refresh(self):
        self._render()
        run_task(lambda b: prof.fetch(b.api), self._loaded,
                 lambda e: self._render(error=str(e)))

    def _loaded(self, p: prof.Profile):
        self.data = p
        self._render()

    def _img(self, label: QLabel, url: str | None, size: int, rounded: bool = False):
        if not url:
            return
        self.icons.append((label, url, size if not rounded else -size))
        self._apply(label, url, size if not rounded else -size)

    def _apply(self, label: QLabel, url: str, size: int):
        pm = self.images.image(url)
        if pm:
            label.setPixmap(_round(pm, -size) if size < 0 else
                            pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _on_image(self, key: str):
        for label, url, size in list(self.icons):
            if key == f"url:{url}":
                try:
                    self._apply(label, url, size)
                except RuntimeError:
                    self.icons.remove((label, url, size))

    def _render(self, error: str | None = None):
        clear(self.lay)
        self.icons = []
        p = self.data
        head = QHBoxLayout()
        head.setSpacing(18)
        av = QLabel((p.username[:1].upper() if p else "?"))
        av.setFixedSize(AVATAR, AVATAR)
        av.setAlignment(Qt.AlignCenter)
        av.setStyleSheet(f"background: {theme.ACCENT}; color: white; border-radius: {AVATAR // 2}px;"
                         "font-size: 30pt; font-weight: 900;")
        if p:
            self._img(av, p.avatar, AVATAR, rounded=True)
        head.addWidget(av)
        names = QVBoxLayout()
        names.addStretch()
        n = QLabel(p.username if p else (self.window_.sidebar.user_name.text()))
        n.setStyleSheet("font-size: 24pt; font-weight: 900;")
        names.addWidget(n)
        sub = QLabel(_("En GOG desde {date}").format(date=p.since) if p and p.since else
                     (_("No se pudo cargar el perfil: {error}").format(error=error) if error else
                      _("Cargando perfil…")))
        sub.setObjectName("Muted")
        names.addWidget(sub)
        names.addStretch()
        head.addLayout(names, 1)
        full = QPushButton(_("Ver perfil completo en GOG"))
        full.setToolTip(_("Tu perfil público de gog.com con horas jugadas y estadísticas"))
        full.setEnabled(bool(p))
        full.clicked.connect(lambda: self.open_store.emit(f"https://www.gog.com/u/{p.username}"))
        head.addWidget(full, 0, Qt.AlignVCenter)
        self.lay.addLayout(head)

        stats = prof.achievement_stats(self.window_.state)
        entries = self.window_.entries
        grid = QGridLayout()
        grid.setSpacing(12)
        grid.addWidget(tile(str(len(entries)), _("juegos en tu biblioteca")), 0, 0)
        grid.addWidget(tile(str(sum(1 for e in entries.values() if e.installed)), _("instalados")), 0, 1)
        grid.addWidget(tile(str(stats.unlocked), n_("logros en {n} juego", "logros en {n} juegos", stats.games)
                            .format(n=stats.games)), 0, 2)
        grid.addWidget(tile(str(stats.perfect), _("juegos al 100 %")), 0, 3)
        self.lay.addLayout(grid)

        row = QHBoxLayout()
        row.setSpacing(16)
        row.addWidget(self._achievements_card(stats, entries), 1)
        row.addWidget(self._wishlist_card(p), 1)
        self.lay.addLayout(row)
        self.lay.addStretch()

    def _achievements_card(self, stats: prof.AchievementStats, entries) -> QFrame:
        c, l = card(_("Últimos logros"))
        if not stats.recent:
            m = QLabel(_("Aún no hay logros consultados. Se cargan al abrir la ficha de cada juego, o "
                         "todos de una vez con el botón de abajo."))
            m.setObjectName("Muted")
            m.setWordWrap(True)
            l.addWidget(m)
        for gid, a in stats.recent:
            r = QHBoxLayout()
            icon = QLabel()
            icon.setFixedSize(40, 40)
            self._img(icon, a.image_unlocked, 40)
            r.addWidget(icon)
            t = QVBoxLayout()
            t.setSpacing(0)
            name = QLabel(f"<b>{a.name}</b>")
            game = entries.get(gid)
            sub = QLabel(f"{game.title if game else gid} · "
                         f"{time.strftime(_('%d/%m/%Y'), time.localtime(a.unlocked_at))}")
            sub.setObjectName("Muted")
            t.addWidget(name)
            t.addWidget(sub)
            r.addLayout(t, 1)
            if game:
                go = QPushButton(_("Ficha"))
                go.setObjectName("Ghost")
                go.clicked.connect(lambda _=False, g=gid: self.open_game.emit(g))
                r.addWidget(go)
            l.addLayout(r)
        if stats.total:
            note = QLabel(n_("{unlocked} de {total} logros en el juego consultado",
                             "{unlocked} de {total} logros en los {n} juegos consultados", stats.games)
                          .format(unlocked=stats.unlocked, total=stats.total, n=stats.games))
            note.setObjectName("Muted")
            l.addWidget(note)
        self.scan_bar = QProgressBar()
        self.scan_bar.setTextVisible(False)
        self.scan_bar.hide()
        l.addWidget(self.scan_bar)
        scan = QPushButton(_("Consultar logros de toda la biblioteca"))
        scan.setToolTip(_("Pide a GOG los logros de cada juego (la primera vez tarda unos minutos)"))
        scan.clicked.connect(lambda: self._scan(scan))
        l.addWidget(scan, 0, Qt.AlignLeft)
        return c

    def _scan(self, btn: QPushButton):
        ids = list(self.window_.entries)
        btn.setEnabled(False)
        btn.setText(_("Consultando…"))
        self.scan_bar.show()
        self.scan_bar.setRange(0, len(ids))

        def work(b):
            prof.refresh_all_achievements(b.api, b.state, ids, lambda i, n: b.log(f"{i}/{n}"))

        def progress(msg: str):
            try:
                self.scan_bar.setValue(int(msg.split("/")[0]))
            except (RuntimeError, ValueError):
                pass

        run_task(work, lambda _r: self._render(), lambda e: self._render(error=str(e)), progress)

    def _wishlist_card(self, p: prof.Profile | None) -> QFrame:
        c, l = card(_("Lista de deseos") + (f" ({len(p.wishlist)})" if p else ""))
        if p and not p.wishlist:
            m = QLabel(_("Tu lista de deseos está vacía."))
            m.setObjectName("Muted")
            l.addWidget(m)
        for w in (p.wishlist[:12] if p else []):
            r = QHBoxLayout()
            name = QLabel(w.title)
            name.setStyleSheet("font-weight: 700;")
            name.setWordWrap(True)
            r.addWidget(name, 1)
            if w.discount:
                d = QLabel(f"-{w.discount}%")
                d.setStyleSheet(f"background: {theme.OK}; color: #0d1f12; border-radius: 6px;"
                                " padding: 2px 6px; font-weight: 800;")
                r.addWidget(d)
                old = QLabel(f"<s>{prof.price(w.base, w.currency)}</s>")
                old.setObjectName("Muted")
                r.addWidget(old)
            pr = QLabel(prof.price(w.final, w.currency))
            pr.setStyleSheet("font-weight: 700;")
            r.addWidget(pr)
            go = QPushButton(_("Ver"))
            go.setObjectName("Ghost")
            go.clicked.connect(lambda _=False, u=w.url: self.open_store.emit(u))
            r.addWidget(go)
            l.addLayout(r)
        if p and len(p.wishlist) > 12:
            more = QPushButton(_("Ver la lista completa ({n})").format(n=len(p.wishlist)))
            more.setObjectName("Ghost")
            more.clicked.connect(lambda: self.open_store.emit("/account/wishlist"))
            l.addWidget(more, 0, Qt.AlignLeft)
        return c


__all__ = ["ProfilePage"]
