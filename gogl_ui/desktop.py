"""Entrada en el menú de aplicaciones (archivo .desktop)."""

from __future__ import annotations

import os
from pathlib import Path

from gogl import APP_TITLE
from gogl.selfcmd import ui_command

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#121116"/>
<path d="M32 10 L54 32 L32 54 L10 32 Z" fill="#8b6cf0"/>
<path d="M32 22 L42 32 L32 42 L22 32 Z" fill="#121116"/>
</svg>
"""


def _apps_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base) / "applications"


def desktop_path() -> Path:
    return _apps_dir() / "gogl.desktop"


def desktop_entry_installed() -> bool:
    return desktop_path().exists()


def install_desktop_entry() -> Path:
    exe = ui_command()
    icon_dir = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "icons/hicolor/scalable/apps"
    icon_dir.mkdir(parents=True, exist_ok=True)
    (icon_dir / "gogl.svg").write_text(ICON_SVG)
    _apps_dir().mkdir(parents=True, exist_ok=True)
    desktop_path().write_text(
        f"[Desktop Entry]\nType=Application\nName={APP_TITLE}\nGenericName=Lanzador de juegos de GOG\n"
        "Comment=Tus juegos de GOG con las partidas sincronizadas\n"
        f'Exec="{exe}"\nIcon=gogl\nTerminal=false\nCategories=Game;\nStartupWMClass=gogl\n')
    return desktop_path()


def refresh_desktop_entry() -> None:
    """Reescribe la entrada si apunta a otro ejecutable (p. ej. a la AppImage
    por su ruta, de antes del lanzador fijo)."""
    if desktop_entry_installed() and f'Exec="{ui_command()}"\n' not in desktop_path().read_text():
        install_desktop_entry()


def remove_desktop_entry() -> None:
    desktop_path().unlink(missing_ok=True)
