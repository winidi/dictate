"""Saved recordings: written before transcribing, deleted only on success, kept tidy."""
import os
import time
from types import SimpleNamespace

import numpy as np
import pytest

import core


@pytest.fixture(autouse=True)
def pending_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "PENDING_DIR", tmp_path / "pending")
    return tmp_path / "pending"


def audio(value=1000, n=1600):
    return np.full((n, 1), value, dtype=np.int16)


def clock(monkeypatch, step=1.0):
    """time.time() that starts at the real time and advances by `step` per call, so
    saves get distinct names and the files' real modification times stay plausible."""
    state = {"now": time.time() - step}

    def fake():
        state["now"] += step
        return state["now"]

    monkeypatch.setattr(time, "time", fake)


# --- save and load ------------------------------------------------------------

def test_save_and_load_give_back_the_same_audio():
    original = np.arange(-800, 800, dtype=np.int16).reshape(-1, 1)

    path = core.save_pending(original)
    loaded = core.load_pending(path)

    assert path.suffix == ".wav" and path.parent == core.PENDING_DIR
    assert loaded.dtype == np.int16 and loaded.shape == original.shape
    assert np.array_equal(loaded, original)


def test_no_half_written_file_is_left_behind(pending_dir):
    core.save_pending(audio())

    assert [p.suffix for p in pending_dir.iterdir()] == [".wav"]


def test_two_saves_in_the_same_millisecond_do_not_overwrite_each_other(monkeypatch):
    frozen = time.time()
    monkeypatch.setattr(time, "time", lambda: frozen)

    first = core.save_pending(audio(1))
    second = core.save_pending(audio(2))

    assert first != second
    assert len(core.list_pending()) == 2
    assert core.list_pending()[0] == second                  # newest first, also for a tie
    assert core.load_pending(first)[0, 0] == 1 and core.load_pending(second)[0, 0] == 2


def test_list_is_newest_first(monkeypatch):
    clock(monkeypatch)
    paths = [core.save_pending(audio(i)) for i in range(3)]

    assert core.list_pending() == list(reversed(paths))


def test_list_without_a_folder_is_empty():
    assert core.list_pending() == []


def test_save_failure_returns_none_and_is_logged(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(core, "PENDING_DIR", blocker / "pending")   # cannot be created

    with caplog.at_level("ERROR", logger="dictate"):
        assert core.save_pending(audio()) is None

    assert "could not save the recording" in caplog.text


# --- deleting -----------------------------------------------------------------

def test_discard_removes_the_file():
    path = core.save_pending(audio())

    core.discard_pending(path)

    assert core.list_pending() == []


def test_discard_of_a_missing_file_is_quiet():
    core.discard_pending(core.PENDING_DIR / "gone.wav")


# --- keeping the folder tidy ------------------------------------------------------

def test_only_the_newest_recordings_are_kept(monkeypatch):
    monkeypatch.setattr(core, "PENDING_KEEP", 3)
    clock(monkeypatch)

    paths = [core.save_pending(audio(i)) for i in range(5)]

    assert core.list_pending() == list(reversed(paths[2:]))


def test_recordings_older_than_two_weeks_are_removed():
    old = core.save_pending(audio(1))
    time.sleep(0.01)
    recent = core.save_pending(audio(2))
    three_weeks = time.time() - 21 * 86400
    os.utime(old, (three_weeks, three_weeks))

    core.prune_pending()

    assert core.list_pending() == [recent]


def test_recordings_inside_the_limits_stay(monkeypatch):
    clock(monkeypatch)
    paths = [core.save_pending(audio(i)) for i in range(4)]

    core.prune_pending(now=time.time())

    assert len(core.list_pending()) == len(paths)


# --- type_text reports whether it worked ----------------------------------------------

@pytest.fixture
def typing(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    notes = []
    monkeypatch.setattr(core, "notify", lambda msg, **kw: notes.append(msg))
    return notes


def test_typing_empty_text_needs_no_tool_and_counts_as_done(monkeypatch, typing):
    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: pytest.fail("ran a tool"))

    assert core.type_text("") is True


def test_typing_succeeds_when_the_tool_exits_cleanly(monkeypatch, typing):
    calls = []
    monkeypatch.setattr(core.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd[0]) or SimpleNamespace(returncode=0))

    assert core.type_text("hallo") is True
    assert calls == ["xdotool"]
    assert typing == []


def test_typing_fails_when_the_tool_reports_an_error(monkeypatch, typing):
    monkeypatch.setattr(core.subprocess, "run", lambda cmd, **k: SimpleNamespace(returncode=1))

    assert core.type_text("hallo") is False
    assert typing and "Typing failed" in typing[0]


def test_typing_fails_when_no_tool_is_installed(monkeypatch, typing):
    def missing(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(core.subprocess, "run", missing)

    assert core.type_text("hallo") is False
    assert typing and "neither xdotool nor ydotool" in typing[0]


def test_typing_falls_back_to_the_second_tool(monkeypatch, typing):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd[0])
        if cmd[0] == "xdotool":
            raise FileNotFoundError("xdotool")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(core.subprocess, "run", run)

    assert core.type_text("hallo") is True
    assert calls == ["xdotool", "ydotool"]
