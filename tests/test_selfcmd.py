import os
import subprocess

import pytest

from gogl import selfcmd, steam


@pytest.fixture
def img(tmp_path, monkeypatch):
    """Una «AppImage» falsa que imprime sus argumentos."""
    monkeypatch.setenv("GOGL_HOME", str(tmp_path / "gogl"))
    monkeypatch.setenv("HOME", str(tmp_path / "casa"))
    p = tmp_path / "casa/Descargas/gogl-0.1-x86_64.AppImage"
    p.parent.mkdir(parents=True)
    p.write_text('#!/bin/sh\necho "$0 $*"\n')
    p.chmod(0o755)
    monkeypatch.setenv("APPIMAGE", str(p))
    return p


def run(cmd, *args):
    return subprocess.run([*cmd, *args], capture_output=True, text=True, env=os.environ)


def test_commands_go_through_launcher(img):
    cmd = selfcmd.cli_command()
    assert cmd == [str(selfcmd.launcher_path())] and selfcmd.ui_command() == cmd[0]
    assert run(cmd, "run", "42").stdout.strip() == f"{img} run 42"


def test_launcher_finds_moved_appimage(img, tmp_path, monkeypatch):
    cmd = selfcmd.cli_command()
    moved = tmp_path / "casa/Juegos/otra carpeta/gogl.AppImage"
    moved.parent.mkdir(parents=True)
    img.rename(moved)
    moved.chmod(0o644)  # p. ej. al copiarla desde una memoria USB
    assert run(cmd, "sync").stdout.strip() == f"{moved} sync"
    # Al arrancar desde la nueva ruta, el lanzador pasa a apuntar ahí directamente.
    monkeypatch.setenv("APPIMAGE", str(moved))
    selfcmd.register()
    assert str(moved) in selfcmd.launcher_path().read_text()


def test_launcher_reports_missing_appimage(img):
    cmd = selfcmd.cli_command()
    img.unlink()
    r = run(cmd)
    assert r.returncode == 127 and "No se encuentra" in r.stderr


def test_without_appimage_no_launcher(tmp_path, monkeypatch):
    monkeypatch.setenv("GOGL_HOME", str(tmp_path))
    monkeypatch.delenv("APPIMAGE", raising=False)
    assert selfcmd.register() is None and selfcmd.cli_command()[1:] == ["-m", "gogl.cli"]


def test_repoint_old_shortcuts(img, tmp_path, monkeypatch):
    monkeypatch.setattr(steam, "steam_running", lambda: False)
    user = tmp_path / "Steam/userdata/1234"
    (user / "config").mkdir(parents=True)
    monkeypatch.setattr(steam, "user_dirs", lambda: [user])
    old = f'"{img}"'
    data = {"shortcuts": {
        "0": {"appid": -5, "AppName": "Juego", "Exe": old, "LaunchOptions": "run 42"},
        "1": {"appid": -6, "AppName": "GOG Linux", "Exe": old, "LaunchOptions": "", "tags": {"0": "gogl"}},
        "2": {"appid": -7, "AppName": "Otro", "Exe": '"/opt/otro.AppImage"', "LaunchOptions": ""},
    }}
    (user / "config/shortcuts.vdf").write_bytes(steam.vdf_dump(data))
    assert steam.repoint_shortcuts() == 2
    sc = steam.vdf_load((user / "config/shortcuts.vdf").read_bytes())["shortcuts"]
    launcher = f'"{selfcmd.launcher_path()}"'
    assert sc["0"]["Exe"] == sc["1"]["Exe"] == launcher and sc["0"]["appid"] == -5
    assert sc["2"]["Exe"] == '"/opt/otro.AppImage"'
    assert steam.repoint_shortcuts() == 0
