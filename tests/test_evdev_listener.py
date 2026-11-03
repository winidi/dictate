from types import SimpleNamespace

from evdev import ecodes
from pynput import keyboard

import core
import evdev_listener as el


def ev(code, value):
    return SimpleNamespace(code=code, value=value)


def test_plain_keys_translate_as_before():
    held = set()
    assert el.translate(ev(ecodes.KEY_F9, 1), held) == keyboard.Key.f9
    assert el.translate(ev(ecodes.KEY_F9, 0), held) == keyboard.Key.f9
    assert el.translate(ev(ecodes.KEY_LEFTSHIFT, 1), held) == keyboard.Key.shift_l
    assert el.translate(ev(ecodes.KEY_A, 1), held) == el._OtherKey(ecodes.KEY_A)
    assert held == {ecodes.KEY_LEFTSHIFT, ecodes.KEY_A}


def test_key_assistant_is_the_copilot_key():
    assert el.translate(ev(el.KEY_ASSISTANT, 1), set()) == core.COPILOT_KEY
    assert core.COPILOT_KEY in core.KEY_MAP["copilot"]


def test_hp_macro_folds_into_one_copilot_press_and_release():
    held = set()
    # HP: Meta down, Shift down, F23 down ... F23 up, Shift up, Meta up
    assert el.translate(ev(ecodes.KEY_LEFTMETA, 1), held) == keyboard.Key.cmd_l
    assert el.translate(ev(ecodes.KEY_LEFTSHIFT, 1), held) == keyboard.Key.shift_l
    assert el.translate(ev(el.KEY_F23, 1), held) == core.COPILOT_KEY
    assert el.translate(ev(el.KEY_F23, 0), held) == core.COPILOT_KEY
    assert el.translate(ev(ecodes.KEY_LEFTSHIFT, 0), held) == keyboard.Key.shift_l
    assert el.translate(ev(ecodes.KEY_LEFTMETA, 0), held) == keyboard.Key.cmd_l
    assert held == set()


def test_hp_macro_release_order_does_not_matter():
    held = set()
    for code in (ecodes.KEY_LEFTMETA, ecodes.KEY_LEFTSHIFT, el.KEY_F23):
        el.translate(ev(code, 1), held)
    # modifiers come up first, F23 last: the release must still be reported
    el.translate(ev(ecodes.KEY_LEFTMETA, 0), held)
    el.translate(ev(ecodes.KEY_LEFTSHIFT, 0), held)
    assert el.translate(ev(el.KEY_F23, 0), held) == core.COPILOT_KEY


def test_bare_f23_is_dropped():
    assert el.translate(ev(el.KEY_F23, 1), set()) is None
    assert el.translate(ev(el.KEY_F23, 1), {ecodes.KEY_LEFTSHIFT}) is None


def test_no_access_reason_mentions_the_group():
    assert "input" in el.no_access_reason()
