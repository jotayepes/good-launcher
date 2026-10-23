"""Biblioteca de juegos comprados y metadatos (carátulas, fondo, descripción)."""

from __future__ import annotations

import html
import json
import re
import time
from pathlib import Path

import httpx

from . import i18n, languages, paths
from .i18n import _
from .api import ApiError, GogApi

FILTERED_PRODUCTS = "https://embed.gog.com/account/getFilteredProducts"
USER_DATA = "https://embed.gog.com/userData.json"
GAMESDB = "https://gamesdb.gog.com/platforms/gog/external_releases/{id}"
PRODUCT = "https://api.gog.com/products/{id}"

DETAILS_TTL = 7 * 24 * 3600


def fetch_owned(api: GogApi) -> list[dict]:
    """Todos los juegos de la cuenta (sin películas). Pagina de 100 en 100."""
    products: list[dict] = []
    page, total = 1, 1
    while page <= total:
        r = api._get(FILTERED_PRODUCTS, params={"mediaType": 1, "sortBy": "title", "page": page})
        try:
            data = r.json()
        except ValueError:
            raise ApiError(_("GOG devolvió una respuesta no válida (¿sesión caducada?)")) from None
        total = int(data.get("totalPages", 1))
        products += [p for p in data.get("products", []) if not p.get("isMovie")]
        page += 1
    return products


def fetch_user(api: GogApi) -> dict:
    return api._get(USER_DATA).json()


def cached_username(api: GogApi | None = None) -> str | None:
    """Nombre de usuario de GOG (de la caché; si no está y hay api, se pide)."""
    p = paths.cache_dir() / "user.json"
    try:
        name = json.loads(p.read_text()).get("username")
        if name:
            return name
    except (OSError, ValueError):
        pass
    if api is None:
        return None
    name = fetch_user(api).get("username")
    if name:
        p.write_text(json.dumps({"username": name}))
    return name


def _url(u: str | None) -> str | None:
    if not u:
        return None
    return "https:" + u if u.startswith("//") else u


def _strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>|</p>|</li>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def _localized(v) -> str:
    if isinstance(v, dict):
        return v.get("es-ES" if i18n.current == "es" else "en-US") or v.get("*") or next(iter(v.values()), "")
    return v or ""


def product_details(game_id: str, http: httpx.Client | None = None) -> dict:
    """Metadatos de presentación cacheados en disco una semana. No requiere sesión."""
    cache = paths.cache_dir() / "details" / i18n.current / f"{game_id}.json"
    try:
        if time.time() - cache.stat().st_mtime < DETAILS_TTL:
            cached = json.loads(cache.read_text())
            if "languages" in cached:
                return cached
    except (OSError, ValueError):
        pass
    http = http or httpx.Client(timeout=20, follow_redirects=True)
    out: dict = {"id": game_id}
    try:
        g = http.get(GAMESDB.format(id=game_id)).json()
        game = g.get("game") or {}
        cover = (game.get("vertical_cover") or {}).get("url_format")
        if cover:
            out["cover"] = cover.format(formatter="_glx_vertical_cover", ext="jpg")
        out["summary"] = _strip_html(_localized(g.get("summary") or game.get("summary")))
        out["developers"] = [d.get("name") for d in game.get("developers", []) if d.get("name")]
        out["genres"] = [_localized(x.get("name")) for x in game.get("genres", [])]
        out["release"] = (g.get("first_release_date") or "")[:10]
    except (httpx.HTTPError, ValueError):
        pass
    try:
        p = http.get(PRODUCT.format(id=game_id), params={"expand": "description"}).json()
        imgs = p.get("images") or {}
        out["background"] = _url(imgs.get("background"))
        out["logo"] = _url(imgs.get("logo2x") or imgs.get("logo"))
        out["icon"] = _url(imgs.get("icon"))
        out["languages"] = languages.from_store(p.get("languages"))
        if not out.get("summary"):
            out["summary"] = _strip_html((p.get("description") or {}).get("lead") or "")
    except (httpx.HTTPError, ValueError):
        pass
    if len(out) > 1:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out))
    return out


def cover_path(game_id: str) -> Path:
    return paths.cache_dir() / "covers" / f"{game_id}.jpg"
