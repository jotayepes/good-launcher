"""Idioma de la interfaz y de los mensajes.

Los textos se escriben en español en el código y ese mismo texto es la clave de
su traducción: `_("Jugar")`, `n_("{n} juego", "{n} juegos", n).format(n=n)`.
Las traducciones son catálogos gettext (gogl/locale/<idioma>.po) que se leen
tal cual, sin compilar; packaging/update_po.py los pone al día con el código.
Para añadir un idioma basta con copiar gogl.pot a <código>.po y traducirlo.

El idioma se decide una vez, al importar este módulo: el ajuste «ui_language»
o, en automático, el del sistema (GOGL_LANG lo fuerza, p. ej. en los tests).
Por eso cambiarlo en los ajustes pide reiniciar."""

from __future__ import annotations

import ast
import os
from pathlib import Path

SOURCE = "es"     # idioma en el que está escrito el código
FALLBACK = "en"   # para un sistema en un idioma sin traducción
LOCALE_DIR = Path(__file__).with_name("locale")

_catalog: dict[str, str | list[str]] = {}
current = SOURCE


def available() -> list[str]:
    """Códigos de idioma con traducción (más el original)."""
    return [SOURCE, *sorted(p.stem for p in LOCALE_DIR.glob("*.po"))]


def native_name(code: str) -> str:
    """Nombre del idioma en ese mismo idioma (cabecera X-Language-Name del .po)."""
    if code == SOURCE:
        return "Español"
    try:
        header = load_po(LOCALE_DIR / f"{code}.po", header=True).get("", "")
    except OSError:
        return code
    for line in header.splitlines():
        key, _sep, value = line.partition(":")
        if key.strip() == "X-Language-Name" and value.strip():
            return value.strip()
    return code


def system_language(env=os.environ) -> str:
    """El primer idioma del sistema que tiene traducción: la lista de LANGUAGE y
    luego el locale que manda (LC_ALL, si no LC_MESSAGES, si no LANG)."""
    langs = available()
    wanted = (env.get("LANGUAGE") or "").split(":")
    wanted.append(next((env[v] for v in ("LC_ALL", "LC_MESSAGES", "LANG") if env.get(v)), ""))
    for item in wanted:
        code = item.split(".")[0].split("@")[0].split("_")[0].split("-")[0].lower()
        if code in langs:
            return code
    return FALLBACK


def resolve(setting: str | None, env=os.environ) -> str:
    forced = env.get("GOGL_LANG")
    for cand in (forced, setting):
        if cand and cand in available():
            return cand
    return system_language(env)


def set_language(code: str) -> None:
    global current, _catalog
    current = code
    try:
        _catalog = {} if code == SOURCE else load_po(LOCALE_DIR / f"{code}.po")
    except OSError:
        _catalog = {}


def _(text: str) -> str:
    t = _catalog.get(text)
    return t if isinstance(t, str) else text


def n_(singular: str, plural: str, n: int) -> str:
    """Singular o plural según n (en español y en inglés: singular solo si n == 1)."""
    t = _catalog.get(singular)
    forms = t if isinstance(t, list) else [singular, plural]
    return forms[0 if n == 1 else 1]


def load_po(path: Path, header: bool = False) -> dict[str, str | list[str]]:
    """{msgid: msgstr}, o {msgid: [singular, plural]}. Omite lo que no está
    traducido y lo marcado «fuzzy». Con header=True incluye la cabecera (msgid "")."""
    out: dict[str, str | list[str]] = {}
    cur: dict[str, str] = {}
    fuzzy = False
    key = ""

    def flush():
        nonlocal cur, fuzzy
        mid = cur.get("msgid")
        if (mid or (header and "msgid" in cur)) and not fuzzy:
            if "msgid_plural" in cur:
                forms = [cur.get("msgstr[0]", ""), cur.get("msgstr[1]", "")]
                if all(forms):
                    out[mid] = forms
            elif cur.get("msgstr"):
                out[mid] = cur["msgstr"]
        cur, fuzzy = {}, False

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#") or line.startswith("msgid "):
            if any(k.startswith("msgstr") for k in cur):
                flush()
            if line.startswith("#,") and "fuzzy" in line:
                fuzzy = True
            if line.startswith("#"):
                continue
        if line.startswith('"'):
            cur[key] += ast.literal_eval(line)
        else:
            key, _sep, rest = line.partition(" ")
            cur[key] = ast.literal_eval(rest)
    flush()
    return out


def _setting() -> str | None:
    try:
        from . import config
        return config.load().get("ui_language")
    except Exception:  # noqa: BLE001 - un settings.json roto no impide arrancar
        return None


def web_language() -> str:
    """Idioma para la web de GOG (rutas /es/…, /en/…)."""
    return current


def date_format() -> str:
    return _("%d/%m/%Y %H:%M")


set_language(resolve(_setting()))
