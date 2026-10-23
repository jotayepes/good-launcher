"""Cliente de cloudstorage.gog.com.

Los objetos se guardan comprimidos con gzip; el `hash` del listado es el MD5 del
objeto almacenado. Nosotros comprimimos de forma determinista (nivel 6, mtime=0)
igual que Galaxy, pero NO asumimos que el hash de un archivo subido desde
Windows coincida con nuestra compresión: el motor guarda por separado el MD5 del
contenido local y el hash de la nube.
"""

from __future__ import annotations

import gzip
import json
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import quote

import httpx

from ..auth import Credentials

CLOUD_URL = "https://cloudstorage.gog.com/v1"
USER_AGENT = ("GOGGalaxyCommunicationService/2.0.13.27 (Windows_32bit) "
              "dont_sync_marker/true installation_source/gog")
# Hash que aparece en entradas sin contenido real; se tratan como inexistentes.
EMPTY_MARKER_HASH = "aadd86936a80ee8a369579c3926f1b3c"

RETRY_STATUS = {408, 429, 500, 502, 503, 504}


class CloudError(Exception):
    pass


@dataclass(frozen=True)
class Response:
    status_code: int
    headers: httpx.Headers
    body: bytes


@dataclass(frozen=True)
class CloudFile:
    key: str            # "<location>/<ruta/relativa>"
    hash: str
    last_modified: str
    size: int | None = None


def gzip_bytes(data: bytes) -> bytes:
    return gzip.compress(data, 6, mtime=0)


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


class CloudStorage:
    def __init__(self, client_id: str, get_credentials: Callable[[bool], Credentials],
                 http: httpx.Client | None = None, retries: int = 4, backoff: float = 1.0):
        self.client_id = client_id
        self._get_credentials = get_credentials
        self.http = http or httpx.Client(timeout=httpx.Timeout(60, connect=15))
        self.retries = retries
        self.backoff = backoff
        self._creds: Credentials | None = None

    # --- bajo nivel -----------------------------------------------------

    def _base(self) -> str:
        return f"{CLOUD_URL}/{self._creds.user_id}/{self.client_id}"

    def _request(self, method: str, key: str | None = None, *, headers=None, content=None,
                 ok=(200, 201, 204), raw: bool = False) -> "Response":
        """Petición con reintentos y refresco de token en 401.

        Con raw=True el cuerpo se devuelve tal cual llega (sin descomprimir), para
        poder verificar el MD5 del objeto almacenado."""
        if self._creds is None:
            self._creds = self._get_credentials(False)
        refreshed = False
        attempt = 0
        while True:
            url = self._base() + (f"/{quote(key)}" if key else "")
            h = {
                "User-Agent": USER_AGENT,
                "X-Object-Meta-User-Agent": USER_AGENT,
                "Authorization": f"Bearer {self._creds.access_token}",
                **(headers or {}),
            }
            try:
                r = self.http.send(self.http.build_request(method, url, headers=h, content=content),
                                   stream=True)
                try:
                    body = b"".join(r.iter_raw() if raw else r.iter_bytes())
                finally:
                    r.close()
            except httpx.TransportError as e:
                if attempt >= self.retries:
                    raise CloudError(f"{method} {key or '/'}: error de red: {e}") from e
                attempt += 1
                time.sleep(self.backoff * 2 ** (attempt - 1))
                continue
            if r.status_code == 401 and not refreshed:
                refreshed = True
                self._creds = self._get_credentials(True)
                continue
            if r.status_code in RETRY_STATUS and attempt < self.retries:
                attempt += 1
                time.sleep(self.backoff * 2 ** (attempt - 1))
                continue
            if r.status_code not in ok:
                text = body[:200].decode(errors="replace")
                raise CloudError(f"{method} {key or '/'} → HTTP {r.status_code}: {text}")
            return Response(r.status_code, r.headers, body)

    # --- operaciones ----------------------------------------------------

    def list(self) -> dict[str, CloudFile]:
        r = self._request("GET", headers={"Accept": "application/json"}, ok=(200, 404))
        if r.status_code == 404:
            return {}
        out = {}
        for item in json.loads(r.body or b"[]"):
            h = item.get("hash")
            if not h or h == EMPTY_MARKER_HASH:
                continue
            out[item["name"]] = CloudFile(item["name"], h, item.get("last_modified", ""),
                                          item.get("bytes"))
        return out

    def download(self, cf: CloudFile) -> tuple[bytes, float | None]:
        """Devuelve (contenido descomprimido, mtime original) verificando el hash."""
        r = self._request("GET", cf.key, headers={"Accept-Encoding": "gzip"}, ok=(200,), raw=True)
        if md5(r.body) != cf.hash:
            raise CloudError(f"Descarga corrupta de {cf.key}: md5 {md5(r.body)} ≠ {cf.hash}")
        data = r.body
        declared_gzip = r.headers.get("content-encoding", "").lower() == "gzip"
        if declared_gzip or data[:2] == b"\x1f\x8b":
            try:
                data = gzip.decompress(data)
            except (OSError, EOFError):
                if declared_gzip:
                    raise CloudError(f"{cf.key}: el gzip descargado no es válido")
        mtime = parse_ts(r.headers.get("x-object-meta-locallastmodified")) or parse_ts(cf.last_modified)
        return data, mtime

    def upload(self, key: str, data: bytes, mtime: float) -> str:
        """Sube el archivo y devuelve el hash que debería aparecer en el listado."""
        body = gzip_bytes(data)
        h = md5(body)
        self._request("PUT", key, content=body, headers={
            "Etag": h,
            "Content-Encoding": "gzip",
            "Content-Type": "application/octet-stream",
            "X-Object-Meta-LocalLastModified": iso_utc(mtime),
        })
        return h

    def delete(self, key: str) -> None:
        self._request("DELETE", key, ok=(200, 204, 404))
