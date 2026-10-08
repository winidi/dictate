"""Run with PYNPUT_BACKEND=dummy .venv/bin/python -m unittest discover -s tests -v."""
import os
import subprocess
import unittest
from unittest.mock import patch

import core


TEXT = "Kannst du Fachwörter wie Kubernetes interpretieren? Über Deployments, Argo CD und Monitoring-Tätigkeiten reden. Grüße: äöüß yz 😃\n"
MODERN_HELP = b"Syntax: <keycode>:<pressed>\n"
LEGACY_HELP = b"Each key sequence can be any number of modifiers and keys, separated by plus (+)\n"


class TypingTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "test"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.notifications = patch.object(core, "notify")
        self.notify = self.notifications.start()
        self.addCleanup(self.notifications.stop)

    def test_wayland_preserves_utf8_text_and_releases_modifiers(self):
        for shortcut, events in (
            ("ctrl+v", ["29:1", "47:1", "47:0", "29:0"]),
            ("ctrl+shift+v", ["29:1", "42:1", "47:1", "47:0", "42:0", "29:0"]),
        ):
            with self.subTest(shortcut=shortcut), patch.object(core.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, stdout=MODERN_HELP)
                core.type_text(TEXT, shortcut, insertion_mode="paste")
                self.assertEqual(run.call_count, 3)
                copy = run.call_args_list[1]
                self.assertEqual(copy.args[0], ["wl-copy", "--type", "text/plain;charset=utf-8"])
                self.assertEqual(copy.kwargs["input"].decode("utf-8"), TEXT)
                self.assertTrue(copy.kwargs["check"])
                self.assertEqual(run.call_args_list[2].args[0], ["ydotool", "key", "--key-delay", "20", *events])
                self.assertFalse(any("type" in call.args[0] for call in run.call_args_list))
                self.notify.assert_not_called()

    def test_legacy_ydotool_uses_symbolic_shortcut(self):
        with patch.object(core.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout=LEGACY_HELP)
            core.type_text(TEXT, "ctrl+shift+v", insertion_mode="paste")
            self.assertEqual(run.call_args.args[0], ["ydotool", "key", "--key-delay", "20", "ctrl+shift+v"])

    def test_missing_daemon_does_not_change_clipboard_or_type_raw_text(self):
        with patch.object(core.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 2, stdout=b"failed to connect socket")
            core.type_text(TEXT)
            self.assertEqual(run.call_count, 1)
            self.assertIn("ydotool unavailable", self.notify.call_args.args[0])

    def test_missing_ydotool_does_not_change_clipboard(self):
        with patch.object(core.subprocess, "run", side_effect=FileNotFoundError(2, "missing", "ydotool")) as run:
            core.type_text(TEXT)
            self.assertEqual(run.call_count, 1)
            self.assertIn("install ydotool", self.notify.call_args.args[0])

    def test_failed_clipboard_copy_never_pastes_old_clipboard(self):
        for error in (FileNotFoundError(2, "missing", "wl-copy"),
                      subprocess.CalledProcessError(1, ["wl-copy"]),
                      subprocess.TimeoutExpired(["wl-copy"], 10)):
            with self.subTest(error=type(error).__name__), patch.object(core.subprocess, "run") as run:
                run.side_effect = [subprocess.CompletedProcess([], 0, stdout=MODERN_HELP), error]
                core.type_text(TEXT, insertion_mode="paste")
                self.assertEqual(run.call_count, 2)
                self.notify.assert_called()
                self.notify.reset_mock()

    def test_failed_paste_reports_error_without_raw_typing_fallback(self):
        with patch.object(core.subprocess, "run") as run:
            run.side_effect = [subprocess.CompletedProcess([], 0, stdout=MODERN_HELP),
                               subprocess.CompletedProcess([], 0),
                               subprocess.CalledProcessError(2, ["ydotool", "key"])]
            core.type_text(TEXT, insertion_mode="paste")
            self.assertEqual(run.call_count, 3)
            self.assertIn("ydotool exit 2", self.notify.call_args.args[0])

    def test_invalid_shortcut_does_not_touch_clipboard(self):
        with patch.object(core.subprocess, "run") as run:
            core.type_text(TEXT, "invalid", insertion_mode="paste")
            run.assert_not_called()
            self.notify.assert_called_once()

    def test_x11_uses_layout_aware_xdotool(self):
        with patch.dict(os.environ, {"XDG_SESSION_TYPE": "x11"}), patch.object(core.subprocess, "run") as run:
            core.type_text(TEXT)
            self.assertEqual(run.call_args.args[0], ["xdotool", "type", "--delay", "0", "--", TEXT])
            self.notify.assert_not_called()

    def test_x11_timeout_does_not_log_transcription(self):
        error = subprocess.TimeoutExpired(["xdotool", "type", TEXT], 30)
        with patch.dict(os.environ, {"XDG_SESSION_TYPE": "x11"}), \
                patch.object(core.subprocess, "run", side_effect=error), patch.object(core.log, "error") as log:
            core.type_text(TEXT)
            log.assert_called_once_with("%s", "Typing timed out")
            self.assertEqual(self.notify.call_args.args[0], "Typing timed out")

    def test_wayland_display_detects_session_without_session_type(self):
        with patch.dict(os.environ, {"XDG_SESSION_TYPE": ""}), patch.object(core, "_type_wayland") as typing:
            core.type_text(TEXT)
            typing.assert_called_once_with(TEXT, "auto")

    def test_default_types_characters_with_no_clipboard_or_paste_shortcut(self):
        with patch.object(core.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout=MODERN_HELP)
            core.type_text("yzäöüß?", typing_layout="at")
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args.args[0], [
                "ydotool", "key", "--key-delay", "2", "--",
                "44:1", "44:0", "21:1", "21:0", "40:1", "40:0",
                "39:1", "39:0", "26:1", "26:0", "12:1", "12:0",
                "42:1", "12:1", "12:0", "42:0",
            ])
            self.notify.assert_not_called()

    def test_default_does_not_paste_unsupported_characters(self):
        with patch.object(core.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout=MODERN_HELP)
            core.type_text("Hello 😃", typing_layout="at")
            self.assertEqual(run.call_count, 1)
            self.assertIn("unavailable in this keyboard layout", self.notify.call_args.args[0])

    def test_legacy_types_layout_aware_physical_key_names(self):
        with patch.object(core.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout=LEGACY_HELP)
            core.type_text("yzäÜ@", typing_layout="at")
            self.assertEqual(run.call_args.args[0], ["ydotool", "key", "--key-delay", "2", "--",
                                                    "z", "y", "'", "Shift+[", "Alt_R+q"])

    def test_long_dictation_is_sent_in_complete_bounded_key_sequences(self):
        with patch.object(core.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout=MODERN_HELP)
            core.type_text("Ü" * 300, typing_layout="at")
            self.assertEqual(run.call_count, 4)
            for call in run.call_args_list[1:]:
                self.assertEqual(call.args[0][-4:], ["42:1", "26:1", "26:0", "42:0"])
                self.assertLessEqual(len(call.args[0]), 6 + 128 * 4)

    def test_windows_and_macos_keep_native_typing(self):
        for platform in ("win32", "darwin"):
            with self.subTest(platform=platform), patch.object(core.sys, "platform", platform), \
                    patch.object(os, "name", "nt" if platform == "win32" else "posix"), \
                    patch.object(core.keyboard, "Controller") as controller, patch.object(core.subprocess, "run") as run:
                core.type_text(TEXT)
                controller.return_value.type.assert_called_once_with(TEXT)
                run.assert_not_called()

    def test_empty_text_does_nothing(self):
        with patch.object(core.subprocess, "run") as run:
            core.type_text("")
            run.assert_not_called()
            self.notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
