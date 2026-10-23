"""Estado persistente en SQLite: juegos registrados, base de sincronización y estado."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    platform TEXT NOT NULL,          -- windows | linux
    install_path TEXT NOT NULL,
    prefix TEXT,                     -- compatdata/prefijo de Proton (solo windows)
    client_id TEXT,
    client_secret TEXT,
    extra TEXT NOT NULL DEFAULT '{}' -- JSON: overrides de rutas, etc.
);
-- Último estado acordado por archivo: MD5 del contenido local y hash del objeto en la nube.
CREATE TABLE IF NOT EXISTS sync_base (
    game_id TEXT NOT NULL,
    key TEXT NOT NULL,               -- clave normalizada (casefold)
    local_md5 TEXT NOT NULL,
    cloud_hash TEXT NOT NULL,
    PRIMARY KEY (game_id, key)
);
CREATE TABLE IF NOT EXISTS sync_status (
    game_id TEXT PRIMARY KEY,
    pending INTEGER NOT NULL DEFAULT 0,  -- 1 = hay que volver a sincronizar
    last_ok REAL,
    last_error TEXT,
    updated REAL
);
-- Sesiones de juego: el vigilante usa esto para saber si el wrapper terminó su post-sync.
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    wrapper_pid INTEGER,
    proton TEXT,
    compat TEXT,
    started REAL,
    ended REAL,
    post_synced INTEGER NOT NULL DEFAULT 0,  -- 0 no, 1 sí, 2 lo intentó el vigilante
    note TEXT
);
-- Juegos comprados (caché de la biblioteca de GOG).
CREATE TABLE IF NOT EXISTS library (
    game_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    data TEXT NOT NULL               -- JSON del producto tal cual lo da GOG
);
-- Credenciales de cada producto (también de los no instalados: los logros las necesitan).
CREATE TABLE IF NOT EXISTS clients (
    game_id TEXT PRIMARY KEY,
    client_id TEXT,                  -- NULL = el producto no tiene (sin logros ni nube)
    client_secret TEXT,
    fetched REAL
);
-- Última lista de logros conocida (para verla sin conexión y detectar los nuevos).
CREATE TABLE IF NOT EXISTS achievements (
    game_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,              -- JSON: items tal cual los da gameplay.gog.com
    fetched REAL
);
CREATE TABLE IF NOT EXISTS hash_cache (
    path TEXT PRIMARY KEY,
    size INTEGER, mtime_ns INTEGER, ino INTEGER,
    md5 TEXT
);
"""


@dataclass
class Game:
    game_id: str
    title: str
    platform: str
    install_path: Path
    prefix: Path | None = None
    client_id: str | None = None
    client_secret: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def save_overrides(self) -> dict[str, str]:
        return self.extra.get("save_overrides", {})


@dataclass(frozen=True)
class BaseEntry:
    local_md5: str
    cloud_hash: str


class State:
    def __init__(self, db_path: Path | None = None):
        self.path = db_path or paths.data_dir() / "state.db"
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)

    # --- juegos ---------------------------------------------------------

    def save_game(self, g: Game) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO games VALUES (?,?,?,?,?,?,?,?)",
            (g.game_id, g.title, g.platform, str(g.install_path),
             str(g.prefix) if g.prefix else None, g.client_id, g.client_secret, json.dumps(g.extra)))

    def game(self, game_id: str) -> Game | None:
        row = self.db.execute("SELECT * FROM games WHERE game_id=?", (game_id,)).fetchone()
        return self._game(row) if row else None

    def games(self) -> list[Game]:
        return [self._game(r) for r in self.db.execute("SELECT * FROM games ORDER BY title")]

    def remove_game(self, game_id: str) -> None:
        for t in ("games", "sync_base", "sync_status"):
            self.db.execute(f"DELETE FROM {t} WHERE game_id=?", (game_id,))

    @staticmethod
    def _game(r) -> Game:
        return Game(r[0], r[1], r[2], Path(r[3]), Path(r[4]) if r[4] else None, r[5], r[6],
                    json.loads(r[7] or "{}"))

    # --- base de sincronización ----------------------------------------

    def base(self, game_id: str) -> dict[str, BaseEntry]:
        rows = self.db.execute("SELECT key, local_md5, cloud_hash FROM sync_base WHERE game_id=?",
                               (game_id,))
        return {k: BaseEntry(l, c) for k, l, c in rows}

    def set_base(self, game_id: str, key: str, local_md5: str, cloud_hash: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO sync_base VALUES (?,?,?,?)",
                        (game_id, key, local_md5, cloud_hash))

    def drop_base(self, game_id: str, key: str) -> None:
        self.db.execute("DELETE FROM sync_base WHERE game_id=? AND key=?", (game_id, key))

    # --- estado ---------------------------------------------------------

    def set_status(self, game_id: str, *, pending: bool, error: str | None = None) -> None:
        now = time.time()
        self.db.execute(
            "INSERT INTO sync_status (game_id, pending, last_ok, last_error, updated) VALUES (?,?,?,?,?) "
            "ON CONFLICT(game_id) DO UPDATE SET pending=excluded.pending, last_error=excluded.last_error, "
            "updated=excluded.updated, last_ok=COALESCE(excluded.last_ok, sync_status.last_ok)",
            (game_id, int(pending), None if (pending or error) else now, error, now))

    def status(self, game_id: str) -> dict | None:
        r = self.db.execute("SELECT pending, last_ok, last_error, updated FROM sync_status WHERE game_id=?",
                            (game_id,)).fetchone()
        return dict(zip(("pending", "last_ok", "last_error", "updated"), r)) if r else None

    def pending_games(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT game_id FROM sync_status WHERE pending=1")]

    # --- sesiones -------------------------------------------------------

    def open_session(self, game_id: str, pid: int, proton: str | None, compat: str | None) -> int:
        cur = self.db.execute(
            "INSERT INTO sessions (game_id, wrapper_pid, proton, compat, started) VALUES (?,?,?,?,?)",
            (game_id, pid, proton, compat, time.time()))
        return cur.lastrowid

    def update_session(self, sid: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE sessions SET {cols} WHERE id=?", (*fields.values(), sid))

    def session(self, sid: int) -> dict | None:
        cur = self.db.execute("SELECT * FROM sessions WHERE id=?", (sid,))
        row = cur.fetchone()
        return dict(zip([c[0] for c in cur.description], row)) if row else None

    # --- biblioteca -----------------------------------------------------

    def replace_library(self, products: list[dict]) -> None:
        self.db.execute("BEGIN")
        try:
            self.db.execute("DELETE FROM library")
            self.db.executemany("INSERT INTO library VALUES (?,?,?)",
                                [(str(p["id"]), p.get("title", ""), json.dumps(p)) for p in products])
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def library(self) -> list[dict]:
        return [json.loads(r[0]) for r in self.db.execute("SELECT data FROM library ORDER BY title")]

    # --- credenciales y logros -----------------------------------------

    def client(self, game_id: str) -> tuple[str | None, str | None, float] | None:
        r = self.db.execute("SELECT client_id, client_secret, fetched FROM clients WHERE game_id=?",
                            (game_id,)).fetchone()
        return tuple(r) if r else None

    def set_client(self, game_id: str, client_id: str | None, client_secret: str | None) -> None:
        self.db.execute("INSERT OR REPLACE INTO clients VALUES (?,?,?,?)",
                        (game_id, client_id, client_secret, time.time()))

    def achievements(self, game_id: str) -> tuple[list[dict], float] | None:
        r = self.db.execute("SELECT data, fetched FROM achievements WHERE game_id=?", (game_id,)).fetchone()
        return (json.loads(r[0]), r[1]) if r else None

    def set_achievements(self, game_id: str, items: list[dict]) -> None:
        self.db.execute("INSERT OR REPLACE INTO achievements VALUES (?,?,?)",
                        (game_id, json.dumps(items), time.time()))

    def achievement_summary(self) -> dict[str, tuple[int, int]]:
        """game_id → (conseguidos, total) de todo lo que hay en caché."""
        out = {}
        for gid, data in self.db.execute("SELECT game_id, data FROM achievements"):
            items = json.loads(data)
            if items:
                out[gid] = (sum(1 for i in items if i.get("date_unlocked")), len(items))
        return out

    # --- caché de hashes -----------------------------------------------

    def cached_md5(self, path: Path, st) -> str | None:
        r = self.db.execute("SELECT size, mtime_ns, ino, md5 FROM hash_cache WHERE path=?",
                            (str(path),)).fetchone()
        if r and r[:3] == (st.st_size, st.st_mtime_ns, st.st_ino):
            return r[3]
        return None

    def store_md5(self, path: Path, st, md5: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO hash_cache VALUES (?,?,?,?,?)",
                        (str(path), st.st_size, st.st_mtime_ns, st.st_ino, md5))
