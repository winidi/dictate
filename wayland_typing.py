"""Convert text to physical key chords using the active XKB layout.

No clipboard, paste shortcut, or compositor-specific input protocol is used.
libxkbcommon is accessed through ctypes so no compiled Python binding is needed.
"""
import ctypes as C
import os
from pathlib import Path
import re
import shlex
import unicodedata


class RuleNames(C.Structure):
    _fields_ = [(name, C.c_char_p) for name in ("rules", "model", "layout", "variant", "options")]


def _bind(lib, name, result, *args):
    function = getattr(lib, name)
    function.restype = result
    function.argtypes = args
    return function


def configured_layout():
    """Fallback when no live XWayland keymap can be read; never assume US."""
    if os.environ.get("XKB_DEFAULT_LAYOUT"):
        return (os.environ["XKB_DEFAULT_LAYOUT"], os.environ.get("XKB_DEFAULT_VARIANT", ""),
                os.environ.get("XKB_DEFAULT_OPTIONS", ""))
    # systemd-localed writes the X11 config; Debian also uses /etc/default/keyboard.
    for path in (Path("/etc/X11/xorg.conf.d/00-keyboard.conf"), Path("/etc/default/keyboard")):
        if not path.exists():
            continue
        content = path.read_text()
        values = {}
        for key in ("Layout", "Variant", "Options"):
            match = re.search(rf'Option\s+"Xkb{key}"\s+"([^"]*)"', content, re.I)
            if match:
                values[key] = match.group(1)
            else:
                match = re.search(rf'^XKB{key.upper()}=(.*)$', content, re.M)
                if match:
                    parts = shlex.split(match.group(1), comments=True)
                    values[key] = parts[0] if parts else ""
        if values.get("Layout"):
            return values["Layout"], values.get("Variant", ""), values.get("Options", "")
    raise RuntimeError("Cannot detect keyboard layout — set Typing layout in Settings (e.g. at or de)")


class Keymap:
    def __init__(self, layout="auto"):
        try:
            self.lib = C.CDLL("libxkbcommon.so.0")
        except OSError as e:
            raise RuntimeError("Layout-aware typing needs libxkbcommon — rerun install.sh") from e
        pointer, uint = C.c_void_p, C.c_uint32
        for name, result, args in (
            ("xkb_context_new", pointer, [C.c_int]),
            ("xkb_context_unref", None, [pointer]),
            ("xkb_keymap_new_from_names", pointer, [pointer, C.POINTER(RuleNames), C.c_int]),
            ("xkb_keymap_unref", None, [pointer]),
            ("xkb_keymap_min_keycode", uint, [pointer]),
            ("xkb_keymap_max_keycode", uint, [pointer]),
            ("xkb_state_new", pointer, [pointer]),
            ("xkb_state_unref", None, [pointer]),
            ("xkb_state_update_key", uint, [pointer, uint, C.c_int]),
            ("xkb_state_key_get_utf32", uint, [pointer, uint]),
            ("xkb_state_key_get_one_sym", uint, [pointer, uint]),
            ("xkb_state_serialize_mods", uint, [pointer, uint]),
            ("xkb_state_serialize_layout", uint, [pointer, uint]),
            ("xkb_state_update_mask", uint, [pointer, uint, uint, uint, uint, uint, uint]),
        ):
            _bind(self.lib, name, result, *args)
        self.context = self.lib.xkb_context_new(0)
        self.keymap = self.state = None
        if not self.context:
            raise RuntimeError("Cannot create XKB context")
        try:
            if layout == "auto":
                self._from_display()
            if not self.keymap:
                if layout == "auto":
                    name, variant, options = configured_layout()
                    if "," in name:
                        raise RuntimeError("Multiple keyboard layouts configured — set the active Typing layout in Settings")
                else:
                    match = re.fullmatch(r"([a-zA-Z0-9_-]+)(?:\(([a-zA-Z0-9_-]+)\))?", layout)
                    if not match:
                        raise ValueError("Typing layout must be an XKB name such as at, de or de(nodeadkeys)")
                    name, variant = match.group(1), match.group(2) or ""
                    options = ""
                names = RuleNames(None, None, name.encode(), variant.encode(), options.encode())
                self.keymap = self.lib.xkb_keymap_new_from_names(self.context, C.byref(names), 0)
                if not self.keymap:
                    raise RuntimeError("Cannot load the selected keyboard layout — check Typing layout in Settings")
                self.state = self.lib.xkb_state_new(self.keymap)
            if not self.state:
                raise RuntimeError("Cannot create XKB keyboard state")
            self.locked = self.lib.xkb_state_serialize_mods(self.state, 1 << 2)
            self.group = self.lib.xkb_state_serialize_layout(self.state, 1 << 7)
            self.characters, self.dead_keys = self._characters()
        except Exception:
            self.close()
            raise

    def _from_display(self):
        if not os.environ.get("DISPLAY"):
            return
        connection = None
        try:
            xcb = C.CDLL("libxcb.so.1")
            x11 = C.CDLL("libxkbcommon-x11.so.0")
            pointer, uint = C.c_void_p, C.c_uint32
            _bind(xcb, "xcb_connect", pointer, C.c_char_p, C.POINTER(C.c_int))
            _bind(xcb, "xcb_connection_has_error", C.c_int, pointer)
            _bind(xcb, "xcb_disconnect", None, pointer)
            _bind(x11, "xkb_x11_setup_xkb_extension", C.c_int, pointer, C.c_uint16,
                  C.c_uint16, C.c_int, pointer, pointer, pointer, pointer)
            _bind(x11, "xkb_x11_get_core_keyboard_device_id", C.c_int32, pointer)
            _bind(x11, "xkb_x11_keymap_new_from_device", pointer, pointer, pointer, C.c_int32, C.c_int)
            _bind(x11, "xkb_x11_state_new_from_device", pointer, pointer, pointer, C.c_int32)
            connection = xcb.xcb_connect(None, None)
            if not connection or xcb.xcb_connection_has_error(connection):
                return
            if not x11.xkb_x11_setup_xkb_extension(connection, 1, 0, 0, None, None, None, None):
                return
            device = x11.xkb_x11_get_core_keyboard_device_id(connection)
            if device < 0:
                return
            keymap = x11.xkb_x11_keymap_new_from_device(self.context, connection, device, 0)
            if keymap:
                state = x11.xkb_x11_state_new_from_device(keymap, connection, device)
                if state:
                    self.keymap, self.state = keymap, state
                else:
                    self.lib.xkb_keymap_unref(keymap)
        except OSError:
            pass
        finally:
            if connection:
                xcb.xcb_disconnect(connection)

    def _characters(self):
        characters, dead_keys = {}, {}
        minimum = max(9, self.lib.xkb_keymap_min_keycode(self.keymap))
        maximum = min(255, self.lib.xkb_keymap_max_keycode(self.keymap))
        # Modifier effects come from the keymap, including custom AltGr options.
        # Linux evdev codes are XKB keycodes minus 8.
        for modifiers in ((), (42,), (100,), (42, 100)):
            self.lib.xkb_state_update_mask(self.state, 0, 0, self.locked, 0, 0, self.group)
            for code in modifiers:
                self.lib.xkb_state_update_key(self.state, code + 8, 1)
            for code in range(minimum, maximum + 1):
                symbol = self.lib.xkb_state_key_get_one_sym(self.state, code)
                chord = (*modifiers, code - 8)
                if 0xFE50 <= symbol <= 0xFE6F:
                    dead_keys.setdefault(symbol, chord)
                    continue
                character = self.lib.xkb_state_key_get_utf32(self.state, code)
                if character and (character >= 32 and character != 127):
                    characters.setdefault(chr(character), chord)
            for code in reversed(modifiers):
                self.lib.xkb_state_update_key(self.state, code + 8, 0)
        # Newlines/tabs use their ordinary physical keys, just as before.
        characters.update({"\n": (28,), "\r": (28,), "\t": (15,)})
        return characters, dead_keys

    def chords(self, text):
        result = []
        accents = {"\u0300": 0xFE50, "\u0301": 0xFE51, "\u0302": 0xFE52,
                   "\u0303": 0xFE53, "\u0308": 0xFE57}
        spacing = {"`": 0xFE50, "´": 0xFE51, "^": 0xFE52, "~": 0xFE53, "¨": 0xFE57}
        for character in text:
            if character in self.characters:
                result.append(self.characters[character])
                continue
            if character in spacing and spacing[character] in self.dead_keys:
                result.extend((self.dead_keys[spacing[character]], self.characters[" "]))
                continue
            decomposed = unicodedata.normalize("NFD", character)
            if (len(decomposed) == 2 and decomposed[0] in self.characters
                    and accents.get(decomposed[1]) in self.dead_keys):
                result.extend((self.dead_keys[accents[decomposed[1]]], self.characters[decomposed[0]]))
                continue
            # Validate everything before sending any events; no silent dropping
            # or automatic clipboard fallback for unavailable characters.
            raise RuntimeError("Text contains characters unavailable in this keyboard layout — choose Clipboard paste in Settings")
        return result

    def close(self):
        if self.state:
            self.lib.xkb_state_unref(self.state)
            self.state = None
        if self.keymap:
            self.lib.xkb_keymap_unref(self.keymap)
            self.keymap = None
        if self.context:
            self.lib.xkb_context_unref(self.context)
            self.context = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def key_chords(text, layout="auto"):
    with Keymap(layout) as keymap:
        return keymap.chords(text)
