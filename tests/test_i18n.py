import re
import subprocess
import sys
from pathlib import Path

import pytest

from gogl import i18n, languages

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER = re.compile(r"\{(\w*)\}")


@pytest.fixture
def english():
    i18n.set_language("en")
    yield
    i18n.set_language("es")


def test_catalogs_up_to_date_and_complete():
    r = subprocess.run([sys.executable, str(ROOT / "packaging/update_po.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("code", [c for c in i18n.available() if c != i18n.SOURCE])
def test_translations_keep_placeholders(code):
    for src, tr in i18n.load_po(i18n.LOCALE_DIR / f"{code}.po").items():
        forms = tr if isinstance(tr, list) else [tr]
        for f in forms:
            # En plural, una forma puede omitir {n} («one file»); el resto debe estar.
            assert set(PLACEHOLDER.findall(f)) - {"n"} == set(PLACEHOLDER.findall(src)) - {"n"}, (src, f)
            f.format(**{k: "x" for k in PLACEHOLDER.findall(src) + ["n"]})


def test_translate_and_plural(english):
    assert i18n._("Jugar") == "Play"
    assert i18n._("texto que no está en el catálogo") == "texto que no está en el catálogo"
    assert i18n.n_("{n} archivo", "{n} archivos", 1).format(n=1) == "1 file"
    assert i18n.n_("{n} archivo", "{n} archivos", 3).format(n=3) == "3 files"


def test_source_language_is_identity():
    i18n.set_language("es")
    assert i18n._("Jugar") == "Jugar"
    assert i18n.n_("{n} archivo", "{n} archivos", 2) == "{n} archivos"


def test_load_po_multiline_plural_and_fuzzy(tmp_path):
    po = tmp_path / "xx.po"
    po.write_text('msgid ""\nmsgstr ""\n"X-Language-Name: Test\\n"\n\n'
                  '#: a.py\nmsgid ""\n"línea uno\\n"\n"línea \\"dos\\""\nmsgstr "one\\ntwo"\n\n'
                  'msgid "{n} gato"\nmsgid_plural "{n} gatos"\nmsgstr[0] "{n} cat"\nmsgstr[1] "{n} cats"\n\n'
                  '#, fuzzy\nmsgid "dudoso"\nmsgstr "doubtful"\n\n'
                  'msgid "vacío"\nmsgstr ""\n', encoding="utf-8")
    cat = i18n.load_po(po)
    assert cat == {'línea uno\nlínea "dos"': "one\ntwo", "{n} gato": ["{n} cat", "{n} cats"]}
    assert "X-Language-Name: Test" in i18n.load_po(po, header=True)[""]


@pytest.mark.parametrize("env,expected", [
    ({"LANG": "es_ES.UTF-8"}, "es"),
    ({"LANG": "en_US.UTF-8"}, "en"),
    ({"LANG": "de_DE.UTF-8"}, "en"),                      # sin traducción: inglés
    ({"LANG": "C"}, "en"),
    ({}, "en"),
    ({"LANG": "en_US.UTF-8", "LC_MESSAGES": "es_MX.UTF-8"}, "es"),
    ({"LANG": "es_ES.UTF-8", "LC_ALL": "en_GB.UTF-8"}, "en"),
    ({"LANG": "de_DE.UTF-8", "LANGUAGE": "de:es:en"}, "es"),  # primera de la lista con traducción
])
def test_system_language(env, expected):
    assert i18n.system_language(env) == expected


def test_resolve_prefers_forced_then_setting():
    assert i18n.resolve(None, {"LANG": "es_ES.UTF-8"}) == "es"
    assert i18n.resolve("en", {"LANG": "es_ES.UTF-8"}) == "en"
    assert i18n.resolve("en", {"LANG": "es_ES.UTF-8", "GOGL_LANG": "es"}) == "es"
    assert i18n.resolve("xx", {"LANG": "es_ES.UTF-8"}) == "es"  # ajuste desconocido: el del sistema


def test_native_names():
    assert i18n.native_name("es") == "Español"
    assert i18n.native_name("en") == "English"


@pytest.mark.parametrize("lang,expected", [
    ("es_ES.UTF-8", "es-ES"), ("es_AR.UTF-8", "es-MX"), ("pt_BR.UTF-8", "pt-BR"), ("pt_PT", "pt-PT"),
    ("zh_CN.UTF-8", "zh-Hans"), ("zh_TW.UTF-8", "zh-Hant"), ("de_AT.UTF-8", "de-DE"),
    ("fr_FR.UTF-8", "fr-FR"), ("C", "en-US"), ("", "en-US"),
])
def test_game_language_from_system(lang, expected):
    assert languages.system_default({"LANG": lang}) == expected


def test_no_shadowed_underscore():
    sys.path.insert(0, str(ROOT / "packaging"))
    try:
        import update_po
    finally:
        sys.path.pop(0)
    _found, errors = update_po.extract()
    assert errors == []
