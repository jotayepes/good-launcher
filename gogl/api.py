"""Acceso a la API pública/autenticada de GOG (builds, manifiestos, remote-config)."""

from __future__ import annotations

import json
import zlib

import httpx

from .auth import AuthManager
from .i18n import _

CONTENT_SYSTEM = "https://content-system.gog.com"
REMOTE_CONFIG = "https://remote-config.gog.com/components/galaxy_client/clients/{client_id}?component_version=2.0.45"


class ApiError(Exception):
    pass


def decode_meta(raw: bytes) -> dict:
    """Los manifiestos v2 van comprimidos con zlib; algunos endpoints devuelven JSON plano."""
    try:
        raw = zlib.decompress(raw)
    except zlib.error:
        pass
    return json.loads(raw)


class GogApi:
    def __init__(self, auth: AuthManager, http: httpx.Client | None = None):
        self.auth = auth
        self.http = http or httpx.Client(timeout=30, follow_redirects=True)

    def _get(self, url: str, **kw) -> httpx.Response:
        creds = self.auth.credentials()
        r = self.http.get(url, headers={"Authorization": f"Bearer {creds.access_token}"}, **kw)
        if r.status_code == 401:
            creds = self.auth.credentials(force_refresh=True)
            r = self.http.get(url, headers={"Authorization": f"Bearer {creds.access_token}"}, **kw)
        if r.status_code != 200:
            raise ApiError(f"GET {url} → {r.status_code}")
        return r

    def builds(self, product_id: str, platform: str = "windows", generation: int = 2) -> list[dict]:
        r = self._get(f"{CONTENT_SYSTEM}/products/{product_id}/os/{platform}/builds",
                      params={"generation": generation})
        return r.json().get("items", [])

    def build_meta(self, build: dict) -> dict:
        return decode_meta(self._get(build["link"]).content)

    def client_credentials(self, product_id: str, platform: str = "windows") -> tuple[str, str]:
        """clientId y clientSecret del juego, sacados del manifiesto v2 más reciente."""
        builds = self.builds(product_id, platform)
        if not builds:
            raise ApiError(_("El producto {id} no tiene builds v2 para {platform}").format(id=product_id, platform=platform))
        meta = self.build_meta(builds[0])
        cid, secret = meta.get("clientId"), meta.get("clientSecret")
        if not cid or not secret:
            raise ApiError(_("El manifiesto de {id} no trae clientId/clientSecret").format(id=product_id))
        return str(cid), str(secret)

    def remote_config(self, client_id: str) -> dict:
        r = self.http.get(REMOTE_CONFIG.format(client_id=client_id))
        if r.status_code != 200:
            raise ApiError(_("remote-config de {id} → {code}").format(id=client_id, code=r.status_code))
        return r.json()
