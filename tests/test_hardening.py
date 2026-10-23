"""Regresiones de la revisión de seguridad y fallos (2026-10-23)."""

import os
import subprocess

import pytest

from gogl import auth, selfcmd, services, steam
from gogl.saves import backup
from gogl.saves.engine import SyncError, safe_cloud_rel


@pytest.mark.parametrize("rel", ["../../.bashrc", "/home/x/.profile", "a/../../b", "..\\x", ""])
def test_unsafe_cloud_keys_are_rejected(rel):
    assert not safe_cloud_rel(rel)


def test_cloud_keys_escaping_save_folder_are_ignored(env, tmp_path):
    env.cloud.put_raw("saves/../../evil.txt", b"x")
    env.cloud.put_raw("saves//" + str(tmp_path / "abs.txt").lstrip("/"), b"x")
    env.cloud.put_raw("saves/ok.sav", b"bien")
    res = env.syncer().run()
    assert res.downloaded == ["saves/ok.sav"]
    assert "saves/../../evil.txt" in res.plan.ignored_cloud
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path / "abs.txt").exists()


def test_download_through_symlinked_subfolder_is_refused(env, tmp_path):
    outside = tmp_path / "fuera"
    outside.mkdir()
    (env.saves / "sub").symlink_to(outside)
    env.cloud.put_raw("saves/sub/x.sav", b"x")
    with pytest.raises(SyncError):
        env.syncer().run()
    assert not (outside / "x.sav").exists()


def test_new_shortcut_does_not_overwrite_on_key_gaps(tmp_path, monkeypatch):
    monkeypatch.setenv("GOGL_HOME", str(tmp_path / "g"))
    monkeypatch.setattr(steam, "steam_running", lambda: False)
    monkeypatch.setattr(steam, "_artwork", lambda *a: None)
    user = tmp_path / "userdata/1"
    (user / "config").mkdir(parents=True)
    data = {"shortcuts": {"0": {"AppName": "A", "LaunchOptions": ""},
                          "2": {"AppName": "B", "LaunchOptions": ""}}}
    (user / "config/shortcuts.vdf").write_bytes(steam.vdf_dump(data))
    from gogl.state import Game
    steam.add_shortcut(Game("42", "Juego", "windows", tmp_path), user_dir=user)
    sc = steam.vdf_load((user / "config/shortcuts.vdf").read_bytes())["shortcuts"]
    assert [sc[k]["AppName"] for k in ("0", "2", "3")] == ["A", "B", "Juego"]


def test_backup_rotation_keeps_newest(tmp_path, monkeypatch):
    """Tres copias en el mismo segundo: «…-1» y «…-2» son las más nuevas."""
    from gogl.saves.locations import SaveLocation
    saves = tmp_path / "s"
    saves.mkdir()
    (saves / "a").write_text("1")
    loc = [SaveLocation("s", "", saves)]
    monkeypatch.setattr(backup.time, "strftime", lambda fmt, *a: fmt.replace("%Y%m%d-%H%M%S", "20261023-101010"))
    made = [backup.backup_locations("1", loc, keep=2, root=tmp_path / "b") for _ in range(3)]
    left = sorted(p.name for p in (tmp_path / "b/1").glob("*.tar.gz"))
    assert left == sorted(p.name for p in made[1:])


def test_corrupt_auth_file_means_logged_out(tmp_path):
    store = tmp_path / "auth.json"
    store.write_text("{roto")
    assert not auth.AuthManager(store).is_logged_in()


def test_systemd_units_quote_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("GOGL_HOME", "/tmp/con espacio/100%")
    monkeypatch.setattr(services, "cli_command", lambda: ["/opt/mi gogl/gogl-launch"])
    svc = services.unit_files()["gogl-sync-pending.service"]
    assert 'Environment="GOGL_HOME=/tmp/con espacio/100%%"' in svc
    assert 'ExecStart="/opt/mi gogl/gogl-launch" "sync" "--pending"' in svc


def test_launcher_ignores_lookalike_appimage(tmp_path, monkeypatch):
    monkeypatch.setenv("GOGL_HOME", str(tmp_path / "gogl"))
    monkeypatch.setenv("HOME", str(tmp_path / "casa"))
    img = tmp_path / "casa/Apps/gogl.AppImage"
    img.parent.mkdir(parents=True)
    img.write_text('#!/bin/sh\necho buena\n')
    img.chmod(0o755)
    monkeypatch.setenv("APPIMAGE", str(img))
    cmd = selfcmd.cli_command()
    img.unlink()
    fake = tmp_path / "casa/Descargas/gogl-nueva.AppImage"
    fake.parent.mkdir(parents=True)
    fake.write_text('#!/bin/sh\necho mala\n')
    r = subprocess.run(cmd, capture_output=True, text=True, env=os.environ)
    assert r.returncode == 127 and "mala" not in r.stdout


def test_stale_comet_auth_dirs_are_removed(tmp_path, monkeypatch):
    from gogl import paths
    from gogl.runner import comet
    monkeypatch.setenv("GOGL_HOME", str(tmp_path))
    dead = paths.data_dir() / "comet/auth-999999999"
    dead.mkdir(parents=True)
    d = comet._write_auth({"refresh_token": "x"})
    assert not dead.exists() and d.exists()


def test_compat_shim_follows_prefix_change(tmp_path, monkeypatch):
    from gogl.runner.game import compat_data_path
    from gogl.state import Game
    monkeypatch.setenv("GOGL_HOME", str(tmp_path / "g"))
    a, b = tmp_path / "a", tmp_path / "b"
    for p in (a, b):
        (p / "drive_c").mkdir(parents=True)
    g = Game("1", "J", "windows", tmp_path, a)
    shim = compat_data_path(g)
    g.prefix = b
    assert compat_data_path(g) == shim and (shim / "pfx").resolve() == b
