"""Perfil de GOG: datos de la cuenta, lista de deseos con precios y resumen de logros.

Las estadísticas de horas de gog.com/u/<usuario> solo responden con la sesión de
la web, así que esas se ven en el perfil web embebido; aquí está lo que da la API
con el token del lanzador."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from .api import GogApi
from .state import State

USERS = "https://users.gog.com/users/{id}"
USER_DATA = "https://embed.gog.com/userData.json"
WISHLIST = "https://embed.gog.com/user/wishlist.json"
PRODUCTS = "https://api.gog.com/products"
PRICES = "https://api.gog.com/products/prices"

MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
          "octubre", "noviembre", "diciembre"]


@dataclass
class WishItem:
    id: str
    title: str
    url: str                   # ruta en gog.com (/game/slug)
    image: str | None = None
    base: int | None = None    # céntimos
    final: int | None = None
    currency: str = ""

    @property
    def discount(self) -> int:
        if self.base and self.final is not None and self.final < self.base:
            return round(100 * (1 - self.final / self.base))
        return 0


@dataclass
class Profile:
    user_id: str
    username: str
    avatar: str | None
    since: str                 # «abril de 2011»
    country: str = ""
    currency: str = ""
    wishlist: list[WishItem] = field(default_factory=list)


def member_since(iso: str) -> str:
    try:
        d = datetime.fromisoformat(iso)
        return f"{MONTHS[d.month - 1]} de {d.year}"
    except (ValueError, TypeError):
        return ""


def price(cents: int | None, currency: str) -> str:
    if cents is None:
        return ""
    if cents == 0:
        return "Gratis"
    value = f"{cents / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{value} €" if currency == "EUR" else f"{value} {currency}"


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fetch(api: GogApi) -> Profile:
    creds = api.auth.credentials()
    u = api._get(USERS.format(id=creds.user_id)).json()
    ud = api._get(USER_DATA).json()
    avatar = (u.get("avatar") or {})
    p = Profile(str(u.get("id") or creds.user_id), u.get("username") or ud.get("username") or "",
                avatar.get("large_2x") or avatar.get("large") or avatar.get("medium_2x"),
                member_since(u.get("created_date") or ""), ud.get("country") or "",
                (ud.get("selectedCurrency") or {}).get("code") or "EUR")
    ids = [k for k, v in (api._get(WISHLIST).json().get("wishlist") or {}).items() if v]
    p.wishlist = wishlist_items(api, ids, p.country, p.currency)
    return p


def wishlist_items(api: GogApi, ids: list[str], country: str, currency: str) -> list[WishItem]:
    items: dict[str, WishItem] = {}
    for chunk in _chunks(ids, 50):
        r = api.http.get(PRODUCTS, params={"ids": ",".join(chunk)})
        if r.status_code != 200:
            continue
        for d in r.json():
            img = (d.get("images") or {}).get("logo2x") or (d.get("images") or {}).get("logo")
            items[str(d["id"])] = WishItem(str(d["id"]), d.get("title") or "", f"/game/{d.get('slug')}",
                                           "https:" + img if img and img.startswith("//") else img)
        r = api.http.get(PRICES, params={"ids": ",".join(chunk), "countryCode": country or "ES",
                                         "currency": currency or "EUR"})
        if r.status_code != 200:
            continue
        for it in (r.json().get("_embedded") or {}).get("items") or []:
            emb = it.get("_embedded") or {}
            pid = str((emb.get("product") or {}).get("id"))
            prices = emb.get("prices") or []
            if pid in items and prices:
                pr = prices[0]
                items[pid].base = _cents(pr.get("basePrice"))
                items[pid].final = _cents(pr.get("finalPrice"))
                items[pid].currency = (pr.get("currency") or {}).get("code") or currency
    out = [items[i] for i in ids if i in items]
    return sorted(out, key=lambda w: (-w.discount, w.title.lower()))


def _cents(s: str | None) -> int | None:
    try:
        return int(str(s).split()[0])
    except (ValueError, IndexError):
        return None


@dataclass
class AchievementStats:
    unlocked: int
    total: int
    games: int                  # juegos con logros consultados
    perfect: int                # juegos al 100 %
    recent: list[tuple[str, object]]   # (game_id, Achievement), los últimos conseguidos


def achievement_stats(state: State, limit: int = 8) -> AchievementStats:
    from .achievements import parse
    unlocked = total = games = perfect = 0
    recent = []
    for gid in state.achievement_summary():
        items = parse(state.achievements(gid)[0])
        got = [a for a in items if a.unlocked]
        games += 1
        unlocked += len(got)
        total += len(items)
        perfect += len(got) == len(items)
        recent += [(gid, a) for a in got]
    recent.sort(key=lambda t: -(t[1].unlocked_at or 0))
    return AchievementStats(unlocked, total, games, perfect, recent[:limit])


def refresh_all_achievements(api: GogApi, state: State, game_ids: list[str], progress=lambda i, n: None,
                             max_age: float = 7 * 24 * 3600) -> int:
    """Consulta los logros de toda la biblioteca (lo ya consultado hace poco se salta)."""
    from . import achievements
    done = 0
    for i, gid in enumerate(game_ids):
        progress(i, len(game_ids))
        cached = state.achievements(gid)
        if cached and time.time() - cached[1] < max_age:
            continue
        try:
            achievements.fetch(api, api.auth, state, gid, offline_ok=False)
            done += 1
        except Exception:  # noqa: BLE001 - un juego que falla no para el resto
            continue
    progress(len(game_ids), len(game_ids))
    return done


__all__ = ["AchievementStats", "Profile", "WishItem", "achievement_stats", "fetch", "price",
           "refresh_all_achievements"]
