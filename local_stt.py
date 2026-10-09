"""Local German STT via sherpa-onnx + parakeet-primeline (int8, CPU-only).

Offline transducer. Loads once at first use, warmed up so subsequent decodes
have consistent latency. Not thread-safe over a single recognizer instance —
we serialize decode calls with a lock; for single-user dictation that's fine.
"""
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, NamedTuple, Optional

import numpy as np

MODEL_DIR = Path.home() / ".local" / "share" / "dictate" / "models" / "parakeet-primeline-onnx"
MODEL_REPO = "flozen1981/parakeet-primeline-onnx"
# Pin the revision so users always get the exact bits we tested against.
MODEL_REVISION = "d548e25b9bfe559aa274f361892dc4ed5d64743a"
MODEL_FILES = [
    "encoder.int8.onnx",
    "encoder.int8.onnx.data",   # the 621 MB weight blob — MUST be next to encoder.int8.onnx
    "decoder.int8.onnx",
    "joiner.int8.onnx",
    "tokens.txt",
]
TARGET_SR = 16000
DEFAULT_THREADS = 4
TARGET_PEAK = 0.5   # quiet pieces are raised to this peak before decoding
MAX_GAIN = 30.0     # but never amplified more than this, so room noise stays noise


def model_installed() -> bool:
    return all((MODEL_DIR / f).exists() for f in MODEL_FILES)


SPEED_WINDOW_S = 5.0     # speed (and so the ETA) is averaged over the last few seconds
MIN_SPEED_SPAN_S = 1.0   # no speed or ETA before this much has been measured
MIN_EMIT_INTERVAL_S = 0.2
MIN_PLAUSIBLE_TOTAL = 1024 * 1024   # smaller byte totals are the hub still adding up file sizes
MAX_ETA_S = 7 * 86400    # beyond this the estimate is shown as "more than 7 days"
STALL_AFTER_S = 10       # silence this long is reported to the user
# huggingface_hub gives up after 10 s without data by default, which a slow link can exceed.
SLOW_LINK_TIMEOUT_S = 60
_MB = 1024 * 1024


class DownloadProgress(NamedTuple):
    done: int                 # bytes received
    total: int                # bytes expected; 0 while still unknown
    speed: float              # bytes/s over the last SPEED_WINDOW_S; 0.0 until measurable
    eta: Optional[float]      # seconds left; None until measurable


class ProgressTracker:
    """Turns a stream of (done, total) byte counts into speed and ETA."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._samples = deque()   # (time, done), oldest first
        self._last_emit = None

    def update(self, done: int, total: int) -> Optional[DownloadProgress]:
        """Record a reading. Returns a snapshot, or None when it is too soon after the last one."""
        now = self._clock()
        self._samples.append((now, done))
        while len(self._samples) > 2 and now - self._samples[1][0] >= SPEED_WINDOW_S:
            self._samples.popleft()
        finished = total > 0 and done >= total
        if (self._last_emit is not None and not finished
                and now - self._last_emit < MIN_EMIT_INTERVAL_S):
            return None
        self._last_emit = now

        t0, d0 = self._samples[0]
        span = now - t0
        speed = (done - d0) / span if span >= MIN_SPEED_SPAN_S and done > d0 else 0.0
        if finished:
            eta = 0.0
        elif speed > 0 and total > done:
            eta = (total - done) / speed
        else:
            eta = None
        return DownloadProgress(done, total, speed, eta)


def format_duration(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60} s"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60} min"
    return f"{s // 86400} d {s % 86400 // 3600} h"


def format_speed(bytes_per_s: float) -> str:
    """MB/s from 1 MB/s up; KB/s below that, so a slow link never shows '0.0 MB/s'."""
    if bytes_per_s >= _MB:
        return f"{bytes_per_s / _MB:.1f} MB/s"
    kb = bytes_per_s / 1024
    return f"{kb:.0f} KB/s" if kb >= 1 else "<1 KB/s"


def format_progress(p: DownloadProgress) -> str:
    """For example '212 / 640 MB · 14.2 MB/s · about 30 s left'."""
    if p.total <= 0:
        return f"{p.done / _MB:.0f} MB downloaded"
    text = f"{p.done / _MB:.0f} / {p.total / _MB:.0f} MB"
    if p.speed > 0:
        text += f" · {format_speed(p.speed)}"
    if p.eta is None:
        text += " · calculating time left ..."
    elif p.eta > MAX_ETA_S:
        text += " · more than 7 days left"
    elif p.eta > 0:
        text += f" · about {format_duration(p.eta)} left"
    return text


def stall_notice(idle_s: float) -> str:
    """Appended to the status line when no data has arrived for a while, so a
    stalled or very slow connection is not mistaken for a frozen window."""
    if idle_s < STALL_AFTER_S:
        return ""
    return f" — no data for {format_duration(idle_s)}, still waiting ..."


def _silent_progress_class(report: Callable[[int, int], None]):
    """A tqdm class for snapshot_download that draws nothing. It reports the byte
    count of the first bar counting bytes, which huggingface_hub creates for the
    network transfer, to report(done, total)."""
    from tqdm.auto import tqdm

    state = {"reporter": None}

    class _SilentBar(tqdm):
        def __init__(self, *args, **kwargs):
            kwargs["leave"] = False
            super().__init__(*args, **kwargs)
            if state["reporter"] is None and kwargs.get("unit") == "B":
                state["reporter"] = self

        def display(self, *args, **kwargs):
            pass

        def update(self, n=1):
            result = super().update(n)
            if state["reporter"] is self:
                report(int(self.n), int(self.total or 0))
            return result

    return _SilentBar


def download_model(
    progress_cb: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[DownloadProgress], None]] = None,
) -> None:
    """Download the pinned model revision from HuggingFace into MODEL_DIR.

    Blocks. Raises RuntimeError on failure. `progress_cb(msg)` gets short
    status strings for GUI display; `on_progress(DownloadProgress)` gets byte
    counts, speed and ETA while files are transferring.
    """
    # Read when huggingface_hub is first imported, so it must be set before that.
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", str(SLOW_LINK_TIMEOUT_S))
    try:
        from huggingface_hub import snapshot_download
    except ImportError as e:
        raise RuntimeError(
            "huggingface_hub is not installed — run: pip install huggingface_hub"
        ) from e
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    if progress_cb:
        progress_cb("Downloading model (~640 MB, one-time) ...")
    kwargs = {"etag_timeout": SLOW_LINK_TIMEOUT_S}
    if on_progress:
        tracker = ProgressTracker()

        def report(done: int, total: int) -> None:
            if total < MIN_PLAUSIBLE_TOTAL:
                return   # the hub adds up file sizes one by one; wait until the total is real
            snapshot = tracker.update(done, total)
            if snapshot:
                on_progress(snapshot)

        kwargs["tqdm_class"] = _silent_progress_class(report)
    snapshot_download(
        repo_id=MODEL_REPO,
        revision=MODEL_REVISION,
        local_dir=str(MODEL_DIR),
        allow_patterns=MODEL_FILES,
        **kwargs,
    )
    if progress_cb:
        progress_cb("Model downloaded.")


class LocalRecognizer:
    """Wraps sherpa_onnx.OfflineRecognizer with async load + warmup + decode lock."""

    def __init__(self, num_threads: int):
        self.num_threads = num_threads
        self._recognizer = None
        self._decode_lock = threading.Lock()
        self._ready = threading.Event()
        self._load_error: Optional[Exception] = None
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self) -> None:
        try:
            import sherpa_onnx
            rec = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=str(MODEL_DIR / "encoder.int8.onnx"),
                decoder=str(MODEL_DIR / "decoder.int8.onnx"),
                joiner=str(MODEL_DIR / "joiner.int8.onnx"),
                tokens=str(MODEL_DIR / "tokens.txt"),
                model_type="nemo_transducer",
                num_threads=self.num_threads,
                decoding_method="greedy_search",
            )
            # Warmup: two 1-second silences. First real decode is ~2x slower
            # without this, and the user is waiting on the first one.
            for _ in range(2):
                s = rec.create_stream()
                s.accept_waveform(TARGET_SR, np.zeros(TARGET_SR, dtype=np.float32))
                rec.decode_stream(s)
            self._recognizer = rec
        except Exception as e:
            self._load_error = e
        finally:
            self._ready.set()

    def is_ready(self) -> bool:
        return self._ready.is_set() and self._recognizer is not None

    def wait_ready(self, timeout: Optional[float] = None) -> bool:
        return self._ready.wait(timeout)

    def transcribe(self, audio: np.ndarray) -> str:
        """Take int16 mono 16 kHz audio (shape (N,) or (N, 1)), return text."""
        self._ready.wait()
        if self._recognizer is None:
            raise RuntimeError(f"local recognizer failed to load: {self._load_error}")
        if audio.ndim > 1:
            audio = audio.squeeze()
        # sherpa-onnx wants float32 in [-1, 1]
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32) / 32768.0
        parts = []
        with self._decode_lock:
            for chunk in _split(audio):
                text = self._decode(chunk)
                # The model now and then returns nothing for a long piece that
                # clearly holds speech; shorter pieces of the same audio decode.
                if not text and len(chunk) > RETRY_CHUNK_S * TARGET_SR:
                    text = " ".join(t for t in map(self._decode, _split(chunk, RETRY_CHUNK_S)) if t)
                parts.append(text)
        return " ".join(p for p in parts if p)

    def _decode(self, chunk: np.ndarray) -> str:
        # Quiet input (speech around -45 dBFS, peak ~0.03) comes back empty
        # as a whole; the same audio raised to a peak of 0.5 decodes in full.
        peak = float(np.abs(chunk).max()) if len(chunk) else 0.0
        if 0.0 < peak < TARGET_PEAK:
            chunk = chunk * min(TARGET_PEAK / peak, MAX_GAIN)
        s = self._recognizer.create_stream()
        s.accept_waveform(TARGET_SR, chunk)
        self._recognizer.decode_stream(s)
        return s.result.text.strip()


# The encoder fails outright on more than 400 s of audio ("Attempting to
# broadcast an axis ...") and its memory grows quadratically before that
# (180 s ≈ 3.5 GB), so long dictations are decoded in pieces.
MAX_CHUNK_S = 90
RETRY_CHUNK_S = 20  # piece length for the second pass over a piece that came back empty
_SEARCH_S = 15      # look for a pause in the last part of each piece
_WIN_S = 0.4


def _split(audio: np.ndarray, max_s: float = MAX_CHUNK_S):
    """Yield pieces of at most max_s, cut at the quietest spot near the end."""
    search_s = min(_SEARCH_S, max_s / 2)
    max_n, search_n, win_n = (int(x * TARGET_SR) for x in (max_s, search_s, _WIN_S))
    while len(audio) > max_n:
        tail = audio[max_n - search_n:max_n]
        n_win = len(tail) // win_n
        energy = (tail[:n_win * win_n].reshape(n_win, win_n) ** 2).mean(axis=1)
        cut = max_n - search_n + int(energy.argmin()) * win_n + win_n // 2
        yield audio[:cut]
        audio = audio[cut:]
    yield audio


_instance: Optional[LocalRecognizer] = None
_instance_lock = threading.Lock()


def get_recognizer(num_threads: int = DEFAULT_THREADS) -> LocalRecognizer:
    """Get or (re)create the process-wide singleton.

    sherpa-onnx does not let us change num_threads on an existing recognizer,
    so a thread-count change drops the old one and builds a fresh one. The
    caller pays the ~0.8 s load cost only when the setting actually changes.
    """
    global _instance
    with _instance_lock:
        if _instance is None or _instance.num_threads != num_threads:
            _instance = LocalRecognizer(num_threads)
        return _instance


def preload(num_threads: int = DEFAULT_THREADS) -> LocalRecognizer:
    """Kick off the model load in the background so the first real decode is fast."""
    return get_recognizer(num_threads)
