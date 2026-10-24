"""Construcción del comando para lanzar un juego (Proton-GE gestionado o nativo)."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .. import APP_NAME, languages, paths
from ..i18n import _
from ..games import read_game_info
from ..saves.locations import resolve_case_insensitive
from ..state import Game
from . import proton as proton_mgr


class LaunchError(Exception):
    pass


@dataclass
class LaunchSpec:
    argv: list[str]
    env: dict[str, str]
    cwd: Path
    compat: Path | None = None
    proton: proton_mgr.ProtonVersion | None = None


def primary_task(install_path: Path) -> dict:
    info = read_game_info(install_path)
    tasks = [t for t in info.get("playTasks", []) if t.get("type", "FileTask") == "FileTask"]
    for t in tasks:
        if t.get("isPrimary"):
            return t
    if tasks:
        return tasks[0]
    raise LaunchError(_("goggame-*.info no tiene ninguna tarea de juego"))


def split_windows_args(cmdline: str) -> list[str]:
    """Trocea argumentos con las reglas de CommandLineToArgvW: las barras
    invertidas son literales salvo delante de comillas. (shlex se comería las
    de rutas como C:\\Users\\....)"""
    args: list[str] = []
    cur: list[str] = []
    in_quotes = has_arg = False
    i, n = 0, len(cmdline)
    while i < n:
        c = cmdline[i]
        if c == "\\":
            j = i
            while j < n and cmdline[j] == "\\":
                j += 1
            count = j - i
            if j < n and cmdline[j] == '"':
                cur.append("\\" * (count // 2))
                if count % 2:
                    cur.append('"')
                    i = j + 1
                else:
                    i = j
            else:
                cur.append("\\" * count)
                i = j
            has_arg = True
            continue
        if c == '"':
            if in_quotes and i + 1 < n and cmdline[i + 1] == '"':
                cur.append('"')
                i += 2
                continue
            in_quotes = not in_quotes
            has_arg = True
        elif c in " \t" and not in_quotes:
            if has_arg:
                args.append("".join(cur))
                cur, has_arg = [], False
        else:
            cur.append(c)
            has_arg = True
        i += 1
    if has_arg:
        args.append("".join(cur))
    return args


def in_gamescope() -> bool:
    return bool(os.environ.get("GAMESCOPE_WAYLAND_DISPLAY")
                or os.environ.get("XDG_CURRENT_DESKTOP", "").lower() == "gamescope")


def _scummvm_windowed(exe: Path, args: list[str]) -> list[str]:
    """ScummVM a pantalla completa cambia el modo de vídeo de Wine y en gamescope la
    imagen sale negra o congelada; en ventana, gamescope la escala solo."""
    if exe.name.lower() != "scummvm.exe" or not in_gamescope():
        return args
    if any(a in ("-f", "-F", "--fullscreen", "--no-fullscreen") for a in args):
        return args
    return ["--no-fullscreen", *args]


def _win_path(base: Path, rel: str) -> Path:
    rel = rel.replace("\\", "/").lstrip("/")
    return resolve_case_insensitive(base / rel)


def compat_data_path(game: Game) -> Path:
    """Directorio estilo compatdata (con pfx/ dentro) para Proton.

    Si el juego se vinculó con un prefijo de Wine «plano» (drive_c en la raíz,
    como los de Lutris), se crea una capa compatdata con pfx → ese prefijo."""
    p = game.prefix or paths.data_dir() / "prefixes" / game.game_id
    if (p / "drive_c").is_dir() and not (p / "pfx").exists():
        shim = paths.data_dir() / "compat" / game.game_id
        shim.mkdir(parents=True, exist_ok=True)
        link = shim / "pfx"
        # Si el prefijo del juego cambió (o el enlace quedó roto), se reapunta.
        if link.is_symlink() and Path(os.readlink(link)) != p:
            link.unlink()
        if not link.is_symlink() and not link.exists():
            link.symlink_to(p)
        return shim
    p.mkdir(parents=True, exist_ok=True)
    return p


def proton_env(game: Game, compat: Path) -> dict[str, str]:
    env = os.environ.copy()
    stub = paths.data_dir() / "steam-stub"
    stub.mkdir(parents=True, exist_ok=True)
    env["STEAM_COMPAT_DATA_PATH"] = str(compat)
    env["STEAM_COMPAT_INSTALL_PATH"] = str(game.install_path)
    # Dentro de Steam (modo juego) estas variables ya vienen puestas y gamescope
    # usa SteamGameId para saber qué ventana es «el juego»: no se pisan.
    env.setdefault("STEAM_COMPAT_CLIENT_INSTALL_PATH", str(stub))
    env.setdefault("SteamAppId", "0")
    env.setdefault("SteamGameId", "0")
    env.setdefault("STEAM_COMPAT_APP_ID", "0")
    env.setdefault("WINEDEBUG", "-all")
    # Xalia (Proton-GE) rastrea las ventanas del juego para manejarlas con el mando;
    # con juegos de teclado y ratón solo lo estorba (excepciones sin fin en el registro
    # y el uso de CPU). La navegación con mando la damos nosotros. Se puede reactivar
    # con PROTON_USE_XALIA=1 en las opciones de lanzamiento del juego.
    env.setdefault("PROTON_USE_XALIA", "0")
    env.update(languages.env_for(game.extra.get("language")))
    return env


def parse_env(text: str) -> dict[str, str]:
    """«CLAVE=valor», una por línea (las líneas vacías y las que empiezan por # se ignoran)."""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip().replace("_", "").isalnum():
            raise LaunchError(_("Variable de entorno no válida: «{line}» (formato CLAVE=valor)").format(line=line))
        out[key.strip()] = value.strip()
    return out


def _apply_options(spec: LaunchSpec, game: Game) -> LaunchSpec:
    """Ajustes de lanzamiento del juego: argumentos, variables y comando previo."""
    opts = game.extra.get("launch") or {}
    if opts.get("args"):
        # Los juegos de Windows trocean sus argumentos como Windows; los nativos, como la shell.
        split = split_windows_args if game.platform == "windows" else shlex.split
        spec.argv += split(opts["args"])
    if opts.get("wayland") and game.platform == "windows":
        spec.env["PROTON_ENABLE_WAYLAND"] = "1"
    spec.env.update(parse_env(opts.get("env") or ""))
    if opts.get("wrapper"):
        wrapper = shlex.split(opts["wrapper"])
        # Como en Steam, «VAR=1 mangohud» son variables y luego el comando: sin esto,
        # el sistema intentaría ejecutar un programa llamado «VAR=1».
        while wrapper and re.match(r"[A-Za-z_][A-Za-z0-9_]*=", wrapper[0]):
            key, _sep, value = wrapper.pop(0).partition("=")
            spec.env[key] = value
        spec.argv = [*wrapper, *spec.argv]
    return spec


def build_launch(game: Game, proton_name: str | None = None) -> LaunchSpec:
    return _apply_options(_build_launch(game, proton_name), game)


def _build_launch(game: Game, proton_name: str | None = None) -> LaunchSpec:
    task = primary_task(game.install_path)
    extra = split_windows_args(task.get("arguments") or "")

    languages.apply_to_game_info(game.install_path, game.extra.get("language"))
    if game.platform == "linux":
        native_env = {**os.environ, **languages.env_for(game.extra.get("language"))}
        native_env.pop("HOST_LC_ALL", None)
        start = game.install_path / "start.sh"
        if start.exists():
            return LaunchSpec([str(start)], native_env, game.install_path)
        base = game.install_path / "game" if (game.install_path / "game").is_dir() else game.install_path
        exe = _win_path(base, task["path"])
        cwd = _win_path(base, task["workingDir"]) if task.get("workingDir") else exe.parent
        return LaunchSpec([str(exe), *extra], native_env, cwd)

    pv = proton_mgr.get(proton_name or game.extra.get("proton"))
    if pv is None:
        raise LaunchError(_("No hay ninguna versión de Proton-GE instalada en {app}").format(app=APP_NAME))
    exe = _win_path(game.install_path, task["path"])
    if not exe.exists():
        raise LaunchError(_("No se encuentra el ejecutable {exe}").format(exe=exe))
    cwd = _win_path(game.install_path, task["workingDir"]) if task.get("workingDir") else exe.parent
    extra = _scummvm_windowed(exe, extra)
    compat = compat_data_path(game)
    argv = [str(pv.script), "waitforexitandrun", str(exe), *extra]
    return LaunchSpec(argv, proton_env(game, compat), cwd, compat, pv)


def log_path(game_id: str) -> Path:
    d = paths.state_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{game_id}.log"


def open_log(game_id: str):
    """Registro de la última partida (Proton, protonfixes, el juego), en vez de la consola."""
    p = log_path(game_id)
    if p.exists() and p.stat().st_size > 5 * 2**20:
        p.replace(p.with_suffix(".log.old"))
    fh = open(p, "a")
    import time as _t
    fh.write(f"\n===== {_t.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    fh.flush()
    return fh


def prefix_ready(compat: Path) -> bool:
    return (compat / "version").exists() and (compat / "pfx" / "drive_c").is_dir()


def init_prefix(game: Game, pv: proton_mgr.ProtonVersion) -> None:
    """Crea el prefijo antes del primer arranque, para que las partidas
    descargadas de la nube tengan dónde ir y Proton no las pise."""
    compat = compat_data_path(game)
    if prefix_ready(compat):
        return
    env = proton_env(game, compat)
    with open(log_path(game.game_id), "a") as log:
        subprocess.run([str(pv.script), "run", "wineboot", "-u"], env=env, cwd=game.install_path,
                       stdout=log, stderr=subprocess.STDOUT, timeout=300)
    wait_wineserver(pv, compat, timeout=120)


def wait_wineserver(pv: proton_mgr.ProtonVersion | None, compat: Path | None,
                    timeout: float | None = None) -> None:
    """Espera a que terminen TODOS los procesos de Wine del prefijo: muchos
    juegos arrancan un lanzador que sale enseguida y deja el juego corriendo."""
    if not pv or not compat or not pv.wineserver:
        return
    env = os.environ.copy()
    env["WINEPREFIX"] = str(compat / "pfx")
    try:
        subprocess.run([str(pv.wineserver), "-w"], env=env, timeout=timeout,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pass
