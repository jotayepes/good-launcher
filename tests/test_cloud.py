import gzip

import httpx
import pytest
import respx

from gogl.auth import Credentials
from gogl.saves.cloud import CloudError, CloudFile, CloudStorage, gzip_bytes, md5

BASE = "https://cloudstorage.gog.com/v1/42/cid"


def storage(tokens=("t1", "t2")):
    it = iter(tokens)
    return CloudStorage("cid", lambda force: Credentials(next(it), "42", 0), backoff=0)


@respx.mock
def test_list_skips_empty_marker_and_handles_404():
    respx.get(BASE).mock(return_value=httpx.Response(200, json=[
        {"name": "saves/a", "hash": "h1", "last_modified": "2024-01-01T00:00:00+00:00", "bytes": 3},
        {"name": "saves/b", "hash": "aadd86936a80ee8a369579c3926f1b3c", "last_modified": ""},
    ]))
    assert list(storage().list()) == ["saves/a"]
    respx.get(BASE).mock(return_value=httpx.Response(404))
    assert storage().list() == {}


@respx.mock
def test_upload_is_deterministic_gzip_with_headers():
    route = respx.put(f"{BASE}/saves/my%20slot.sav").mock(return_value=httpx.Response(201))
    h = storage().upload("saves/my slot.sav", b"data", 1_700_000_000)
    req = route.calls.last.request
    assert req.content == gzip_bytes(b"data") and h == md5(req.content)
    assert req.headers["Etag"] == h
    assert req.headers["Content-Encoding"] == "gzip"
    assert req.headers["X-Object-Meta-LocalLastModified"] == "2023-11-14T22:13:20+00:00"
    assert req.headers["Authorization"] == "Bearer t1"


@respx.mock
def test_download_verifies_hash_and_decompresses():
    body = gzip.compress(b"save", 9, mtime=5)
    respx.get(f"{BASE}/saves/a").mock(return_value=httpx.Response(
        200, content=body, headers={"Content-Encoding": "gzip",
                                    "X-Object-Meta-LocalLastModified": "2024-01-01T00:00:00+00:00"}))
    data, mtime = storage().download(CloudFile("saves/a", md5(body), ""))
    assert data == b"save" and mtime == 1704067200
    with pytest.raises(CloudError):
        storage().download(CloudFile("saves/a", "wrong", ""))


@respx.mock
def test_refreshes_token_on_401():
    route = respx.get(BASE).mock(side_effect=[httpx.Response(401), httpx.Response(200, json=[])])
    assert storage().list() == {}
    assert route.calls[1].request.headers["Authorization"] == "Bearer t2"


@respx.mock
def test_retries_on_503():
    route = respx.delete(f"{BASE}/saves/a").mock(side_effect=[httpx.Response(503), httpx.Response(204)])
    storage().delete("saves/a")
    assert route.call_count == 2
