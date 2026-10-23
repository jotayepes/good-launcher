"""Tarjeta «Logros» de la ficha del juego: progreso, conseguidos y pendientes."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QHBoxLayout, QLabel, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget)

from gogl import achievements
from gogl.achievements import Achievement
from gogl.i18n import _

from . import theme
from .images import ImageStore
from .workers import run_task

ICON = 48
PAGE = 8


def _gray(pm: QPixmap) -> QPixmap:
    return QPixmap.fromImage(pm.toImage().convertToFormat(QImage.Format_Grayscale8))


class AchievementRow(QWidget):
    def __init__(self, a: Achievement, images: ImageStore, reveal: bool):
        super().__init__()
        self.a, self.images = a, images
        hidden = not a.unlocked and not a.visible and not reveal
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 4, 0, 4)
        row.setSpacing(12)
        self.icon = QLabel()
        self.icon.setFixedSize(ICON, ICON)
        self.icon.setStyleSheet(f"background: {theme.SURFACE_2}; border-radius: 8px;")
        self.icon.setAlignment(Qt.AlignCenter)
        row.addWidget(self.icon)
        text = QVBoxLayout()
        text.setSpacing(1)
        name = QLabel(_("Logro oculto") if hidden else a.name)
        name.setStyleSheet("font-weight: 700;" + ("" if a.unlocked else f"color: {theme.MUTED};"))
        text.addWidget(name)
        desc = QLabel(_("Sigue jugando para descubrirlo.") if hidden else a.description)
        desc.setObjectName("Muted")
        desc.setWordWrap(True)
        text.addWidget(desc)
        row.addLayout(text, 1)
        side = QVBoxLayout()
        side.setSpacing(1)
        if a.unlocked:
            when = QLabel(time.strftime(_("%d/%m/%Y"), time.localtime(a.unlocked_at)))
            when.setStyleSheet(f"color: {theme.OK}; font-weight: 700;")
            when.setAlignment(Qt.AlignRight)
            side.addWidget(when)
        if a.rarity:
            r = QLabel(_("{pct} de jugadores").format(pct=f"{a.rarity:.1f}%"))
            r.setObjectName("Muted")
            r.setAlignment(Qt.AlignRight)
            r.setToolTip(a.rarity_label)
            side.addWidget(r)
        row.addLayout(side)
        self.hidden = hidden
        self.refresh_icon()

    def refresh_icon(self, key: str | None = None):
        url = self.a.image if not self.hidden else ""
        if key is not None and key != f"url:{url}":
            return
        pm = self.images.image(url) if url else None
        if pm:
            pm = pm.scaled(ICON, ICON, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.icon.setPixmap(pm if self.a.unlocked else _gray(pm))
        else:
            self.icon.setText("🏆" if self.a.unlocked else "🔒")


class AchievementsPanel(QWidget):
    """Contenido de la tarjeta (el título lo pone la ficha)."""

    def __init__(self, images: ImageStore, state):
        super().__init__()
        self.images, self.state = images, state
        self.game_id: str | None = None
        self.items: list[Achievement] = []
        self.compat: str = ""
        self.tab = "unlocked"
        self.expanded = False
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(8)
        self.rows: list[AchievementRow] = []
        images.loaded.connect(self._on_image)

    def show_game(self, game_id: str, compat: str):
        """compat: texto sobre si se consiguen jugando con gogl ("" = no instalado)."""
        if game_id != self.game_id:
            self.items, self.expanded = [], False
        self.game_id, self.compat = game_id, compat
        cached = achievements.cached(self.state, game_id)
        if cached is not None:
            self.items = cached
            self._render()
        else:
            self._message(_("Cargando logros…"))
        run_task(lambda b: achievements.fetch(b.api, b.auth, b.state, game_id),
                 lambda items: self._loaded(game_id, items),
                 lambda e: self._failed(game_id, e))

    def _loaded(self, game_id: str, items: list[Achievement]):
        if game_id != self.game_id:
            return
        self.items = items
        self._render()

    def _failed(self, game_id: str, e: Exception):
        if game_id == self.game_id and not self.items:
            self._message(_("No se pudieron cargar los logros: {error}").format(error=e))

    def _clear(self):
        from .game_page import clear
        clear(self.lay)
        self.rows = []

    def _message(self, text: str):
        self._clear()
        l = QLabel(text)
        l.setObjectName("Muted")
        l.setWordWrap(True)
        self.lay.addWidget(l)

    def _render(self):
        self._clear()
        if not self.items:
            self._message(_("Este juego no tiene logros de GOG."))
            return
        got = [a for a in self.items if a.unlocked]
        missing = [a for a in self.items if not a.unlocked]
        head = QHBoxLayout()
        count = QLabel(_("{got} de {total}").format(got=f"<b style='font-size:15pt'>{len(got)}</b>",
                                                    total=len(self.items)))
        head.addWidget(count)
        bar = QProgressBar()
        bar.setTextVisible(False)
        bar.setRange(0, len(self.items))
        bar.setValue(len(got))
        bar.setFixedHeight(8)
        head.addWidget(bar, 1)
        pct = QLabel(f"{100 * len(got) // len(self.items)}%")
        pct.setStyleSheet("font-weight: 700;")
        head.addWidget(pct)
        self.lay.addLayout(head)
        if self.compat:
            c = QLabel(self.compat)
            c.setObjectName("Muted")
            c.setWordWrap(True)
            self.lay.addWidget(c)

        tabs = QHBoxLayout()
        group = QButtonGroup(self)
        for key, label in (("unlocked", _("Conseguidos ({n})").format(n=len(got))),
                           ("locked", _("Pendientes ({n})").format(n=len(missing)))):
            b = QPushButton(label)
            b.setObjectName("Chip")
            b.setCheckable(True)
            b.setChecked(self.tab == key)
            b.clicked.connect(lambda _=False, k=key: self._set_tab(k))
            group.addButton(b)
            tabs.addWidget(b)
        tabs.addStretch()
        hidden_count = sum(1 for a in missing if not a.visible)
        if self.tab == "locked" and hidden_count:
            self.reveal = getattr(self, "reveal", False)
            chk = QCheckBox(_("Mostrar ocultos ({n})").format(n=hidden_count))
            chk.setChecked(self.reveal)
            chk.toggled.connect(self._set_reveal)
            tabs.addWidget(chk)
        self.lay.addLayout(tabs)

        if self.tab == "unlocked":
            shown = sorted(got, key=lambda a: -(a.unlocked_at or 0))
            empty = _("Todavía no has conseguido ninguno.")
        else:
            # Primero los más comunes: suelen ser los siguientes que caerán.
            shown = sorted(missing, key=lambda a: (not a.visible, -a.rarity))
            empty = _("¡Los tienes todos!")
        if not shown:
            self._message_inline(empty)
            return
        limit = len(shown) if self.expanded else PAGE
        for a in shown[:limit]:
            r = AchievementRow(a, self.images, getattr(self, "reveal", False))
            self.rows.append(r)
            self.lay.addWidget(r)
        if len(shown) > limit:
            more = QPushButton(_("Ver todos ({n})").format(n=len(shown)))
            more.setObjectName("Ghost")
            more.clicked.connect(self._expand)
            self.lay.addWidget(more, 0, Qt.AlignLeft)

    def _message_inline(self, text: str):
        l = QLabel(text)
        l.setObjectName("Muted")
        self.lay.addWidget(l)

    def _set_tab(self, key: str):
        self.tab, self.expanded = key, False
        self._render()

    def _set_reveal(self, on: bool):
        self.reveal = on
        self._render()

    def _expand(self):
        self.expanded = True
        self._render()

    def _on_image(self, key: str):
        for r in self.rows:
            try:
                r.refresh_icon(key)
            except RuntimeError:
                pass


__all__ = ["AchievementsPanel"]
