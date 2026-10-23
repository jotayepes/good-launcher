import json

import httpx
import respx

from gogl.auth import Credentials
from gogl.saves.account import AccountCloud

BASE = "https://cloudstorage.gog.com/v2/users/7/containers"


class FakeAuth:
    def __init__(self):
        self.calls = []

    def credentials(self, *a, force_refresh=False, **k):
        self.calls.append(force_refresh)
        return Credentials("new" if force_refresh else "old", "7", 9e18)


def _container(gids, size, sid):
    return {"games": [{"id": g, "quota": 100} for g in gids],
            "container": {"size": size, "files": 2, "space_id": sid, "quota": 100}}


@respx.mock
def test_containers_paginate_sort_and_title():
    def page(req):
        p = int(req.url.params["page"])
        items = [_container([1], 10, "a")] if p == 1 else [_container([2, 3], 95, "b")]
        return httpx.Response(200, json={"pagination": {"page": p, "limit": 50, "pages": 2}, "items": items})

    respx.get(url__regex=rf"{BASE}\?.*").mock(side_effect=page)
    respx.get(url__regex=r"https://api\.gog\.com/v2/games.*").mock(return_value=httpx.Response(200, json={
        "_embedded": {"items": [{"_embedded": {"product": {"id": 3, "title": "Tres"}}}]}}))
    cs = AccountCloud(FakeAuth()).containers({"1": "Uno", "2": "Dos"})
    assert [c.space_id for c in cs] == ["b", "a"]          # el más lleno primero
    assert cs[0].title == "Dos / Tres" and cs[0].usage == 0.95


@respx.mock
def test_refreshes_token_once_and_deletes():
    auth = FakeAuth()
    seen = []

    def handler(req):
        seen.append((req.method, req.headers["Authorization"], str(req.url), req.content))
        if req.headers["Authorization"] == "Bearer old":
            return httpx.Response(401)
        return httpx.Response(204)

    respx.route(url__startswith=BASE).mock(side_effect=handler)
    cloud = AccountCloud(auth)
    cloud.delete_files("s1", ["saves/a.sav", "saves/a.png"])
    assert auth.calls == [False, True]
    assert seen[-1][0] == "POST" and json.loads(seen[-1][3]) == {"delete_files": ["saves/a.sav", "saves/a.png"]}
    cloud.delete_all("s1")
    assert seen[-1][0] == "DELETE" and seen[-1][2].endswith("/s1?operation=all")


@respx.mock
def test_saves_and_auto_clean_preview():
    respx.get(url__regex=rf"{BASE}/s1\?.*").mock(return_value=httpx.Response(200, json={
        "pagination": {"pages": 1}, "items": [
            {"name": "old", "size": 5, "type": "savegame", "timestamp": "2024-01-01T00:00:00Z", "files": ["s/old"]},
            {"name": "non-game-related", "size": 1, "type": "nogame", "timestamp": "2025-01-01T00:00:00Z",
             "files": ["s/x.json"]},
            {"name": "new", "size": 5, "type": "savegame", "timestamp": "2026-01-01T00:00:00Z", "files": ["s/new"]}]}))
    respx.get(f"{BASE}/s1/auto-clean").mock(return_value=httpx.Response(409))
    cloud = AccountCloud(FakeAuth())
    assert [s.name for s in cloud.saves("s1")] == ["new", "old", "non-game-related"]
    assert cloud.auto_clean_preview("s1") == []  # 409: GOG no propone nada
