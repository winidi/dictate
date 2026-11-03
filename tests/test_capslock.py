import capslock


class FakeGsettings:
    def __init__(self, options=None, fail=False):
        self.options = list(options or [])
        self.fail = fail
        self.sets = []

    def get(self):
        if self.fail:
            raise RuntimeError("no schema")
        return list(self.options)

    def set(self, options):
        self.options = list(options)
        self.sets.append(list(options))


class FakeCaps:
    def __init__(self, off_ok=True):
        self.off_ok = off_ok
        self.calls = 0

    def turn_off(self):
        self.calls += 1
        return self.off_ok


def test_parse_and_format_options():
    assert capslock.parse_options("@as []") == []
    assert capslock.parse_options("['caps:none', 'grp:alt_shift_toggle']") == ["caps:none", "grp:alt_shift_toggle"]
    assert capslock.format_options(["a", "b"]) == "['a', 'b']"
    assert capslock.format_options([]) == "[]"


def test_apply_adds_option_and_takes_ownership():
    g = FakeGsettings(["grp:alt_shift_toggle"])
    managed, msg = capslock.apply(True, False, backend=g, caps=FakeCaps())
    assert (managed, msg) == (True, "")
    assert g.options == ["grp:alt_shift_toggle", "caps:none"]


def test_apply_leaves_a_user_setting_alone():
    g = FakeGsettings(["caps:none"])
    assert capslock.apply(True, False, backend=g, caps=FakeCaps()) == (False, "")
    assert g.sets == []
    # ... and never removes what it did not add
    assert capslock.apply(False, False, backend=g) == (False, "")
    assert g.options == ["caps:none"]


def test_apply_removes_only_its_own_option():
    g = FakeGsettings(["caps:none", "grp:win_space_toggle"])
    assert capslock.apply(False, True, backend=g) == (False, "")
    assert g.options == ["grp:win_space_toggle"]


def test_apply_refuses_when_caps_lock_stays_on():
    g = FakeGsettings([])
    managed, msg = capslock.apply(True, False, backend=g, caps=FakeCaps(off_ok=False))
    assert managed is False
    assert "could not be switched off" in msg
    assert g.sets == []


def test_apply_reports_backend_errors():
    managed, msg = capslock.apply(True, True, backend=FakeGsettings(fail=True), caps=FakeCaps())
    assert managed is True          # ownership is not forgotten over an error
    assert msg.startswith("Could not change the Caps Lock setting")


def test_is_available_with_backend():
    assert capslock.is_available(FakeGsettings([])) is True
    assert capslock.is_available(FakeGsettings(fail=True)) is False
