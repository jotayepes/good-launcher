"""Bloqueo por juego: el wrapper del juego y el vigilante post-sync nunca
sincronizan el mismo juego a la vez."""

from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager

from . import paths


class LockTimeout(Exception):
    pass


@contextmanager
def game_lock(game_id: str, timeout: float | None = None):
    path = paths.locks_dir() / f"{game_id}.lock"
    with open(path, "a") as fh:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if deadline is not None and time.monotonic() > deadline:
                    raise LockTimeout(f"El juego {game_id} está sincronizándose en otro proceso")
                time.sleep(0.5)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
