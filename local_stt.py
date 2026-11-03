"""Local German STT via sherpa-onnx + parakeet-primeline (int8, CPU-only).

Offline transducer. Loads once at first use, warmed up so subsequent decodes
have consistent latency. Not thread-safe over a single recognizer instance —
we serialize decode calls with a lock; for single-user dictation that's fine.
"""
import os
import threading
from typing import Callable, Optional

import numpy as np

import core
from i18n import tr

# huggingface_hub warns on Windows that its cache cannot use symlinks; we
# download straight into MODEL_DIR, so the cache layout does not matter.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
# Its progress bars go nowhere useful: the GUI watches the folder instead,
# and under pythonw.exe there is no console to draw them on (issue #20).
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
# The hub gives up after 10 s without data by default, which a slow link exceeds.
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")

MODEL_DIR = core.DATA_DIR / "models" / "parakeet-primeline-onnx"
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
MODEL_BYTES = 650_000_000   # used for the progress bar when the hub cannot be asked for the sizes
TARGET_SR = 16000
DEFAULT_THREADS = 4
TARGET_PEAK = 0.5   # quiet pieces are raised to this peak before decoding
MAX_GAIN = 30.0     # but never amplified more than this, so room noise stays noise


def model_installed() -> bool:
    return all((MODEL_DIR / f).exists() for f in MODEL_FILES)


def model_size_bytes() -> int:
    """Total size of the model files, from the hub; MODEL_BYTES when offline."""
    try:
        from huggingface_hub import HfApi
        infos = HfApi().get_paths_info(MODEL_REPO, MODEL_FILES, revision=MODEL_REVISION)
        total = sum(getattr(i, "size", 0) or 0 for i in infos)
        return total or MODEL_BYTES
    except Exception:
        return MODEL_BYTES


def download_model(progress_cb: Optional[Callable[[str], None]] = None,
                   fraction_cb: Optional[Callable[[Optional[float]], None]] = None) -> None:
    """Download the pinned model revision from HuggingFace into MODEL_DIR.

    Blocks. Raises RuntimeError on failure. `progress_cb(msg)` gets status
    lines with bytes, speed and time left; `fraction_cb(0..1 or None)` feeds a
    progress bar.
    """
    try:
        from huggingface_hub import snapshot_download
    except ImportError as e:
        raise RuntimeError(
            "huggingface_hub is not installed — run: pip install huggingface_hub"
        ) from e
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    if progress_cb:
        progress_cb(tr("Downloading model (~640 MB, one-time) ..."))
    meter = core.ProgressMeter(tr("Model"), progress_cb, fraction_cb)
    core.run_watching_folder(
        lambda: snapshot_download(
            repo_id=MODEL_REPO,
            revision=MODEL_REVISION,
            local_dir=str(MODEL_DIR),
            allow_patterns=MODEL_FILES,
            etag_timeout=60,
        ),
        MODEL_DIR, model_size_bytes(), meter,
    )
    if progress_cb:
        progress_cb(tr("Model downloaded."))


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
