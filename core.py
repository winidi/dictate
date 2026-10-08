"""Shared dictation core: config, recording, transcription, typing."""
import io
import json
import logging
import logging.handlers
import subprocess
import threading
import wave
from pathlib import Path

import numpy as np
import requests
import sounddevice as sd
import sys
from pynput import keyboard

SAMPLE_RATE = 16000
CHANNELS = 1

PROVIDERS = {
    "groq": {
        "label":         "Groq",
        "url":           "https://api.groq.com/openai/v1/audio/transcriptions",
        "models_url":    "https://api.groq.com/openai/v1/models",
        "key_field":     "api_key",
        "key_prefix":    "gsk_",
        "default_model": "whisper-large-v3-turbo",
        "models": [
            ("whisper-large-v3-turbo (fastest)",   "whisper-large-v3-turbo"),
            ("whisper-large-v3 (most accurate)",    "whisper-large-v3"),
        ],
        "is_local":      False,
    },
    "openai": {
        "label":         "OpenAI",
        "url":           "https://api.openai.com/v1/audio/transcriptions",
        "models_url":    "https://api.openai.com/v1/models",
        "key_field":     "openai_api_key",
        "key_prefix":    "sk-",
        "default_model": "gpt-4o-mini-transcribe",
        "models": [
            ("gpt-4o-mini-transcribe (fast · cheap)", "gpt-4o-mini-transcribe"),
            ("gpt-4o-transcribe (best quality)",      "gpt-4o-transcribe"),
            ("whisper-1 (classic)",                   "whisper-1"),
        ],
        "is_local":      False,
    },
    "yorik": {
        "label":         "Yorik (home server)",
        "url":           "http://127.0.0.1:8000/v1/audio/transcriptions",
        "models_url":    None,
        "key_field":     "yorik_token",
        "key_prefix":    "yk_",
        "default_model": "parakeet",
        "models": [("Yorik's configured engine (Parakeet, CPU)", "parakeet")],
        "is_local":      False,
        "url_field":     "yorik_url",   # base URL, editable in Settings
    },
    "parakeet_local": {
        "label":         "Local (Parakeet DE, CPU offline)",
        "url":           None,
        "models_url":    None,
        "key_field":     None,
        "key_prefix":    None,
        "default_model": "parakeet-primeline-int8",
        "models": [("parakeet-primeline (German, int8)", "parakeet-primeline-int8")],
        "is_local":      True,
    },
}

# Backward-compat aliases used elsewhere in the codebase.
DEFAULT_PROVIDER = "groq"
DEFAULT_MODEL    = PROVIDERS[DEFAULT_PROVIDER]["default_model"]
GROQ_URL         = PROVIDERS["groq"]["url"]
GROQ_MODELS_URL  = PROVIDERS["groq"]["models_url"]

CONFIG_DIR = Path.home() / ".config" / "dictate"
CONFIG_PATH = CONFIG_DIR / "config.json"
LOG_PATH = Path.home() / ".local" / "state" / "dictate" / "dictate.log"

log = logging.getLogger("dictate")


def setup_logging():
    """Every recording leaves one line in LOG_PATH, so a dictation that went
    nowhere can be traced afterwards. Never logs transcribed text."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)

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
    # Mouse-side buttons. Sentinels are tuples emitted by evdev_listener;
    # pynput won't see these (mouse events go through a separate listener) —
    # so these hotkeys only work when the evdev backend is active.
    "mouse_back":    {("mouse", "back")},
    "mouse_forward": {("mouse", "forward")},
    "mouse_side":    {("mouse", "side")},
    "mouse_extra":   {("mouse", "extra")},
    "mouse_task":    {("mouse", "task")},
})

DEFAULT_CONFIG = {
    "provider":              DEFAULT_PROVIDER,
    "api_key":               "",   # Groq key (kept name for backward compat)
    "openai_api_key":        "",   # OpenAI key
    "mode":                  "ptt",
    "key":                   "f9",
    "model":                 DEFAULT_MODEL,
    "threshold":             0.0,
    "paste_shortcut":        "ctrl+v",  # Wayland; terminals may need ctrl+shift+v
    "insertion_mode":        "type",  # clipboard paste is an explicit opt-in
    "typing_layout":         "auto",  # XKB layout override, e.g. at or de(nodeadkeys)
    "local_stt_num_threads": 4,    # sherpa-onnx CPU thread count for parakeet_local
    "yorik_url":             "http://127.0.0.1:8000",  # Yorik base URL (provider "yorik")
    "yorik_token":           "",   # personal API token from Yorik Settings → API tokens
}


def provider_url(cfg, p):
    """Endpoint URL for an HTTP provider; Yorik's comes from the config."""
    if p.get("url_field"):
        base = (cfg.get(p["url_field"]) or "").strip().rstrip("/")
        if base:
            return base + "/v1/audio/transcriptions"
    return p["url"]


def provider_key(cfg):
    """Return a truthy value iff the selected provider is ready to use.

    HTTP providers need an API key. The local provider is 'ready' when the
    ONNX model files have been downloaded — no key, no network.
    """
    provider = cfg.get("provider", DEFAULT_PROVIDER)
    p = PROVIDERS.get(provider, PROVIDERS[DEFAULT_PROVIDER])
    if p.get("is_local"):
        try:
            import local_stt
            return "installed" if local_stt.model_installed() else ""
        except ImportError:
            return ""
    return cfg.get(p["key_field"], "")


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


def notify(msg, urgency="normal", timeout_ms=1500):
    try:
        subprocess.run(
            ["notify-send", "-u", urgency, "-t", str(timeout_ms), "Dictate", msg],
            check=False,
        )
    except FileNotFoundError:
        pass


_WAYLAND_INSERT_LOCK = threading.Lock()


def _ydotool_key_style():
    # ydotool 0.x (Ubuntu 24.04) takes symbolic combinations; 1.x (Arch)
    # takes press/release keycodes. Probe before changing the clipboard:
    # 1.x also fails here if its required daemon is not running.
    help_result = subprocess.run(
        ["ydotool", "key", "--help"], stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, timeout=10,
    )
    help_text = help_result.stdout.lower()
    if help_result.returncode == 0 and b"keycode" in help_text:
        return "keycodes"
    if b"separated by plus" in help_text:
        return "symbolic"
    raise RuntimeError("ydotool unavailable — check its daemon and /dev/uinput access")


def _paste_wayland(text, shortcut):
    """Optional clipboard mode for text a keyboard layout cannot represent."""
    keys = {
        "ctrl+v": [29, 47],
        "ctrl+shift+v": [29, 42, 47],
    }
    if shortcut not in keys:
        raise ValueError("Unknown paste shortcut — select one in Settings")
    if _ydotool_key_style() == "keycodes":
        codes = keys[shortcut]
        events = [f"{code}:1" for code in codes]
        events += [f"{code}:0" for code in reversed(codes)]
        paste = ["ydotool", "key", "--key-delay", "20", *events]
    else:
        paste = ["ydotool", "key", "--key-delay", "20", shortcut]

    subprocess.run(
        ["wl-copy", "--type", "text/plain;charset=utf-8"],
        input=text.encode("utf-8"), check=True, timeout=10,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(paste, check=True, timeout=10, stderr=subprocess.DEVNULL)


def _type_wayland(text, layout):
    from wayland_typing import key_chords

    style = _ydotool_key_style()
    chords = key_chords(text, layout)
    if style == "symbolic":
        # ydotool 0.x names physical keys using their US labels, even when
        # the compositor assigns a different symbol. Alt_R is the AltGr key.
        names = {15: "Tab", 28: "Enter", 42: "Shift", 100: "Alt_R",
                 41: "`", 43: "\\", 57: " "}
        for start, row in ((2, "1234567890-="), (16, "qwertyuiop[]"),
                           (30, "asdfghjkl;'"), (44, "zxcvbnm,./")):
            names.update({start + i: key for i, key in enumerate(row)})
        if any(code not in names for chord in chords for code in chord):
            raise RuntimeError("This text needs keycodes unsupported by ydotool 0.x — upgrade ydotool or select Clipboard paste")
        sequences = ["+".join(names[code] for code in chord) for chord in chords]
    else:
        sequences = [[*(f"{code}:1" for code in chord),
                      *(f"{code}:0" for code in reversed(chord))] for chord in chords]

    # Bound argv size for long dictations. Every character has its own
    # press/release sequence; no paste shortcut or clipboard is involved.
    for start in range(0, len(sequences), 128):
        batch = sequences[start:start + 128]
        args = batch if style == "symbolic" else [event for sequence in batch for event in sequence]
        subprocess.run(["ydotool", "key", "--key-delay", "2", "--", *args],
                       check=True, timeout=10, stderr=subprocess.DEVNULL)


def type_text(text, paste_shortcut="ctrl+v", insertion_mode="type", typing_layout="auto"):
    if not text:
        return
    import os
    if os.name == "nt" or sys.platform == "darwin":
        # Windows / macOS: no xdotool or ydotool; pynput types through the
        # OS input API and respects the active keyboard layout.
        keyboard.Controller().type(text)
        return
    on_x11 = os.environ.get("XDG_SESSION_TYPE") == "x11"
    on_wayland = os.environ.get("XDG_SESSION_TYPE") == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY"))
    try:
        # X11's xdotool respects XKB. Wayland types individual key events
        # matched to XKB; clipboard paste is only used when explicitly chosen.
        if on_wayland and not on_x11:
            with _WAYLAND_INSERT_LOCK:
                if insertion_mode == "type":
                    _type_wayland(text, typing_layout)
                elif insertion_mode == "paste":
                    _paste_wayland(text, paste_shortcut)
                else:
                    raise ValueError("Unknown insertion mode — select one in Settings")
            return
        subprocess.run(["xdotool", "type", "--delay", "0", "--", text],
                       check=True, timeout=30)
    except FileNotFoundError as e:
        log.error("typing tool missing: %s", e.filename)
        notify(f"Typing failed: install {e.filename}", urgency="critical", timeout_ms=5000)
    except subprocess.CalledProcessError as e:
        tool = e.cmd[0]
        log.error("%s exited with %s — text not inserted", tool, e.returncode)
        notify(f"Typing failed ({tool} exit {e.returncode})", urgency="critical", timeout_ms=5000)
    except (subprocess.TimeoutExpired, RuntimeError, ValueError) as e:
        # Do not log TimeoutExpired/CalledProcessError verbatim: their command
        # may contain the transcription on X11.
        message = "Typing timed out" if isinstance(e, subprocess.TimeoutExpired) else str(e)
        log.error("%s", message)
        notify(message, urgency="critical", timeout_ms=5000)


class Recorder:
    """Each recording is a "take" (stream + its frames). detach() hands the
    current take to the caller without touching PipeWire, so a new start()
    can never collide with a take that is still being finished on a worker
    thread (trailing buffer + slow PipeWire close)."""

    def __init__(self):
        self._take = None
        self.lock = threading.Lock()

    @property
    def recording(self):
        return self._take is not None

    def start(self):
        with self.lock:
            if self._take is not None:
                return
            frames = []

            def callback(indata, n, time_info, status):
                frames.append(indata.copy())

            stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                callback=callback,
            )
            stream.start()
            self._take = (stream, frames)

    def detach(self):
        """Non-blocking: give up the current take (or None). Safe on the GUI thread."""
        with self.lock:
            take, self._take = self._take, None
            return take

    @staticmethod
    def finish(take):
        """Close a detached take and return its audio, or None if it has none.
        Blocks for hundreds of ms on PipeWire close — call from a worker."""
        if take is None:
            return None
        stream, frames = take
        try:
            stream.stop()
            stream.close()
        except Exception:
            # A vanished mic must not lose what was already captured.
            log.exception("closing input stream failed")
        if not frames:
            return None
        return np.concatenate(frames, axis=0)

    def stop(self):
        return self.finish(self.detach())


def to_wav_bytes(audio):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())
    buf.seek(0)
    return buf


FAILED_DIR = LOG_PATH.parent / "failed"
FAILED_KEEP = 3


def keep_failed_audio(audio):
    """Save a recording that held sound but came back empty, so the failure
    can be replayed. Only the newest FAILED_KEEP files are kept."""
    try:
        import time
        FAILED_DIR.mkdir(parents=True, exist_ok=True)
        path = FAILED_DIR / time.strftime("%Y%m%d-%H%M%S.wav")
        path.write_bytes(to_wav_bytes(audio).getvalue())
        for old in sorted(FAILED_DIR.glob("*.wav"))[:-FAILED_KEEP]:
            old.unlink()
        log.info("kept failed recording as %s", path)
    except Exception:
        log.exception("could not keep failed recording")


def transcribe(audio, cfg):
    provider = cfg.get("provider", DEFAULT_PROVIDER)
    p = PROVIDERS.get(provider, PROVIDERS[DEFAULT_PROVIDER])
    if p.get("is_local"):
        import local_stt
        threads = int(cfg.get("local_stt_num_threads", local_stt.DEFAULT_THREADS))
        return local_stt.get_recognizer(num_threads=threads).transcribe(audio)
    # HTTP providers
    key = cfg.get(p["key_field"], "")
    model = cfg.get("model") or p["default_model"]
    wav = to_wav_bytes(audio)
    files = {"file": ("audio.wav", wav, "audio/wav")}
    data = {"model": model, "response_format": "text"}
    headers = {"Authorization": f"Bearer {key}"}
    r = requests.post(provider_url(cfg, p), files=files, data=data, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text.strip()


def test_api_key(api_key, provider=DEFAULT_PROVIDER, cfg=None):
    p = PROVIDERS.get(provider, PROVIDERS[DEFAULT_PROVIDER])
    if provider == "yorik":
        # A token is valid when Yorik lists the caller's tokens with it.
        base = ((cfg or {}).get("yorik_url") or DEFAULT_CONFIG["yorik_url"]).strip().rstrip("/")
        try:
            r = requests.get(base + "/api/tokens", headers={"Authorization": f"Bearer {api_key}"}, timeout=10)
            return r.status_code == 200
        except Exception:
            return False
    if p.get("is_local") or not p.get("models_url"):
        return False  # no key to test — caller should not have called this
    try:
        r = requests.get(
            p["models_url"],
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5,
        )
        return r.status_code == 200
    except Exception:
        return False
