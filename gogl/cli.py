"""Línea de comandos de gogl."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import selfcmd
from .api import GogApi
from .auth import AuthManager, login_url
from .games import register_installed
from .saves.engine import ConflictError
from .state import State
from .sync import SyncService, SyncUnavailable, describe

EXIT_ERROR, EXIT_CONFLICT, EXIT_UNAVAILABLE = 1, 2, 3


def _log(msg: str) -> None:
    print(f"· {msg}", file=sys.stderr)


def cmd_login(args, auth: AuthManager, **_):
    code = args.code
    if not code:
        print("1. Abre esta URL en el navegador e inicia sesión en GOG:\n")
        print(f"   {login_url()}\n")
        print("2. Al terminar acabarás en una página en blanco de embed.gog.com/on_login_success.")
        code = input("   Pega aquí la URL completa (o solo el valor de code=): ")
    user_id = auth.login_with_code(code)
    print(f"Sesión iniciada (user_id {user_id})")


def cmd_logout(args, auth: AuthManager, **_):
    auth.logout()
    print("Sesión cerrada")


def cmd_games_add(args, state: State, api: GogApi, **_):
    game = register_installed(Path(args.path), Path(args.prefix) if args.prefix else None, api,
                              args.platform)
    old = state.game(game.game_id)
    if old:
        game.extra = old.extra
    state.save_game(game)
    print(f"Registrado {game.title} ({game.game_id}) · {game.platform} · clientId {game.client_id}")


def cmd_games_list(args, state: State, **_):
    for g in state.games():
        st = state.status(g.game_id) or {}
        flag = "PENDIENTE" if st.get("pending") else ""
        print(f"{g.game_id:>12}  {g.platform:7}  {g.title}  {flag}")


def cmd_games_remove(args, state: State, **_):
    state.remove_game(args.game_id)
    print("Eliminado del registro (no se ha borrado ningún archivo)")


def cmd_games_save_path(args, state: State, **_):
    g = state.game(args.game_id)
    if not g:
        raise SyncUnavailable(f"Juego {args.game_id} no registrado")
    overrides = g.extra.setdefault("save_overrides", {})
    if args.path:
        overrides[args.location] = str(Path(args.path).expanduser().resolve())
    else:
        overrides.pop(args.location, None)
    state.save_game(g)
    print("Guardado")


def cmd_saves(args, state: State, service: SyncService, **_):
    g = state.game(args.game_id)
    if not g:
        raise SyncUnavailable(f"Juego {args.game_id} no registrado")
    print(f"{g.title} ({g.game_id})")
    for loc in service.locations(g):
        exists = "existe" if loc.path.is_dir() else "NO existe aún"
        print(f"  {loc.name}: {loc.template}\n      → {loc.path} ({exists})")
    st = state.status(g.game_id)
    if st:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(st["last_ok"])) if st["last_ok"] else "nunca"
        print(f"  último sync correcto: {when}" + ("  · PENDIENTE: " + (st["last_error"] or "") if st["pending"] else ""))


def cmd_sync(args, service: SyncService, **_):
    kw = dict(prefer=args.prefer, dry_run=args.dry_run, wait_network=0 if args.no_wait else 30)
    if args.pending:
        results = service.sync_pending(**kw)
        if not results:
            print("No hay juegos pendientes")
        failed = False
        for gid, r in results.items():
            if isinstance(r, Exception):
                failed = True
                print(f"{gid}: ERROR {r}")
            else:
                print(f"{gid}: {describe(r)}")
        return EXIT_ERROR if failed else 0
    if not args.game_id:
        raise SystemExit("Indica un juego o --pending")
    try:
        res = service.sync(args.game_id, **kw)
    except ConflictError as e:
        print("Hay conflictos: las partidas cambiaron aquí y en la nube desde el último sync.")
        for it in e.plan.conflicts:
            l = time.strftime("%Y-%m-%d %H:%M", time.localtime(it.local.mtime)) if it.local else "borrado"
            c = it.cloud.last_modified if it.cloud else "borrado"
            print(f"  {it.key}\n      local: {l}   nube: {c}")
        print("Repite con --prefer local o --prefer cloud (se hará copia de seguridad antes).")
        return EXIT_CONFLICT
    for w in res.plan.warnings:
        print(f"aviso: {w}")
    print(describe(res))
    if res.plan.ignored_cloud:
        print(f"({len(res.plan.ignored_cloud)} archivo(s) en la nube fuera de las ubicaciones conocidas, ignorados)")
    return 0


def cmd_run(args, **_):
    from . import session
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if has_display and not args.no_ui:
        try:
            from gogl_ui.session_window import run_with_window
        except ImportError:
            pass
        else:
            return run_with_window(args.game_id)
    session.install_signal_handlers()
    return session.run_game(args.game_id, session.ConsoleUI())


def cmd_watch(args, **_):
    from .session import watch
    return watch(args.game_id, args.session, args.pid, log=_log)


def cmd_proton_list(args, **_):
    from .runner import proton
    d = proton.default()
    vers = proton.installed()
    if not vers:
        print("No hay ninguna versión instalada. Usa: gogl proton install")
    for v in vers:
        print(f"{'*' if d and v.name == d.name else ' '} {v.name}   {v.path}")


def cmd_proton_available(args, **_):
    from .runner import proton
    have = {v.name for v in proton.installed()}
    for r in proton.available():
        print(f"{r.name:20} {r.size / 2**20:6.0f} MB  {'(instalado)' if r.name in have else ''}")


def cmd_proton_install(args, **_):
    from .runner import proton
    rels = proton.available()
    rel = rels[0] if not args.version else next((r for r in rels if r.name == args.version), None)
    if rel is None:
        raise SystemExit(f"No se encuentra {args.version}")
    last = [-1]

    def progress(done, total):
        pct = int(done * 100 / total) if total else 0
        if pct != last[0]:
            last[0] = pct
            print(f"\rDescargando {rel.name}: {pct}%", end="", file=sys.stderr, flush=True)

    v = proton.install(rel, progress)
    print(f"\nInstalado en {v.path}")
    if not proton.default() or len(proton.installed()) == 1 or args.default:
        proton.set_default(v.name)


def cmd_proton_default(args, **_):
    from .runner import proton
    proton.set_default(args.version)
    print(f"{args.version} es ahora la versión por defecto")


def cmd_proton_remove(args, **_):
    from .runner import proton
    proton.remove(args.version)
    print("Eliminado")


def cmd_steam_add(args, state: State, **_):
    from .steam import add_shortcut
    g = state.game(args.game_id)
    if not g:
        raise SyncUnavailable(f"Juego {args.game_id} no registrado")
    r = add_shortcut(g, force=args.force)
    print(f"{'Actualizado' if r.updated else 'Añadido'} «{g.title}» en Steam (appid {r.appid}, usuario {r.user_dir.name})")


def cmd_service(args, **_):
    from . import services
    if args.action == "install":
        print(f"Timer instalado en {services.install()}")
    else:
        services.uninstall()
        print("Timer eliminado")


def _progress_printer():
    last = [0.0]

    def show(p):
        now = time.monotonic()
        if now - last[0] < 0.5 and p.stage not in ("done",):
            return
        last[0] = now
        if p.stage == "prepare":
            msg = "Preparando…"
        elif p.stage == "verify":
            msg = f"Verificando {p.files_done}/{p.files_total} archivos"
        elif p.stage == "finalize":
            msg = "Terminando…"
        else:
            msg = (f"{p.fraction * 100:5.1f}%  {p.downloaded / 2**20:,.0f}/{p.download_total / 2**20:,.0f} MB"
                   f"  {p.speed / 2**20:5.1f} MB/s  ({p.files_done}/{p.files_total} archivos)")
        print(f"\r{msg:<80}", end="", file=sys.stderr, flush=True)
    return show


def cmd_install(args, api: GogApi, **_):
    from .content import install_game
    g = install_game(api, args.game_id, platform=args.platform, language=args.lang,
                     dlcs=None if args.dlc == "all" else [] if args.dlc == "none" else args.dlc.split(","),
                     install_path=Path(args.path).expanduser() if args.path else None,
                     progress=_progress_printer())
    print(f"\nInstalado {g.title} en {g.install_path}")


def cmd_update(args, api: GogApi, **_):
    from .content import check_update, update_game
    upd = check_update(api, args.game_id)
    if not upd:
        print("Ya está actualizado")
        return 0
    print(f"Actualizando {upd[0]} → {upd[1]}")
    update_game(api, args.game_id, progress=_progress_printer())
    print("\nActualizado")


def cmd_verify(args, api: GogApi, **_):
    from .content import Installer
    inst = Installer(api, progress=_progress_printer())
    bad = inst.repair(args.game_id) if args.repair else inst.verify(args.game_id)
    print()
    if not bad:
        print("Todos los archivos están correctos")
        return 0
    print(f"{len(bad)} archivo(s) {'reparados' if args.repair else 'dañados o ausentes'}:")
    for b in bad[:30]:
        print(f"  {b}")
    return 0 if args.repair else EXIT_ERROR


def cmd_uninstall(args, state: State, **_):
    from .content import uninstall
    g = state.game(args.game_id)
    if not g:
        raise SyncUnavailable(f"Juego {args.game_id} no registrado")
    if not args.yes:
        if input(f"¿Desinstalar {g.title} y borrar {g.install_path}? [s/N] ").strip().lower() != "s":
            return 0
    uninstall(state, args.game_id, remove_prefix=args.remove_prefix)
    print("Desinstalado")


def _size(n: int) -> str:
    return f"{n / 2**30:.1f} GB" if n >= 2**30 else f"{n / 2**20:.0f} MB"


def cmd_offline_list(args, api: GogApi, **_):
    from .content.offline import downloaded_files, list_downloads
    off = list_downloads(api, args.game_id)
    print("Instaladores offline:" if off.installers else "Sin instaladores offline")
    for b in off.installers:
        done = " ✓" if downloaded_files(b) else ""
        print(f"  {b.id:<28} {b.os:<8} {b.language:<6} {b.version:<24} {_size(b.total_size):>9}"
              f"  ({len(b.files)} archivo{'s' if len(b.files) != 1 else ''}){done}")
    print("Extras:" if off.extras else "Sin extras")
    for b in off.extras:
        done = " ✓" if downloaded_files(b) else ""
        print(f"  {b.id:<28} {b.type_label:<20} {b.name:<30} {_size(b.total_size):>9}{done}")


def cmd_offline_download(args, api: GogApi, **_):
    from .content.offline import OfflineDownloader, bundle_dir, list_downloads
    off = list_downloads(api, args.game_id)
    if args.extras:
        chosen = off.extras
    else:
        chosen = [b for b in off.installers
                  if b.os == args.os and (not args.lang or b.language == args.lang)]
    if args.ids:
        chosen = [b for b in off.installers + off.extras if b.id in args.ids.split(",")]
    if not chosen:
        print("No hay nada que coincida (mira `gogl offline list`)", file=sys.stderr)
        return EXIT_ERROR
    for b in chosen:
        print(f"{b.name} ({b.type_label if b.kind == 'extra' else b.language}) → {bundle_dir(b)}")
        OfflineDownloader(api, _progress_printer()).download(b)
        print()
    print("Descarga completada")


def cmd_achievements(args, state: State, auth: AuthManager, api: GogApi, **_):
    from . import achievements
    items = achievements.fetch(api, auth, state, args.game_id)
    if not items:
        print("Este juego no tiene logros")
        return 0
    got = [a for a in items if a.unlocked]
    print(f"{len(got)} de {len(items)} logros")
    for a in sorted(items, key=lambda a: (not a.unlocked, -(a.unlocked_at or 0), a.name)):
        mark = "✓" if a.unlocked else "·"
        when = time.strftime("  %d/%m/%Y", time.localtime(a.unlocked_at)) if a.unlocked else ""
        name = a.name if a.visible or a.unlocked else "(oculto)"
        print(f"  {mark} {name}{when}  [{a.rarity:.1f}%]")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gogl", description="Lanzador de GOG para Linux")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("login", help="iniciar sesión en GOG")
    s.add_argument("code", nargs="?", help="código o URL de on_login_success")
    s.set_defaults(func=cmd_login)
    sub.add_parser("logout", help="cerrar sesión").set_defaults(func=cmd_logout)

    games = sub.add_parser("games", help="juegos registrados").add_subparsers(dest="games_cmd", required=True)
    s = games.add_parser("add", help="registrar un juego ya instalado")
    s.add_argument("path", help="carpeta de instalación (la que contiene goggame-*.info)")
    s.add_argument("--prefix", help="prefijo de Proton/Wine (compatdata o pfx)")
    s.add_argument("--platform", choices=["windows", "linux"])
    s.set_defaults(func=cmd_games_add)
    games.add_parser("list", help="listar juegos").set_defaults(func=cmd_games_list)
    s = games.add_parser("remove", help="quitar del registro")
    s.add_argument("game_id")
    s.set_defaults(func=cmd_games_remove)
    s = games.add_parser("save-path", help="forzar la ruta local de una ubicación de partidas")
    s.add_argument("game_id")
    s.add_argument("location", help="nombre de la ubicación (ver `gogl saves`)")
    s.add_argument("path", nargs="?", help="ruta; omitir para volver a la automática")
    s.set_defaults(func=cmd_games_save_path)

    s = sub.add_parser("saves", help="mostrar dónde están las partidas de un juego")
    s.add_argument("game_id")
    s.set_defaults(func=cmd_saves)

    s = sub.add_parser("sync", help="sincronizar partidas con la nube de GOG")
    s.add_argument("game_id", nargs="?")
    s.add_argument("--pending", action="store_true", help="sincronizar todos los juegos pendientes")
    s.add_argument("--dry-run", action="store_true", help="mostrar qué se haría sin tocar nada")
    s.add_argument("--prefer", choices=["local", "cloud"], help="cómo resolver conflictos")
    s.add_argument("--no-wait", action="store_true", help="no esperar a que haya red")
    s.set_defaults(func=cmd_sync)
    s = sub.add_parser("install", help="descargar e instalar un juego de tu biblioteca")
    s.add_argument("game_id")
    s.add_argument("--platform", choices=["windows", "linux"], default="windows")
    s.add_argument("--lang", help="idioma, p. ej. es-ES (por defecto español si existe)")
    s.add_argument("--dlc", default="all", help="all, none o lista de ids separados por comas")
    s.add_argument("--path", help="carpeta de instalación (por defecto ~/Games/gogl/<juego>)")
    s.set_defaults(func=cmd_install)
    s = sub.add_parser("update", help="actualizar a la última versión (solo descarga lo cambiado)")
    s.add_argument("game_id")
    s.set_defaults(func=cmd_update)
    s = sub.add_parser("verify", help="comprobar la integridad de los archivos")
    s.add_argument("game_id")
    s.add_argument("--repair", action="store_true", help="volver a descargar lo dañado")
    s.set_defaults(func=cmd_verify)
    s = sub.add_parser("uninstall", help="desinstalar (las partidas en la nube no se tocan)")
    s.add_argument("game_id")
    s.add_argument("--remove-prefix", action="store_true", help="borrar también el prefijo de Proton")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_uninstall)

    off = sub.add_parser("offline", help="extras e instaladores offline de tu cuenta").add_subparsers(
        dest="offline_cmd", required=True)
    s = off.add_parser("list", help="qué hay para descargar")
    s.add_argument("game_id")
    s.set_defaults(func=cmd_offline_list)
    s = off.add_parser("download", help="descargar (por defecto, el instalador de Windows)")
    s.add_argument("game_id")
    s.add_argument("--os", choices=["windows", "linux", "mac"], default="windows")
    s.add_argument("--lang", help="idioma del instalador, p. ej. es")
    s.add_argument("--extras", action="store_true", help="todos los extras en vez del instalador")
    s.add_argument("--ids", help="ids concretos separados por comas (ver `list`)")
    s.set_defaults(func=cmd_offline_download)

    s = sub.add_parser("achievements", help="logros del juego (conseguidos y pendientes)")
    s.add_argument("game_id")
    s.set_defaults(func=cmd_achievements)

    s = sub.add_parser("run", help="jugar: sincroniza, lanza el juego y vuelve a sincronizar")
    s.add_argument("game_id")
    s.add_argument("--no-ui", action="store_true", help="sin ventana (preguntas por terminal)")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("watch")  # interno: vigilante post-sync lanzado con systemd-run
    s.add_argument("game_id")
    s.add_argument("--session", type=int, required=True)
    s.add_argument("--pid", type=int, required=True)
    s.set_defaults(func=cmd_watch)

    pr = sub.add_parser("proton", help="versiones de Proton-GE gestionadas por gogl").add_subparsers(
        dest="proton_cmd", required=True)
    pr.add_parser("list", help="versiones instaladas").set_defaults(func=cmd_proton_list)
    pr.add_parser("available", help="versiones publicadas").set_defaults(func=cmd_proton_available)
    s = pr.add_parser("install", help="descargar e instalar (por defecto la última)")
    s.add_argument("version", nargs="?")
    s.add_argument("--default", action="store_true", help="usarla por defecto")
    s.set_defaults(func=cmd_proton_install)
    s = pr.add_parser("default", help="elegir la versión por defecto")
    s.add_argument("version")
    s.set_defaults(func=cmd_proton_default)
    s = pr.add_parser("remove", help="borrar una versión")
    s.add_argument("version")
    s.set_defaults(func=cmd_proton_remove)

    st = sub.add_parser("steam", help="integración con Steam").add_subparsers(dest="steam_cmd", required=True)
    s = st.add_parser("add", help="añadir el juego a Steam como acceso directo")
    s.add_argument("game_id")
    s.add_argument("--force", action="store_true", help="aunque Steam esté abierto")
    s.set_defaults(func=cmd_steam_add)

    s = sub.add_parser("service", help="timer que reintenta las subidas pendientes cada 15 min")
    s.add_argument("action", choices=["install", "uninstall"])
    s.set_defaults(func=cmd_service)
    return p


def main(argv: list[str] | None = None) -> int:
    selfcmd.register()
    args = build_parser().parse_args(argv)
    state = State()
    auth = AuthManager()
    api = GogApi(auth)
    service = SyncService(state, auth, api, log=_log)
    try:
        return args.func(args, state=state, auth=auth, api=api, service=service) or 0
    except SyncUnavailable as e:
        print(e, file=sys.stderr)
        return EXIT_UNAVAILABLE
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001 - mensaje legible en vez de traza
        print(f"Error: {e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
