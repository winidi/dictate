"""The error says the real reason for missing input access: not in the group, or
in the group but the login session is older."""
from types import SimpleNamespace

import pytest

try:
    import evdev_listener
except Exception as e:   # no evdev, no X display for pynput, not Linux, ...
    pytest.skip(f"evdev_listener cannot be imported here: {e}", allow_module_level=True)

INPUT_GID = 104
USER = SimpleNamespace(pw_name="alice", pw_gid=1000)


def setup_system(monkeypatch, members=(), session_groups=(1000,), user_gid=1000, group_exists=True):
    group = SimpleNamespace(gr_gid=INPUT_GID, gr_mem=list(members))

    def getgrnam(name):
        if not group_exists:
            raise KeyError(name)
        return group

    monkeypatch.setattr(evdev_listener.grp, "getgrnam", getgrnam)
    monkeypatch.setattr(evdev_listener.pwd, "getpwuid",
                        lambda uid: SimpleNamespace(pw_name=USER.pw_name, pw_gid=user_gid))
    monkeypatch.setattr(evdev_listener.os, "getgroups", lambda: list(session_groups))


def test_not_in_the_group_says_how_to_join_it(monkeypatch):
    setup_system(monkeypatch, members=["bob"])

    reason = evdev_listener._no_access_reason()

    assert reason == ("alice is not in group `input`, which is needed to read /dev/input. "
                      "Please run: sudo usermod -a -G input alice, then log out and log in again")


def test_in_the_group_but_older_session_says_why_and_asks_for_a_new_login(monkeypatch):
    setup_system(monkeypatch, members=["alice"])

    reason = evdev_listener._no_access_reason()

    assert reason == ("alice is in group `input`, but this login session started before that, "
                      "so /dev/input cannot be read yet. Please log out and log in again "
                      "(or reboot)")


def test_group_given_as_primary_group_counts_as_membership(monkeypatch):
    setup_system(monkeypatch, members=[], user_gid=INPUT_GID)

    assert "login session started before" in evdev_listener._no_access_reason()


def test_session_that_has_the_group_gets_the_generic_message(monkeypatch):
    setup_system(monkeypatch, members=["alice"], session_groups=(1000, INPUT_GID))

    assert evdev_listener._no_access_reason() == "no readable keyboard devices in /dev/input"


def test_system_without_an_input_group_gets_the_generic_message(monkeypatch):
    setup_system(monkeypatch, group_exists=False)

    assert evdev_listener._no_access_reason() == "no readable keyboard devices in /dev/input"


def test_listener_start_raises_with_that_reason(monkeypatch):
    setup_system(monkeypatch, members=["alice"])
    monkeypatch.setattr(evdev_listener, "_find_keyboards", lambda: [])

    with pytest.raises(RuntimeError, match="Please log out and log in again"):
        evdev_listener.Listener().start()
