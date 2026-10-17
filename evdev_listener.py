"""Evdev-based global keyboard listener.

pynput's X11 backend goes blind on Wayland (Mutter doesn't forward global
key events to XWayland's XRecord). Reading /dev/input/event* directly via
evdev works on both Wayland and Xorg regardless of window focus, so we
prefer it over pynput's Listener whenever the input group grants access.

Requires:
  - python3-evdev
  - membership in group `input` (sudo usermod -a -G input $USER; re-login)
"""
import glob
import grp
import os
import pwd
import select
import sys
import threading

import evdev
from evdev import ecodes
from pynput import keyboard

_KEY_MAP = {
    ecodes.KEY_LEFTCTRL:   keyboard.Key.ctrl_l,
    ecodes.KEY_RIGHTCTRL:  keyboard.Key.ctrl_r,
    ecodes.KEY_LEFTALT:    keyboard.Key.alt_l,
    ecodes.KEY_RIGHTALT:   keyboard.Key.alt_r,
    ecodes.KEY_LEFTSHIFT:  keyboard.Key.shift_l,
    ecodes.KEY_RIGHTSHIFT: keyboard.Key.shift_r,
    ecodes.KEY_LEFTMETA:   keyboard.Key.cmd_l,
    ecodes.KEY_RIGHTMETA:  keyboard.Key.cmd_r,
    ecodes.KEY_CAPSLOCK:   keyboard.Key.caps_lock,
    ecodes.KEY_PAUSE:      keyboard.Key.pause,
    ecodes.KEY_SCROLLLOCK: keyboard.Key.scroll_lock,
    ecodes.KEY_INSERT:     keyboard.Key.insert,
    ecodes.KEY_HOME:       keyboard.Key.home,
    ecodes.KEY_END:        keyboard.Key.end,
    ecodes.KEY_PAGEUP:     keyboard.Key.page_up,
    ecodes.KEY_PAGEDOWN:   keyboard.Key.page_down,
    # Arrow keys pick a pie-menu mode while the hotkey is held (gui.py).
    ecodes.KEY_UP:         keyboard.Key.up,
    ecodes.KEY_DOWN:       keyboard.Key.down,
    ecodes.KEY_LEFT:       keyboard.Key.left,
    ecodes.KEY_RIGHT:      keyboard.Key.right,
}
for _i in range(1, 13):
    _KEY_MAP[getattr(ecodes, f"KEY_F{_i}")] = getattr(keyboard.Key, f"f{_i}")

# Mouse-side buttons (thumb rocker + gesture button on Logitech MX etc).
# Exposed as ("mouse", name) tuples so they slot into core.KEY_MAP as sentinels
# without needing a pynput.Key equivalent.
_KEY_MAP[ecodes.BTN_SIDE]    = ("mouse", "side")
_KEY_MAP[ecodes.BTN_EXTRA]   = ("mouse", "extra")
_KEY_MAP[ecodes.BTN_FORWARD] = ("mouse", "forward")
_KEY_MAP[ecodes.BTN_BACK]    = ("mouse", "back")
_KEY_MAP[ecodes.BTN_TASK]    = ("mouse", "task")


class _OtherKey:
    __slots__ = ("code",)
    def __init__(self, code): self.code = code
    def __hash__(self):       return hash(("_OtherKey", self.code))
    def __eq__(self, other):  return isinstance(other, _OtherKey) and other.code == self.code
    def __repr__(self):       return f"<OtherKey {self.code}>"


def _find_keyboards():
    """Return open evdev devices for anything that emits keys or mouse-side buttons.

    Includes real keyboards AND mice, so mouse-side buttons can be used as
    hotkeys. Left/right/middle mouse aren't in _KEY_MAP, so normal clicks
    still pass through to the OS untouched — evdev is passive-read only.
    """
    devs = []
    interesting_side_btns = {ecodes.BTN_SIDE, ecodes.BTN_EXTRA,
                             ecodes.BTN_FORWARD, ecodes.BTN_BACK, ecodes.BTN_TASK}
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            d = evdev.InputDevice(path)
        except (PermissionError, OSError):
            continue
        # Skip only ydotool's own transient virtual keyboard to avoid a
        # typing feedback loop. Do NOT skip other uinput devices — Solaar's
        # "solaar-keyboard" is a virtual re-emitter for Logitech hardware
        # and is often the ONLY device that delivers modifier release
        # events on this setup.
        name = (d.name or "").lower()
        if "ydotool" in name:
            d.close()
            continue
        all_caps = d.capabilities()
        caps = set(all_caps.get(ecodes.EV_KEY, []))
        is_keyboard = ecodes.KEY_A in caps and ecodes.KEY_Z in caps and ecodes.KEY_LEFTCTRL in caps
        is_useful_mouse = bool(caps & interesting_side_btns)
        # Any relative pointer (a mouse, not a touchpad) is read as well: its
        # motion drives the pie menu while the hotkey is held. Motion events
        # are only forwarded while track_motion is set, so the cost is a
        # dict lookup per event the rest of the time.
        is_pointer = ecodes.REL_X in set(all_caps.get(ecodes.EV_REL, []))
        if is_keyboard or is_useful_mouse or is_pointer:
            devs.append(d)
        else:
            d.close()
    return devs


def _no_access_reason():
    """Why no input device could be read, saying why first and what to do second.

    Group changes reach only sessions started afterwards, so being in group
    `input` in the group database is not the same as this session having it.
    """
    generic = "no readable keyboard devices in /dev/input"
    try:
        group = grp.getgrnam("input")
        user = pwd.getpwuid(os.getuid())
    except KeyError:
        return generic
    if group.gr_gid in os.getgroups():
        return generic
    if user.pw_name in group.gr_mem or user.pw_gid == group.gr_gid:
        return (f"{user.pw_name} is in group `input`, but this login session started before "
                "that, so /dev/input cannot be read yet. Please log out and log in again "
                "(or reboot)")
    return (f"{user.pw_name} is not in group `input`, which is needed to read /dev/input. "
            f"Please run: sudo usermod -a -G input {user.pw_name}, then log out and log in again")


class Listener:
    """Drop-in replacement for pynput.keyboard.Listener."""

    def __init__(self, on_press=None, on_release=None, on_move=None):
        self._on_press = on_press
        self._on_release = on_release
        self._on_move = on_move          # on_move("rel", dx, dy) from mouse motion
        self.track_motion = False        # gui.py sets this while the hotkey is held
        self._devices = []
        self._thread = None
        self._stop_r = None
        self._stop_w = None
        self.daemon = True

    def start(self):
        self._devices = _find_keyboards()
        if not self._devices:
            raise RuntimeError(_no_access_reason())
        self._stop_r, self._stop_w = os.pipe()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        fds = [self._stop_r] + [d.fd for d in self._devices]
        try:
            while True:
                r, _, _ = select.select(fds, [], [])
                if self._stop_r in r:
                    return
                for d in self._devices:
                    if d.fd not in r:
                        continue
                    try:
                        for ev in d.read():
                            if ev.type == ecodes.EV_REL:
                                if self.track_motion and self._on_move and ev.code in (ecodes.REL_X, ecodes.REL_Y):
                                    try:
                                        if ev.code == ecodes.REL_X:
                                            self._on_move("rel", ev.value, 0)
                                        else:
                                            self._on_move("rel", 0, ev.value)
                                    except Exception as e:
                                        print(f"evdev move callback error: {e}", file=sys.stderr)
                                continue
                            if ev.type != ecodes.EV_KEY:
                                continue
                            if ev.value == 2:
                                continue  # auto-repeat; pynput hides this too
                            key = _KEY_MAP.get(ev.code) or _OtherKey(ev.code)
                            cb = self._on_press if ev.value == 1 else self._on_release
                            if cb:
                                try:
                                    cb(key)
                                except Exception as e:
                                    print(f"evdev callback error: {e}", file=sys.stderr)
                    except BlockingIOError:
                        pass
                    except OSError:
                        pass
        finally:
            for d in self._devices:
                try:
                    d.close()
                except Exception:
                    pass

    def stop(self):
        stop_r, stop_w = self._stop_r, self._stop_w
        if stop_w is None:
            return
        self._stop_r = None
        self._stop_w = None
        try:
            os.write(stop_w, b"x")
        except Exception:
            pass
        # Join BEFORE closing fds so select() can't race a closed descriptor.
        if self._thread:
            self._thread.join(timeout=2)
        for fd in (stop_r, stop_w):
            try:
                os.close(fd)
            except Exception:
                pass

    def join(self):
        if self._thread:
            self._thread.join()
