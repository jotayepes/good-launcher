"""Builds de GOG (Content System v1 y v2) → plan de instalación.

v2: build → meta (zlib) → depots → manifiesto de depot (zlib) → archivos en
trozos comprimidos con zlib, direccionados por el MD5 comprimido.
v1: build → repository.json → manifiesto de depot → archivos como rangos de
bytes dentro de un único «main.bin» por producto."""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from .. import languages
from ..api import GogApi
from ..i18n import _
from .common import (DEPS_META, DEPS_PRODUCT, DEPS_REPO, META_V2, REDIST_PREFIX, V1_LANGUAGES, Chunk,
                     ContentError, FileEntry, InstallPlan, fetch_json, galaxy_path, win_to_posix)

OWNED_URL = "https://embed.gog.com/user/data/games"
PRODUCT_URL = "https://api.gog.com/products/{id}"
SUPPORT_DIR = "gog-support"
SFC_DIR = ".gogl-sfc"
V1_NAME_TO_CODE = {v: k for k, v in V1_LANGUAGES.items()}
# .NET Framework: Proton trae Wine Mono, y sus instaladores suelen colgarse bajo
# Wine. No se descargan (algunos pesan 250 MB). Los .NET Core/Desktop sí.
SKIPPED_DEPENDENCIES = {"dotNet35", "dotNet35C", "dotNet4", "dotNet4C", "dotNet45", "dotNet46",
                        "dotNet47", "dotNet472"}


@dataclass
class BuildInfo:
    game_id: str
    platform: str
    generation: int
    build_id: str
    version: str
    title: str
    install_dir_name: str
    link: str
    languages: list[str]                 # idiomas con depot propio
    dlcs: list[tuple[str, str]]          # (productId, nombre) que el usuario posee
    depots: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    store_languages: list[str] = field(default_factory=list)  # los que anuncia la tienda

    @property
    def all_languages(self) -> list[str]:
        """Idiomas que se pueden elegir: los de la tienda más los que tienen depot."""
        return sorted(set(self.languages) | set(self.store_languages)) or ["en-US"]

    def depot_language(self, language: str) -> str:
        """Qué depot de idioma descargar. Si el elegido no tiene depot propio, el
        depot base (normalmente «en-US») ya trae todos los idiomas del juego."""
        if language in self.languages or not self.languages:
            return language
        return "en-US" if "en-US" in self.languages else self.languages[0]

    def default_language(self, preferred: str | None = None) -> str:
        preferred = preferred or languages.preferred()
        for cand in (preferred, "en-US"):
            if cand in self.all_languages:
                return cand
        return self.all_languages[0]

    def selected_depots(self, language: str, dlcs: list[str]) -> list[dict]:
        language = self.depot_language(language)
        ids = {self.game_id, *dlcs}
        out = []
        for d in self.depots:
            if self.generation == 2:
                pid_ok = d.get("productId") in ids
                langs = d.get("languages") or ["*"]
                lang_ok = "*" in langs or language in langs
                bits = d.get("osBitness") or []
                bits_ok = not bits or "64" in bits
                if pid_ok and lang_ok and bits_ok:
                    out.append(d)
            else:
                if "manifest" not in d:
                    continue  # entradas de redistribuibles
                pid_ok = bool(set(d.get("gameIDs", [])) & ids)
                langs = d.get("languages") or ["Neutral"]
                lang_ok = "Neutral" in langs or V1_LANGUAGES.get(language, language) in langs
                if pid_ok and lang_ok:
                    out.append(d)
        return out

    def estimate(self, language: str, dlcs: list[str]) -> tuple[int, int]:
        """(descarga, espacio en disco) aproximados, sin bajar los manifiestos."""
        down = disk = 0
        for d in self.selected_depots(language, dlcs):
            size = int(d.get("size", 0))
            disk += size
            down += int(d.get("compressedSize", size))
        return down, disk


def owned_ids(api: GogApi) -> set[str]:
    try:
        return {str(x) for x in api._get(OWNED_URL).json().get("owned", [])}
    except Exception:  # noqa: BLE001 - sin esto solo no se ofrecen DLC
        return set()


def latest_build(api: GogApi, game_id: str, platform: str = "windows") -> dict:
    builds = api.builds(game_id, platform, generation=2)
    builds = [b for b in builds if b.get("public", True) and not b.get("branch")] or builds
    if not builds:
        raise ContentError(_("No hay builds de {platform} para el juego {id}").format(platform=platform, id=game_id))
    return builds[0]


def store_languages(game_id: str, http: httpx.Client) -> list[str]:
    try:
        r = http.get(PRODUCT_URL.format(id=game_id))
        return languages.from_store(r.json().get("languages")) if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        return []


def build_info(api: GogApi, game_id: str, platform: str = "windows",
               owned: set[str] | None = None, http: httpx.Client | None = None) -> BuildInfo:
    http = http or httpx.Client(timeout=60, follow_redirects=True)
    info = _build_info(api, game_id, platform, owned, http)
    info.store_languages = store_languages(game_id, http)
    return info


def _build_info(api: GogApi, game_id: str, platform: str, owned: set[str] | None,
                http: httpx.Client) -> BuildInfo:
    b = latest_build(api, game_id, platform)
    owned = owned if owned is not None else owned_ids(api)
    meta = fetch_json(http, b["link"])
    if b.get("generation", 2) == 2:
        langs = sorted({l for d in meta.get("depots", []) if d.get("productId") == game_id
                        for l in d.get("languages", []) if l != "*"})
        dlcs = [(str(p["productId"]), p.get("name", "")) for p in meta.get("products", [])
                if str(p["productId"]) != game_id and str(p["productId"]) in owned]
        title = next((p.get("name") for p in meta.get("products", []) if str(p["productId"]) == game_id),
                     game_id)
        return BuildInfo(game_id, platform, 2, str(b["build_id"]), b.get("version_name", ""), title,
                         meta.get("installDirectory") or title, b["link"], langs, dlcs,
                         meta.get("depots", []), meta)
    product = meta["product"]
    langs = sorted({V1_NAME_TO_CODE.get(l, l) for d in product.get("depots", [])
                    for l in d.get("languages", []) if l != "Neutral"})
    names = {str(g["gameID"]): (g.get("name") or {}).get("en", "") for g in product.get("gameIDs", [])}
    dlcs = [(gid, n) for gid, n in names.items() if gid != game_id and gid in owned]
    return BuildInfo(game_id, platform, 1, str(b["build_id"]), b.get("version_name", ""),
                     product.get("projectName") or names.get(game_id, game_id),
                     product.get("installDirectory") or game_id, b["link"], langs, dlcs,
                     product.get("depots", []), meta)


def make_plan(info: BuildInfo, language: str, dlcs: list[str],
              http: httpx.Client | None = None) -> InstallPlan:
    http = http or httpx.Client(timeout=120, follow_redirects=True)
    plan = InstallPlan(info.game_id, info.title, info.platform, info.generation, info.build_id,
                       info.version, info.install_dir_name, language, list(dlcs),
                       depot_language=info.depot_language(language))
    depots = info.selected_depots(language, dlcs)
    if not depots:
        raise ContentError(_("No hay archivos para el idioma {language}").format(language=language))
    files: dict[str, FileEntry] = {}
    if info.generation == 2:
        plan.client_id = info.meta.get("clientId")
        plan.client_secret = info.meta.get("clientSecret")
        for d in depots:
            depot = fetch_json(http, META_V2 + galaxy_path(d["manifest"]))["depot"]
            _add_v2_depot(plan, files, depot, str(d["productId"]), d["manifest"])
    else:
        plan.v1_timestamp = info.meta["product"]["timestamp"]
        base = info.link.rsplit("/", 1)[0]
        for d in depots:
            depot = fetch_json(http, f"{base}/{d['manifest']}")["depot"]
            pid = str(d["gameIDs"][0])
            for f in depot.get("files", []):
                path = f["path"].lstrip("/")
                if f.get("directory"):
                    plan.dirs.append(path)
                    continue
                if f.get("support"):
                    path = f"{SUPPORT_DIR}/{path}"
                files[path.casefold()] = FileEntry(
                    path, int(f["size"]), pid, md5=f.get("hash"), v1_offset=int(f.get("offset", 0)),
                    v1_blob=f.get("url") or f"{pid}/main.bin")
    _add_setup(plan, info, dlcs)
    _add_dependencies(plan, files, dependency_ids(info), http)
    plan.files = list(files.values())
    plan.validate()
    return plan


def dependency_ids(info: BuildInfo) -> list[str]:
    """Dependencias que pide el juego: v2 las lista en el meta; v1 como depots «redist»."""
    if info.generation == 2:
        ids = list(info.meta.get("dependencies") or [])
        if info.meta.get("scriptInterpreter"):
            ids.append("ISI")
    else:
        ids = [d["redist"] for d in info.meta["product"].get("depots", []) if "redist" in d]
    return [d for d in dict.fromkeys(ids) if d not in SKIPPED_DEPENDENCIES]


def _add_setup(plan: InstallPlan, info: BuildInfo, dlcs: list[str]) -> None:
    """Lo que Galaxy ejecuta tras instalar: scripts de GOG o instaladores de soporte."""
    wanted = {info.game_id, *dlcs}
    if info.generation == 2:
        products = [p for p in info.meta.get("products", []) if str(p["productId"]) in wanted]
        if info.meta.get("scriptInterpreter"):
            plan.script_products = [str(p["productId"]) for p in products]
        else:
            plan.support_commands = [{"product_id": str(p["productId"]), "executable": p["temp_executable"],
                                      "arguments": p.get("temp_arguments") or ""}
                                     for p in products if p.get("temp_executable")]
        return
    lang = V1_LANGUAGES.get(plan.language, plan.language)
    for c in info.meta["product"].get("support_commands") or []:
        langs = c.get("languages") or ["Neutral"]
        if str(c.get("gameID")) in wanted and ("Neutral" in langs or lang in langs) and c.get("executable"):
            plan.support_commands.append({"product_id": str(c["gameID"]),
                                          "executable": c["executable"].lstrip("/"),
                                          "arguments": c.get("argument") or ""})


_repo_cache: dict[str, dict] = {}


def dependency_repository(http: httpx.Client) -> dict:
    if "repo" not in _repo_cache:
        manifest = fetch_json(http, DEPS_REPO)["repository_manifest"]
        _repo_cache["repo"] = fetch_json(http, manifest)
    return _repo_cache["repo"]


def _add_dependencies(plan: InstallPlan, files: dict[str, FileEntry], ids: list[str],
                      http: httpx.Client) -> None:
    """DOSBox, ScummVM, nGlide… van dentro de la carpeta del juego (los usa al
    arrancar); Visual C++, DirectX… («__redist/») a una carpeta compartida, y se
    instalan en el prefijo antes de la primera partida."""
    plan.dependencies = ids
    if not ids:
        return
    by_id = {d["dependencyId"]: d for d in dependency_repository(http).get("depots", [])}
    for dep_id in ids:
        d = by_id.get(dep_id)
        if not d:
            continue  # GOG ya no la publica: el juego suele arrancar igual
        depot = fetch_json(http, DEPS_META + galaxy_path(d["manifest"]))["depot"]
        dep_files: dict[str, FileEntry] = {}
        _add_v2_depot(plan, dep_files, depot, DEPS_PRODUCT, d["manifest"])
        for key, f in dep_files.items():
            if f.path.startswith(REDIST_PREFIX) and not f.sfc:
                plan.redist_files.append(f)
            else:
                files[key] = f
        exe = (d.get("executable") or {}).get("path") or ""
        if exe and not d.get("internal"):
            plan.redist.append({"id": dep_id, "name": d.get("readableName") or dep_id,
                                "executable": win_to_posix(exe),
                                "arguments": d["executable"].get("arguments") or ""})


def _add_v2_depot(plan: InstallPlan, files: dict[str, FileEntry], depot: dict, pid: str, manifest: str):
    sfc_path = None
    sfc = depot.get("smallFilesContainer")
    if sfc:
        sfc_path = f"{SFC_DIR}/{manifest}"
        chunks, off = [], 0
        for c in sfc.get("chunks", []):
            chunks.append(Chunk(off, c["size"], c["md5"], c["compressedSize"], c["compressedMd5"]))
            off += c["size"]
        files[sfc_path] = FileEntry(sfc_path, off, pid, chunks=chunks)
    for it in depot.get("items", []):
        t = it.get("type")
        path = win_to_posix(it["path"])
        if t == "DepotDirectory":
            plan.dirs.append(path)
            continue
        if t == "DepotLink":
            plan.links.append((path, win_to_posix(it["target"])))
            continue
        if t != "DepotFile":
            continue
        flags = it.get("flags") or []
        if "support" in flags:
            path = f"{SUPPORT_DIR}/{pid}/{path}"
        chunks, off = [], 0
        for c in it.get("chunks", []):
            chunks.append(Chunk(off, c["size"], c["md5"], c["compressedSize"], c["compressedMd5"]))
            off += c["size"]
        md5 = it.get("md5") or (chunks[0].md5 if len(chunks) == 1 else None)
        entry = FileEntry(path, off, pid, executable="executable" in flags, md5=md5, chunks=chunks)
        ref = it.get("sfcRef")
        if ref and sfc_path:
            entry.size = int(ref["size"])
            entry.md5 = it.get("md5") or entry.md5
            entry.chunks = []
            entry.sfc = (sfc_path, int(ref["offset"]))
        files[path.casefold()] = entry
