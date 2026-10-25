"""Mando y pantalla táctil dentro de las páginas web (la tienda, el perfil…).

Qt no sabe qué hay dentro de una vista web, así que el foco del mando se mueve
con JavaScript en un «mundo» aparte del de la página (no ve sus scripts ni ellos
a este): las mismas reglas espaciales que en navigation.pick(), entre enlaces,
botones y campos visibles. Cuando no queda nada en esa dirección, la página se
desplaza; y si ya no puede, el foco sale de la web a la interfaz (la barra de la
tienda, la lateral).

Al pulsar A sobre un enlace o botón se hace un clic de verdad en su centro (como
con el dedo); sobre un campo de texto se abre el teclado de Steam.

En gamescope, el dedo llega como ratón: arrastrar seleccionaba texto o cogía un
enlace en vez de desplazar. TOUCH_JS lo convierte en desplazamiento (con
inercia), como en un navegador táctil."""

from __future__ import annotations

import json
import time

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget

from . import theme
from .web import HAS_WEBENGINE

if HAS_WEBENGINE:
    from PySide6.QtWebEngineCore import QWebEngineScript
    from PySide6.QtWebEngineWidgets import QWebEngineView

PAD_JS = r"""
if (!window.__goglPad) window.__goglPad = (() => {
  const SEL = 'a[href], area[href], button, input:not([type=hidden]), select, textarea, summary, ' +
    '[tabindex]:not([tabindex="-1"]), [role=button], [role=link], [role=tab], [role=menuitem], ' +
    '[role=option], [role=checkbox], [role=radio], [role=switch], [contenteditable=""], [contenteditable=true]';
  const CLS = '__gogl_pad_focus';
  let cur = null;

  function style() {
    if (document.getElementById('__gogl_pad_style')) return;
    const s = document.createElement('style');
    s.id = '__gogl_pad_style';
    s.textContent = `.${CLS} { outline: 3px solid ACCENT !important; outline-offset: 2px !important;
      box-shadow: 0 0 0 7px ACCENT55 !important; }`;
    (document.head || document.documentElement).appendChild(s);
  }
  const vw = () => innerWidth, vh = () => innerHeight;
  function rectOf(el) {
    if (!el || !el.isConnected || el.disabled || el.closest('[inert]')) return null;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return null;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.pointerEvents === 'none' || +cs.opacity === 0) return null;
    return r;
  }
  const inView = r => r.bottom > 0 && r.top < vh() && r.right > 0 && r.left < vw();
  function onTop(el, r) {  // no tapado por un aviso o un menú encima
    const x = Math.min(Math.max(r.left + r.width / 2, 1), vw() - 1);
    const y = Math.min(Math.max(r.top + r.height / 2, 1), vh() - 1);
    const hit = document.elementFromPoint(x, y);
    return !!hit && (hit === el || el.contains(hit) || hit.contains(el) && hit.matches(SEL));
  }
  function visible() {
    const out = [];
    for (const el of document.querySelectorAll(SEL)) {
      const r = rectOf(el);
      if (r && inView(r) && onTop(el, r)) out.push({el, r});
    }
    return out;
  }
  function pick(c, cands, d) {  // como navigation.pick()
    let best = null, key = null;
    for (const x of cands) {
      const r = x.r;
      let ok, major, beam, minor;
      if (d === 'down' || d === 'up') {
        ok = d === 'down' ? r.top > c.top && r.bottom > c.bottom : r.bottom < c.bottom && r.top < c.top;
        major = d === 'down' ? r.top - c.bottom : c.top - r.bottom;
        beam = r.left <= c.right && r.right >= c.left;
        minor = Math.abs((r.left + r.right) / 2 - (c.left + c.right) / 2);
      } else {
        ok = d === 'right' ? r.left > c.left && r.right > c.right : r.right < c.right && r.left < c.left;
        major = d === 'right' ? r.left - c.right : c.left - r.right;
        beam = r.top <= c.bottom && r.bottom >= c.top;
        minor = Math.abs((r.top + r.bottom) / 2 - (c.top + c.bottom) / 2);
      }
      if (!ok) continue;
      major = Math.max(major, 0);
      const k = [beam ? 0 : 1, 13 * major * major + minor * minor];
      if (key === null || k[0] < key[0] || k[0] === key[0] && k[1] < key[1]) { best = x; key = k; }
    }
    return best;
  }
  function focus(el) {
    style();
    if (cur && cur !== el) cur.classList.remove(CLS);
    cur = el;
    el.classList.add(CLS);
    try { el.focus({preventScroll: true, focusVisible: true}); } catch (e) {}
    const r = el.getBoundingClientRect(), m = Math.min(120, vh() * 0.15);
    const se = document.scrollingElement || document.documentElement;
    if (r.top < m) se.scrollBy({top: r.top - m, behavior: 'instant'});
    else if (r.bottom > vh() - m) se.scrollBy({top: Math.min(r.bottom - vh() + m, r.top - m), behavior: 'instant'});
  }
  const box = r => [r.left, r.top, r.width, r.height];
  function current() {
    const r = rectOf(cur);
    return r && inView(r) ? r : null;
  }
  function canScroll(d) {
    const se = document.scrollingElement || document.documentElement;
    if (d === 'down') return se.scrollTop + innerHeight < se.scrollHeight - 2;
    if (d === 'up') return se.scrollTop > 0;
    return false;
  }
  function scroll(dx, dy) {
    let n = cur && cur.isConnected ? cur.parentElement : null;
    for (; n && n !== document.body && n !== document.documentElement; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (dy && /(auto|scroll)/.test(cs.overflowY) && n.scrollHeight > n.clientHeight) break;
      if (dx && /(auto|scroll)/.test(cs.overflowX) && n.scrollWidth > n.clientWidth) break;
    }
    if (!n || n === document.body || n === document.documentElement) n = document.scrollingElement || document.documentElement;
    n.scrollBy({left: dx, top: dy, behavior: 'instant'});
  }
  // Un borde de la vista, como si el foco viniera de fuera de la página.
  function edge(d) {
    const w = vw(), h = vh();
    return {up: {left: 0, right: w, top: h + 1, bottom: h + 2}, down: {left: 0, right: w, top: -2, bottom: -1},
            left: {left: w + 1, right: w + 2, top: 0, bottom: h}, right: {left: -2, right: -1, top: 0, bottom: h}}[d];
  }
  return {
    // 'moved' | 'scrolled' | {edge: [x, y, ancho, alto] o null}: no queda nada hacia allí
    move(d) {
      const c = current(), cands = visible().filter(x => !cur || x.el !== cur && !x.el.contains(cur) && !cur.contains(x.el));
      const t = pick(c || edge(d), cands, d);
      if (t) { focus(t.el); return 'moved'; }
      if (canScroll(d)) { scroll(0, (d === 'down' ? 1 : -1) * vh() * 0.6); return 'scrolled'; }
      return {edge: c ? box(c) : null};
    },
    // Al llegar desde la interfaz: el último elemento si aún se ve, si no el más cercano a `from`.
    enter(d, from) {
      if (current()) { focus(cur); return true; }
      const cands = visible();
      let t = null;
      if (from) t = pick({left: from[0], top: from[1], right: from[0] + from[2], bottom: from[1] + from[3]}, cands, d);
      if (!t && d) t = pick(edge(d), cands, d);
      if (!t) t = cands.sort((a, b) => a.r.top - b.r.top || a.r.left - b.r.left)[0];
      if (t) focus(t.el);
      return !!t;
    },
    // Qué hacer con A: {click: [x, y]}, {input: true} o null si no hay nada marcado.
    activate() {
      const r = current();
      if (!r) return null;
      if (cur.matches('input:not([type=button]):not([type=submit]):not([type=checkbox]):not([type=radio]), textarea, [contenteditable=""], [contenteditable=true]')) {
        cur.focus();
        return {input: true};
      }
      if (cur.tagName === 'SELECT') {  // su lista sería otra ventana: se pasa a la opción siguiente
        const n = cur.options.length;
        if (n) {
          cur.selectedIndex = (cur.selectedIndex + 1) % n;
          cur.dispatchEvent(new Event('input', {bubbles: true}));
          cur.dispatchEvent(new Event('change', {bubbles: true}));
        }
        return {select: true};
      }
      const x0 = Math.max(r.left, 0), x1 = Math.min(r.right, vw()), y0 = Math.max(r.top, 0), y1 = Math.min(r.bottom, vh());
      return {click: [(x0 + x1) / 2, (y0 + y1) / 2]};
    },
    scroll(dy) { scroll(0, dy); },
    page(sign) { scroll(0, sign * vh() * 0.85); },
    clear() { if (cur) cur.classList.remove(CLS); cur = null; },
  };
})();
""".replace("ACCENT55", theme.ACCENT + "55").replace("ACCENT", theme.ACCENT)

TOUCH_JS = r"""
(() => {
  if (window.__goglTouch) return;
  window.__goglTouch = true;
  const editable = el => el && el.closest && el.closest('input, textarea, [contenteditable=""], [contenteditable=true]');
  let s = null, dragging = false, eat = false, fling = 0;
  function scroller(el, dx, dy) {
    for (let n = el; n && n !== document.body && n !== document.documentElement; n = n.parentElement) {
      if (!(n instanceof Element)) continue;
      const cs = getComputedStyle(n);
      if (Math.abs(dy) >= Math.abs(dx) ? /(auto|scroll)/.test(cs.overflowY) && n.scrollHeight > n.clientHeight
                                       : /(auto|scroll)/.test(cs.overflowX) && n.scrollWidth > n.clientWidth) return n;
    }
    return document.scrollingElement || document.documentElement;
  }
  addEventListener('pointerdown', e => {
    cancelAnimationFrame(fling);
    if (e.pointerType !== 'mouse' || e.button !== 0 || editable(e.target)) { s = null; return; }
    s = {x: e.clientX, y: e.clientY, t: e.target, el: null, v: [0, 0], at: e.timeStamp};
    dragging = false;
  }, true);
  addEventListener('pointermove', e => {
    if (!s) return;
    if (!(e.buttons & 1)) { s = null; return; }
    const dx = e.clientX - s.x, dy = e.clientY - s.y;
    if (!dragging) {
      if (Math.hypot(dx, dy) < 12) return;
      dragging = true;
      s.el = scroller(s.t, dx, dy);
    }
    s.el.scrollBy({left: -dx, top: -dy, behavior: 'instant'});
    const dt = Math.max(e.timeStamp - s.at, 1);
    s.v = [0.8 * (-dx / dt) + 0.2 * s.v[0], 0.8 * (-dy / dt) + 0.2 * s.v[1]];
    s.x = e.clientX; s.y = e.clientY; s.at = e.timeStamp;
    e.preventDefault();
    e.stopPropagation();
  }, true);
  addEventListener('pointerup', e => {
    if (dragging && s) {
      eat = true;  // el clic que viene detrás del arrastre no cuenta
      setTimeout(() => { eat = false; }, 0);
      let [vx, vy] = s.v, last = performance.now();
      const el = s.el;
      if (Math.hypot(vx, vy) > 0.3) {  // inercia
        const step = now => {
          const dt = now - last; last = now;
          el.scrollBy({left: vx * dt, top: vy * dt, behavior: 'instant'});
          vx *= Math.pow(0.995, dt); vy *= Math.pow(0.995, dt);
          if (Math.hypot(vx, vy) > 0.05) fling = requestAnimationFrame(step);
        };
        fling = requestAnimationFrame(step);
      }
    }
    s = null;
    dragging = false;
  }, true);
  addEventListener('click', e => { if (eat) { e.preventDefault(); e.stopImmediatePropagation(); } }, true);
  addEventListener('dragstart', e => e.preventDefault(), true);
  addEventListener('selectstart', e => { if (!editable(e.target)) e.preventDefault(); }, true);
})();
"""

BUSY_FOR = 0.5  # s: una orden sin respuesta (la página cambiaba) no bloquea las siguientes


def is_view(w) -> bool:
    return HAS_WEBENGINE and isinstance(w, QWebEngineView)


def view_of(w: QWidget | None):
    """La vista web que contiene a w (el foco de Qt está en un hijo suyo), o None."""
    while w is not None:
        if is_view(w):
            return w
        w = w.parentWidget()
    return None


def install_touch(page) -> None:
    """Arrastrar para desplazar en cada documento de la página (solo en gamescope)."""
    s = QWebEngineScript()
    s.setName("gogl-touch")
    s.setSourceCode(TOUCH_JS)
    s.setInjectionPoint(QWebEngineScript.DocumentCreation)
    s.setWorldId(QWebEngineScript.ApplicationWorld)
    s.setRunsOnSubFrames(True)
    page.scripts().insert(s)


def _call(view, expr: str, done=None, exclusive: bool = False) -> None:
    if exclusive:
        if time.monotonic() < getattr(view, "_pad_busy", 0):
            return
        view._pad_busy = time.monotonic() + BUSY_FOR

    def cb(result, view=view):
        if exclusive:
            view._pad_busy = 0
        if done is not None:
            try:
                done(json.loads(result) if isinstance(result, str) else None)
            except ValueError:
                done(None)

    # Como texto JSON: los objetos de JavaScript no siempre llegan bien a Python.
    view.page().runJavaScript(f"{PAD_JS}\nJSON.stringify({expr})", QWebEngineScript.ApplicationWorld, cb)


def _to_global(view, box) -> QRect | None:
    """Un rectángulo de la página (px CSS desde la esquina visible) en coordenadas de pantalla."""
    if not box:
        return None
    z = view.page().zoomFactor()
    x, y, w, h = (float(v) * z for v in box)
    return QRect(view.mapToGlobal(QPoint(int(x), int(y))), QRect(0, 0, max(int(w), 1), max(int(h), 1)).size())


def move(view, d: str, on_edge) -> None:
    """Mueve el foco dentro de la página; on_edge(rect global o None) si no queda nada hacia d.
    Mientras no hay página (cargando, error) también se sale."""
    def done(r):
        if r in ("moved", "scrolled"):
            return
        on_edge(_to_global(view, r.get("edge")) if isinstance(r, dict) else None)
    _call(view, f"__goglPad.move({json.dumps(d)})", done, exclusive=True)


def enter(view, d: str | None = None, origin: QRect | None = None) -> None:
    """El foco llega a la vista desde la interfaz (origin: de dónde venía, en pantalla)."""
    frm = None
    if origin is not None:
        z = view.page().zoomFactor() or 1.0
        p = view.mapFromGlobal(origin.topLeft())
        frm = [p.x() / z, p.y() / z, origin.width() / z, origin.height() / z]
    _call(view, f"__goglPad.enter({json.dumps(d)}, {json.dumps(frm)})")


def activate(view, on_input) -> None:
    def done(r):
        if not isinstance(r, dict):
            enter(view)  # nada marcado (la página acaba de cargar): A muestra dónde está el foco
            return
        if r.get("input"):
            on_input()
        elif r.get("click"):
            z = view.page().zoomFactor()
            x, y = (float(v) * z for v in r["click"])
            click(view, QPointF(x, y))
    _call(view, "__goglPad.activate()", done, exclusive=True)


def click(view, pos: QPointF) -> None:
    """Un clic izquierdo de verdad (pulsar y soltar) en pos, dentro de la vista."""
    target = view.focusProxy() or view
    local = QPointF(target.mapFrom(view, pos.toPoint())) if target is not view else pos
    glob = QPointF(target.mapToGlobal(local.toPoint()))
    for etype, buttons in ((QEvent.MouseMove, Qt.NoButton), (QEvent.MouseButtonPress, Qt.LeftButton),
                           (QEvent.MouseButtonRelease, Qt.NoButton)):
        button = Qt.NoButton if etype == QEvent.MouseMove else Qt.LeftButton
        QApplication.sendEvent(target, QMouseEvent(etype, local, glob, button, buttons, Qt.NoModifier))


def scroll(view, dy: int) -> None:
    _call(view, f"__goglPad.scroll({int(dy)})")


def page(view, sign: int) -> None:
    _call(view, f"__goglPad.page({1 if sign > 0 else -1})")


def clear(view) -> None:
    """Quita la marca del foco (el foco se ha ido de la página o ya no se usa el mando)."""
    _call(view, "__goglPad.clear()")


__all__ = ["PAD_JS", "TOUCH_JS", "activate", "clear", "click", "enter", "install_touch", "is_view", "move",
           "page", "scroll", "view_of"]
