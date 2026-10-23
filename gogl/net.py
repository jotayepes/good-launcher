"""Espera a que haya red. Al volver de suspensión en SteamOS el wifi tarda unos
segundos: sincronizar antes de tiempo fallaría en silencio."""

from __future__ import annotations

import socket
import time

from .i18n import _

PROBE_HOST = ("cloudstorage.gog.com", 443)


def network_ready(timeout: float = 5) -> bool:
    try:
        with socket.create_connection(PROBE_HOST, timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_network(max_wait: float = 30, log=lambda _: None) -> bool:
    deadline = time.monotonic() + max_wait
    first = True
    while True:
        if network_ready():
            return True
        if time.monotonic() >= deadline:
            return False
        if first:
            log(_("esperando conexión de red…"))
            first = False
        time.sleep(2)
