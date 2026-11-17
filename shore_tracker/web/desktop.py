"""Control only this user's dedicated X11 kiosk window; no hardware commands."""
from __future__ import annotations

import ctypes as c
import os
from pathlib import Path
import shlex
import subprocess
import threading
import time

_lock = threading.Lock()


def _desktop_environment() -> dict[str, str]:
    env = os.environ.copy()
    if env.get("DISPLAY"):
        return env
    for proc in Path("/proc").iterdir():
        try:
            if (not proc.name.isdigit() or proc.stat().st_uid != os.getuid()
                    or b"/usr/lib/gnome-session/gnome-session-binary\0" not in (proc / "cmdline").read_bytes()):
                continue
            values = dict(v.split(b"=", 1) for v in (proc / "environ").read_bytes().split(b"\0") if b"=" in v)
            for key in ("DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"):
                if key.encode() in values:
                    env[key] = values[key.encode()].decode()
            if env.get("DISPLAY"):
                return env
        except (OSError, ValueError):
            continue
    raise RuntimeError("找不到 Jetson 桌面工作階段")


def _browser_pid() -> int:
    profile = "--user-data-dir=" + str(Path.home() / ".local/share/shore-tracker/browser")
    matches = []
    for proc in Path("/proc").iterdir():
        try:
            if not proc.name.isdigit() or proc.stat().st_uid != os.getuid():
                continue
            args = [v.decode() for v in (proc / "cmdline").read_bytes().split(b"\0") if v]
            # Chromium rewrites argv as one space-delimited string on this Nano.
            if len(args) == 1:
                args = shlex.split(args[0])
            if (args and "chromium" in Path(args[0]).name and profile in args
                    and not any(v.startswith("--type=") for v in args)):
                matches.append(int(proc.name))
        except (OSError, ValueError, UnicodeError):
            continue
    if len(matches) != 1:
        raise RuntimeError("找不到唯一的 Shore Jetson 瀏覽器")
    return matches[0]


def _command(env: dict[str, str], *args: str) -> str:
    return subprocess.check_output(args, env=env, text=True, stderr=subprocess.DEVNULL, timeout=3).strip()


class _Data(c.Union):
    _fields_ = [("b", c.c_char * 20), ("s", c.c_short * 10), ("l", c.c_long * 5)]


class _Client(c.Structure):
    _fields_ = [("type", c.c_int), ("serial", c.c_ulong), ("send_event", c.c_int),
                ("display", c.c_void_p), ("window", c.c_ulong), ("message_type", c.c_ulong),
                ("format", c.c_int), ("data", _Data)]


class _Event(c.Union):
    _fields_ = [("client", _Client), ("pad", c.c_long * 24)]


def _fullscreen(env: dict[str, str], window: int, enabled: bool) -> None:
    # EWMH _NET_WM_STATE client messages; uses the installed libX11, no new package.
    x = c.CDLL("libX11.so.6")
    x.XOpenDisplay.argtypes = [c.c_char_p]
    x.XOpenDisplay.restype = c.c_void_p
    x.XDefaultRootWindow.argtypes = [c.c_void_p]
    x.XDefaultRootWindow.restype = c.c_ulong
    x.XInternAtom.argtypes = [c.c_void_p, c.c_char_p, c.c_int]
    x.XInternAtom.restype = c.c_ulong
    x.XSendEvent.argtypes = [c.c_void_p, c.c_ulong, c.c_int, c.c_long, c.POINTER(_Event)]
    x.XFlush.argtypes = [c.c_void_p]
    x.XCloseDisplay.argtypes = [c.c_void_p]
    # Xlib reads XAUTHORITY from the process environment, not the display name.
    authority = env.get("XAUTHORITY")
    previous = os.environ.get("XAUTHORITY")
    try:
        if authority:
            os.environ["XAUTHORITY"] = authority
        display = x.XOpenDisplay(env["DISPLAY"].encode())
    finally:
        if previous is None:
            os.environ.pop("XAUTHORITY", None)
        else:
            os.environ["XAUTHORITY"] = previous
    if not display:
        raise RuntimeError("無法連接 Jetson 桌面")
    try:
        def atom(name: str) -> int:
            return x.XInternAtom(display, name.encode(), 0)
        root = x.XDefaultRootWindow(display)
        def state(action: int, first: str, second: str | None = None) -> None:
            event = _Event()
            event.client.type, event.client.display = 33, display
            event.client.window = window
            event.client.message_type, event.client.format = atom("_NET_WM_STATE"), 32
            event.client.data.l[:] = (action, atom(first), atom(second) if second else 0, 1, 0)
            if not x.XSendEvent(display, root, 0, (1 << 19) | (1 << 20), c.byref(event)):
                raise RuntimeError("桌面未接受視窗調整")
        if enabled:
            state(1, "_NET_WM_STATE_FULLSCREEN")
        else:
            state(0, "_NET_WM_STATE_FULLSCREEN", "_NET_WM_STATE_MAXIMIZED_VERT")
            state(0, "_NET_WM_STATE_MAXIMIZED_HORZ")
        x.XFlush(display)
    finally:
        x.XCloseDisplay(display)


def set_kiosk_compact(compact: bool) -> dict:
    """Expose the desktop with a half-size native window, or restore fullscreen."""
    if type(compact) is not bool:
        raise ValueError("compact 必須為布林值")
    with _lock:
        try:
            env = _desktop_environment()
            pid = _browser_pid()
            windows = _command(env, "xdotool", "search", "--onlyvisible", "--pid", str(pid)).splitlines()
            if len(windows) != 1 or not windows[0].isdigit():
                raise RuntimeError("找不到唯一的 kiosk 視窗")
            window = windows[0]
            screen = [int(v) for v in _command(env, "xdotool", "getdisplaygeometry").split()]
            width, height = [v // 2 if compact else v for v in screen]
            _fullscreen(env, int(window), not compact)
            for _ in range(15):
                time.sleep(.1)
                if compact:
                    _command(env, "xdotool", "windowsize", window, str(width), str(height))
                geometry = dict(v.split("=", 1) for v in _command(env, "xdotool", "getwindowgeometry", "--shell", window).splitlines())
                if int(geometry["WIDTH"]) == width and int(geometry["HEIGHT"]) == height:
                    return {"compact": compact, "width": width, "height": height}
            raise RuntimeError("視窗尺寸未完成調整")
        except (OSError, subprocess.SubprocessError, KeyError, ValueError) as exc:
            raise RuntimeError("無法調整 Jetson 瀏覽器視窗") from exc
