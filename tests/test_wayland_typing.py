"""Check generated key chords against libxkbcommon's forward key/compose handling."""
import ctypes as C
import os
import shutil
import subprocess
import unittest
from unittest.mock import patch

from wayland_typing import Keymap, _bind, configured_layout


def render(keymap, chords):
    lib = keymap.lib
    pointer, uint = C.c_void_p, C.c_uint32
    for name, result, args in (
        ("xkb_compose_table_new_from_locale", pointer, [pointer, C.c_char_p, C.c_int]),
        ("xkb_compose_table_unref", None, [pointer]),
        ("xkb_compose_state_new", pointer, [pointer, C.c_int]),
        ("xkb_compose_state_unref", None, [pointer]),
        ("xkb_compose_state_feed", C.c_int, [pointer, uint]),
        ("xkb_compose_state_get_status", C.c_int, [pointer]),
        ("xkb_compose_state_get_utf8", C.c_int, [pointer, pointer, C.c_size_t]),
        ("xkb_compose_state_reset", None, [pointer]),
    ):
        _bind(lib, name, result, *args)
    state = lib.xkb_state_new(keymap.keymap)
    table = lib.xkb_compose_table_new_from_locale(keymap.context, b"C.UTF-8", 0)
    compose = lib.xkb_compose_state_new(table, 0)
    assert state and table and compose
    lib.xkb_state_update_mask(state, 0, 0, keymap.locked, 0, 0, keymap.group)
    result = []
    try:
        for chord in chords:
            for code in chord:
                lib.xkb_state_update_key(state, code + 8, 1)
            code = chord[-1] + 8
            lib.xkb_compose_state_feed(compose, lib.xkb_state_key_get_one_sym(state, code))
            status = lib.xkb_compose_state_get_status(compose)
            if status == 2:
                buffer = C.create_string_buffer(128)
                lib.xkb_compose_state_get_utf8(compose, buffer, len(buffer))
                result.append(buffer.value.decode("utf-8"))
                lib.xkb_compose_state_reset(compose)
            elif status == 0:
                character = lib.xkb_state_key_get_utf32(state, code)
                if character:
                    result.append(chr(character))
            elif status == 3:
                raise AssertionError("Generated compose sequence was cancelled")
            for code in reversed(chord):
                lib.xkb_state_update_key(state, code + 8, 0)
        assert lib.xkb_compose_state_get_status(compose) == 0
        return "".join(result).replace("\r", "\n")
    finally:
        lib.xkb_compose_state_unref(compose)
        lib.xkb_compose_table_unref(table)
        lib.xkb_state_unref(state)


class KeymapTests(unittest.TestCase):
    def test_german_text_round_trips_as_individual_keys(self):
        text = "Kannst du auch Fachwörter wie Kubernetes korrekt interpretieren, damit ich über meine Deployments reden kann? Argo CD nutze ich für Monitoring-Tätigkeiten. äöüß ÄÖÜ yz YZ — … € @ {} [] | \\ ~ ^\n"
        for layout in ("de", "at", "de(nodeadkeys)"):
            with self.subTest(layout=layout), Keymap(layout) as keymap:
                self.assertEqual(render(keymap, keymap.chords(text)), text)

    def test_dead_key_accents_round_trip(self):
        text = "Café déjà vu: naïve, résumé, rôle."
        with Keymap("de") as keymap:
            self.assertEqual(render(keymap, keymap.chords(text)), text)

    def test_us_and_french_layouts(self):
        for layout, text in (("us", "Kubernetes Deployments yz YZ! @#$"),
                             ("fr", "azerty QWERTY éèçà! @ €")):
            with self.subTest(layout=layout), Keymap(layout) as keymap:
                self.assertEqual(render(keymap, keymap.chords(text)), text)

    def test_caps_lock_is_accounted_for(self):
        with Keymap("de") as keymap:
            index = _bind(keymap.lib, "xkb_keymap_mod_get_index", C.c_uint32,
                          C.c_void_p, C.c_char_p)(keymap.keymap, b"Lock")
            keymap.locked = 1 << index
            keymap.characters, keymap.dead_keys = keymap._characters()
            text = "yz YZ äöü ÄÖÜ Deployments!"
            self.assertEqual(render(keymap, keymap.chords(text)), text)

    def test_unavailable_character_fails_without_dropping_it(self):
        with Keymap("de") as keymap:
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                keymap.chords("Kubernetes 😃")

    def test_invalid_layout_is_rejected(self):
        with self.assertRaises(ValueError):
            Keymap("de,us")

    def test_explicit_layout_overrides_system_defaults(self):
        with patch.dict(os.environ, {"XKB_DEFAULT_LAYOUT": "us"}), Keymap("de") as keymap:
            self.assertEqual(keymap.chords("yz"), [(44,), (21,)])

    def test_fallback_reads_xkb_environment_and_refuses_multiple_layouts(self):
        with patch.dict(os.environ, {"DISPLAY": "", "XKB_DEFAULT_LAYOUT": "de", "XKB_DEFAULT_VARIANT": "nodeadkeys"}):
            self.assertEqual(configured_layout()[:2], ("de", "nodeadkeys"))
            with Keymap() as keymap:
                self.assertEqual(render(keymap, keymap.chords("Grüße yz")), "Grüße yz")
        with patch.dict(os.environ, {"DISPLAY": "", "XKB_DEFAULT_LAYOUT": "de,us"}):
            with self.assertRaisesRegex(RuntimeError, "Multiple keyboard layouts"):
                Keymap()

    @unittest.skipUnless(shutil.which("Xvfb") and shutil.which("setxkbmap"), "needs Xvfb and setxkbmap")
    def test_auto_reads_live_keymap_and_caps_lock_without_changing_it(self):
        server = subprocess.Popen(["Xvfb", "-displayfd", "1", "-screen", "0", "800x600x24"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        display = None
        try:
            name = ":" + server.stdout.readline().strip()
            x11 = C.CDLL("libX11.so.6")
            _bind(x11, "XOpenDisplay", C.c_void_p, C.c_char_p)
            _bind(x11, "XCloseDisplay", C.c_int, C.c_void_p)
            _bind(x11, "XkbLockModifiers", C.c_int, C.c_void_p, C.c_uint, C.c_uint, C.c_uint)
            _bind(x11, "XSync", C.c_int, C.c_void_p, C.c_int)
            display = x11.XOpenDisplay(name.encode())
            self.assertTrue(display)
            env = dict(os.environ, DISPLAY=name)
            subprocess.run(["setxkbmap", "-layout", "de"], env=env, check=True)
            x11.XkbLockModifiers(display, 256, 2, 2)  # core keyboard, Caps Lock
            x11.XSync(display, 0)
            with patch.dict(os.environ, {"DISPLAY": name, "XKB_DEFAULT_LAYOUT": "us"}), \
                    patch("wayland_typing.configured_layout", side_effect=AssertionError("must use the live map")), \
                    Keymap() as keymap:
                text = "Kubernetes äöü ÄÖÜ yz YZ!"
                self.assertTrue(keymap.locked)
                self.assertEqual(render(keymap, keymap.chords(text)), text)
        finally:
            if display:
                x11.XCloseDisplay(display)
            server.terminate()
            server.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
