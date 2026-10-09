"""Pasting instead of typing, with a fake clipboard, window and key press."""
import json
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

import core

TERMINAL = "gnome-terminal-server Gnome-terminal"
BROWSER = "google-chrome Google-chrome"


class FakeX:
    """Stands in for xdotool, xprop and the clipboard tool, and records what happens."""

    def __init__(self, monkeypatch, window=TERMINAL, clipboard=b"old text", key_rc=0,
                 tools=("xsel", "xdotool", "xprop"), write_fails=False, key_raises=None):
        self.clipboard = clipboard           # None: empty or not text
        self.window = window
        self.key_rc = key_rc
        self.key_raises = key_raises         # exception the key press raises, e.g. TimeoutExpired
        self.write_fails = write_fails
        self.events = []
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setattr(core.shutil, "which", lambda name: f"/usr/bin/{name}" if name in tools else None)
        monkeypatch.setattr(core.subprocess, "run", self.run)
        monkeypatch.setattr(core.time, "sleep", lambda s: self.events.append("wait"))
        self.notes = []
        monkeypatch.setattr(core, "notify", lambda msg, **kw: self.notes.append(msg))

    def run(self, cmd, **kwargs):
        name = cmd[0]
        if name == "xdotool" and cmd[1] == "getactivewindow":
            return SimpleNamespace(stdout="42\n", returncode=0)
        if name == "xprop":
            names = ", ".join(f'"{n}"' for n in self.window.split())
            return SimpleNamespace(stdout=f"WM_CLASS(STRING) = {names}\n", returncode=0)
        if name in ("xsel", "xclip"):
            if "--output" in cmd or "-o" in cmd:
                self.events.append("read")
                if self.clipboard is None:
                    return SimpleNamespace(stdout=b"", returncode=1)
                return SimpleNamespace(stdout=self.clipboard, returncode=0)
            if self.write_fails:
                raise subprocess.CalledProcessError(1, cmd)
            self.clipboard = kwargs["input"]
            self.events.append(("write", kwargs["input"]))
            return SimpleNamespace(returncode=0)
        if name == "xdotool" and cmd[1] == "key":
            self.events.append(("key", cmd[-1]))
            if self.key_raises:
                raise self.key_raises
            return SimpleNamespace(returncode=self.key_rc)
        raise AssertionError(f"unexpected command {cmd}")


# --- which shortcut ---------------------------------------------------------------

@pytest.mark.parametrize("window_class, shortcut", [
    ("gnome-terminal-server Gnome-terminal", "ctrl+shift+v"),
    ("kitty kitty", "ctrl+shift+v"),
    ("Alacritty Alacritty", "ctrl+shift+v"),
    ("konsole konsole", "ctrl+shift+v"),
    ("kgx org.gnome.Console", "ctrl+shift+v"),
    ("xterm XTerm", "shift+Insert"),
    ("urxvt URxvt", "shift+Insert"),
    ("st-256color st-256color", "shift+Insert"),
    ("google-chrome Google-chrome", "ctrl+v"),
    ("Navigator firefox", "ctrl+v"),
    ("libreoffice-writer libreoffice-writer", "ctrl+v"),
    ("", "ctrl+v"),
])
def test_paste_shortcut_by_window_class(window_class, shortcut):
    assert core.paste_shortcut(window_class) == shortcut


def test_the_window_class_is_read_from_the_focused_window(monkeypatch):
    FakeX(monkeypatch, window=BROWSER)

    assert core._active_window_class() == "google-chrome Google-chrome"


def test_an_unknown_window_class_means_ctrl_v(monkeypatch):
    def broken(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(core.subprocess, "run", broken)

    assert core._active_window_class() == ""
    assert core.paste_shortcut(core._active_window_class()) == "ctrl+v"


# --- pasting ------------------------------------------------------------------------------

def test_paste_puts_text_on_the_clipboard_presses_the_shortcut_and_restores_the_clipboard(monkeypatch):
    x = FakeX(monkeypatch)

    assert core.paste_text("hallo welt") is True

    assert x.events == ["read", ("write", b"hallo welt"), ("key", "ctrl+shift+v"), "wait",
                        ("write", b"old text")]
    assert x.clipboard == b"old text"


def test_the_shortcut_follows_the_focused_window(monkeypatch):
    x = FakeX(monkeypatch, window=BROWSER)

    core.paste_text("x")

    assert ("key", "ctrl+v") in x.events


def test_umlauts_survive_the_clipboard(monkeypatch):
    x = FakeX(monkeypatch)

    core.paste_text("Größe über Äpfel")

    assert ("write", "Größe über Äpfel".encode()) in x.events


def test_a_clipboard_that_held_no_text_cannot_be_restored_and_keeps_the_dictated_text(monkeypatch):
    x = FakeX(monkeypatch, clipboard=None)

    assert core.paste_text("hallo") is True

    assert x.clipboard == b"hallo"
    assert [e for e in x.events if isinstance(e, tuple) and e[0] == "write"] == [("write", b"hallo")]


def test_a_failed_key_press_returns_false_and_still_restores_the_clipboard(monkeypatch):
    x = FakeX(monkeypatch, key_rc=1)

    assert core.paste_text("hallo") is False

    assert x.clipboard == b"old text"
    assert x.notes and "Pasting failed" in x.notes[0]


@pytest.mark.parametrize("error", [
    subprocess.TimeoutExpired("xdotool", 5),
    PermissionError("xdotool is not executable"),
    subprocess.SubprocessError("tool crashed"),
])
def test_a_key_press_that_raises_is_reported_and_the_clipboard_is_still_restored(monkeypatch, error):
    x = FakeX(monkeypatch, key_raises=error)

    assert core.paste_text("hallo") is False                # no exception escapes

    assert x.clipboard == b"old text"
    assert x.notes and "Pasting failed" in x.notes[0]


def test_a_raising_key_press_is_logged(monkeypatch, caplog):
    FakeX(monkeypatch, key_raises=subprocess.TimeoutExpired("xdotool", 5))

    with caplog.at_level("ERROR", logger="dictate"):
        core.paste_text("hallo")

    assert "paste key press failed" in caplog.text


def test_empty_text_needs_no_clipboard_and_counts_as_done(monkeypatch):
    x = FakeX(monkeypatch)

    assert core.paste_text("") is True
    assert x.events == []


def test_xclip_is_preferred_when_both_clipboard_tools_exist(monkeypatch):
    seen = []
    FakeX(monkeypatch, tools=("xclip", "xsel", "xdotool", "xprop"))
    original = core.subprocess.run

    def spy(cmd, **kwargs):
        seen.append(cmd[0])
        return original(cmd, **kwargs)

    monkeypatch.setattr(core.subprocess, "run", spy)

    core.paste_text("x")

    assert "xclip" in seen and "xsel" not in seen


@pytest.mark.parametrize("session, tools, reason", [
    ("wayland", ("xsel", "xdotool", "xprop"), "X11"),
    ("x11", ("xsel", "xprop"), "xdotool"),
    ("x11", ("xdotool", "xprop"), "clipboard tool"),
])
def test_pasting_is_unavailable_where_it_cannot_work(monkeypatch, session, tools, reason):
    x = FakeX(monkeypatch, tools=tools)
    monkeypatch.setenv("XDG_SESSION_TYPE", session)

    with pytest.raises(core.PasteUnavailable, match=reason):
        core.paste_text("hallo")

    assert x.events == []


def test_a_clipboard_that_cannot_be_written_inserts_nothing(monkeypatch):
    x = FakeX(monkeypatch, write_fails=True)

    with pytest.raises(core.PasteUnavailable, match="clipboard"):
        core.paste_text("hallo")

    assert not any(isinstance(e, tuple) and e[0] == "key" for e in x.events)


# --- choosing between paste and type ---------------------------------------------------------

@pytest.fixture
def methods(monkeypatch):
    """Records which way output_text goes."""
    calls = SimpleNamespace(paste=[], type=[], notes=[], paste_result=True, unavailable=False, type_result=True)

    def paste(text):
        calls.paste.append(text)
        if calls.unavailable:
            raise core.PasteUnavailable("test: no X11")
        return calls.paste_result

    def type_(text):
        calls.type.append(text)
        return calls.type_result

    monkeypatch.setattr(core, "paste_text", paste)
    monkeypatch.setattr(core, "type_text", type_)
    monkeypatch.setattr(core, "notify", lambda msg, **kw: calls.notes.append(msg))
    monkeypatch.setattr(core, "_paste_fallback_told", False)
    return calls


def test_paste_is_used_when_chosen(methods):
    assert core.output_text("hallo", "paste") is True
    assert methods.paste == ["hallo"] and methods.type == []


def test_typing_is_used_when_chosen(methods):
    assert core.output_text("hallo", "type") is True
    assert methods.type == ["hallo"] and methods.paste == []


def test_paste_is_the_default_method(methods):
    core.output_text("hallo")

    assert methods.paste == ["hallo"]


def test_an_unknown_method_value_pastes(methods):
    core.output_text("hallo", "something")

    assert methods.paste == ["hallo"]


def test_pasting_that_cannot_work_falls_back_to_typing_and_tells_the_user_once(methods):
    methods.unavailable = True

    assert core.output_text("eins", "paste") is True
    assert core.output_text("zwei", "paste") is True

    assert methods.type == ["eins", "zwei"]
    assert len(methods.notes) == 1 and "typing the text instead" in methods.notes[0]


def test_a_failed_paste_is_reported_and_not_typed_on_top(methods):
    methods.paste_result = False

    assert core.output_text("hallo", "paste") is False
    assert methods.type == []


def test_a_failed_typing_is_reported(methods):
    methods.type_result = False

    assert core.output_text("hallo", "type") is False


def test_how_long_the_output_took_is_logged(methods, caplog):
    with caplog.at_level("INFO", logger="dictate"):
        core.output_text("hallo", "paste")
        core.output_text("hallo", "type")

    assert "output via paste: 5 chars in" in caplog.text
    assert "output via type: 5 chars in" in caplog.text


# --- one dictation at a time (real threads) ----------------------------------------------------------

def run_in_threads(targets, join_timeout=5):
    """Start each callable in its own real thread, in order; return the exceptions they raised."""
    errors = []

    def wrap(fn):
        def run():
            try:
                fn()
            except Exception as e:      # collected, so a test can look at it
                errors.append(e)
        return run

    threads = [threading.Thread(target=wrap(fn)) for fn in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(join_timeout)
    assert not any(t.is_alive() for t in threads), "a thread is stuck: the lock was never released"
    return errors


@pytest.fixture
def slow_output(monkeypatch):
    """paste/type fakes that take a moment and record when they start and end."""
    log = []

    def make(name, result=True, boom=None, pause=0.05):
        def fake(text):
            log.append((name, "start", text))
            time.sleep(pause)
            try:
                if boom:
                    raise boom
                return result
            finally:
                log.append((name, "end", text))
        return fake

    monkeypatch.setattr(core, "paste_text", make("paste"))
    monkeypatch.setattr(core, "type_text", make("type"))
    monkeypatch.setattr(core, "notify", lambda *a, **k: None)
    return SimpleNamespace(log=log, make=make)


def test_two_dictations_at_once_are_inserted_one_after_the_other(slow_output):
    errors = run_in_threads([lambda: core.output_text("eins", "paste"),
                             lambda: core.output_text("zwei", "type")])

    assert errors == []
    states = [state for _, state, _ in slow_output.log]
    assert states == ["start", "end", "start", "end"]            # never start, start, end, end
    assert sorted(text for _, state, text in slow_output.log if state == "start") == ["eins", "zwei"]


def test_many_dictations_never_overlap(slow_output):
    errors = run_in_threads([lambda i=i: core.output_text(f"text {i}", "paste") for i in range(6)])

    assert errors == []
    states = [state for _, state, _ in slow_output.log]
    assert states == ["start", "end"] * 6


def test_dictations_are_inserted_in_the_order_they_arrived(slow_output, monkeypatch):
    first_may_end = threading.Event()
    inserted = []

    def paste(text):
        inserted.append(text)
        if text == "text 0":
            assert first_may_end.wait(5)
        return True

    monkeypatch.setattr(core, "paste_text", paste)
    threads = []

    def call(i):
        core.output_text(f"text {i}", "paste")

    for i in range(5):
        t = threading.Thread(target=call, args=(i,))
        t.start()
        threads.append(t)
        # wait until this caller has taken its ticket, so the arrival order is certain
        deadline = time.monotonic() + 5
        while core._output_lock._next_ticket < i + 1 and time.monotonic() < deadline:
            time.sleep(0.001)
    first_may_end.set()
    for t in threads:
        t.join(5)

    assert inserted == [f"text {i}" for i in range(5)]


def test_a_failing_dictation_does_not_block_the_next_one(slow_output, monkeypatch):
    monkeypatch.setattr(core, "paste_text", slow_output.make("paste", boom=RuntimeError("clipboard exploded")))

    errors = run_in_threads([lambda: core.output_text("kaputt", "paste")])
    monkeypatch.setattr(core, "paste_text", slow_output.make("paste"))
    second = run_in_threads([lambda: core.output_text("danach", "paste")])

    assert [str(e) for e in errors] == ["clipboard exploded"]
    assert second == []
    assert core.output_text("und noch einmal", "paste") is True      # also from the calling thread


def test_a_failed_paste_result_releases_the_lock_too(slow_output, monkeypatch):
    monkeypatch.setattr(core, "paste_text", slow_output.make("paste", result=False))

    assert core.output_text("a", "paste") is False
    assert core.output_text("b", "paste") is False


def test_overlapping_real_pastes_keep_the_clipboard_sequence_intact(monkeypatch):
    """The restore of one paste must finish before the next paste reads the clipboard."""
    x = FakeX(monkeypatch)
    monkeypatch.setattr(core.time, "sleep", lambda s: (x.events.append("wait"), time.sleep(0.05))[0])

    errors = run_in_threads([lambda: core.output_text("eins", "paste"),
                             lambda: core.output_text("zwei", "paste")])

    kinds = [e if isinstance(e, str) else e[0] for e in x.events]
    assert errors == []
    assert kinds == ["read", "write", "key", "wait", "write"] * 2
    assert x.clipboard == b"old text"                  # the user's text, not a dictation


# --- config ------------------------------------------------------------------------------------

def test_paste_is_the_default_in_the_config():
    assert core.DEFAULT_CONFIG["output_method"] == "paste"


def test_an_old_config_without_the_setting_gets_paste(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"key": "f9"}))
    monkeypatch.setattr(core, "CONFIG_PATH", path)

    assert core.load_config()["output_method"] == "paste"


def test_a_saved_choice_of_typing_is_kept(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"output_method": "type"}))
    monkeypatch.setattr(core, "CONFIG_PATH", path)

    assert core.load_config()["output_method"] == "type"
