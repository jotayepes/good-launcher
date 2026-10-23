"""Idiomas: códigos de la tienda de GOG, de los depots, de Unix y de Windows.

Muchos juegos de GOG publican un único depot etiquetado «en-US» que en
realidad trae todos los idiomas: el juego elige al arrancar según el idioma
de Windows. Bajo Proton eso sale del locale de Unix, y Proton solo respeta
LC_ALL si le llega como HOST_LC_ALL. Por eso el idioma elegido se aplica al
lanzar el juego, no solo al descargar."""

from __future__ import annotations

import functools
import json
import os
import subprocess
from pathlib import Path

from . import config
from .i18n import _

# Códigos de la tienda (api.gog.com/products → "languages") → códigos de depot.
STORE_TO_CODE = {
    "en": "en-US", "de": "de-DE", "fr": "fr-FR", "es": "es-ES", "es_mx": "es-MX", "it": "it-IT",
    "pl": "pl-PL", "ru": "ru-RU", "cn": "zh-Hans", "zh": "zh-Hans", "jp": "ja-JP", "ja": "ja-JP",
    "ko": "ko-KR", "pt": "pt-PT", "br": "pt-BR", "cz": "cs-CZ", "cs": "cs-CZ", "hu": "hu-HU",
    "tr": "tr-TR", "nl": "nl-NL", "uk": "uk-UA", "sv": "sv-SE", "da": "da-DK", "fi": "fi-FI",
    "no": "nb-NO", "ar": "ar-SA", "th": "th-TH", "ro": "ro-RO", "gk": "el-GR", "el": "el-GR",
}

NAMES = {
    "en-US": _("Inglés"), "es-ES": _("Español"), "es-MX": _("Español (Latinoamérica)"), "fr-FR": _("Francés"),
    "de-DE": _("Alemán"), "it-IT": _("Italiano"), "pt-BR": _("Portugués (Brasil)"), "pt-PT": _("Portugués"),
    "ru-RU": _("Ruso"), "pl-PL": _("Polaco"), "zh-Hans": _("Chino simplificado"),
    "zh-Hant": _("Chino tradicional"), "ja-JP": _("Japonés"), "ko-KR": _("Coreano"), "cs-CZ": _("Checo"),
    "hu-HU": _("Húngaro"), "tr-TR": _("Turco"), "nl-NL": _("Neerlandés"), "uk-UA": _("Ucraniano"),
    "sv-SE": _("Sueco"), "da-DK": _("Danés"), "fi-FI": _("Finés"), "nb-NO": _("Noruego"),
    "ar-SA": _("Árabe"), "th-TH": _("Tailandés"), "ro-RO": _("Rumano"), "el-GR": _("Griego"),
}

# Nombre en inglés que Galaxy escribe en goggame-<id>.info («language»).
GALAXY_NAMES = {
    "en-US": "English", "es-ES": "Spanish", "es-MX": "Latin American Spanish", "fr-FR": "French",
    "de-DE": "German", "it-IT": "Italian", "pt-BR": "Brazilian Portuguese", "pt-PT": "Portuguese",
    "ru-RU": "Russian", "pl-PL": "Polish", "zh-Hans": "Chinese", "zh-Hant": "Traditional Chinese",
    "ja-JP": "Japanese", "ko-KR": "Korean", "cs-CZ": "Czech", "hu-HU": "Hungarian", "tr-TR": "Turkish",
    "nl-NL": "Dutch", "uk-UA": "Ukrainian", "sv-SE": "Swedish", "da-DK": "Danish", "fi-FI": "Finnish",
    "nb-NO": "Norwegian", "ar-SA": "Arabic", "th-TH": "Thai", "ro-RO": "Romanian", "el-GR": "Greek",
}

_UNIX_SPECIAL = {"zh-Hans": "zh_CN", "zh-Hant": "zh_TW"}


def name(code: str) -> str:
    return NAMES.get(code, code)


def system_default(env=os.environ) -> str:
    """Idioma de juego que corresponde al locale del sistema (en-US si no hay ninguno)."""
    loc = next((env[v] for v in ("LC_ALL", "LC_MESSAGES", "LANG") if env.get(v)), "")
    loc = loc.split(".")[0].split("@")[0]
    lang, _sep, region = loc.partition("_")
    if lang == "zh":
        return "zh-Hant" if region in ("TW", "HK", "MO") else "zh-Hans"
    if lang == "es" and region and region != "ES":
        return "es-MX"  # GOG solo distingue el español de España y el latinoamericano
    if lang == "pt":
        return "pt-BR" if region == "BR" else "pt-PT"
    if f"{lang}-{region}" in NAMES:
        return f"{lang}-{region}"
    return next((c for c in NAMES if c.split("-")[0] == lang), "en-US")


def preferred() -> str:
    """Idioma con el que se instalan los juegos: el elegido en Ajustes o el del sistema."""
    return config.load().get("language") or system_default()


def from_store(store_languages: dict | list | None) -> list[str]:
    keys = store_languages.keys() if isinstance(store_languages, dict) else (store_languages or [])
    return sorted({STORE_TO_CODE.get(k, k) for k in keys})


def unix_locale(code: str) -> str:
    base = _UNIX_SPECIAL.get(code) or code.replace("-", "_")
    return f"{base}.UTF-8"


@functools.lru_cache(maxsize=1)
def _available() -> frozenset[str]:
    try:
        out = subprocess.run(["locale", "-a"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    return frozenset(l.strip().lower().replace("utf-8", "utf8") for l in out.splitlines())


def locale_available(loc: str) -> bool:
    avail = _available()
    return not avail or loc.lower().replace("utf-8", "utf8") in avail


def env_for(code: str | None) -> dict[str, str]:
    """Variables para que Wine/Proton (y los juegos nativos) usen ese idioma."""
    if not code:
        return {}
    loc = unix_locale(code)
    if not locale_available(loc):
        return {}
    return {"HOST_LC_ALL": loc, "LC_ALL": loc, "LANG": loc, "LANGUAGE": loc.split(".")[0]}


def apply_to_game_info(install_path: Path, code: str | None) -> None:
    """Galaxy guarda el idioma elegido en goggame-<id>.info; algunos juegos (los
    que usan el SDK de Galaxy) lo leen de ahí."""
    if not code or code not in GALAXY_NAMES:
        return
    for p in list(install_path.glob("goggame-*.info")) + list(install_path.glob("game/goggame-*.info")):
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if data.get("language") != GALAXY_NAMES[code]:
            data["language"] = GALAXY_NAMES[code]
            try:
                p.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass


# LCID de Windows para la clave Nls\Language (idioma de instalación del sistema).
LCID = {
    "en-US": "0409", "es-ES": "0c0a", "es-MX": "080a", "fr-FR": "040c", "de-DE": "0407",
    "it-IT": "0410", "pt-BR": "0416", "pt-PT": "0816", "ru-RU": "0419", "pl-PL": "0415",
    "zh-Hans": "0804", "zh-Hant": "0404", "ja-JP": "0411", "ko-KR": "0412", "cs-CZ": "0405",
    "hu-HU": "040e", "tr-TR": "041f", "nl-NL": "0413", "uk-UA": "0422", "sv-SE": "041d",
    "da-DK": "0406", "fi-FI": "040b", "nb-NO": "0414", "ar-SA": "0401", "th-TH": "041e",
    "ro-RO": "0418", "el-GR": "0408",
}

_NLS_SECTION = "[System\\\\ControlSet001\\\\Control\\\\Nls\\\\Language]"


def apply_to_prefix(pfx: Path, code: str | None) -> bool:
    """Pone el idioma de instalación de Windows del prefijo (algunos juegos antiguos
    lo leen del registro en vez de preguntar a Windows). Debe llamarse con Wine
    parado: wineserver reescribe system.reg al salir."""
    lcid = LCID.get(code or "")
    reg = pfx / "system.reg"
    if not lcid or not reg.exists():
        return False
    lines = reg.read_text(encoding="utf-8", errors="surrogateescape").splitlines(keepends=True)
    out, in_section, changed = [], False, False
    for line in lines:
        if line.startswith("["):
            in_section = line.startswith(_NLS_SECTION)
        elif in_section:
            for key in ('"Default"=', '"InstallLanguage"='):
                if line.startswith(key):
                    new = f'{key}"{lcid}"\n'
                    if line != new:
                        line, changed = new, True
        out.append(line)
    if changed:
        tmp = reg.with_suffix(".reg.gogl-tmp")
        tmp.write_text("".join(out), encoding="utf-8", errors="surrogateescape")
        tmp.replace(reg)
    return changed
