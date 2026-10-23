"""Extras («goodies») e instaladores offline de la cuenta.

GOG los da en `api.gog.com/products/{id}?expand=downloads`: cada paquete tiene
uno o varios archivos con un `downlink` que, pedido con el token, devuelve la URL
firmada del CDN (caduca) y un XML con el MD5 de cada trozo de 10 MB. Así:

- se descarga a «<nombre>.gogl-part» con peticiones Range, y un corte o una
  cancelación se reanudan desde donde se quedaron;
- al terminar se comprueban los trozos y solo se repiten los que fallen;
- si la URL firmada caduca a mitad, se pide otra.

Algunos extras no tienen XML: en ese caso solo se comprueba el tamaño.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

import httpx

from .. import config
from ..api import ApiError, GogApi
from .common import ContentError, safe_name
from .installer import Cancelled, Progress

PRODUCT = "https://api.gog.com/products/{id}"
PART = ".gogl-part"
BLOCK = 1024 * 1024
RETRIES = 5

EXTRA_TYPES = {
    "manuals": "Manual", "artworks": "Arte", "wallpapers": "Fondos de pantalla",
    "audio": "Banda sonora", "soundtrack": "Banda sonora", "video": "Vídeo",
    "avatars": "Avatares", "guides & reference": "Guía", "game add-ons": "Contenido adicional",
    "e-books": "Libro", "images": "Imágenes", "maps": "Mapas", "other": "Otros",
}


@dataclass
class OfflineFile:
    id: str
    size: int
    downlink: str


@dataclass
class Bundle:
    """Un paquete descargable: un instalador (con sus partes) o un extra."""
    kind: str                 # installer | extra
    id: str
    product_id: str
    product_title: str
    name: str
    total_size: int
    files: list[OfflineFile]
    os: str = ""              # instaladores: windows | linux | mac
    language: str = ""
    language_name: str = ""
    version: str = ""
    type: str = ""            # extras: manuals, wallpapers…

    @property
    def key(self) -> str:
        return f"{self.product_id}:{self.kind}:{self.id}"

    @property
    def type_label(self) -> str:
        return EXTRA_TYPES.get(self.type.lower(), self.type.capitalize() or "Extra")


@dataclass
class Offline:
    installers: list[Bundle] = field(default_factory=list)
    extras: list[Bundle] = field(default_factory=list)


def _bundles(product: dict, kind: str, section: str) -> list[Bundle]:
    out = []
    for d in (product.get("downloads") or {}).get(section) or []:
        files = [OfflineFile(str(f["id"]), int(f.get("size") or 0), f["downlink"])
                 for f in d.get("files") or [] if f.get("downlink")]
        if not files:
            continue
        out.append(Bundle(
            kind=kind, id=str(d.get("id")), product_id=str(product["id"]),
            product_title=product.get("title") or "", name=d.get("name") or "",
            total_size=int(d.get("total_size") or sum(f.size for f in files)), files=files,
            os=d.get("os") or "", language=d.get("language") or "",
            language_name=d.get("language_full") or "", version=d.get("version") or "",
            type=d.get("type") or ""))
    return out


def list_downloads(api: GogApi, game_id: str) -> Offline:
    """Instaladores (del juego y de sus DLC poseídos) y extras."""
    p = api._get(PRODUCT.format(id=game_id), params={"expand": "downloads,expanded_dlcs"}).json()
    res = Offline(_bundles(p, "installer", "installers"), _bundles(p, "extra", "bonus_content"))
    for dlc in p.get("expanded_dlcs") or []:
        # Sin instaladores = DLC no comprado (la API los lista igualmente).
        res.installers += _bundles(dlc, "installer", "installers")
        res.extras += _bundles(dlc, "extra", "bonus_content")
    return res


# --- carpetas ---------------------------------------------------------------------

def _xdg_download_dir() -> Path:
    try:
        out = subprocess.run(["xdg-user-dir", "DOWNLOAD"], capture_output=True, text=True,
                             timeout=5).stdout.strip()
        if out and Path(out) != Path.home():
            return Path(out)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return Path.home() / "Downloads"


def downloads_root() -> Path:
    custom = config.load().get("offline_root")
    return Path(custom).expanduser() if custom else _xdg_download_dir() / "GOG"


def bundle_dir(b: Bundle, root: Path | None = None) -> Path:
    base = (root or downloads_root()) / safe_name(b.product_title)
    if b.kind == "extra":
        return base / "Extras"
    sub = "-".join(safe_name(x) for x in (b.os, b.language, b.version) if x)
    return base / "Instaladores" / sub


# --- descarga ---------------------------------------------------------------------

@dataclass
class Chunk:
    start: int
    end: int       # inclusivo
    md5: str


@dataclass
class Checksum:
    name: str
    size: int
    md5: str
    chunks: list[Chunk]


def parse_checksum(xml: str) -> Checksum | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    if root.tag != "file":
        return None
    chunks = [Chunk(int(c.get("from")), int(c.get("to")), (c.text or "").strip())
              for c in root.findall("chunk") if c.get("method", "md5") == "md5"]
    return Checksum(root.get("name") or "", int(root.get("total_size") or 0),
                    root.get("md5") or "", chunks)


class OfflineDownloader:
    def __init__(self, api: GogApi, progress: Callable[[Progress], None] = lambda p: None,
                 cancel: threading.Event | None = None, http: httpx.Client | None = None):
        self.api = api
        self.http = http or httpx.Client(timeout=httpx.Timeout(30, read=60), follow_redirects=True)
        self.progress_cb = progress
        self.cancel = cancel or threading.Event()
        self.p = Progress()
        self._last_emit = 0.0
        self._speed_mark = (time.monotonic(), 0)

    def _emit(self, force: bool = False):
        now = time.monotonic()
        t0, b0 = self._speed_mark
        if now - t0 >= 1:
            self.p.speed = (self.p.downloaded - b0) / (now - t0)
            self._speed_mark = (now, self.p.downloaded)
        if force or now - self._last_emit > 0.25:
            self._last_emit = now
            self.progress_cb(self.p)

    def _check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled()

    def _resolve(self, f: OfflineFile) -> tuple[str, Checksum | None]:
        info = self.api._get(f.downlink).json()
        url = info.get("downlink")
        if not url:
            raise ContentError("GOG no ha dado el enlace de descarga")
        checksum = None
        if info.get("checksum"):
            try:
                r = self.http.get(info["checksum"])
                if r.status_code == 200:
                    checksum = parse_checksum(r.text)
            except httpx.HTTPError:
                pass
        return url, checksum

    def download(self, bundle: Bundle, dest: Path | None = None) -> list[Path]:
        dest = dest or bundle_dir(bundle)
        dest.mkdir(parents=True, exist_ok=True)
        self.p = Progress(stage="prepare", files_total=len(bundle.files),
                          download_total=bundle.total_size)
        self._emit(True)
        need = bundle.total_size - sum(_part_size(dest, f) for f in bundle.files)
        free = shutil.disk_usage(dest).free
        if need > free:
            raise ContentError(f"No hay espacio suficiente en {dest}: hacen falta "
                               f"{need / 2**30:.1f} GB y quedan {free / 2**30:.1f} GB")
        out = []
        for f in bundle.files:
            self._check_cancel()
            out.append(self._file(f, dest))
            self.p.files_done += 1
        self.p.stage = "done"
        self._emit(True)
        return out

    def _file(self, f: OfflineFile, dest: Path) -> Path:
        url, cs = self._resolve(f)
        name = (cs.name if cs and cs.name else "") or unquote(Path(urlparse(url).path).name)
        name = safe_name(name)
        target = dest / name
        # El tamaño de la API va redondeado (a MB en los extras); el real es el
        # del XML o, si no lo hay, el que diga el servidor.
        size = cs.size if cs and cs.size else self._remote_size(url) or f.size
        self.p.download_total += size - f.size
        if target.exists() and target.stat().st_size == size:
            self.p.downloaded += size
            self._emit()
            _remember(dest, f, name)
            return target
        part = dest / (name + PART)
        self.p.stage = "download"
        have = part.stat().st_size if part.exists() else 0
        if have > size:
            part.unlink()
            have = 0
        self.p.downloaded += have
        url = self._fetch(f, url, part, have, size)
        if cs and cs.chunks:
            self.p.stage = "verify"
            self._emit(True)
            for c in self._bad_chunks(part, cs):
                self._check_cancel()
                self.p.stage = "download"
                self.p.downloaded -= c.end - c.start + 1
                url = self._fetch(f, url, part, c.start, c.end + 1, overwrite=True)
            if self._bad_chunks(part, cs):
                raise ContentError(f"{name}: los datos descargados no coinciden con los de GOG")
        elif part.stat().st_size != size:
            raise ContentError(f"{name}: tamaño incorrecto tras la descarga")
        os.replace(part, target)
        _remember(dest, f, name)
        return target

    def _remote_size(self, url: str) -> int | None:
        try:
            r = self.http.head(url)
            if r.status_code == 200 and r.headers.get("content-length"):
                return int(r.headers["content-length"])
        except (httpx.HTTPError, ValueError):
            pass
        return None

    def _fetch(self, f: OfflineFile, url: str, part: Path, start: int, end: int,
               overwrite: bool = False) -> str:
        """Descarga [start, end) en part. Devuelve la URL vigente (puede renovarse)."""
        pos = start
        tries = 0
        while pos < end:
            self._check_cancel()
            try:
                headers = {"Range": f"bytes={pos}-{end - 1}"}
                with self.http.stream("GET", url, headers=headers) as r:
                    if r.status_code in (401, 403, 404, 410):
                        url, _ = self._resolve(f)  # enlace caducado
                        tries += 1
                        if tries > RETRIES:
                            raise ContentError(f"GOG rechaza la descarga ({r.status_code})")
                        continue
                    if r.status_code == 200 and pos > 0:
                        raise ContentError("el servidor no permite reanudar la descarga")
                    if r.status_code not in (200, 206):
                        raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request,
                                                    response=r)
                    mode = "r+b" if overwrite or part.exists() else "wb"
                    with open(part, mode) as fh:
                        fh.seek(pos)
                        for data in r.iter_bytes(BLOCK):
                            self._check_cancel()
                            data = data[:end - pos]
                            fh.write(data)
                            pos += len(data)
                            self.p.downloaded += len(data)
                            self._emit()
                            if pos >= end:
                                break
                tries = 0
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                tries += 1
                if tries > RETRIES:
                    raise ContentError(f"error de red: {e}") from e
                time.sleep(min(2 ** tries, 30))
        return url

    def _bad_chunks(self, path: Path, cs: Checksum) -> list[Chunk]:
        bad = []
        with open(path, "rb") as fh:
            for c in cs.chunks:
                self._check_cancel()
                fh.seek(c.start)
                h = hashlib.md5()
                left = c.end - c.start + 1
                while left > 0:
                    data = fh.read(min(BLOCK, left))
                    if not data:
                        break
                    h.update(data)
                    left -= len(data)
                if left > 0 or h.hexdigest() != c.md5:
                    bad.append(c)
        return bad


# --- qué hay ya descargado ---------------------------------------------------------
# Un índice pequeño por carpeta (id de archivo → nombre) para saber, sin pedir
# nada a GOG, si un paquete está completo.

INDEX = ".gogl-offline"


def _remember(dest: Path, f: OfflineFile, name: str) -> None:
    idx = dest / INDEX
    lines = idx.read_text().splitlines() if idx.exists() else []
    entry = f"{f.id}\t{name}"
    if entry not in lines:
        lines = [l for l in lines if not l.startswith(f"{f.id}\t")] + [entry]
        idx.write_text("\n".join(lines) + "\n")


def _index(dest: Path) -> dict[str, str]:
    try:
        return dict(l.split("\t", 1) for l in (dest / INDEX).read_text().splitlines() if "\t" in l)
    except OSError:
        return {}


def _part_size(dest: Path, f: OfflineFile) -> int:
    name = _index(dest).get(f.id)
    if name and (dest / name).exists():
        return (dest / name).stat().st_size
    return 0


def downloaded_files(b: Bundle, root: Path | None = None) -> list[Path] | None:
    """Rutas de los archivos si el paquete está completo; None si falta alguno."""
    dest = bundle_dir(b, root)
    idx = _index(dest)
    out = []
    for f in b.files:
        name = idx.get(f.id)
        if not name or not (dest / name).exists():
            return None
        out.append(dest / name)
    return out


def download_bundle(api: GogApi, game_id: str, bundle_key: str, progress=lambda p: None,
                    cancel=None) -> list[Path]:
    """Para la cola de descargas: vuelve a pedir la lista (los enlaces caducan)."""
    off = list_downloads(api, game_id)
    for b in off.installers + off.extras:
        if b.key == bundle_key:
            return OfflineDownloader(api, progress, cancel).download(b)
    raise ApiError("Ese archivo ya no está disponible en tu cuenta de GOG")


__all__ = ["Bundle", "Offline", "OfflineDownloader", "OfflineFile", "bundle_dir", "download_bundle",
           "downloaded_files", "downloads_root", "list_downloads", "parse_checksum"]
