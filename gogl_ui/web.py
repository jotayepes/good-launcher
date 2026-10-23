"""Navegador embebido para gog.com: un único perfil persistente.

El inicio de sesión, la tienda y el perfil comparten el mismo perfil de Qt
WebEngine, guardado en disco: así gog.com recuerda la sesión entre arranques y
la tienda sabe qué juegos tienes. Las ventanas emergentes (PayPal, 3-D Secure
del banco, el inicio de sesión de la tienda) se abren en un diálogo con el mismo
perfil. Los pagos ocurren siempre en las páginas de GOG o de su pasarela: aquí
no se lee ni se guarda nada de ellos.

Orden de destrucción: una página tiene que desaparecer antes que su perfil (si
no, Qt WebEngine avisa «Expect troubles» y puede cerrarse de golpe). Por eso el
perfil no tiene padre y, al salir, se borran primero todas las páginas."""

from __future__ import annotations

import re
import weakref

from PySide6.QtCore import QCoreApplication, QEvent, Qt, QUrl
from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout

from gogl import i18n, paths

try:
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView
    HAS_WEBENGINE = True
except ImportError:  # pragma: no cover - depende de la instalación
    HAS_WEBENGINE = False

_profile = None
_pages: "weakref.WeakSet" = weakref.WeakSet()


def profile():
    """El perfil compartido (se crea la primera vez)."""
    global _profile
    if _profile is None:
        _profile = QWebEngineProfile("gogl")
        _profile.setPersistentStoragePath(str(paths.data_dir() / "web"))
        _profile.setCachePath(str(paths.cache_dir() / "web"))
        _profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
        _profile.setHttpAcceptLanguage("es-ES,es;q=0.9,en;q=0.8" if i18n.current == "es" else
                                       f"{i18n.current},en;q=0.8")
        _profile.downloadRequested.connect(_on_download)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(shutdown)
    return _profile


def shutdown() -> None:
    global _profile
    for page in list(_pages):
        try:
            page.deleteLater()
        except RuntimeError:
            pass
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    if _profile is not None:
        _profile.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        _profile = None


def clear_session() -> None:
    """Al cerrar sesión en el lanzador: fuera cookies y caché de gog.com."""
    p = profile()
    p.cookieStore().deleteAllCookies()
    p.clearHttpCache()


def _from_gog(url: QUrl) -> bool:
    host = url.host().lower()
    return url.scheme() == "https" and (host == "gog.com" or host.endswith(".gog.com"))


def _on_download(item) -> None:
    # Lo que se descargue desde la web (p. ej. un instalador desde la cuenta) va a
    # la carpeta de extras e instaladores, en «Desde la web». Solo de GOG: una
    # página ajena (o un anuncio en una ventana emergente) no puede dejar archivos.
    page = item.page() if hasattr(item, "page") else None
    # La URL final puede ser de la CDN que contrate GOG: vale si la pidió una página de GOG.
    if not _from_gog(item.url()) and not (page is not None and _from_gog(page.url())):
        item.cancel()
        return
    from gogl.content.offline import downloads_root
    d = downloads_root() / "Desde la web"
    d.mkdir(parents=True, exist_ok=True)
    item.setDownloadDirectory(str(d))
    item.accept()


if HAS_WEBENGINE:
    class GogPage(QWebEnginePage):
        """Página con el perfil compartido: sin ruido de consola y con ventanas emergentes."""

        def __init__(self, parent=None):
            super().__init__(profile(), parent)
            _pages.add(self)
            self.popups: list[PopupDialog] = []

        def javaScriptConsoleMessage(self, *_args):
            pass

        def createWindow(self, _type):
            dlg = PopupDialog(self.view().window() if self.view() else None)
            self.popups.append(dlg)
            dlg.finished.connect(lambda _r, d=dlg: self.popups.remove(d) if d in self.popups else None)
            dlg.show()
            return dlg.view.page()

    class PopupDialog(QDialog):
        """Ventana emergente de la web (pasarela de pago, inicio de sesión…)."""

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setAttribute(Qt.WA_DeleteOnClose)
            self.resize(560, 760)
            lay = QVBoxLayout(self)
            lay.setContentsMargins(0, 0, 0, 0)
            self.view = QWebEngineView(self)
            self.view.setPage(GogPage(self.view))
            self.view.titleChanged.connect(self.setWindowTitle)
            self.view.page().windowCloseRequested.connect(self.close)
            lay.addWidget(self.view)

    def make_view(parent=None) -> "QWebEngineView":
        view = QWebEngineView(parent)
        view.setPage(GogPage(view))
        return view


def store_url(path: str = "") -> QUrl:
    """URL de gog.com en el idioma de la interfaz. La ruta puede traer ya un idioma
    (la biblioteca las guarda en inglés: «/en/game/…»), que se sustituye."""
    path = re.sub(r"^/[a-z]{2}(?=/|$)", "", path or "/games")
    return QUrl(f"https://www.gog.com/{i18n.web_language()}{path}")


__all__ = ["HAS_WEBENGINE", "clear_session", "make_view", "profile", "shutdown", "store_url"]
