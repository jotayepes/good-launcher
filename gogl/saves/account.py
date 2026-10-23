"""Partidas en la nube de toda la cuenta: lo que muestra gog.com/account/cloud-saves.

La API (cloudstorage.gog.com/v2) agrupa por «contenedor» (el espacio de un
juego; algunos juegos comparten uno), con su tamaño y cuota, y dentro agrupa los
archivos por partida guardada. Acepta el mismo token de sesión que el resto de
gogl, sin credenciales de cada juego.

Ojo con el sync: borrar aquí una partida de un juego vinculado hace que la
siguiente sincronización la quite también de este equipo (con copia de
seguridad antes), igual que si se hubiera borrado desde otro PC."""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from ..auth import AuthManager
from ..i18n import _
from .cloud import CloudError, parse_ts

API = "https://cloudstorage.gog.com/v2/users/{user}/containers"
GAMES_API = "https://api.gog.com/v2/games"
PAGE = 50


@dataclass
class Container:
    space_id: str
    game_ids: list[str]
    size: int
    files: int
    quota: int
    titles: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return " / ".join(self.titles) or ", ".join(self.game_ids)

    @property
    def usage(self) -> float:
        return self.size / self.quota if self.quota else 0.0


@dataclass
class SaveGroup:
    """Una partida guardada (o el grupo de archivos que GOG no reconoce como del juego)."""
    name: str
    kind: str          # savegame | nogame | …
    size: int
    timestamp: float | None
    files: list[str]

    @property
    def unrelated(self) -> bool:
        return self.kind == "nogame"


class AccountCloud:
    def __init__(self, auth: AuthManager, http: httpx.Client | None = None):
        self.auth = auth
        self.http = http or httpx.Client(timeout=httpx.Timeout(60, connect=15))

    def _req(self, method: str, path: str = "", ok=(200, 204), **kw) -> dict:
        for force in (False, True):
            creds = self.auth.credentials(force_refresh=force)
            url = API.format(user=creds.user_id) + path
            try:
                r = self.http.request(method, url, headers={"Authorization": f"Bearer {creds.access_token}",
                                                            "Accept": "application/json"}, **kw)
            except httpx.HTTPError as e:
                raise CloudError(_("Sin conexión con la nube de GOG: {error}").format(error=e)) from e
            if r.status_code == 401 and not force:
                continue
            if r.status_code not in ok:
                raise CloudError(_("Nube de GOG → HTTP {code}: {text}").format(code=r.status_code, text=r.text[:200]))
            return r.json() if r.content and r.status_code in (200, 204) else {}
        raise CloudError(_("La nube de GOG rechazó la sesión"))

    def _pages(self, path: str) -> list[dict]:
        items, page = [], 1
        while True:
            d = self._req("GET", path, params={"page": page, "limit": PAGE})
            items += d.get("items") or []
            if page >= int((d.get("pagination") or {}).get("pages") or 1):
                return items
            page += 1

    def containers(self, titles: dict[str, str] | None = None) -> list[Container]:
        """Juegos con partidas en la nube, de más a menos espacio usado."""
        out = []
        for it in self._pages(""):
            c = it.get("container") or {}
            games = it.get("games") or []
            out.append(Container(str(c.get("space_id")), [str(g["id"]) for g in games],
                                 int(c.get("size") or 0), int(c.get("files") or 0),
                                 int(c.get("quota") or (games[0].get("quota") if games else 0) or 0)))
        self._fill_titles(out, titles or {})
        return sorted(out, key=lambda c: c.size, reverse=True)

    def _fill_titles(self, containers: list[Container], known: dict[str, str]) -> None:
        missing = sorted({g for c in containers for g in c.game_ids if g not in known})
        names = dict(known)
        for i in range(0, len(missing), 50):
            try:
                r = self.http.get(GAMES_API, params={"productIds": ",".join(missing[i:i + 50])})
                for p in (r.json().get("_embedded") or {}).get("items", []) if r.status_code == 200 else []:
                    prod = (p.get("_embedded") or {}).get("product") or {}
                    if prod.get("id"):
                        names[str(prod["id"])] = prod.get("title") or str(prod["id"])
            except (httpx.HTTPError, ValueError):
                pass  # sin título: se muestra el id
        for c in containers:
            c.titles = list(dict.fromkeys(names[g] for g in c.game_ids if g in names))

    def saves(self, space_id: str) -> list[SaveGroup]:
        """Partidas de un juego, de la más reciente a la más antigua."""
        out = [SaveGroup(it.get("name", ""), it.get("type", ""), int(it.get("size") or 0),
                         parse_ts(it.get("timestamp")), list(it.get("files") or []))
               for it in self._pages(f"/{space_id}")]
        return sorted(out, key=lambda s: (not s.unrelated, s.timestamp or 0), reverse=True)

    def auto_clean_preview(self, space_id: str) -> list[SaveGroup]:
        """Lo que borraría la limpieza automática de GOG (archivos que no reconoce
        como partidas y, si se pasa de la cuota, las partidas más antiguas)."""
        # 409: GOG no propone nada que limpiar en este juego.
        d = self._req("GET", f"/{space_id}/auto-clean", ok=(200, 204, 409))
        return [SaveGroup(it.get("name", ""), it.get("type", ""), int(it.get("size") or 0),
                          parse_ts(it.get("timestamp")), list(it.get("files") or []))
                for it in d.get("items") or []]

    def delete_files(self, space_id: str, files: list[str]) -> None:
        if files:
            self._req("POST", f"/{space_id}", json={"delete_files": files})

    def auto_clean(self, space_id: str) -> None:
        self._req("DELETE", f"/{space_id}", params={"operation": "auto"})

    def delete_all(self, space_id: str) -> None:
        self._req("DELETE", f"/{space_id}", params={"operation": "all"})
