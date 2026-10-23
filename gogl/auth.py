"""Autenticación con GOG (OAuth del cliente Galaxy) y tokens por juego.

Los refresh tokens de GOG rotan: si dos procesos refrescan a la vez (p. ej. el
wrapper del juego y el vigilante post-sync), uno de ellos invalida al otro y el
usuario queda deslogueado. Por eso todo refresco ocurre bajo un flock.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from . import paths
from .i18n import _

GALAXY_CLIENT_ID = "46899977096215655"
GALAXY_CLIENT_SECRET = "9d85c43b1482497dbbce61f6e4aa173a433796eeae2ca8c5f6129f2dc4de46d9"
REDIRECT_URI = "https://embed.gog.com/on_login_success?origin=client"
AUTH_URL = "https://auth.gog.com"

# Margen para refrescar antes de que caduque de verdad.
EXPIRY_MARGIN = 120


class AuthError(Exception):
    pass


@dataclass
class Credentials:
    access_token: str
    user_id: str
    expires_at: float


def login_url() -> str:
    q = urlencode({
        "client_id": GALAXY_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "layout": "client2",
    })
    return f"{AUTH_URL}/auth?{q}"


def extract_code(code_or_url: str) -> str:
    """Acepta el código a pelo o la URL completa de on_login_success."""
    s = code_or_url.strip()
    if "://" in s:
        codes = parse_qs(urlparse(s).query).get("code")
        if not codes:
            raise AuthError(_("La URL no contiene ningún parámetro 'code'"))
        return codes[0]
    return s


class AuthManager:
    def __init__(self, store: Path | None = None, http: httpx.Client | None = None):
        self.store = store or paths.config_dir() / "auth.json"
        self.lock_path = self.store.with_suffix(".lock")
        self.http = http or httpx.Client(timeout=30)

    # --- almacenamiento -------------------------------------------------

    @contextmanager
    def _locked(self):
        with open(self.lock_path, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _load(self) -> dict:
        try:
            return json.loads(self.store.read_text())
        except FileNotFoundError:
            return {}
        except ValueError:
            return {}  # dañado (p. ej. un corte de luz): hay que volver a iniciar sesión

    def _save(self, data: dict) -> None:
        tmp = self.store.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.store)

    def is_logged_in(self) -> bool:
        return "galaxy" in self._load()

    def logout(self) -> None:
        with self._locked():
            self.store.unlink(missing_ok=True)

    # --- peticiones -----------------------------------------------------

    def _token_request(self, params: dict) -> dict:
        r = self.http.get(f"{AUTH_URL}/token", params=params)
        if r.status_code != 200:
            raise AuthError(_("auth.gog.com respondió {code}: {text}").format(code=r.status_code, text=r.text[:200]))
        tok = r.json()
        if "access_token" not in tok:
            raise AuthError(_("Respuesta de token inesperada: {response}").format(response=tok))
        tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600))
        return tok

    def login_with_code(self, code_or_url: str) -> str:
        tok = self._token_request({
            "client_id": GALAXY_CLIENT_ID,
            "client_secret": GALAXY_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": extract_code(code_or_url),
            "redirect_uri": REDIRECT_URI,
        })
        with self._locked():
            self._save({"galaxy": tok, "clients": {}})
        return str(tok["user_id"])

    def _galaxy_locked(self, data: dict) -> dict:
        """Devuelve el token de Galaxy vigente; refresca si hace falta. Requiere lock."""
        g = data.get("galaxy")
        if not g:
            raise AuthError(_("No has iniciado sesión. Ejecuta: gogl login"))
        if g["expires_at"] - EXPIRY_MARGIN > time.time():
            return g
        new = self._token_request({
            "client_id": GALAXY_CLIENT_ID,
            "client_secret": GALAXY_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": g["refresh_token"],
        })
        new.setdefault("user_id", g["user_id"])
        data["galaxy"] = new
        self._save(data)
        return new

    def galaxy_tokens(self) -> dict:
        """access_token, refresh_token y user_id vigentes (para Comet). Comet solo
        usa el refresh token con without_new_session=1, que no lo invalida."""
        with self._locked():
            g = self._galaxy_locked(self._load())
            return {k: str(g[k]) for k in ("access_token", "refresh_token", "user_id")}

    def credentials(self, client_id: str | None = None, client_secret: str | None = None,
                    force_refresh: bool = False) -> Credentials:
        """Token de Galaxy, o el token de almacenamiento de un juego concreto."""
        with self._locked():
            data = self._load()
            if force_refresh and client_id is None and "galaxy" in data:
                data["galaxy"]["expires_at"] = 0
            g = self._galaxy_locked(data)
            if client_id is None or client_id == GALAXY_CLIENT_ID:
                return Credentials(g["access_token"], str(g["user_id"]), g["expires_at"])

            clients = data.setdefault("clients", {})
            c = clients.get(client_id)
            if c and not force_refresh and c["expires_at"] - EXPIRY_MARGIN > time.time():
                return Credentials(c["access_token"], str(c.get("user_id", g["user_id"])), c["expires_at"])
            if not client_secret:
                raise AuthError(_("Falta el clientSecret del juego {id}").format(id=client_id))
            c = self._token_request({
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": g["refresh_token"],
                "without_new_session": "1",
            })
            c.setdefault("user_id", g["user_id"])
            # El refresh token de Galaxy no se toca: este token es derivado.
            c.pop("refresh_token", None)
            clients[client_id] = c
            self._save(data)
            return Credentials(c["access_token"], str(c["user_id"]), c["expires_at"])
