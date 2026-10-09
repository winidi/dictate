"""Speed/ETA tracking, formatting and the download hook, all without network."""
import os
import sys
import types

import pytest

import local_stt
from local_stt import (
    DownloadProgress, ProgressTracker, format_duration, format_progress, format_speed,
    stall_notice,
)

MB = 1024 * 1024


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def run(tracker, clock, rate, seconds, total, start=0, step=0.5):
    """Feed readings at a steady byte rate; return the last snapshot."""
    snapshot, done = None, start
    for _ in range(int(seconds / step)):
        clock.advance(step)
        done += int(rate * step)
        snapshot = tracker.update(done, total) or snapshot
    return snapshot


# --- ProgressTracker ---------------------------------------------------------

def test_no_speed_or_eta_before_a_second_is_measured():
    clock = FakeClock()
    tracker = ProgressTracker(clock)

    first = tracker.update(0, 100 * MB)

    assert first == DownloadProgress(0, 100 * MB, 0.0, None)


def test_steady_rate_gives_speed_and_eta():
    clock = FakeClock()
    tracker = ProgressTracker(clock)

    snap = run(tracker, clock, rate=10 * MB, seconds=4, total=100 * MB)

    assert snap.speed == pytest.approx(10 * MB, rel=0.05)
    assert snap.eta == pytest.approx((100 * MB - snap.done) / (10 * MB), rel=0.05)


def test_speed_follows_the_recent_window_not_the_whole_history():
    clock = FakeClock()
    tracker = ProgressTracker(clock)
    slow = run(tracker, clock, rate=1 * MB, seconds=10, total=1000 * MB)

    fast = run(tracker, clock, rate=10 * MB, seconds=10, total=1000 * MB, start=slow.done)

    assert fast.speed == pytest.approx(10 * MB, rel=0.1)


def test_readings_closer_than_the_interval_are_dropped():
    clock = FakeClock()
    tracker = ProgressTracker(clock)
    tracker.update(0, 100 * MB)

    clock.advance(0.05)
    too_soon = tracker.update(MB, 100 * MB)
    clock.advance(0.25)
    later = tracker.update(2 * MB, 100 * MB)

    assert too_soon is None
    assert later is not None and later.done == 2 * MB


def test_finished_download_is_always_reported_with_zero_eta():
    clock = FakeClock()
    tracker = ProgressTracker(clock)
    tracker.update(0, 100 * MB)

    clock.advance(0.01)
    last = tracker.update(100 * MB, 100 * MB)

    assert last is not None and last.eta == 0.0


def test_unknown_total_has_no_eta():
    clock = FakeClock()
    tracker = ProgressTracker(clock)

    snap = run(tracker, clock, rate=5 * MB, seconds=3, total=0)

    assert snap.eta is None and snap.speed > 0


def test_stalled_transfer_has_no_eta():
    clock = FakeClock()
    tracker = ProgressTracker(clock)
    tracker.update(10 * MB, 100 * MB)

    clock.advance(3)
    snap = tracker.update(10 * MB, 100 * MB)

    assert snap.speed == 0.0 and snap.eta is None


# --- formatting --------------------------------------------------------------

@pytest.mark.parametrize("seconds, text", [
    (0, "0 s"),
    (29.6, "30 s"),
    (59.4, "59 s"),
    (60, "1 min 0 s"),
    (125, "2 min 5 s"),
    (3725, "1 h 2 min"),
    (-5, "0 s"),
])
def test_format_duration(seconds, text):
    assert format_duration(seconds) == text


def test_format_progress_with_speed_and_eta():
    p = DownloadProgress(212 * MB, 640 * MB, 14.2 * MB, 30.0)

    assert format_progress(p) == "212 / 640 MB · 14.2 MB/s · about 30 s left"


def test_format_progress_while_still_calculating():
    p = DownloadProgress(5 * MB, 640 * MB, 0.0, None)

    assert format_progress(p) == "5 / 640 MB · calculating time left ..."


def test_format_progress_without_known_total():
    assert format_progress(DownloadProgress(7 * MB, 0, 0.0, None)) == "7 MB downloaded"


def test_format_progress_when_done_has_no_time_left():
    p = DownloadProgress(640 * MB, 640 * MB, 9 * MB, 0.0)

    assert format_progress(p) == "640 / 640 MB · 9.0 MB/s"


# --- slow connections --------------------------------------------------------

@pytest.mark.parametrize("bytes_per_s, text", [
    (14.2 * MB, "14.2 MB/s"),
    (1 * MB, "1.0 MB/s"),
    (512 * 1024, "512 KB/s"),
    (40 * 1024, "40 KB/s"),
    (1024, "1 KB/s"),
    (300, "<1 KB/s"),
    (0, "<1 KB/s"),
])
def test_format_speed_switches_to_kb_for_slow_links(bytes_per_s, text):
    assert format_speed(bytes_per_s) == text


@pytest.mark.parametrize("seconds, text", [
    (86400, "1 d 0 h"),
    (2 * 86400 + 3 * 3600 + 120, "2 d 3 h"),
])
def test_format_duration_counts_days(seconds, text):
    assert format_duration(seconds) == text


def test_format_progress_for_a_slow_link_shows_kb_and_hours():
    # 40 KB/s with about 600 MB left is roughly four and a half hours.
    p = DownloadProgress(40 * MB, 640 * MB, 40 * 1024, 15000.0)

    assert format_progress(p) == "40 / 640 MB · 40 KB/s · about 4 h 10 min left"


def test_format_progress_caps_absurd_estimates():
    p = DownloadProgress(1 * MB, 640 * MB, 500, 10 * 86400.0)

    assert format_progress(p) == "1 / 640 MB · <1 KB/s · more than 7 days left"


def test_stall_notice_is_empty_while_data_keeps_arriving():
    assert stall_notice(0) == "" and stall_notice(9.9) == ""


def test_stall_notice_says_how_long_nothing_arrived():
    assert stall_notice(10) == " — no data for 10 s, still waiting ..."
    assert stall_notice(125) == " — no data for 2 min 5 s, still waiting ..."


def test_tracker_still_estimates_when_data_arrives_in_rare_bursts():
    clock = FakeClock()
    tracker = ProgressTracker(clock)
    tracker.update(0, 100 * MB)

    clock.advance(4)
    tracker.update(MB, 100 * MB)
    clock.advance(4)
    snap = tracker.update(2 * MB, 100 * MB)

    assert snap.speed == pytest.approx(MB / 4, rel=0.05)
    assert snap.eta == pytest.approx(98 * MB / (MB / 4), rel=0.05)


# --- the tqdm class handed to huggingface_hub --------------------------------

def test_silent_bar_reports_only_the_first_byte_bar_and_prints_nothing(capsys):
    seen = []
    bar_class = local_stt._silent_progress_class(lambda done, total: seen.append((done, total)))

    transfer = bar_class(total=0, unit="B", desc="Downloading bytes")
    reconstruct = bar_class(total=0, unit="B", desc="Reconstructing")
    files = bar_class(total=5, desc="Fetching 5 files")
    transfer.total += 100
    transfer.update(40)
    reconstruct.update(10)
    files.update(1)
    transfer.update(60)
    for bar in (transfer, reconstruct, files):
        bar.close()

    assert seen == [(40, 100), (100, 100)]
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


# --- download_model ----------------------------------------------------------

def fake_hub(monkeypatch, chunks):
    """Install a huggingface_hub whose snapshot_download drives tqdm_class like the real one."""
    calls = []

    def snapshot_download(**kwargs):
        calls.append(kwargs)
        bar_class = kwargs.get("tqdm_class")
        if bar_class:
            bar = bar_class(total=0, unit="B", desc="Downloading bytes")
            bar.total = sum(chunks)
            for chunk in chunks:
                bar.update(chunk)

    module = types.ModuleType("huggingface_hub")
    module.snapshot_download = snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", module)
    return calls


def test_download_model_reports_progress_and_status(monkeypatch, tmp_path):
    monkeypatch.setattr(local_stt, "MODEL_DIR", tmp_path / "model")
    clock = FakeClock()
    calls = fake_hub(monkeypatch, [10 * MB, 20 * MB, 30 * MB])
    snapshots, messages = [], []

    class TickingTracker(local_stt.ProgressTracker):
        """Half a second passes between two readings."""

        def __init__(self):
            super().__init__(clock)

        def update(self, done, total):
            clock.advance(0.5)
            return super().update(done, total)

    monkeypatch.setattr(local_stt, "ProgressTracker", TickingTracker)

    local_stt.download_model(progress_cb=messages.append, on_progress=snapshots.append)

    assert [s.done for s in snapshots] == [10 * MB, 30 * MB, 60 * MB]
    assert snapshots[-1].total == 60 * MB and snapshots[-1].eta == 0.0
    assert messages[0].startswith("Downloading model") and messages[-1] == "Model downloaded."
    assert calls[0]["repo_id"] == local_stt.MODEL_REPO
    assert calls[0]["revision"] == local_stt.MODEL_REVISION


def test_download_model_waits_until_the_total_is_real(monkeypatch, tmp_path):
    """The hub registers file sizes one by one: a first reading of 100 of 100 bytes
    must not show up as a finished download."""
    monkeypatch.setattr(local_stt, "MODEL_DIR", tmp_path / "model")
    module = types.ModuleType("huggingface_hub")

    def snapshot_download(**kwargs):
        bar = kwargs["tqdm_class"](total=0, unit="B", desc="Downloading bytes")
        bar.total = 100                       # only a tiny file registered so far
        bar.update(100)
        bar.total = 600 * MB                  # the big weight file registers
        bar.update(5 * MB)

    module.snapshot_download = snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", module)
    snapshots = []

    local_stt.download_model(on_progress=snapshots.append)

    assert [(s.done, s.total) for s in snapshots] == [(100 + 5 * MB, 600 * MB)]


def test_download_model_without_progress_callback_uses_the_hubs_own_bars(monkeypatch, tmp_path):
    monkeypatch.setattr(local_stt, "MODEL_DIR", tmp_path / "model")
    calls = fake_hub(monkeypatch, [MB])

    local_stt.download_model()

    assert "tqdm_class" not in calls[0]


def test_download_model_allows_slow_links_more_time(monkeypatch, tmp_path):
    monkeypatch.setattr(local_stt, "MODEL_DIR", tmp_path / "model")
    # Register the variable with monkeypatch so it is removed again after the test.
    monkeypatch.setenv("HF_HUB_DOWNLOAD_TIMEOUT", "placeholder")
    monkeypatch.delenv("HF_HUB_DOWNLOAD_TIMEOUT")
    calls = fake_hub(monkeypatch, [MB])

    local_stt.download_model(on_progress=lambda p: None)

    assert calls[0]["etag_timeout"] == local_stt.SLOW_LINK_TIMEOUT_S
    assert os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] == str(local_stt.SLOW_LINK_TIMEOUT_S)


def test_download_model_keeps_a_timeout_the_user_already_set(monkeypatch, tmp_path):
    monkeypatch.setattr(local_stt, "MODEL_DIR", tmp_path / "model")
    monkeypatch.setenv("HF_HUB_DOWNLOAD_TIMEOUT", "5")
    fake_hub(monkeypatch, [MB])

    local_stt.download_model()

    assert os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] == "5"


def test_download_model_failure_propagates(monkeypatch, tmp_path):
    monkeypatch.setattr(local_stt, "MODEL_DIR", tmp_path / "model")
    module = types.ModuleType("huggingface_hub")

    def broken(**kwargs):
        raise OSError("network down")

    module.snapshot_download = broken
    monkeypatch.setitem(sys.modules, "huggingface_hub", module)

    with pytest.raises(OSError, match="network down"):
        local_stt.download_model(on_progress=lambda p: None)


def test_download_model_without_huggingface_hub_says_what_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)

    with pytest.raises(RuntimeError, match="pip install huggingface_hub"):
        local_stt.download_model()
