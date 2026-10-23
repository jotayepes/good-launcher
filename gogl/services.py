"""Timer de systemd del usuario que reintenta las sincronizaciones pendientes."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .selfcmd import cli_command

UNIT = "gogl-sync-pending"


def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def systemd_quote(s: str, exec_line: bool = True) -> str:
    """Una palabra entre comillas con el escape de systemd: «%» es especial siempre
    y «$» solo en ExecStart= (en Environment= no se expande)."""
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if exec_line:
        s = s.replace("$", "$$")
    return f'"{s}"'


def unit_files() -> dict[str, str]:
    cmd = " ".join(systemd_quote(c) for c in (*cli_command(), "sync", "--pending"))
    env = "".join(f"Environment={systemd_quote(f'{k}={os.environ[k]}', exec_line=False)}\n" for k in
                  ("GOGL_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME") if k in os.environ)
    return {
        f"{UNIT}.service": (
            "[Unit]\nDescription=gogl: subir partidas pendientes\n"
            "After=network-online.target\nWants=network-online.target\n\n"
            f"[Service]\nType=oneshot\n{env}ExecStart={cmd}\n"),
        f"{UNIT}.timer": (
            "[Unit]\nDescription=gogl: reintentar partidas pendientes cada 15 minutos\n\n"
            "[Timer]\nOnBootSec=2min\nOnUnitActiveSec=15min\nPersistent=true\n\n"
            "[Install]\nWantedBy=timers.target\n"),
    }


def install(enable: bool = True) -> Path:
    d = unit_dir()
    d.mkdir(parents=True, exist_ok=True)
    for name, text in unit_files().items():
        (d / name).write_text(text)
    if enable:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", "--now", f"{UNIT}.timer"], check=False)
    return d


def refresh() -> None:
    """Reescribe las unidades instaladas si el comando ha cambiado."""
    if installed() and any((unit_dir() / n).read_text() != t for n, t in unit_files().items()):
        install()


def uninstall() -> None:
    subprocess.run(["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"], check=False)
    for name in unit_files():
        (unit_dir() / name).unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)


def installed() -> bool:
    return (unit_dir() / f"{UNIT}.timer").exists()
