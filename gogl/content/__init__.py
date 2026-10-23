"""Instalador propio de juegos de GOG (Content System v1/v2 e instaladores de Linux)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from .. import config
from ..api import GogApi
from ..state import Game, State
from .builds import BuildInfo, build_info, latest_build, make_plan
from .common import ContentError, InstallPlan, safe_name
from .installer import Cancelled, Installer, Progress, load_manifest, redist_dir, uninstall

DEFAULT_ROOT = Path.home() / "Games" / "gogl"


def install_root() -> Path:
    return Path(config.load().get("install_root") or DEFAULT_ROOT).expanduser()


def default_install_path(info_or_name) -> Path:
    name = info_or_name.install_dir_name if isinstance(info_or_name, BuildInfo) else str(info_or_name)
    return install_root() / safe_name(name)  # el nombre viene del manifiesto


def install_game(api: GogApi, game_id: str, *, platform: str = "windows", language: str | None = None,
                 dlcs: list[str] | None = None, install_path: Path | None = None,
                 progress: Callable[[Progress], None] = lambda p: None,
                 cancel: threading.Event | None = None, state: State | None = None) -> Game:
    inst = Installer(api, state, progress, cancel)
    if platform == "linux":
        from .linux import install_linux
        return install_linux(inst, game_id, install_path,
                             language or config.load().get("language") or "es-ES")
    inst.p.stage = "prepare"
    progress(inst.p)
    info = build_info(api, game_id, platform)
    lang = language or info.default_language(config.load().get("language") or "es-ES")
    chosen = [d for d, _ in info.dlcs] if dlcs is None else dlcs
    plan = make_plan(info, lang, chosen)
    return inst.install(plan, install_path or default_install_path(info))


def check_update(api: GogApi, game_id: str) -> tuple[str, str] | None:
    """(versión instalada, versión nueva) si hay actualización; None si está al día."""
    m = load_manifest(game_id)
    if not m or m["plan"].get("generation") == 0:
        return None
    b = latest_build(api, game_id, m["plan"]["platform"])
    if str(b["build_id"]) != m["plan"]["build_id"]:
        return m["plan"].get("version", ""), b.get("version_name", "")
    if m["plan"].get("dependencies") is None and m["plan"]["platform"] == "windows":
        # Instalado antes de que gogl bajara las dependencias (DOSBox, Visual C++…):
        # «actualizar» trae solo lo que falta.
        return m["plan"].get("version", ""), b.get("version_name", "")
    return None


def update_game(api: GogApi, game_id: str, progress=lambda p: None, cancel=None,
                state: State | None = None) -> Game:
    m = load_manifest(game_id)
    if not m:
        raise ContentError("Este juego no se instaló con gogl")
    p = m["plan"]
    return install_game(api, game_id, platform=p["platform"], language=p["language"], dlcs=p["dlcs"],
                        install_path=Path(m["install_path"]), progress=progress, cancel=cancel,
                        state=state)


__all__ = ["BuildInfo", "Cancelled", "ContentError", "InstallPlan", "Installer", "Progress",
           "build_info", "check_update", "default_install_path", "install_game", "install_root",
           "load_manifest", "redist_dir", "uninstall", "update_game"]
