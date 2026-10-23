import pytest

from gogl.saves.locations import (DEFAULT_LOCATION, LocationError, parse_remote_config,
                                  resolve_template)


def cfg(locations, enabled=True):
    return {"content": {"Windows": {"cloudStorage": {"enabled": enabled, "quota": 1000,
                                                      "locations": locations}}}}


def test_parse_locations():
    c = parse_remote_config(cfg([{"name": "saves", "location": "<?INSTALL?>/saves"}]), "cid")
    assert c.enabled and c.locations == [("saves", "<?INSTALL?>/saves")]


def test_default_location_for_galaxy_sdk_games():
    c = parse_remote_config(cfg([]), "123")
    assert c.locations == [(DEFAULT_LOCATION,
                            "<?APPLICATION_DATA_LOCAL?>/GOG.com/Galaxy/Applications/123/Storage/Shared/Files")]


def test_disabled():
    assert not parse_remote_config(cfg([], enabled=False), "1").enabled
    assert not parse_remote_config({}, "1").enabled


def test_resolve_proton_paths(tmp_path):
    compat = tmp_path / "compat"
    user = compat / "pfx/drive_c/users/steamuser"
    (user / "Saved Games").mkdir(parents=True)
    install = tmp_path / "game"
    assert resolve_template("<?SAVED_GAMES?>\\Studio\\Game", install, compat) == user / "Saved Games/Studio/Game"
    assert resolve_template("<?APPLICATION_DATA_LOCAL_LOW?>/X", install, compat) == user / "AppData/LocalLow/X"
    assert resolve_template("<?INSTALL?>/saves/../Save", install, compat) == install / "Save"


def test_resolve_case_insensitive(tmp_path):
    user = tmp_path / "pfx/drive_c/users/steamuser"
    (user / "Documents/My Games/WITCHER").mkdir(parents=True)
    p = resolve_template("<?DOCUMENTS?>/my games/Witcher/gamesaves", tmp_path, tmp_path / "pfx")
    assert p == user / "Documents/My Games/WITCHER/gamesaves"


def test_unknown_variable(tmp_path):
    with pytest.raises(LocationError):
        resolve_template("<?FOO?>/x", tmp_path, tmp_path)


def test_native_requires_prefix_for_wine_vars(tmp_path):
    with pytest.raises(LocationError):
        resolve_template("<?DOCUMENTS?>/x", tmp_path, None)
