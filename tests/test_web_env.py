import os
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWebEngineWidgets")

from gogl_ui import web

OVERLAY = "/home/deck/.local/share/Steam/ubuntu12_64/gameoverlayrenderer.so"


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GOGL_HOME", str(tmp_path / "home"))
    for k in ("QT_QUICK_BACKEND", "QTWEBENGINE_CHROMIUM_FLAGS", "QTWEBENGINEPROCESS_PATH",
              "GAMESCOPE_WAYLAND_DISPLAY", "XDG_CURRENT_DESKTOP", "LD_PRELOAD"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_game_mode_draws_web_by_software(clean_env):
    clean_env.setenv("XDG_CURRENT_DESKTOP", "gamescope")
    clean_env.setenv("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --foo")
    web.prepare_environment()
    assert os.environ["QT_QUICK_BACKEND"] == "software"
    assert os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] == "--disable-gpu --foo --disable-gpu-compositing"


def test_desktop_without_steam_touches_nothing(clean_env):
    web.prepare_environment()
    assert not {"QT_QUICK_BACKEND", "QTWEBENGINE_CHROMIUM_FLAGS", "QTWEBENGINEPROCESS_PATH"} & set(os.environ)


def test_chromium_processes_start_without_steam_overlay(clean_env, tmp_path):
    other = "/usr/lib/libother.so"
    clean_env.setenv("LD_PRELOAD", f"{other}:{OVERLAY} {OVERLAY.replace('64', '32')}")
    web.prepare_environment()
    wrapper = os.environ["QTWEBENGINEPROCESS_PATH"]
    script = Path(wrapper).read_text()
    assert os.access(script.splitlines()[-1].split()[1].strip("'"), os.X_OK)
    # El mismo envoltorio, pero enseñando el entorno en vez de arrancar Chromium.
    probe = tmp_path / "probe.sh"
    probe.write_text(script.rsplit("exec ", 1)[0] + 'printf "%s" "${LD_PRELOAD-<unset>}"\n')
    run = lambda preload: subprocess.run(["sh", str(probe)], check=True, capture_output=True,
                                         text=True, env={"LD_PRELOAD": preload}).stdout
    assert run(os.environ["LD_PRELOAD"]) == other
    assert run(OVERLAY) == "<unset>"
