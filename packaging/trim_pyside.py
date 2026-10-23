"""Quita de PySide6 los módulos de Qt que gogl no usa (3D, multimedia, gráficas,
herramientas de desarrollo…), y comprueba con ldd que lo que queda no ha
perdido ninguna biblioteca de Qt de la que dependa.

Uso: python trim_pyside.py <ruta a site-packages/PySide6>"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Módulos de Qt sin uso en gogl. Los que necesita QtWebEngine (Quick, Qml,
# WebChannel, Positioning, OpenGL, Network…) se conservan.
UNUSED = [
    "3D", "Quick3D", "Charts", "ChartsQml", "DataVisualization", "DataVisualizationQml", "Graphs",
    "GraphsWidgets", "Multimedia", "MultimediaQuick", "MultimediaWidgets", "SpatialAudio",
    "TextToSpeech", "Location", "Sensors", "SensorsQuick", "SerialPort", "SerialBus", "Bluetooth",
    "Nfc", "Scxml", "ScxmlQml", "StateMachine", "StateMachineQml", "RemoteObjects",
    "RemoteObjectsQml", "HttpServer", "VirtualKeyboard", "VirtualKeyboardSettings", "Designer",
    "DesignerComponents", "Help", "Pdf", "PdfQuick", "PdfWidgets", "Sql", "Test", "QuickTest",
    "UiTools", "ShaderTools", "QuickTimeline", "QuickTimelineBlendTrees", "Quick3DPhysics",
    "LanguageServer", "JsonRpc", "Concurrent", "Linguist", "Canvas", "WebView", "WebViewQuick",
    "WebEngineQuick", "WebEngineQuickDelegatesQml", "WebSockets", "Protobuf", "ProtobufQtCoreTypes",
    "ProtobufQtGuiTypes", "ProtobufWellKnownTypes", "Grpc", "GrpcQuick", "QuickVectorImage",
    "Lottie", "LottieVectorImage", "Coap", "Mqtt", "OpcUa", "AxContainer",
]
UNUSED_PLUGINS = ["sqldrivers", "multimedia", "sensors", "position", "geoservices", "texttospeech",
                  "canbus", "scxmldatamodel", "renderers", "sceneparsers", "geometryloaders",
                  "assetimporters", "designer", "qmltooling", "virtualkeyboard", "renderplugins",
                  "qmllint", "help", "webview"]
KEEP_TRANSLATIONS = re.compile(r"^(qt|qtbase|qtwebengine)_(es|en)\.qm$")
KEEP_WEBENGINE_LOCALES = {"es.pak", "es-419.pak", "en-US.pak", "en-GB.pak"}


def rm(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    elif p.exists() or p.is_symlink():
        p.unlink()


def unused(name: str) -> bool:
    # Prefijo: «Quick3D» también quita libQt6Quick3DEffects, libQt6Quick3DXr…
    return any(re.match(rf"(lib)?Qt6?{re.escape(m)}", name) for m in UNUSED)


def main(pyside: Path) -> int:
    qt = pyside / "Qt"
    before = sum(f.stat().st_size for f in pyside.rglob("*") if f.is_file())
    # Herramientas de desarrollo, ejemplos y cabeceras.
    for name in ("examples", "include", "typesystems", "glue", "doc", "scripts", "support"):
        rm(pyside / name)
    for f in pyside.iterdir():
        if f.is_file() and os.access(f, os.X_OK) and f.suffix == "" and f.name != "__init__.py":
            rm(f)  # designer, assistant, linguist, lupdate, qmlls…
    for f in list(pyside.glob("Qt*.abi3.so")) + list(pyside.glob("Qt*.pyi")):
        if unused(f.name.split(".")[0]):
            rm(f)
    for f in (qt / "lib").iterdir():
        if unused(f.name):
            rm(f)
    rm(qt / "qml")
    for name in UNUSED_PLUGINS:
        rm(qt / "plugins" / name)
    if (qt / "libexec").is_dir():
        for f in (qt / "libexec").iterdir():
            if f.name != "QtWebEngineProcess":
                rm(f)
    tr = qt / "translations"
    if tr.is_dir():
        for f in tr.iterdir():
            if f.is_file() and not KEEP_TRANSLATIONS.match(f.name):
                rm(f)
        locales = tr / "qtwebengine_locales"
        if locales.is_dir():
            for f in locales.iterdir():
                if f.name not in KEEP_WEBENGINE_LOCALES:
                    rm(f)
    rc = check(pyside)
    after = sum(f.stat().st_size for f in pyside.rglob("*") if f.is_file())
    print(f"   PySide6: {before / 2**20:.0f} MB → {after / 2**20:.0f} MB")
    return rc


def missing_qt(path: Path, env: dict) -> list[str]:
    out = subprocess.run(["ldd", str(path)], capture_output=True, text=True, env=env)
    text = out.stdout + out.stderr
    return sorted({m for m in re.findall(r"(libQt6\w+\.so\.6)", text)
                   if re.search(rf"{re.escape(m)}[^\n]*not found|not found[^\n]*{re.escape(m)}", text)}
                  | ({"(versión de Qt)"} if "version `Qt_" in text else set()))


def check(pyside: Path) -> int:
    """Quita bibliotecas y plugins que se han quedado sin sus dependencias (nadie
    los puede cargar ya) y falla si se rompió algo que gogl sí usa."""
    qt = pyside / "Qt"
    env = {**os.environ, "LD_LIBRARY_PATH": f"{qt / 'lib'}:{pyside.parent / 'shiboken6'}"}
    while True:
        orphans = [f for f in list((qt / "lib").glob("*.so.6")) + list((qt / "plugins").rglob("*.so"))
                   if missing_qt(f, env)]
        if not orphans:
            break
        for f in orphans:
            print(f"   quitado (dependía de algo recortado): {f.relative_to(pyside)}")
            rm(f)
    needed = [pyside / f"{m}.abi3.so" for m in ("QtCore", "QtGui", "QtWidgets", "QtNetwork",
                                                "QtWebEngineCore", "QtWebEngineWidgets")]
    needed += [qt / "libexec" / "QtWebEngineProcess", qt / "plugins" / "platforms" / "libqxcb.so"]
    broken = [f"{f.relative_to(pyside)}: {', '.join(missing_qt(f, env)) or 'no existe'}"
              for f in needed if not f.exists() or missing_qt(f, env)]
    if broken:
        print("   Se ha roto algo que gogl necesita:\n   " + "\n   ".join(broken), file=sys.stderr)
        return 1
    for f in list(pyside.glob("Qt*.abi3.so")):
        if missing_qt(f, env):  # binding de un módulo que ya no está
            rm(f)
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
