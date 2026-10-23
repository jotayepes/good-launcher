"""Preparación del prefijo antes de la primera partida (y tras cada actualización).

Lo mismo que hace Galaxy al instalar en Windows:
- instala los redistribuibles que pide el juego (Visual C++, DirectX, PhysX…);
- ejecuta sus scripts de instalación: el intérprete de scripts de GOG
  (goggame-<id>.script: registro, .ini, carpetas) o el instalador de soporte de
  cada producto (juegos antiguos).

Cada paso se apunta en <compatdata>/gogl-setup.json, así que solo se repite lo
que cambie. Un paso que falla se registra y no impide jugar: Proton suele
cubrir lo que falte."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import languages
from ..i18n import _
from ..content import load_manifest, redist_dir
from ..content.builds import SUPPORT_DIR
from ..content.common import InstallPlan
from ..state import Game
from . import proton as proton_mgr
from .game import compat_data_path, log_path, proton_env, split_windows_args, wait_wineserver

MARKER = "gogl-setup.json"
ISI = "__redist/ISI/scriptinterpreter.exe"
STEP_TIMEOUT = 15 * 60


@dataclass
class Step:
    key: str          # lo que se apunta como hecho
    label: str        # para el usuario
    args: list[str]   # tras «proton run»
    cwd: Path


def win_path(p: Path) -> str:
    """Ruta de Linux vista desde Wine (la unidad Z: es la raíz)."""
    return "Z:" + str(p).replace("/", "\\")


def _load_marker(compat: Path) -> dict:
    try:
        return json.loads((compat / MARKER).read_text())
    except (OSError, ValueError):
        return {}


def _save_marker(compat: Path, data: dict) -> None:
    p = compat / MARKER
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(p)


def plan_steps(game: Game, plan: InstallPlan, done: dict) -> list[Step]:
    """Pasos pendientes para este prefijo (sin ejecutar nada)."""
    steps: list[Step] = []
    root = redist_dir()
    have = set(done.get("redist", []))
    for r in plan.redist:
        exe = root / r["executable"]
        if r["id"] in have or not exe.exists():
            continue
        args = split_windows_args(r.get("arguments") or "")
        if exe.suffix.lower() == ".msi":
            argv = ["msiexec", "/i", win_path(exe), *args]
            if r["id"] == "PHYSXLEGACY":
                argv.append("/qb")  # sin esto pregunta
        else:
            argv = [str(exe), *args]
        steps.append(Step(f"redist:{r['id']}", r.get("name") or r["id"], argv, exe.parent))

    if done.get("scripts") == plan.build_id:
        return steps
    install = game.install_path
    lang = plan.language or "en-US"
    lang_name = languages.GALAXY_NAMES.get(lang, "English")
    common = [f"/DIR={win_path(install)}", f"/Language={lang_name}", f"/LANG={lang_name}",
              f"/lang-code={lang}", "/galaxyclient", f"/buildId={plan.build_id}",
              f"/versionName={plan.version}", "/nodesktopshorctut", "/nodesktopshortcut"]
    isi = root / ISI
    for pid in plan.script_products:
        if isi.exists() and (install / f"goggame-{pid}.script").exists():
            steps.append(Step("scripts", _("Script de instalación"),
                              [str(isi), "/VERYSILENT", f"/ProductId={pid}",
                               f"/supportDir={win_path(install / SUPPORT_DIR)}", *common], install))
    for c in plan.support_commands:
        exe = _support_exe(install, c)
        if exe:
            steps.append(Step("scripts", _("Instalador de soporte de GOG"),
                              [str(exe), "/VERYSILENT", f"/ProductId={c['product_id']}", *common,
                               *split_windows_args(c.get("arguments") or "")], install))
    if not any(s.key == "scripts" for s in steps):
        steps.append(Step("scripts", "", [], install))  # nada que ejecutar: solo apuntarlo
    return steps


def _support_exe(install: Path, c: dict) -> Path | None:
    base = install / SUPPORT_DIR
    for p in (base / c["product_id"] / c["executable"], base / c["executable"]):
        if p.exists():
            return p
    return None


def pending_steps(game: Game) -> list[Step]:
    if game.platform != "windows":
        return []
    m = load_manifest(game.game_id)
    if not m or Path(m["install_path"]) != game.install_path:
        return []  # vinculado desde otro lanzador: ya se preparó allí
    plan = InstallPlan.from_json(m["plan"])
    steps = plan_steps(game, plan, _load_marker(compat_data_path(game)))
    return steps


def run_setup(game: Game, pv: proton_mgr.ProtonVersion, steps: list[Step],
              status: Callable[[str], None] = lambda s: None) -> list[str]:
    """Ejecuta los pasos; devuelve los que fallaron (para avisar, no para bloquear)."""
    compat = compat_data_path(game)
    m = load_manifest(game.game_id)
    build_id = m["plan"]["build_id"] if m else ""
    marker = _load_marker(compat)
    env = proton_env(game, compat)
    failed: list[str] = []
    with open(log_path(game.game_id), "a") as log:
        for s in steps:
            if s.args:
                status(_("Instalando {name}…").format(name=s.label))
                log.write(f"[gogl] preparación: {s.label}: {' '.join(s.args)}\n")
                log.flush()
                try:
                    r = subprocess.run([str(pv.script), "run", *s.args], env=env, cwd=s.cwd,
                                       stdout=log, stderr=subprocess.STDOUT, timeout=STEP_TIMEOUT)
                    # Muchos instaladores devuelven «reinicio necesario» (3010) o «ya
                    # hay una versión más nueva» (1638): eso también es éxito.
                    if r.returncode not in (0, 1638, 3010):
                        failed.append(s.label)
                        log.write(f"[gogl] {s.label} terminó con código {r.returncode}\n")
                except subprocess.TimeoutExpired:
                    failed.append(s.label)
                    log.write(f"[gogl] {s.label}: tiempo agotado\n")
                except OSError as e:
                    failed.append(s.label)
                    log.write(f"[gogl] {s.label}: no se pudo ejecutar: {e}\n")
                wait_wineserver(pv, compat, timeout=120)
            # Se apunta aunque falle: reintentarlo en cada arranque no lo arreglaría.
            if s.key.startswith("redist:"):
                marker.setdefault("redist", []).append(s.key.split(":", 1)[1])
            else:
                marker["scripts"] = build_id
            _save_marker(compat, marker)
    return failed


def ensure_galaxy_service(game: Game, pv: proton_mgr.ProtonVersion,
                          status: Callable[[str], None] = lambda s: None) -> bool:
    """Registra en el prefijo el servicio ficticio «GalaxyCommunication» que
    algunas versiones del Galaxy SDK exigen para hablar con Comet. Una vez por prefijo."""
    from . import comet
    compat = compat_data_path(game)
    marker = _load_marker(compat)
    if marker.get("galaxy_service") == comet.VERSION:
        return True
    status(_("Preparando los logros (solo la primera vez)…"))
    with open(log_path(game.game_id), "a") as log:
        log.write("[gogl] registrando el servicio GalaxyCommunication para los logros\n")
        log.flush()
        try:
            r = subprocess.run([str(pv.script), "run", *comet.service_step_args()],
                               env=proton_env(game, compat), cwd=comet.dummy_dir(),
                               stdout=log, stderr=subprocess.STDOUT, timeout=120)
            ok = r.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as e:
            log.write(f"[gogl] no se pudo registrar el servicio: {e}\n")
            ok = False
    wait_wineserver(pv, compat, timeout=60)
    # Como los demás pasos, se apunta aunque falle (p. ej. porque el servicio ya
    # existía): repetirlo en cada arranque no lo arreglaría.
    marker["galaxy_service"] = comet.VERSION
    _save_marker(compat, marker)
    return ok


def reset(game: Game) -> None:
    """Para repetir la preparación en el próximo arranque."""
    (compat_data_path(game) / MARKER).unlink(missing_ok=True)
