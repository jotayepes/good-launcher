"""Accesos directos «juego ajeno a Steam» para el modo juego de SteamOS.

El acceso directo ejecuta `gogl run <id>`: Proton lo invoca gogl, así que en
Steam NO hay que forzar ninguna herramienta de compatibilidad.

Steam reescribe shortcuts.vdf al cerrarse, así que hay que editarlo con Steam
cerrado o los cambios se pierden."""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import APP_NAME
from .library import cover_path, product_details
from .selfcmd import cli_command, ui_command
from .state import Game


class SteamError(Exception):
    pass


# --- VDF binario ------------------------------------------------------------------

def vdf_load(data: bytes) -> dict:
    pos = 0

    def cstr() -> str:
        nonlocal pos
        end = data.index(b"\x00", pos)
        s = data[pos:end].decode("utf-8", "replace")
        pos = end + 1
        return s

    def map_() -> dict:
        nonlocal pos
        out: dict = {}
        while True:
            t = data[pos]
            pos += 1
            if t == 0x08:
                return out
            key = cstr()
            if t == 0x00:
                out[key] = map_()
            elif t == 0x01:
                out[key] = cstr()
            elif t == 0x02:
                out[key] = struct.unpack_from("<i", data, pos)[0]
                pos += 4
            else:
                raise SteamError(f"Tipo VDF desconocido 0x{t:02x}")

    return map_() if data else {}


def vdf_dump(obj: dict) -> bytes:
    out = bytearray()

    def w(d: dict):
        for k, v in d.items():
            kb = str(k).encode() + b"\x00"
            if isinstance(v, dict):
                out.extend(b"\x00" + kb)
                w(v)
                out.append(0x08)
            elif isinstance(v, int):
                out.extend(b"\x02" + kb + struct.pack("<i", v))
            else:
                out.extend(b"\x01" + kb + str(v).encode() + b"\x00")

    w(obj)
    out.append(0x08)
    return bytes(out)


# --- localizar Steam ----------------------------------------------------------------

def steam_roots() -> list[Path]:
    home = Path.home()
    cands = [home / ".local/share/Steam", home / ".steam/steam",
             home / ".var/app/com.valvesoftware.Steam/.local/share/Steam"]
    seen, out = set(), []
    for c in cands:
        if (c / "userdata").is_dir() and c.resolve() not in seen:
            seen.add(c.resolve())
            out.append(c)
    return out


def user_dirs() -> list[Path]:
    """Directorios userdata/<id> con configuración, el usado más recientemente primero."""
    dirs = []
    for root in steam_roots():
        for d in (root / "userdata").iterdir():
            if d.name.isdigit() and d.name != "0" and (d / "config").is_dir():
                dirs.append(d)
    def mtime(d: Path) -> float:
        try:
            return (d / "config" / "localconfig.vdf").stat().st_mtime
        except OSError:
            return 0
    return sorted(dirs, key=mtime, reverse=True)


def steam_running() -> bool:
    try:
        out = subprocess.run(["pgrep", "-x", "steam"], capture_output=True).stdout
        return bool(out.strip())
    except OSError:
        return False


# --- accesos directos -----------------------------------------------------------------

def shortcut_appid(exe: str, name: str) -> int:
    """El appid que calcula Steam para un acceso directo (sin signo)."""
    return (zlib.crc32((exe + name).encode()) & 0xFFFFFFFF) | 0x80000000


def launcher_command() -> tuple[str, str]:
    """(Exe, prefijo de argumentos) para invocar gogl desde Steam."""
    cmd = cli_command()
    if len(cmd) == 1:  # lanzador fijo de la AppImage
        return cmd[0], ""
    script = shutil.which("gogl") or str(Path(sys.executable).parent / "gogl")
    if Path(script).exists():
        return script, ""
    return sys.executable, "-m gogl.cli"


@dataclass
class ShortcutResult:
    appid: int
    user_dir: Path
    updated: bool


def add_shortcut(game: Game, user_dir: Path | None = None, force: bool = False,
                 http: httpx.Client | None = None) -> ShortcutResult:
    if steam_running() and not force:
        raise SteamError("Steam está abierto: ciérralo antes, o perderá el acceso directo al salir")
    if user_dir is None:
        dirs = user_dirs()
        if not dirs:
            raise SteamError("No se ha encontrado ninguna instalación de Steam con usuario")
        user_dir = dirs[0]
    cfg = user_dir / "config"
    vdf_path = cfg / "shortcuts.vdf"
    data = vdf_load(vdf_path.read_bytes()) if vdf_path.exists() else {}
    shortcuts = data.setdefault("shortcuts", {})

    exe, pre = launcher_command()
    quoted = f'"{exe}"'
    launch = f"{pre} run {game.game_id}".strip()
    appid = shortcut_appid(quoted, game.title)
    entry = {
        "appid": struct.unpack("<i", struct.pack("<I", appid))[0],
        "AppName": game.title,
        "Exe": quoted,
        "StartDir": f'"{Path(exe).parent}"',
        "icon": "",
        "ShortcutPath": "",
        "LaunchOptions": launch,
        "IsHidden": 0,
        "AllowDesktopConfig": 1,
        "AllowOverlay": 1,
        "OpenVR": 0,
        "Devkit": 0,
        "DevkitGameID": "",
        "DevkitOverrideAppID": 0,
        "LastPlayTime": 0,
        "FlatpakAppID": "",
        "tags": {"0": "GOG"},
    }
    updated = False
    for k, sc in shortcuts.items():
        if sc.get("LaunchOptions", "").split()[-2:] == ["run", game.game_id]:
            # Conservamos su appid para no perder artwork, horas ni ajustes de Steam.
            entry["appid"] = sc.get("appid", entry["appid"])
            appid = entry["appid"] & 0xFFFFFFFF
            entry["LastPlayTime"] = sc.get("LastPlayTime", 0)
            entry["tags"] = sc.get("tags", entry["tags"])
            shortcuts[k] = entry
            updated = True
            break
    else:
        shortcuts[_next_key(shortcuts)] = entry

    _save_shortcuts(vdf_path, data)
    _artwork(game.game_id, appid, cfg / "grid", http)
    return ShortcutResult(appid, user_dir, updated)


def repoint_shortcuts() -> int:
    """Reapunta al lanzador fijo los accesos directos de gogl que llaman a una
    AppImage por su ruta (los creados antes de existir el lanzador). Conserva el
    appid, así que no se pierden carátulas ni horas. Devuelve cuántos cambia."""
    if len(cli_command()) != 1 or steam_running():
        return 0
    exe = ui_command()
    changed = 0
    for d in user_dirs():
        vdf_path = d / "config" / "shortcuts.vdf"
        if not vdf_path.exists():
            continue
        data = vdf_load(vdf_path.read_bytes())
        n = 0
        for sc in data.get("shortcuts", {}).values():
            ours = (sc.get("LaunchOptions", "").split()[-2:-1] == ["run"]
                    or LAUNCHER_TAG in (sc.get("tags") or {}).values())
            if ours and sc.get("Exe", "").lower().endswith('.appimage"'):
                sc["Exe"] = f'"{exe}"'
                sc["StartDir"] = f'"{Path(exe).parent}"'
                n += 1
        if n:
            _save_shortcuts(vdf_path, data)
            changed += n
    return changed


def _next_key(shortcuts: dict) -> str:
    """Clave libre: si hay huecos («0», «2»), len() pisaría un acceso directo existente."""
    return str(max((int(k) for k in shortcuts if str(k).isdigit()), default=-1) + 1)


def _save_shortcuts(vdf_path: Path, data: dict) -> None:
    if vdf_path.exists():
        shutil.copy2(vdf_path, vdf_path.with_suffix(".vdf.gogl-bak"))
    tmp = vdf_path.with_suffix(".tmp")
    tmp.write_bytes(vdf_dump(data))
    os.replace(tmp, vdf_path)


LAUNCHER_TAG = "gogl"


def add_launcher_shortcut(user_dir: Path | None = None, force: bool = False) -> ShortcutResult:
    """gogl como «juego no de Steam»: así se abre desde el modo juego y Steam le
    pasa el mando (como un mando de Xbox virtual, que gogl lee)."""
    if steam_running() and not force:
        raise SteamError("Steam está abierto: ciérralo antes, o perderá el acceso directo al salir")
    if user_dir is None:
        dirs = user_dirs()
        if not dirs:
            raise SteamError("No se ha encontrado ninguna instalación de Steam con usuario")
        user_dir = dirs[0]
    vdf_path = user_dir / "config" / "shortcuts.vdf"
    data = vdf_load(vdf_path.read_bytes()) if vdf_path.exists() else {}
    shortcuts = data.setdefault("shortcuts", {})
    exe = ui_command()
    quoted = f'"{exe}"'
    appid = shortcut_appid(quoted, APP_NAME)
    entry = {
        "appid": struct.unpack("<i", struct.pack("<I", appid))[0], "AppName": APP_NAME, "Exe": quoted,
        "StartDir": f'"{Path(exe).parent}"', "icon": "", "ShortcutPath": "", "LaunchOptions": "",
        "IsHidden": 0, "AllowDesktopConfig": 1, "AllowOverlay": 1, "OpenVR": 0, "Devkit": 0,
        "DevkitGameID": "", "DevkitOverrideAppID": 0, "LastPlayTime": 0, "FlatpakAppID": "",
        "tags": {"0": LAUNCHER_TAG},
    }
    updated = False
    for k, sc in shortcuts.items():
        # También con los nombres que tuvo antes, para renombrar en vez de duplicar.
        if LAUNCHER_TAG in (sc.get("tags") or {}).values() and sc.get("AppName") in (APP_NAME, "gogl", "GOG Linux"):
            entry["appid"] = sc.get("appid", entry["appid"])
            appid = entry["appid"] & 0xFFFFFFFF
            entry["LastPlayTime"] = sc.get("LastPlayTime", 0)
            shortcuts[k] = entry
            updated = True
            break
    else:
        shortcuts[_next_key(shortcuts)] = entry
    _save_shortcuts(vdf_path, data)
    return ShortcutResult(appid, user_dir, updated)


def _artwork(game_id: str, appid: int, grid: Path, http: httpx.Client | None) -> None:
    """Carátula vertical, cabecera (hero) y cápsula horizontal. El logo de GOG
    no tiene transparencia y queda mal sobre la cabecera, así que no se usa."""
    grid.mkdir(parents=True, exist_ok=True)
    http = http or httpx.Client(timeout=30, follow_redirects=True)
    try:
        d = product_details(game_id, http)
    except Exception:  # noqa: BLE001 - el artwork es opcional
        return
    jobs = [(d.get("cover"), f"{appid}p.jpg"), (d.get("background"), f"{appid}_hero.jpg"),
            (d.get("background"), f"{appid}.jpg")]
    for url, name in jobs:
        if not url:
            continue
        try:
            if name.endswith("p.jpg") and cover_path(game_id).exists():
                shutil.copy2(cover_path(game_id), grid / name)
                continue
            r = http.get(url)
            if r.status_code == 200:
                (grid / name).write_bytes(r.content)
        except (httpx.HTTPError, OSError):
            pass
