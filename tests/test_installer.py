import hashlib
import io
import json
import os
import re
import threading
import zipfile
import zlib

import httpx
import pytest
import respx

from gogl.api import GogApi
from gogl.auth import Credentials
from gogl.content import builds as cbuilds
from gogl.content import install_game, update_game
from gogl.content.common import DEPS_META, META_V2, ContentError, galaxy_path
from gogl.content.installer import Cancelled, Installer, load_manifest
from gogl.state import State

CS = "https://content-system.gog.com"


class FakeAuth:
    def credentials(self, *a, **k):
        return Credentials("tok", "1", 9e18)


def md5(b):
    return hashlib.md5(b).hexdigest()


class FakeCDN:
    """CDN de GOG en memoria: builds, metas, manifiestos, enlaces firmados y trozos."""

    def __init__(self, chunk_size=4):
        self.chunk_size = chunk_size
        self.blobs: dict[str, bytes] = {}       # md5 comprimido → bytes comprimidos
        self.metas: dict[str, bytes] = {}       # url → contenido
        self.builds: dict[str, list] = {}
        self.chunk_hits: list[str] = []
        self.corrupt_next = 0
        self.on_hit = lambda n: None
        self.store_languages = {"en": "English", "es": "español", "fr": "français"}
        self.router = None
        self.deps: list[dict] = []
        self.dependency("MSVC2005", [self.file_item("__redist\\MSVC2005\\vcredist_x86.exe", b"VC5")],
                        exe="__redist\\MSVC2005\\vcredist_x86.exe", args="/q")

    def dependency(self, dep_id, items, exe="", args="", internal=False):
        """Entrada del repositorio de dependencias de GOG (DOSBox, Visual C++…)."""
        body = zlib.compress(json.dumps({"depot": {"items": items}}).encode())
        h = md5(body)
        self.metas[DEPS_META + galaxy_path(h)] = body
        self.deps.append({"dependencyId": dep_id, "manifest": h, "internal": internal,
                          "readableName": dep_id, "executable": {"path": exe, "arguments": args}})

    def file_item(self, path, data, flags=()):
        chunks = []
        for i in range(0, max(len(data), 1), self.chunk_size):
            raw = data[i:i + self.chunk_size]
            comp = zlib.compress(raw)
            self.blobs[md5(comp)] = comp
            chunks.append({"md5": md5(raw), "size": len(raw), "compressedMd5": md5(comp),
                           "compressedSize": len(comp)})
        it = {"type": "DepotFile", "path": path, "chunks": chunks, "flags": list(flags)}
        if len(chunks) > 1:
            it["md5"] = md5(data)
        return it

    def depot(self, items, extra=None):
        body = zlib.compress(json.dumps({"depot": {"items": items, **(extra or {})}}).encode())
        h = md5(body)
        self.metas[META_V2 + galaxy_path(h)] = body
        return h

    def build(self, gid, build_id, depots, products=None, version="1.0", **extra):
        meta = {"version": 2, "baseProductId": gid, "installDirectory": "Juego de Prueba",
                "clientId": "cid", "clientSecret": "csecret", "depots": depots,
                "products": products or [{"productId": gid, "name": "Juego de Prueba"}], **extra}
        url = f"https://cdn.test/meta/{build_id}"
        self.metas[url] = zlib.compress(json.dumps(meta).encode())
        self.builds.setdefault(gid, []).insert(0, {"build_id": build_id, "generation": 2, "link": url,
                                                   "public": True, "version_name": version})

    def mount(self, router):
        self.router = router
        router.get(url__regex=rf"{CS}/products/(\d+)/os/\w+/builds").mock(
            side_effect=lambda req: httpx.Response(200, json={
                "items": self.builds[re.search(r"products/(\d+)", str(req.url)).group(1)]}))
        router.get(url__regex=rf"{CS}/products/(\d+)/secure_link").mock(side_effect=lambda req: httpx.Response(
            200, json={"urls": [{"url_format": "https://chunks.test{path}?token={token}",
                                 "parameters": {"path": "/store/" + re.search(r"products/(\d+)", str(req.url)).group(1),
                                                "token": "t"}}]}))
        router.get(url__regex=r"https://chunks\.test/(?!.*main\.bin).*").mock(side_effect=self._chunk)
        router.get(url__regex=rf"{CS}/dependencies/repository.*").mock(
            return_value=httpx.Response(200, json={"repository_manifest": "https://cdn.test/deps.json"}))
        router.get("https://cdn.test/deps.json").mock(
            side_effect=lambda req: httpx.Response(200, content=zlib.compress(
                json.dumps({"depots": self.deps}).encode())))
        router.get(url__regex=rf"{CS}/open_link.*").mock(return_value=httpx.Response(
            200, json={"urls": [{"url_format": "https://chunks.test/deps", "parameters": {}}]}))
        router.get(url__regex=r"https://(cdn\.test|gog-cdn-fastly\.gog\.com)/.*").mock(
            side_effect=lambda req: httpx.Response(200, content=self.metas[str(req.url)]))
        router.get(url__regex=r"https://api\.gog\.com/products/\d+$").mock(
            return_value=httpx.Response(200, json={"languages": self.store_languages}))
        router.get("https://embed.gog.com/user/data/games").mock(
            return_value=httpx.Response(200, json={"owned": [100, 200]}))

    def _chunk(self, req):
        h = req.url.path.rsplit("/", 1)[-1]
        self.chunk_hits.append(h)
        self.on_hit(len(self.chunk_hits))
        if self.corrupt_next:
            self.corrupt_next -= 1
            return httpx.Response(200, content=b"basura")
        return httpx.Response(200, content=self.blobs[h])


@pytest.fixture
def cdn(tmp_path, monkeypatch):
    monkeypatch.setenv("GOGL_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(cbuilds, "_repo_cache", {})
    c = FakeCDN()
    with respx.mock(assert_all_called=False) as router:
        c.mount(router)
        yield c


@pytest.fixture
def api():
    return GogApi(FakeAuth())


def simple_build(cdn, build_id="b1", exe=b"MZ-ejecutable", data=b"0123456789abcdef", extra_items=()):
    base = cdn.depot([cdn.file_item("bin\\Game.exe", exe, ["executable"]),
                      cdn.file_item("data\\big.pak", data),
                      {"type": "DepotDirectory", "path": "saves"},
                      {"type": "DepotLink", "path": "link.pak", "target": "data/big.pak"},
                      *extra_items])
    es = cdn.depot([cdn.file_item("lang\\es.txt", b"hola")])
    fr = cdn.depot([cdn.file_item("lang\\fr.txt", b"salut")])
    dlc = cdn.depot([cdn.file_item("dlc\\extra.pak", b"DLC!")])
    support = cdn.depot([cdn.file_item("redist\\setup.exe", b"S", ["support"])])
    cdn.build("100", build_id, [
        {"manifest": base, "productId": "100", "languages": ["*"], "size": 30, "compressedSize": 30},
        {"manifest": es, "productId": "100", "languages": ["es-ES"], "size": 4, "compressedSize": 4},
        {"manifest": fr, "productId": "100", "languages": ["fr-FR"], "size": 5, "compressedSize": 5},
        {"manifest": dlc, "productId": "200", "languages": ["*"], "size": 4, "compressedSize": 4},
        {"manifest": support, "productId": "100", "languages": ["*"], "size": 1, "compressedSize": 1},
    ], products=[{"productId": "100", "name": "Juego de Prueba"}, {"productId": "200", "name": "DLC"}])


def test_install_v2(cdn, api, tmp_path):
    simple_build(cdn)
    dest = tmp_path / "games/juego"
    g = install_game(api, "100", language="es-ES", install_path=dest)
    assert (dest / "bin/Game.exe").read_bytes() == b"MZ-ejecutable"
    assert os.access(dest / "bin/Game.exe", os.X_OK)
    assert (dest / "data/big.pak").read_bytes() == b"0123456789abcdef"
    assert (dest / "lang/es.txt").read_bytes() == b"hola"
    assert not (dest / "lang/fr.txt").exists()
    assert (dest / "dlc/extra.pak").read_bytes() == b"DLC!"  # DLC poseído: incluido
    assert (dest / "gog-support/100/redist/setup.exe").exists()
    assert (dest / "saves").is_dir()
    assert os.readlink(dest / "link.pak") == "data/big.pak"
    assert not list(dest.rglob("*.gogl-part"))
    assert g.client_id == "cid" and g.client_secret == "csecret" and g.platform == "windows"
    assert State().game("100").extra["managed"] is True
    assert load_manifest("100")["plan"]["build_id"] == "b1"


def test_build_info_languages_and_estimate(cdn, api):
    simple_build(cdn)
    info = cbuilds.build_info(api, "100")
    assert info.languages == ["es-ES", "fr-FR"]
    assert info.dlcs == [("200", "DLC")]
    assert info.default_language("de-DE") == "es-ES" or info.default_language("de-DE") == "en-US" \
        or info.default_language("de-DE") in info.languages
    assert info.estimate("es-ES", []) == (35, 35)
    assert info.estimate("es-ES", ["200"]) == (39, 39)


def test_update_downloads_only_changes(cdn, api, tmp_path):
    simple_build(cdn)
    dest = tmp_path / "g"
    install_game(api, "100", language="es-ES", install_path=dest)
    # build nueva: cambia un trozo de big.pak y desaparece Game.exe
    base = cdn.depot([cdn.file_item("data\\big.pak", b"0123XXXX89abcdef")])
    cdn.build("100", "b2", [{"manifest": base, "productId": "100", "languages": ["*"]}], version="2.0")
    cdn.chunk_hits.clear()
    update_game(api, "100")
    assert (dest / "data/big.pak").read_bytes() == b"0123XXXX89abcdef"
    assert not (dest / "bin/Game.exe").exists()
    assert len(cdn.chunk_hits) == 1  # solo el trozo nuevo
    assert load_manifest("100")["plan"]["version"] == "2.0"


def test_corrupt_chunk_is_retried(cdn, api, tmp_path):
    simple_build(cdn)
    cdn.corrupt_next = 2
    install_game(api, "100", language="es-ES", install_path=tmp_path / "g")
    assert (tmp_path / "g/data/big.pak").read_bytes() == b"0123456789abcdef"


def test_persistently_corrupt_chunk_fails_without_partial_file(cdn, api, tmp_path):
    simple_build(cdn)
    cdn.corrupt_next = 10_000
    with pytest.raises(ContentError):
        install_game(api, "100", language="es-ES", install_path=tmp_path / "g")
    assert not (tmp_path / "g/data/big.pak").exists()


def test_cancel_and_resume(cdn, api, tmp_path):
    simple_build(cdn, data=bytes(range(64)))
    cancel = threading.Event()
    cdn.on_hit = lambda n: cancel.set() if n >= 5 else None
    with pytest.raises(Cancelled):
        install_game(api, "100", language="es-ES", install_path=tmp_path / "g", cancel=cancel)
    cdn.on_hit = lambda n: None
    got_before = set(cdn.chunk_hits)
    cdn.chunk_hits.clear()
    install_game(api, "100", language="es-ES", install_path=tmp_path / "g")
    assert (tmp_path / "g/data/big.pak").read_bytes() == bytes(range(64))
    # Los trozos que ya estaban bien en los .gogl-part no se vuelven a pedir.
    assert got_before and not (got_before & set(cdn.chunk_hits))


def test_verify_and_repair(cdn, api, tmp_path):
    simple_build(cdn)
    dest = tmp_path / "g"
    install_game(api, "100", language="es-ES", install_path=dest)
    inst = Installer(api)
    assert inst.verify("100") == []
    (dest / "data/big.pak").write_bytes(b"0123456789abcdeX")
    (dest / "lang/es.txt").unlink()
    assert sorted(inst.verify("100")) == ["data/big.pak", "lang/es.txt"]
    Installer(api).repair("100")
    assert (dest / "data/big.pak").read_bytes() == b"0123456789abcdef"
    assert Installer(api).verify("100") == []


def test_small_files_container(cdn, api, tmp_path):
    a, b = b"pequeno-A", b"pequeno-B!"
    container = a + b
    comp = zlib.compress(container)
    cdn.blobs[md5(comp)] = comp
    sfc = {"chunks": [{"md5": md5(container), "size": len(container), "compressedMd5": md5(comp),
                       "compressedSize": len(comp)}]}
    items = [{"type": "DepotFile", "path": "cfg\\a.ini", "md5": md5(a), "chunks": [],
              "sfcRef": {"offset": 0, "size": len(a)}},
             {"type": "DepotFile", "path": "cfg\\b.ini", "md5": md5(b), "chunks": [],
              "sfcRef": {"offset": len(a), "size": len(b)}}]
    h = cdn.depot(items, {"smallFilesContainer": sfc})
    cdn.build("100", "b1", [{"manifest": h, "productId": "100", "languages": ["*"]}])
    install_game(api, "100", install_path=tmp_path / "g")
    assert (tmp_path / "g/cfg/a.ini").read_bytes() == a
    assert (tmp_path / "g/cfg/b.ini").read_bytes() == b
    assert not (tmp_path / "g/.gogl-sfc").exists()


def test_v1_install(cdn, api, tmp_path):
    blob = b"AAAA" + b"hello world" + b"BBBB"
    repo = {"product": {"timestamp": 123, "installDirectory": "Viejo", "projectName": "Viejo",
                        "gameIDs": [{"gameID": "100", "name": {"en": "Viejo"}}],
                        "depots": [{"languages": ["Neutral"], "manifest": "m.json", "gameIDs": ["100"],
                                    "size": "11"},
                                   {"redist": "MSVC2005"}]}}
    man = {"depot": {"files": [
        {"path": "/data/hello.txt", "size": 11, "offset": 4, "hash": md5(b"hello world"),
         "url": "100/main.bin"},
        {"path": "/data/sub", "directory": True}]}}
    cdn.metas["https://cdn.test/v1/repository.json"] = json.dumps(repo).encode()
    cdn.metas["https://cdn.test/v1/m.json"] = json.dumps(man).encode()
    cdn.builds["100"] = [{"build_id": "old", "generation": 1, "link": "https://cdn.test/v1/repository.json"}]

    def ranged(req):
        start, end = map(int, req.headers["Range"].split("=")[1].split("-"))
        assert "/main.bin" in str(req.url)
        return httpx.Response(206, content=blob[start:end + 1])

    cdn.router.get(url__regex=r"https://chunks\.test/store/100/main\.bin.*").mock(side_effect=ranged)
    install_game(api, "100", install_path=tmp_path / "g")
    assert (tmp_path / "g/data/hello.txt").read_bytes() == b"hello world"
    assert (tmp_path / "g/data/sub").is_dir()


def _mojo_installer(files: dict[str, tuple[bytes, int]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, (data, mode) in files.items():
            zi = zipfile.ZipInfo(name)
            zi.external_attr = (0o100000 | mode) << 16
            z.writestr(zi, data)
    return b"#!/bin/sh\n# makeself + mojosetup\nexit 0\n" + b"\x00" * 1000 + buf.getvalue()


def test_linux_installer(cdn, api, tmp_path):
    sh = _mojo_installer({"data/noarch/start.sh": (b"#!/bin/sh\n", 0o755),
                          "data/noarch/game/Game.x86_64": (b"ELF", 0o755),
                          "data/noarch/game/data.pak": (b"datos", 0o644),
                          "scripts/mojosetup_init.lua": (b"--", 0o644)})
    cdn.router.get("https://api.gog.com/products/300").mock(return_value=httpx.Response(200, json={
        "title": "Juego Linux", "downloads": {"installers": [
            {"os": "linux", "language": "en", "version": "1.2", "files": [
                {"id": "en3installer0", "size": len(sh), "downlink": "https://api.gog.com/dl/300"}]}]}}))
    cdn.router.get("https://api.gog.com/dl/300").mock(return_value=httpx.Response(
        200, json={"downlink": "https://files.test/game.sh", "checksum": "https://files.test/game.xml"}))
    cdn.router.get("https://files.test/game.sh").mock(return_value=httpx.Response(200, content=sh))
    cdn.router.get("https://files.test/game.xml").mock(return_value=httpx.Response(
        200, text=f'<file name="game.sh" md5="{md5(sh)}" />'))
    g = install_game(api, "300", platform="linux", install_path=tmp_path / "lin")
    assert g.platform == "linux" and g.prefix is None
    assert os.access(tmp_path / "lin/start.sh", os.X_OK)
    assert (tmp_path / "lin/game/data.pak").read_bytes() == b"datos"
    assert not (tmp_path / "lin/scripts").exists()


def test_linux_installer_rejects_path_traversal(cdn, api, tmp_path):
    sh = _mojo_installer({"data/noarch/../../../evil": (b"x", 0o644)})
    cdn.router.get("https://api.gog.com/products/301").mock(return_value=httpx.Response(200, json={
        "title": "Malo", "downloads": {"installers": [{"os": "linux", "language": "en", "files": [
            {"id": "x", "size": len(sh), "downlink": "https://api.gog.com/dl/301"}]}]}}))
    cdn.router.get("https://api.gog.com/dl/301").mock(return_value=httpx.Response(
        200, json={"downlink": "https://files.test/evil.sh"}))
    cdn.router.get("https://files.test/evil.sh").mock(return_value=httpx.Response(200, content=sh))
    with pytest.raises(ContentError):
        install_game(api, "301", platform="linux", install_path=tmp_path / "lin")
    assert not (tmp_path / "evil").exists()


def test_single_depot_game_installs_in_spanish(cdn, api, tmp_path):
    """Como Broken Sword 2: un solo depot «en-US» con todos los idiomas dentro."""
    from gogl import languages
    from gogl.runner import game as rgame
    base = cdn.depot([cdn.file_item("Game.exe", b"MZ"), cdn.file_item("Text\\Text_spanish.clu", b"ES")])
    gog = cdn.depot([cdn.file_item("goggame-100.info", json.dumps(
        {"gameId": "100", "language": "English", "playTasks": [{"isPrimary": True, "path": "Game.exe"}]}).encode())])
    cdn.build("100", "b1", [{"manifest": base, "productId": "100", "languages": ["en-US"]},
                            {"manifest": gog, "productId": "100", "languages": ["en-US"]}])
    info = cbuilds.build_info(api, "100")
    assert info.languages == ["en-US"]
    assert "es-ES" in info.all_languages and info.default_language("es-ES") == "es-ES"
    assert info.estimate("es-ES", []) == info.estimate("en-US", [])  # mismo depot
    g = install_game(api, "100", install_path=tmp_path / "g")  # idioma por defecto: español
    assert g.extra["language"] == "es-ES"
    assert load_manifest("100")["plan"]["depot_language"] == "en-US"
    assert json.loads((tmp_path / "g/goggame-100.info").read_text())["language"] == "Spanish"
    env = rgame.proton_env(g, tmp_path / "compat")
    if languages.locale_available("es_ES.UTF-8"):
        assert env["HOST_LC_ALL"] == "es_ES.UTF-8" and env["LANG"] == "es_ES.UTF-8"


def test_language_codes():
    from gogl import languages
    assert languages.from_store({"es": "", "br": "", "cn": "", "jp": ""}) == ["es-ES", "ja-JP", "pt-BR", "zh-Hans"]
    assert languages.unix_locale("zh-Hans") == "zh_CN.UTF-8"
    assert languages.unix_locale("pt-BR") == "pt_BR.UTF-8"


def test_apply_language_to_prefix_registry(tmp_path):
    from gogl import languages
    reg = tmp_path / "system.reg"
    reg.write_text('WINE REGISTRY Version 2\n\n[System\\\\ControlSet001\\\\Control\\\\Nls\\\\Language] 1\n'
                   '#time=1\n"Default"="0409"\n"InstallLanguage"="0409"\n\n[Other] 1\n"Default"="x"\n')
    assert languages.apply_to_prefix(tmp_path, "es-ES")
    text = reg.read_text()
    assert '"Default"="0c0a"' in text and '"InstallLanguage"="0c0a"' in text
    assert '"Default"="x"' in text  # otras secciones intactas
    assert not languages.apply_to_prefix(tmp_path, "es-ES")  # idempotente


def test_dependencies_go_to_game_dir_or_shared_redist(cdn, api, tmp_path, monkeypatch):
    from gogl.content import redist_dir
    from gogl.runner import setup
    cdn.dependency("DOSBox074_2CS", [cdn.file_item("DOSBOX\\DOSBox.exe", b"dosbox")], internal=True)
    cdn.dependency("MSVC2017", [cdn.file_item("__redist\\MSVC2017\\VC_redist.x86.exe", b"vc2017")],
                   exe="__redist\\MSVC2017\\VC_redist.x86.exe", args="/install /quiet /norestart")
    cdn.dependency("ISI", [cdn.file_item("__redist\\ISI\\scriptinterpreter.exe", b"isi")],
                   exe="__redist\\ISI\\scriptinterpreter.exe", internal=True)
    base = cdn.depot([cdn.file_item("Game.exe", b"MZ"), cdn.file_item("goggame-100.script", b"{}")])
    cdn.build("100", "b1", [{"manifest": base, "productId": "100", "languages": ["*"], "size": 2}],
              dependencies=["DOSBox074_2CS", "MSVC2017", "dotNet4"], scriptInterpreter=True)
    dest = tmp_path / "g"
    g = install_game(api, "100", language="es-ES", install_path=dest)

    assert (dest / "DOSBOX/DOSBox.exe").read_bytes() == b"dosbox"  # la usa el juego al arrancar
    assert (redist_dir() / "__redist/MSVC2017/VC_redist.x86.exe").read_bytes() == b"vc2017"
    assert not (dest / "__redist").exists()
    plan = load_manifest("100")["plan"]
    assert plan["dependencies"] == ["DOSBox074_2CS", "MSVC2017", "ISI"]  # .NET 4: Wine Mono
    assert [r["id"] for r in plan["redist"]] == ["MSVC2017"]              # ISI no se «instala»

    g.prefix = tmp_path / "pfx"
    steps = setup.pending_steps(g)
    assert [s.key for s in steps] == ["redist:MSVC2017", "scripts"]
    assert steps[0].args[1:] == ["/install", "/quiet", "/norestart"]
    isi = steps[1].args
    assert isi[0].endswith("__redist/ISI/scriptinterpreter.exe") and "/ProductId=100" in isi
    assert f"/DIR=Z:{str(dest).replace('/', chr(92))}" in isi and "/Language=Spanish" in isi

    class FakeProton:
        script = tmp_path / "proton"
        wineserver = None

    calls = []
    monkeypatch.setattr(setup.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or setup.subprocess.CompletedProcess(argv, 3010))
    assert setup.run_setup(g, FakeProton(), steps) == []
    assert [c[1] for c in calls] == ["run", "run"]
    assert setup.pending_steps(g) == []  # hecho: no se repite en el siguiente arranque

    # Otro juego que pide lo mismo no vuelve a descargarlo.
    cdn.chunk_hits.clear()
    cdn.build("100", "b2", [{"manifest": base, "productId": "100", "languages": ["*"], "size": 2}],
              dependencies=["MSVC2017"])
    update_game(api, "100")
    assert md5(zlib.compress(b"vc2017")) not in cdn.chunk_hits


def test_v1_support_commands_and_redist(cdn, api, tmp_path):
    repo = {"product": {"timestamp": 1, "installDirectory": "V", "projectName": "V",
                        "gameIDs": [{"gameID": "100", "name": {"en": "V"}}],
                        "support_commands": [{"languages": ["Neutral"], "executable": "/galaxy_v.exe",
                                              "gameID": "100", "argument": "", "systems": ["Windows"]}],
                        "depots": [{"languages": ["Neutral"], "manifest": "m.json", "gameIDs": ["100"]},
                                   {"redist": "MSVC2005", "executable": "__redist/MSVC2005/vcredist_x86.exe",
                                    "argument": "/q"}]}}
    cdn.metas["https://cdn.test/v1/r.json"] = json.dumps(repo).encode()
    cdn.metas["https://cdn.test/v1/m.json"] = json.dumps({"depot": {"files": []}}).encode()
    cdn.builds["100"] = [{"build_id": "v1b", "generation": 1, "link": "https://cdn.test/v1/r.json"}]
    info = cbuilds.build_info(api, "100")
    plan = cbuilds.make_plan(info, "en-US", [])
    assert plan.support_commands == [{"product_id": "100", "executable": "galaxy_v.exe", "arguments": ""}]
    assert [f.path for f in plan.redist_files] == ["__redist/MSVC2005/vcredist_x86.exe"]
    assert plan.redist[0]["arguments"] == "/q"


def test_old_manifest_without_dependencies_offers_update(cdn, api, tmp_path):
    from gogl.content import check_update
    from gogl.content.installer import save_manifest
    simple_build(cdn)
    install_game(api, "100", install_path=tmp_path / "g")
    assert check_update(api, "100") is None
    m = load_manifest("100")
    m["plan"].pop("dependencies")
    save_manifest("100", m)
    assert check_update(api, "100") == ("1.0", "1.0")


def test_linux_multiple_files_are_alternative_versions(cdn, api, tmp_path):
    old = _mojo_installer({"data/noarch/start.sh": (b"#!/bin/sh\n# v3\n", 0o755)})
    new = _mojo_installer({"data/noarch/start.sh": (b"#!/bin/sh\n# v4\n", 0o755)})
    cdn.router.get("https://api.gog.com/products/302").mock(return_value=httpx.Response(200, json={
        "title": "Dungeon", "downloads": {"installers": [{"os": "linux", "language": "en", "files": [
            {"id": "en3installer0", "size": len(old), "downlink": "https://api.gog.com/dl/302/0"},
            {"id": "en3installer1", "size": len(new), "downlink": "https://api.gog.com/dl/302/1"}]}]}}))
    for i, ver in ((0, "2.1.0.3"), (1, "2.1.0.4")):
        cdn.router.get(f"https://api.gog.com/dl/302/{i}").mock(return_value=httpx.Response(
            200, json={"downlink": f"https://files.test/gog_dungeon_{ver}.sh?token=x"}))
    cdn.router.get(url__regex=r"https://files\.test/gog_dungeon_2\.1\.0\.3\.sh.*").mock(
        return_value=httpx.Response(200, content=old))
    cdn.router.get(url__regex=r"https://files\.test/gog_dungeon_2\.1\.0\.4\.sh.*").mock(
        return_value=httpx.Response(200, content=new))
    install_game(api, "302", platform="linux", install_path=tmp_path / "d")
    assert (tmp_path / "d/start.sh").read_bytes().endswith(b"# v4\n")


def test_uninstall_keeps_foreign_files_in_existing_folder(cdn, api, tmp_path):
    """Instalar directamente en una carpeta con más cosas (p. ej. ~/Games) y
    desinstalar no puede llevarse por delante lo que no es del juego."""
    from gogl.content import uninstall
    simple_build(cdn)
    games = tmp_path / "Games"
    (games / "OtroJuego").mkdir(parents=True)
    (games / "OtroJuego/partida.sav").write_text("no me borres")
    g = install_game(api, "100", language="es-ES", install_path=games)
    uninstall(State(), g.game_id)
    assert (games / "OtroJuego/partida.sav").read_text() == "no me borres"
    assert not (games / "bin/Game.exe").exists() and not (games / "bin").exists()


def test_uninstall_removes_folder_created_by_gogl(cdn, api, tmp_path):
    from gogl.content import uninstall
    simple_build(cdn)
    dest = tmp_path / "Games/juego"
    g = install_game(api, "100", language="es-ES", install_path=dest)
    (dest / "config.ini").write_text("creado por el juego")
    uninstall(State(), g.game_id)
    assert not dest.exists()


@pytest.mark.parametrize("item", [
    {"type": "DepotDirectory", "path": "..\\..\\fuera"},
    {"type": "DepotLink", "path": "enlace", "target": "../../fuera"},
    {"type": "DepotLink", "path": "..\\enlace", "target": "x"},
])
def test_manifest_paths_cannot_escape(cdn, api, tmp_path, item):
    simple_build(cdn, extra_items=[item])
    with pytest.raises(ContentError):
        install_game(api, "100", install_path=tmp_path / "g/juego")


def test_manifest_file_cannot_escape(cdn, api, tmp_path):
    simple_build(cdn, extra_items=[cdn.file_item("..\\..\\evil.txt", b"x")])
    with pytest.raises(ContentError):
        install_game(api, "100", install_path=tmp_path / "g/juego")
    assert not (tmp_path / "evil.txt").exists()


def test_default_install_dir_name_is_sanitized(monkeypatch, tmp_path):
    from gogl.content import default_install_path
    monkeypatch.setenv("GOGL_HOME", str(tmp_path / "home"))
    assert default_install_path("../..").parent == default_install_path("x").parent


def test_v1_rejects_server_ignoring_range(cdn, api, tmp_path):
    blob = b"AAAA" + b"hello world" + b"BBBB"
    repo = {"product": {"timestamp": 1, "installDirectory": "V", "projectName": "V",
                        "gameIDs": [{"gameID": "100", "name": {"en": "V"}}],
                        "depots": [{"languages": ["Neutral"], "manifest": "m.json", "gameIDs": ["100"]}]}}
    man = {"depot": {"files": [{"path": "/a.txt", "size": 11, "offset": 4, "hash": md5(b"hello world"),
                                "url": "100/main.bin"}]}}
    cdn.metas["https://cdn.test/v1/repository.json"] = json.dumps(repo).encode()
    cdn.metas["https://cdn.test/v1/m.json"] = json.dumps(man).encode()
    cdn.builds["100"] = [{"build_id": "o", "generation": 1, "link": "https://cdn.test/v1/repository.json"}]
    cdn.router.get(url__regex=r"https://chunks\.test/store/100/main\.bin.*").mock(
        return_value=httpx.Response(200, content=blob))
    with pytest.raises(ContentError, match="rango"):
        install_game(api, "100", install_path=tmp_path / "g")


def _linux_game(cdn, sh, gid="302"):
    cdn.router.get(f"https://api.gog.com/products/{gid}").mock(return_value=httpx.Response(200, json={
        "title": "Con enlaces", "downloads": {"installers": [{"os": "linux", "language": "en", "files": [
            {"id": "x", "size": len(sh), "downlink": f"https://api.gog.com/dl/{gid}"}]}]}}))
    cdn.router.get(f"https://api.gog.com/dl/{gid}").mock(return_value=httpx.Response(
        200, json={"downlink": f"https://files.test/{gid}.sh"}))
    cdn.router.get(f"https://files.test/{gid}.sh").mock(return_value=httpx.Response(200, content=sh))


def test_linux_reinstall_keeps_symlinked_libraries(cdn, api, tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("data/noarch/lib/libfoo.so.1", b"REAL")
        zi = zipfile.ZipInfo("data/noarch/lib/libfoo.so")
        zi.external_attr = (0o120777) << 16
        z.writestr(zi, "libfoo.so.1")
    sh = b"#!/bin/sh\nexit 0\n" + buf.getvalue()
    _linux_game(cdn, sh)
    for _ in range(2):
        install_game(api, "302", platform="linux", install_path=tmp_path / "lin")
    lib = tmp_path / "lin/lib"
    assert (lib / "libfoo.so.1").read_bytes() == b"REAL" and not (lib / "libfoo.so.1").is_symlink()
    assert os.readlink(lib / "libfoo.so") == "libfoo.so.1"


def test_linux_symlinked_dir_cannot_escape(cdn, api, tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        zi = zipfile.ZipInfo("data/noarch/d")
        zi.external_attr = (0o120777) << 16
        z.writestr(zi, str(tmp_path))
        z.writestr("data/noarch/d/evil", b"x")
    sh = b"#!/bin/sh\nexit 0\n" + buf.getvalue()
    _linux_game(cdn, sh, "303")
    with pytest.raises(ContentError):
        install_game(api, "303", platform="linux", install_path=tmp_path / "lin")
    assert not (tmp_path / "evil").exists()


def test_linux_stale_bigger_download_is_discarded(cdn, api, tmp_path):
    sh = _mojo_installer({"data/noarch/start.sh": (b"#!/bin/sh\n", 0o755)})
    _linux_game(cdn, sh, "304")
    from gogl import paths
    stale = paths.data_dir() / "downloads/304-x.sh"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"\0" * (len(sh) + 100))
    install_game(api, "304", platform="linux", install_path=tmp_path / "lin")
    assert (tmp_path / "lin/start.sh").exists()
