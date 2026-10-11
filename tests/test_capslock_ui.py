"""Caps Lock checkbox in Settings, and applying it when the listener starts."""
import os
import threading
import time

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REAL_THREAD = threading.Thread    # kept before the fixtures replace it


class InlineThread:
    """Runs its target at once, so a test sees the result right after the call."""

    def __init__(self, target=None, args=(), daemon=None, **kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


@pytest.fixture(autouse=True)
def never_touch_the_real_marker(monkeypatch, tmp_path):
    import capslock
    monkeypatch.setattr(capslock, "MARKER", tmp_path / "state" / "capslock-managed")


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def gui_module(qapp):
    try:
        import gui
    except Exception as e:   # no X display for pynput, no audio library, ...
        pytest.skip(f"gui cannot be imported here: {e}")
    return gui


# --- the checkbox -----------------------------------------------------------------

@pytest.fixture
def make_dialog(gui_module, monkeypatch):
    monkeypatch.setattr(gui_module.capslock, "is_available", lambda *a, **k: True)

    def make(**overrides):
        cfg = dict(gui_module.core.DEFAULT_CONFIG, provider="parakeet_local", **overrides)
        return gui_module.SettingsDialog(cfg)
    return make


def test_checkbox_is_hidden_for_other_hotkeys(make_dialog):
    dialog = make_dialog(key="f9")

    assert dialog.capslock_box.isHidden()


def test_checkbox_is_shown_and_checked_by_default_for_caps_lock(make_dialog):
    dialog = make_dialog(key="caps_lock")

    assert not dialog.capslock_box.isHidden()
    assert dialog.capslock_box.isChecked()
    assert dialog.values()["disable_capslock"] is True


def test_checkbox_follows_the_selected_hotkey(make_dialog):
    dialog = make_dialog(key="f9")

    dialog.key_box.setCurrentIndex(dialog.key_box.findData("caps_lock"))
    assert not dialog.capslock_box.isHidden()

    dialog.key_box.setCurrentIndex(dialog.key_box.findData("pause"))
    assert dialog.capslock_box.isHidden()


def test_unchecked_choice_is_kept(make_dialog):
    dialog = make_dialog(key="caps_lock", disable_capslock=False)

    assert not dialog.capslock_box.isChecked()
    assert dialog.values()["disable_capslock"] is False


def test_checkbox_sits_directly_below_the_hotkey_row(make_dialog):
    dialog = make_dialog(key="caps_lock")

    hotkey_row, _ = dialog.form.getWidgetPosition(dialog.key_box)
    checkbox_row, _ = dialog.form.getWidgetPosition(dialog.capslock_box)

    assert checkbox_row == hotkey_row + 1


def test_checkbox_is_disabled_without_gnome(gui_module, monkeypatch):
    monkeypatch.setattr(gui_module.capslock, "is_available", lambda *a, **k: False)
    cfg = dict(gui_module.core.DEFAULT_CONFIG, key="caps_lock")

    dialog = gui_module.SettingsDialog(cfg)

    assert not dialog.capslock_box.isEnabled()
    assert "GNOME" in dialog.capslock_box.toolTip()


# --- applying it when the listener starts -----------------------------------------

class Recorder:
    """Replaces capslock.apply and core.notify/save_config; records the calls."""

    def __init__(self, result=(False, "")):
        self.result = result
        self.applied, self.notified, self.saved, self.backends = [], [], [], []

    def apply(self, wanted, managed, backend=None):
        self.applied.append((wanted, managed))
        self.backends.append(backend)
        return self.result

    def notify(self, msg, urgency="normal", timeout_ms=1500):
        self.notified.append(msg)

    def save_config(self, cfg):
        self.saved.append(dict(cfg))


@pytest.fixture
def make_window(gui_module, monkeypatch):
    evdev_listener = pytest.importorskip("evdev_listener")

    class FakeEvdev(evdev_listener.Listener):
        def stop(self):
            pass

    class FakePynput:
        def stop(self):
            pass

    windows, threads = [], []

    class Recording(REAL_THREAD):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            threads.append(self)

    def make(key, evdev, disable=True, managed=False, result=(False, ""), inline=True):
        """inline=True runs worker threads at once (deterministic); False uses real ones."""
        cfg = dict(gui_module.core.DEFAULT_CONFIG, key=key, disable_capslock=disable,
                   capslock_managed=managed)
        rec = Recorder(result)
        monkeypatch.setattr(gui_module.core, "load_config", lambda: dict(cfg))
        monkeypatch.setattr(gui_module.core, "save_config", rec.save_config)
        monkeypatch.setattr(gui_module.core, "notify", rec.notify)
        monkeypatch.setattr(gui_module.capslock, "apply", rec.apply)
        monkeypatch.setattr(gui_module.threading, "Thread", InlineThread if inline else Recording)
        monkeypatch.setattr(gui_module, "_make_listener",
                            lambda on_press, on_release: FakeEvdev() if evdev else FakePynput())
        window = gui_module.MainWindow()
        windows.append(window)
        return window, rec

    make.threads = threads
    yield make

    # A window that outlives its test must not fire its quit handler against the real desktop.
    from PyQt6.QtWidgets import QApplication
    for window in windows:
        try:
            QApplication.instance().aboutToQuit.disconnect(window._release_capslock)
        except TypeError:
            pass


def test_caps_lock_with_evdev_switches_the_function_off_and_remembers_it(make_window):
    window, rec = make_window("caps_lock", evdev=True, result=(True, ""))

    assert rec.applied == [(True, False)]
    assert rec.saved[-1]["capslock_managed"] is True
    assert rec.notified == []
    window.close()


def test_without_evdev_the_function_is_kept_and_the_user_is_told(make_window):
    window, rec = make_window("caps_lock", evdev=False)

    assert rec.applied == [(False, False)]           # hotkey would die with caps:none under pynput
    assert rec.saved == []
    assert len(rec.notified) == 1 and "log out and in" in rec.notified[0]
    window.close()


def test_unticked_box_leaves_caps_lock_alone(make_window):
    window, rec = make_window("caps_lock", evdev=True, disable=False)

    assert rec.applied == [(False, False)]
    assert rec.notified == []
    window.close()


def test_choosing_another_hotkey_gives_caps_lock_back(make_window):
    window, rec = make_window("f9", evdev=True, managed=True, result=(False, ""))

    assert rec.applied == [(False, True)]
    assert rec.saved[-1]["capslock_managed"] is False
    window.close()


def test_closing_the_app_gives_caps_lock_back(make_window):
    window, rec = make_window("caps_lock", evdev=True, managed=True, result=(True, ""))
    rec.applied.clear()
    rec.result = (False, "")      # what removing the option returns

    window._release_capslock()

    assert rec.applied == [(False, True)]
    assert rec.saved[-1]["capslock_managed"] is False
    window.close()


def test_closing_the_app_leaves_a_users_own_setting_alone(make_window):
    window, rec = make_window("caps_lock", evdev=True, managed=False, result=(False, ""))
    rec.applied.clear()
    rec.saved.clear()

    window._release_capslock()

    assert rec.applied == [(False, False)]        # apply() itself ignores what is not ours
    assert rec.saved == []
    window.close()


def test_a_failed_release_is_logged_and_ownership_is_kept(make_window, caplog):
    window, rec = make_window("caps_lock", evdev=True, managed=True, result=(True, "no dbus"))
    rec.saved.clear()

    with caplog.at_level("WARNING", logger="dictate"):
        window._release_capslock()

    assert rec.saved == []                         # still ours, so the next start can retry
    assert "no dbus" in caplog.text
    window.close()


def test_the_quit_signal_is_connected_to_the_release(gui_module, make_window, qapp):
    before = qapp.receivers(qapp.aboutToQuit)

    window, _ = make_window("f9", evdev=True)

    assert qapp.receivers(qapp.aboutToQuit) == before + 1
    window.close()


def test_failure_message_reaches_the_user(make_window):
    window, rec = make_window("caps_lock", evdev=True, result=(False, "Could not change the Caps Lock setting: no dbus"))

    assert rec.notified == ["Could not change the Caps Lock setting: no dbus"]
    window.close()


# --- the desktop calls stay off the GUI thread -------------------------------------------------

def settle(qapp, threads):
    """Wait for the worker threads and deliver their signals to the GUI thread."""
    for _ in range(3):
        for thread in list(threads):
            thread.join(5)
        qapp.processEvents()


def test_a_hung_desktop_call_does_not_freeze_the_gui_thread(make_window, gui_module, qapp, monkeypatch):
    window, rec = make_window("caps_lock", evdev=True, inline=False)
    settle(qapp, make_window.threads)
    rec.saved.clear()
    entered, release = threading.Event(), threading.Event()

    def hung_apply(wanted, managed, backend=None):
        entered.set()
        assert release.wait(5), "the test never released the worker"
        return True, ""

    monkeypatch.setattr(gui_module.capslock, "apply", hung_apply)

    started = time.monotonic()
    window._apply_capslock_setting()
    took = time.monotonic() - started

    assert took < 0.5                    # the GUI thread came back at once ...
    assert entered.wait(5)               # ... while the worker really sits in the hung call
    assert rec.saved == []
    release.set()
    settle(qapp, make_window.threads)
    assert rec.saved[-1]["capslock_managed"] is True      # the result arrives on the GUI thread
    window.close()


def test_two_desktop_changes_never_run_at_the_same_time(make_window, gui_module, qapp, monkeypatch):
    window, rec = make_window("caps_lock", evdev=True, inline=False)
    settle(qapp, make_window.threads)
    gate, first_in = threading.Event(), threading.Event()
    running, peak, guard = [], [0], threading.Lock()

    def slow_apply(wanted, managed, backend=None):
        with guard:
            running.append(1)
            peak[0] = max(peak[0], len(running))
        first_in.set()
        gate.wait(5)
        with guard:
            running.pop()
        return False, ""

    monkeypatch.setattr(gui_module.capslock, "apply", slow_apply)

    window._apply_capslock_setting()
    assert first_in.wait(5)
    window._apply_capslock_setting()     # for example Settings saved while the first change runs
    time.sleep(0.2)
    assert len(running) == 1             # the second one waits for the first
    gate.set()
    settle(qapp, make_window.threads)

    assert peak[0] == 1
    window.close()


def test_closing_uses_short_timeouts(make_window, gui_module):
    window, rec = make_window("caps_lock", evdev=True, managed=True)
    rec.backends.clear()

    window._release_capslock()

    backend = rec.backends[-1]
    assert isinstance(backend, gui_module.capslock.Gsettings)
    assert backend.timeout == gui_module.capslock.QUIT_TIMEOUT_S
    window.close()


def test_closing_does_not_wait_long_for_a_busy_worker(make_window, gui_module, monkeypatch):
    monkeypatch.setattr(gui_module.capslock, "QUIT_LOCK_WAIT_S", 0.1)
    window, rec = make_window("caps_lock", evdev=True, managed=True)
    rec.applied.clear()
    window._capslock_lock.acquire()      # a worker is busy
    try:
        started = time.monotonic()
        window._release_capslock()
        took = time.monotonic() - started
    finally:
        window._capslock_lock.release()

    assert took < 1.0
    assert rec.applied == [(False, True)]          # the function is given back anyway
    window.close()


# --- the "Last transcription" area follows the window ------------------------------------------

def test_last_transcription_area_grows_with_the_window_in_both_directions(make_window, gui_module, qapp):
    window, _ = make_window("f9", evdev=True)
    window.show()
    qapp.processEvents()
    before = window.last.size()

    window.resize(window.width() + 200, window.height() + 150)
    qapp.processEvents()

    assert window.last.width() >= before.width() + 150
    assert window.last.height() >= before.height() + 100
    window.close()


def test_last_transcription_area_has_a_minimum_size(make_window, gui_module):
    window, _ = make_window("f9", evdev=True)

    assert window.last.minimumWidth() == gui_module.LAST_MIN_WIDTH
    assert window.last.minimumHeight() == gui_module.LAST_MIN_HEIGHT
    assert window.last.maximumHeight() > gui_module.LAST_MIN_HEIGHT     # no fixed cap any more
    window.close()


def test_an_unexpected_error_in_the_worker_is_reported(make_window, gui_module, monkeypatch):
    window, rec = make_window("caps_lock", evdev=True)
    rec.notified.clear()

    def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(gui_module.capslock, "apply", boom)

    window._apply_capslock_setting()

    assert len(rec.notified) == 1 and "kaboom" in rec.notified[0]
    window.close()
