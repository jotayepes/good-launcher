"""Control con mando: lee los mandos por evdev y mueve el foco de la interfaz.

Sin dependencias: Qt 6 ya no trae soporte de mandos, así que se leen los
dispositivos /dev/input/event* directamente (las reglas «uaccess» de systemd
dan acceso a los mandos al usuario de la sesión).

En el modo juego de SteamOS, Steam se queda con el mando físico y crea uno
virtual tipo Xbox para el programa que lanzó; además pone en
SDL_GAMECONTROLLER_IGNORE_DEVICES los físicos, para que no se lean dos veces.
Aquí se respeta igual que hace SDL.

La navegación es espacial: arriba/abajo/izquierda/derecha llevan al control
más cercano en esa dirección; las listas y cuadrículas (la biblioteca) se
mueven por dentro con las flechas y solo se sale de ellas por el borde."""

from __future__ import annotations

import errno
import fcntl
import glob
import os
import struct
from dataclasses import dataclass, field

# --- evdev ---------------------------------------------------------------------

EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
BTN_SOUTH, BTN_EAST, BTN_C, BTN_NORTH, BTN_WEST = 0x130, 0x131, 0x132, 0x133, 0x134
BTN_TL, BTN_TR, BTN_TL2, BTN_TR2 = 0x136, 0x137, 0x138, 0x139
BTN_SELECT, BTN_START, BTN_MODE = 0x13A, 0x13B, 0x13C
BTN_DPAD_UP, BTN_DPAD_DOWN, BTN_DPAD_LEFT, BTN_DPAD_RIGHT = 0x220, 0x221, 0x222, 0x223
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0, 1, 2, 3, 4, 5
ABS_HAT0X, ABS_HAT0Y = 0x10, 0x11

EVENT = struct.Struct("llHHi")  # struct input_event (timeval + type, code, value)

# En los mandos tipo Xbox (y el virtual de Steam), el código 0x133 es la X
# (botón izquierdo) y 0x134 la Y (botón superior), como los numera SDL.
BUTTONS = {
    BTN_SOUTH: "accept", BTN_EAST: "back", BTN_NORTH: "x", BTN_WEST: "y",
    BTN_TL: "lb", BTN_TR: "rb", BTN_TL2: "lt", BTN_TR2: "rt",
    BTN_SELECT: "select", BTN_START: "start",
    BTN_DPAD_UP: "up", BTN_DPAD_DOWN: "down", BTN_DPAD_LEFT: "left", BTN_DPAD_RIGHT: "right",
}
DIRECTIONS = ("up", "down", "left", "right")
# Stick derecho a fondo: en los diálogos y menús cambia de opción (fuera, desplaza).
RIGHT_STICK = {"rs-up": "up", "rs-down": "down", "rs-left": "left", "rs-right": "right"}


def _ioc(direction: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord("E") << 8) | nr


def _eviocgbit(ev: int, length: int) -> int:
    return _ioc(2, 0x20 + ev, length)


EVIOCGID = _ioc(2, 0x02, 8)
EVIOCGABS = lambda axis: _ioc(2, 0x40 + axis, 24)  # noqa: E731


def _has_bit(buf: bytes, bit: int) -> bool:
    return bool(buf[bit // 8] >> (bit % 8) & 1) if bit // 8 < len(buf) else False


def ignored_devices(env: dict | None = None) -> tuple[set[tuple[int, int]], set[tuple[int, int]] | None]:
    """(ignorados, permitidos) según las variables que Steam pasa a los juegos."""
    env = os.environ if env is None else env

    def parse(v: str) -> set[tuple[int, int]]:
        out = set()
        for item in v.split(","):
            try:
                vid, pid = item.strip().split("/")
                out.add((int(vid, 16), int(pid, 16)))
            except ValueError:
                continue
        return out

    ign = parse(env.get("SDL_GAMECONTROLLER_IGNORE_DEVICES", ""))
    exc = env.get("SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT")
    return ign, (parse(exc) if exc else None)


@dataclass
class Axis:
    lo: int = -32768
    hi: int = 32767

    def norm(self, v: int) -> float:
        """A [-1, 1] (o [0, 1] para gatillos, cuyo mínimo es 0)."""
        if self.hi == self.lo:
            return 0.0
        if self.lo >= 0:
            return (v - self.lo) / (self.hi - self.lo)
        return max(-1.0, min(1.0, (2 * (v - self.lo) / (self.hi - self.lo)) - 1))


@dataclass
class Translator:
    """Eventos evdev de un mando → acciones («up», «accept»…) y desplazamiento analógico."""
    axes: dict[int, Axis] = field(default_factory=dict)
    held: dict[str, set[str]] = field(default_factory=dict)  # dirección → fuentes que la mantienen
    triggers: dict[int, bool] = field(default_factory=dict)
    right: dict[int, str | None] = field(default_factory=dict)  # eje del stick dcho. → dirección
    scroll: float = 0.0  # stick derecho, vertical: -1 arriba … 1 abajo

    PRESS, RELEASE = 0.55, 0.35  # histéresis del stick
    DEAD = 0.25

    def feed(self, etype: int, code: int, value: int) -> list[tuple[str, bool]]:
        """Devuelve [(acción, pulsada)]."""
        if etype == EV_KEY and code in BUTTONS:
            if value == 2:  # autorrepetición del núcleo: la hacemos nosotros
                return []
            name = BUTTONS[code]
            if name in DIRECTIONS:
                return self._source(f"btn-{name}", {name} if value else set())
            return [(name, bool(value))]
        if etype != EV_ABS:
            return []
        ax = self.axes.get(code, Axis(-1, 1) if code in (ABS_HAT0X, ABS_HAT0Y) else Axis())
        v = ax.norm(value)
        if code in (ABS_HAT0X, ABS_HAT0Y):
            neg, pos = ("left", "right") if code == ABS_HAT0X else ("up", "down")
            return self._source(f"hat{code}", {neg} if v < -0.5 else {pos} if v > 0.5 else set())
        if code in (ABS_X, ABS_Y):
            neg, pos = ("left", "right") if code == ABS_X else ("up", "down")
            src = f"stick{code}"
            cur = next((d for d, s in self.held.items() if src in s), None)
            want = self._hysteresis(v, cur, neg, pos)
            return self._source(src, {want} if want else set())
        if code in (ABS_RX, ABS_RY):
            if code == ABS_RY:
                self.scroll = 0.0 if abs(v) < self.DEAD else v
            neg, pos = ("rs-left", "rs-right") if code == ABS_RX else ("rs-up", "rs-down")
            cur = self.right.get(code)
            want = self._hysteresis(v, cur, neg, pos)
            if want == cur:
                return []
            self.right[code] = want
            return ([(cur, False)] if cur else []) + ([(want, True)] if want else [])
        if code in (ABS_Z, ABS_RZ):
            on = v > 0.6 if ax.lo >= 0 else v > 0.2
            if on != self.triggers.get(code, False):
                self.triggers[code] = on
                return [("lt" if code == ABS_Z else "rt", on)]
        return []

    def _hysteresis(self, v: float, cur: str | None, neg: str, pos: str) -> str | None:
        if v <= -self.PRESS:
            return neg
        if v >= self.PRESS:
            return pos
        if abs(v) < self.RELEASE:
            return None
        return cur  # zona de histéresis: no cambia

    def _source(self, src: str, want: set[str]) -> list[tuple[str, bool]]:
        out = []
        for d in DIRECTIONS:
            holders = self.held.setdefault(d, set())
            was = bool(holders)
            if d in want:
                holders.add(src)
            else:
                holders.discard(src)
            if bool(holders) != was:
                out.append((d, bool(holders)))
        return out

    def reset(self) -> list[tuple[str, bool]]:
        out = [(d, False) for d, s in self.held.items() if s]
        out += [(d, False) for d in self.right.values() if d]
        self.held.clear()
        self.right.clear()
        self.scroll = 0.0
        return out


class Device:
    def __init__(self, path: str, fd: int, ident: tuple[int, int]):
        self.path, self.fd, self.ident = path, fd, ident
        self.tr = Translator()
        for code in (ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ, ABS_HAT0X, ABS_HAT0Y):
            try:
                buf = fcntl.ioctl(fd, EVIOCGABS(code), bytes(24))
                _val, lo, hi = struct.unpack("6i", buf)[:3]
                self.tr.axes[code] = Axis(lo, hi)
            except OSError:
                pass

    def read(self) -> list[tuple[str, bool]] | None:
        """Acciones pendientes; None si el mando se ha desconectado."""
        out: list[tuple[str, bool]] = []
        while True:
            try:
                data = os.read(self.fd, EVENT.size * 64)
            except BlockingIOError:
                return out
            except OSError as e:
                if e.errno in (errno.ENODEV, errno.EBADF, errno.EIO):
                    return None
                return out
            if not data:
                return None
            for i in range(0, len(data) - EVENT.size + 1, EVENT.size):
                _s, _us, etype, code, value = EVENT.unpack_from(data, i)
                out.extend(self.tr.feed(etype, code, value))

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


def open_gamepad(path: str, env: dict | None = None) -> Device | None:
    """Abre el dispositivo si es un mando (tiene botón A) y no está excluido."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        keys = fcntl.ioctl(fd, _eviocgbit(EV_KEY, 0x300 // 8), bytes(0x300 // 8))
        if not _has_bit(keys, BTN_SOUTH):
            raise OSError
        _bus, vendor, product, _ver = struct.unpack("4H", fcntl.ioctl(fd, EVIOCGID, bytes(8)))
        ign, only = ignored_devices(env)
        if (vendor, product) in ign or (only is not None and (vendor, product) not in only):
            raise OSError
        return Device(path, fd, (vendor, product))
    except OSError:
        os.close(fd)
        return None


def candidates() -> list[str]:
    return sorted(glob.glob("/dev/input/event*"))
