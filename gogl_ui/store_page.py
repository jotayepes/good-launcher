"""Tienda de GOG dentro del lanzador.

Es gog.com en el navegador embebido (perfil persistente, ver web.py). La compra
se hace en el checkout de GOG; el lanzador solo:
- avisa cuando la página es de un juego que ya tienes, con acceso a su ficha;
- al salir del checkout, actualiza la biblioteca y enseña lo que haya entrado nuevo.
"""

from __future__ import annotations

import re

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton, QVBoxLayout,
                               QWidget)

from gogl.i18n import _

from . import theme, webpad
from .web import HAS_WEBENGINE, make_view, store_url

GAME_PATH = re.compile(r"^(?:/[a-z]{2})?/game/([^/?#]+)")
HOME = "/"


class StorePage(QWidget):
    open_game = Signal(str)          # game_id
    purchase_finished = Signal()     # se salió del checkout: refrescar biblioteca

    def __init__(self, owned_slug):
        """owned_slug(slug) -> (game_id, título) si el juego está en tu biblioteca."""
        super().__init__()
        self.owned_slug = owned_slug
        self.view = None
        self.in_checkout = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        bar = QFrame()
        bar.setObjectName("StoreBar")
        bar.setStyleSheet(f"#StoreBar {{ background: {theme.SURFACE}; border-bottom: 1px solid {theme.SURFACE_2}; }}")
        row = QHBoxLayout(bar)
        row.setContentsMargins(16, 8, 16, 8)
        row.setSpacing(6)
        self.back_btn = self._tool(row, "←", _("Atrás"), lambda: self.view.back())
        self.fwd_btn = self._tool(row, "→", _("Adelante"), lambda: self.view.forward())
        self._tool(row, "⟳", _("Recargar"), lambda: self.view.reload())
        self._tool(row, _("Inicio"), _("Portada de la tienda"), lambda: self.go(HOME))
        self.search = QLineEdit()
        self.search.setPlaceholderText(_("Buscar en la tienda de GOG…"))
        self.search.returnPressed.connect(self._search)
        row.addWidget(self.search, 1)
        self._tool(row, _("Ofertas"), _("Juegos rebajados"), lambda: self.go("/games?discounted=true"))
        self._tool(row, _("Lista de deseos"), _("Tu lista de deseos en GOG"), lambda: self.go("/account/wishlist"))
        self._tool(row, _("Abrir en el navegador"), _("Abrir esta página en tu navegador"),
                   lambda: QDesktopServices.openUrl(self.view.url()))
        lay.addWidget(bar)

        # Mientras carga: la primera vez Qt WebEngine tarda en arrancar y la página
        # se quedaba en blanco sin más.
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(3)
        self.progress.setStyleSheet(f"QProgressBar {{ border: none; background: {theme.SURFACE}; }}"
                                    f"QProgressBar::chunk {{ background: {theme.ACCENT}; }}")
        self.progress.hide()
        lay.addWidget(self.progress)

        self.banner = QFrame()
        self.banner.setObjectName("StoreBanner")
        self.banner.setStyleSheet(f"#StoreBanner {{ background: {theme.SURFACE_2}; }}")
        brow = QHBoxLayout(self.banner)
        brow.setContentsMargins(16, 8, 16, 8)
        self.banner_text = QLabel()
        brow.addWidget(self.banner_text, 1)
        self.banner_btn = QPushButton()
        self._banner_fn = lambda: None
        self.banner_btn.clicked.connect(lambda: self._banner_fn())
        brow.addWidget(self.banner_btn)
        close = QPushButton("✕")
        close.setObjectName("Ghost")
        close.clicked.connect(self.banner.hide)
        brow.addWidget(close)
        self.banner.hide()
        lay.addWidget(self.banner)

        self.body = QVBoxLayout()
        lay.addLayout(self.body, 1)
        if not HAS_WEBENGINE:
            msg = QLabel(_("Para ver la tienda hace falta Qt WebEngine. Ábrela en tu navegador:"))
            msg.setAlignment(Qt.AlignCenter)
            self.body.addWidget(msg)

    def _tool(self, row, text, tip, fn) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("Ghost")
        b.setToolTip(tip)
        b.clicked.connect(lambda: self.view and fn())
        row.addWidget(b)
        return b

    def ensure_view(self):
        """La vista web se crea la primera vez que se abre la tienda (arrancarla cuesta)."""
        if self.view is not None or not HAS_WEBENGINE:
            return
        self.view = make_view(self)
        self.view.urlChanged.connect(self._on_url)
        self.view.loadStarted.connect(lambda: (self.progress.setValue(0), self.progress.show()))
        self.view.loadProgress.connect(self.progress.setValue)
        self.view.loadFinished.connect(lambda _ok: self.progress.hide())
        self.body.addWidget(self.view, 1)
        self.go(HOME)

    def go(self, path_or_url: str):
        self.ensure_view()
        if self.view is None:
            QDesktopServices.openUrl(QUrl(path_or_url if "://" in path_or_url else store_url(path_or_url)))
            return
        self.view.load(QUrl(path_or_url) if "://" in path_or_url else store_url(path_or_url))

    def _search(self):
        q = self.search.text().strip()
        if q:
            self.go("/games?query=" + QUrl.toPercentEncoding(q).data().decode())

    def _on_url(self, url: QUrl):
        self.back_btn.setEnabled(self.view.history().canGoBack())
        self.fwd_btn.setEnabled(self.view.history().canGoForward())
        path = url.path()
        checkout = "checkout" in path
        if self.in_checkout and not checkout:
            self.purchase_finished.emit()
        self.in_checkout = checkout
        m = GAME_PATH.match(path) if url.host().endswith("gog.com") else None
        owned = self.owned_slug(m.group(1)) if m else None
        if owned:
            game_id, title = owned
            self.show_banner("✓ " + _("Ya tienes {title} en tu biblioteca.").format(title=f"<b>{title}</b>"),
                             _("Abrir su ficha"),
                             lambda: self.open_game.emit(game_id))
        elif self.banner.property("kind") == "owned":
            self.banner.hide()

    def show_banner(self, text: str, action: str, fn, kind: str = "owned"):
        self.banner_text.setText(text)
        self.banner.setProperty("kind", kind)
        self._banner_fn = fn
        self.banner_btn.setText(action)
        self.banner.show()

    def leaving(self):
        """La tienda deja de verse: si se quedó en mitad del checkout, refresca igualmente."""
        if self.view is not None:
            webpad.clear(self.view)
        if self.in_checkout:
            self.in_checkout = False
            self.purchase_finished.emit()


__all__ = ["StorePage"]
