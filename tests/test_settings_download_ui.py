"""The Settings dialog shows download progress; no real download is started."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from local_stt import DownloadProgress  # noqa: E402

MB = 1024 * 1024


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialog(qapp):
    try:
        import core
        import gui
    except Exception as e:   # no X display for pynput, no audio library, ...
        pytest.skip(f"gui cannot be imported here: {e}")
    cfg = dict(core.DEFAULT_CONFIG, provider="parakeet_local")
    return gui.SettingsDialog(cfg)


def test_progress_bar_is_hidden_until_a_download_starts(dialog):
    assert dialog.local_progress_bar.isHidden()


def test_progress_updates_bar_and_status_text(dialog):
    dialog._set_downloading(True)

    dialog._dl_progress.emit(DownloadProgress(320 * MB, 640 * MB, 14 * MB, 30.0))

    assert not dialog.local_progress_bar.isHidden()
    assert dialog.local_progress_bar.value() == 500
    assert dialog.local_status_label.text() == "320 / 640 MB · 14.0 MB/s · about 30 s left"


def test_bar_stays_a_busy_indicator_while_the_total_is_unknown(dialog):
    dialog._set_downloading(True)
    dialog.local_progress_bar.setRange(0, 0)

    dialog._dl_progress.emit(DownloadProgress(3 * MB, 0, 0.0, None))

    assert dialog.local_progress_bar.maximum() == 0
    assert dialog.local_status_label.text() == "3 MB downloaded"


def test_status_line_says_when_a_slow_connection_has_stalled(dialog):
    dialog._set_downloading(True)
    dialog._dl_progress.emit(DownloadProgress(40 * MB, 640 * MB, 40 * 1024, 15000.0))
    arrived = dialog._dl_last_data

    dialog._tick_download(now=arrived + 5)
    quiet = dialog.local_status_label.text()
    dialog._tick_download(now=arrived + 30)
    stalled = dialog.local_status_label.text()

    assert quiet == "40 / 640 MB · 40 KB/s · about 4 h 10 min left"
    assert stalled == quiet + " — no data for 30 s, still waiting ..."


def test_new_data_clears_the_stall_notice(dialog):
    dialog._set_downloading(True)
    dialog._dl_progress.emit(DownloadProgress(40 * MB, 640 * MB, 40 * 1024, 15000.0))
    dialog._tick_download(now=dialog._dl_last_data + 60)

    dialog._dl_progress.emit(DownloadProgress(41 * MB, 640 * MB, 40 * 1024, 14900.0))

    assert "no data" not in dialog.local_status_label.text()


def test_stall_check_runs_only_while_downloading(dialog):
    assert not dialog._dl_timer.isActive()

    dialog._set_downloading(True)
    assert dialog._dl_timer.isActive()

    dialog._dl_done.emit(True, "Installed.")
    assert not dialog._dl_timer.isActive()
    assert dialog.local_status_label.text() == "Installed."


def test_clicking_download_shows_a_busy_bar_and_starts_the_worker(dialog, monkeypatch):
    import gui

    started = []

    class FakeThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            started.append(self.target)

    monkeypatch.setattr(gui.threading, "Thread", FakeThread)

    dialog.on_download_model()

    assert started == [dialog._download_worker]
    assert not dialog.local_progress_bar.isHidden()
    assert dialog.local_progress_bar.maximum() == 0          # busy indicator
    assert not dialog.local_download_btn.isEnabled()
    assert dialog.local_status_label.text() == "Starting download..."
    assert dialog._dl_timer.isActive()


def test_status_messages_from_the_worker_reach_the_status_line(dialog):
    dialog._set_downloading(True)

    dialog._dl_status.emit("Downloading model (~640 MB, one-time) ...")

    assert dialog.local_status_label.text() == "Downloading model (~640 MB, one-time) ..."


def test_worker_forwards_progress_and_reports_success(dialog, monkeypatch):
    import local_stt
    events = []
    dialog._dl_progress.connect(lambda p: events.append(("progress", p.done)))
    dialog._dl_done.connect(lambda ok, msg: events.append(("done", ok, msg)))

    def fake_download(progress_cb, on_progress):
        progress_cb("Downloading model (~640 MB, one-time) ...")
        on_progress(DownloadProgress(10 * MB, 20 * MB, 0.0, None))

    monkeypatch.setattr(local_stt, "download_model", fake_download)

    dialog._download_worker()

    assert events == [("progress", 10 * MB), ("done", True, "Installed.")]


def test_worker_reports_a_failed_download(dialog, monkeypatch):
    import local_stt
    done = []
    dialog._dl_done.connect(lambda ok, msg: done.append((ok, msg)))

    def broken(progress_cb, on_progress):
        raise OSError("network down")

    monkeypatch.setattr(local_stt, "download_model", broken)

    dialog._download_worker()

    assert done == [(False, "Download failed: network down")]


def test_finished_download_hides_the_bar_and_offers_redownload(dialog):
    dialog._set_downloading(True)

    dialog._dl_done.emit(True, "Installed.")

    assert dialog.local_progress_bar.isHidden()
    assert dialog.local_status_label.text() == "Installed."
    assert dialog.local_download_btn.text() == "Re-download model"


def test_failed_download_hides_the_bar_and_offers_retry(dialog):
    dialog._set_downloading(True)

    dialog._dl_done.emit(False, "Download failed: network down")

    assert dialog.local_progress_bar.isHidden()
    assert dialog.local_download_btn.text() == "Retry download"
    assert dialog.local_download_btn.isEnabled()
