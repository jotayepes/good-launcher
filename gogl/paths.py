"""Rutas XDG del lanzador. Se pueden redirigir con GOGL_HOME (útil en tests)."""

import os
from pathlib import Path


def _xdg(env: str, default: str) -> Path:
    base = os.environ.get("GOGL_HOME")
    if base:
        return Path(base) / env.lower()
    return Path(os.environ.get(env) or Path.home() / default) / "gogl"


def config_dir() -> Path:
    return _ensure(_xdg("XDG_CONFIG_HOME", ".config"))


def data_dir() -> Path:
    return _ensure(_xdg("XDG_DATA_HOME", ".local/share"))


def state_dir() -> Path:
    return _ensure(_xdg("XDG_STATE_HOME", ".local/state"))


def cache_dir() -> Path:
    return _ensure(_xdg("XDG_CACHE_HOME", ".cache"))


def locks_dir() -> Path:
    return _ensure(state_dir() / "locks")


def backups_dir() -> Path:
    return _ensure(data_dir() / "backups")


def _ensure(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p
