import os
import shutil

import pytest

from gogl.saves.engine import Action, ConflictError, SyncError, plan_sync, scan_local


def sync(env, **kw):
    return env.syncer().run(**kw)


def test_first_upload_then_noop(env):
    env.write("slot1.sav", b"A")
    r = sync(env)
    assert r.uploaded == ["saves/slot1.sav"]
    assert env.cloud.content("saves/slot1.sav") == b"A"
    r = sync(env)
    assert r.plan.changes == []


def test_download_into_missing_dirs(env):
    env.cloud.put_raw("saves/profile/slot1.sav", b"cloud")
    r = sync(env)
    assert r.downloaded == ["saves/profile/slot1.sav"]
    assert (env.saves / "profile/slot1.sav").read_bytes() == b"cloud"
    assert sync(env).plan.changes == []


def test_local_change_uploads_even_if_mtime_is_older(env):
    p = env.write("slot1.sav", b"A")
    sync(env)
    p.write_bytes(b"B")
    os.utime(p, (1, 1))  # reloj desfasado: mtime en 1970
    r = sync(env)
    assert r.uploaded == ["saves/slot1.sav"]
    assert env.cloud.content("saves/slot1.sav") == b"B"


def test_touch_without_change_does_nothing(env):
    p = env.write("slot1.sav", b"A")
    sync(env)
    os.utime(p, (2_000_000_000, 2_000_000_000))
    assert sync(env).plan.changes == []


def test_cloud_change_downloads(env):
    env.write("slot1.sav", b"A")
    sync(env)
    env.cloud.put_raw("saves/slot1.sav", b"from-windows", galaxy_style=True)
    r = sync(env)
    assert r.downloaded == ["saves/slot1.sav"]
    assert (env.saves / "slot1.sav").read_bytes() == b"from-windows"
    assert env.backups  # copia antes de sobrescribir


def test_both_changed_is_conflict_and_nothing_applied(env):
    p = env.write("slot1.sav", b"A")
    env.write("other.sav", b"X")
    sync(env)
    p.write_bytes(b"local")
    env.write("new_local.sav", b"N")
    env.cloud.put_raw("saves/slot1.sav", b"cloud")
    calls = len(env.cloud.calls)
    with pytest.raises(ConflictError) as e:
        sync(env)
    assert [i.key for i in e.value.plan.conflicts] == ["saves/slot1.sav"]
    assert env.cloud.calls[calls:] == []  # no se subió ni el archivo nuevo
    assert p.read_bytes() == b"local"


@pytest.mark.parametrize("prefer,expected", [("local", b"local"), ("cloud", b"cloud")])
def test_conflict_resolution(env, prefer, expected):
    p = env.write("slot1.sav", b"A")
    sync(env)
    p.write_bytes(b"local")
    env.cloud.put_raw("saves/slot1.sav", b"cloud")
    sync(env, prefer=prefer)
    assert p.read_bytes() == expected
    assert env.cloud.content("saves/slot1.sav") == expected
    assert sync(env).plan.changes == []


def test_no_base_same_content_different_compression_is_adopted(env):
    env.write("slot1.sav", b"same")
    env.cloud.put_raw("saves/slot1.sav", b"same", galaxy_style=True)
    r = sync(env)
    assert r.plan.conflicts == [] and r.uploaded == [] and r.downloaded == []
    assert sync(env).plan.changes == []


def test_no_base_different_content_is_conflict(env):
    env.write("slot1.sav", b"mine")
    env.cloud.put_raw("saves/slot1.sav", b"theirs")
    with pytest.raises(ConflictError):
        sync(env)


def test_local_delete_propagates_with_base(env):
    env.write("a.sav", b"A")
    p = env.write("b.sav", b"B")
    sync(env)
    p.unlink()
    r = sync(env)
    assert r.deleted_cloud == ["saves/b.sav"]
    assert "saves/b.sav" not in env.cloud.objects


def test_cloud_delete_propagates_with_base(env):
    env.write("a.sav", b"A")
    env.write("b.sav", b"B")
    sync(env)
    env.cloud.delete("saves/b.sav")
    r = sync(env)
    assert r.deleted_local == ["saves/b.sav"]
    assert not (env.saves / "b.sav").exists()


def test_empty_prefix_restores_instead_of_wiping_cloud(env):
    """Prefijo recreado (carpeta de partidas vacía): nunca debe vaciar la nube."""
    env.write("a.sav", b"A")
    env.write("b.sav", b"B")
    sync(env)
    shutil.rmtree(env.saves)
    r = sync(env)
    assert r.deleted_cloud == []
    assert sorted(r.downloaded) == ["saves/a.sav", "saves/b.sav"]
    assert (env.saves / "a.sav").read_bytes() == b"A"


def test_empty_cloud_listing_does_not_wipe_local(env):
    env.write("a.sav", b"A")
    sync(env)
    env.cloud.objects.clear()
    r = sync(env)
    assert r.deleted_local == [] and r.uploaded == ["saves/a.sav"]
    assert (env.saves / "a.sav").exists()


def test_upload_lost_by_server_is_retried(env):
    env.write("a.sav", b"A")
    env.cloud.lose_uploads = 1
    r = sync(env)
    assert r.uploaded == ["saves/a.sav"]
    assert [c for c in env.cloud.calls if c[0] == "upload"] == [("upload", "saves/a.sav")] * 2


def test_upload_never_confirmed_fails_and_base_not_advanced(env):
    env.write("a.sav", b"A")
    env.cloud.lose_uploads = 99
    with pytest.raises(SyncError):
        sync(env)
    assert env.state.base("1") == {}
    env.cloud.lose_uploads = 0
    assert sync(env).uploaded == ["saves/a.sav"]


def test_killed_between_upload_and_verify_recovers(env):
    """Si nos matan tras el PUT y antes de verificar, el siguiente sync no
    ve un conflicto falso: reconoce que la nube ya tiene nuestro contenido."""
    p = env.write("a.sav", b"A")
    sync(env)
    p.write_bytes(b"B")
    env.cloud.upload("saves/a.sav", b"B", 0)  # subida hecha, base sin actualizar
    r = sync(env)
    assert r.plan.conflicts == [] and r.uploaded == []
    assert sync(env).plan.changes == []


def test_case_insensitive_match_with_cloud_keys(env):
    env.write("Profile/Slot1.SAV", b"A")
    env.cloud.put_raw("saves/profile/slot1.sav", b"A")
    r = sync(env)
    assert r.plan.conflicts == [] and r.uploaded == [] and r.downloaded == []
    env.cloud.put_raw("saves/profile/slot1.sav", b"B")
    sync(env)
    assert (env.saves / "Profile/Slot1.SAV").read_bytes() == b"B"
    assert not (env.saves / "profile").exists()


def test_location_prefix_is_exact(env):
    """«saves2/x» no pertenece a la ubicación «saves»."""
    env.cloud.put_raw("saves2/x.sav", b"X")
    plan = plan_sync(scan_local(env.locs), env.cloud.list(), {}, env.locs)
    assert plan.ignored_cloud == ["saves2/x.sav"]
    assert all(i.action == Action.NOOP for i in plan.items)


def test_dry_run_touches_nothing(env):
    env.write("a.sav", b"A")
    r = sync(env, dry_run=True)
    assert [i.action for i in r.plan.changes] == [Action.UPLOAD]
    assert env.cloud.objects == {} and env.state.base("1") == {}
