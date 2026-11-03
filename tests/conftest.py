"""Test setup: the modules import pynput and sounddevice at load time, which
want a display and PortAudio. Neither is needed for these tests."""
import os
import sys
import types
from pathlib import Path

os.environ.setdefault("PYNPUT_BACKEND", "dummy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import sounddevice  # noqa: F401
except Exception:
    sys.modules["sounddevice"] = types.SimpleNamespace(InputStream=None)

try:
    import evdev  # noqa: F401
except ImportError:
    # Just enough of python-evdev for evdev_listener to import.
    ecodes = types.SimpleNamespace(
        KEY_LEFTCTRL=29, KEY_RIGHTCTRL=97, KEY_LEFTALT=56, KEY_RIGHTALT=100,
        KEY_LEFTSHIFT=42, KEY_RIGHTSHIFT=54, KEY_LEFTMETA=125, KEY_RIGHTMETA=126,
        KEY_CAPSLOCK=58, KEY_PAUSE=119, KEY_SCROLLLOCK=70, KEY_INSERT=110,
        KEY_HOME=102, KEY_END=107, KEY_PAGEUP=104, KEY_PAGEDOWN=109,
        KEY_UP=103, KEY_DOWN=108, KEY_LEFT=105, KEY_RIGHT=106, KEY_F23=193,
        KEY_A=30, KEY_Z=44, BTN_SIDE=0x113, BTN_EXTRA=0x114, BTN_FORWARD=0x115,
        BTN_BACK=0x116, BTN_TASK=0x117, EV_KEY=1, EV_REL=2, REL_X=0, REL_Y=1,
        **{f"KEY_F{i}": 58 + i for i in range(1, 11)}, KEY_F11=87, KEY_F12=88,
    )
    fake = types.ModuleType("evdev")
    fake.ecodes = ecodes
    fake.InputDevice = None
    sys.modules["evdev"] = fake
    sys.modules["evdev.ecodes"] = ecodes
