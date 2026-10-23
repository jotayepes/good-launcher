"""Sesión de juego: pre-sync → juego → esperar a que termine de verdad → post-sync.

El caso difícil es el modo juego de SteamOS: al pulsar «Salir del
juego», Steam mata todo el árbol de procesos que lanzó, incluido el lanzador,
y la partida nunca se sube. Aquí, antes de arrancar el juego, se lanza un
*vigilante* como unidad transitoria de systemd del usuario (`systemd-run
--user`). Queda fuera del cgroup de Steam, así que sobrevive. Espera a que
muera el wrapper; si este no llegó a completar el post-sync, lo hace él.
"""

from __future__ import annotations

import os
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Protocol

from . import APP_NAME
from .api import GogApi
from .auth import AuthManager
from .runner import proton as proton_mgr
from .selfcmd import cli_command
from .runner.game import (LaunchError, build_launch, init_prefix, open_log, prefix_ready,
                          wait_wineserver)
from .saves.engine import ConflictError, SyncPlan
from .state import State
from .sync import Offline, SyncService, SyncUnavailable, describe

PRE, POST = "pre", "post"


class Cancelled(Exception):
    pass


_current: subprocess.Popen | None = None


def terminate_current(*_args) -> None:
    """Steam pidió cerrar: cerramos el juego y seguimos al post-sync. Si después
    nos matan sin más, el vigilante termina el trabajo."""
    if _current is not None and _current.poll() is None:
        try:
            _current.terminate()
        except ProcessLookupError:
            pass


def install_signal_handlers() -> None:
    """Solo funciona desde el hilo principal; la interfaz Qt lo llama ella misma."""
    signal.signal(signal.SIGTERM, terminate_current)
    signal.signal(signal.SIGHUP, terminate_current)


class SessionUI(Protocol):
    def status(self, text: str) -> None: ...
    def log(self, text: str) -> None: ...
    def ask_conflict(self, title: str, plan: SyncPlan, phase: str) -> str | None: ...
    def ask_offline(self, phase: str) -> str: ...            # retry | continue | cancel
    def ask_error(self, message: str, phase: str) -> str: ...  # retry | continue | cancel
    def error(self, message: str) -> None: ...
    def hide(self) -> None: ...
    def show(self) -> None: ...
    def achievement(self, name: str, description: str, icon: str | None) -> None: ...


class ConsoleUI:
    """Sin interfaz gráfica: pregunta por terminal si la hay; si no, opciones seguras."""

    def __init__(self):
        self.tty = sys.stdin.isatty()

    def status(self, text):
        print(f"» {text}", file=sys.stderr)

    def log(self, text):
        print(f"  {text}", file=sys.stderr)

    def _ask(self, prompt: str, options: dict[str, str], default: str) -> str:
        if not self.tty:
            return default
        keys = "/".join(options)
        while True:
            a = input(f"{prompt} [{keys}] ").strip().lower()
            if a in options:
                return options[a]

    def ask_conflict(self, title, plan, phase):
        for it in plan.conflicts:
            print(f"  conflicto: {it.key}", file=sys.stderr)
        return self._ask("¿Conservar (l)ocal, (n)ube o (s)altar?",
                         {"l": "local", "n": "cloud", "s": None}, None)

    def ask_offline(self, phase):
        return self._ask("Sin conexión. ¿(r)eintentar, (c)ontinuar sin sincronizar o (s)alir?",
                         {"r": "retry", "c": "continue", "s": "cancel"}, "continue")

    def ask_error(self, message, phase):
        print(f"  error: {message}", file=sys.stderr)
        return self._ask("¿(r)eintentar, (c)ontinuar o (s)alir?",
                         {"r": "retry", "c": "continue", "s": "cancel"}, "continue")

    def error(self, message):
        print(f"Error: {message}", file=sys.stderr)

    def hide(self):
        pass

    def show(self):
        pass

    def achievement(self, name, description, icon):
        print(f"🏆 Logro conseguido: {name}", file=sys.stderr)
        notify_achievement(name, description, icon)


def _sync_phase(service: SyncService, game, ui: SessionUI, phase: str) -> bool:
    """True si quedó sincronizado. Lanza Cancelled si el usuario aborta."""
    prefer = None
    ui.status("Descargando partidas de la nube…" if phase == PRE else "Subiendo partidas a la nube…")
    while True:
        try:
            res = service.sync(game.game_id, prefer=prefer, wait_network=20)
            ui.log(describe(res))
            return True
        except ConflictError as e:
            prefer = ui.ask_conflict(game.title, e.plan, phase)
            if prefer is None:
                ui.log("conflicto sin resolver: queda pendiente")
                return False
        except Offline:
            choice = ui.ask_offline(phase)
            if choice == "cancel":
                raise Cancelled()
            if choice == "continue":
                return False
        except SyncUnavailable as e:
            ui.log(str(e))  # p. ej. el juego no tiene partidas en la nube
            return True
        except Exception as e:  # noqa: BLE001
            choice = ui.ask_error(str(e), phase)
            if choice == "cancel":
                raise Cancelled()
            if choice == "continue":
                return False


def _prepare(game, pv, ui: SessionUI) -> None:
    """Redistribuibles y scripts de instalación de GOG, solo lo que falte."""
    from .runner.setup import pending_steps, run_setup
    try:
        steps = pending_steps(game)
    except Exception as e:  # noqa: BLE001 - nunca debe impedir jugar
        ui.log(f"aviso: no se pudo comprobar la preparación del juego: {e}")
        return
    if not any(s.args for s in steps):
        if steps:
            run_setup(game, pv, steps)
        return
    ui.status("Instalando componentes de Windows que necesita el juego (solo la primera vez)…")
    try:
        failed = run_setup(game, pv, steps, ui.log)
    except Exception as e:  # noqa: BLE001 - nunca debe impedir jugar
        ui.log(f"aviso: falló la preparación del juego: {e}")
        return
    if failed:
        ui.log("aviso: no terminó: " + ", ".join(failed) + " (detalles en el registro del juego)")


def run_game(game_id: str, ui: SessionUI) -> int:
    state = State()
    auth = AuthManager()
    service = SyncService(state, auth, GogApi(auth), log=ui.log)
    game = state.game(game_id)
    if game is None:
        ui.error(f"Juego {game_id} no registrado")
        return 1
    try:
        spec = build_launch(game)
    except (LaunchError, OSError) as e:
        ui.error(str(e))
        return 1
    syncable = game.platform == "windows" and bool(game.client_id) and auth.is_logged_in()

    try:
        if spec.proton and not prefix_ready(spec.compat):
            ui.status("Preparando el prefijo de Proton (solo la primera vez)…")
            try:
                init_prefix(game, spec.proton)
            except (OSError, subprocess.SubprocessError) as e:
                # Proton lo vuelve a intentar al arrancar el juego.
                ui.log(f"aviso: no se pudo preparar el prefijo: {e}")
        if spec.proton:
            _prepare(game, spec.proton, ui)
        if syncable:
            _sync_phase(service, game, ui, PRE)
    except Cancelled:
        return 0

    sid = state.open_session(game_id, os.getpid(), spec.proton.name if spec.proton else None,
                             str(spec.compat) if spec.compat else None)
    if syncable:
        if not spawn_watcher(game_id, sid, os.getpid()):
            ui.log("aviso: sin systemd-run; si Steam cierra el lanzador, la subida quedará pendiente")

    global _current
    log = open_log(game_id)
    tracker = _start_achievements(game, spec, auth, ui, log)
    ui.status(f"Jugando a {game.title}")
    ui.hide()
    if spec.compat:
        from . import languages
        languages.apply_to_prefix(spec.compat / "pfx", game.extra.get("language"))
    try:
        _current = subprocess.Popen(spec.argv, env=spec.env, cwd=spec.cwd, stdout=log,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    except OSError as e:
        if tracker:
            tracker.stop()
        log.close()
        state.update_session(sid, ended=time.time(), post_synced=1, note=f"no arrancó: {e}")
        ui.show()
        ui.error(f"No se pudo arrancar el juego: {e}")
        return 1
    rc = _current.wait()
    _current = None
    log.write(f"[gogl] el juego terminó con código {rc}\n")
    ui.show()
    ui.status("Esperando a que el juego se cierre del todo…")
    wait_wineserver(spec.proton, spec.compat, timeout=600)
    state.update_session(sid, ended=time.time())
    if tracker:
        tracker.finish(ui)
    log.close()

    if syncable:
        try:
            ok = _sync_phase(service, game, ui, POST)
        except Cancelled:
            ok = False
        state.update_session(sid, post_synced=1, note="ok" if ok else "pendiente")
    return rc


# --- logros ---------------------------------------------------------------------

class _Achievements:
    """Comet durante la partida + aviso de cada logro + resumen al terminar."""

    def __init__(self, game, comet_proc, known: dict):
        self.game, self.comet, self.known = game, comet_proc, known

    def stop(self):
        self.comet.stop()

    def finish(self, ui: SessionUI):
        if self.comet.running():
            ui.status("Guardando los logros…")
        self.comet.stop()
        got = self.comet.unlocked
        if got:
            names = ", ".join(name for _key, name in got)
            ui.status(f"Has conseguido {len(got)} logro{'s' if len(got) != 1 else ''}: {names}")
            time.sleep(3)  # que dé tiempo a leerlo antes de que se cierre la ventana
        # Refresca la copia local para que la ficha muestre lo nuevo.
        from . import achievements
        try:
            auth = AuthManager()
            achievements.fetch(GogApi(auth), auth, State(), self.game.game_id)
        except Exception:  # noqa: BLE001 - sin red se verá la próxima vez
            pass


def _start_achievements(game, spec, auth: AuthManager, ui: SessionUI, log) -> _Achievements | None:
    """Arranca Comet si el juego puede conseguir logros. Nunca impide jugar."""
    from .runner import comet
    if game.platform != "windows" or not spec.proton or not auth.is_logged_in():
        return None
    if not comet.enabled(game) or not comet.uses_galaxy_sdk(game.install_path):
        return None
    try:
        comet.ensure()
        from .runner.setup import ensure_galaxy_service
        ensure_galaxy_service(game, spec.proton, ui.status)
        from .library import cached_username
        username = cached_username(GogApi(auth)) or "gogl"
        tokens = auth.galaxy_tokens()
    except Exception as e:  # noqa: BLE001
        ui.log(f"aviso: los logros no se registrarán en esta partida: {e}")
        return None

    from . import achievements
    known = {a.key: a for a in achievements.cached(State(), game.game_id) or []}

    def refresh_known():
        # En otro hilo (y con su propia conexión a la base de datos): solo hace
        # falta para el icono y la descripción de la notificación.
        try:
            a_auth = AuthManager()
            for a in achievements.fetch(GogApi(a_auth), a_auth, State(), game.game_id):
                known[a.key] = a
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=refresh_known, daemon=True).start()

    def on_unlock(key: str, name: str):
        a = known.get(key)
        icon = achievement_icon(a.image_unlocked) if a else None
        ui.achievement(a.name if a else name, a.description if a else "", icon)

    proc = comet.Comet(tokens, username, log, on_unlock)
    try:
        proc.start()
    except OSError as e:
        ui.log(f"aviso: no se pudo arrancar Comet: {e}")
        return None
    log.write(f"[gogl] Comet {comet.VERSION} activo: los logros se registran mientras juegas\n")
    log.flush()
    return _Achievements(game, proc, known)


def achievement_icon(url: str) -> str | None:
    """Descarga (o reutiliza) el icono de un logro para la notificación."""
    if not url:
        return None
    import hashlib
    import httpx
    from . import paths
    d = paths.cache_dir() / "achievements"
    d.mkdir(parents=True, exist_ok=True)
    p = d / (hashlib.sha1(url.encode()).hexdigest() + Path(url).suffix)
    if p.exists():
        return str(p)
    try:
        r = httpx.get(url, timeout=3, follow_redirects=True)
        if r.status_code == 200:
            p.write_bytes(r.content)
            return str(p)
    except httpx.HTTPError:
        pass
    return None


def notify_achievement(name: str, description: str, icon: str | None) -> None:
    if shutil.which("notify-send"):
        args = ["notify-send", "-a", APP_NAME, "-i", icon or "trophy-gold"]
        subprocess.run([*args, f"🏆 {name}", description or "Logro conseguido"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- vigilante -----------------------------------------------------------------

def spawn_watcher(game_id: str, sid: int, pid: int) -> bool:
    cmd = [*cli_command(), "watch", game_id, "--session", str(sid), "--pid", str(pid)]
    if shutil.which("systemd-run"):
        envs = [f"--setenv={k}={os.environ[k]}" for k in
                ("GOGL_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")
                if k in os.environ]
        try:
            r = subprocess.run(["systemd-run", "--user", "--collect", "--quiet",
                                f"--unit=gogl-watch-{game_id}-{sid}", *envs, "--", *cmd],
                               timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
    subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return False


def wait_pid(pid: int) -> None:
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    except (OSError, AttributeError):
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(2)
    try:
        select.select([fd], [], [])
    finally:
        os.close(fd)


def notify(summary: str, body: str = "") -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", APP_NAME, summary, body],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def watch(game_id: str, sid: int, pid: int, log=print) -> int:
    wait_pid(pid)
    time.sleep(1)  # margen para que el wrapper confirme su post-sync en la base de datos
    state = State()
    s = state.session(sid)
    if s is None or s["post_synced"]:
        return 0
    log("el lanzador terminó sin subir las partidas; me encargo yo")
    pv = proton_mgr.get(s["proton"]) if s["proton"] else None
    wait_wineserver(pv, Path(s["compat"]) if s["compat"] else None, timeout=900)
    auth = AuthManager()
    service = SyncService(state, auth, GogApi(auth), log=log)
    game = state.game(game_id)
    title = game.title if game else game_id
    try:
        res = service.sync(game_id, wait_network=180, lock_timeout=1800)
        state.update_session(sid, post_synced=2, note="vigilante: ok")
        log(describe(res))
        if res.uploaded or res.deleted_cloud:
            notify("Partidas subidas", title)
        return 0
    except Offline:
        state.update_session(sid, post_synced=2, note="vigilante: sin conexión")
        notify("Partidas pendientes de subir", f"{title}: se subirán cuando haya conexión")
    except SyncUnavailable as e:  # el juego no tiene sincronización: nada pendiente
        state.update_session(sid, post_synced=2, note=f"vigilante: {e}")
        return 0
    except ConflictError:
        state.update_session(sid, post_synced=2, note="vigilante: conflicto")
        notify("Conflicto de partidas", f"{title}: ábrelo en {APP_NAME} para elegir qué versión conservar")
    except Exception as e:  # noqa: BLE001
        state.update_session(sid, post_synced=2, note=f"vigilante: {e}")
        notify("Partidas pendientes de subir", f"{title}: se reintentará más tarde")
    return 1
