"""Cómo volver a invocar gogl desde otro proceso.

Dentro de una AppImage, sys.executable vive en un montaje temporal que
desaparece cuando se cierra la aplicación: un proceso que deba sobrevivirla (el
vigilante, el timer de systemd, un acceso directo de Steam) tiene que lanzar la
propia AppImage, que vuelve a montarse.

Y como la AppImage puede moverse, nadie la llama por su ruta: todos llaman a un
lanzador fijo (data_dir()/gogl-launch) que la AppImage reescribe al arrancar con
su ubicación actual y que, si ya no está ahí, la busca."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

from .paths import data_dir

# Si la AppImage se ha movido y aún no se ha vuelto a abrir, se busca en la
# carpeta personal (sin entrar en carpetas ocultas) y en los discos y tarjetas SD
# montados, pero solo se ejecuta si es idéntica (mismo SHA-256) a la última que
# se abrió: una memoria USB o una descarga con un nombre parecido no se ejecutan.
LAUNCHER_SH = """#!/bin/sh
# Generado por gogl: Steam, el menú de aplicaciones y los servicios llaman a este
# archivo, que abre la AppImage de gogl esté donde esté.
A={path}
H={sha}
if [ ! -f "$A" ]; then
    A=$(find "$HOME" "$HOME/.local/bin" /run/media /media -maxdepth 5 \\
            -mindepth 1 -name '.*' -prune -o -type f -iname '*gogl*.appimage' \\
            -printf '%T@ %p\\n' 2>/dev/null | sort -rn | cut -d ' ' -f 2- |
        while IFS= read -r f; do
            if [ "$(sha256sum "$f" 2>/dev/null | cut -d ' ' -f 1)" = "$H" ]; then
                printf '%s\\n' "$f"
                break
            fi
        done)
fi
if [ -z "$A" ]; then
    msg="No se encuentra la AppImage de gogl. Ábrela una vez desde donde esté y todo volverá a funcionar."
    echo "$msg" >&2
    command -v notify-send >/dev/null && notify-send gogl "$msg"
    exit 127
fi
[ -x "$A" ] || chmod +x "$A" 2>/dev/null
exec "$A" "$@"
"""


def appimage() -> str | None:
    p = os.environ.get("APPIMAGE")
    return p if p and Path(p).exists() else None


def launcher_path() -> Path:
    return data_dir() / "gogl-launch"


def _sha256(img: str) -> str:
    """SHA-256 de la AppImage, cacheado por ruta, tamaño y fecha (pesa ~200 MB)."""
    st = os.stat(img)
    ident = [img, st.st_size, st.st_mtime_ns]
    cache = data_dir() / "gogl-launch.id"
    try:
        c = json.loads(cache.read_text())
        if c.get("ident") == ident:
            return c["sha256"]
    except (OSError, ValueError, KeyError):
        pass
    h = hashlib.sha256()
    with open(img, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    try:
        cache.write_text(json.dumps({"ident": ident, "sha256": h.hexdigest()}))
    except OSError:
        pass
    return h.hexdigest()


def register() -> Path | None:
    """Apunta el lanzador fijo a la AppImage en ejecución. Se llama al arrancar."""
    img = appimage()
    if not img:
        return None
    path = launcher_path()
    text = LAUNCHER_SH.format(path=shlex.quote(img), sha=_sha256(img))
    try:
        if path.read_text() == text:
            return path
    except OSError:
        pass
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.chmod(0o755)
    os.replace(tmp, path)
    return path


def cli_command() -> list[str]:
    """Argumentos para ejecutar el comando `gogl` (añadir después la orden)."""
    if appimage():
        return [str(register())]
    return [sys.executable, "-m", "gogl.cli"]


def ui_command() -> str:
    """Ejecutable de la interfaz gráfica."""
    if appimage():
        return str(register())
    return shutil.which("gogl-ui") or str(Path(sys.executable).parent / "gogl-ui")
