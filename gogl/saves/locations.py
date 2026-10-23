"""Ubicaciones de partidas: lectura del remote-config de GOG y resolución a rutas Linux.

GOG describe las carpetas con variables (<?INSTALL?>, <?DOCUMENTS?>, ...) pensadas
para Windows. Aquí se traducen a rutas dentro del prefijo de Proton/Wine, sin
depender de que Wine esté arrancado (no consultamos el registro), y resolviendo
mayúsculas/minúsculas porque en Windows dan igual pero en Linux no.
"""

from __future__ import annotations

import getpass
import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LOCATION = "__default"
DEFAULT_TEMPLATE = "<?APPLICATION_DATA_LOCAL?>/GOG.com/Galaxy/Applications/{client_id}/Storage/Shared/Files"

_VAR_RE = re.compile(r"<\?(\w+)\?>|%(\w+)%")


class LocationError(Exception):
    pass


@dataclass(frozen=True)
class SaveLocation:
    name: str
    template: str
    path: Path


@dataclass(frozen=True)
class CloudConfig:
    enabled: bool
    quota: int | None
    locations: list[tuple[str, str]]  # (nombre, plantilla)


def parse_remote_config(cfg: dict, client_id: str, platform: str = "Windows") -> CloudConfig:
    cs = (cfg.get("content") or {}).get(platform, {}).get("cloudStorage") or {}
    if not cs.get("enabled"):
        return CloudConfig(False, None, [])
    locs = [(l["name"], l["location"]) for l in cs.get("locations") or []]
    if not locs:
        # Juegos que usan la API de almacenamiento del SDK de Galaxy.
        locs = [(DEFAULT_LOCATION, DEFAULT_TEMPLATE.format(client_id=client_id))]
    return CloudConfig(True, cs.get("quota"), locs)


def normalize_prefix(prefix: Path) -> Path:
    """Acepta tanto un compatdata de Proton (con pfx/) como un prefijo de Wine."""
    if (prefix / "pfx" / "drive_c").is_dir():
        return prefix / "pfx"
    return prefix


def wine_user_dir(prefix: Path) -> Path:
    users = normalize_prefix(prefix) / "drive_c" / "users"
    for name in ("steamuser", getpass.getuser()):
        if (users / name).is_dir():
            return users / name
    return users / "steamuser"


def variables(install_path: Path, prefix: Path | None) -> dict[str, Path]:
    v: dict[str, Path] = {"INSTALL": install_path}
    if prefix is not None:
        u = wine_user_dir(prefix)
        v.update({
            "USER_PROFILE": u,
            "USERPROFILE": u,
            "DOCUMENTS": u / "Documents",
            "SAVED_GAMES": u / "Saved Games",
            "APPLICATION_DATA_ROAMING": u / "AppData" / "Roaming",
            "APPDATA": u / "AppData" / "Roaming",
            "APPLICATION_DATA_LOCAL": u / "AppData" / "Local",
            "LOCALAPPDATA": u / "AppData" / "Local",
            "APPLICATION_DATA_LOCAL_LOW": u / "AppData" / "LocalLow",
        })
    return v


def resolve_case_insensitive(path: Path) -> Path:
    """Recorre la ruta y, componente a componente, usa la variante con las
    mayúsculas que ya existan en disco. Lo que no exista se deja tal cual."""
    parts = path.parts
    cur = Path(parts[0])
    for i, part in enumerate(parts[1:], start=1):
        cand = cur / part
        if cand.exists():
            cur = cand
            continue
        match = None
        if cur.is_dir():
            low = part.casefold()
            try:
                for entry in os.scandir(cur):
                    if entry.name.casefold() == low:
                        match = entry.name
                        break
            except OSError:
                pass
        if match is None:
            return cur.joinpath(*parts[i:])
        cur = cur / match
    return cur


def resolve_template(template: str, install_path: Path, prefix: Path | None) -> Path:
    vars_ = variables(install_path, prefix)

    def sub(m: re.Match) -> str:
        name = (m.group(1) or m.group(2)).upper()
        if name not in vars_:
            if prefix is None and name != "INSTALL":
                raise LocationError(f"{m.group(0)} requiere un prefijo de Wine/Proton")
            raise LocationError(f"Variable desconocida {m.group(0)} en «{template}»")
        return str(vars_[name])

    raw = _VAR_RE.sub(sub, template).replace("\\", "/")
    if not raw.startswith("/"):
        raise LocationError(f"La ruta «{template}» no es absoluta tras resolver variables")
    return resolve_case_insensitive(Path(os.path.normpath(raw)))


def resolve_locations(cfg: CloudConfig, install_path: Path, prefix: Path | None,
                      overrides: dict[str, str] | None = None) -> list[SaveLocation]:
    overrides = overrides or {}
    out = []
    for name, template in cfg.locations:
        if name in overrides:
            path = Path(overrides[name]).expanduser()
        else:
            path = resolve_template(template, install_path, prefix)
        out.append(SaveLocation(name, template, path))
    return out
