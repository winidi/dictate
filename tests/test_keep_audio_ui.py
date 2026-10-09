"""A recording is saved before it is transcribed and removed only after its text was typed."""
import os
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import core  # noqa: E402

LOUD = np.full((8000, 1), 2000, dtype=np.int16)    # speech-like level
QUIET = np.full((8000, 1), 100, dtype=np.int16)    # below the silence limit


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


class InlineThread:
    """Runs its target at once, so a test sees the result right after the call."""

    def __init__(self, target=None, args=(), daemon=None, **kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


@pytest.fixture(autouse=True)
def pending_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "PENDING_DIR", tmp_path / "pending")


@pytest.fixture
def make_app(gui_module, monkeypatch):
    """Builds a MainWindow whose recorder, transcriber and typing are fakes."""

    class FakeListener:
        def stop(self):
            pass

    def make():
        state = SimpleNamespace(audio=LOUD, typed=[], errors=[], type_ok=True,
                                transcribe=lambda audio, cfg: "hello")
        cfg = dict(core.DEFAULT_CONFIG)
        monkeypatch.setattr(core, "load_config", lambda: dict(cfg))
        monkeypatch.setattr(core, "notify", lambda *a, **k: None)
        monkeypatch.setattr(core, "transcribe", lambda audio, cfg: state.transcribe(audio, cfg))
        monkeypatch.setattr(core, "type_text", lambda text: (state.typed.append(text), state.type_ok)[1])
        monkeypatch.setattr(core.Recorder, "finish", staticmethod(lambda take: state.audio))
        monkeypatch.setattr(gui_module, "_make_listener", lambda press, release: FakeListener())
        monkeypatch.setattr(gui_module.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(gui_module.threading, "Thread", InlineThread)
        state.window = gui_module.MainWindow()
        state.window.bridge.error.connect(state.errors.append)
        return state
    return make


@pytest.fixture
def app(make_app):
    return make_app()


def dictate(app, audio=LOUD):
    app.audio = audio
    app.window._stop_and_transcribe_worker(take=None)


def raises(message):
    def transcribe(audio, cfg):
        raise OSError(message)
    return transcribe


# --- success -------------------------------------------------------------------------

def test_recording_is_saved_before_transcribing_and_deleted_after_typing(app):
    seen = {}

    def transcribe(audio, cfg):
        seen["saved_while_transcribing"] = len(core.list_pending())
        return "hello"

    app.transcribe = transcribe

    dictate(app)

    assert seen["saved_while_transcribing"] == 1
    assert app.typed == ["hello"]
    assert core.list_pending() == []
    assert app.errors == []
    assert app.window.retry_btn.isHidden()


def test_the_recording_in_progress_is_not_offered_for_retry(app):
    seen = {}

    def transcribe(audio, cfg):
        seen["offered"] = app.window.pending_files()
        seen["button_hidden"] = app.window.retry_btn.isHidden()
        return "hello"

    app.transcribe = transcribe

    dictate(app)

    assert seen == {"offered": [], "button_hidden": True}


# --- failures keep the recording -----------------------------------------------------------

def test_a_transcription_error_keeps_the_recording_and_offers_retry(app):
    app.transcribe = raises("network down")

    dictate(app)

    assert len(core.list_pending()) == 1
    assert len(app.errors) == 1
    assert "Recording kept, press Retry" in app.errors[0] and "network down" in app.errors[0]
    assert not app.window.retry_btn.isHidden()
    assert app.typed == []


def test_an_empty_result_keeps_the_recording(app):
    app.transcribe = lambda audio, cfg: ""

    dictate(app)

    assert len(core.list_pending()) == 1
    assert "no speech recognised" in app.errors[0] and "Recording kept" in app.errors[0]
    assert not app.window.retry_btn.isHidden()


def test_silence_is_not_saved_and_not_offered(app):
    app.transcribe = lambda audio, cfg: ""

    dictate(app, QUIET)

    assert core.list_pending() == []
    assert "microphone was silent" in app.errors[0] and "Recording kept" not in app.errors[0]
    assert app.window.retry_btn.isHidden()


def test_a_typing_failure_keeps_the_recording(app):
    app.type_ok = False

    dictate(app)

    assert app.typed == ["hello"]
    assert len(core.list_pending()) == 1
    assert "Typing failed" in app.errors[0] and "Recording kept" in app.errors[0]
    assert not app.window.retry_btn.isHidden()


# --- retry ---------------------------------------------------------------------------------

def test_retry_transcribes_the_saved_audio_and_deletes_it_after_typing(app):
    core.save_pending(LOUD)
    app.window.refresh_retry()
    assert not app.window.retry_btn.isHidden()
    heard = []
    app.transcribe = lambda audio, cfg: (heard.append(audio.copy()), "second try")[1]

    app.window.on_retry()

    assert np.array_equal(heard[0], LOUD)
    assert app.typed == ["second try"]
    assert core.list_pending() == []
    assert app.window.retry_btn.isHidden()


def test_a_failing_retry_keeps_the_recording_for_the_next_try(app):
    core.save_pending(LOUD)
    app.transcribe = raises("still down")

    app.window.on_retry()
    first = core.list_pending()
    app.transcribe = lambda audio, cfg: "finally"
    app.window.on_retry()

    assert len(first) == 1 and "still down" in app.errors[0]
    assert app.typed == ["finally"]
    assert core.list_pending() == []


def test_retry_takes_the_newest_recording_first(app):
    older = np.full((8000, 1), 1111, dtype=np.int16)
    newer = np.full((8000, 1), 2222, dtype=np.int16)
    core.save_pending(older)
    core.save_pending(newer)
    heard = []
    app.transcribe = lambda audio, cfg: (heard.append(int(audio[0, 0])), "x")[1]

    app.window.on_retry()
    app.window.on_retry()

    assert heard == [2222, 1111]
    assert core.list_pending() == []


def test_retry_without_saved_recordings_does_nothing(app):
    app.transcribe = lambda audio, cfg: pytest.fail("transcribed nothing")

    app.window.on_retry()

    assert app.typed == [] and app.window.retry_btn.isHidden()


def test_an_unreadable_recording_is_removed_and_does_not_block_the_others(app):
    core.PENDING_DIR.mkdir(parents=True)
    (core.PENDING_DIR / "29990101-000000-000.wav").write_bytes(b"not a wav file")   # sorts newest
    good = core.save_pending(LOUD)
    heard = []
    app.transcribe = lambda audio, cfg: (heard.append(1), "ok")[1]

    app.window.on_retry()           # hits the broken file first
    assert "unreadable" in app.window.status_label.text()
    assert core.list_pending() == [good]
    app.window.on_retry()           # now the good one

    assert heard == [1] and core.list_pending() == []


# --- start-up ------------------------------------------------------------------------------

def test_leftover_recordings_offer_a_retry_at_start(make_app):
    core.save_pending(LOUD)

    app = make_app()

    assert not app.window.retry_btn.isHidden()
    assert app.window.retry_btn.text() == "Retry transcription"


def test_several_leftovers_show_how_many(make_app):
    core.save_pending(LOUD)
    core.save_pending(QUIET)

    app = make_app()

    assert app.window.retry_btn.text() == "Retry transcription (2 saved recordings)"


def test_nothing_saved_means_no_retry_button(app):
    assert app.window.retry_btn.isHidden()


def test_old_leftovers_are_removed_at_start(make_app):
    old = core.save_pending(LOUD)
    three_weeks = os.path.getmtime(old) - 21 * 86400
    os.utime(old, (three_weeks, three_weeks))

    app = make_app()

    assert core.list_pending() == []
    assert app.window.retry_btn.isHidden()
