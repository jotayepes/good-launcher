"""Orquestación de la sincronización de un juego: credenciales, ubicaciones,
bloqueo, espera de red y registro del estado (pendiente / ok / error)."""

from __future__ import annotations

from typing import Callable

from .api import ApiError, GogApi
from .i18n import _
from .auth import AuthError, AuthManager
from .locking import game_lock
from .net import wait_for_network
from .saves.backup import backup_locations
from .saves.cloud import CloudError, CloudStorage
from .saves.engine import Action, ConflictError, SyncError, SyncResult, Syncer
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
            raise SyncUnavailable(_("Juego {id} no registrado").format(id=game_id))
        return g

    def cloud_config(self, game: Game, refresh: bool = True) -> CloudConfig:
        """remote-config de GOG, cacheado en el juego para poder resolver rutas sin red."""
        if not game.client_id:
            raise SyncUnavailable(_("{title}: sin clientId, no admite partidas en la nube").format(title=game.title))
        if refresh or "remote_config" not in game.extra:
            try:
                game.extra["remote_config"] = self.api.remote_config(game.client_id)
                self.state.save_game(game)
            except (ApiError, OSError) as e:
                if "remote_config" not in game.extra:
                    raise
                self.log(_("aviso: no se pudo refrescar remote-config ({error}); uso la copia guardada")
                         .format(error=e))
        return parse_remote_config(game.extra["remote_config"], game.client_id)

    def locations(self, game: Game, refresh: bool = True) -> list[SaveLocation]:
        if game.platform != "windows":
            raise SyncUnavailable(_("{title}: los juegos nativos de Linux no tienen partidas en la nube")
                                  .format(title=game.title))
        if game.prefix is None:
            raise SyncUnavailable(_("{title}: no tiene prefijo de Proton asignado").format(title=game.title))
        cfg = self.cloud_config(game, refresh)
        if not cfg.enabled:
            raise SyncUnavailable(_("{title}: GOG no tiene partidas en la nube para este juego")
                                  .format(title=game.title))
        return resolve_locations(cfg, game.install_path, game.prefix, game.save_overrides)

    def _syncer(self, game: Game, locations: list[SaveLocation]) -> Syncer:
        if not game.client_secret:
            _cid, game.client_secret = self.api.client_credentials(game.game_id, "windows")
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
                    self.state.set_status(game_id, pending=True, error=_("sin conexión"))
                raise Offline(_("No hay conexión con GOG; la sincronización queda pendiente"))
            try:
                locations = self.locations(game)
                for loc in locations:
                    self.log(_("ubicación «{name}»: {path}").format(name=loc.name, path=loc.path))
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

    def mark_pending(self, game_id: str, reason: str | None = None) -> None:
        self.state.set_status(game_id, pending=True, error=reason or _("partida sin subir"))

    def sync_pending(self, **kw) -> dict[str, Exception | SyncResult]:
        out: dict[str, Exception | SyncResult] = {}
        for gid in self.state.pending_games():
            try:
                out[gid] = self.sync(gid, **kw)
            except Exception as e:  # noqa: BLE001 - se informa por juego
                out[gid] = e
        return out


ACTION_LABELS = {
    Action.ADOPT: _("igualar"), Action.UPLOAD: _("subir"), Action.DOWNLOAD: _("descargar"),
    Action.DELETE_CLOUD: _("borrar nube"), Action.DELETE_LOCAL: _("borrar local"),
    Action.DROP_BASE: _("olvidar"), Action.CONFLICT: _("conflicto"),
}


def describe(res: SyncResult) -> str:
    if res.dry_run:
        lines = [f"  {ACTION_LABELS.get(it.action, it.action.value):13} {it.key}  ({it.reason})"
                 for it in res.plan.changes]
        return "\n".join(lines) or "  " + _("(nada que hacer)")
    parts = []
    for label, lst in ((_("{n} subidos"), res.uploaded), (_("{n} descargados"), res.downloaded),
                       (_("{n} borrados en la nube"), res.deleted_cloud),
                       (_("{n} borrados en local"), res.deleted_local)):
        if lst:
            parts.append(label.format(n=len(lst)))
    return ", ".join(parts) or _("ya estaba sincronizado")


__all__ = ["SyncService", "SyncUnavailable", "Offline", "ConflictError", "describe"]
