"""Versiones de Proton-GE gestionadas por gogl, en su propio directorio de datos.

Se descargan de los releases oficiales de GloriousEggroll, se verifica el
SHA-512 publicado y se descomprimen en ~/.local/share/gogl/proton/<versión>.
No se usa nada de la instalación de Steam."""

from __future__ import annotations

import hashlib
import platform
import re
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from .. import config, paths

RELEASES_URL = "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases"


class ProtonError(Exception):
    pass


@dataclass(frozen=True)
class Release:
    name: str
    url: str
    size: int
    checksum_url: str | None


@dataclass(frozen=True)
class ProtonVersion:
    name: str
    path: Path

    @property
    def script(self) -> Path:
        return self.path / "proton"

    @property
    def wineserver(self) -> Path | None:
        for d in ("files", "dist"):
            p = self.path / d / "bin" / "wineserver"
            if p.exists():
                return p
        return None


def proton_root() -> Path:
    d = paths.data_dir() / "proton"
    d.mkdir(parents=True, exist_ok=True)
    return d


def installed() -> list[ProtonVersion]:
    out = [ProtonVersion(d.name, d) for d in proton_root().iterdir()
           if d.is_dir() and not d.name.startswith(".") and (d / "proton").exists()]
    return sorted(out, key=lambda v: _version_key(v.name), reverse=True)


def _version_key(name: str):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", name)]


def default() -> ProtonVersion | None:
    """La versión elegida por el usuario, o la más reciente instalada."""
    vers = installed()
    want = config.load().get("default_proton")
    for v in vers:
        if v.name == want:
            return v
    return vers[0] if vers else None


def get(name: str | None) -> ProtonVersion | None:
    if not name:
        return default()
    return next((v for v in installed() if v.name == name), None)


def set_default(name: str) -> None:
    if not get(name):
        raise ProtonError(f"{name} no está instalado")
    config.set_value("default_proton", name)


def remove(name: str) -> None:
    v = get(name)
    if not v:
        raise ProtonError(f"{name} no está instalado")
    shutil.rmtree(v.path)
    if config.load().get("default_proton") == name:
        config.set_value("default_proton", None)


def available(limit: int = 10, http: httpx.Client | None = None) -> list[Release]:
    http = http or httpx.Client(timeout=30, follow_redirects=True)
    r = http.get(RELEASES_URL, params={"per_page": limit},
                 headers={"Accept": "application/vnd.github+json"})
    if r.status_code != 200:
        raise ProtonError(f"GitHub respondió {r.status_code}")
    arch = platform.machine() or "x86_64"
    out = []
    for rel in r.json():
        assets = {a["name"]: a for a in rel.get("assets", [])}
        tag = rel["tag_name"]
        tar = assets.get(f"{tag}-{arch}.tar.gz") or assets.get(f"{tag}.tar.gz")
        if not tar:
            continue
        chk = assets.get(tar["name"].removesuffix(".tar.gz") + ".sha512sum")
        out.append(Release(tag, tar["browser_download_url"], tar["size"],
                           chk["browser_download_url"] if chk else None))
    return out


def install(release: Release, progress: Callable[[int, int], None] = lambda d, t: None,
            http: httpx.Client | None = None) -> ProtonVersion:
    if get(release.name):
        return get(release.name)
    if not release.checksum_url:
        raise ProtonError(f"{release.name} no publica su SHA-512: no se puede verificar la descarga")
    http = http or httpx.Client(timeout=httpx.Timeout(60, connect=20), follow_redirects=True)
    root = proton_root()
    tmp_dir = root / f".tmp-{release.name}"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir()
    try:
        archive = tmp_dir / "proton.tar.gz"
        h = hashlib.sha512()
        with http.stream("GET", release.url) as r:
            if r.status_code != 200:
                raise ProtonError(f"Descarga fallida: HTTP {r.status_code}")
            total = int(r.headers.get("content-length") or release.size)
            done = 0
            with open(archive, "wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    progress(done, total)
        r = http.get(release.checksum_url)
        expected = r.text.split()[0].lower() if r.status_code == 200 and r.text.split() else ""
        if h.hexdigest() != expected:
            raise ProtonError("El SHA-512 del archivo descargado no coincide; descarga corrupta")
        out = tmp_dir / "x"
        with tarfile.open(archive, "r:gz") as tar:
            # filtro «tar»: impide rutas fuera del destino pero conserva los
            # enlaces simbólicos del prefijo por defecto (dosdevices/z: → /).
            tar.extractall(out, filter="tar")
        tops = [d for d in out.iterdir() if d.is_dir()]
        if len(tops) != 1 or not (tops[0] / "proton").exists():
            raise ProtonError("El archivo no tiene la estructura esperada de Proton")
        dest = root / release.name
        tops[0].rename(dest)
        return ProtonVersion(release.name, dest)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
