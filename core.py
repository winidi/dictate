"""Shared dictation core: config, recording, transcription, typing."""
import io
import json
import subprocess
import threading
import wave
from pathlib import Path

import numpy as np
import requests
import sounddevice as sd
from pynput import keyboard

SAMPLE_RATE = 16000
CHANNELS = 1
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
DEFAULT_MODEL = "whisper-large-v3-turbo"

CONFIG_DIR = Path.home() / ".config" / "dictate"
CONFIG_PATH = CONFIG_DIR / "config.json"

KEY_MAP = {f"f{i}": {getattr(keyboard.Key, f"f{i}")} for i in range(1, 13)}
KEY_MAP.update({
    "pause":       {keyboard.Key.pause},
    "scroll_lock": {keyboard.Key.scroll_lock},
    "insert":      {keyboard.Key.insert},
    "home":        {keyboard.Key.home},
    "end":         {keyboard.Key.end},
    "page_up":     {keyboard.Key.page_up},
    "page_down":   {keyboard.Key.page_down},
    "caps_lock":   {keyboard.Key.caps_lock},
    "ctrl":        {keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r},
    "ctrl_l":      {keyboard.Key.ctrl_l},
    "ctrl_r":      {keyboard.Key.ctrl_r},
    "alt":         {keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r},
    "alt_l":       {keyboard.Key.alt_l},
    "alt_r":       {keyboard.Key.alt_r, keyboard.Key.alt_gr},
    "shift":       {keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r},
    "super":       {keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r},
})

DEFAULT_CONFIG = {
    "api_key": "",
    "mode": "ptt",
    "key": "f9",
    "model": DEFAULT_MODEL,
    "threshold": 0.0,  # seconds; long-press time before recording starts
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    try:
        CONFIG_PATH.chmod(0o600)
    except Exception:
        pass


def notify(msg, urgency="normal"):
    subprocess.run(
        ["notify-send", "-u", urgency, "-t", "1500", "Dictate", msg],
        check=False,
    )


def type_text(text):
    if not text:
        return
    subprocess.run(["xdotool", "type", "--delay", "0", "--", text], check=False)


class Recorder:
    def __init__(self):
        self.frames = []
        self.stream = None
        self.recording = False
        self.lock = threading.Lock()

    def _callback(self, indata, frames, time_info, status):
        self.frames.append(indata.copy())

    def start(self):
        with self.lock:
            if self.recording:
                return
            self.frames = []
            self.stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                callback=self._callback,
            )
            self.stream.start()
            self.recording = True

    def stop(self):
        with self.lock:
            if not self.recording:
                return None
            self.stream.stop()
            self.stream.close()
            self.stream = None
            self.recording = False
            if not self.frames:
                return None
            return np.concatenate(self.frames, axis=0)


def to_wav_bytes(audio):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())
    buf.seek(0)
    return buf


def transcribe(audio, api_key, model=DEFAULT_MODEL):
    wav = to_wav_bytes(audio)
    files = {"file": ("audio.wav", wav, "audio/wav")}
    data = {"model": model, "response_format": "text"}
    headers = {"Authorization": f"Bearer {api_key}"}
    r = requests.post(GROQ_URL, files=files, data=data, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text.strip()


def test_api_key(api_key):
    try:
        r = requests.get(
            GROQ_MODELS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5,
        )
        return r.status_code == 200
    except Exception:
        return False
