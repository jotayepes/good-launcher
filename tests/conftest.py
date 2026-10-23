import gzip
import os

# Los tests comparan textos en español, sea cual sea el idioma del sistema.
os.environ["GOGL_LANG"] = "es"

import pytest  # noqa: E402

from gogl.saves.cloud import CloudFile, gzip_bytes, iso_utc, md5
from gogl.saves.engine import Syncer
from gogl.saves.locations import SaveLocation
from gogl.state import Game, State


class FakeCloud:
    """Imita CloudStorage en memoria. `lose_uploads` simula el bug de «dice que
    sube pero luego no está»."""

    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.lose_uploads = 0
        self.calls: list[tuple[str, str]] = []

    def put_raw(self, key: str, data: bytes, galaxy_style: bool = False, ts: float = 1_700_000_000):
        body = gzip.compress(data, 9, mtime=12345) if galaxy_style else gzip_bytes(data)
        self.objects[key] = (body, iso_utc(ts))

    def content(self, key: str) -> bytes:
        return gzip.decompress(self.objects[key][0])

    def list(self):
        return {k: CloudFile(k, md5(b), ts, len(b)) for k, (b, ts) in self.objects.items()}

    def download(self, cf):
        self.calls.append(("download", cf.key))
        body, ts = self.objects[cf.key]
        return gzip.decompress(body), 1_700_000_000.0

    def upload(self, key, data, mtime):
        self.calls.append(("upload", key))
        body = gzip_bytes(data)
        if self.lose_uploads:
            self.lose_uploads -= 1
        else:
            self.objects[key] = (body, iso_utc(mtime))
        return md5(body)

    def delete(self, key):
        self.calls.append(("delete", key))
        self.objects.pop(key, None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("GOGL_HOME", str(tmp_path / "home"))
    saves = tmp_path / "saves"
    saves.mkdir()
    state = State(tmp_path / "state.db")
    game = Game("1", "Test", "windows", tmp_path / "game", tmp_path / "pfx", "cid", "secret")
    state.save_game(game)
    cloud = FakeCloud()
    locs = [SaveLocation("saves", "<?INSTALL?>/saves", saves)]
    backups = []

    def make():
        def backup():
            backups.append(True)
            return None
        return Syncer(state, game, locs, cloud, backup=backup)

    class Env:
        pass

    e = Env()
    e.saves, e.state, e.game, e.cloud, e.locs, e.syncer, e.backups = saves, state, game, cloud, locs, make, backups

    def write(rel, data):
        p = saves / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    e.write = write
    return e

