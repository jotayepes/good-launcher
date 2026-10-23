"""Biblioteca: cuadrícula de carátulas con búsqueda y filtros."""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import (QAbstractListModel, QModelIndex, QRect, QRectF, QSize,
                            QSortFilterProxyModel, Qt, Signal)
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QButtonGroup, QHBoxLayout, QLabel, QLineEdit, QListView,
                               QPushButton, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget)

from gogl.i18n import _, n_
from gogl.state import Game

from . import navigation, theme
from .images import ImageStore

CARD_W, COVER_H, TEXT_H = 164, 219, 46


@dataclass
class Entry:
    game_id: str
    title: str
    platforms: list[str]            # "windows" / "linux" / "mac"
    product: dict = field(default_factory=dict)
    game: Game | None = None        # instalación registrada
    sync: str | None = None         # ok | pending | conflict | None

    @property
    def installed(self) -> bool:
        return self.game is not None

    @property
    def fallback_image(self) -> str | None:
        img = self.product.get("image")
        return f"https:{img}_196.jpg" if img and img.startswith("//") else None


def build_entries(products: list[dict], games: list[Game], statuses: dict[str, dict]) -> list[Entry]:
    by_id: dict[str, Entry] = {}
    for p in products:
        w = p.get("worksOn") or {}
        plats = [k for k, v in (("windows", w.get("Windows")), ("linux", w.get("Linux")),
                                ("mac", w.get("Mac"))) if v]
        by_id[str(p["id"])] = Entry(str(p["id"]), p.get("title", ""), plats, p)
    for g in games:
        e = by_id.get(g.game_id) or by_id.setdefault(g.game_id, Entry(g.game_id, g.title, [g.platform]))
        e.game = g
        st = statuses.get(g.game_id)
        if st:
            e.sync = ("conflict" if st.get("last_error") == "conflicto" else "pending") if st["pending"] else "ok"
    return sorted(by_id.values(), key=lambda e: e.title.casefold())


class LibraryModel(QAbstractListModel):
    EntryRole = Qt.UserRole + 1

    def __init__(self):
        super().__init__()
        self.entries: list[Entry] = []

    def set_entries(self, entries: list[Entry]):
        self.beginResetModel()
        self.entries = entries
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.entries)

    def data(self, index, role=Qt.DisplayRole):
        e = self.entries[index.row()]
        if role == Qt.DisplayRole:
            return e.title
        if role == self.EntryRole:
            return e
        if role == Qt.ToolTipRole:
            return e.title
        return None

    def refresh_id(self, game_id: str):
        for i, e in enumerate(self.entries):
            if e.game_id == game_id:
                idx = self.index(i)
                self.dataChanged.emit(idx, idx)


FILTERS = {
    "all": lambda e: True,
    "installed": lambda e: e.installed,
    "windows": lambda e: "windows" in e.platforms,
    "linux": lambda e: "linux" in e.platforms,
    "pending": lambda e: e.sync in ("pending", "conflict"),
}


class LibraryProxy(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.text = ""
        self.mode = "all"

    def set_text(self, t: str):
        self.text = t.casefold().strip()
        self.invalidateRowsFilter()

    def set_mode(self, m: str):
        self.mode = m
        self.invalidateRowsFilter()

    def filterAcceptsRow(self, row, parent):
        e = self.sourceModel().entries[row]
        return FILTERS[self.mode](e) and (not self.text or self.text in e.title.casefold())


class CoverDelegate(QStyledItemDelegate):
    def __init__(self, images: ImageStore, downloads=None, parent=None):
        super().__init__(parent)
        self.images = images
        self.downloads = downloads

    def sizeHint(self, option, index):
        return QSize(CARD_W, COVER_H + TEXT_H)

    def paint(self, p: QPainter, option, index):
        e: Entry = index.data(LibraryModel.EntryRole)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        r = option.rect
        hover = bool(option.state & QStyle.State_MouseOver)
        selected = bool(option.state & QStyle.State_Selected)
        focused = selected and bool(option.state & QStyle.State_HasFocus) and navigation.gamepad_active()
        cover = QRectF(r.x() + 2, r.y() + 2, CARD_W - 4, COVER_H - 4)

        path = QPainterPath()
        path.addRoundedRect(cover, 8, 8)
        p.setClipPath(path)
        pm = self.images.cover(e.game_id, e.fallback_image)
        if pm:
            # «cover»: escalar para llenar y recortar centrado.
            s = pm.size().scaled(cover.size().toSize(), Qt.KeepAspectRatioByExpanding)
            x = cover.x() + (cover.width() - s.width()) / 2
            y = cover.y() + (cover.height() - s.height()) / 2
            p.drawPixmap(QRect(int(x), int(y), s.width(), s.height()), pm)
        else:
            g = QLinearGradient(cover.topLeft(), cover.bottomRight())
            g.setColorAt(0, QColor(theme.SURFACE_2))
            g.setColorAt(1, QColor(theme.SURFACE))
            p.fillRect(cover, g)
            p.setPen(QColor(theme.MUTED))
            f = QFont(option.font)
            f.setBold(True)
            p.setFont(f)
            p.drawText(cover.adjusted(12, 12, -12, -12), Qt.AlignCenter | Qt.TextWordWrap, e.title)
        if not e.installed and not hover and not focused:
            p.fillRect(cover, QColor(0, 0, 0, 70))
        p.setClipping(False)

        if focused:  # elemento actual con el foco en la cuadrícula (teclado o mando)
            p.setPen(QPen(QColor("white"), 3))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(cover.adjusted(-1, -1, 1, 1), 9, 9)
        elif hover or selected:
            p.setPen(QPen(QColor(theme.ACCENT), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(cover.adjusted(-1, -1, 1, 1), 9, 9)

        self._badges(p, cover, e)

        # Título y subtítulo
        tr = QRect(r.x() + 4, r.y() + COVER_H + 4, CARD_W - 8, 18)
        f = QFont(option.font)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(theme.TEXT))
        p.drawText(tr, Qt.AlignLeft | Qt.AlignVCenter,
                   p.fontMetrics().elidedText(e.title, Qt.ElideRight, tr.width()))
        f.setBold(False)
        f.setPointSizeF(f.pointSizeF() * 0.85)
        p.setFont(f)
        p.setPen(QColor(theme.MUTED))
        sub = _("Instalado") if e.installed else " · ".join(x.capitalize() for x in e.platforms if x != "mac")
        p.drawText(tr.translated(0, 18), Qt.AlignLeft | Qt.AlignVCenter, sub)
        p.restore()

    def _badges(self, p: QPainter, cover: QRectF, e: Entry):
        job = self.downloads.active(e.game_id) if self.downloads else None
        if job:
            label, color = (_("En cola") if job.status == "queued" else
                            _("Descargando {pct}").format(pct=f"{job.progress.fraction * 100:.0f}%"),
                            theme.ACCENT)
        elif not e.installed:
            return
        else:
            label, color = {"pending": (_("Sync pendiente"), theme.WARN),
                            "conflict": (_("Conflicto"), theme.DANGER)}.get(e.sync, (_("Instalado"), theme.OK))
        f = p.font()
        f.setBold(True)
        f.setPointSizeF(8)
        p.setFont(f)
        w = p.fontMetrics().horizontalAdvance(label) + 22
        r = QRectF(cover.left() + 8, cover.top() + 8, w, 20)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(12, 11, 15, 215)))
        p.drawRoundedRect(r, 10, 10)
        p.setBrush(QBrush(QColor(color)))
        p.drawEllipse(QRectF(r.left() + 8, r.center().y() - 3, 6, 6))
        p.setPen(QColor(theme.TEXT))
        p.drawText(r.adjusted(18, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, label)


class LibraryPage(QWidget):
    open_game = Signal(object)  # Entry
    refresh_requested = Signal()

    def __init__(self, images: ImageStore, downloads=None):
        super().__init__()
        self.images = images
        self.model = LibraryModel()
        self.proxy = LibraryProxy()
        self.proxy.setSourceModel(self.model)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 20, 0)
        root.setSpacing(14)

        head = QHBoxLayout()
        self.title = QLabel(_("Biblioteca"))
        self.title.setStyleSheet("font-size: 20pt; font-weight: 800;")
        self.count = QLabel()
        self.count.setObjectName("Muted")
        head.addWidget(self.title)
        head.addWidget(self.count, 0, Qt.AlignBottom)
        head.addStretch()
        self.search = QLineEdit()
        self.search.setPlaceholderText(_("Buscar en tu biblioteca…"))
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(300)
        self.search.textChanged.connect(self._on_search)
        head.addWidget(self.search)
        refresh = QPushButton("⟳")
        refresh.setObjectName("Ghost")
        refresh.setToolTip(_("Actualizar biblioteca desde GOG"))
        refresh.clicked.connect(self.refresh_requested)
        head.addWidget(refresh)
        root.addLayout(head)

        chips = QHBoxLayout()
        chips.setSpacing(6)
        self.chip_group = QButtonGroup(self)
        self.chips: dict[str, QPushButton] = {}
        for key, label in (("all", _("Todos")), ("installed", _("Instalados")), ("windows", "Windows"),
                           ("linux", "Linux"), ("pending", _("Sincronización pendiente"))):
            b = QPushButton(label)
            b.setObjectName("Chip")
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, k=key: self.set_mode(k))
            self.chip_group.addButton(b)
            self.chips[key] = b
            chips.addWidget(b)
        chips.addStretch()
        root.addLayout(chips)
        self.chips["all"].setChecked(True)

        self.view = QListView()
        self.view.setViewMode(QListView.IconMode)
        self.view.setResizeMode(QListView.Adjust)
        self.view.setMovement(QListView.Static)
        self.view.setUniformItemSizes(True)
        self.view.setSpacing(10)
        self.view.setMouseTracking(True)
        self.view.setSelectionMode(QListView.SingleSelection)
        self.view.setVerticalScrollMode(QListView.ScrollPerPixel)
        self.view.verticalScrollBar().setSingleStep(24)
        self.view.setModel(self.proxy)
        self.view.setItemDelegate(CoverDelegate(images, downloads, self.view))
        self.view.activated.connect(self._activate)
        self.view.clicked.connect(self._activate)
        root.addWidget(self.view, 1)

        self.empty = QLabel()
        self.empty.setObjectName("Muted")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.hide()
        root.addWidget(self.empty, 1)

        images.loaded.connect(lambda _k: self.view.viewport().update())
        self.proxy.rowsInserted.connect(self._update_count)
        self.proxy.modelReset.connect(self._update_count)
        self.proxy.layoutChanged.connect(self._update_count)

    def set_entries(self, entries: list[Entry]):
        cur = self.view.currentIndex().data(LibraryModel.EntryRole)
        self.model.set_entries(entries)
        self._update_count()
        if cur is not None:  # conservar el elemento actual (el foco del mando)
            self.select(cur.game_id)

    def select(self, game_id: str):
        for row in range(self.proxy.rowCount()):
            idx = self.proxy.index(row, 0)
            if idx.data(LibraryModel.EntryRole).game_id == game_id:
                self.view.setCurrentIndex(idx)
                return

    def set_mode(self, mode: str):
        self.chips[mode].setChecked(True)
        self.proxy.set_mode(mode)
        self._update_count()

    def _on_search(self, t: str):
        self.proxy.set_text(t)
        self._update_count()

    def _update_count(self, *_args):
        n, total = self.proxy.rowCount(), self.model.rowCount()
        self.count.setText("  " + (n_("{n} de {total} juego", "{n} de {total} juegos", total) if n != total else
                                   n_("{total} juego", "{total} juegos", total)).format(n=n, total=total))
        empty = n == 0
        self.view.setVisible(not empty)
        self.empty.setVisible(empty)
        if empty:
            self.empty.setText(_("Aún no hay juegos. Pulsa ⟳ para cargar tu biblioteca de GOG.")
                               if total == 0 else _("Ningún juego coincide con el filtro."))

    def _activate(self, idx):
        self.open_game.emit(idx.data(LibraryModel.EntryRole))
