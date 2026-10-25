"""Punto de entrada de la interfaz gráfica."""

import os
import sys

from PySide6.QtWidgets import QApplication

from gogl import APP_TITLE, selfcmd, services, steam
from gogl.i18n import _

from . import navigation, qt_i18n, theme, web
from .desktop import desktop_entry_installed, refresh_desktop_entry
from .main_window import MainWindow


def _refresh_integrations() -> None:
    """Deja el menú, el timer y los accesos de Steam llamando al lanzador fijo,
    que apunta a donde esté ahora la AppImage."""
    try:
        if selfcmd.register():
            refresh_desktop_entry()
            services.refresh()
            steam.repoint_shortcuts()
    except Exception as e:  # noqa: BLE001 - nunca impide abrir la interfaz
        print("gogl: " + _("no se han podido actualizar las integraciones: {error}").format(error=e),
              file=sys.stderr)


def main() -> int:
    _refresh_integrations()
    if not navigation.in_gamescope():
        # La ventana se crea desde el principio preparada para la vista web: si no,
        # al abrir la tienda Qt la destruye y la vuelve a crear (se ve cerrarse y
        # abrirse). En el modo juego se dibuja por software (web.prepare_environment).
        os.environ.setdefault("QT_WIDGETS_RHI", "1")
    web.prepare_environment()
    app = QApplication(sys.argv)
    app.setApplicationName("gogl")
    app.setApplicationDisplayName(APP_TITLE)
    from .icon import app_icon
    app.setWindowIcon(app_icon())
    if desktop_entry_installed():
        app.setDesktopFileName("gogl")
    qt_i18n.install(app)
    theme.apply(app)
    navigation.install(app)
    w = MainWindow()
    if navigation.in_gamescope():
        w.showFullScreen()
    else:
        w.show()
    if "--gamepad" in sys.argv or navigation.in_gamescope():
        navigation.instance().set_active(True)  # foco visible desde el principio
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
