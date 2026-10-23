"""Logros de GOG (gameplay.gog.com).

La lista se pide con el token del propio juego (su clientId/clientSecret, los
mismos que usa la nube), así que también funciona con juegos no instalados: las
credenciales se sacan del manifiesto de la build más reciente y se guardan.

Cada consulta se guarda en state.db: la ficha los muestra sin conexión y la
sesión de juego compara con esa copia para saber qué logros son nuevos.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

import httpx

from .api import ApiError, GogApi
from .auth import AuthManager
from .i18n import _
from .state import State

GAMEPLAY = "https://gameplay.gog.com/clients/{client_id}/users/{user_id}/achievements"
# Un producto sin credenciales (sin build v2) se vuelve a comprobar pasado este tiempo.
NO_CLIENT_TTL = 30 * 24 * 3600


@dataclass
class Achievement:
    id: str
    key: str
    name: str
    description: str
    visible: bool
    image_locked: str
    image_unlocked: str
    unlocked_at: float | None
    rarity: float
    rarity_label: str

    @property
    def unlocked(self) -> bool:
        return self.unlocked_at is not None

    @property
    def image(self) -> str:
        return self.image_unlocked if self.unlocked else self.image_locked


def _ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse(items: list[dict]) -> list[Achievement]:
    return [Achievement(
        id=str(i.get("achievement_id", "")), key=i.get("achievement_key", ""),
        name=i.get("name") or i.get("achievement_key", ""), description=i.get("description") or "",
        visible=bool(i.get("visible", True)), image_locked=i.get("image_url_locked") or "",
        image_unlocked=i.get("image_url_unlocked") or "", unlocked_at=_ts(i.get("date_unlocked")),
        rarity=float(i.get("rarity") or 0), rarity_label=i.get("rarity_level_description") or "")
        for i in items]


def credentials(api: GogApi, state: State, game_id: str) -> tuple[str, str] | None:
    """clientId/clientSecret del producto, o None si no tiene (juegos antiguos sin Galaxy)."""
    g = state.game(game_id)
    if g and g.client_id and g.client_secret:
        return g.client_id, g.client_secret
    cached = state.client(game_id)
    if cached:
        cid, secret, fetched = cached
        if cid and secret:
            return cid, secret
        if time.time() - fetched < NO_CLIENT_TTL:
            return None
    for platform in ("windows", "osx"):
        try:
            cid, secret = api.client_credentials(game_id, platform)
            state.set_client(game_id, cid, secret)
            return cid, secret
        except ApiError:
            continue
    state.set_client(game_id, None, None)
    return None


def fetch_raw(api: GogApi, auth: AuthManager, client_id: str, client_secret: str) -> list[dict]:
    creds = auth.credentials(client_id, client_secret)
    url = GAMEPLAY.format(client_id=client_id, user_id=creds.user_id)
    items: list[dict] = []
    params: dict = {}
    retried = False
    while True:
        r = api.http.get(url, params=params, headers={"Authorization": f"Bearer {creds.access_token}"})
        if r.status_code == 401 and not retried:
            retried = True
            creds = auth.credentials(client_id, client_secret, force_refresh=True)
            continue
        if r.status_code != 200:
            raise ApiError(_("logros: gameplay.gog.com respondió {code}").format(code=r.status_code))
        data = r.json()
        items += data.get("items") or []
        token = data.get("page_token")
        if not token or len(items) >= int(data.get("total_count") or 0):
            return items
        params = {"page_token": token}


def fetch(api: GogApi, auth: AuthManager, state: State, game_id: str,
          offline_ok: bool = True) -> list[Achievement]:
    """Logros actuales (y los guarda). Sin conexión devuelve la última copia conocida."""
    try:
        creds = credentials(api, state, game_id)
        if creds is None:
            state.set_achievements(game_id, [])
            return []
        items = fetch_raw(api, auth, *creds)
    except (httpx.HTTPError, ApiError):
        cached = state.achievements(game_id)
        if offline_ok and cached:
            return parse(cached[0])
        raise
    state.set_achievements(game_id, items)
    return parse(items)


def cached(state: State, game_id: str) -> list[Achievement] | None:
    c = state.achievements(game_id)
    return parse(c[0]) if c else None


def newly_unlocked(before: list[Achievement], after: list[Achievement]) -> list[Achievement]:
    was = {a.id for a in before if a.unlocked}
    return [a for a in after if a.unlocked and a.id not in was]


__all__ = ["Achievement", "cached", "credentials", "fetch", "newly_unlocked", "parse"]
