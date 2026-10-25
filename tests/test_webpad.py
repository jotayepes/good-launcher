"""Mando y dedo dentro de una página web (webpad.py), con Qt WebEngine de verdad.

Va en un proceso aparte: Qt WebEngine tiene que arrancar antes que QApplication y
con su propio entorno (sin sandbox, que como root no arranca)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWebEngineWidgets")

ROOT = Path(__file__).resolve().parent.parent

PAGE = """<html><head><style>
body { margin: 0; font: 16px sans-serif } .row { display: flex; gap: 20px; margin: 30px }
a, button { padding: 10px; border: 1px solid #888 } #tall { height: 3000px }
</style></head><body>
<div class=row><a href="#a1" id=a1>uno</a><a href="#a2" id=a2>dos</a><button id=b3>tres</button></div>
<div class=row><a href="#a4" id=a4>cuatro</a><a href="#a5" id=a5>cinco</a></div>
<input id=q placeholder=buscar>
<div id=tall>texto</div>
<script>document.addEventListener('click', e => { document.title = 'click:' + (e.target.id || e.target.tagName) }, true)</script>
</body></html>"""

SCENARIO = r"""
import json, os, sys
from gogl_ui import navigation, web, webpad
web.prepare_environment()
from PySide6.QtCore import QEvent, QEventLoop, QPointF, Qt, QTimer, QUrl
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QHBoxLayout, QMainWindow, QPushButton, QVBoxLayout, QWidget

app = QApplication([])
nav = navigation.install(app, require_active=False)
nav.devices = {}
win = QMainWindow()
body = QWidget()
win.setCentralWidget(body)
win.resize(1000, 700)
lay = QVBoxLayout(body)
bar = QHBoxLayout()
lay.addLayout(bar)
left, right = QPushButton("izq"), QPushButton("dcha")
bar.addWidget(left)
bar.addStretch(1)
bar.addWidget(right)
view = web.make_view(body)
lay.addWidget(view, 1)
win.show()
win.activateWindow()
out = {}

def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()

def js(expr):
    res, loop = [], QEventLoop()
    view.page().runJavaScript(f"JSON.stringify({expr})", 0, lambda r: (res.append(r), loop.quit()))
    loop.exec()
    return json.loads(res[0]) if res[0] else None

def marked():
    return js("(document.querySelector('.__gogl_pad_focus') || {}).id || null")

def press(name):
    nav.handle(name, True)
    nav.handle(name, False)
    wait(200)

done = []
view.loadFinished.connect(done.append)
view.setHtml(os.environ["PAGE"], QUrl("http://localhost/"))
while not done:
    wait(50)
wait(300)
nav.set_active(True)
nav.mouse_watch.stop()
navigation.focus_widget(left)
press("down")
out["enter"] = [marked(), webpad.view_of(QApplication.focusWidget()) is view]
press("right"); press("down")
out["moved"] = marked()
press("accept")
out["click"] = view.title()
press("down"); press("down")
out["scrolled"] = [marked(), js("scrollY") > 0]
for _ in range(8):
    press("up")
out["left_web"] = [QApplication.focusWidget().text() if isinstance(QApplication.focusWidget(), QPushButton) else None,
                   marked()]
press("down")
nav._page(None, 1)
wait(200)
out["page"] = js("scrollY")

# El dedo (en gamescope llega como ratón): arrastrar desplaza y no hace clic.
js("scrollTo(0, 0)")
view.page().runJavaScript("document.title = 'nada'")
t = view.focusProxy()
def mouse(kind, y, button, buttons):
    p = QPointF(40, y)
    QApplication.sendEvent(t, QMouseEvent(kind, p, QPointF(t.mapToGlobal(p.toPoint())), button, buttons, Qt.NoModifier))
mouse(QEvent.MouseMove, 400, Qt.NoButton, Qt.NoButton)
mouse(QEvent.MouseButtonPress, 400, Qt.LeftButton, Qt.LeftButton)
for i in range(1, 11):
    mouse(QEvent.MouseMove, 400 - i * 25, Qt.NoButton, Qt.LeftButton)
    wait(16)
mouse(QEvent.MouseButtonRelease, 150, Qt.LeftButton, Qt.NoButton)
wait(150)
out["drag"] = [js("scrollY"), view.title()]
print("RESULT " + json.dumps(out), flush=True)
os._exit(0)
"""


def test_gamepad_and_touch_inside_web_page(tmp_path):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QTWEBENGINE_DISABLE_SANDBOX="1",
               XDG_CURRENT_DESKTOP="gamescope", GOGL_HOME=str(tmp_path), PAGE=PAGE,
               PYTHONPATH=str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    for k in ("QT_QUICK_BACKEND", "QTWEBENGINE_CHROMIUM_FLAGS", "QTWEBENGINEPROCESS_PATH", "LD_PRELOAD"):
        env.pop(k, None)
    run = subprocess.run([sys.executable, "-c", SCENARIO], env=env, capture_output=True, text=True, timeout=120)
    line = next((ln for ln in run.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line, run.stdout + run.stderr
    out = json.loads(line[len("RESULT "):])
    assert out["enter"] == ["a1", True]          # desde el botón de la izquierda, el enlace de debajo
    assert out["moved"] == "a5"                   # dos → abajo: cinco, el que está debajo
    assert out["click"] == "click:a5"             # A es un clic de verdad en el enlace
    assert out["scrolled"] == ["q", True]         # sin nada más abajo, la página baja
    assert out["left_web"] == ["izq", None]       # por arriba se sale a la interfaz, sin marca en la web
    assert out["page"] > 300                      # RT: una pantalla más abajo
    scroll_y, title = out["drag"]
    assert scroll_y > 150 and title == "nada"     # arrastrar desplaza y no hace clic
