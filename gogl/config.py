"""Ajustes del lanzador (settings.json en el directorio de configuración)."""

from __future__ import annotations

import json
import os

from . import paths

DEFAULTS = {
    "default_proton": None,   # nombre de la versión gestionada, p. ej. "GE-Proton11-11"
}


def load() -> dict:
    try:
        data = json.loads((paths.config_dir() / "settings.json").read_text())
    except (OSError, ValueError):
        data = {}
    return {**DEFAULTS, **data}


def save(settings: dict) -> None:
    p = paths.config_dir() / "settings.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=1))
    os.replace(tmp, p)


def set_value(key: str, value) -> None:
    s = load()
    s[key] = value
    save(s)
