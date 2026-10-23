"""Cola de descargas (instalar, actualizar, reparar, extras e instaladores offline) y su página."""

from __future__ import annotations

import copy
import threading
import traceback
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from gogl.content import Cancelled, Installer, Progress, install_game, update_game
from gogl.content.offline import download_bundle

from . import theme
from .game_page import clear
from .workers import Backend

KIND_LABEL = {"install": "Instalación", "update": "Actualización", "repair": "Reparación",
              "offline": "Descarga"}


@dataclass
class Job:
    game_id: str
    title: str
    kind: str                       # install | update | repair | offline
    params: dict = field(default_factory=dict)
    # Identifica el trabajo en la cola: el game_id, salvo las descargas offline
    # (varias por juego), que usan la clave del paquete.
    key: str = ""
    status: str = "queued"          # queued | running | done | failed | cancelled
    progress: Progress = field(default_factory=Progress)
    error: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self):
        self.key = self.key or self.game_id

    @property
    def label(self) -> str:
        return self.params.get("label") or KIND_LABEL[self.kind]


class DownloadManager(QObject):
    changed = Signal()
    progressed = Signal(str)               # clave del trabajo
    finished = Signal(str, bool, str)      # game_id, ok, mensaje
    _progress_sig = Signal(str, object)
    _done_sig = Signal(str, str, str)      # clave, status, error

    def __init__(self):
        super().__init__()
        self.jobs: list[Job] = []
        self._progress_sig.connect(self._on_progress)
        self._done_sig.connect(self._on_done)

    def active(self, key: str) -> Job | None:
        return next((j for j in self.jobs if j.key == key and j.status in ("queued", "running")), None)

    def find(self, key: str) -> Job | None:
        return next((j for j in reversed(self.jobs) if j.key == key), None)

    def pending_count(self) -> int:
        return sum(1 for j in self.jobs if j.status in ("queued", "running"))

    def enqueue(self, job: Job) -> None:
        if self.active(job.key):
            return
        self.jobs = [j for j in self.jobs if j.key != job.key]
        self.jobs.append(job)
        self.changed.emit()
        self._next()

    def cancel(self, key: str) -> None:
        j = self.active(key)
        if not j:
            return
        j.cancel.set()
        if j.status == "queued":
            j.status = "cancelled"
            self.changed.emit()

    def retry(self, key: str) -> None:
        j = next((j for j in self.jobs if j.key == key), None)
        if j and j.status in ("failed", "cancelled"):
            self.enqueue(Job(j.game_id, j.title, j.kind, j.params, key=j.key))

    def clear_finished(self) -> None:
        self.jobs = [j for j in self.jobs if j.status in ("queued", "running")]
        self.changed.emit()

    def _next(self):
        if any(j.status == "running" for j in self.jobs):
            return
        job = next((j for j in self.jobs if j.status == "queued"), None)
        if not job:
            return
        job.status = "running"
        self.changed.emit()
        threading.Thread(target=self._work, args=(job,), daemon=True).start()

    def _work(self, job: Job):
        def progress(p: Progress):
            self._progress_sig.emit(job.key, copy.copy(p))

        try:
            b = Backend()
            if job.kind == "install":
                install_game(b.api, job.game_id, progress=progress, cancel=job.cancel, state=b.state,
                             **job.params)
            elif job.kind == "update":
                update_game(b.api, job.game_id, progress=progress, cancel=job.cancel, state=b.state)
            elif job.kind == "repair":
                Installer(b.api, b.state, progress, job.cancel).repair(job.game_id)
            elif job.kind == "offline":
                download_bundle(b.api, job.game_id, job.key, progress, job.cancel)
            self._done_sig.emit(job.key, "done", "")
        except Cancelled:
            self._done_sig.emit(job.key, "cancelled", "")
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._done_sig.emit(job.key, "failed", str(e))

    def _on_progress(self, key: str, p: Progress):
        j = self.active(key)
        if j:
            j.progress = p
            self.progressed.emit(key)

    def _on_done(self, key: str, status: str, error: str):
        j = next((j for j in self.jobs if j.key == key and j.status == "running"), None)
        if j:
            j.status, j.error = status, error
            msg = {"done": f"{j.label}: completada", "cancelled": f"{j.label}: cancelada",
                   "failed": error}[status]
            self.finished.emit(j.game_id, status == "done", msg)
        self.changed.emit()
        self._next()


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def eta(p: Progress) -> str:
    if p.speed <= 0 or not p.download_total:
        return ""
    s = int((p.download_total - p.downloaded) / p.speed)
    if s >= 3600:
        return f"{s // 3600} h {s % 3600 // 60} min"
    if s >= 60:
        return f"{s // 60} min {s % 60} s"
    return f"{s} s" if s else "menos de 1 s"


def status_text(j: Job) -> str:
    p = j.progress
    if j.status == "queued":
        return "En cola"
    if j.status == "done":
        return f"{j.label}: completada" if j.kind == "offline" else f"{KIND_LABEL[j.kind]} completada"
    if j.status == "cancelled":
        return "Cancelada · puedes reanudarla: lo descargado no se pierde"
    if j.status == "failed":
        return f"Error: {j.error}"
    if p.stage == "prepare":
        return "Preparando: leyendo la lista de archivos de GOG…"
    if p.stage == "verify":
        return f"Verificando archivos · {p.files_done}/{p.files_total}"
    if p.stage == "finalize":
        return "Terminando…"
    bits = [f"{human(p.downloaded)} de {human(p.download_total)}"]
    if p.speed:
        bits.append(f"{human(p.speed)}/s")
    if eta(p):
        bits.append(f"quedan {eta(p)}")
    return " · ".join(bits)


class JobRow(QFrame):
    def __init__(self, job: Job, manager: DownloadManager):
        super().__init__()
        self.setObjectName("Card")
        self.job = job
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        top = QHBoxLayout()
        self.title = QLabel(f"<b>{job.title}</b>  <span style='color:{theme.MUTED}'>· {job.label}</span>")
        top.addWidget(self.title, 1)
        self.pct = QLabel()
        self.pct.setStyleSheet("font-weight: 700;")
        top.addWidget(self.pct)
        self.btn = QPushButton()
        self.btn.setObjectName("Ghost")
        self.btn.clicked.connect(self._action)
        top.addWidget(self.btn)
        lay.addLayout(top)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 1000)
        lay.addWidget(self.bar)
        self.status = QLabel()
        self.status.setObjectName("Muted")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.manager = manager
        self.refresh()

    def refresh(self):
        j = self.job
        running = j.status in ("queued", "running")
        self.btn.setText("Cancelar" if running else "Reanudar" if j.status in ("failed", "cancelled") else "")
        self.btn.setVisible(bool(self.btn.text()))
        indeterminate = j.status == "running" and j.progress.stage in ("prepare", "finalize")
        self.bar.setRange(0, 0 if indeterminate else 1000)
        self.bar.setValue(1000 if j.status == "done" else int(j.progress.fraction * 1000))
        self.pct.setText(f"{j.progress.fraction * 100:.0f}%" if j.status == "running" and not indeterminate else "")
        self.status.setText(status_text(j))
        color = {"failed": theme.DANGER, "done": theme.OK}.get(j.status)
        self.status.setStyleSheet(f"color: {color};" if color else "")

    def _action(self):
        if self.job.status in ("queued", "running"):
            self.manager.cancel(self.job.key)
        else:
            self.manager.retry(self.job.key)


class DownloadsPage(QWidget):
    def __init__(self, manager: DownloadManager):
        super().__init__()
        self.manager = manager
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 22, 28, 0)
        head = QHBoxLayout()
        t = QLabel("Descargas")
        t.setStyleSheet("font-size: 20pt; font-weight: 800;")
        head.addWidget(t)
        head.addStretch()
        clr = QPushButton("Limpiar terminadas")
        clr.setObjectName("Ghost")
        clr.clicked.connect(manager.clear_finished)
        head.addWidget(clr)
        outer.addLayout(head)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        scroll.setWidget(body)
        self.list = QVBoxLayout(body)
        self.list.setContentsMargins(0, 12, 0, 20)
        self.list.setSpacing(12)
        outer.addWidget(scroll, 1)
        self.rows: dict[str, JobRow] = {}
        manager.changed.connect(self.rebuild)
        manager.progressed.connect(lambda gid: self.rows[gid].refresh() if gid in self.rows else None)
        self.rebuild()

    def rebuild(self):
        clear(self.list)
        self.rows = {}
        if not self.manager.jobs:
            e = QLabel("No hay descargas. Instala un juego desde su ficha en la biblioteca.")
            e.setObjectName("Muted")
            e.setAlignment(Qt.AlignCenter)
            self.list.addWidget(e)
        for j in reversed(self.manager.jobs):
            row = JobRow(j, self.manager)
            self.rows[j.key] = row
            self.list.addWidget(row)
        self.list.addStretch()


__all__ = ["DownloadManager", "DownloadsPage", "Job", "human"]
