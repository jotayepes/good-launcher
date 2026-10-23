"""Orquestación de la sincronización de un juego: credenciales, ubicaciones,
bloqueo, espera de red y registro del estado (pendiente / ok / error)."""

from __future__ import annotations

from typing import Callable

from .api import ApiError, GogApi
from .auth import AuthError, AuthManager
from .locking import game_lock
from .net import wait_for_network
from .saves.backup import backup_locations
from .saves.cloud import CloudError, CloudStorage
from .saves.engine import ConflictError, SyncError, SyncResult, Syncer
from .saves.locations import CloudConfig, LocationError, SaveLocation, parse_remote_config, resolve_locations
from .state import Game, State


class SyncUnavailable(Exception):
    """El juego no tiene sincronización (nativo de Linux, desactivada en GOG, sin red…)."""


class Offline(SyncUnavailable):
    """No hay conexión con GOG en este momento."""


RECOVERABLE = (CloudError, SyncError, AuthError, ApiError, LocationError, OSError)


class SyncService:
    def __init__(self, state: State, auth: AuthManager, api: GogApi | None = None,
                 log: Callable[[str], None] = print):
        self.state = state
        self.auth = auth
        self.api = api or GogApi(auth)
        self.log = log

    def _game(self, game_id: str) -> Game:
        g = self.state.game(game_id)
        if g is None:
            raise SyncUnavailable(f"Juego {game_id} no registrado")
        return g

    def cloud_config(self, game: Game, refresh: bool = True) -> CloudConfig:
        """remote-config de GOG, cacheado en el juego para poder resolver rutas sin red."""
        if not game.client_id:
            raise SyncUnavailable(f"{game.title}: sin clientId, no admite partidas en la nube")
        if refresh or "remote_config" not in game.extra:
            try:
                game.extra["remote_config"] = self.api.remote_config(game.client_id)
                self.state.save_game(game)
            except (ApiError, OSError) as e:
                if "remote_config" not in game.extra:
                    raise
                self.log(f"aviso: no se pudo refrescar remote-config ({e}); uso la copia guardada")
        return parse_remote_config(game.extra["remote_config"], game.client_id)

    def locations(self, game: Game, refresh: bool = True) -> list[SaveLocation]:
        if game.platform != "windows":
            raise SyncUnavailable(f"{game.title}: los juegos nativos de Linux no tienen partidas en la nube")
        if game.prefix is None:
            raise SyncUnavailable(f"{game.title}: no tiene prefijo de Proton asignado")
        cfg = self.cloud_config(game, refresh)
        if not cfg.enabled:
            raise SyncUnavailable(f"{game.title}: GOG no tiene partidas en la nube para este juego")
        return resolve_locations(cfg, game.install_path, game.prefix, game.save_overrides)

    def _syncer(self, game: Game, locations: list[SaveLocation]) -> Syncer:
        if not game.client_secret:
            _, game.client_secret = self.api.client_credentials(game.game_id, "windows")
            self.state.save_game(game)
        cloud = CloudStorage(
            game.client_id,
            lambda force: self.auth.credentials(game.client_id, game.client_secret, force_refresh=force))
        return Syncer(self.state, game, locations, cloud, log=self.log,
                      backup=lambda: backup_locations(game.game_id, locations))

    def sync(self, game_id: str, *, prefer: str | None = None, dry_run: bool = False,
             wait_network: float = 30, lock_timeout: float | None = 600) -> SyncResult:
        game = self._game(game_id)
        with game_lock(game_id, lock_timeout):
            if wait_network and not wait_for_network(wait_network, self.log):
                if not dry_run:
                    self.state.set_status(game_id, pending=True, error="sin conexión")
                raise Offline("No hay conexión con GOG; la sincronización queda pendiente")
            try:
                locations = self.locations(game)
                for loc in locations:
                    self.log(f"ubicación «{loc.name}»: {loc.path}")
                res = self._syncer(game, locations).run(prefer=prefer, dry_run=dry_run)
            except ConflictError:
                if not dry_run:
                    self.state.set_status(game_id, pending=True, error="conflicto")
                raise
            except RECOVERABLE as e:
                if not dry_run:
                    self.state.set_status(game_id, pending=True, error=str(e))
                raise
            if not dry_run:
                self.state.set_status(game_id, pending=False)
            return res

    def mark_pending(self, game_id: str, reason: str = "partida sin subir") -> None:
        self.state.set_status(game_id, pending=True, error=reason)

    def sync_pending(self, **kw) -> dict[str, Exception | SyncResult]:
        out: dict[str, Exception | SyncResult] = {}
        for gid in self.state.pending_games():
            try:
                out[gid] = self.sync(gid, **kw)
            except Exception as e:  # noqa: BLE001 - se informa por juego
                out[gid] = e
        return out


def describe(res: SyncResult) -> str:
    if res.dry_run:
        lines = [f"  {it.action.value:13} {it.key}  ({it.reason})" for it in res.plan.changes]
        return "\n".join(lines) or "  (nada que hacer)"
    parts = []
    for label, lst in (("subidos", res.uploaded), ("descargados", res.downloaded),
                       ("borrados en la nube", res.deleted_cloud), ("borrados en local", res.deleted_local)):
        if lst:
            parts.append(f"{len(lst)} {label}")
    return ", ".join(parts) or "ya estaba sincronizado"


__all__ = ["SyncService", "SyncUnavailable", "Offline", "ConflictError", "describe"]
