"""Descarga e instalación (también reanudación, actualización y reparación).

- Cada archivo se escribe en «<nombre>.gogl-part» y solo se renombra al
  completarse y verificarse: un corte nunca deja un archivo a medias con su
  nombre real.
- Los trozos se verifican con MD5 (comprimido y descomprimido en v2; el archivo
  completo en v1) y se escriben con pwrite en su posición, en paralelo.
- Un registro de progreso (.partial.jsonl) permite reanudar sin re-verificar
  lo ya instalado. El manifiesto final guarda la firma de cada archivo para
  que una actualización solo descargue lo que cambió.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import threading
import time
import zlib
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from .. import paths
from ..api import GogApi
from ..state import Game, State
from .common import Chunk, ContentError, FileEntry, InstallPlan, SecureLinks, galaxy_path, safe_rel

V1_PIECE = 32 * 1024 * 1024
RETRIES = 5


class Cancelled(Exception):
    pass


@dataclass
class Progress:
    downloaded: int = 0     # bytes recibidos de la red
    download_total: int = 0
    written: int = 0        # bytes escritos en disco
    disk_total: int = 0
    files_done: int = 0
    files_total: int = 0
    speed: float = 0.0      # bytes/s de red
    stage: str = "download"  # prepare | download | finalize | done

    @property
    def fraction(self) -> float:
        if self.download_total:
            return min(1.0, self.downloaded / self.download_total)
        return self.files_done / self.files_total if self.files_total else 0.0


def manifests_dir() -> Path:
    d = paths.data_dir() / "manifests"
    d.mkdir(parents=True, exist_ok=True)
    return d


def redist_dir() -> Path:
    """Instaladores de Visual C++, DirectX… compartidos por todos los juegos."""
    d = paths.data_dir() / "redist"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_manifest(game_id: str) -> dict | None:
    try:
        return json.loads((manifests_dir() / f"{game_id}.json").read_text())
    except (OSError, ValueError):
        return None


def save_manifest(game_id: str, data: dict) -> None:
    p = manifests_dir() / f"{game_id}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, p)


def md5_file(path: Path, start: int = 0, length: int | None = None) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        fh.seek(start)
        left = length
        while True:
            n = 1 << 20 if left is None else min(1 << 20, left)
            if n == 0:
                break
            b = fh.read(n)
            if not b:
                break
            h.update(b)
            if left is not None:
                left -= len(b)
    return h.hexdigest()


def file_ok(path: Path, f: FileEntry) -> bool:
    """Comprueba el contenido real contra el manifiesto."""
    try:
        if path.stat().st_size != f.size:
            return False
    except OSError:
        return False
    if f.chunks:
        return all(md5_file(path, c.offset, c.size) == c.md5 for c in f.chunks)
    if f.md5:
        return md5_file(path) == f.md5
    return True


class Installer:
    def __init__(self, api: GogApi, state: State | None = None,
                 progress: Callable[[Progress], None] = lambda p: None,
                 cancel: threading.Event | None = None, workers: int = 6,
                 http: httpx.Client | None = None):
        self.api = api
        self.state = state or State()
        self.on_progress = progress
        self.cancel = cancel or threading.Event()
        self.workers = workers
        self.http = http or httpx.Client(timeout=httpx.Timeout(60, connect=20), follow_redirects=True,
                                         limits=httpx.Limits(max_connections=workers * 2))
        self.p = Progress()
        self._lock = threading.Lock()
        self._last_emit = 0.0
        self._speed_mark = (time.monotonic(), 0)

    # --- progreso -------------------------------------------------------

    def _emit(self, force: bool = False):
        now = time.monotonic()
        if not force and now - self._last_emit < 0.25:
            return
        t0, b0 = self._speed_mark
        if now - t0 >= 1.0:
            self.p.speed = (self.p.downloaded - b0) / (now - t0)
            self._speed_mark = (now, self.p.downloaded)
        self._last_emit = now
        self.on_progress(self.p)

    def _add(self, downloaded: int = 0, written: int = 0, files: int = 0):
        with self._lock:
            self.p.downloaded += downloaded
            self.p.written += written
            self.p.files_done += files
            self._emit()

    def _check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled()

    # --- instalación ------------------------------------------------------

    def install(self, plan: InstallPlan, install_path: Path, prefix: Path | None = None) -> Game:
        plan.validate()
        fresh = dir_is_new(install_path)
        install_path.mkdir(parents=True, exist_ok=True)
        old = load_manifest(plan.game_id)
        old_files = {}
        same_place = bool(old) and Path(old.get("install_path", "")) == install_path
        if same_place:
            old_files = {f["path"].casefold(): f for f in old["plan"]["files"]}
        journal_path = manifests_dir() / f"{plan.game_id}.partial.jsonl"
        done_sigs, journal_created = self._read_journal(journal_path, plan.build_id)
        # Solo se puede borrar la carpeta entera al desinstalar si la creó gogl
        # (o estaba vacía): si no, ahí hay cosas del usuario.
        created_dir = old.get("created_dir", False) if same_place else (fresh or journal_created)

        todo: list[FileEntry] = []
        for f in plan.files:
            dest = install_path / f.path
            key = f.path.casefold()
            prev = old_files.get(key)
            unchanged = prev and _signature(prev) == f.signature and dest.exists() \
                and dest.stat().st_size == f.size
            if unchanged or (done_sigs.get(key) == f.signature and dest.exists()):
                continue
            todo.append(f)

        redist_root = redist_dir()
        redist_todo = [f for f in plan.redist_files if not file_ok(redist_root / f.path, f)]

        need = sum(f.size for f in todo)
        free = shutil.disk_usage(install_path).free
        if need > free:
            raise ContentError(f"No hay espacio suficiente: hacen falta {need / 2**30:.1f} GB y hay "
                               f"{free / 2**30:.1f} GB libres en {install_path}")

        self.p = Progress(download_total=sum(f.download_size for f in todo + redist_todo),
                          disk_total=need + sum(f.size for f in redist_todo),
                          files_total=len(todo) + len(redist_todo))
        self._emit(True)
        for d in plan.dirs:
            (install_path / d).mkdir(parents=True, exist_ok=True)

        links = SecureLinks(self.api, plan.generation, f"/{plan.platform}/{plan.v1_timestamp}/")
        with open(journal_path, "a") as journal:
            journal.write(json.dumps({"build": plan.build_id, "created": created_dir}) + "\n")
            journal.flush()
            jlock = threading.Lock()

            def finished(f: FileEntry):
                with jlock:
                    journal.write(json.dumps({"path": f.path.casefold(), "sig": f.signature}) + "\n")
                    journal.flush()

            # Los contenedores de archivos pequeños primero: sus archivos se extraen de ellos.
            sfcs = [f for f in todo if f.path.startswith(".gogl-sfc/")]
            normal = [f for f in todo if not f.sfc and f not in sfcs]
            small = [f for f in todo if f.sfc]
            if small:
                needed = {f.sfc[0] for f in small}
                have = {f.path for f in sfcs}
                by_path = {f.path: f for f in plan.files}
                sfcs += [by_path[p] for p in needed - have
                         if p in by_path and not file_ok(install_path / p, by_path[p])]
            self._run(sfcs + normal, install_path, links, finished, old_files)
            for f in small:
                self._extract_sfc(f, install_path)
                finished(f)
                self._add(written=f.size, files=1)
        # Sin registro de progreso: se comprueban por contenido en cada instalación.
        self._run(redist_todo, redist_root, links, lambda f: None)

        self.p.stage = "finalize"
        self._emit(True)
        shutil.rmtree(install_path / ".gogl-sfc", ignore_errors=True)
        for path, target in plan.links:
            link = install_path / path
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(target)

        # Archivos que estaban en la build anterior y ya no.
        new_keys = {f.path.casefold() for f in plan.files}
        for key, f in old_files.items():
            if key not in new_keys:
                try:
                    (install_path / safe_rel(f["path"])).unlink(missing_ok=True)
                except ContentError:
                    pass

        save_manifest(plan.game_id, {"install_path": str(install_path), "installed": time.time(),
                                     "created_dir": created_dir, "plan": plan.to_json()})
        journal_path.unlink(missing_ok=True)
        game = self._register(plan, install_path, prefix)
        self.p.stage = "done"
        self._emit(True)
        return game

    def _register(self, plan: InstallPlan, install_path: Path, prefix: Path | None) -> Game:
        old = self.state.game(plan.game_id)
        extra = old.extra if old else {}
        extra.update({"managed": True, "build_id": plan.build_id, "version": plan.version,
                      "language": plan.language, "dlcs": plan.dlcs})
        from .. import languages
        languages.apply_to_game_info(install_path, plan.language)
        if plan.platform == "windows" and prefix is None:
            prefix = (old.prefix if old and old.prefix else paths.data_dir() / "prefixes" / plan.game_id)
        game = Game(plan.game_id, plan.title, plan.platform, install_path,
                    prefix if plan.platform == "windows" else None,
                    plan.client_id or (old.client_id if old else None),
                    plan.client_secret or (old.client_secret if old else None), extra)
        if plan.platform == "windows" and not game.client_id:
            try:
                game.client_id, game.client_secret = self.api.client_credentials(plan.game_id)
            except Exception:  # noqa: BLE001 - sin esto solo no habrá sync hasta más tarde
                pass
        self.state.save_game(game)
        return game

    @staticmethod
    def _read_journal(path: Path, build_id: str) -> tuple[dict[str, str], bool]:
        """(firma de cada archivo ya terminado, si la carpeta la creó gogl)."""
        out: dict[str, str] = {}
        created = False
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return out, created
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if "path" in rec:
                out[rec["path"]] = rec["sig"]
            created = created or rec.get("created") is True
        return out, created

    # --- descarga ---------------------------------------------------------

    def _run(self, files: list[FileEntry], root: Path, links: SecureLinks, finished,
             old_files: dict | None = None):
        if not files:
            return
        tasks = []
        for f in files:
            dest = root / f.path
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = dest.with_name(dest.name + ".gogl-part")
            fd = os.open(part, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                if os.fstat(fd).st_size != f.size:
                    os.ftruncate(fd, f.size)
            finally:
                os.close(fd)
            state = _FileState(f, dest, part)
            prev = (old_files or {}).get(f.path.casefold())
            if prev and dest.exists():
                # Trozos que ya están en la versión instalada (aunque se hayan movido).
                state.old_chunks = {c["md5"]: (c["offset"], c["size"]) for c in prev.get("chunks", [])}
            if f.chunks:
                for c in f.chunks:
                    tasks.append((state, c))
            elif f.size == 0:
                tasks.append((state, None))
            else:
                for off in range(0, f.size, V1_PIECE):
                    tasks.append((state, (off, min(V1_PIECE, f.size - off))))
            state.pending = sum(1 for t in tasks if t[0] is state)

        def work(state: _FileState, piece):
            self._check_cancel()
            if piece is None:
                pass
            elif isinstance(piece, Chunk):
                self._chunk_v2(state, piece, links)
            else:
                self._piece_v1(state, piece, links)
            with state.lock:
                state.pending -= 1
                last = state.pending == 0
            if last:
                self._finish_file(state)
                finished(state.f)
                self._add(files=1)

        with ThreadPoolExecutor(self.workers) as pool:
            futures = [pool.submit(work, s, p) for s, p in tasks]
            done, _ = wait(futures, return_when=FIRST_EXCEPTION)
            for fut in done:
                if fut.exception():
                    self.cancel.set()
                    for other in futures:
                        other.cancel()
                    raise fut.exception()

    def _chunk_v2(self, st: "_FileState", c: Chunk, links: SecureLinks):
        # Reanudación: si el .gogl-part ya tiene este trozo correcto, no se baja.
        if md5_file(st.part, c.offset, c.size) == c.md5:
            self._add(downloaded=c.compressed_size, written=c.size)
            return
        old = st.old_chunks.get(c.md5)
        if old and old[1] == c.size:
            with open(st.dest, "rb") as fh:
                fh.seek(old[0])
                data = fh.read(c.size)
            if hashlib.md5(data).hexdigest() == c.md5:
                _pwrite(st.part, c.offset, data)
                self._add(downloaded=c.compressed_size, written=c.size)
                return
        last_err = None
        for attempt in range(RETRIES):
            self._check_cancel()
            url = links.url(st.f.product_id, galaxy_path(c.compressed_md5), attempt)
            try:
                r = self.http.get(url)
                if r.status_code in (401, 403):
                    links.endpoints(st.f.product_id, refresh=True)
                    raise ContentError(f"HTTP {r.status_code}")
                if r.status_code != 200:
                    raise ContentError(f"HTTP {r.status_code}")
                raw = r.content
                if hashlib.md5(raw).hexdigest() != c.compressed_md5:
                    raise ContentError("MD5 comprimido incorrecto")
                data = zlib.decompress(raw)
                if len(data) != c.size or hashlib.md5(data).hexdigest() != c.md5:
                    raise ContentError("trozo descomprimido incorrecto")
                _pwrite(st.part, c.offset, data)
                self._add(downloaded=len(raw), written=len(data))
                return
            except (httpx.HTTPError, ContentError, zlib.error) as e:
                last_err = e
                time.sleep(min(2 ** attempt, 10))
        raise ContentError(f"No se pudo descargar {st.f.path}: {last_err}")

    def _piece_v1(self, st: "_FileState", piece: tuple[int, int], links: SecureLinks):
        off, size = piece
        blob_pid, blob_name = st.f.v1_blob.split("/", 1)
        start = st.f.v1_offset + off
        last_err = None
        for attempt in range(RETRIES):
            self._check_cancel()
            url = links.url(blob_pid, blob_name, attempt)
            try:
                with self.http.stream("GET", url, headers={"Range": f"bytes={start}-{start + size - 1}"}) as r:
                    if r.status_code in (401, 403):
                        links.endpoints(blob_pid, refresh=True)
                        raise ContentError(f"HTTP {r.status_code}")
                    # Un 200 es el main.bin entero (pueden ser GB): solo vale si es justo este trozo.
                    whole = r.status_code == 200 and start == 0 and r.headers.get("content-length") == str(size)
                    if r.status_code != 206 and not whole:
                        raise ContentError(f"HTTP {r.status_code}: el servidor no respeta el rango pedido")
                    data = r.read()
                if len(data) != size:
                    raise ContentError(f"{len(data)} bytes de {size}")
                _pwrite(st.part, off, data)
                self._add(downloaded=size, written=size)
                return
            except (httpx.HTTPError, ContentError) as e:
                last_err = e
                time.sleep(min(2 ** attempt, 10))
        raise ContentError(f"No se pudo descargar {st.f.path}: {last_err}")

    def _finish_file(self, st: "_FileState"):
        f = st.f
        if not f.chunks and f.md5 and f.size and md5_file(st.part) != f.md5:
            st.part.unlink(missing_ok=True)
            raise ContentError(f"{f.path}: el MD5 del archivo descargado no coincide")
        fd = os.open(st.part, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        if f.executable:
            st.part.chmod(st.part.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(st.part, st.dest)

    def _extract_sfc(self, f: FileEntry, root: Path):
        container, offset = f.sfc
        with open(root / container, "rb") as fh:
            fh.seek(offset)
            data = fh.read(f.size)
        if f.md5 and hashlib.md5(data).hexdigest() != f.md5:
            raise ContentError(f"{f.path}: contenido incorrecto en el contenedor de archivos pequeños")
        dest = root / f.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".gogl-part")
        tmp.write_bytes(data)
        if f.executable:
            tmp.chmod(0o755)
        os.replace(tmp, dest)

    # --- verificación -----------------------------------------------------

    def verify(self, game_id: str) -> list[str]:
        """Rutas que no coinciden con el manifiesto (faltan o están corruptas)."""
        m = load_manifest(game_id)
        if not m:
            raise ContentError("Este juego no se instaló con gogl; no hay manifiesto para verificar")
        plan = InstallPlan.from_json(m["plan"])
        root = Path(m["install_path"])
        files = [f for f in plan.files if not safe_rel(f.path).startswith(".gogl-sfc/")]
        self.p = Progress(files_total=len(files), disk_total=sum(f.size for f in files), stage="verify")
        bad = []
        for f in files:
            self._check_cancel()
            if not file_ok(root / f.path, f):
                bad.append(f.path)
            self._add(written=f.size, files=1)
        return bad

    def repair(self, game_id: str) -> list[str]:
        bad = self.verify(game_id)
        if bad:
            m = load_manifest(game_id)
            root = Path(m["install_path"])
            for p in bad:
                (root / p).unlink(missing_ok=True)
            plan = InstallPlan.from_json(m["plan"])
            # Sin manifiesto previo para estos archivos → se vuelven a descargar.
            m["plan"]["files"] = [f for f in m["plan"]["files"] if f["path"] not in set(bad)]
            save_manifest(game_id, m)
            self.install(plan, root)
        return bad


class _FileState:
    def __init__(self, f: FileEntry, dest: Path, part: Path):
        self.f, self.dest, self.part = f, dest, part
        self.pending = 0
        self.lock = threading.Lock()
        self.old_chunks: dict[str, tuple[int, int]] = {}


def _pwrite(path: Path, offset: int, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY)
    try:
        view = memoryview(data)
        while view:
            n = os.pwrite(fd, view, offset)
            view = view[n:]
            offset += n
    finally:
        os.close(fd)


def _signature(f: dict) -> str:
    if f.get("md5"):
        return f["md5"]
    return ",".join(c["md5"] for c in f.get("chunks", []))


def safe_to_delete(path: Path) -> bool:
    """Nunca borrar la raíz, el home ni una carpeta de primer o segundo nivel."""
    p = path.resolve()
    return len(p.parts) >= 4 and p != Path.home().resolve()


def dir_is_new(path: Path) -> bool:
    try:
        return not any(path.iterdir())
    except FileNotFoundError:
        return True
    except OSError:
        return False


def remove_installed_files(root: Path, plan: dict) -> None:
    """Borra solo lo que instaló gogl (archivos, enlaces y las carpetas que queden
    vacías). Lo demás que haya en la carpeta, del usuario o de otros juegos, se queda."""
    rels = [f["path"] for f in plan.get("files", [])] + [l[0] for l in plan.get("links", [])]
    dirs = {root / d for d in plan.get("dirs", [])}
    for rel in rels:
        try:
            p = root / safe_rel(rel)
        except ContentError:
            continue
        p.unlink(missing_ok=True)
        p.with_name(p.name + ".gogl-part").unlink(missing_ok=True)
        dirs.update(d for d in p.parents if root in d.parents)
    shutil.rmtree(root / ".gogl-sfc", ignore_errors=True)
    for d in sorted(dirs, key=lambda d: len(d.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


def backup_saves(g: Game) -> Path | None:
    """Copia de las carpetas de partidas antes de borrar nada (algunos juegos las
    guardan dentro de su carpeta de instalación). No necesita red: usa la
    configuración de GOG cacheada."""
    from ..saves.backup import backup_locations
    from ..saves.locations import parse_remote_config, resolve_locations
    cfg = g.extra.get("remote_config")
    if g.platform != "windows" or not cfg or not g.client_id:
        return None
    try:
        locs = resolve_locations(parse_remote_config(cfg, g.client_id), g.install_path, g.prefix,
                                 g.save_overrides)
        return backup_locations(g.game_id, locs, keep=50)
    except Exception:  # noqa: BLE001 - la copia es una red de seguridad, no debe bloquear
        return None


def uninstall(state: State, game_id: str, remove_prefix: bool = False) -> None:
    g = state.game(game_id)
    if g:
        backup_saves(g)
    m = load_manifest(game_id)
    if m:
        root = Path(m["install_path"])
        if not safe_to_delete(root):
            raise ContentError(f"Me niego a borrar {root}: parece una carpeta del sistema")
        if m.get("created_dir"):
            shutil.rmtree(root, ignore_errors=True)
        else:
            remove_installed_files(root, m["plan"])
        (manifests_dir() / f"{game_id}.json").unlink(missing_ok=True)
    if remove_prefix and g and g.prefix and safe_to_delete(g.prefix):
        shutil.rmtree(g.prefix, ignore_errors=True)
    state.remove_game(game_id)
