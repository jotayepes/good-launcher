"""Línea de comandos de gogl."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import selfcmd
from .api import GogApi
from .i18n import _, n_
from .auth import AuthManager, login_url
from .games import register_installed
from .saves.engine import ConflictError
from .state import State
from .sync import SyncService, SyncUnavailable, describe

EXIT_ERROR, EXIT_CONFLICT, EXIT_UNAVAILABLE = 1, 2, 3


def _log(msg: str) -> None:
    print(f"· {msg}", file=sys.stderr)


def cmd_login(args, auth: AuthManager, **_kw):
    code = args.code
    if not code:
        print(_("1. Abre esta URL en el navegador e inicia sesión en GOG:") + "\n")
        print(f"   {login_url()}\n")
        print(_("2. Al terminar acabarás en una página en blanco de embed.gog.com/on_login_success."))
        code = input("   " + _("Pega aquí la URL completa (o solo el valor de code=): "))
    user_id = auth.login_with_code(code)
    print(_("Sesión iniciada (user_id {id})").format(id=user_id))


def cmd_logout(args, auth: AuthManager, **_kw):
    auth.logout()
    print(_("Sesión cerrada"))


def cmd_games_add(args, state: State, api: GogApi, **_kw):
    game = register_installed(Path(args.path), Path(args.prefix) if args.prefix else None, api,
                              args.platform)
    old = state.game(game.game_id)
    if old:
        game.extra = old.extra
    state.save_game(game)
    print(_("Registrado {title} ({id}) · {platform} · clientId {client}").format(
        title=game.title, id=game.game_id, platform=game.platform, client=game.client_id))


def cmd_games_list(args, state: State, **_kw):
    for g in state.games():
        st = state.status(g.game_id) or {}
        flag = _("PENDIENTE") if st.get("pending") else ""
        print(f"{g.game_id:>12}  {g.platform:7}  {g.title}  {flag}")


def cmd_games_remove(args, state: State, **_kw):
    state.remove_game(args.game_id)
    print(_("Eliminado del registro (no se ha borrado ningún archivo)"))


def cmd_games_save_path(args, state: State, **_kw):
    g = state.game(args.game_id)
    if not g:
        raise SyncUnavailable(_("Juego {id} no registrado").format(id=args.game_id))
    overrides = g.extra.setdefault("save_overrides", {})
    if args.path:
        overrides[args.location] = str(Path(args.path).expanduser().resolve())
    else:
        overrides.pop(args.location, None)
    state.save_game(g)
    print(_("Guardado"))


def cmd_saves(args, state: State, service: SyncService, **_kw):
    g = state.game(args.game_id)
    if not g:
        raise SyncUnavailable(_("Juego {id} no registrado").format(id=args.game_id))
    print(f"{g.title} ({g.game_id})")
    for loc in service.locations(g):
        exists = _("existe") if loc.path.is_dir() else _("NO existe aún")
        print(f"  {loc.name}: {loc.template}\n      → {loc.path} ({exists})")
    st = state.status(g.game_id)
    if st:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(st["last_ok"])) if st["last_ok"] else _("nunca")
        print("  " + _("último sync correcto: {when}").format(when=when) +
              ("  · " + _("PENDIENTE: {reason}").format(reason=st["last_error"] or "") if st["pending"] else ""))


def cmd_sync(args, service: SyncService, **_kw):
    kw = dict(prefer=args.prefer, dry_run=args.dry_run, wait_network=0 if args.no_wait else 30)
    if args.pending:
        results = service.sync_pending(**kw)
        if not results:
            print(_("No hay juegos pendientes"))
        failed = False
        for gid, r in results.items():
            if isinstance(r, Exception):
                failed = True
                print(f"{gid}: " + _("ERROR {error}").format(error=r))
            else:
                print(f"{gid}: {describe(r)}")
        return EXIT_ERROR if failed else 0
    if not args.game_id:
        raise SystemExit(_("Indica un juego o --pending"))
    try:
        res = service.sync(args.game_id, **kw)
    except ConflictError as e:
        print(_("Hay conflictos: las partidas cambiaron aquí y en la nube desde el último sync."))
        for it in e.plan.conflicts:
            l = time.strftime("%Y-%m-%d %H:%M", time.localtime(it.local.mtime)) if it.local else _("borrado")
            c = it.cloud.last_modified if it.cloud else _("borrado")
            print(f"  {it.key}\n      " + _("local: {local}   nube: {cloud}").format(local=l, cloud=c))
        print(_("Repite con --prefer local o --prefer cloud (se hará copia de seguridad antes)."))
        return EXIT_CONFLICT
    for w in res.plan.warnings:
        print(_("aviso: {warning}").format(warning=w))
    print(describe(res))
    if res.plan.ignored_cloud:
        print(_("({n} archivo(s) en la nube fuera de las ubicaciones conocidas, ignorados)")
              .format(n=len(res.plan.ignored_cloud)))
    return 0


def cmd_run(args, **_kw):
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


def cmd_watch(args, **_kw):
    from .session import watch
    return watch(args.game_id, args.session, args.pid, log=_log)


def cmd_proton_list(args, **_kw):
    from .runner import proton
    d = proton.default()
    vers = proton.installed()
    if not vers:
        print(_("No hay ninguna versión instalada. Usa: gogl proton install"))
    for v in vers:
        print(f"{'*' if d and v.name == d.name else ' '} {v.name}   {v.path}")


def cmd_proton_available(args, **_kw):
    from .runner import proton
    have = {v.name for v in proton.installed()}
    for r in proton.available():
        print(f"{r.name:20} {r.size / 2**20:6.0f} MB  {_('(instalado)') if r.name in have else ''}")


def cmd_proton_install(args, **_kw):
    from .runner import proton
    rels = proton.available()
    rel = rels[0] if not args.version else next((r for r in rels if r.name == args.version), None)
    if rel is None:
        raise SystemExit(_("No se encuentra {name}").format(name=args.version))
    last = [-1]

    def progress(done, total):
        pct = int(done * 100 / total) if total else 0
        if pct != last[0]:
            last[0] = pct
            print("\r" + _("Descargando {name}: {pct}%").format(name=rel.name, pct=pct), end="",
                  file=sys.stderr, flush=True)

    v = proton.install(rel, progress)
    print("\n" + _("Instalado en {path}").format(path=v.path))
    if not proton.default() or len(proton.installed()) == 1 or args.default:
        proton.set_default(v.name)


def cmd_proton_default(args, **_kw):
    from .runner import proton
    proton.set_default(args.version)
    print(_("{name} es ahora la versión por defecto").format(name=args.version))


def cmd_proton_remove(args, **_kw):
    from .runner import proton
    proton.remove(args.version)
    print(_("Eliminado"))


def cmd_steam_add(args, state: State, **_kw):
    from .steam import add_shortcut
    g = state.game(args.game_id)
    if not g:
        raise SyncUnavailable(_("Juego {id} no registrado").format(id=args.game_id))
    r = add_shortcut(g, force=args.force)
    print((_("Actualizado «{title}» en Steam (appid {appid}, usuario {user})") if r.updated else
           _("Añadido «{title}» en Steam (appid {appid}, usuario {user})"))
          .format(title=g.title, appid=r.appid, user=r.user_dir.name))


def cmd_service(args, **_kw):
    from . import services
    if args.action == "install":
        print(_("Timer instalado en {path}").format(path=services.install()))
    else:
        services.uninstall()
        print(_("Timer eliminado"))


def _progress_printer():
    last = [0.0]

    def show(p):
        now = time.monotonic()
        if now - last[0] < 0.5 and p.stage not in ("done",):
            return
        last[0] = now
        if p.stage == "prepare":
            msg = _("Preparando…")
        elif p.stage == "verify":
            msg = _("Verificando {done}/{total} archivos").format(done=p.files_done, total=p.files_total)
        elif p.stage == "finalize":
            msg = _("Terminando…")
        else:
            msg = (f"{p.fraction * 100:5.1f}%  {p.downloaded / 2**20:,.0f}/{p.download_total / 2**20:,.0f} MB"
                   f"  {p.speed / 2**20:5.1f} MB/s  (" +
                   _("{done}/{total} archivos").format(done=p.files_done, total=p.files_total) + ")")
        print(f"\r{msg:<80}", end="", file=sys.stderr, flush=True)
    return show


def cmd_install(args, api: GogApi, **_kw):
    from .content import install_game
    g = install_game(api, args.game_id, platform=args.platform, language=args.lang,
                     dlcs=None if args.dlc == "all" else [] if args.dlc == "none" else args.dlc.split(","),
                     install_path=Path(args.path).expanduser() if args.path else None,
                     progress=_progress_printer())
    print("\n" + _("Instalado {title} en {path}").format(title=g.title, path=g.install_path))


def cmd_update(args, api: GogApi, **_kw):
    from .content import check_update, update_game
    upd = check_update(api, args.game_id)
    if not upd:
        print(_("Ya está actualizado"))
        return 0
    print(_("Actualizando {old} → {new}").format(old=upd[0], new=upd[1]))
    update_game(api, args.game_id, progress=_progress_printer())
    print("\n" + _("Actualizado"))


def cmd_verify(args, api: GogApi, **_kw):
    from .content import Installer
    inst = Installer(api, progress=_progress_printer())
    bad = inst.repair(args.game_id) if args.repair else inst.verify(args.game_id)
    print()
    if not bad:
        print(_("Todos los archivos están correctos"))
        return 0
    print((_("{n} archivo(s) reparados:") if args.repair else _("{n} archivo(s) dañados o ausentes:"))
          .format(n=len(bad)))
    for b in bad[:30]:
        print(f"  {b}")
    return 0 if args.repair else EXIT_ERROR


def cmd_uninstall(args, state: State, **_kw):
    from .content import uninstall
    g = state.game(args.game_id)
    if not g:
        raise SyncUnavailable(_("Juego {id} no registrado").format(id=args.game_id))
    if not args.yes:
        answer = input(_("¿Desinstalar {title} y borrar {path}? [s/N] ").format(title=g.title, path=g.install_path))
        if answer.strip().lower() not in ("s", "y", "sí", "si", "yes"):
            return 0
    uninstall(state, args.game_id, remove_prefix=args.remove_prefix)
    print(_("Desinstalado"))


def _size(n: int) -> str:
    return f"{n / 2**30:.1f} GB" if n >= 2**30 else f"{n / 2**20:.0f} MB"


def cmd_offline_list(args, api: GogApi, **_kw):
    from .content.offline import downloaded_files, list_downloads
    off = list_downloads(api, args.game_id)
    print(_("Instaladores offline:") if off.installers else _("Sin instaladores offline"))
    for b in off.installers:
        done = " ✓" if downloaded_files(b) else ""
        print(f"  {b.id:<28} {b.os:<8} {b.language:<6} {b.version:<24} {_size(b.total_size):>9}"
              f"  ({n_('{n} archivo', '{n} archivos', len(b.files)).format(n=len(b.files))}){done}")
    print(_("Extras:") if off.extras else _("Sin extras"))
    for b in off.extras:
        done = " ✓" if downloaded_files(b) else ""
        print(f"  {b.id:<28} {b.type_label:<20} {b.name:<30} {_size(b.total_size):>9}{done}")


def cmd_offline_download(args, api: GogApi, **_kw):
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
        print(_("No hay nada que coincida (mira `gogl offline list`)"), file=sys.stderr)
        return EXIT_ERROR
    for b in chosen:
        print(f"{b.name} ({b.type_label if b.kind == 'extra' else b.language}) → {bundle_dir(b)}")
        OfflineDownloader(api, _progress_printer()).download(b)
        print()
    print(_("Descarga completada"))


def cmd_achievements(args, state: State, auth: AuthManager, api: GogApi, **_kw):
    from . import achievements
    items = achievements.fetch(api, auth, state, args.game_id)
    if not items:
        print(_("Este juego no tiene logros"))
        return 0
    got = [a for a in items if a.unlocked]
    print(_("{got} de {total} logros").format(got=len(got), total=len(items)))
    for a in sorted(items, key=lambda a: (not a.unlocked, -(a.unlocked_at or 0), a.name)):
        mark = "✓" if a.unlocked else "·"
        when = "  " + time.strftime(_("%d/%m/%Y"), time.localtime(a.unlocked_at)) if a.unlocked else ""
        name = a.name if a.visible or a.unlocked else _("(oculto)")
        print(f"  {mark} {name}{when}  [{a.rarity:.1f}%]")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gogl", description=_("Lanzador de GOG para Linux"))
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("login", help=_("iniciar sesión en GOG"))
    s.add_argument("code", nargs="?", help=_("código o URL de on_login_success"))
    s.set_defaults(func=cmd_login)
    sub.add_parser("logout", help=_("cerrar sesión")).set_defaults(func=cmd_logout)

    games = sub.add_parser("games", help=_("juegos registrados")).add_subparsers(dest="games_cmd", required=True)
    s = games.add_parser("add", help=_("registrar un juego ya instalado"))
    s.add_argument("path", help=_("carpeta de instalación (la que contiene goggame-*.info)"))
    s.add_argument("--prefix", help=_("prefijo de Proton/Wine (compatdata o pfx)"))
    s.add_argument("--platform", choices=["windows", "linux"])
    s.set_defaults(func=cmd_games_add)
    games.add_parser("list", help=_("listar juegos")).set_defaults(func=cmd_games_list)
    s = games.add_parser("remove", help=_("quitar del registro"))
    s.add_argument("game_id")
    s.set_defaults(func=cmd_games_remove)
    s = games.add_parser("save-path", help=_("forzar la ruta local de una ubicación de partidas"))
    s.add_argument("game_id")
    s.add_argument("location", help=_("nombre de la ubicación (ver `gogl saves`)"))
    s.add_argument("path", nargs="?", help=_("ruta; omitir para volver a la automática"))
    s.set_defaults(func=cmd_games_save_path)

    s = sub.add_parser("saves", help=_("mostrar dónde están las partidas de un juego"))
    s.add_argument("game_id")
    s.set_defaults(func=cmd_saves)

    s = sub.add_parser("sync", help=_("sincronizar partidas con la nube de GOG"))
    s.add_argument("game_id", nargs="?")
    s.add_argument("--pending", action="store_true", help=_("sincronizar todos los juegos pendientes"))
    s.add_argument("--dry-run", action="store_true", help=_("mostrar qué se haría sin tocar nada"))
    s.add_argument("--prefer", choices=["local", "cloud"], help=_("cómo resolver conflictos"))
    s.add_argument("--no-wait", action="store_true", help=_("no esperar a que haya red"))
    s.set_defaults(func=cmd_sync)
    s = sub.add_parser("install", help=_("descargar e instalar un juego de tu biblioteca"))
    s.add_argument("game_id")
    s.add_argument("--platform", choices=["windows", "linux"], default="windows")
    s.add_argument("--lang", help=_("idioma, p. ej. es-ES (por defecto el del sistema si existe)"))
    s.add_argument("--dlc", default="all", help=_("all, none o lista de ids separados por comas"))
    s.add_argument("--path", help=_("carpeta de instalación (por defecto ~/Games/gogl/<juego>)"))
    s.set_defaults(func=cmd_install)
    s = sub.add_parser("update", help=_("actualizar a la última versión (solo descarga lo cambiado)"))
    s.add_argument("game_id")
    s.set_defaults(func=cmd_update)
    s = sub.add_parser("verify", help=_("comprobar la integridad de los archivos"))
    s.add_argument("game_id")
    s.add_argument("--repair", action="store_true", help=_("volver a descargar lo dañado"))
    s.set_defaults(func=cmd_verify)
    s = sub.add_parser("uninstall", help=_("desinstalar (las partidas en la nube no se tocan)"))
    s.add_argument("game_id")
    s.add_argument("--remove-prefix", action="store_true", help=_("borrar también el prefijo de Proton"))
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_uninstall)

    off = sub.add_parser("offline", help=_("extras e instaladores offline de tu cuenta")).add_subparsers(
        dest="offline_cmd", required=True)
    s = off.add_parser("list", help=_("qué hay para descargar"))
    s.add_argument("game_id")
    s.set_defaults(func=cmd_offline_list)
    s = off.add_parser("download", help=_("descargar (por defecto, el instalador de Windows)"))
    s.add_argument("game_id")
    s.add_argument("--os", choices=["windows", "linux", "mac"], default="windows")
    s.add_argument("--lang", help=_("idioma del instalador, p. ej. es"))
    s.add_argument("--extras", action="store_true", help=_("todos los extras en vez del instalador"))
    s.add_argument("--ids", help=_("ids concretos separados por comas (ver `list`)"))
    s.set_defaults(func=cmd_offline_download)

    s = sub.add_parser("achievements", help=_("logros del juego (conseguidos y pendientes)"))
    s.add_argument("game_id")
    s.set_defaults(func=cmd_achievements)

    s = sub.add_parser("run", help=_("jugar: sincroniza, lanza el juego y vuelve a sincronizar"))
    s.add_argument("game_id")
    s.add_argument("--no-ui", action="store_true", help=_("sin ventana (preguntas por terminal)"))
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("watch")  # interno: vigilante post-sync lanzado con systemd-run
    s.add_argument("game_id")
    s.add_argument("--session", type=int, required=True)
    s.add_argument("--pid", type=int, required=True)
    s.set_defaults(func=cmd_watch)

    pr = sub.add_parser("proton", help=_("versiones de Proton-GE gestionadas por gogl")).add_subparsers(
        dest="proton_cmd", required=True)
    pr.add_parser("list", help=_("versiones instaladas")).set_defaults(func=cmd_proton_list)
    pr.add_parser("available", help=_("versiones publicadas")).set_defaults(func=cmd_proton_available)
    s = pr.add_parser("install", help=_("descargar e instalar (por defecto la última)"))
    s.add_argument("version", nargs="?")
    s.add_argument("--default", action="store_true", help=_("usarla por defecto"))
    s.set_defaults(func=cmd_proton_install)
    s = pr.add_parser("default", help=_("elegir la versión por defecto"))
    s.add_argument("version")
    s.set_defaults(func=cmd_proton_default)
    s = pr.add_parser("remove", help=_("borrar una versión"))
    s.add_argument("version")
    s.set_defaults(func=cmd_proton_remove)

    st = sub.add_parser("steam", help=_("integración con Steam")).add_subparsers(dest="steam_cmd", required=True)
    s = st.add_parser("add", help=_("añadir el juego a Steam como acceso directo"))
    s.add_argument("game_id")
    s.add_argument("--force", action="store_true", help=_("aunque Steam esté abierto"))
    s.set_defaults(func=cmd_steam_add)

    s = sub.add_parser("service", help=_("timer que reintenta las subidas pendientes cada 15 min"))
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
        print(_("Error: {error}").format(error=e), file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
