#!/usr/bin/env python3
"""X11 cursor sampler for OpenScreen on Linux.

The Linux counterpart of the Windows ``cursor-sampler.exe`` and the macOS cursor helper: it
reports, every ``interval`` milliseconds, where the pointer is, which cursor is showing (arrow,
hand, text I-beam, ...) and whether the left button is down, as JSON lines on stdout:

    {"type": "ready", "timestampMs": ...}
    {"type": "sample", "timestampMs": ..., "x": 1069, "y": 999, "visible": true,
     "handle": "41", "cursorType": "pointer", "leftButtonDown": false, "asset": null}

Coordinates are X screen pixels (physical). ``timestampMs`` is wall-clock epoch milliseconds, the
same clock as ``Date.now()`` in Electron.

Dependency-free: it talks to libX11 / libXfixes / libXcursor through ctypes, so it needs only the
X11 runtime libraries every X desktop already has (no compiler, no -dev headers, no pip).

Which cursor is showing comes from XFixes. Apps that load themed cursors by name (GTK, Qt, most
X toolkits) give XFixes that name ("hand2", "xterm", "pointer", ...). Apps that build a cursor
from pixels (Chromium can) leave it unnamed; for those the pixels are matched against the
images the current cursor theme ships for each known name, at the sizes a scaled desktop uses.
Anything still unknown is reported as "arrow".
"""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import json
import os
import signal
import sys
import time

# ── cursor name → OpenScreen's NativeCursorType ───────────────────────────────────────
# Covers the legacy X cursor-font names and the CSS / freedesktop names cursor themes alias.
CURSOR_TYPES: dict[str, str] = {}


def _alias(cursor_type: str, *names: str) -> None:
    for name in names:
        CURSOR_TYPES[name] = cursor_type


_alias("arrow", "left_ptr", "default", "arrow", "top_left_arrow", "right_ptr", "draft_large",
       "draft_small", "context-menu", "copy", "alias", "dnd-copy", "dnd-link", "dnd-none")
_alias("text", "xterm", "text", "ibeam", "vertical-text")
_alias("pointer", "hand2", "hand1", "hand", "pointer", "pointing_hand", "e29285e634086352946a0e7090d73106")
_alias("crosshair", "crosshair", "cross", "tcross", "cross_reverse", "cell", "plus", "diamond_cross")
_alias("open-hand", "grab", "openhand", "hand-open")
_alias("closed-hand", "grabbing", "closedhand", "dnd-move", "dnd-ask", "hand-closed")
_alias("resize-ew", "sb_h_double_arrow", "h_double_arrow", "ew-resize", "col-resize",
       "e-resize", "w-resize", "left_side", "right_side", "size_hor", "split_h")
_alias("resize-ns", "sb_v_double_arrow", "v_double_arrow", "ns-resize", "row-resize",
       "n-resize", "s-resize", "top_side", "bottom_side", "size_ver", "split_v",
       "double_arrow")
_alias("resize-nesw", "nesw-resize", "ne-resize", "sw-resize", "top_right_corner",
       "bottom_left_corner", "fd_double_arrow", "size_bdiag")
_alias("resize-nwse", "nwse-resize", "nw-resize", "se-resize", "top_left_corner",
       "bottom_right_corner", "bd_double_arrow", "size_fdiag")
_alias("move", "fleur", "move", "all-scroll", "size_all")
_alias("not-allowed", "not-allowed", "crossed_circle", "no-drop", "forbidden", "circle",
       "pirate", "X_cursor")
_alias("wait", "watch", "wait")
_alias("app-starting", "left_ptr_watch", "progress", "half-busy")
_alias("help", "question_arrow", "help", "whats_this", "left_ptr_help", "dnd-question")
_alias("up-arrow", "center_ptr", "up-arrow", "sb_up_arrow")


def cursor_type_for_name(name: str | None) -> str | None:
    """The OpenScreen cursor type for an X cursor name, or None when the name is unknown."""
    if not name:
        return None
    return CURSOR_TYPES.get(name) or CURSOR_TYPES.get(name.strip().lower())


# ── ctypes bindings ──────────────────────────────────────────────────────────────────
class XFixesCursorImage(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_short),
        ("y", ctypes.c_short),
        ("width", ctypes.c_ushort),
        ("height", ctypes.c_ushort),
        ("xhot", ctypes.c_ushort),
        ("yhot", ctypes.c_ushort),
        ("cursor_serial", ctypes.c_ulong),
        ("pixels", ctypes.POINTER(ctypes.c_ulong)),
        ("atom", ctypes.c_ulong),
        ("name", ctypes.c_char_p),
    ]


class XcursorImage(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint),
        ("size", ctypes.c_uint),
        ("width", ctypes.c_uint),
        ("height", ctypes.c_uint),
        ("xhot", ctypes.c_uint),
        ("yhot", ctypes.c_uint),
        ("delay", ctypes.c_uint),
        ("pixels", ctypes.POINTER(ctypes.c_uint)),
    ]


BUTTON1_MASK = 1 << 8


def _load(name: str, fallback: str):
    path = ctypes.util.find_library(name) or fallback
    return ctypes.CDLL(path)


class X11:
    def __init__(self) -> None:
        self.xlib = _load("X11", "libX11.so.6")
        self.xfixes = _load("Xfixes", "libXfixes.so.3")
        try:
            self.xcursor = _load("Xcursor", "libXcursor.so.1")
        except OSError:
            self.xcursor = None

        self.xlib.XOpenDisplay.restype = ctypes.c_void_p
        self.xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.xlib.XDefaultRootWindow.restype = ctypes.c_ulong
        self.xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self.xlib.XQueryPointer.restype = ctypes.c_int
        self.xlib.XQueryPointer.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
        ]
        self.xlib.XFree.argtypes = [ctypes.c_void_p]
        self.xfixes.XFixesGetCursorImage.restype = ctypes.POINTER(XFixesCursorImage)
        self.xfixes.XFixesGetCursorImage.argtypes = [ctypes.c_void_p]
        if self.xcursor is not None:
            self.xcursor.XcursorGetTheme.restype = ctypes.c_char_p
            self.xcursor.XcursorGetTheme.argtypes = [ctypes.c_void_p]
            self.xcursor.XcursorGetDefaultSize.restype = ctypes.c_int
            self.xcursor.XcursorGetDefaultSize.argtypes = [ctypes.c_void_p]
            self.xcursor.XcursorLibraryLoadImage.restype = ctypes.POINTER(XcursorImage)
            self.xcursor.XcursorLibraryLoadImage.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
            self.xcursor.XcursorImageDestroy.argtypes = [ctypes.POINTER(XcursorImage)]

        self.display = self.xlib.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError("cannot open the X display (is this an X11 session with DISPLAY set?)")
        self.root = self.xlib.XDefaultRootWindow(self.display)

    def pointer(self) -> tuple[int, int, bool]:
        root_ret, child_ret = ctypes.c_ulong(), ctypes.c_ulong()
        rx, ry, wx, wy = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        mask = ctypes.c_uint()
        self.xlib.XQueryPointer(
            self.display, self.root, ctypes.byref(root_ret), ctypes.byref(child_ret),
            ctypes.byref(rx), ctypes.byref(ry), ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(mask),
        )
        return rx.value, ry.value, bool(mask.value & BUTTON1_MASK)

    def cursor(self) -> tuple[int, str, str | None]:
        """(serial, pixel digest, name) of the cursor currently showing."""
        image = self.xfixes.XFixesGetCursorImage(self.display)
        if not image:
            return 0, "", None
        try:
            c = image.contents
            count = int(c.width) * int(c.height)
            # XFixes hands back each ARGB pixel in an unsigned long; only the low 32 bits count.
            argb = (ctypes.c_ulong * count).from_address(ctypes.addressof(c.pixels.contents))
            digest = _digest(c.width, c.height, (p & 0xFFFFFFFF for p in argb))
            name = c.name.decode("utf-8", "replace") if c.name else None
            return int(c.cursor_serial), digest, name
        finally:
            self.xlib.XFree(image)

    def theme_digests(self) -> dict[str, str]:
        """digest → cursor type for every known name in the current theme, at the sizes a
        scaled desktop loads (unnamed cursors are matched against these)."""
        if self.xcursor is None:
            return {}
        theme = self.xcursor.XcursorGetTheme(self.display)
        base = self.xcursor.XcursorGetDefaultSize(self.display) or 24
        sizes = sorted({base, 24, 32, 40, 48, 64, int(base * 1.25), int(base * 1.5), base * 2})
        table: dict[str, str] = {}
        for name, cursor_type in CURSOR_TYPES.items():
            for size in sizes:
                image = self.xcursor.XcursorLibraryLoadImage(name.encode(), theme, size)
                if not image:
                    continue
                try:
                    c = image.contents
                    count = int(c.width) * int(c.height)
                    pixels = (ctypes.c_uint * count).from_address(ctypes.addressof(c.pixels.contents))
                    table.setdefault(_digest(c.width, c.height, pixels), cursor_type)
                finally:
                    self.xcursor.XcursorImageDestroy(image)
        return table


def _digest(width: int, height: int, pixels) -> str:
    h = hashlib.blake2b(digest_size=12)
    h.update(f"{width}x{height}:".encode())
    h.update(b"".join(int(p).to_bytes(4, "little") for p in pixels))
    return h.hexdigest()


# ── main loop ────────────────────────────────────────────────────────────────────────
def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    interval_ms = 33.0
    if len(argv) > 1:
        try:
            interval_ms = max(5.0, float(argv[1]))
        except ValueError:
            pass

    try:
        x = X11()
    except Exception as exc:  # noqa: BLE001 — reported to the session, which falls back
        emit({"type": "error", "timestampMs": time.time() * 1000, "message": str(exc)})
        return 1

    theme = x.theme_digests()
    by_serial: dict[int, str] = {}
    running = True

    def _stop(*_args) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    emit({"type": "ready", "timestampMs": time.time() * 1000})
    interval = interval_ms / 1000.0
    next_tick = time.monotonic()
    while running:
        try:
            px, py, left_down = x.pointer()
            serial, digest, name = x.cursor()
            cursor_type = by_serial.get(serial)
            if cursor_type is None:
                cursor_type = cursor_type_for_name(name) or theme.get(digest) or "arrow"
                if serial:
                    by_serial[serial] = cursor_type
            emit({
                "type": "sample",
                "timestampMs": time.time() * 1000,
                "x": px,
                "y": py,
                "visible": True,
                "handle": str(serial) if serial else None,
                "cursorType": cursor_type,
                "leftButtonDown": left_down,
                "asset": None,
            })
        except BrokenPipeError:
            # The reader went away (recording stopped). Point stdout at /dev/null so the
            # interpreter's exit-time flush doesn't print a second BrokenPipeError.
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            return 0
        except Exception as exc:  # noqa: BLE001 — one bad tick must not end the recording
            sys.stderr.write(f"cursor sample failed: {exc}\n")
        next_tick += interval
        delay = next_tick - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            next_tick = time.monotonic()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
