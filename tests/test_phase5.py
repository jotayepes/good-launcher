"""Fases 5–7: extras e instaladores offline, logros (Comet) y perfil."""

import hashlib
import json
import os
import stat
import threading
import time

import httpx
import pytest
import respx

from gogl import achievements, profile
from gogl.api import GogApi
from gogl.auth import Credentials
from gogl.content import offline
from gogl.content.installer import Cancelled
from gogl.runner import comet
from gogl.state import Game, State

PRODUCT = "https://api.gog.com/products/42"
CDN = "https://cdn.test/secure"


class FakeAuth:
    def __init__(self):
        self.calls = []

    def credentials(self, client_id=None, client_secret=None, force_refresh=False):
        self.calls.append((client_id, force_refresh))
        return Credentials("tok-" + (client_id or "galaxy"), "7", 9e18)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GOGL_HOME", str(tmp_path / "home"))
    return tmp_path


def md5(b):
    return hashlib.md5(b).hexdigest()


# --- extras e instaladores offline ------------------------------------------------

class FakeOffline:
    """api.gog.com + CDN de GOG para las descargas offline (con Range y XML de checksum)."""

    def __init__(self, router, files: dict[str, bytes], chunk=4, with_xml=True):
        self.files, self.chunk, self.with_xml = files, chunk, with_xml
        self.corrupt_once: set[int] = set()     # posiciones de trozo que llegan mal la 1.ª vez
        self.expire_once = False
        self.ranges: list[str] = []
        self.resolves = 0
        router.get(PRODUCT).mock(side_effect=self.product)
        router.get(url__regex=r"https://api\.gog\.com/products/42/downlink/.*").mock(side_effect=self.downlink)
        router.route(url__startswith=CDN).mock(side_effect=self.cdn)

    def product(self, request):
        assert request.url.params["expand"] == "downloads,expanded_dlcs"
        inst = [{"id": f"f{i}", "size": 1 << 20, "downlink": f"https://api.gog.com/products/42/downlink/installer/f{i}"}
                for i, _ in enumerate(self.files)]
        return httpx.Response(200, json={
            "id": 42, "title": "Juego: de Prueba",
            "downloads": {
                "installers": [{"id": "installer_windows_es", "name": "Juego", "os": "windows", "language": "es",
                                "language_full": "español", "version": "1.0", "total_size": 2 << 20,
                                "files": inst},
                               {"id": "installer_linux_en", "name": "Juego", "os": "linux", "language": "en",
                                "version": "1.0", "total_size": 1, "files": []}],
                "bonus_content": [{"id": 9, "name": "manual", "type": "manuals", "total_size": 1 << 20,
                                   "files": [{"id": 9, "size": 1 << 20,
                                              "downlink": "https://api.gog.com/products/42/downlink/product_bonus/9"}]}]},
            "expanded_dlcs": [{"id": 43, "title": "DLC", "downloads": {"installers": [], "bonus_content": []}}],
        })

    def name(self, fid: str) -> str:
        return {f"f{i}": n for i, n in enumerate(self.files)}.get(fid, "manual.zip")

    def downlink(self, request):
        self.resolves += 1
        fid = request.url.path.rsplit("/", 1)[1]
        name = self.name(fid if fid != "9" else "x")
        tok = self.resolves
        return httpx.Response(200, json={"downlink": f"{CDN}/{tok}/{name}",
                                         "checksum": f"{CDN}/{tok}/{name}.xml"})

    def data(self, name: str) -> bytes:
        return self.files.get(name, b"MANUAL-PDF-1234567")

    def cdn(self, request):
        _, tok, name = request.url.path.rsplit("/", 2)
        if name.endswith(".xml"):
            if not self.with_xml:
                return httpx.Response(404)
            data = self.data(name[:-4])
            chunks = "".join(
                f'<chunk id="{i}" from="{o}" to="{min(o + self.chunk, len(data)) - 1}" method="md5">'
                f"{md5(data[o:o + self.chunk])}</chunk>"
                for i, o in enumerate(range(0, len(data), self.chunk)))
            return httpx.Response(200, text=f'<file name="{name[:-4]}" md5="{md5(data)}" '
                                            f'total_size="{len(data)}">{chunks}</file>')
        if self.expire_once:
            self.expire_once = False
            return httpx.Response(403)
        data = self.data(name)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(data))})
        rng = request.headers.get("range")
        self.ranges.append(rng)
        start, end = 0, len(data) - 1
        if rng:
            a, b = rng.split("=")[1].split("-")
            start, end = int(a), int(b or len(data) - 1)
        body = bytearray(data[start:end + 1])
        for pos in sorted(self.corrupt_once):
            if start <= pos <= end:
                self.corrupt_once.discard(pos)
                body[pos - start] ^= 0xFF
        return httpx.Response(206 if rng else 200, content=bytes(body))


def api(router=None):
    return GogApi(FakeAuth(), httpx.Client())


def test_list_downloads_parses_installers_extras_and_skips_empty():
    with respx.mock(assert_all_called=False) as r:
        FakeOffline(r, {"setup.exe": b"x", "setup-1.bin": b"y"})
        off = offline.list_downloads(api(), "42")
    assert [b.id for b in off.installers] == ["installer_windows_es"]  # el de Linux no tiene archivos
    b = off.installers[0]
    assert b.os == "windows" and b.language_name == "español" and len(b.files) == 2
    assert b.key == "42:installer:installer_windows_es"
    assert [e.type_label for e in off.extras] == ["Manual"]


def test_download_multipart_installer_verifies_chunks_and_repairs_bad_one(home):
    files = {"setup.exe": b"0123456789abcdef", "setup-1.bin": b"ZYXWVUTSRQ"}
    with respx.mock(assert_all_called=False) as r:
        cdn = FakeOffline(r, files)
        cdn.corrupt_once = {4}  # el 2.º trozo llega mal: solo se repite ese
        b = offline.list_downloads(api(), "42").installers[0]
        dest = home / "dl"
        out = offline.OfflineDownloader(api()).download(b, dest)
    assert [p.read_bytes() for p in out] == list(files.values())
    assert "bytes=4-7" in cdn.ranges  # reparación del trozo 4..7
    assert not list(dest.glob("*.gogl-part"))
    assert offline._index(dest) == {"f0": "setup.exe", "f1": "setup-1.bin"}


def test_download_resumes_from_partial_file(home):
    data = b"A" * 10 + b"B" * 10
    with respx.mock(assert_all_called=False) as r:
        cdn = FakeOffline(r, {"setup.exe": data})
        b = offline.list_downloads(api(), "42").installers[0]
        dest = home / "dl"
        dest.mkdir()
        (dest / "setup.exe.gogl-part").write_bytes(data[:12])
        offline.OfflineDownloader(api()).download(b, dest)
    assert (dest / "setup.exe").read_bytes() == data
    assert cdn.ranges[0] == "bytes=12-19"


def test_expired_link_is_renewed(home):
    with respx.mock(assert_all_called=False) as r:
        cdn = FakeOffline(r, {"setup.exe": b"hola mundo!!"})
        cdn.expire_once = True
        b = offline.list_downloads(api(), "42").installers[0]
        offline.OfflineDownloader(api()).download(b, home / "dl")
    assert (home / "dl" / "setup.exe").read_bytes() == b"hola mundo!!"
    assert cdn.resolves == 2


def test_extra_without_checksum_uses_real_size_from_server(home):
    with respx.mock(assert_all_called=False) as r:
        FakeOffline(r, {}, with_xml=False)
        b = offline.list_downloads(api(), "42").extras[0]
        assert b.total_size == 1 << 20  # la API redondea a MB
        out = offline.OfflineDownloader(api()).download(b, home / "x")
    assert out[0].read_bytes() == b"MANUAL-PDF-1234567"


def test_cancel_keeps_partial_and_bundle_dir_layout(home, monkeypatch):
    monkeypatch.setattr(offline.config, "load", lambda: {"offline_root": str(home / "GOG")})
    cancel = threading.Event()
    cancel.set()
    with respx.mock(assert_all_called=False) as r:
        FakeOffline(r, {"setup.exe": b"datos"})
        b = offline.list_downloads(api(), "42").installers[0]
        with pytest.raises(Cancelled):
            offline.OfflineDownloader(api(), cancel=cancel).download(b)
    assert offline.bundle_dir(b) == home / "GOG" / "Juego de Prueba" / "Instaladores" / "windows-es-1.0"
    assert offline.downloaded_files(b) is None


def test_parse_checksum_rejects_garbage():
    assert offline.parse_checksum("<html>no</html>") is None
    assert offline.parse_checksum("no es xml") is None


# --- logros ---------------------------------------------------------------------

def ach(i, unlocked=None, visible=True, rarity=10.0):
    return {"achievement_id": str(i), "achievement_key": f"K{i}", "name": f"Logro {i}", "description": "d",
            "visible": visible, "image_url_locked": f"https://img/{i}l.png",
            "image_url_unlocked": f"https://img/{i}u.png", "date_unlocked": unlocked, "rarity": rarity,
            "rarity_level_description": "Raro"}


def test_achievements_fetch_paginates_caches_and_works_offline(home):
    st = State(home / "s.db")
    st.save_game(Game("42", "J", "windows", home / "g", client_id="cid", client_secret="sec"))
    pages = [
        {"total_count": 3, "page_token": "p2", "items": [ach(1, "2025-03-12T10:00:00+0000"), ach(2)]},
        {"total_count": 3, "page_token": None, "items": [ach(3, visible=False)]},
    ]
    with respx.mock() as r:
        route = r.get("https://gameplay.gog.com/clients/cid/users/7/achievements").mock(
            side_effect=[httpx.Response(200, json=p) for p in pages])
        a = GogApi(FakeAuth(), httpx.Client())
        items = achievements.fetch(a, a.auth, st, "42")
    assert route.calls[1].request.url.params["page_token"] == "p2"
    assert [x.unlocked for x in items] == [True, False, False]
    assert items[0].unlocked_at == pytest.approx(1741773600)
    assert st.achievement_summary() == {"42": (1, 3)}
    with respx.mock() as r:
        r.get("https://gameplay.gog.com/clients/cid/users/7/achievements").mock(side_effect=httpx.ConnectError("x"))
        assert len(achievements.fetch(a, a.auth, st, "42")) == 3  # copia local


def test_credentials_for_uninstalled_game_are_fetched_once(home, monkeypatch):
    st = State(home / "s.db")
    a = GogApi(FakeAuth(), httpx.Client())
    calls = []

    def cc(gid, platform="windows"):
        calls.append(platform)
        if gid == "1":
            return "cid1", "s1"
        raise achievements.ApiError("sin builds")

    monkeypatch.setattr(a, "client_credentials", cc)
    assert achievements.credentials(a, st, "1") == ("cid1", "s1")
    assert achievements.credentials(a, st, "1") == ("cid1", "s1")
    assert achievements.credentials(a, st, "2") is None
    assert achievements.credentials(a, st, "2") is None  # no se repite cada vez
    assert calls == ["windows", "windows", "osx"]


def test_newly_unlocked():
    before = achievements.parse([ach(1, "2025-01-01T00:00:00+0000"), ach(2)])
    after = achievements.parse([ach(1, "2025-01-01T00:00:00+0000"), ach(2, "2025-02-01T00:00:00+0000")])
    assert [a.key for a in achievements.newly_unlocked(before, after)] == ["K2"]


# --- Comet ----------------------------------------------------------------------

def test_uses_galaxy_sdk(tmp_path):
    (tmp_path / "bin" / "x64").mkdir(parents=True)
    assert not comet.uses_galaxy_sdk(tmp_path)
    (tmp_path / "bin" / "x64" / "Galaxy64.dll").write_bytes(b"")
    assert comet.uses_galaxy_sdk(tmp_path)


def test_enabled_respects_settings_and_game(monkeypatch):
    g = Game("1", "J", "windows", "/x")
    monkeypatch.setattr(comet.config, "load", lambda: {})
    assert comet.enabled(g)
    g.extra["achievements"] = False
    assert not comet.enabled(g)
    monkeypatch.setattr(comet.config, "load", lambda: {"achievements": False})
    assert not comet.enabled(Game("1", "J", "windows", "/x"))


def test_comet_process_reports_unlocks_and_hides_tokens(home, monkeypatch, tmp_path):
    """Un «Comet» de mentira: comprueba que recibe los tokens por archivo (no por
    argumentos), que la línea de desbloqueo llega a la notificación y que al pararlo
    se borra el archivo de credenciales."""
    fake = tmp_path / "comet"
    fake.write_text(
        "#!/bin/sh\n"
        'cat "$XDG_CONFIG_PATH/heroic/gog_store/auth.json" > "$0.auth"\n'
        'echo "$@" > "$0.args"\n'
        'echo "[x INFO  comet] Unlocking achievement ALL_IN, All In" >&2\n'
        "trap 'exit 0' INT\n"
        "while true; do sleep 0.1; done\n")
    fake.chmod(0o755)
    monkeypatch.setattr(comet, "binary", lambda: fake)
    got = []
    log = open(tmp_path / "log.txt", "w")
    c = comet.Comet({"access_token": "AT", "refresh_token": "RT", "user_id": "7"}, "yo", log,
                    lambda k, n: got.append((k, n)))
    c.start()
    for _ in range(50):
        if got:
            break
        time.sleep(0.1)
    auth_dir = c.auth_dir
    assert stat.S_IMODE(os.stat(auth_dir).st_mode) == 0o700
    c.stop(grace=5)
    log.close()
    assert got == [("ALL_IN", "All In")] and c.unlocked == got
    assert json.loads((tmp_path / "comet.auth").read_text()) == {
        comet.GALAXY_CLIENT_ID: {"access_token": "AT", "refresh_token": "RT", "user_id": "7"}}
    args = (tmp_path / "comet.args").read_text()
    assert "RT" not in args and "--from-heroic" in args and "--username yo" in args
    assert not auth_dir.exists()
    assert "[comet]" in (tmp_path / "log.txt").read_text()


def test_comet_download_checks_sha(home, monkeypatch):
    monkeypatch.setitem(comet.SHA256, comet.BINARY, "0" * 64)
    with respx.mock() as r:
        r.get(url__regex=r".*/comet-x86_64-unknown-linux-gnu").mock(return_value=httpx.Response(200, content=b"evil"))
        with pytest.raises(comet.CometError):
            comet.ensure(httpx.Client())
    assert not comet.binary().exists()


# --- perfil ---------------------------------------------------------------------

def test_price_and_member_since():
    assert profile.price(4999, "EUR") == "49,99 €"
    assert profile.price(123456, "EUR") == "1.234,56 €"
    assert profile.price(0, "EUR") == "Gratis"
    assert profile.price(999, "USD") == "9,99 USD"
    assert profile.member_since("2011-04-22T10:54:05+00:00") == "abril de 2011"


def test_wishlist_with_prices_sorted_by_discount():
    with respx.mock() as r:
        r.get("https://api.gog.com/products").mock(return_value=httpx.Response(200, json=[
            {"id": 1, "title": "Caro", "slug": "caro"}, {"id": 2, "title": "Rebajado", "slug": "rebajado"}]))
        r.get("https://api.gog.com/products/prices").mock(return_value=httpx.Response(200, json={"_embedded": {
            "items": [
                {"_embedded": {"product": {"id": 1}, "prices": [
                    {"currency": {"code": "EUR"}, "basePrice": "5999 EUR", "finalPrice": "5999 EUR"}]}},
                {"_embedded": {"product": {"id": 2}, "prices": [
                    {"currency": {"code": "EUR"}, "basePrice": "2000 EUR", "finalPrice": "500 EUR"}]}}]}}))
        items = profile.wishlist_items(GogApi(FakeAuth(), httpx.Client()), ["1", "2"], "ES", "EUR")
    assert [(w.title, w.discount) for w in items] == [("Rebajado", 75), ("Caro", 0)]
    assert items[0].url == "/game/rebajado"


def test_achievement_stats(home):
    st = State(home / "s.db")
    st.set_achievements("1", [ach(1, "2025-01-01T00:00:00+0000"), ach(2, "2025-03-01T00:00:00+0000")])
    st.set_achievements("2", [ach(3), ach(4, "2025-02-01T00:00:00+0000")])
    st.set_achievements("3", [])
    s = profile.achievement_stats(st)
    assert (s.unlocked, s.total, s.games, s.perfect) == (3, 4, 2, 1)
    assert [a.key for _, a in s.recent] == ["K2", "K4", "K1"]
