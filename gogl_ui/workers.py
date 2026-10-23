"""Trabajo en segundo plano sin congelar la interfaz.

SQLite y httpx no se comparten entre hilos: cada tarea recibe un `Backend`
recién creado en su propio hilo."""

from __future__ import annotations

import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from gogl.api import GogApi
from gogl.auth import AuthManager
from gogl.state import State
from gogl.sync import SyncService


class Backend:
    def __init__(self, log: Callable[[str], None] = lambda _: None):
        self.log = log
        self.state = State()
        self.auth = AuthManager()
        self.api = GogApi(self.auth)
        self.service = SyncService(self.state, self.auth, self.api, log=log)


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(object)
    progress = Signal(str)


class _Task(QRunnable):
    def __init__(self, fn, signals: _Signals):
        super().__init__()
        self.fn = fn
        self.signals = signals

    def run(self):
        try:
            backend = Backend(log=self.signals.progress.emit)
            result = self.fn(backend)
        except Exception as e:  # noqa: BLE001 - se entrega a la UI
            traceback.print_exc()
            self.signals.failed.emit(e)
        else:
            self.signals.done.emit(result)


_live: set[_Signals] = set()


def run_task(fn: Callable[[Backend], Any], on_done: Callable[[Any], None] | None = None,
             on_error: Callable[[Exception], None] | None = None,
             on_progress: Callable[[str], None] | None = None) -> None:
    """Ejecuta fn(backend) en el pool; los callbacks llegan al hilo de la UI."""
    sig = _Signals()
    _live.add(sig)  # mantener vivo hasta que termine

    def finish(cb, value):
        _live.discard(sig)
        if cb:
            cb(value)

    sig.done.connect(lambda v: finish(on_done, v))
    sig.failed.connect(lambda e: finish(on_error, e))
    if on_progress:
        sig.progress.connect(on_progress)
    QThreadPool.globalInstance().start(_Task(fn, sig))
