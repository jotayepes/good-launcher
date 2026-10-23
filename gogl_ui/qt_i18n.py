"""Textos propios de Qt (botones Sí/No/Cancelar, diálogos de archivos…) en el
idioma de la interfaz."""

from __future__ import annotations

from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator

from gogl import i18n

_translators: list[QTranslator] = []  # Qt no se queda con la referencia


def install(app) -> None:
    QLocale.setDefault(QLocale(i18n.current))
    path = QLibraryInfo.path(QLibraryInfo.TranslationsPath)
    for name in ("qtbase", "qtwebengine"):
        t = QTranslator(app)
        if t.load(f"{name}_{i18n.current}", path):
            app.installTranslator(t)
            _translators.append(t)
