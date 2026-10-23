#!/usr/bin/env python3
"""Pone al día las traducciones con los textos del código.

Busca las llamadas _("…") y n_("…", "…", n) en gogl/ y gogl_ui/, escribe la
plantilla gogl/locale/gogl.pot y actualiza cada <idioma>.po: conserva lo ya
traducido, añade lo nuevo sin traducir y quita lo que ya no se usa.

Uso: packaging/update_po.py          actualiza los archivos
     packaging/update_po.py --check  solo comprueba (falla si falta algo)"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from gogl.i18n import LOCALE_DIR, load_po  # noqa: E402

SOURCES = ("gogl", "gogl_ui")
HEADER = """\
msgid ""
msgstr ""
"Project-Id-Version: gogl\\n"
"Language: {lang}\\n"
"X-Language-Name: {name}\\n"
"MIME-Version: 1.0\\n"
"Content-Type: text/plain; charset=UTF-8\\n"
"Content-Transfer-Encoding: 8bit\\n"
"Plural-Forms: nplurals=2; plural=(n != 1);\\n"
"""


def _binds_underscore(fn) -> bool:
    """¿Usa la función `_` como nombre propio (parámetro, `for _ in`, `a, _ = …`)?"""
    a = fn.args
    params = a.posonlyargs + a.args + a.kwonlyargs + [x for x in (a.vararg, a.kwarg) if x]
    if any(p.arg == "_" for p in params):
        return True
    body = [fn.body] if isinstance(fn, ast.Lambda) else fn.body
    todo = list(body)
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue  # su propio ámbito
        if isinstance(node, ast.Name) and node.id == "_" and isinstance(node.ctx, ast.Store):
            return True
        if isinstance(node, ast.ExceptHandler) and node.name == "_":
            return True
        todo.extend(ast.iter_child_nodes(node))
    return False


def _calls_underscore(fn) -> bool:
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("_", "n_")
               for n in ast.walk(fn))


def extract() -> tuple[dict[tuple[str, ...], list[str]], list[str]]:
    """{(msgid,) o (msgid, plural): [archivos]} en orden de aparición, y errores."""
    found: dict[tuple[str, ...], list[str]] = {}
    errors = []
    for pkg in SOURCES:
        for path in sorted((ROOT / pkg).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), rel)
            for node in ast.walk(tree):
                if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                        and _binds_underscore(node) and _calls_underscore(node)):
                    errors.append(f"{rel}:{node.lineno}: usa «_» como variable y también llama a _()")
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id in ("_", "n_")):
                    continue
                n = 1 if node.func.id == "_" else 2
                args = node.args[:n]
                if len(node.args) != (1 if n == 1 else 3) or not all(
                        isinstance(a, ast.Constant) and isinstance(a.value, str) for a in args):
                    errors.append(f"{rel}:{node.lineno}: {node.func.id}() necesita texto literal")
                    continue
                key = tuple(a.value for a in args)
                files = found.setdefault(key, [])
                if rel not in files:
                    files.append(rel)
    return found, errors


def quote(s: str) -> str:
    body = s.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t")
    lines = body.split("\n")
    if len(lines) == 1:
        return f'"{body}"'
    parts = [l + "\\n" for l in lines[:-1]] + ([lines[-1]] if lines[-1] else [])
    return '""\n' + "\n".join(f'"{p}"' for p in parts)


def render(found, lang: str, name: str, old: dict) -> tuple[str, int]:
    out = [HEADER.format(lang=lang, name=name)]
    missing = 0
    for key, files in found.items():
        t = old.get(key[0])
        out.append("#: " + " ".join(files))
        out.append("msgid " + quote(key[0]))
        if len(key) == 2:
            forms = t if isinstance(t, list) else ["", ""]
            missing += not all(forms)
            out.append("msgid_plural " + quote(key[1]))
            out += [f"msgstr[{i}] " + quote(f) for i, f in enumerate(forms)]
        else:
            t = t if isinstance(t, str) else ""
            missing += not t
            out.append("msgstr " + quote(t))
        out.append("")
    return "\n".join(out), missing


def main() -> int:
    check = "--check" in sys.argv
    found, errors = extract()
    for e in errors:
        print(e, file=sys.stderr)
    ok = not errors
    outputs = {LOCALE_DIR / "gogl.pot": render(found, "", "", {})[0]}
    for po in sorted(LOCALE_DIR.glob("*.po")):
        header = load_po(po, header=True).get("", "")
        name = next((l.split(":", 1)[1].strip() for l in header.splitlines()
                     if l.startswith("X-Language-Name:")), po.stem)
        text, missing = render(found, po.stem, name, load_po(po))
        outputs[po] = text
        print(f"{po.name}: {len(found)} textos, {missing} sin traducir")
        ok &= not missing
    for path, text in outputs.items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != text:
            if check:
                print(f"{path.relative_to(ROOT)} no está al día", file=sys.stderr)
                ok = False
            else:
                path.write_text(text, encoding="utf-8")
    return 0 if ok or not check else 1


if __name__ == "__main__":
    sys.exit(main())
