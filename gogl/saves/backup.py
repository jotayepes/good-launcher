"""Copias de seguridad locales antes de sobrescribir o borrar partidas."""

from __future__ import annotations

import re
import tarfile
import time
from pathlib import Path

from .. import paths
from .locations import SaveLocation

KEEP = 10


def backup_locations(game_id: str, locations: list[SaveLocation], keep: int = KEEP,
                     root: Path | None = None) -> Path | None:
    existing = [l for l in locations if l.path.is_dir() and any(l.path.iterdir())]
    if not existing:
        return None
    d = (root or paths.backups_dir()) / game_id
    d.mkdir(parents=True, exist_ok=True)
    out = d / time.strftime("%Y%m%d-%H%M%S.tar.gz")
    n = 1
    while out.exists():
        out = d / time.strftime(f"%Y%m%d-%H%M%S-{n}.tar.gz")
        n += 1
    tmp = out.with_suffix(".part")
    with tarfile.open(tmp, "w:gz") as tar:
        for loc in existing:
            tar.add(loc.path, arcname=loc.name)
    tmp.rename(out)
    for old in sorted(d.glob("*.tar.gz"), key=_age)[:-keep]:
        old.unlink(missing_ok=True)
    return out


def _age(p: Path) -> tuple:
    """Orden cronológico: por nombre, «…-101010-1» quedaría antes que «…-101010»."""
    m = re.fullmatch(r"(\d{8}-\d{6})(?:-(\d+))?\.tar\.gz", p.name)
    return (m.group(1), int(m.group(2) or 0)) if m else ("", 0)
