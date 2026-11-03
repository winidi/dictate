"""A pool of recognizers so several people can dictate at once.

sherpa-onnx fixes the thread count when a recognizer is created, so "give
each user fewer cores when it gets busy" is done with several instances of
different sizes: the first request gets the big one (up to four threads),
requests that arrive while it is busy get smaller ones, and when every
instance is busy the request waits a moment. Each instance holds its own copy
of the model (about a gigabyte), so the RAM, not the cores, usually decides
how many there can be. Instances are created the first time they are needed.
"""
import io
import os
import threading
import time
import wave
from typing import List, Optional

import numpy as np

import core
import local_stt

INSTANCE_MB = 1200       # model plus working memory per recognizer, generous
PRIMARY_MAX_THREADS = 4  # more brings little, see README
WAIT_S = 30.0            # a request waits this long for a free recognizer, then 503


def total_ram_mb() -> int:
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e6)
    except (ValueError, OSError, AttributeError):
        return 4000


def plan_workers(cores: int, ram_mb: int) -> List[int]:
    """Thread counts of the recognizer instances for this machine.

    [4] on a quad core with 2 GB, [4, 2] with 4 GB, [4, 2, 2] with 8 GB,
    [2, 1] on a 2-core VPS with 4 GB. The sum may exceed the cores a little:
    two people dictating at the same moment each get a slower answer, not a
    refusal.
    """
    cores = max(1, cores)
    sizes = [min(PRIMARY_MAX_THREADS, cores)]
    max_instances = max(1, (ram_mb - 600) // INSTANCE_MB)   # 600 MB for the system
    extra = max(1, cores // 2)
    while len(sizes) < min(max_instances, 1 + cores // 2):
        sizes.append(extra)
    return sizes


def decode_audio(data: bytes) -> np.ndarray:
    """Bytes of an audio file -> int16 mono 16 kHz samples, shape (N, 1).

    WAV of any rate, width or channel count is read with the standard library;
    other formats (FLAC, OGG/Opus, MP3 with a recent libsndfile) through
    soundfile when it is installed.
    """
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            rate, channels, width = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
            frames = wf.readframes(wf.getnframes())
        if width == 2:
            samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        elif width == 1:
            samples = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128) / 128.0
        elif width == 4:
            samples = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
        else:
            raise ValueError(f"unsupported WAV sample width {width}")
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
    except (wave.Error, EOFError):
        try:
            import soundfile as sf
        except ImportError as e:
            raise ValueError("not a WAV file and soundfile is not installed") from e
        try:
            samples, rate = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
        except Exception as e:          # soundfile.LibsndfileError, among others
            raise ValueError(f"unsupported audio format: {e}") from e
        samples = samples.mean(axis=1)
    if rate != core.SAMPLE_RATE and len(samples):
        n = int(round(len(samples) * core.SAMPLE_RATE / rate))
        samples = np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples)
    return (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).reshape(-1, 1)


class RecognizerPool:
    def __init__(self, sizes: Optional[List[int]] = None):
        self.sizes = list(sizes or plan_workers(os.cpu_count() or 1, total_ram_mb()))
        self._lock = threading.Condition()
        self._instances = []            # created lazily, index parallels self.sizes
        self._busy = set()              # indices in use
        self.in_flight = 0
        self.waited_total = 0.0

    def preload(self) -> None:
        """Load the primary instance now, so the first request is not slowed by it."""
        with self._lock:
            self._instance(0)

    def _instance(self, i: int):
        while len(self._instances) <= i:
            idx = len(self._instances)
            core.log.info("recognizer %d: loading with %d threads", idx, self.sizes[idx])
            self._instances.append(local_stt.LocalRecognizer(self.sizes[idx]))
        return self._instances[i]

    def _pick(self) -> Optional[int]:
        """Index of the biggest idle instance, creating one if the plan allows."""
        for i in range(len(self._instances)):
            if i not in self._busy:
                return i
        if len(self._instances) < len(self.sizes):
            return len(self._instances)
        return None

    def transcribe(self, audio: np.ndarray) -> str:
        """Decode audio on a free recognizer; waits up to WAIT_S for one."""
        t0 = time.monotonic()
        with self._lock:
            deadline = t0 + WAIT_S
            while (i := self._pick()) is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("all recognizers are busy")
                self._lock.wait(remaining)
            rec = self._instance(i)
            self._busy.add(i)
            self.in_flight += 1
            self.waited_total += time.monotonic() - t0
        try:
            return rec.transcribe(audio)
        finally:
            with self._lock:
                self._busy.discard(i)
                self.in_flight -= 1
                self._lock.notify()

    def status(self) -> dict:
        with self._lock:
            return {
                "planned_threads": self.sizes,
                "loaded": len(self._instances),
                "ready": sum(1 for r in self._instances if r.is_ready()),
                "busy": len(self._busy),
            }
