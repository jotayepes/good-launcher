"""Juegos nativos de Linux: GOG los distribuye como instaladores .sh de MojoSetup
(un script makeself con un zip pegado al final). No ejecutamos el script: el
zip se abre directamente y se extrae data/noarch/ a la carpeta del juego."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import time
import zipfile
from pathlib import Path

from .. import paths
from .common import ContentError, FileEntry, InstallPlan, safe_name
from .installer import Cancelled, Installer, Progress, dir_is_new, load_manifest, save_manifest

PRODUCT_URL = "https://api.gog.com/products/{id}"
PAYLOAD = "data/noarch/"


def linux_installer(installer: Installer, game_id: str, language: str = "es-ES") -> tuple[dict, dict]:
    """(producto, instalador de Linux elegido por idioma)."""
    r = installer.http.get(PRODUCT_URL.format(id=game_id), params={"expand": "downloads"})
    if r.status_code != 200:
        raise ContentError(f"api.gog.com → HTTP {r.status_code}")
    product = r.json()
    linux = [i for i in (product.get("downloads") or {}).get("installers", []) if i.get("os") == "linux"]
    if not linux:
        raise ContentError("Este juego no tiene versión nativa de Linux")
    short = language.split("-")[0]
    chosen = next((i for i in linux if i.get("language") == short), None) or \
        next((i for i in linux if i.get("language") == "en"), linux[0])
    return product, chosen


def install_linux(installer: Installer, game_id: str, install_path: Path | None,
                  language: str = "es-ES"):
    product, inst = linux_installer(installer, game_id, language)
    if install_path is None:
        from . import install_root
        install_path = install_root() / safe_name(product.get("title") or game_id)
    f, link = _pick_file(installer, inst.get("files") or [])
    url, checksum_url = link.get("downlink"), link.get("checksum")
    expected = None
    if checksum_url:
        try:
            m = re.search(r'md5="([0-9a-f]{32})"', installer.http.get(checksum_url).text)
            expected = m.group(1) if m else None
        except Exception:  # noqa: BLE001 - la suma es opcional en algunos instaladores
            expected = None

    dl_dir = paths.data_dir() / "downloads"
    dl_dir.mkdir(parents=True, exist_ok=True)
    sh = dl_dir / f"{game_id}-{f['id']}.sh"
    size = int(f.get("size") or 0)
    installer.p = Progress(download_total=size, disk_total=size, files_total=1)
    _download_resumable(installer, url, sh, size)
    if expected and _md5(sh) != expected:
        sh.unlink(missing_ok=True)
        raise ContentError("El instalador descargado está corrupto (MD5 distinto); vuelve a intentarlo")

    installer.p.stage = "finalize"
    installer._emit(True)
    old = load_manifest(game_id)
    if old and Path(old.get("install_path", "")) == install_path:
        created_dir = old.get("created_dir", False)
    else:
        created_dir = dir_is_new(install_path)
    entries, links = _extract(sh, install_path, installer)
    plan = InstallPlan(game_id, product.get("title", game_id), "linux", 0, inst.get("version") or "",
                       inst.get("version") or "", install_path.name, language, [], files=entries,
                       links=links)
    save_manifest(game_id, {"install_path": str(install_path), "installed": time.time(),
                            "created_dir": created_dir, "plan": plan.to_json()})
    sh.unlink(missing_ok=True)
    game = installer._register(plan, install_path, None)
    installer.p.stage = "done"
    installer._emit(True)
    return game


def _sh_version(url: str) -> tuple[int, ...] | None:
    """gog_juego_2.1.0.4.sh → (2, 1, 0, 4); None si no es un .sh."""
    name = url.split("?", 1)[0].rsplit("/", 1)[-1]
    if not name.endswith(".sh"):
        return None
    m = re.search(r"(\d+(?:\.\d+)+)\.sh$", name)
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def _pick_file(installer: Installer, files: list[dict]) -> tuple[dict, dict]:
    """El archivo a descargar y su enlace. Cuando GOG lista varios para Linux
    no son partes de un mismo instalador, sino versiones distintas completas
    (cada una un .sh con su zip): se elige la más reciente."""
    if not files:
        raise ContentError("GOG no lista ningún archivo para este instalador")
    links = []
    for f in files:
        link = installer.api._get(f["downlink"]).json()
        if not link.get("downlink"):
            raise ContentError("GOG no devolvió enlace de descarga")
        links.append((f, link))
    if len(links) == 1:
        return links[0]
    versions = [_sh_version(link["downlink"]) for _f, link in links]
    if any(v is None for v in versions):
        raise ContentError("Este instalador de Linux viene partido en varios archivos que no son .sh; "
                           "gogl no sabe unirlos. Instala la versión de Windows.")
    best = max(range(len(links)), key=lambda i: (versions[i], i))
    return links[best]


def _download_resumable(installer: Installer, url: str, dest: Path, size: int) -> None:
    for attempt in range(5):
        have = dest.stat().st_size if dest.exists() else 0
        if size and have > size:
            # Restos de otra versión del instalador: no se puede reanudar.
            dest.unlink()
            have = 0
        if size and have == size:
            installer.p.downloaded = have
            return
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with installer.http.stream("GET", url, headers=headers) as r:
                if r.status_code == 200 and have:
                    have = 0  # el servidor ignoró el Range: empezar de cero
                elif r.status_code not in (200, 206):
                    raise ContentError(f"HTTP {r.status_code}")
                installer.p.downloaded = have
                with open(dest, "ab" if have else "wb") as fh:
                    for chunk in r.iter_bytes(1 << 20):
                        installer._check_cancel()
                        fh.write(chunk)
                        installer._add(downloaded=len(chunk))
            if not size or dest.stat().st_size == size:
                return
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            if attempt == 4:
                raise ContentError(f"No se pudo descargar el instalador: {e}") from e
            time.sleep(2 ** attempt)


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _extract(sh: Path, root: Path, installer: Installer) -> tuple[list[FileEntry], list[tuple[str, str]]]:
    """(archivos, enlaces simbólicos) extraídos."""
    root.mkdir(parents=True, exist_ok=True)
    real_root = root.resolve()
    out, links = [], []
    try:
        zf = zipfile.ZipFile(sh)  # zipfile admite datos antepuestos (el script makeself)
    except zipfile.BadZipFile as e:
        raise ContentError("El instalador no contiene un zip válido") from e
    with zf:
        members = [m for m in zf.infolist() if m.filename.startswith(PAYLOAD) and m.filename != PAYLOAD]
        installer.p.disk_total = sum(m.file_size for m in members)
        installer.p.files_total = len(members)
        for m in members:
            installer._check_cancel()
            rel = m.filename[len(PAYLOAD):].rstrip("/")
            parts = rel.split("/")
            if not rel or rel.startswith("/") or ".." in parts:
                raise ContentError(f"Ruta sospechosa en el instalador: {m.filename}")
            # Se escribe en la ruta tal cual, sin resolver: resolverla seguiría los
            # enlaces de una instalación anterior y pisaría el archivo al que apuntan.
            dest = root / rel
            mode = m.external_attr >> 16
            if m.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Pero la carpeta sí se resuelve: un enlace a carpeta no puede sacarnos fuera.
            parent = dest.parent.resolve()
            if parent != real_root and real_root not in parent.parents:
                raise ContentError(f"Ruta sospechosa en el instalador: {m.filename}")
            if stat.S_ISLNK(mode):
                target = zf.read(m).decode()
                if dest.is_symlink() or dest.is_file():
                    dest.unlink()
                os.symlink(target, dest)
                links.append((rel, target))
                continue
            tmp = dest.with_name(dest.name + ".gogl-part")
            with zf.open(m) as src, open(tmp, "wb") as dst:
                while b := src.read(1 << 20):
                    dst.write(b)
            if mode & 0o111:
                tmp.chmod(0o755)
            os.replace(tmp, dest)
            out.append(FileEntry(rel, m.file_size, "", executable=bool(mode & 0o111)))
            installer._add(written=m.file_size, files=1)
    return out, links
