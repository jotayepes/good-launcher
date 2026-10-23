"""Modelo común de un plan de instalación y enlaces firmados del CDN de GOG."""

from __future__ import annotations

import posixpath
import re
import threading
import time
from dataclasses import asdict, dataclass, field

import httpx

from ..api import CONTENT_SYSTEM, ApiError, GogApi
from ..i18n import _

META_V2 = "https://gog-cdn-fastly.gog.com/content-system/v2/meta/"
DEPS_REPO = "https://content-system.gog.com/dependencies/repository?generation=2"
DEPS_META = "https://gog-cdn-fastly.gog.com/content-system/v2/dependencies/meta/"
DEPS_LINK = "https://content-system.gog.com/open_link"
DEPS_PRODUCT = "__deps__"  # «producto» de los archivos de dependencias (DOSBox, Visual C++…)
REDIST_PREFIX = "__redist/"  # dependencias que se instalan en el prefijo, no en el juego
LINK_TTL = 20 * 60  # los enlaces firmados caducan; se renuevan con margen

# Códigos de idioma de v2 → nombres que usa v1.
V1_LANGUAGES = {
    "en-US": "English", "es-ES": "Spanish", "es-MX": "Latin American Spanish", "fr-FR": "French",
    "de-DE": "German", "it-IT": "Italian", "pt-BR": "Brazilian Portuguese", "pt-PT": "Portuguese",
    "ru-RU": "Russian", "pl-PL": "Polish", "zh-Hans": "Chinese", "ja-JP": "Japanese", "ko-KR": "Korean",
    "cs-CZ": "Czech", "hu-HU": "Hungarian", "tr-TR": "Turkish", "nl-NL": "Dutch",
}
LANGUAGE_NAMES = {
    "en-US": "Inglés", "es-ES": "Español", "es-MX": "Español (Latinoamérica)", "fr-FR": "Francés",
    "de-DE": "Alemán", "it-IT": "Italiano", "pt-BR": "Portugués (Brasil)", "pt-PT": "Portugués",
    "ru-RU": "Ruso", "pl-PL": "Polaco", "zh-Hans": "Chino simplificado", "zh-Hant": "Chino tradicional",
    "ja-JP": "Japonés", "ko-KR": "Coreano", "cs-CZ": "Checo", "hu-HU": "Húngaro", "tr-TR": "Turco",
    "nl-NL": "Neerlandés",
}


class ContentError(Exception):
    pass


def galaxy_path(h: str) -> str:
    return f"{h[:2]}/{h[2:4]}/{h}"


def win_to_posix(p: str) -> str:
    return p.replace("\\", "/").strip("/")


def safe_rel(rel: str) -> str:
    """Ruta relativa que no puede salir de su carpeta (sin «..» ni ser absoluta).
    Los manifiestos no van firmados: nunca se usan sus rutas sin pasar por aquí."""
    parts = rel.split("/")
    if not rel or rel.startswith("/") or ".." in parts or "\x00" in rel:
        raise ContentError(_("Ruta sospechosa en el manifiesto: {path}").format(path=repr(rel)))
    return rel


def safe_link(path: str, target: str) -> None:
    """Un enlace del manifiesto debe apuntar dentro de la carpeta del juego: si
    no, lo que se escribiera a través de él acabaría fuera."""
    safe_rel(path)
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    if target.startswith("/") or resolved == ".." or resolved.startswith("../"):
        raise ContentError(_("Enlace sospechoso en el manifiesto: {path} → {target}").format(path=path, target=target))


def safe_name(s: str) -> str:
    """Un nombre de carpeta o archivo sin separadores, «..» ni caracteres raros."""
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", s).strip(" .")
    return s or "juego"


@dataclass
class Chunk:
    offset: int            # posición en el archivo final
    size: int              # tamaño descomprimido
    md5: str
    compressed_size: int = 0
    compressed_md5: str = ""


@dataclass
class FileEntry:
    path: str              # relativa a la carpeta de instalación, con «/»
    size: int
    product_id: str
    executable: bool = False
    md5: str | None = None             # del archivo completo, si se conoce
    chunks: list[Chunk] = field(default_factory=list)   # v2
    v1_offset: int | None = None       # v1: posición en main.bin
    v1_blob: str | None = None         # v1: «<productId>/main.bin»
    sfc: tuple[str, int] | None = None  # v2: (ruta del contenedor, offset) para archivos pequeños

    @property
    def signature(self) -> str:
        """Identidad del contenido: si no cambia entre builds, no se vuelve a descargar."""
        if self.md5:
            return self.md5
        return ",".join(c.md5 for c in self.chunks)

    @property
    def download_size(self) -> int:
        if self.sfc:
            return 0
        if self.chunks:
            return sum(c.compressed_size for c in self.chunks)
        return self.size


@dataclass
class InstallPlan:
    game_id: str
    title: str
    platform: str          # windows | linux
    generation: int        # 1, 2, o 0 para instaladores .sh de Linux
    build_id: str
    version: str
    install_dir_name: str
    language: str
    dlcs: list[str]
    client_id: str | None = None
    client_secret: str | None = None
    files: list[FileEntry] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)
    v1_timestamp: int | None = None
    depot_language: str = ""   # el depot descargado; «language» es el que se usa al jugar
    # Dependencias de GOG. None = manifiesto de antes de que gogl las instalara.
    dependencies: list[str] | None = None
    redist_files: list[FileEntry] = field(default_factory=list)  # en la carpeta compartida
    redist: list[dict] = field(default_factory=list)       # {id, name, executable, arguments}
    support_commands: list[dict] = field(default_factory=list)  # {product_id, executable, arguments}
    script_products: list[str] = field(default_factory=list)    # productos con goggame-<id>.script

    @property
    def download_size(self) -> int:
        return sum(f.download_size for f in self.files)

    @property
    def disk_size(self) -> int:
        return sum(f.size for f in self.files if not f.path.startswith(".gogl-sfc/"))

    def to_json(self) -> dict:
        return asdict(self)

    def validate(self) -> None:
        """Lanza ContentError si alguna ruta del plan saldría de su carpeta."""
        for f in self.files + self.redist_files:
            safe_rel(f.path)
            if f.sfc:
                safe_rel(f.sfc[0])
        for d in self.dirs:
            safe_rel(d)
        for path, target in self.links:
            safe_link(path, target)

    @classmethod
    def from_json(cls, d: dict) -> "InstallPlan":
        d = {**d, "files": [_file_from_json(f) for f in d.get("files", [])],
             "redist_files": [_file_from_json(f) for f in d.get("redist_files", [])],
             "links": [tuple(x) for x in d.get("links", [])]}
        return cls(**d)


def _file_from_json(f: dict) -> FileEntry:
    f = dict(f)
    f["chunks"] = [Chunk(**c) for c in f.get("chunks", [])]
    if f.get("sfc"):
        f["sfc"] = tuple(f["sfc"])
    return FileEntry(**f)


class SecureLinks:
    """Enlaces firmados por producto, cacheados y renovados al caducar o ante 401/403."""

    def __init__(self, api: GogApi, generation: int, v1_path: str = "/"):
        self.api = api
        self.generation = generation
        self.v1_path = v1_path
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._lock = threading.Lock()

    def endpoints(self, product_id: str, refresh: bool = False) -> list[dict]:
        with self._lock:
            hit = self._cache.get(product_id)
            if hit and not refresh and time.time() - hit[0] < LINK_TTL:
                return hit[1]
            if product_id == DEPS_PRODUCT:
                # Enlace abierto (sin sesión), el mismo para juegos v1 y v2.
                r = self.api.http.get(DEPS_LINK, params={"generation": 2, "_version": 2,
                                                         "path": "/dependencies/store/"})
                if r.status_code != 200:
                    raise ContentError(_("Enlaces de dependencias → HTTP {code}").format(code=r.status_code))
            else:
                if self.generation == 2:
                    params = {"_version": 2, "generation": 2, "path": "/"}
                else:
                    params = {"_version": 2, "type": "depot", "path": self.v1_path}
                r = self.api._get(f"{CONTENT_SYSTEM}/products/{product_id}/secure_link", params=params)
            urls = r.json().get("urls") or []
            if not urls:
                raise ContentError(_("GOG no devolvió enlaces de descarga para {id}").format(id=product_id))
            self._cache[product_id] = (time.time(), urls)
            return urls

    def url(self, product_id: str, rel: str, attempt: int = 0) -> str:
        eps = self.endpoints(product_id, refresh=attempt > 0 and attempt % 2 == 0)
        ep = eps[attempt % len(eps)]
        params = dict(ep.get("parameters") or {})
        params["path"] = (params.get("path", "").rstrip("/") + "/" + rel.lstrip("/"))
        url = ep["url_format"]
        if "{path}" not in url:  # p. ej. el de dependencias: la ruta va al final
            url = url.rstrip("/") + params.pop("path")
        for k, v in params.items():
            url = url.replace("{" + k + "}", str(v))
        return url


def fetch_json(http: httpx.Client, url: str) -> dict:
    """Descarga JSON, comprimido con zlib o no (los manifiestos v2 lo están)."""
    from ..api import decode_meta
    r = http.get(url)
    if r.status_code != 200:
        raise ContentError(f"GET {url} → HTTP {r.status_code}")
    return decode_meta(r.content)


__all__ = ["ApiError", "Chunk", "ContentError", "FileEntry", "InstallPlan", "SecureLinks",
           "galaxy_path", "win_to_posix", "safe_rel", "safe_link", "safe_name", "fetch_json", "META_V2",
           "DEPS_META", "DEPS_PRODUCT", "DEPS_REPO", "REDIST_PREFIX", "V1_LANGUAGES", "LANGUAGE_NAMES"]
