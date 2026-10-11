"""Switch the Caps Lock function off in the desktop so the key can be the dictation hotkey.

Dictate only listens to the key, it does not consume it, so the desktop would still
toggle Caps Lock on every press and the typed text would come out in capitals. On GNOME
the keyboard option `caps:none` removes that function while the raw key events, which
the evdev listener reads, keep arriving.

Only `caps:none` is ever added or removed; other keyboard options stay as they are. It
is removed again only when this app added it (`managed`), never when the user had it.
Ownership is kept in two places, the config and a small marker file, so that losing the
config does not leave the user without Caps Lock for good.

Caps Lock must be off at the moment the function is switched off: with `caps:none` the
key can no longer undo it, so a state left on would stay on. `apply` therefore switches
it off first, and does not touch the desktop if it cannot.
"""
import ast
import glob
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

SCHEMA = "org.gnome.desktop.input-sources"
KEY = "xkb-options"
OPTION = "caps:none"

GSETTINGS_TIMEOUT_S = 5
QUIT_TIMEOUT_S = 2       # while the app closes, a hung tool must not hold it up
QUIT_LOCK_WAIT_S = 2     # and neither may a worker thread that is still busy
XSET_TIMEOUT_S = 3
PRESS_TIMEOUT_S = 3
STATE_DIR = Path.home() / ".local" / "state" / "dictate"
MARKER = STATE_DIR / "capslock-managed"


def parse_options(text):
    """GVariant text such as "@as []" or "['caps:none', 'grp:alt_shift_toggle']" -> list."""
    text = text.strip()
    if text.startswith("@as"):
        text = text[3:].strip()
    return [str(option) for option in ast.literal_eval(text)]


def format_options(options):
    return "[" + ", ".join(repr(option) for option in options) + "]"


class Gsettings:
    """The GNOME setting. Tests replace it."""

    def __init__(self, timeout=GSETTINGS_TIMEOUT_S):
        self.timeout = timeout

    def get(self):
        out = subprocess.run(["gsettings", "get", SCHEMA, KEY],
                             check=True, capture_output=True, text=True, timeout=self.timeout)
        return parse_options(out.stdout)

    def set(self, options):
        subprocess.run(["gsettings", "set", SCHEMA, KEY, format_options(options)],
                       check=True, capture_output=True, text=True, timeout=self.timeout)


# --- the Caps Lock state -----------------------------------------------------------------

LED_GLOB = "/sys/class/leds/*capslock*/brightness"
SETTLE_S = 1.0           # how long to wait for the state to change after a key press
POLL_S = 0.05
PRESS_COMMANDS = (["xdotool", "key", "Caps_Lock"], ["ydotool", "key", "58:1", "58:0"])


class CapsLockState:
    """Whether Caps Lock is on, and a way to switch it off. Tests replace it."""

    @staticmethod
    def _x_server_state():
        """True/False from the X server, None when it cannot be asked (not X11, no xset, error)."""
        if os.environ.get("XDG_SESSION_TYPE") != "x11" or not shutil.which("xset"):
            return None
        try:
            out = subprocess.run(["xset", "q"], capture_output=True, text=True,
                                 timeout=XSET_TIMEOUT_S).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        found = re.search(r"Caps Lock:\s+(on|off)", out)
        return None if found is None else found.group(1) == "on"

    @staticmethod
    def _led_state():
        """True/False from the keyboard LEDs, None when no LED could be read."""
        values = []
        for path in glob.glob(LED_GLOB):
            try:
                with open(path) as f:
                    values.append(int(f.read().strip() or 0))
            except (OSError, ValueError):
                continue
        return any(v > 0 for v in values) if values else None

    def is_on(self):
        """On X11 the server decides: it is the state the typed text depends on, and a stale
        LED of some other device must not make the app press the key. The LEDs only answer
        where the server cannot be asked."""
        state = self._x_server_state()
        if state is None:
            state = self._led_state()
        return bool(state)

    def _press_and_check(self, cmd):
        try:
            subprocess.run(cmd, capture_output=True, timeout=PRESS_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError):
            return False
        deadline = time.monotonic() + SETTLE_S
        while self.is_on():
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL_S)
        return True

    def turn_off(self):
        """Press Caps Lock once and read the state again. True only when it is off now."""
        if not self.is_on():
            return True
        return any(self._press_and_check(cmd) for cmd in PRESS_COMMANDS if shutil.which(cmd[0]))


# --- who switched the function off -------------------------------------------------------------

def marker_exists(marker=None):
    return Path(marker or MARKER).exists()


def write_marker(marker=None):
    """Remember that this app added `caps:none`: a file for the owner only, in a private folder.
    Best effort; the config holds the same fact."""
    path = Path(marker or MARKER)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("caps:none was added by Dictate\n")
    except OSError:
        pass


def remove_marker(marker=None):
    try:
        Path(marker or MARKER).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def is_available(backend=None):
    """True when the option can be read, that is on GNOME with gsettings installed."""
    if backend is None:
        if not shutil.which("gsettings"):
            return False
        backend = Gsettings()
    try:
        backend.get()
    except Exception:
        return False
    return True


def _sync_marker(managed, marker):
    if managed:
        write_marker(marker)
    else:
        remove_marker(marker)


def apply(wanted, managed, backend=None, caps=None, marker=None):
    """Make the desktop match `wanted`. Returns (managed, message).

    managed: True when this app added `caps:none` earlier, so it may remove it again. The
        marker file counts as well, so a lost config does not make the app forget.
    message: what to tell the user when something went wrong, otherwise "".
    """
    managed = bool(managed) or marker_exists(marker)
    if not wanted and not managed:
        return False, ""
    backend = backend or Gsettings()
    try:
        options = backend.get()
        if wanted:
            if OPTION in options:
                _sync_marker(managed, marker)   # already set; whoever set it keeps ownership
                return managed, ""
            caps = caps or CapsLockState()
            if not caps.turn_off():
                return managed, ("Caps Lock is on and could not be switched off, so its function "
                                 "stays enabled. Press Caps Lock once, then restart Dictate.")
            backend.set(options + [OPTION])
            write_marker(marker)
            return True, ""
        if OPTION in options:
            backend.set([o for o in options if o != OPTION])
        remove_marker(marker)
        return False, ""
    except Exception as e:
        return managed, f"Could not change the Caps Lock setting: {e}"
