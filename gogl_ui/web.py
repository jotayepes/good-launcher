"""Navegador embebido para gog.com: un único perfil persistente.

El inicio de sesión, la tienda y el perfil comparten el mismo perfil de Qt
WebEngine, guardado en disco: así gog.com recuerda la sesión entre arranques y
la tienda sabe qué juegos tienes. Las ventanas emergentes (PayPal, 3-D Secure
del banco, el inicio de sesión de la tienda) se abren en un diálogo con el mismo
perfil. Los pagos ocurren siempre en las páginas de GOG o de su pasarela: aquí
no se lee ni se guarda nada de ellos.

Orden de destrucción: una página tiene que desaparecer antes que su perfil (si
no, Qt WebEngine avisa «Expect troubles» y puede cerrarse de golpe). Por eso el
perfil no tiene padre y, al salir, se borran primero todas las páginas.

Dentro de Steam (sobre todo en el modo juego), ver prepare_environment(). Lo que
tarda la web en arrancar, cargar y cerrarse queda en web.log (carpeta de estado):
en la Deck no hay otra forma de verlo."""

from __future__ import annotations

import os
import re
import shlex
import signal
import threading
import time
import weakref

from PySide6.QtCore import QCoreApplication, QEvent, QLibraryInfo, Qt, QTimer, QUrl
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
_started = time.monotonic()
_log_ready = False
_heartbeat = None

CLOSE_LIMIT = 8  # s: si Qt WebEngine tarda más en cerrarse, se corta (ver shutdown)


OVERLAY_LIB = "gameoverlayrenderer"  # el overlay de Steam, que Steam mete en LD_PRELOAD

# Envoltorio de QtWebEngineProcess: quita el overlay de Steam de LD_PRELOAD y
# arranca el de verdad. Lo heredan todos los procesos de Chromium (zigoto,
# renderizadores, GPU…), que nacen de él.
_WRAPPER = """#!/bin/sh
# Generado por gogl (gogl_ui/web.py) en cada arranque: no editar.
keep=
set -f
IFS=': '
for lib in ${{LD_PRELOAD:-}}; do
    case "$lib" in
        *{overlay}*) ;;
        *) keep="${{keep:+$keep:}}$lib" ;;
    esac
done
if [ -n "$keep" ]; then export LD_PRELOAD="$keep"; else unset LD_PRELOAD; fi
type=browser
for arg in "$@"; do
    case "$arg" in --type=*) type="${{arg#--type=}}" ;; esac
done
printf '        -  %s arranca QtWebEngineProcess (%s)\n' "$(date +%H:%M:%S.%N | cut -c1-12)" "$type" \
    >> {log} 2>/dev/null
exec {real} "$@"
"""


def store_available() -> bool:
    """La tienda y los enlaces a gog.com (también en el modo juego: prepare_environment)."""
    return HAS_WEBENGINE


def prepare_environment() -> None:
    """Antes de crear QApplication. En el modo juego, abrir una página web colgaba la
    Deck. Dos causas, con su remedio:

    - La primera vista web hace que Qt destruya la ventana nativa y la cree de nuevo
      en OpenGL, y gamescope se queda sin la ventana del «juego». En gamescope la vista
      web se dibuja por software (Qt Quick y Chromium sin GPU): la ventana sigue siendo
      la misma de principio a fin. Para las páginas de GOG no hace falta más.
    - Steam mete su overlay (gameoverlayrenderer.so) con LD_PRELOAD en todo lo que
      arranca el «juego», también en los procesos de Chromium, que tienen su propio
      sandbox y GPU. Esos procesos se lanzan sin él (un envoltorio en
      QTWEBENGINEPROCESS_PATH); la ventana del lanzador y los juegos lo conservan.

    Además, en el modo juego:
    - Sin sondear la GPU (QSG_RHI_BACKEND=null). Aunque dibuje por software, al crear
      la primera vista Qt WebEngine abre en la ventana del lanzador un contexto OpenGL
      y un dispositivo Vulkan solo para saber el fabricante de la gráfica. Con las capas
      de Vulkan de Steam y gamescope, la ventana se quedaba minutos sin redibujarse
      (ni la barra de carga se veía). Qt Quick no se usa para nada más que la web.
    - La web no se expone a la accesibilidad (AT-SPI): Qt WebEngine traduce entonces
      cada nodo de la página a objetos de D-Bus, algo que con una página como la tienda
      va lentísimo (QTBUG-59922) y aquí no se usa.
    - Sin el sandbox de Chromium (QTWEBENGINE_DISABLE_SANDBOX), como hace Heroic al
      abrirse desde Steam (--no-sandbox; en Flatpak lo sustituye zypak). Crear el perfil
      espera a que arranquen los zigotos de Chromium, que montan ese sandbox, y en el
      modo juego la espera llegaba a minutos (web.log se quedaba en «creando el perfil
      web»). Las páginas que se abren son las de GOG y su pasarela de pago."""
    if not HAS_WEBENGINE:
        return
    from .navigation import in_gamescope
    if in_gamescope():
        os.environ.setdefault("QT_QUICK_BACKEND", "software")
        os.environ.setdefault("QSG_RHI_BACKEND", "null")
        os.environ.setdefault("QTWEBENGINE_ENABLE_LINUX_ACCESSIBILITY", "0")
        os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
        flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
        flags += [f for f in ("--disable-gpu", "--disable-gpu-compositing") if f not in flags]
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags)
    if OVERLAY_LIB in os.environ.get("LD_PRELOAD", "") and "QTWEBENGINEPROCESS_PATH" not in os.environ:
        wrapper = _process_wrapper()
        if wrapper:
            os.environ["QTWEBENGINEPROCESS_PATH"] = wrapper


def _process_wrapper() -> str | None:
    real = os.path.join(QLibraryInfo.path(QLibraryInfo.LibraryExecutablesPath), "QtWebEngineProcess")
    if not os.access(real, os.X_OK):
        return None
    # Se reescribe en cada arranque: la AppImage se monta cada vez en otra ruta.
    wrapper = paths.cache_dir() / "QtWebEngineProcess"
    tmp = wrapper.with_name(wrapper.name + f".{os.getpid()}")
    try:
        tmp.write_text(_WRAPPER.format(overlay=OVERLAY_LIB, real=shlex.quote(real),
                                       log=shlex.quote(str(paths.state_dir() / "web.log"))))
        tmp.chmod(0o755)
        tmp.replace(wrapper)
    except OSError:
        return None
    return str(wrapper)


def trace(msg: str) -> None:
    """Una línea en web.log con los segundos desde el arranque. Nunca falla."""
    global _log_ready
    try:
        log = paths.state_dir() / "web.log"
        if not _log_ready:  # uno por arranque; el anterior se guarda como web.log.old
            _log_ready = True
            if log.exists():
                log.replace(log.with_suffix(".log.old"))
            keys = ("XDG_CURRENT_DESKTOP", "QT_QUICK_BACKEND", "QSG_RHI_BACKEND", "QTWEBENGINE_CHROMIUM_FLAGS",
                    "QTWEBENGINEPROCESS_PATH", "QTWEBENGINE_ENABLE_LINUX_ACCESSIBILITY",
                    "QTWEBENGINE_DISABLE_SANDBOX", "QT_QPA_PLATFORM", "WAYLAND_DISPLAY", "DISPLAY",
                    "LD_LIBRARY_PATH")
            env = {k: os.environ[k] for k in keys if k in os.environ}
            with log.open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} pid {os.getpid()} {env}\n")
        with log.open("a", encoding="utf-8") as f:
            now = time.time()
            wall = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now % 1 * 1000):03d}"
            f.write(f"{time.monotonic() - _started:9.2f}s {wall} {msg}\n")
    except OSError:
        pass


def _beat() -> None:
    """Si el hilo de la interfaz se queda parado (algo de la web lo bloquea), se apunta."""
    global _last_beat
    now = time.monotonic()
    if now - _last_beat > 1.5:
        trace(f"la interfaz estuvo bloqueada {now - _last_beat:.1f} s")
    _last_beat = now


_last_beat = 0.0


def profile():
    """El perfil compartido (se crea la primera vez)."""
    global _profile, _heartbeat, _last_beat
    if _profile is None:
        trace("creando el perfil web")
        # Crear el perfil arranca Chromium y espera a sus procesos sin soltar Python:
        # si Steam pide cerrar mientras tanto, que el proceso termine en el acto (si no,
        # el aviso no se atiende hasta que Chromium acaba de arrancar).
        handlers = {sig: signal.signal(sig, signal.SIG_DFL) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            _profile = QWebEngineProfile("gogl")
        finally:
            for sig, h in handlers.items():
                signal.signal(sig, h if h is not None else signal.SIG_DFL)
        _profile.setPersistentStoragePath(str(paths.data_dir() / "web"))
        _profile.setCachePath(str(paths.cache_dir() / "web"))
        _profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
        _profile.setHttpAcceptLanguage("es-ES,es;q=0.9,en;q=0.8" if i18n.current == "es" else
                                       f"{i18n.current},en;q=0.8")
        _profile.downloadRequested.connect(_on_download)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(shutdown)
            _last_beat = time.monotonic()
            _heartbeat = QTimer(app)
            _heartbeat.setInterval(250)
            _heartbeat.timeout.connect(_beat)
            _heartbeat.start()
        trace("perfil web listo")
    return _profile


def shutdown() -> None:
    """Al salir: páginas y perfil, en ese orden. Si se usó la web, el cierre tiene un
    límite: en la Deck llegaba a tardar minutos y Steam seguía dando el «juego» por
    abierto. Pasado CLOSE_LIMIT se cierran los procesos de Chromium y se sale."""
    global _profile
    if _heartbeat is not None:
        _heartbeat.stop()
    if _profile is not None:
        trace("cerrando la web")
        _arm_close_limit()
    for page in list(_pages):
        try:
            page.deleteLater()
        except RuntimeError:
            pass
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    if _profile is not None:
        trace("páginas cerradas")
        _profile.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        _profile = None
        trace("perfil cerrado")


def _arm_close_limit() -> None:
    import atexit
    atexit.register(lambda: trace("fin del proceso"))
    _close_timer(CLOSE_LIMIT)
    # Por si el hilo del temporizador no llega a correr (Qt bloqueado sin soltar Python):
    # la alarma la atiende el núcleo, que termina el proceso. No si hay tareas en marcha
    # (p. ej. sincronizando partidas): esas se esperan.
    if not _busy() and signal.getsignal(signal.SIGALRM) in (signal.SIG_DFL, None):
        signal.alarm(CLOSE_LIMIT * 2)


def _close_timer(seconds: float) -> None:
    t = threading.Timer(seconds, _force_exit)
    t.daemon = True
    t.start()


def _busy() -> bool:
    from PySide6.QtCore import QThreadPool
    return QThreadPool.globalInstance().activeThreadCount() > 0


def _force_exit() -> None:
    if _busy():
        signal.alarm(0)
        _close_timer(2)
        return
    trace(f"el cierre pasa de {CLOSE_LIMIT} s: se fuerza")
    for pid in _web_processes():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    os._exit(0)


def _web_processes() -> list[int]:
    """Los procesos de Chromium que cuelgan de este (zigoto, renderizadores…)."""
    me, out = os.getpid(), []
    children = {me}
    try:
        stats = []
        for d in os.listdir("/proc"):
            if d.isdigit():
                try:
                    with open(f"/proc/{d}/stat", "rb") as f:
                        data = f.read().decode(errors="replace")
                except OSError:
                    continue
                ppid = int(data.rsplit(")", 1)[1].split()[1])
                stats.append((int(d), ppid, data[data.find("(") + 1:data.rfind(")")]))
    except OSError:
        return out
    grew = True
    while grew:  # descendientes de cualquier nivel
        grew = False
        for pid, ppid, _name in stats:
            if ppid in children and pid not in children:
                children.add(pid)
                grew = True
    return [pid for pid, _ppid, name in stats if pid in children and pid != me and "QtWebEngine" in name]


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
            from .navigation import in_gamescope
            if in_gamescope():
                from .webpad import install_touch
                install_touch(self)
            self.loadStarted.connect(lambda: trace(f"cargando {self.requestedUrl().toString()}"))
            self.loadFinished.connect(lambda ok: trace(f"cargada ({'bien' if ok else 'con error'}) "
                                                       f"{self.url().toString()}"))
            self.renderProcessTerminated.connect(
                lambda status, code: trace(f"el proceso de la página terminó: {status} (código {code})"))

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
        trace("creando una vista web")
        view = QWebEngineView(parent)
        view.setPage(GogPage(view))
        trace("vista web creada")
        from .navigation import in_gamescope
        if in_gamescope():
            view.loadFinished.connect(lambda _ok, v=weakref.ref(view): _reshow_once(v()))
        return view

    def _reshow_once(view) -> None:
        """En gamescope, la primera página podía quedarse sin verse hasta ir a otra
        sección y volver. Eso mismo (ocultar y mostrar la vista) se hace solo una vez,
        al terminar la primera carga."""
        if view is None or getattr(view, "_reshown", False):
            return
        view._reshown = True
        if view.isVisible():
            trace("volviendo a mostrar la vista web")
            focused = view.hasFocus() or view.isAncestorOf(QApplication.focusWidget())
            view.hide()
            view.show()
            if focused:
                view.setFocus()  # al ocultarla, Qt pasa el foco a otro control


def store_url(path: str = "") -> QUrl:
    """URL de gog.com en el idioma de la interfaz. La ruta puede traer ya un idioma
    (la biblioteca las guarda en inglés: «/en/game/…»), que se sustituye."""
    path = re.sub(r"^/[a-z]{2}(?=/|$)", "", path or "/games")
    return QUrl(f"https://www.gog.com/{i18n.web_language()}{path}")


__all__ = ["HAS_WEBENGINE", "clear_session", "make_view", "prepare_environment", "profile", "shutdown",
           "store_available", "store_url", "trace"]
