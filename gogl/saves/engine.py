"""Motor de sincronización: merge a 3 bandas por hash.

Para cada archivo comparamos tres cosas:
  B  base: (md5 del contenido local, hash en la nube) acordados en el último sync
  L  md5 del contenido local actual
  C  hash del objeto en la nube ahora mismo

Sin fechas de por medio: un reloj desfasado, una zona horaria o un juego que
"toca" el archivo sin cambiarlo no pueden invertir la dirección del sync.

Reglas de seguridad:
  * Un borrado solo se propaga si hay base que demuestre que el archivo existía.
  * Si en una ubicación desaparecen TODOS los archivos de un lado (prefijo
    recreado, carpeta movida, listado vacío por un fallo), no se propaga nada:
    esa ubicación se trata como si no hubiera base.
  * Si hay cualquier conflicto no se aplica nada hasta que el usuario elija
    (por juego, no por archivo), para no mezclar partidas de dos máquinas.
  * Las subidas se verifican volviendo a listar la nube; la base solo avanza
    cuando la nube confirma el hash.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from ..i18n import _
from ..state import BaseEntry, Game, State
from .cloud import CloudFile, CloudStorage, gzip_bytes, md5
from .locations import SaveLocation, resolve_case_insensitive


class Action(str, Enum):
    NOOP = "noop"
    ADOPT = "adopt"              # ya son iguales: solo se actualiza la base
    UPLOAD = "upload"
    DOWNLOAD = "download"
    DELETE_CLOUD = "delete_cloud"
    DELETE_LOCAL = "delete_local"
    DROP_BASE = "drop_base"      # borrado en ambos lados
    CONFLICT = "conflict"


@dataclass
class LocalFile:
    key: str
    path: Path
    size: int
    mtime: float
    md5: str


@dataclass
class Item:
    action: Action
    norm: str
    local: LocalFile | None
    cloud: CloudFile | None
    reason: str = ""

    @property
    def key(self) -> str:
        return self.cloud.key if self.cloud else self.local.key if self.local else self.norm


@dataclass
class SyncPlan:
    items: list[Item]
    warnings: list[str] = field(default_factory=list)
    ignored_cloud: list[str] = field(default_factory=list)

    def of(self, *actions: Action) -> list[Item]:
        return [i for i in self.items if i.action in actions]

    @property
    def conflicts(self) -> list[Item]:
        return self.of(Action.CONFLICT)

    @property
    def changes(self) -> list[Item]:
        return [i for i in self.items if i.action not in (Action.NOOP,)]


class ConflictError(Exception):
    def __init__(self, plan: SyncPlan):
        super().__init__(_("{n} archivo(s) en conflicto").format(n=len(plan.conflicts)))
        self.plan = plan


class SyncError(Exception):
    pass


@dataclass
class SyncResult:
    plan: SyncPlan
    uploaded: list[str] = field(default_factory=list)
    downloaded: list[str] = field(default_factory=list)
    deleted_cloud: list[str] = field(default_factory=list)
    deleted_local: list[str] = field(default_factory=list)
    backup: Path | None = None
    dry_run: bool = False


def norm_key(key: str) -> str:
    return key.replace("\\", "/").casefold()


def location_of(key: str, locations: list[SaveLocation]) -> tuple[SaveLocation, str] | None:
    """Ubicación a la que pertenece una clave «nombre/ruta». Coincidencia exacta
    del nombre seguido de «/»: un simple startswith mezclaría «saves» con «saves2»."""
    k = key.replace("\\", "/")
    for loc in locations:
        prefix = loc.name + "/"
        if k[:len(prefix)].casefold() == prefix.casefold():
            return loc, k[len(prefix):]
    return None


def safe_cloud_rel(rel: str) -> bool:
    """La parte de la clave tras la ubicación no puede salir de ella: las claves
    las pone quien escriba en la nube (otro equipo, un juego), no nosotros."""
    parts = rel.replace("\\", "/").split("/")
    return bool(rel) and not rel.startswith(("/", "\\")) and ".." not in parts and "\x00" not in rel


# --- escaneo local ------------------------------------------------------------

def scan_local(locations: list[SaveLocation], state: State | None = None) -> dict[str, LocalFile]:
    out: dict[str, LocalFile] = {}
    for loc in locations:
        if not loc.path.is_dir():
            continue
        for root, dirs, files in os.walk(loc.path):
            dirs[:] = [d for d in dirs if not d.startswith(".gogl-")]
            for name in files:
                if name.startswith(".gogl-tmp-"):
                    continue
                p = Path(root) / name
                try:
                    st = p.stat()
                except OSError:
                    continue
                h = state.cached_md5(p, st) if state else None
                if h is None:
                    h = md5(p.read_bytes())
                    if state:
                        state.store_md5(p, st, h)
                rel = p.relative_to(loc.path).as_posix()
                key = f"{loc.name}/{rel}"
                out[norm_key(key)] = LocalFile(key, p, st.st_size, st.st_mtime, h)
    return out


# --- planificación (pura, sin E/S de red) -------------------------------------

def plan_sync(local: dict[str, LocalFile], cloud: dict[str, CloudFile], base: dict[str, BaseEntry],
              locations: list[SaveLocation]) -> SyncPlan:
    warnings: list[str] = []
    ignored: list[str] = []

    cloud_n: dict[str, CloudFile] = {}
    for k, cf in cloud.items():
        found = location_of(k, locations)
        if found is None or not safe_cloud_rel(found[1]):
            ignored.append(k)
        else:
            cloud_n[norm_key(k)] = cf

    # Guardia contra borrados masivos.
    base = dict(base)
    for loc in locations:
        p = loc.name.casefold() + "/"
        in_base = [k for k in base if k.startswith(p)]
        has_local = any(k.startswith(p) for k in local)
        has_cloud = any(k.startswith(p) for k in cloud_n)
        if not in_base or (not has_local and not has_cloud):
            continue
        if not has_local:
            warnings.append(_("«{name}»: no hay archivos locales ({path}); no se borrará nada en la nube, "
                              "se restaurará desde ella").format(name=loc.name, path=loc.path))
            for k in in_base:
                del base[k]
        elif not has_cloud:
            warnings.append(_("«{name}»: la nube está vacía; no se borrará nada local, se volverá a subir")
                            .format(name=loc.name))
            for k in in_base:
                del base[k]

    items: list[Item] = []
    for k in sorted(set(local) | set(cloud_n) | set(base)):
        L, C, B = local.get(k), cloud_n.get(k), base.get(k)
        items.append(_decide(k, L, C, B))
    return SyncPlan(items, warnings, ignored)


def _same_content(L: LocalFile, C: CloudFile) -> bool:
    try:
        return md5(gzip_bytes(L.path.read_bytes())) == C.hash
    except OSError:
        return False


NO_HISTORY = _("existe en ambos lados sin historial")


def _decide(k: str, L: LocalFile | None, C: CloudFile | None, B: BaseEntry | None) -> Item:
    if B is None:
        if L and C:
            if _same_content(L, C):
                return Item(Action.ADOPT, k, L, C, _("mismo contenido"))
            # Puede ser el mismo contenido comprimido de otra forma (subido por
            # Galaxy en Windows): el Syncer lo resuelve descargando y comparando.
            return Item(Action.CONFLICT, k, L, C, NO_HISTORY)
        if L:
            return Item(Action.UPLOAD, k, L, C, _("solo local"))
        return Item(Action.DOWNLOAD, k, L, C, _("solo en la nube"))

    l_changed = L is None or L.md5 != B.local_md5
    c_changed = C is None or C.hash != B.cloud_hash
    if L is None and C is None:
        return Item(Action.DROP_BASE, k, L, C, _("borrado en ambos lados"))
    if not l_changed and not c_changed:
        return Item(Action.NOOP, k, L, C)
    if l_changed and not c_changed:
        return (Item(Action.UPLOAD, k, L, C, _("modificado en local")) if L
                else Item(Action.DELETE_CLOUD, k, L, C, _("borrado en local")))
    if c_changed and not l_changed:
        return (Item(Action.DOWNLOAD, k, L, C, _("modificado en la nube")) if C
                else Item(Action.DELETE_LOCAL, k, L, C, _("borrado en la nube")))
    if L and C and _same_content(L, C):
        return Item(Action.ADOPT, k, L, C, _("mismo cambio en ambos lados"))
    return Item(Action.CONFLICT, k, L, C, _("modificado en ambos lados"))


def resolve_conflicts(plan: SyncPlan, prefer: str) -> None:
    for it in plan.conflicts:
        if prefer == "local":
            it.action = Action.UPLOAD if it.local else Action.DELETE_CLOUD
        elif prefer == "cloud":
            it.action = Action.DOWNLOAD if it.cloud else Action.DELETE_LOCAL
        else:
            raise ValueError(prefer)
        it.reason += " → " + (_("se mantiene local") if prefer == "local" else _("se mantiene nube"))


# --- escritura atómica --------------------------------------------------------

def atomic_write(path: Path, data: bytes, mtime: float | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".gogl-tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mtime:
            os.utime(tmp, (mtime, mtime))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _prune_empty_dirs(path: Path, stop: Path) -> None:
    d = path.parent
    while d != stop and stop in d.parents:
        try:
            d.rmdir()
        except OSError:
            return
        d = d.parent


# --- ejecución ----------------------------------------------------------------

class Syncer:
    def __init__(self, state: State, game: Game, locations: list[SaveLocation], cloud: CloudStorage,
                 log: Callable[[str], None] = lambda _: None,
                 backup: Callable[[], Path | None] | None = None):
        self.state = state
        self.game = game
        self.locations = locations
        self.cloud = cloud
        self.log = log
        self.backup = backup
        self._downloaded: dict[str, tuple[bytes, float | None]] = {}

    def analyze(self) -> SyncPlan:
        local = scan_local(self.locations, self.state)
        remote = self.cloud.list()
        plan = plan_sync(local, remote, self.state.base(self.game.game_id), self.locations)
        # Conflictos "sin historial": puede que sea el mismo contenido con otra
        # compresión. Lo comprobamos descargando (las partidas pesan poco).
        for it in plan.conflicts:
            if it.local and it.cloud and it.reason == NO_HISTORY:
                data, mt = self.cloud.download(it.cloud)
                self._downloaded[it.norm] = (data, mt)
                if md5(data) == it.local.md5:
                    it.action, it.reason = Action.ADOPT, _("mismo contenido (otra compresión)")
        for w in plan.warnings:
            self.log(_("aviso: {warning}").format(warning=w))
        return plan

    def run(self, prefer: str | None = None, dry_run: bool = False,
            plan: SyncPlan | None = None) -> SyncResult:
        plan = plan or self.analyze()
        if plan.conflicts:
            if prefer is None:
                raise ConflictError(plan)
            resolve_conflicts(plan, prefer)
        res = SyncResult(plan, dry_run=dry_run)
        if dry_run:
            return res

        gid = self.game.game_id
        destructive = [i for i in plan.items
                       if i.action == Action.DELETE_LOCAL or (i.action == Action.DOWNLOAD and i.local)]
        if destructive and self.backup:
            res.backup = self.backup()
            if res.backup:
                self.log(_("copia de seguridad: {path}").format(path=res.backup))

        for it in plan.items:
            if it.action == Action.ADOPT:
                self.state.set_base(gid, it.norm, it.local.md5, it.cloud.hash)
            elif it.action == Action.DROP_BASE:
                self.state.drop_base(gid, it.norm)
            elif it.action == Action.DOWNLOAD:
                self._download(it)
                res.downloaded.append(it.key)
            elif it.action == Action.DELETE_LOCAL:
                self.log(_("borrando local {file}").format(file=it.key))
                loc = location_of(it.local.key, self.locations)
                it.local.path.unlink(missing_ok=True)
                if loc:
                    _prune_empty_dirs(it.local.path, loc[0].path)
                self.state.drop_base(gid, it.norm)
                res.deleted_local.append(it.key)

        expected: dict[str, tuple[Item, str]] = {}
        for it in plan.of(Action.UPLOAD):
            expected[it.norm] = (it, self._upload(it))
        for it in plan.of(Action.DELETE_CLOUD):
            self.log(_("borrando en la nube {file}").format(file=it.key))
            self.cloud.delete(it.cloud.key)

        if expected or plan.of(Action.DELETE_CLOUD):
            self._verify(plan, expected, res)
        return res

    def _download(self, it: Item) -> None:
        self.log(_("descargando {file}").format(file=it.cloud.key))
        data, mtime = self._downloaded.pop(it.norm, None) or self.cloud.download(it.cloud)
        if it.local:
            path = it.local.path
        else:
            found = location_of(it.cloud.key, self.locations)
            loc, rel = found
            if not safe_cloud_rel(rel):
                raise SyncError(_("Ruta sospechosa en la nube: {file}").format(file=it.cloud.key))
            path = resolve_case_insensitive(loc.path / rel)
            base = loc.path.resolve()
            if base not in path.resolve().parents:
                raise SyncError(_("{file} saldría de la carpeta de partidas").format(file=it.cloud.key))
        atomic_write(path, data, mtime)
        st = path.stat()
        h = md5(data)
        self.state.store_md5(path, st, h)
        self.state.set_base(self.game.game_id, it.norm, h, it.cloud.hash)

    def _upload(self, it: Item) -> str:
        self.log(_("subiendo {file}").format(file=it.local.key))
        data = it.local.path.read_bytes()
        it.local.md5 = md5(data)  # por si cambió desde el escaneo
        key = it.cloud.key if it.cloud else it.local.key
        return self.cloud.upload(key, data, it.local.mtime)

    def _verify(self, plan: SyncPlan, expected: dict[str, tuple[Item, str]], res: SyncResult,
                attempts: int = 3) -> None:
        gid = self.game.game_id
        deletes = {i.norm: i for i in plan.of(Action.DELETE_CLOUD)}
        for attempt in range(attempts):
            listing = {norm_key(k): v for k, v in self.cloud.list().items()}
            bad_up = {}
            for n, (it, h) in expected.items():
                cf = listing.get(n)
                if cf and cf.hash == h:
                    self.state.set_base(gid, n, it.local.md5, cf.hash)
                    res.uploaded.append(it.key)
                else:
                    bad_up[n] = (it, h)
            bad_del = {}
            for n, it in deletes.items():
                if n in listing:
                    bad_del[n] = it
                else:
                    self.state.drop_base(gid, n)
                    res.deleted_cloud.append(it.key)
            if not bad_up and not bad_del:
                self.log(_("verificado: la nube coincide con lo subido"))
                return
            if attempt == attempts - 1:
                break
            self.log(_("la nube aún no refleja {n} cambio(s); reintentando").format(n=len(bad_up) + len(bad_del)))
            for n, (it, _h) in bad_up.items():
                bad_up[n] = (it, self._upload(it))
            for it in bad_del.values():
                self.cloud.delete(it.cloud.key)
            expected, deletes = bad_up, bad_del
        names = [it.key for it, _h in bad_up.values()] + [it.key for it in bad_del.values()]
        raise SyncError(_("La nube no confirmó estos cambios: {files}").format(files=", ".join(names)))
