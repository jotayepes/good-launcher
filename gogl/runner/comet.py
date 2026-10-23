"""Logros mientras se juega, con Comet.

Los juegos con logros usan el Galaxy SDK (Galaxy.dll/Galaxy64.dll), que habla
por el puerto local 9977 con el «Communication Service» de Galaxy. Comet
(https://github.com/imLinguin/comet, Apache-2.0) implementa ese servicio en
Linux; Wine comparte la red local con el sistema, así que el juego, dentro de
Proton, llega a él sin más.

- Se descarga la versión fijada del release oficial y se verifica su SHA-256,
  como Proton-GE, en ~/.local/share/gogl/comet/<versión>.
- `gogl run` lo arranca antes del juego y lo cierra al terminar, dándole unos
  segundos para que suba lo que falte. Sin conexión, Comet guarda los logros y
  los sube la próxima vez que se juegue con red.
- Algunas versiones del SDK exigen que exista el servicio de Windows
  «GalaxyCommunication»: se registra un servicio ficticio en el prefijo (una
  vez por prefijo).
- Comet escribe «Unlocking achievement <clave>, <nombre>» en su salida: de ahí
  sale la notificación al momento.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import zipfile
from pathlib import Path
from typing import Callable

import httpx

from .. import config, paths

VERSION = "v0.3.2"
URL = "https://github.com/imLinguin/comet/releases/download/{version}/{name}"
BINARY = "comet-x86_64-unknown-linux-gnu"
DUMMY = "dummy-service.zip"
SHA256 = {
    BINARY: "2d6694d544fd3155d90d540e70bc1be767a6b9fdda130275f2b79616ff14e843",
    DUMMY: "7fc5facd1fce4d516cd3ce527e7e4dbe0bfd17649cea805e8ea7c37d4ba58d9a",
}
SERVICE_TARGET = "C:\\ProgramData\\GOG.com\\Galaxy\\redists"
UNLOCK_RE = re.compile(r"Unlocking achievement (\S+), (.*)$")
STOP_GRACE = 15


class CometError(Exception):
    pass


def comet_dir() -> Path:
    return paths.data_dir() / "comet" / VERSION


def binary() -> Path:
    return comet_dir() / "comet"


def dummy_dir() -> Path:
    return comet_dir() / "dummy-service"


def installed() -> bool:
    return binary().exists() and (dummy_dir() / "install-dummy-service.bat").exists()


def enabled(game) -> bool:
    """Activado en Ajustes (por defecto sí) y no desactivado en el juego."""
    if not config.load().get("achievements", True):
        return False
    return game.extra.get("achievements", True) is not False


def uses_galaxy_sdk(install_path: Path) -> bool:
    """El juego trae el Galaxy SDK (si no, no puede desbloquear logros fuera de Galaxy)."""
    names = {"galaxy.dll", "galaxy64.dll"}
    base_depth = len(install_path.parts)
    for root, dirs, files in os.walk(install_path):
        if any(f.lower() in names for f in files):
            return True
        if len(Path(root).parts) - base_depth >= 3:
            dirs[:] = []
    return False


def _download(name: str, dest: Path, http: httpx.Client) -> None:
    tmp = dest.with_suffix(".part")
    h = hashlib.sha256()
    with http.stream("GET", URL.format(version=VERSION, name=name)) as r:
        if r.status_code != 200:
            raise CometError(f"no se pudo descargar Comet ({r.status_code})")
        with open(tmp, "wb") as fh:
            for chunk in r.iter_bytes(1 << 20):
                h.update(chunk)
                fh.write(chunk)
    if h.hexdigest() != SHA256[name]:
        tmp.unlink(missing_ok=True)
        raise CometError("la descarga de Comet no coincide con la suma publicada")
    tmp.replace(dest)


def ensure(http: httpx.Client | None = None) -> None:
    """Descarga Comet y el servicio ficticio si faltan."""
    if installed():
        return
    d = comet_dir()
    d.mkdir(parents=True, exist_ok=True)
    http = http or httpx.Client(timeout=httpx.Timeout(30, read=120), follow_redirects=True)
    if not binary().exists():
        _download(BINARY, d / BINARY, http)
        (d / BINARY).chmod(0o755)
        (d / BINARY).replace(binary())
    zpath = d / DUMMY
    _download(DUMMY, zpath, http)
    with zipfile.ZipFile(zpath) as z:
        for member in z.namelist():
            name = Path(member).name
            if name and "/" not in member.strip("/"):
                (dummy_dir()).mkdir(exist_ok=True)
                (dummy_dir() / name).write_bytes(z.read(member))
    zpath.unlink()


def service_step_args() -> list[str]:
    """Para `proton run`: registra el servicio ficticio en el prefijo."""
    bat = dummy_dir() / "install-dummy-service.bat"
    return ["cmd", "/c", "Z:" + str(bat).replace("/", "\\")]


GALAXY_CLIENT_ID = "46899977096215655"


def _clean_stale_auth() -> None:
    """Si Steam mató el lanzador a mitad de partida, su copia de los tokens se
    quedó en disco: se borran las de procesos que ya no existen."""
    for d in (paths.data_dir() / "comet").glob("auth-*"):
        pid = d.name.removeprefix("auth-")
        if pid.isdigit() and int(pid) != os.getpid():
            try:
                os.kill(int(pid), 0)
                continue  # sigue vivo: otra partida en curso
            except ProcessLookupError:
                pass
            except PermissionError:
                continue
        shutil.rmtree(d, ignore_errors=True)


def _write_auth(tokens: dict) -> Path:
    _clean_stale_auth()
    d = paths.data_dir() / "comet" / f"auth-{os.getpid()}"
    shutil.rmtree(d, ignore_errors=True)
    (d / "heroic" / "gog_store").mkdir(parents=True, mode=0o700)
    d.chmod(0o700)
    f = d / "heroic" / "gog_store" / "auth.json"
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({GALAXY_CLIENT_ID: tokens}, fh)
    return d


class Comet:
    """El proceso de Comet durante una sesión de juego."""

    def __init__(self, tokens: dict, username: str, log=None,
                 on_unlock: Callable[[str, str], None] = lambda key, name: None):
        self.tokens, self.username = tokens, username
        self.log = log
        self.on_unlock = on_unlock
        self.proc: subprocess.Popen | None = None
        self.auth_dir: Path | None = None
        self.unlocked: list[tuple[str, str]] = []

    def start(self) -> None:
        # Los tokens no van en la línea de órdenes (la ve cualquier usuario con
        # `ps`): Comet los lee de un auth.json con el formato de Heroic, en una
        # carpeta privada a la que apunta XDG_CONFIG_PATH (solo la usa ese importador).
        self.auth_dir = _write_auth(self.tokens)
        env = dict(os.environ, COMET_LOG="info", XDG_CONFIG_PATH=str(self.auth_dir))
        argv = [str(binary()), "--from-heroic", "--username", self.username]
        if shutil.which("setpriv"):
            # Si Steam mata el lanzador, Comet recibe Ctrl+C: sube lo que tenga y sale
            # (lo que no llegue a subir queda guardado para la próxima partida).
            argv = ["setpriv", "--pdeathsig", "INT", "--", *argv]
        self.proc = subprocess.Popen(
            argv,
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, errors="replace", start_new_session=True)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            line = line.rstrip()
            if self.log:
                try:
                    self.log.write(f"[comet] {line}\n")
                    self.log.flush()
                except ValueError:  # registro ya cerrado
                    pass
            m = UNLOCK_RE.search(line)
            if m:
                self.unlocked.append((m.group(1), m.group(2)))
                try:
                    self.on_unlock(m.group(1), m.group(2))
                except Exception:  # noqa: BLE001 - una notificación nunca para la sesión
                    pass

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, grace: float = STOP_GRACE) -> None:
        """Ctrl+C a Comet: termina de subir lo pendiente y sale."""
        if not self.running():
            if self.auth_dir:
                shutil.rmtree(self.auth_dir, ignore_errors=True)
            return
        try:
            os.killpg(self.proc.pid, signal.SIGINT)
            self.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        except ProcessLookupError:
            pass
        finally:
            shutil.rmtree(self.auth_dir, ignore_errors=True)


__all__ = ["Comet", "CometError", "enabled", "ensure", "installed", "service_step_args",
           "uses_galaxy_sdk"]
