"""Registro de juegos instalados a partir de sus archivos goggame-<id>.info."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from . import paths
from .api import ApiError, GogApi
from .auth import AuthError
from .state import Game


class GameInfoError(Exception):
    pass


def read_game_info(install_path: Path) -> dict:
    """Devuelve el goggame-*.info del juego base (no el de los DLC)."""
    infos = []
    for p in list(install_path.glob("goggame-*.info")) + list(install_path.glob("game/goggame-*.info")):
        try:
            infos.append(json.loads(p.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError):
            continue
    if not infos:
        raise GameInfoError(f"No hay ningún goggame-*.info en {install_path}")
    for info in infos:
        if str(info.get("gameId")) == str(info.get("rootGameId", info.get("gameId"))):
            return info
    return infos[0]


def detect_platform(info: dict) -> str:
    exe = any(t.get("path", "").lower().endswith(".exe") for t in info.get("playTasks", []))
    return "windows" if exe else "linux"


def register_installed(install_path: Path, prefix: Path | None, api: GogApi | None,
                       platform: str | None = None) -> Game:
    install_path = install_path.resolve()
    info = read_game_info(install_path)
    game_id = str(info["gameId"])
    platform = platform or detect_platform(info)
    client_id = info.get("clientId")
    client_secret = None
    if platform == "windows" and api is not None:
        # Sin sesión o sin red se registra igual: el secreto se pide al sincronizar.
        try:
            cid, client_secret = api.client_credentials(game_id, "windows")
            client_id = client_id or cid
        except (AuthError, ApiError, httpx.HTTPError):
            pass
    if platform == "windows" and prefix is None:
        prefix = paths.data_dir() / "prefixes" / game_id  # prefijo propio, estilo compatdata
    return Game(
        game_id=game_id,
        title=info.get("name", game_id),
        platform=platform,
        install_path=install_path,
        prefix=prefix.resolve() if prefix else None,
        client_id=str(client_id) if client_id else None,
        client_secret=client_secret,
    )
