#!/usr/bin/env bash
# Construye dist/gogl-<versión>-x86_64.AppImage: Python propio + PySide6 + gogl,
# sin depender del Python ni de las bibliotecas de Qt del sistema (SteamOS tiene
# el sistema de solo lectura).
#
# Todo lo que se descarga tiene la versión y la suma SHA-256 fijadas aquí.
# Uso: packaging/build-appimage.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build/appimage"
CACHE="$BUILD/cache"
APPDIR="$BUILD/AppDir"

PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20260929/cpython-3.13.15%2B20260929-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
PY_SHA="c552deaf9ed0e678dee4ccc760d6dcd7cab33f1bd43e8037123b4a4db8508e14"
TOOL_URL="https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage"
TOOL_SHA="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64"
RUNTIME_SHA="2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"

VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/gogl/__init__.py")"
OUT="$ROOT/dist/gogl-$VERSION-x86_64.AppImage"

fetch() {  # url sha256 destino
    local url="$1" sha="$2" dest="$3"
    if [[ -f "$dest" ]] && echo "$sha  $dest" | sha256sum -c --quiet 2>/dev/null; then
        return
    fi
    echo ">> descargando $(basename "$dest")"
    curl -fL --retry 3 -o "$dest.part" "$url"
    echo "$sha  $dest.part" | sha256sum -c --quiet
    mv "$dest.part" "$dest"
}

mkdir -p "$CACHE" "$ROOT/dist"
fetch "$PY_URL" "$PY_SHA" "$CACHE/python.tar.gz"
fetch "$TOOL_URL" "$TOOL_SHA" "$CACHE/appimagetool"
fetch "$RUNTIME_URL" "$RUNTIME_SHA" "$CACHE/runtime-x86_64"
chmod +x "$CACHE/appimagetool"

echo ">> preparando AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr"
tar -xzf "$CACHE/python.tar.gz" -C "$APPDIR/usr"   # → usr/python
PY="$APPDIR/usr/python/bin/python3"

echo ">> instalando gogl y sus dependencias"
"$PY" -s -m pip install --quiet --no-cache-dir --disable-pip-version-check \
    --constraint "$ROOT/packaging/appimage-constraints.txt" "$ROOT[ui]"

echo ">> recortando Qt (módulos que gogl no usa)"
SITE="$("$PY" -s -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
PYSIDE="$SITE/PySide6"
"$PY" -s "$ROOT/packaging/trim_pyside.py" "$PYSIDE"
"$PY" -s -m pip uninstall --quiet --yes pip
rm -rf "$APPDIR/usr/python/lib/python3.13/test" "$APPDIR/usr/python/lib/python3.13/idlelib" \
       "$APPDIR/usr/python/lib/python3.13/tkinter" "$APPDIR/usr/python/lib/python3.13/turtledemo" \
       "$APPDIR/usr/python/include" "$APPDIR/usr/python/share"
"$PY" -s -m compileall -q -j0 "$SITE/gogl" "$SITE/gogl_ui" >/dev/null

echo ">> comprobando que arranca"
QT_QPA_PLATFORM=offscreen GOGL_HOME="$BUILD/check-home" "$PY" -s -c '
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineWidgets import QWebEngineView  # inicio de sesión
import gogl.cli, gogl_ui.main_window
app = QApplication([])
print("   Qt", __import__("PySide6").__version__, "· WebEngine OK")
'
rm -rf "$BUILD/check-home"

install -m 755 "$ROOT/packaging/AppRun" "$APPDIR/AppRun"
mkdir -p "$APPDIR/usr/share/icons/hicolor/scalable/apps"
rm -f "$APPDIR/gogl.svg"
"$PY" -s -c 'from gogl_ui.desktop import ICON_SVG; print(ICON_SVG, end="")' \
    > "$APPDIR/usr/share/icons/hicolor/scalable/apps/gogl.svg"
# .DirIcon en PNG de 256 px (no un enlace al SVG): es lo que leen los generadores
# de miniaturas de AppImage (Dolphin con libappimage, GNOME, AppImageLauncher…).
mkdir -p "$APPDIR/usr/share/icons/hicolor/256x256/apps"
QT_QPA_PLATFORM=offscreen "$PY" -s -c 'import sys
from PySide6.QtGui import QGuiApplication
app = QGuiApplication([])
from gogl_ui.icon import save_png
save_png(sys.argv[1], 256)' "$APPDIR/usr/share/icons/hicolor/256x256/apps/gogl.png"
cp "$APPDIR/usr/share/icons/hicolor/256x256/apps/gogl.png" "$APPDIR/gogl.png"
rm -f "$APPDIR/.DirIcon"
cp "$APPDIR/gogl.png" "$APPDIR/.DirIcon"
cat > "$APPDIR/gogl.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Good Launcher
GenericName=Lanzador de juegos de GOG
Comment=Tus juegos de GOG con las partidas sincronizadas
Exec=gogl
Icon=gogl
Terminal=false
Categories=Game;
StartupWMClass=gogl
EOF

echo ">> empaquetando"
rm -f "$OUT"
ARCH=x86_64 "$CACHE/appimagetool" --appimage-extract-and-run --no-appstream \
    --runtime-file "$CACHE/runtime-x86_64" "$APPDIR" "$OUT" >/dev/null
echo ">> listo: $OUT ($(du -h "$OUT" | cut -f1))"
