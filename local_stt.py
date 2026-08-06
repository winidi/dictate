"""Local German STT via sherpa-onnx + parakeet-primeline (int8, CPU-only).

Offline transducer. Loads once at first use, warmed up so subsequent decodes
have consistent latency. Not thread-safe over a single recognizer instance —
we serialize decode calls with a lock; for single-user dictation that's fine.
"""
import threading
from pathlib import Path
from typing import Callable, Optional

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


def model_installed() -> bool:
    return all((MODEL_DIR / f).exists() for f in MODEL_FILES)


def download_model(progress_cb: Optional[Callable[[str], None]] = None) -> None:
    """Download the pinned model revision from HuggingFace into MODEL_DIR.

    Blocks. Raises RuntimeError on failure. `progress_cb(msg)` gets short
    status strings for GUI display.
    """
    try:
        from huggingface_hub import snapshot_download
    except ImportError as e:
        raise RuntimeError(
            "huggingface_hub is not installed — run: pip install huggingface_hub"
        ) from e
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    if progress_cb:
        progress_cb("Downloading model (~640 MB, one-time) ...")
    snapshot_download(
        repo_id=MODEL_REPO,
        revision=MODEL_REVISION,
        local_dir=str(MODEL_DIR),
        allow_patterns=MODEL_FILES,
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
        with self._decode_lock:
            s = self._recognizer.create_stream()
            s.accept_waveform(TARGET_SR, audio)
            self._recognizer.decode_stream(s)
            return s.result.text


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
