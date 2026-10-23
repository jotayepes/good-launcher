"""Carga de carátulas y fondos con caché en disco y en memoria."""

from __future__ import annotations

import hashlib
from collections import OrderedDict

import httpx
from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QImage, QPixmap

from gogl import paths
from gogl.library import cover_path, product_details

MEMORY_LIMIT = 400


class _Signals(QObject):
    ready = Signal(str, QImage)


class _Fetch(QRunnable):
    def __init__(self, key: str, game_id: str | None, url: str | None, fallback: str | None,
                 signals: _Signals):
        super().__init__()
        self.key, self.game_id, self.url, self.fallback, self.signals = key, game_id, url, fallback, signals

    def run(self):
        img = QImage()
        try:
            if self.game_id:
                path = cover_path(self.game_id)
                if not path.exists():
                    url = product_details(self.game_id).get("cover") or self.fallback
                    _download(url, path)
            else:
                path = _url_cache(self.url)
                if not path.exists():
                    _download(self.url, path)
            if path.exists():
                img.load(str(path))
        except (httpx.HTTPError, OSError):
            pass
        self.signals.ready.emit(self.key, img)


def _url_cache(url: str):
    return paths.cache_dir() / "images" / (hashlib.sha1(url.encode()).hexdigest() + ".img")


def _download(url: str | None, path) -> None:
    if not url:
        return
    r = httpx.get(url, timeout=20, follow_redirects=True)
    if r.status_code == 200 and r.content:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(r.content)
        tmp.replace(path)


class ImageStore(QObject):
    """`loaded(key)` se emite cuando una imagen pedida ya está disponible."""

    loaded = Signal(str)

    def __init__(self):
        super().__init__()
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(6)
        self._mem: OrderedDict[str, QPixmap] = OrderedDict()
        self._pending: set[str] = set()
        self._missing: set[str] = set()
        self._sig = _Signals()
        self._sig.ready.connect(self._on_ready)

    def cover(self, game_id: str, fallback: str | None = None) -> QPixmap | None:
        key = f"cover:{game_id}"
        return self._get(key, lambda: _Fetch(key, game_id, None, fallback, self._sig))

    def image(self, url: str | None) -> QPixmap | None:
        if not url:
            return None
        key = f"url:{url}"
        return self._get(key, lambda: _Fetch(key, None, url, None, self._sig))

    def _get(self, key, make) -> QPixmap | None:
        pm = self._mem.get(key)
        if pm is not None:
            self._mem.move_to_end(key)
            return pm
        if key not in self._pending and key not in self._missing:
            self._pending.add(key)
            self.pool.start(make())
        return None

    def _on_ready(self, key: str, img: QImage):
        self._pending.discard(key)
        if img.isNull():
            self._missing.add(key)
            return
        if key.startswith("cover:") and img.width() > 400:
            img = img.scaledToWidth(400, mode=Qt.SmoothTransformation)
        self._mem[key] = QPixmap.fromImage(img)
        while len(self._mem) > MEMORY_LIMIT:
            self._mem.popitem(last=False)
        self.loaded.emit(key)

