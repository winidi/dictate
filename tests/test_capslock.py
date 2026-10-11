"""Switching the Caps Lock function off in the desktop, without touching a real desktop."""
import stat
import subprocess
from types import SimpleNamespace

import pytest

import capslock

REAL_STATE = capslock.CapsLockState   # kept for the tests of the class itself


class FakeCaps:
    """Caps Lock state without a keyboard; remembers the order of events."""

    def __init__(self, on=False, can_turn_off=True, events=None):
        self.on = on
        self.can_turn_off = can_turn_off
        self.events = events if events is not None else []

    def turn_off(self):
        self.events.append("turn_off")
        if self.on and self.can_turn_off:
            self.on = False
        return not self.on


@pytest.fixture(autouse=True)
def never_touch_the_real_keyboard(monkeypatch, tmp_path):
    """A test that forgets to pass a fake gets one that is off, not the real LED and key,
    and a marker file in a temporary folder, not the real one."""
    monkeypatch.setattr(capslock, "CapsLockState", FakeCaps)
    monkeypatch.setattr(capslock, "MARKER", tmp_path / "state" / "capslock-managed")


class FakeBackend:
    """Stands in for gsettings; remembers every write."""

    def __init__(self, options=(), fail=None, events=None):
        self.options = list(options)
        self.fail = fail
        self.writes = []
        self.events = events

    def get(self):
        if self.fail:
            raise self.fail
        return list(self.options)

    def set(self, options):
        if self.fail:
            raise self.fail
        self.options = list(options)
        self.writes.append(list(options))
        if self.events is not None:
            self.events.append("set")


# --- reading and writing the GVariant text -----------------------------------

@pytest.mark.parametrize("text, options", [
    ("@as []\n", []),
    ("[]", []),
    ("['caps:none']\n", ["caps:none"]),
    ("['grp:alt_shift_toggle', 'caps:none']", ["grp:alt_shift_toggle", "caps:none"]),
])
def test_parse_options(text, options):
    assert capslock.parse_options(text) == options


def test_format_options_round_trips():
    options = ["grp:alt_shift_toggle", "caps:none"]

    assert capslock.format_options(options) == "['grp:alt_shift_toggle', 'caps:none']"
    assert capslock.parse_options(capslock.format_options(options)) == options
    assert capslock.format_options([]) == "[]"


# --- apply ---------------------------------------------------------------------

def test_wanted_adds_the_option_and_takes_ownership():
    backend = FakeBackend(["grp:alt_shift_toggle"])

    managed, message = capslock.apply(True, False, backend)

    assert backend.options == ["grp:alt_shift_toggle", "caps:none"]
    assert (managed, message) == (True, "")


def test_wanted_leaves_a_users_own_setting_alone():
    backend = FakeBackend(["caps:none"])

    managed, message = capslock.apply(True, False, backend)

    assert backend.writes == []
    assert (managed, message) == (False, "")


def test_wanted_again_keeps_ownership_without_writing():
    backend = FakeBackend(["caps:none"])

    managed, _ = capslock.apply(True, True, backend)

    assert backend.writes == [] and managed is True


def test_not_wanted_removes_only_what_the_app_added():
    backend = FakeBackend(["grp:alt_shift_toggle", "caps:none"])

    managed, message = capslock.apply(False, True, backend)

    assert backend.options == ["grp:alt_shift_toggle"]
    assert (managed, message) == (False, "")


def test_not_wanted_never_removes_a_users_own_setting():
    backend = FakeBackend(["caps:none"])

    managed, message = capslock.apply(False, False, backend)

    assert backend.writes == [] and backend.options == ["caps:none"]
    assert (managed, message) == (False, "")


def test_not_wanted_and_not_managed_does_not_even_look_at_the_desktop():
    backend = FakeBackend(fail=RuntimeError("must not be called"))

    assert capslock.apply(False, False, backend) == (False, "")


def test_not_wanted_but_option_already_gone_just_forgets_ownership():
    backend = FakeBackend([])

    managed, _ = capslock.apply(False, True, backend)

    assert backend.writes == [] and managed is False


def test_failure_is_reported_and_ownership_stays_as_it_was():
    backend = FakeBackend(fail=OSError("no dbus"))

    managed, message = capslock.apply(True, False, backend)

    assert managed is False
    assert message == "Could not change the Caps Lock setting: no dbus"


def test_caps_lock_is_switched_off_before_its_function_is(monkeypatch):
    events = []
    caps = FakeCaps(on=True, events=events)
    backend = FakeBackend([], events=events)

    managed, message = capslock.apply(True, False, backend, caps)

    assert events == ["turn_off", "set"]            # state first, then the option
    assert caps.on is False and backend.options == ["caps:none"]
    assert (managed, message) == (True, "")


def test_nothing_is_changed_when_caps_lock_cannot_be_switched_off():
    caps = FakeCaps(on=True, can_turn_off=False)
    backend = FakeBackend(["grp:alt_shift_toggle"])

    managed, message = capslock.apply(True, False, backend, caps)

    assert backend.writes == []                     # the key must not end up frozen on
    assert managed is False
    assert "could not be switched off" in message


def test_caps_lock_state_is_left_alone_when_the_option_is_not_added():
    for wanted, managed, options in [(True, False, ["caps:none"]),     # user's own option
                                     (True, True, ["caps:none"]),      # already ours
                                     (False, True, ["caps:none"]),     # being removed
                                     (False, False, ["caps:none"])]:   # nothing to do
        caps = FakeCaps(on=True)
        capslock.apply(wanted, managed, FakeBackend(options), caps)
        assert caps.events == [], (wanted, managed)


# --- Caps Lock state ------------------------------------------------------------

@pytest.fixture
def led(tmp_path, monkeypatch):
    path = tmp_path / "input3::capslock" / "brightness"
    path.parent.mkdir()
    path.write_text("0\n")
    monkeypatch.setattr(capslock, "LED_GLOB", str(tmp_path / "*capslock*" / "brightness"))
    monkeypatch.setattr(capslock, "SETTLE_S", 0.2)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")    # only the LEDs answer, never xset
    return path


def test_state_is_read_from_the_keyboard_led(led):
    state = REAL_STATE()

    assert state.is_on() is False
    led.write_text("1\n")
    assert state.is_on() is True


def test_outside_x11_the_led_decides_and_xset_is_not_asked(led, monkeypatch):
    monkeypatch.setattr(capslock.subprocess, "run", lambda *a, **k: pytest.fail("asked xset"))

    assert REAL_STATE().is_on() is False


def fake_xset(monkeypatch, output="", error=None, xset_present=True):
    """An X11 session whose `xset q` answers `output` (or raises `error`)."""
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setattr(capslock.shutil, "which",
                        lambda name: f"/usr/bin/{name}" if name == "xset" and xset_present else None)

    def run(cmd, **kwargs):
        if error:
            raise error
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(capslock.subprocess, "run", run)


@pytest.mark.parametrize("output, expected", [
    ("00: Caps Lock:   on     01: Num Lock:    on", True),
    ("00: Caps Lock:   off    01: Num Lock:    on", False),
])
def test_on_x11_the_x_server_answers(monkeypatch, tmp_path, output, expected):
    monkeypatch.setattr(capslock, "LED_GLOB", str(tmp_path / "no-led-here"))
    fake_xset(monkeypatch, output)

    assert REAL_STATE().is_on() is expected


def test_on_x11_a_stale_led_does_not_make_caps_lock_look_on(led, monkeypatch):
    led.write_text("1\n")                              # some other device's LED, stuck on
    fake_xset(monkeypatch, "00: Caps Lock:   off    01: Num Lock:    on")

    assert REAL_STATE().is_on() is False


def test_on_x11_the_server_state_counts_even_when_the_led_is_dark(led, monkeypatch):
    led.write_text("0\n")
    fake_xset(monkeypatch, "00: Caps Lock:   on     01: Num Lock:    on")

    assert REAL_STATE().is_on() is True


@pytest.mark.parametrize("failure", [
    {"error": subprocess.TimeoutExpired("xset", 3)},
    {"error": OSError("no display")},
    {"output": "garbage without the expected line"},
    {"xset_present": False},
])
def test_on_x11_the_leds_answer_when_the_x_server_cannot(led, monkeypatch, failure):
    led.write_text("1\n")
    fake_xset(monkeypatch, **failure)

    assert REAL_STATE().is_on() is True


def test_state_is_off_when_it_cannot_be_determined(monkeypatch, tmp_path):
    monkeypatch.setattr(capslock, "LED_GLOB", str(tmp_path / "no-led-here"))
    monkeypatch.setattr(capslock.shutil, "which", lambda name: None)

    assert REAL_STATE().is_on() is False


def test_turn_off_does_nothing_when_already_off(led, monkeypatch):
    monkeypatch.setattr(capslock.subprocess, "run", lambda *a, **k: pytest.fail("pressed a key"))

    assert REAL_STATE().turn_off() is True


def test_turn_off_presses_the_key_and_checks_the_result(led, monkeypatch):
    led.write_text("1\n")
    pressed = []

    def press(cmd, **kwargs):
        pressed.append(cmd)
        led.write_text("0\n")

    monkeypatch.setattr(capslock.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(capslock.subprocess, "run", press)

    assert REAL_STATE().turn_off() is True
    assert pressed == [["xdotool", "key", "Caps_Lock"]]


def test_turn_off_tries_ydotool_when_xdotool_is_missing(led, monkeypatch):
    led.write_text("1\n")
    pressed = []

    def press(cmd, **kwargs):
        pressed.append(cmd[0])
        led.write_text("0\n")

    monkeypatch.setattr(capslock.shutil, "which", lambda name: "/usr/bin/ydotool" if name == "ydotool" else None)
    monkeypatch.setattr(capslock.subprocess, "run", press)

    assert REAL_STATE().turn_off() is True
    assert pressed == ["ydotool"]


def test_turn_off_reports_failure_when_the_state_does_not_change(led, monkeypatch):
    led.write_text("1\n")
    monkeypatch.setattr(capslock.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(capslock.subprocess, "run", lambda *a, **k: None)

    assert REAL_STATE().turn_off() is False


def test_turn_off_reports_failure_without_any_tool(led, monkeypatch):
    led.write_text("1\n")
    monkeypatch.setattr(capslock.shutil, "which", lambda name: None)

    assert REAL_STATE().turn_off() is False


def test_a_stale_led_alone_never_makes_the_app_press_the_key(led, monkeypatch):
    """The reported danger: a foreign LED reads on, the app presses Caps Lock and turns it ON."""
    led.write_text("1\n")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setattr(capslock.shutil, "which", lambda name: f"/usr/bin/{name}")

    def run(cmd, **kwargs):
        if cmd[0] == "xset":
            return SimpleNamespace(stdout="00: Caps Lock:   off    01: Num Lock:    on")
        pytest.fail(f"pressed a key: {cmd}")

    monkeypatch.setattr(capslock.subprocess, "run", run)

    assert REAL_STATE().turn_off() is True


def press_recorder(led, monkeypatch, effective):
    """xdotool/ydotool presses; only the tools named in `effective` switch the LED off."""
    led.write_text("1\n")
    pressed = []
    monkeypatch.setattr(capslock.shutil, "which", lambda name: f"/usr/bin/{name}")

    def press(cmd, **kwargs):
        pressed.append(cmd[0])
        if cmd[0] in effective:
            led.write_text("0\n")

    monkeypatch.setattr(capslock.subprocess, "run", press)
    return pressed


def test_turn_off_tries_the_next_tool_when_the_first_has_no_effect(led, monkeypatch):
    pressed = press_recorder(led, monkeypatch, effective={"ydotool"})

    assert REAL_STATE().turn_off() is True
    assert pressed == ["xdotool", "ydotool"]


def test_turn_off_does_not_press_a_second_tool_once_the_first_worked(led, monkeypatch):
    pressed = press_recorder(led, monkeypatch, effective={"xdotool", "ydotool"})

    assert REAL_STATE().turn_off() is True
    assert pressed == ["xdotool"]


def test_turn_off_reports_failure_when_no_tool_changes_the_state(led, monkeypatch):
    pressed = press_recorder(led, monkeypatch, effective=set())

    assert REAL_STATE().turn_off() is False
    assert pressed == ["xdotool", "ydotool"]


def test_a_key_press_that_times_out_is_a_failure_not_a_crash(led, monkeypatch):
    led.write_text("1\n")
    monkeypatch.setattr(capslock.shutil, "which", lambda name: f"/usr/bin/{name}")

    def hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 3)

    monkeypatch.setattr(capslock.subprocess, "run", hang)

    assert REAL_STATE().turn_off() is False


# --- who switched the function off: the marker file -----------------------------------

def test_the_marker_is_private(tmp_path):
    marker = tmp_path / "fresh" / "state" / "capslock-managed"

    capslock.write_marker(marker)

    assert marker.exists()
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert stat.S_IMODE(marker.parent.stat().st_mode) == 0o700


def test_removing_the_marker_twice_is_quiet(tmp_path):
    marker = tmp_path / "capslock-managed"
    capslock.write_marker(marker)

    capslock.remove_marker(marker)
    capslock.remove_marker(marker)

    assert not marker.exists()


def test_a_marker_that_cannot_be_written_is_not_an_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")

    capslock.write_marker(blocker / "sub" / "capslock-managed")      # parent is a file

    assert capslock.marker_exists(blocker / "sub" / "capslock-managed") is False


def test_switching_the_function_off_leaves_a_marker_and_giving_it_back_removes_it():
    backend = FakeBackend([])

    capslock.apply(True, False, backend, FakeCaps())
    assert capslock.marker_exists()

    capslock.apply(False, True, backend)
    assert not capslock.marker_exists()
    assert backend.options == []


def test_a_lost_config_does_not_make_the_app_forget_it_switched_the_function_off():
    """The config says 'not managed' (file lost) but the marker says otherwise."""
    backend = FakeBackend(["caps:none"])
    capslock.write_marker()

    managed, message = capslock.apply(False, False, backend)

    assert backend.options == []                  # the user gets Caps Lock back
    assert (managed, message) == (False, "")
    assert not capslock.marker_exists()


def test_a_lost_config_keeps_the_ownership_while_the_function_stays_off():
    backend = FakeBackend(["caps:none"])
    capslock.write_marker()

    managed, _ = capslock.apply(True, False, backend)

    assert backend.writes == [] and managed is True
    assert capslock.marker_exists()


def test_a_lost_marker_is_written_again_while_the_config_still_knows():
    backend = FakeBackend(["caps:none"])

    managed, _ = capslock.apply(True, True, backend)

    assert managed is True and capslock.marker_exists()


def test_a_setting_the_user_made_gets_no_marker():
    backend = FakeBackend(["caps:none"])

    managed, _ = capslock.apply(True, False, backend)

    assert managed is False and not capslock.marker_exists()


def test_a_failure_leaves_the_marker_as_it_was():
    backend = FakeBackend(fail=OSError("no dbus"))
    capslock.write_marker()

    managed, message = capslock.apply(False, True, backend)

    assert managed is True and "no dbus" in message
    assert capslock.marker_exists()


def test_the_marker_location_can_be_given(tmp_path):
    marker = tmp_path / "elsewhere" / "marker"

    capslock.apply(True, False, FakeBackend([]), FakeCaps(), marker=marker)

    assert marker.exists() and not capslock.marker_exists()      # not at the default place


def test_the_quit_timeout_reaches_the_gsettings_calls(monkeypatch):
    seen = []

    def run(cmd, **kwargs):
        seen.append(kwargs["timeout"])
        return SimpleNamespace(stdout="['caps:none']\n")

    monkeypatch.setattr(capslock.subprocess, "run", run)

    capslock.Gsettings(timeout=capslock.QUIT_TIMEOUT_S).get()
    capslock.Gsettings().get()

    assert seen == [capslock.QUIT_TIMEOUT_S, capslock.GSETTINGS_TIMEOUT_S]
    assert capslock.QUIT_TIMEOUT_S < capslock.GSETTINGS_TIMEOUT_S


# --- gsettings wrapper and availability ----------------------------------------

def test_gsettings_get_and_set_call_the_tool(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(stdout="['caps:none']\n")

    monkeypatch.setattr(capslock.subprocess, "run", fake_run)
    backend = capslock.Gsettings()

    assert backend.get() == ["caps:none"]
    backend.set(["a", "caps:none"])

    assert calls[0] == ["gsettings", "get", capslock.SCHEMA, capslock.KEY]
    assert calls[1] == ["gsettings", "set", capslock.SCHEMA, capslock.KEY, "['a', 'caps:none']"]


def test_available_when_the_setting_can_be_read():
    assert capslock.is_available(FakeBackend()) is True


def test_unavailable_when_reading_fails():
    assert capslock.is_available(FakeBackend(fail=subprocess.CalledProcessError(1, "gsettings"))) is False


def test_unavailable_without_the_gsettings_tool(monkeypatch):
    monkeypatch.setattr(capslock.shutil, "which", lambda name: None)

    assert capslock.is_available() is False
