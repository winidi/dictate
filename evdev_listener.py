"""Evdev-based global keyboard listener.

pynput's X11 backend goes blind on Wayland (Mutter doesn't forward global
key events to XWayland's XRecord). Reading /dev/input/event* directly via
evdev works on both Wayland and Xorg regardless of window focus, so we
prefer it over pynput's Listener whenever the input group grants access.

The Microsoft Copilot key arrives in one of two ways and both are reported
to the callback as the single core.COPILOT_KEY sentinel:
  - as KEY_ASSISTANT (0x247) on standards-compliant keyboards (kernel >= 6.6);
  - as the Super+Shift+F23 macro on HP laptops, which have no KEY_ASSISTANT
    at all. The three codes are folded back into one press/release here.

Requires:
  - python3-evdev
  - membership in group `input` (sudo usermod -a -G input $USER; re-login)
"""
import glob
import os
import select
import sys
import threading

import evdev
from evdev import ecodes
from pynput import keyboard

from core import COPILOT_KEY

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

# Microsoft Copilot key. Kernels >= 6.6 report it as KEY_ASSISTANT
# (0x247); before that it was reported as left-Super, where it is
# indistinguishable from the Super key. Spelled as a literal because
# python3-evdev may predate the symbol.
_KEY_MAP[0x247] = COPILOT_KEY

# HP firmware (ZBook Ultra G1a and siblings) does not emit KEY_ASSISTANT at
# all: the Copilot key is a keyboard *macro* that presses left-Meta, then
# left-Shift, then KEY_F23 (0xc1), and releases all three. F23 lives on no
# physical key of any supported keyboard, so seeing it while Super+Shift are
# held identifies the Copilot key unambiguously. _HP_COPILOT_MODS is the set
# of codes that must be down for the F23 press to become COPILOT_KEY.
_HP_COPILOT_MODS = {ecodes.KEY_LEFTMETA, ecodes.KEY_LEFTSHIFT}

# Mouse-side buttons (thumb rocker + gesture button on Logitech MX etc).
# Exposed as ("mouse", name) tuples so they slot into core.KEY_MAP as sentinels
# without needing a pynput.Key equivalent.
# Plain left/middle/right clicks stay out of _KEY_MAP: the GUI treats an
# unmapped click as "another key", so a click while the hotkey is held
# cancels the recording — same as on Windows/macOS/X11.
_KEY_MAP[ecodes.BTN_SIDE]    = ("mouse", "side")
_KEY_MAP[ecodes.BTN_EXTRA]   = ("mouse", "extra")
_KEY_MAP[ecodes.BTN_FORWARD] = ("mouse", "forward")
_KEY_MAP[ecodes.BTN_BACK]    = ("mouse", "back")
_KEY_MAP[ecodes.BTN_TASK]    = ("mouse", "task")


def _copilot_mods_held(held):
    """True while any Super/Shift code of the HP Copilot gesture is still down."""
    return bool(_HP_COPILOT_MODS & held)


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
                             ecodes.BTN_FORWARD, ecodes.BTN_BACK, ecodes.BTN_TASK,
                             0x247}  # KEY_ASSISTANT — the Copilot key
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
            raise RuntimeError(
                "no readable keyboard devices in /dev/input "
                "(user not in group `input`? run: sudo usermod -a -G input $USER, then re-login)"
            )
        self._stop_r, self._stop_w = os.pipe()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        fds = [self._stop_r] + [d.fd for d in self._devices]
        # Keys currently down, across all keyboards, so the HP Copilot
        # shortcut (Super+Shift+F23) can be recognised as one gesture.
        held = set()
        # True between the Copilot press and its release. The release is
        # mirrored from this flag instead of re-testing the modifier set:
        # HP does not guarantee that F23 comes up before Super/Shift, and if
        # it does not, the held-set test would fail and the release would be
        # swallowed, leaving the app stuck "recording".
        copilot_down = False
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
                            copilot_down = self._dispatch(ev, held, copilot_down)
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

    def _dispatch(self, ev, held, copilot_down):
        """Translate one key event and hand it to the callbacks.

        `held` is the shared set of codes currently down; it exists only to
        fold HP's Super+Shift+F23 macro into a single Copilot press/release
        — that gesture is reported as three separate key codes, and the app
        should see one key, not a shortcut.

        `copilot_down` carries whether the fold is currently open. The press
        opens it (Super+Shift are guaranteed down by then); the release
        closes it by matching the F23-up, not by re-testing the modifier
        set, because HP may report the modifier releases first. Returns the
        new state.
        """
        is_press = ev.value == 1
        if is_press:
            held.add(ev.code)
        else:
            held.discard(ev.code)

        if ev.code == ecodes.KEY_F23:
            if is_press and _HP_COPILOT_MODS <= held:
                copilot_down = True          # Super+Shift+F23 -> Copilot down
            elif not is_press and copilot_down:
                copilot_down = False         # F23 up -> Copilot up
            else:
                # Bare F23 (no Super+Shift) is not the Copilot key; drop it
                # so it can never be mistaken for the hotkey, and swallow
                # F23-up events that do not belong to an open fold.
                return copilot_down
            key = COPILOT_KEY
        else:
            key = _KEY_MAP.get(ev.code) or _OtherKey(ev.code)

        # Super/Shift belong to the Copilot gesture for as long as either
        # the fold is open or these codes are still in the gesture; report
        # them only once the fold has closed AND both are up. This covers
        # both release orders: F23-first leaves the fold closed with the
        # mods still down, mods-first leaves the fold open.
        if ev.code in _HP_COPILOT_MODS and (copilot_down or _copilot_mods_held(held)):
            return copilot_down

        cb = self._on_press if is_press else self._on_release
        if cb:
            try:
                cb(key)
            except Exception as e:
                print(f"evdev callback error: {e}", file=sys.stderr)
        return copilot_down

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
