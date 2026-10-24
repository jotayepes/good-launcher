import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import time

import httpx
import pytest
import respx

from gogl import session, steam
from gogl.runner import game as rgame
from gogl.runner import proton
from gogl.state import Game, State


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GOGL_HOME", str(tmp_path / "home"))
    return tmp_path


# --- Steam ----------------------------------------------------------------------

def test_vdf_roundtrip():
    data = {"shortcuts": {"0": {"appid": -123, "AppName": "Juego ñ", "tags": {"0": "GOG"}}}}
    assert steam.vdf_load(steam.vdf_dump(data)) == data


def test_add_shortcut_creates_and_updates(home, monkeypatch):
    monkeypatch.setattr(steam, "steam_running", lambda: False)
    monkeypatch.setattr(steam, "product_details", lambda gid, http=None: {})
    user = home / "Steam/userdata/1234"
    (user / "config").mkdir(parents=True)
    other = {"shortcuts": {"0": {"appid": 5, "AppName": "Otro", "LaunchOptions": ""}}}
    (user / "config/shortcuts.vdf").write_bytes(steam.vdf_dump(other))
    g = Game("42", "Mi Juego", "windows", home / "g")

    r = steam.add_shortcut(g, user_dir=user)
    data = steam.vdf_load((user / "config/shortcuts.vdf").read_bytes())["shortcuts"]
    assert not r.updated and len(data) == 2
    sc = data["1"]
    assert sc["AppName"] == "Mi Juego" and sc["LaunchOptions"].endswith("run 42")
    assert sc["appid"] & 0xFFFFFFFF == r.appid and r.appid & 0x80000000
    assert (user / "config/shortcuts.vdf.gogl-bak").exists()

    r2 = steam.add_shortcut(g, user_dir=user)
    data = steam.vdf_load((user / "config/shortcuts.vdf").read_bytes())["shortcuts"]
    assert r2.updated and len(data) == 2 and r2.appid == r.appid


def test_launcher_shortcut_does_not_clash_with_games(home, monkeypatch):
    monkeypatch.setattr(steam, "steam_running", lambda: False)
    monkeypatch.setattr(steam, "product_details", lambda gid, http=None: {})
    user = home / "Steam/userdata/1234"
    (user / "config").mkdir(parents=True)
    steam.add_shortcut(Game("42", "gogl", "windows", home / "g"), user_dir=user)  # juego llamado igual
    r = steam.add_launcher_shortcut(user_dir=user)
    r2 = steam.add_launcher_shortcut(user_dir=user)
    data = steam.vdf_load((user / "config/shortcuts.vdf").read_bytes())["shortcuts"]
    assert not r.updated and r2.updated and r2.appid == r.appid and len(data) == 2
    sc = data["1"]
    assert sc["LaunchOptions"] == "" and sc["tags"] == {"0": "gogl"}
    assert data["0"]["LaunchOptions"].endswith("run 42")


def test_add_shortcut_refuses_with_steam_open(home, monkeypatch):
    monkeypatch.setattr(steam, "steam_running", lambda: True)
    with pytest.raises(steam.SteamError):
        steam.add_shortcut(Game("1", "x", "windows", home), user_dir=home)


# --- Proton gestionado ----------------------------------------------------------

def _fake_proton_tar(name: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(path, data=b"", mode=0o755):
            ti = tarfile.TarInfo(f"{name}/{path}")
            ti.size, ti.mode = len(data), mode
            tar.addfile(ti, io.BytesIO(data))
        add("proton", b"#!/bin/sh\n")
        add("files/bin/wineserver", b"#!/bin/sh\n")
        link = tarfile.TarInfo(f"{name}/files/share/default_pfx/dosdevices/z:")
        link.type, link.linkname = tarfile.SYMTYPE, "/"
        tar.addfile(link)
    return buf.getvalue()


@respx.mock
def test_proton_install_verifies_checksum(home):
    body = _fake_proton_tar("GE-Proton9-1")
    respx.get(proton.RELEASES_URL).mock(return_value=httpx.Response(200, json=[{
        "tag_name": "GE-Proton9-1", "assets": [
            {"name": "GE-Proton9-1-x86_64.tar.gz", "browser_download_url": "https://x/p.tgz", "size": len(body)},
            {"name": "GE-Proton9-1-x86_64.sha512sum", "browser_download_url": "https://x/p.sha", "size": 1},
        ]}]))
    respx.get("https://x/p.tgz").mock(return_value=httpx.Response(200, content=body))
    respx.get("https://x/p.sha").mock(return_value=httpx.Response(
        200, text=f"{hashlib.sha512(body).hexdigest()}  GE-Proton9-1-x86_64.tar.gz"))
    rel = proton.available()[0]
    v = proton.install(rel)
    assert v.script.exists() and v.wineserver
    assert os.readlink(v.path / "files/share/default_pfx/dosdevices/z:") == "/"
    assert proton.default().name == "GE-Proton9-1"
    assert not list(proton.proton_root().glob(".tmp-*"))


@respx.mock
def test_proton_install_rejects_bad_checksum(home):
    respx.get("https://x/p.tgz").mock(return_value=httpx.Response(200, content=_fake_proton_tar("GE-X")))
    respx.get("https://x/p.sha").mock(return_value=httpx.Response(200, text="00" * 64 + "  f"))
    with pytest.raises(proton.ProtonError):
        proton.install(proton.Release("GE-X", "https://x/p.tgz", 1, "https://x/p.sha"))
    assert proton.installed() == []


# --- Lanzamiento ----------------------------------------------------------------

FAKE_PROTON = """#!/bin/sh
# Registra cómo nos llaman y "ejecuta" el juego.
echo "$@" >> "$STEAM_COMPAT_DATA_PATH/../proton_calls.txt"
env | grep -E '^(STEAM_COMPAT_DATA_PATH|SteamGameId)=' >> "$STEAM_COMPAT_DATA_PATH/../proton_env.txt"
if [ "$1" = "run" ]; then mkdir -p "$STEAM_COMPAT_DATA_PATH/pfx/drive_c/users/steamuser"; echo 1 > "$STEAM_COMPAT_DATA_PATH/version"; fi
exit 0
"""


def _setup_windows_game(home, monkeypatch):
    pdir = proton.proton_root() / "GE-Fake"
    (pdir / "files/bin").mkdir(parents=True)
    (pdir / "proton").write_text(FAKE_PROTON)
    (pdir / "proton").chmod(0o755)
    (pdir / "files/bin/wineserver").write_text("#!/bin/sh\nexit 0\n")
    (pdir / "files/bin/wineserver").chmod(0o755)
    inst = home / "game"
    (inst / "Bin").mkdir(parents=True)
    (inst / "Bin/Game.exe").write_bytes(b"MZ")
    (inst / "goggame-7.info").write_text(json.dumps({
        "gameId": "7", "name": "Siete", "playTasks": [
            {"isPrimary": True, "type": "FileTask", "path": "bin\\game.exe", "workingDir": "bin",
             "arguments": "-skipintro"}]}))
    g = Game("7", "Siete", "windows", inst, home / "pfxs/7", None, None)
    State().save_game(g)
    return g


def test_build_launch_windows(home, monkeypatch):
    g = _setup_windows_game(home, monkeypatch)
    monkeypatch.setenv("SteamGameId", "999")  # como en el modo juego
    spec = rgame.build_launch(g)
    assert spec.argv[1:] == ["waitforexitandrun", str(home / "game/Bin/Game.exe"), "-skipintro"]
    assert spec.cwd == home / "game/Bin"
    assert spec.env["STEAM_COMPAT_DATA_PATH"] == str(home / "pfxs/7")
    assert spec.env["SteamGameId"] == "999"  # no se pisa el de Steam
    assert spec.env["PROTON_USE_XALIA"] == "0"


def test_launch_options(home, monkeypatch):
    g = _setup_windows_game(home, monkeypatch)
    g.extra["launch"] = {"args": '-windowed "C:\\Mis Juegos"', "wrapper": "mangohud --dlsym",
                         "env": "# comentario\nDXVK_HUD=fps\n\nPROTON_USE_WINED3D=1"}
    spec = rgame.build_launch(g)
    assert spec.argv[:2] == ["mangohud", "--dlsym"] and spec.argv[3] == "waitforexitandrun"
    assert spec.argv[-3:] == ["-skipintro", "-windowed", "C:\\Mis Juegos"]
    assert spec.env["DXVK_HUD"] == "fps" and spec.env["PROTON_USE_WINED3D"] == "1"
    g.extra["launch"] = {"env": "sin igual"}
    with pytest.raises(rgame.LaunchError):
        rgame.build_launch(g)


def test_plain_wine_prefix_gets_compat_shim(home):
    wine = home / "wineprefix"
    (wine / "drive_c").mkdir(parents=True)
    g = Game("8", "Ocho", "windows", home, wine)
    compat = rgame.compat_data_path(g)
    assert (compat / "pfx").resolve() == wine.resolve()


class RecUI(session.ConsoleUI):
    def __init__(self):
        super().__init__()
        self.tty = False
        self.events = []

    def status(self, text):
        self.events.append(("status", text))

    def hide(self):
        self.events.append(("hide",))

    def show(self):
        self.events.append(("show",))

    def error(self, message):
        self.events.append(("error", message))


def test_run_game_without_sync(home, monkeypatch):
    g = _setup_windows_game(home, monkeypatch)
    ui = RecUI()
    assert session.run_game(g.game_id, ui) == 0
    calls = (home / "pfxs/proton_calls.txt").read_text().splitlines()
    assert calls[0] == "run wineboot -u"  # prefijo creado antes de jugar
    assert calls[1].startswith("waitforexitandrun")
    assert ("hide",) in ui.events and ("show",) in ui.events
    s = State().db.execute("SELECT ended FROM sessions").fetchone()
    assert s[0] is not None


def test_run_game_reports_missing_proton(home):
    inst = home / "g"
    inst.mkdir()
    (inst / "goggame-9.info").write_text(json.dumps({"gameId": "9", "playTasks": [
        {"isPrimary": True, "path": "a.exe"}]}))
    State().save_game(Game("9", "Nueve", "windows", inst, None))
    ui = RecUI()
    assert session.run_game("9", ui) == 1
    assert ui.events[-1][0] == "error" and "Proton" in ui.events[-1][1]


def test_watch_exits_if_wrapper_already_synced(home):
    st = State()
    sid = st.open_session("1", 0, None, None)
    st.update_session(sid, post_synced=1)
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    t = time.monotonic()
    assert session.watch("1", sid, p.pid) == 0
    assert time.monotonic() - t < 5


def test_watch_syncs_when_wrapper_was_killed(home, monkeypatch):
    st = State()
    sid = st.open_session("1", 0, None, None)
    calls = []

    class FakeService:
        def __init__(self, *a, **k):
            pass

        def sync(self, gid, **kw):
            calls.append(gid)
            from gogl.saves.engine import SyncPlan, SyncResult
            return SyncResult(SyncPlan([]), uploaded=["saves/a"])

    monkeypatch.setattr(session, "SyncService", FakeService)
    monkeypatch.setattr(session, "notify", lambda *a: calls.append("notify"))
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3)"])
    assert session.watch("1", sid, p.pid) == 0
    assert calls == ["1", "notify"]
    assert State().session(sid)["post_synced"] == 2


@pytest.mark.parametrize("cmd,expected", [
    (r'-path C:\Users\me\Saves -x', ["-path", r"C:\Users\me\Saves", "-x"]),
    (r'"C:\Program Files\X" "a b"', [r"C:\Program Files\X", "a b"]),
    (r'a\\\"b "c\\" d', [r'a\"b', 'c\\', "d"]),
    (r'""  x', ["", "x"]),
    ("", []),
])
def test_split_windows_args(cmd, expected):
    assert rgame.split_windows_args(cmd) == expected
